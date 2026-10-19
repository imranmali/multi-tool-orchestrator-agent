"""Capability-based routing with pluggable ranking, health tracking and circuit breaking.

The router answers "which registered tool should serve capability X?". It ranks the
enabled tools offering a capability, drops those the caller excludes or deems
ineligible, skips tools whose circuit breaker is open, and returns the best one.

Ranking keys are always *higher is better*; ties break by tool name ascending.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol, Union

from .core import Capability, NoRouteError, ToolSpec, ValidationError
from .registry import ToolRegistry

__all__ = [
    "BALANCED_WEIGHTS",
    "CLOSED",
    "HALF_OPEN",
    "OPEN",
    "STRATEGIES",
    "CapabilityRouter",
    "RouteExplanation",
    "RoutingStrategy",
    "ToolStats",
]

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half-open"

REASON_EXCLUDED = "excluded"
REASON_NOT_ELIGIBLE = "not eligible"
REASON_CIRCUIT_OPEN = "circuit open"

#: Weights of the ``balanced`` strategy's normalised components (sum to 1.0).
BALANCED_WEIGHTS: dict[str, float] = {
    "priority": 0.4,
    "cost": 0.2,
    "latency": 0.2,
    "health": 0.2,
}


class RoutingStrategy(Protocol):
    """A custom scoring function: higher scores rank first."""

    def __call__(self, spec: ToolSpec, capability: Capability, stats: ToolStats) -> float: ...


StrategyLike = Union[str, RoutingStrategy]
Eligible = Callable[[ToolSpec], bool]


@dataclass(frozen=True)
class ToolStats:
    """Immutable snapshot of a tool's observed health."""

    name: str
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    avg_latency: float | None = None
    circuit: str = CLOSED

    @property
    def calls(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float:
        """Fraction of successful calls; 1.0 when nothing has been observed."""
        total = self.calls
        return 1.0 if total == 0 else self.successes / total


@dataclass(frozen=True)
class RouteExplanation:
    """Why a tool was ranked where it was, or why it was rejected."""

    tool: str
    version: str
    rank: int | None
    score: float | None
    key: tuple[float, ...] | None
    reason: str | None
    stats: ToolStats

    @property
    def selected(self) -> bool:
        return self.rank == 1


@dataclass
class _Health:
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    latency_total: float = 0.0
    latency_samples: int = 0
    opened_at: float | None = None


_Entry = tuple[ToolSpec, Capability, ToolStats]
_Ranker = Callable[[list[_Entry]], list[tuple[float, ...]]]


def _per_entry(fn: Callable[[ToolSpec, Capability, ToolStats], tuple[float, ...]]) -> _Ranker:
    return lambda entries: [fn(spec, cap, st) for spec, cap, st in entries]


def _balanced(entries: list[_Entry]) -> list[tuple[float, ...]]:
    caps = [cap for _, cap, _ in entries]
    p_min = min(c.priority for c in caps)
    p_max = max(c.priority for c in caps)
    cost_max = max(c.cost for c in caps)
    lat_max = max(c.latency for c in caps)
    w = BALANCED_WEIGHTS
    keys = []
    for _, cap, st in entries:
        prio_n = (cap.priority - p_min) / (p_max - p_min) if p_max > p_min else 1.0
        cheap_n = _inverse_norm(cap.cost, cost_max)
        fast_n = _inverse_norm(cap.latency, lat_max)
        score = (
            w["priority"] * prio_n
            + w["cost"] * cheap_n
            + w["latency"] * fast_n
            + w["health"] * st.success_rate
        )
        keys.append((score,))
    return keys


def _inverse_norm(value: float, maximum: float) -> float:
    """``1 - value / maximum`` in [0, 1]; never NaN, even for infinite hints."""
    if maximum <= 0:
        return 1.0
    if math.isinf(maximum):
        # inf/inf is NaN: an infinite hint is the worst possible, finite ones the best.
        return 0.0 if math.isinf(value) else 1.0
    return 1.0 - value / maximum


_BUILTIN: dict[str, _Ranker] = {
    "priority": _per_entry(lambda s, c, st: (float(c.priority), -c.cost, -c.latency)),
    "cheapest": _per_entry(lambda s, c, st: (-c.cost, float(c.priority), -c.latency)),
    "fastest": _per_entry(lambda s, c, st: (-c.latency, float(c.priority), -c.cost)),
    "balanced": _balanced,
}

#: Names of the built-in strategies.
STRATEGIES: frozenset[str] = frozenset(_BUILTIN)


def _custom(fn: RoutingStrategy) -> _Ranker:
    def rank(entries: list[_Entry]) -> list[tuple[float, ...]]:
        keys = []
        for spec, cap, st in entries:
            score = fn(spec, cap, st)
            if isinstance(score, bool) or not isinstance(score, (int, float)) or math.isnan(score):
                raise ValidationError(
                    f"routing strategy returned non-numeric score {score!r} for tool {spec.name!r}"
                )
            keys.append((float(score),))
        return keys

    return rank


def _resolve(strategy: StrategyLike) -> _Ranker:
    if isinstance(strategy, str):
        try:
            return _BUILTIN[strategy]
        except KeyError:
            known = ", ".join(sorted(STRATEGIES))
            raise ValidationError(
                f"unknown routing strategy {strategy!r}; expected one of {known} or a callable"
            ) from None
    if callable(strategy):
        return _custom(strategy)
    raise ValidationError(f"routing strategy must be a name or a callable, got {strategy!r}")


class CapabilityRouter:
    """Picks the registered tool that should serve a requested capability."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        strategy: StrategyLike = "balanced",
        failure_threshold: int = 3,
        cooldown: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(failure_threshold, bool) or not isinstance(failure_threshold, int):
            raise ValidationError("failure_threshold must be an int")
        if failure_threshold < 1:
            raise ValidationError("failure_threshold must be >= 1")
        if cooldown < 0:
            raise ValidationError("cooldown must be >= 0")
        self._registry = registry
        self._ranker = _resolve(strategy)
        self._strategy = strategy
        self._threshold = failure_threshold
        self._cooldown = float(cooldown)
        self._clock = clock
        self._lock = threading.Lock()
        self._health: dict[str, _Health] = {}

    # -- configuration ------------------------------------------------------

    @property
    def strategy(self) -> StrategyLike:
        return self._strategy

    @property
    def failure_threshold(self) -> int:
        return self._threshold

    @property
    def cooldown(self) -> float:
        return self._cooldown

    # -- health -------------------------------------------------------------

    def record_success(self, name: str, latency: float | None = None) -> None:
        """Report a successful invocation; closes the tool's circuit."""
        if latency is not None and (latency < 0 or math.isnan(latency)):
            raise ValidationError("latency must be >= 0")
        with self._lock:
            h = self._health.setdefault(name, _Health())
            h.successes += 1
            h.consecutive_failures = 0
            h.opened_at = None
            if latency is not None:
                h.latency_total += latency
                h.latency_samples += 1

    def record_failure(self, name: str) -> None:
        """Report a failed invocation; may open (or reopen) the tool's circuit.

        The cooldown (re)starts when the threshold is reached from the closed
        state or when a half-open tool fails. A failure reported while the
        circuit is already open (e.g. a straggling in-flight call) is counted
        but does not push the cooldown back.
        """
        with self._lock:
            now = self._clock()
            h = self._health.setdefault(name, _Health())
            h.failures += 1
            h.consecutive_failures += 1
            if h.opened_at is None:
                if h.consecutive_failures >= self._threshold:
                    h.opened_at = now
            elif now - h.opened_at >= self._cooldown:  # half-open: reopen
                h.opened_at = now

    def stats(self, name: str) -> ToolStats:
        with self._lock:
            return self._snapshot(name, self._clock())

    def circuit_state(self, name: str) -> str:
        return self.stats(name).circuit

    def reset(self, name: str | None = None) -> None:
        """Forget health for one tool, or for every tool when ``name`` is None."""
        with self._lock:
            if name is None:
                self._health.clear()
            else:
                self._health.pop(name, None)

    def _snapshot(self, name: str, now: float) -> ToolStats:
        h = self._health.get(name)
        if h is None:
            return ToolStats(name=name)
        if h.opened_at is None:
            circuit = CLOSED
        elif now - h.opened_at < self._cooldown:
            circuit = OPEN
        else:
            circuit = HALF_OPEN
        return ToolStats(
            name=name,
            successes=h.successes,
            failures=h.failures,
            consecutive_failures=h.consecutive_failures,
            avg_latency=h.latency_total / h.latency_samples if h.latency_samples else None,
            circuit=circuit,
        )

    # -- routing ------------------------------------------------------------

    def candidates(
        self,
        capability: str,
        *,
        eligible: Eligible | None = None,
        exclude: Iterable[str] = (),
        strategy: StrategyLike | None = None,
    ) -> list[ToolSpec]:
        """Usable tools offering ``capability``, best first."""
        ranked, _, _ = self._evaluate(capability, eligible, exclude, strategy)
        return [spec for spec, _, _, _ in ranked]

    def route(
        self,
        capability: str,
        *,
        eligible: Eligible | None = None,
        exclude: Iterable[str] = (),
        strategy: StrategyLike | None = None,
    ) -> ToolSpec:
        """The best usable tool for ``capability``; raises NoRouteError if none."""
        ranked, rejected, offered = self._evaluate(capability, eligible, exclude, strategy)
        if ranked:
            return ranked[0][0]
        if not offered:
            raise NoRouteError(f"no enabled tool offers capability {capability!r}")
        details = ", ".join(f"{spec.name} ({reason})" for spec, reason, _ in rejected)
        noun = "tool offers" if offered == 1 else "tools offer"
        raise NoRouteError(
            f"{offered} {noun} capability {capability!r} but none are usable: {details}"
        )

    def route_many(
        self,
        capabilities: Iterable[str],
        *,
        eligible: Eligible | None = None,
        exclude: Iterable[str] = (),
        strategy: StrategyLike | None = None,
    ) -> dict[str, ToolSpec]:
        """Route each capability (duplicates collapse); fails on the first unroutable one."""
        excluded = frozenset(exclude)
        result: dict[str, ToolSpec] = {}
        for cap in capabilities:
            if cap not in result:
                result[cap] = self.route(
                    cap, eligible=eligible, exclude=excluded, strategy=strategy
                )
        return result

    def explain(
        self,
        capability: str,
        *,
        eligible: Eligible | None = None,
        exclude: Iterable[str] = (),
        strategy: StrategyLike | None = None,
    ) -> list[RouteExplanation]:
        """Ranked candidates (rank 1 = selected) followed by rejected tools, by name."""
        ranked, rejected, _ = self._evaluate(capability, eligible, exclude, strategy)
        out = [
            RouteExplanation(spec.name, spec.version, i, key[0], key, None, st)
            for i, (spec, _, st, key) in enumerate(ranked, start=1)
        ]
        out.extend(
            RouteExplanation(spec.name, spec.version, None, None, None, reason, st)
            for spec, reason, st in rejected
        )
        return out

    def _evaluate(
        self,
        capability: str,
        eligible: Eligible | None,
        exclude: Iterable[str],
        strategy: StrategyLike | None,
    ) -> tuple[
        list[tuple[ToolSpec, Capability, ToolStats, tuple[float, ...]]],
        list[tuple[ToolSpec, str, ToolStats]],
        int,
    ]:
        ranker = self._ranker if strategy is None else _resolve(strategy)
        excluded = {exclude} if isinstance(exclude, str) else set(exclude)
        offered = self._registry.find_by_capability(capability)
        with self._lock:
            now = self._clock()
            snapshots = {spec.name: self._snapshot(spec.name, now) for spec in offered}

        entries: list[_Entry] = []
        rejected: list[tuple[ToolSpec, str, ToolStats]] = []
        for spec in offered:
            st = snapshots[spec.name]
            if spec.name in excluded:
                reason = REASON_EXCLUDED
            elif eligible is not None and not eligible(spec):
                reason = REASON_NOT_ELIGIBLE
            elif st.circuit == OPEN:
                reason = REASON_CIRCUIT_OPEN
            else:
                cap = spec.capability(capability)
                assert cap is not None  # guaranteed by find_by_capability
                entries.append((spec, cap, st))
                continue
            rejected.append((spec, reason, st))

        keys = ranker(entries) if entries else []
        order = sorted(
            range(len(entries)),
            key=lambda i: (tuple(-k for k in keys[i]), entries[i][0].name),
        )
        ranked = [(*entries[i], keys[i]) for i in order]
        rejected.sort(key=lambda r: r[0].name)
        return ranked, rejected, len(offered)
