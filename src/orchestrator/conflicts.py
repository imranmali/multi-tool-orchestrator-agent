"""Conflict resolution: reconciling divergent tool results and locking shared resources.

Two independent facilities live here:

* :class:`ConflictResolver` turns several :class:`~orchestrator.core.ToolResult` objects
  produced for the same request (fan-out) into one :class:`Resolution`.
* :class:`ResourceLockManager` provides FIFO-fair readers-writer locks over named
  resources for concurrently running plan steps.

This module depends only on :mod:`orchestrator.core` and the standard library.
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .core import ConflictError, ToolResult, ValidationError

__all__ = [
    "Resolution",
    "ConflictResolver",
    "ResourceLockManager",
    "detect_write_conflicts",
    "LOCK_MODES",
]

StrategyFn = Callable[..., Any]

# --------------------------------------------------------------------------- helpers

_MISSING = object()


def _canonical(value: Any) -> str | None:
    """Order-insensitive canonical form of a JSON-like value, or None if unserialisable."""
    try:
        return json.dumps(value, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        return None


def _same(a: Any, b: Any, ka: Any = _MISSING, kb: Any = _MISSING) -> bool:
    """Structural equality: canonical JSON when available, falling back to ``==``."""
    if ka is _MISSING:
        ka = _canonical(a)
    if kb is _MISSING:
        kb = _canonical(b)
    if ka is not None and kb is not None:
        return ka == kb
    try:
        return bool(a == b)
    except Exception:  # pragma: no cover - exotic __eq__
        return False


def _fmt_path(path: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in path) if path else "<root>"


@dataclass(frozen=True)
class Resolution:
    """Outcome of reconciling several tool results."""

    value: Any
    strategy: str
    winners: tuple[str, ...]
    dissenters: tuple[str, ...]
    confidence: float
    details: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- resolver


class ConflictResolver:
    """Reconciles successful results from several tools answering the same request.

    Built-in strategies: ``first_success``, ``majority`` (option ``quorum``),
    ``priority``, ``merge`` (option ``on_conflict`` in ``error``/``priority``/``first``)
    and ``numeric`` (option ``agg`` in ``mean``/``median``/``min``/``max``).

    A strategy is a callable ``fn(results, **options)`` receiving only the successful
    results (in supplied order) and returning a :class:`Resolution` or a plain value;
    plain values are wrapped with winners = participants whose value equals it.
    """

    BUILTIN_STRATEGIES = ("first_success", "majority", "priority", "merge", "numeric")

    def __init__(
        self,
        default_strategy: str = "majority",
        *,
        priorities: Mapping[str, int] | None = None,
    ) -> None:
        self._priorities: dict[str, int] = dict(priorities or {})
        self._strategies: dict[str, StrategyFn] = {
            "first_success": self._first_success,
            "majority": self._majority,
            "priority": self._priority,
            "merge": self._merge,
            "numeric": self._numeric,
        }
        if not isinstance(default_strategy, str) or default_strategy not in self._strategies:
            raise ValidationError(f"unknown default strategy {default_strategy!r}")
        self.default_strategy = default_strategy

    # -- public API ---------------------------------------------------------------

    @property
    def strategies(self) -> tuple[str, ...]:
        """Names of all built-in and registered strategies."""
        return tuple(sorted(self._strategies))

    def register_strategy(self, name: str, fn: StrategyFn, *, replace: bool = False) -> None:
        """Register ``fn(results, **options)`` under ``name``."""
        if not isinstance(name, str) or not name:
            raise ValidationError("strategy name must be a non-empty string")
        if not callable(fn):
            raise ValidationError(f"strategy {name!r} must be callable")
        if name in self._strategies and not replace:
            raise ValidationError(f"strategy {name!r} is already registered")
        self._strategies[name] = fn

    def priority_of(self, result: ToolResult) -> int:
        """Configured priority by tool name, else ``metadata['priority']``, else 0."""
        if result.tool in self._priorities:
            return self._priorities[result.tool]
        meta = result.metadata or {}
        try:
            return int(meta.get("priority", 0))
        except (TypeError, ValueError):
            return 0

    def resolve(
        self,
        results: Sequence[ToolResult],
        strategy: str | StrategyFn | None = None,
        **options: Any,
    ) -> Resolution:
        """Reconcile ``results`` with ``strategy`` (default strategy when None)."""
        participants = [r for r in results if r.ok]
        if not participants:
            failures = ", ".join(f"{r.tool}: {r.error or 'unknown error'}" for r in results)
            raise ConflictError(
                "no successful results to reconcile" + (f" ({failures})" if failures else "")
            )

        if strategy is None:
            strategy = self.default_strategy
        if isinstance(strategy, str):
            name = strategy
            fn = self._strategies.get(name)
            if fn is None:
                raise ValidationError(
                    f"unknown strategy {name!r}; known: {', '.join(self.strategies)}"
                )
        elif callable(strategy):
            fn = strategy
            name = getattr(strategy, "__name__", None) or "custom"
            if name == "<lambda>":
                name = "custom"
        else:
            raise ValidationError(f"strategy must be a name or callable, got {strategy!r}")

        outcome = fn(participants, **options)
        if isinstance(outcome, Resolution):
            return outcome
        return self._wrap(name, outcome, participants)

    # -- shared building blocks ----------------------------------------------------

    @staticmethod
    def _wrap(
        name: str,
        value: Any,
        participants: Sequence[ToolResult],
        details: Mapping[str, Any] | None = None,
    ) -> Resolution:
        key = _canonical(value)
        winners = tuple(r.tool for r in participants if _same(r.value, value, kb=key))
        dissenters = tuple(r.tool for r in participants if r.tool not in winners)
        return Resolution(
            value=value,
            strategy=name,
            winners=winners,
            dissenters=dissenters,
            confidence=len(winners) / len(participants),
            details=dict(details or {}),
        )

    @staticmethod
    def _check_options(strategy: str, options: Mapping[str, Any], allowed: Iterable[str]) -> None:
        unknown = sorted(set(options) - set(allowed))
        if unknown:
            raise ValidationError(
                f"strategy {strategy!r} does not accept option(s) {', '.join(unknown)}"
            )

    def _rank(self, result: ToolResult) -> tuple[int, str]:
        """Sort key: highest priority first, then alphabetical tool name."""
        return (-self.priority_of(result), result.tool)

    # -- built-in strategies --------------------------------------------------------

    def _first_success(self, results: Sequence[ToolResult], **options: Any) -> Resolution:
        self._check_options("first_success", options, ())
        return self._wrap("first_success", results[0].value, results)

    def _priority(self, results: Sequence[ToolResult], **options: Any) -> Resolution:
        self._check_options("priority", options, ())
        best = min(results, key=self._rank)
        return self._wrap(
            "priority",
            best.value,
            results,
            {"selected": best.tool, "priority": self.priority_of(best)},
        )

    def _majority(
        self, results: Sequence[ToolResult], quorum: float | None = None, **options: Any
    ) -> Resolution:
        self._check_options("majority", options, ("quorum",))
        if quorum is not None:
            if (
                isinstance(quorum, bool)
                or not isinstance(quorum, (int, float))
                or not 0 < quorum <= 1
            ):
                raise ValidationError(f"quorum must be in (0, 1], got {quorum!r}")

        groups: list[tuple[Any, str | None, list[ToolResult]]] = []
        for result in results:
            key = _canonical(result.value)
            for value, gkey, members in groups:
                if _same(result.value, value, ka=key, kb=gkey):
                    members.append(result)
                    break
            else:
                groups.append((result.value, key, [result]))

        def group_rank(group: tuple[Any, str | None, list[ToolResult]]) -> tuple[int, int, str]:
            members = group[2]
            top = min(members, key=self._rank)
            return (-len(members), -self.priority_of(top), top.tool)

        ordered = sorted(groups, key=group_rank)
        value, _, members = ordered[0]
        share = len(members) / len(results)
        tied = len(ordered) > 1 and len(ordered[1][2]) == len(members)
        details = {
            "groups": [[r.tool for r in g[2]] for g in ordered],
            "tie_broken": tied,
            "quorum": quorum,
        }
        if quorum is not None and share < quorum:
            raise ConflictError(
                f"majority quorum {quorum} not met: largest group "
                f"{[r.tool for r in members]} has {len(members)}/{len(results)}"
            )
        return self._wrap("majority", value, results, details)

    def _merge(
        self, results: Sequence[ToolResult], on_conflict: str = "error", **options: Any
    ) -> Resolution:
        self._check_options("merge", options, ("on_conflict",))
        if on_conflict not in ("error", "priority", "first"):
            raise ValidationError(
                f"on_conflict must be 'error', 'priority' or 'first', got {on_conflict!r}"
            )
        values = [r.value for r in results]
        if not all(isinstance(v, Mapping) for v in values):
            first = values[0]
            if all(_same(first, v) for v in values[1:]):
                return self._wrap("merge", first, results, {"conflicts": [], "on_conflict": on_conflict})
            raise ConflictError(
                "merge requires dictionary values unless all values are equal; got "
                + ", ".join(f"{r.tool}: {type(r.value).__name__}" for r in results)
            )

        conflicts: list[dict[str, Any]] = []
        dissenters: set[str] = set()

        def merge_at(path: tuple[Any, ...], contribs: list[tuple[ToolResult, Any]]) -> Any:
            vals = [v for _, v in contribs]
            if all(isinstance(v, Mapping) for v in vals):
                keys: list[Any] = []
                for v in vals:
                    for k in v:
                        if k not in keys:
                            keys.append(k)
                return {
                    k: merge_at(path + (k,), [(r, v[k]) for r, v in contribs if k in v])
                    for k in keys
                }
            if all(isinstance(v, list) for v in vals):
                merged: list[Any] = []
                seen: list[str | None] = []
                for v in vals:
                    for item in v:
                        ikey = _canonical(item)
                        if not any(
                            _same(item, m, ka=ikey, kb=mk) for m, mk in zip(merged, seen)
                        ):
                            merged.append(item)
                            seen.append(ikey)
                return merged
            first_key = _canonical(vals[0])
            if all(_same(vals[0], v, ka=first_key) for v in vals[1:]):
                return vals[0]
            # Genuine conflict at this path.
            if on_conflict == "first":
                chosen_result, chosen = contribs[0]
            elif on_conflict == "priority":
                chosen_result, chosen = min(contribs, key=lambda rv: self._rank(rv[0]))
            else:
                chosen_result, chosen = contribs[0]  # placeholder; error raised later
            ckey = _canonical(chosen)
            for r, v in contribs:
                if not _same(v, chosen, kb=ckey):
                    dissenters.add(r.tool)
            conflicts.append(
                {
                    "path": _fmt_path(path),
                    "keys": path,
                    "values": {r.tool: v for r, v in contribs},
                    "chosen": None if on_conflict == "error" else chosen_result.tool,
                }
            )
            return chosen

        merged_value = merge_at((), [(r, r.value) for r in results])
        if conflicts and on_conflict == "error":
            described = "; ".join(
                f"{c['path']} ({', '.join(sorted(c['values']))})" for c in conflicts
            )
            raise ConflictError(f"merge conflict at {described}")

        winners = tuple(r.tool for r in results if r.tool not in dissenters)
        return Resolution(
            value=merged_value,
            strategy="merge",
            winners=winners,
            dissenters=tuple(r.tool for r in results if r.tool in dissenters),
            confidence=len(winners) / len(results),
            details={
                "conflicts": conflicts,
                "conflicting_paths": [c["path"] for c in conflicts],
                "on_conflict": on_conflict,
            },
        )

    def _numeric(self, results: Sequence[ToolResult], agg: str = "mean", **options: Any) -> Resolution:
        self._check_options("numeric", options, ("agg",))
        aggregators: dict[str, Callable[[list[float]], float]] = {
            "mean": statistics.fmean,
            "median": statistics.median,
            "min": min,
            "max": max,
        }
        if agg not in aggregators:
            raise ValidationError(f"agg must be one of {', '.join(aggregators)}, got {agg!r}")
        bad = [
            r.tool
            for r in results
            if isinstance(r.value, bool)
            or not isinstance(r.value, (int, float))
            or (isinstance(r.value, float) and math.isnan(r.value))
        ]
        if bad:
            raise ConflictError(f"numeric strategy got non-numeric values from {', '.join(bad)}")
        nums = [r.value for r in results]
        value = aggregators[agg](nums)
        if isinstance(value, float) and value.is_integer() and all(isinstance(n, int) for n in nums):
            value = int(value)
        low, high = min(nums), max(nums)
        spread = high - low
        scale = max(abs(low), abs(high))
        confidence = 1.0 if spread == 0 else max(0.0, min(1.0, 1 - spread / scale)) if scale else 1.0
        return Resolution(
            value=value,
            strategy="numeric",
            winners=tuple(r.tool for r in results),
            dissenters=(),
            confidence=confidence,
            details={"agg": agg, "values": {r.tool: r.value for r in results}, "spread": spread},
        )


# --------------------------------------------------------------------------- resource locks

LOCK_MODES = ("read", "write")


def _validate_mode(resource: Any, mode: Any) -> None:
    if not isinstance(resource, str) or not resource:
        raise ValidationError(f"resource name must be a non-empty string, got {resource!r}")
    if mode not in LOCK_MODES:
        raise ValidationError(
            f"invalid lock mode {mode!r} for resource {resource!r}; expected 'read' or 'write'"
        )


def _compatible(mode: str, holders: Mapping[str, str]) -> bool:
    if mode == "write":
        return not holders
    return all(m == "read" for m in holders.values())


def detect_write_conflicts(
    claims: Mapping[str, Mapping[str, str]],
) -> list[tuple[str, str, str]]:
    """Return ``(resource, owner_a, owner_b)`` for every conflicting pair of claims.

    Two claims on the same resource conflict when at least one is ``write``. Owner
    names in each triple are alphabetical; the list is sorted by resource then owners.
    """
    by_resource: dict[str, dict[str, str]] = {}
    for owner, resources in claims.items():
        for resource, mode in resources.items():
            _validate_mode(resource, mode)
            by_resource.setdefault(resource, {})[owner] = mode
    out: list[tuple[str, str, str]] = []
    for resource, owners in by_resource.items():
        names = sorted(owners)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                if owners[a] == "write" or owners[b] == "write":
                    out.append((resource, a, b))
    out.sort()
    return out


@dataclass
class _Waiter:
    owner: str
    mode: str
    future: asyncio.Future


@dataclass
class _ResourceState:
    holders: dict[str, str] = field(default_factory=dict)
    queue: deque = field(default_factory=deque)


class ResourceLockManager:
    """FIFO-fair readers-writer locks over named resources.

    * Many readers OR one writer may hold a resource.
    * Requests on a resource are granted strictly in arrival order; consecutive
      readers at the head of the queue are granted together, and a waiting writer
      blocks readers that arrive after it (no writer starvation).
    * Multi-resource requests acquire resources in sorted name order, so opposite
      request orderings cannot deadlock.
    * Locks are not re-entrant: requesting a resource the owner already holds or
      waits for raises :class:`ConflictError` immediately.
    * Resources are released on normal exit, exceptions and cancellation; a request
      cancelled or timed out while waiting leaves nothing held or queued.

    A manager is bound to the event loop it is used from and is not thread-safe.
    """

    def __init__(self) -> None:
        self._states: dict[str, _ResourceState] = {}

    # -- introspection ---------------------------------------------------------------

    def holders(self, resource: str) -> dict[str, str]:
        state = self._states.get(resource)
        return dict(state.holders) if state else {}

    def waiting(self, resource: str) -> int:
        state = self._states.get(resource)
        if not state:
            return 0
        return sum(1 for w in state.queue if not w.future.done())

    detect_write_conflicts = staticmethod(detect_write_conflicts)

    # -- locking ---------------------------------------------------------------------

    def hold(
        self,
        owner: str,
        resources: Mapping[str, str],
        *,
        timeout: float | None = None,
    ) -> "_Hold":
        """Async context manager holding ``resources`` (name -> 'read'|'write') for ``owner``."""
        if not isinstance(owner, str) or not owner:
            raise ValidationError(f"owner must be a non-empty string, got {owner!r}")
        for resource, mode in resources.items():
            _validate_mode(resource, mode)
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
                raise ValidationError(f"timeout must be a non-negative number, got {timeout!r}")
        plan = tuple(sorted(resources.items()))
        return _Hold(self, owner, plan, timeout)

    def _state(self, resource: str) -> _ResourceState:
        state = self._states.get(resource)
        if state is None:
            state = self._states[resource] = _ResourceState()
        return state

    def _gc(self, resource: str) -> None:
        state = self._states.get(resource)
        if state is not None and not state.holders and not state.queue:
            del self._states[resource]

    def _grant(self, resource: str) -> None:
        state = self._states.get(resource)
        if state is None:
            return
        queue = state.queue
        while queue:
            head = queue[0]
            if head.future.done():  # cancelled or otherwise abandoned
                queue.popleft()
                continue
            if not _compatible(head.mode, state.holders):
                break
            queue.popleft()
            state.holders[head.owner] = head.mode
            head.future.set_result(True)
        self._gc(resource)

    def _release(self, owner: str, resources: Iterable[str]) -> None:
        for resource in resources:
            state = self._states.get(resource)
            if state is not None:
                state.holders.pop(owner, None)
            self._grant(resource)

    async def _acquire_one(self, owner: str, resource: str, mode: str) -> None:
        state = self._state(resource)
        if owner in state.holders or any(
            w.owner == owner and not w.future.done() for w in state.queue
        ):
            self._gc(resource)
            raise ConflictError(
                f"owner {owner!r} already holds or awaits resource {resource!r}; "
                "locks are not re-entrant"
            )
        if not state.queue and _compatible(mode, state.holders):
            state.holders[owner] = mode
            return
        waiter = _Waiter(owner, mode, asyncio.get_running_loop().create_future())
        state.queue.append(waiter)
        try:
            await waiter.future
        except BaseException:
            if waiter.future.done() and not waiter.future.cancelled():
                # Granted in the same tick as the cancellation/timeout: give it back.
                state.holders.pop(owner, None)
            else:
                waiter.future.cancel()
                try:
                    state.queue.remove(waiter)
                except ValueError:
                    pass
            self._grant(resource)
            raise

    async def _acquire_all(self, owner: str, plan: tuple[tuple[str, str], ...], acquired: list[str]) -> None:
        for resource, mode in plan:
            await self._acquire_one(owner, resource, mode)
            acquired.append(resource)


class _Hold:
    """Single-use async context manager returned by :meth:`ResourceLockManager.hold`."""

    def __init__(
        self,
        manager: ResourceLockManager,
        owner: str,
        plan: tuple[tuple[str, str], ...],
        timeout: float | None,
    ) -> None:
        self._manager = manager
        self._owner = owner
        self._plan = plan
        self._timeout = timeout
        self._acquired: list[str] = []
        self._entered = False

    @property
    def resources(self) -> dict[str, str]:
        return dict(self._plan)

    async def __aenter__(self) -> "_Hold":
        if self._entered:
            raise ConflictError("a hold can only be entered once")
        self._entered = True
        try:
            if self._timeout is None:
                await self._manager._acquire_all(self._owner, self._plan, self._acquired)
            else:
                async with asyncio.timeout(self._timeout):
                    await self._manager._acquire_all(self._owner, self._plan, self._acquired)
        except TimeoutError:
            self._release()
            raise ConflictError(
                f"owner {self._owner!r} could not acquire "
                f"{', '.join(f'{r}:{m}' for r, m in self._plan)} within {self._timeout}s"
            ) from None
        except BaseException:
            self._release()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._release()

    def _release(self) -> None:
        acquired, self._acquired = self._acquired, []
        self._manager._release(self._owner, acquired)
