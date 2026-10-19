"""Dependency-aware parallel execution of multi-step tool plans.

A :class:`Plan` is a DAG of :class:`Step` objects. :class:`ParallelExecutor`
starts every step as soon as all of its dependencies have succeeded, bounded
by a concurrency limit, with per-step timeouts, retries, failure propagation,
optional resource locking and observability events.

The executor never routes, checks permissions or resolves conflicts itself:
those concerns are plugged in through the :data:`Invoker` callable and the
:class:`LockManager` protocol by the top-level orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncContextManager,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Literal,
    Mapping,
    Protocol,
    Sequence,
)

from .core import (
    CycleError,
    ExecutionError,
    ToolResult,
    ToolTimeoutError,
    ValidationError,
)
from .registry import ToolRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "EventKind",
    "ExecutionReport",
    "Invoker",
    "LockManager",
    "ParallelExecutor",
    "Plan",
    "Ref",
    "Step",
    "StepEvent",
    "StepOutcome",
    "StepStatus",
    "default_invoker",
]

ResourceMode = Literal["read", "write"]
StepStatus = Literal["succeeded", "failed", "skipped", "cancelled"]
EventKind = Literal["started", "succeeded", "failed", "retrying", "skipped", "cancelled"]

_RESOURCE_MODES = frozenset({"read", "write"})


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    """Placeholder in step args, resolved to an earlier step's output.

    ``path`` is a dotted list of mapping keys / sequence indexes, e.g.
    ``"items.0.title"``. ``None`` means the whole output value.
    """

    step: str
    path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.step, str) or not self.step:
            raise ValidationError(f"Ref step must be a non-empty string, got {self.step!r}")
        if self.path is not None and (not isinstance(self.path, str) or not self.path):
            raise ValidationError(f"Ref path must be a non-empty string or None, got {self.path!r}")

    def resolve(self, outputs: Mapping[str, Any]) -> Any:
        if self.step not in outputs:
            raise ValidationError(f"reference to step {self.step!r} has no output available")
        value = outputs[self.step]
        if self.path is None:
            return value
        for segment in self.path.split("."):
            value = _lookup(value, segment, self)
        return value


def _lookup(value: Any, segment: str, ref: Ref) -> Any:
    if isinstance(value, Mapping):
        if segment in value:
            return value[segment]
        if _is_int(segment) and int(segment) in value:
            return value[int(segment)]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if _is_int(segment):
            try:
                return value[int(segment)]
            except IndexError:
                pass
    raise ValidationError(
        f"cannot resolve path {ref.path!r} of step {ref.step!r}: no {segment!r} in "
        f"{type(value).__name__}"
    )


_INT_SEGMENT_RE = re.compile(r"-?[0-9]+")


def _is_int(segment: str) -> bool:
    """True only for segments ``int()`` accepts: ASCII digits with an optional ``-``."""
    return _INT_SEGMENT_RE.fullmatch(segment) is not None


def _iter_refs(value: Any) -> Iterator[Ref]:
    if isinstance(value, Ref):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_refs(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_refs(item)


def _resolve_refs(value: Any, outputs: Mapping[str, Any]) -> Any:
    if isinstance(value, Ref):
        return value.resolve(outputs)
    if isinstance(value, Mapping):
        return {key: _resolve_refs(item, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(item, outputs) for item in value]
    if isinstance(value, tuple):
        return tuple(_resolve_refs(item, outputs) for item in value)
    return value


@dataclass(frozen=True, eq=False)
class Step:
    """One tool invocation inside a :class:`Plan`.

    Exactly one of ``tool`` (optionally pinned by ``version``) or ``capability``
    must be set. Any :class:`Ref` found (recursively) in ``args`` adds its step
    to ``depends_on``.
    """

    id: str
    tool: str | None = None
    capability: str | None = None
    args: Mapping[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    timeout: float | None = None
    retries: int = 0
    retry_backoff: float = 0.0
    resources: Mapping[str, str] = field(default_factory=dict)
    version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValidationError(f"step id must be a non-empty string, got {self.id!r}")
        if (self.tool is None) == (self.capability is None):
            raise ValidationError(
                f"step {self.id!r}: exactly one of 'tool' or 'capability' must be set"
            )
        if self.version is not None and self.tool is None:
            raise ValidationError(f"step {self.id!r}: 'version' requires 'tool'")
        if not isinstance(self.args, Mapping):
            raise ValidationError(f"step {self.id!r}: args must be a mapping")
        if not isinstance(self.retries, int) or isinstance(self.retries, bool) or self.retries < 0:
            raise ValidationError(f"step {self.id!r}: retries must be an int >= 0")
        if self.retry_backoff < 0:
            raise ValidationError(f"step {self.id!r}: retry_backoff must be >= 0")
        if self.timeout is not None and self.timeout <= 0:
            raise ValidationError(f"step {self.id!r}: timeout must be > 0")
        if not isinstance(self.resources, Mapping):
            raise ValidationError(f"step {self.id!r}: resources must be a mapping")
        for resource, mode in self.resources.items():
            if mode not in _RESOURCE_MODES:
                raise ValidationError(
                    f"step {self.id!r}: resource {resource!r} mode must be 'read' or 'write', "
                    f"got {mode!r}"
                )
        if isinstance(self.depends_on, str):
            raise ValidationError(f"step {self.id!r}: depends_on must be a sequence of step ids")
        deps: list[str] = []
        for dep in (*self.depends_on, *(ref.step for ref in _iter_refs(self.args))):
            if not isinstance(dep, str) or not dep:
                raise ValidationError(f"step {self.id!r}: invalid dependency {dep!r}")
            if dep not in deps:
                deps.append(dep)
        object.__setattr__(self, "depends_on", tuple(deps))
        object.__setattr__(self, "args", dict(self.args))
        object.__setattr__(self, "resources", dict(self.resources))

    @property
    def target(self) -> str:
        """Human-readable target, e.g. ``calc@1.0.0`` or ``capability:search``."""
        if self.tool is not None:
            return f"{self.tool}@{self.version}" if self.version else self.tool
        return f"capability:{self.capability}"


class Plan:
    """A validated DAG of steps."""

    def __init__(self, steps: Iterable[Step]) -> None:
        self._steps: dict[str, Step] = {}
        for step in steps:
            if not isinstance(step, Step):
                raise ValidationError(f"plan entries must be Step objects, got {step!r}")
            if step.id in self._steps:
                raise ValidationError(f"duplicate step id {step.id!r}")
            self._steps[step.id] = step
        for step in self._steps.values():
            for dep in step.depends_on:
                if dep not in self._steps:
                    raise ValidationError(f"step {step.id!r} depends on unknown step {dep!r}")
        self._check_cycles()
        self._levels = self._compute_levels()
        self._dependents: dict[str, list[str]] = {sid: [] for sid in self._steps}
        for step in self._steps.values():
            for dep in step.depends_on:
                self._dependents[dep].append(step.id)

    # -- validation ---------------------------------------------------------

    # Both passes are iterative so that very long dependency chains cannot hit
    # Python's recursion limit, whatever order the steps were declared in.

    def _check_cycles(self) -> None:
        white, grey, black = 0, 1, 2
        color = dict.fromkeys(self._steps, white)

        for root in self._steps:
            if color[root] != white:
                continue
            color[root] = grey
            path = [root]
            stack = [iter(self._steps[root].depends_on)]
            while stack:
                dep = next(stack[-1], None)
                if dep is None:
                    stack.pop()
                    color[path.pop()] = black
                elif color[dep] == grey:
                    cycle = path[path.index(dep):] + [dep]
                    raise CycleError("dependency cycle: " + " -> ".join(cycle))
                elif color[dep] == white:
                    color[dep] = grey
                    path.append(dep)
                    stack.append(iter(self._steps[dep].depends_on))

    def _compute_levels(self) -> list[list[str]]:
        # Kahn's algorithm (the graph is known to be acyclic at this point).
        level: dict[str, int] = {}
        pending = {sid: len(step.depends_on) for sid, step in self._steps.items()}
        dependents: dict[str, list[str]] = {sid: [] for sid in self._steps}
        for step in self._steps.values():
            for dep in step.depends_on:
                dependents[dep].append(step.id)
        ready = [sid for sid, count in pending.items() if count == 0]
        for sid in ready:
            level[sid] = 0
        while ready:
            sid = ready.pop()
            for child in dependents[sid]:
                level[child] = max(level.get(child, 0), level[sid] + 1)
                pending[child] -= 1
                if pending[child] == 0:
                    ready.append(child)

        layers: list[list[str]] = [[] for _ in range(max(level.values(), default=-1) + 1)]
        for sid in self._steps:  # keeps declaration order within each layer
            layers[level[sid]].append(sid)
        return layers

    # -- queries ------------------------------------------------------------

    def levels(self) -> list[list[str]]:
        """Topological layers; each step sits one layer after its deepest dependency."""
        return [list(layer) for layer in self._levels]

    def order(self) -> list[str]:
        """A valid execution order (layers flattened)."""
        return [sid for layer in self._levels for sid in layer]

    def dependents(self, step_id: str) -> list[str]:
        """Direct dependents of ``step_id``."""
        return list(self._dependents[step_id])

    @property
    def steps(self) -> list[Step]:
        return list(self._steps.values())

    def __getitem__(self, step_id: str) -> Step:
        return self._steps[step_id]

    def __contains__(self, step_id: object) -> bool:
        return step_id in self._steps

    def __iter__(self) -> Iterator[Step]:
        return iter(self._steps.values())

    def __len__(self) -> int:
        return len(self._steps)

    def __repr__(self) -> str:
        return f"Plan({list(self._steps)})"


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

Invoker = Callable[[Step, dict[str, Any]], Awaitable[ToolResult]]
"""Performs one attempt of a step with its resolved args and returns a ToolResult."""


class LockManager(Protocol):
    """Duck-typed resource lock manager (e.g. the conflicts module's)."""

    def hold(self, owner: str, resources: Mapping[str, str]) -> AsyncContextManager[Any]:
        """Context manager holding ``resources`` (name → "read"/"write") for ``owner``."""
        ...


def default_invoker(registry: ToolRegistry) -> Invoker:
    """Invoker that calls ``step.tool`` straight from ``registry`` (no routing)."""

    async def invoke(step: Step, args: dict[str, Any]) -> ToolResult:
        if step.tool is None:
            raise ExecutionError(
                f"step {step.id!r} targets capability {step.capability!r}; routing is required "
                "to pick a tool (use a routing-aware invoker)"
            )
        spec = registry.get(step.tool, step.version)
        if not registry.is_enabled(spec.name, spec.version):
            raise ExecutionError(f"tool {spec.name}@{spec.version} is disabled")
        spec.validate_input(args)
        started = time.perf_counter()
        value = await spec.handler(**args)
        return ToolResult(
            tool=spec.name,
            version=spec.version,
            ok=True,
            value=value,
            duration=time.perf_counter() - started,
        )

    return invoke


# ---------------------------------------------------------------------------
# Events and report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepEvent:
    kind: EventKind
    step_id: str
    attempt: int = 0
    error: str | None = None
    at: float = field(default_factory=time.monotonic)


EventListener = Callable[[StepEvent], None]


@dataclass(frozen=True)
class StepOutcome:
    """Final state of one step. Times are ``time.monotonic()`` values."""

    step_id: str
    status: StepStatus
    result: ToolResult | None = None
    error: str | None = None
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    exception: BaseException | None = field(default=None, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    @property
    def value(self) -> Any:
        return self.result.value if self.result is not None else None

    @property
    def duration(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at


@dataclass(frozen=True)
class ExecutionReport:
    """Outcome of every step of a plan, in plan declaration order."""

    results: dict[str, StepOutcome]
    started_at: float = 0.0
    finished_at: float = 0.0

    def _with(self, status: StepStatus) -> list[str]:
        return [sid for sid, out in self.results.items() if out.status == status]

    @property
    def ok(self) -> bool:
        return all(out.status == "succeeded" for out in self.results.values())

    @property
    def outputs(self) -> dict[str, Any]:
        return {sid: out.value for sid, out in self.results.items() if out.status == "succeeded"}

    @property
    def succeeded(self) -> list[str]:
        return self._with("succeeded")

    @property
    def failed(self) -> list[str]:
        return self._with("failed")

    @property
    def skipped(self) -> list[str]:
        return self._with("skipped")

    @property
    def cancelled(self) -> list[str]:
        return self._with("cancelled")

    @property
    def duration(self) -> float:
        return self.finished_at - self.started_at

    def __getitem__(self, step_id: str) -> StepOutcome:
        return self.results[step_id]

    def raise_for_failure(self) -> None:
        """Raise :class:`ExecutionError` summarizing failures unless every step succeeded."""
        if self.ok:
            return
        parts = [f"{sid}: {self.results[sid].error}" for sid in self.failed]
        if self.skipped:
            parts.append(f"skipped: {', '.join(self.skipped)}")
        if self.cancelled:
            parts.append(f"cancelled: {', '.join(self.cancelled)}")
        raise ExecutionError("plan execution failed; " + "; ".join(parts))


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class _Progress:
    __slots__ = ("started_at", "attempts")

    def __init__(self) -> None:
        self.started_at: float | None = None
        self.attempts = 0


class ParallelExecutor:
    """Runs a :class:`Plan` with maximal dependency-respecting parallelism."""

    def __init__(
        self,
        invoker: Invoker,
        *,
        max_concurrency: int = 8,
        fail_fast: bool = False,
        default_timeout: float | None = None,
        lock_manager: LockManager | None = None,
        on_event: EventListener | None = None,
    ) -> None:
        if not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValidationError("max_concurrency must be an int >= 1")
        if default_timeout is not None and default_timeout <= 0:
            raise ValidationError("default_timeout must be > 0")
        self.invoker = invoker
        self.max_concurrency = max_concurrency
        self.fail_fast = fail_fast
        self.default_timeout = default_timeout
        self.lock_manager = lock_manager
        self.on_event = on_event

    # -- public -------------------------------------------------------------

    async def run(self, plan: Plan) -> ExecutionReport:
        run_started = time.monotonic()
        semaphore = asyncio.Semaphore(self.max_concurrency)
        outcomes: dict[str, StepOutcome] = {}
        outputs: dict[str, Any] = {}
        progress: dict[str, _Progress] = {}
        running: dict[asyncio.Task[StepOutcome], str] = {}
        order = plan.order()

        def schedule() -> None:
            for sid in order:
                if sid in outcomes or sid in progress:
                    continue
                step = plan[sid]
                dep_states = [outcomes[d].status if d in outcomes else None for d in step.depends_on]
                if any(state not in (None, "succeeded") for state in dep_states):
                    outcomes[sid] = StepOutcome(sid, "skipped", error=_skip_reason(step, outcomes))
                    self._emit(StepEvent("skipped", sid, error=outcomes[sid].error))
                elif all(state == "succeeded" for state in dep_states):
                    progress[sid] = _Progress()
                    task = asyncio.create_task(
                        self._run_step(step, outputs, semaphore, progress[sid]),
                        name=f"step:{sid}",
                    )
                    running[task] = sid

        try:
            schedule()
            while running:
                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for task in sorted(done, key=lambda t: order.index(running[t])):
                    sid = running.pop(task)
                    outcome = self._collect(task, plan[sid], progress[sid])
                    outcomes[sid] = outcome
                    if outcome.status == "succeeded":
                        outputs[sid] = outcome.value
                abort = self.fail_fast and any(o.status == "failed" for o in outcomes.values())
                if abort:
                    await self._cancel_all(running, progress, outcomes)
                    _mark_skipped(plan, order, outcomes, self._emit)
                    for sid in order:
                        if sid not in outcomes:
                            outcomes[sid] = StepOutcome(sid, "cancelled", error="cancelled (fail-fast)")
                            self._emit(StepEvent("cancelled", sid))
                    break
                schedule()
        finally:
            if running:  # run() itself was cancelled or crashed: don't leak tasks
                for task in running:
                    task.cancel()
                await asyncio.gather(*running, return_exceptions=True)

        return ExecutionReport(
            results={step.id: outcomes[step.id] for step in plan},
            started_at=run_started,
            finished_at=time.monotonic(),
        )

    # -- internals ----------------------------------------------------------

    def _collect(self, task: asyncio.Task[StepOutcome], step: Step, prog: _Progress) -> StepOutcome:
        """The finished step task's outcome; never raises for a step-local problem.

        ``_run_step`` already turns every failure into an outcome, so this only
        guards against a step task that ended cancelled or crashed anyway: such a
        step is reported as failed instead of aborting the whole run.
        """
        if task.cancelled():
            return self._failed(step, prog, "CancelledError: step task was cancelled")
        exc = task.exception()
        if exc is None:
            return task.result()
        if not isinstance(exc, Exception):  # KeyboardInterrupt, SystemExit, ...
            raise exc
        return self._failed(step, prog, f"{type(exc).__name__}: {exc}", exc=exc)

    async def _cancel_all(
        self,
        running: dict[asyncio.Task[StepOutcome], str],
        progress: dict[str, _Progress],
        outcomes: dict[str, StepOutcome],
    ) -> None:
        tasks = list(running)
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        now = time.monotonic()
        for task, result in zip(tasks, results):
            sid = running.pop(task)
            if isinstance(result, StepOutcome):  # finished before the cancel landed
                outcomes[sid] = result
                continue
            prog = progress[sid]
            outcomes[sid] = StepOutcome(
                sid,
                "cancelled",
                error="cancelled (fail-fast)",
                attempts=prog.attempts,
                started_at=prog.started_at,
                finished_at=now,
            )
            self._emit(StepEvent("cancelled", sid, attempt=prog.attempts))

    async def _run_step(
        self,
        step: Step,
        outputs: Mapping[str, Any],
        semaphore: asyncio.Semaphore,
        prog: _Progress,
    ) -> StepOutcome:
        prog.started_at = time.monotonic()
        self._emit(StepEvent("started", step.id))
        try:
            args = _resolve_refs(step.args, outputs)
        except Exception as exc:  # noqa: BLE001 - an unresolvable ref fails only this step
            return self._failed(step, prog, f"{type(exc).__name__}: {exc}", exc=exc)

        last_result: ToolResult | None = None
        last_exc: BaseException | None = None
        error = ""
        for attempt in range(step.retries + 1):
            if attempt:
                self._emit(StepEvent("retrying", step.id, attempt=prog.attempts, error=error))
                delay = step.retry_backoff * 2 ** (attempt - 1)
                if delay > 0:
                    await asyncio.sleep(delay)
            prog.attempts += 1
            try:
                result = await self._attempt(step, args, semaphore)
            except asyncio.CancelledError as exc:
                if _being_cancelled():
                    raise  # run() (or fail-fast) is cancelling this step: propagate
                # The tool itself raised CancelledError (e.g. it awaited a child
                # task that was cancelled): an ordinary failed attempt.
                last_exc, last_result = exc, None
                error = f"CancelledError: {exc}" if str(exc) else "CancelledError: tool was cancelled"
                continue
            except Exception as exc:  # noqa: BLE001 - every failure becomes an outcome
                last_exc, last_result = exc, None
                error = f"{type(exc).__name__}: {exc}"
                continue
            if result.ok:
                outcome = StepOutcome(
                    step.id,
                    "succeeded",
                    result=result,
                    attempts=prog.attempts,
                    started_at=prog.started_at,
                    finished_at=time.monotonic(),
                )
                self._emit(StepEvent("succeeded", step.id, attempt=prog.attempts))
                return outcome
            last_exc, last_result = None, result
            error = result.error or f"tool {result.tool}@{result.version} returned ok=False"
        return self._failed(step, prog, error, result=last_result, exc=last_exc)

    async def _attempt(
        self, step: Step, args: dict[str, Any], semaphore: asyncio.Semaphore
    ) -> ToolResult:
        # Lock first, then the concurrency slot: see design.md (no idle slots
        # parked on contended resources; slot holders never wait on locks).
        if self.lock_manager is not None and step.resources:
            async with self.lock_manager.hold(step.id, step.resources):
                async with semaphore:
                    return await self._invoke(step, args)
        async with semaphore:
            return await self._invoke(step, args)

    async def _invoke(self, step: Step, args: dict[str, Any]) -> ToolResult:
        timeout = step.timeout if step.timeout is not None else self.default_timeout
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                result = await self.invoker(step, args)
        except TimeoutError as exc:
            # Only the executor's own deadline is a step timeout; a TimeoutError
            # raised by the tool/invoker itself is an ordinary failure.
            if deadline.expired() and not isinstance(exc, ToolTimeoutError):
                raise ToolTimeoutError(f"step {step.id!r} timed out after {timeout}s") from None
            raise
        if not isinstance(result, ToolResult):
            raise ExecutionError(
                f"invoker returned {type(result).__name__} for step {step.id!r}; "
                "expected ToolResult"
            )
        return result

    def _failed(
        self,
        step: Step,
        prog: _Progress,
        error: str,
        *,
        result: ToolResult | None = None,
        exc: BaseException | None = None,
    ) -> StepOutcome:
        self._emit(StepEvent("failed", step.id, attempt=prog.attempts, error=error))
        return StepOutcome(
            step.id,
            "failed",
            result=result,
            error=error,
            attempts=prog.attempts,
            started_at=prog.started_at,
            finished_at=time.monotonic(),
            exception=exc,
        )

    def _emit(self, event: StepEvent) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001 - observers must never break execution
            logger.exception("executor event listener failed for %s", event)


def _being_cancelled() -> bool:
    """True when the current task has a pending cancellation request."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _skip_reason(step: Step, outcomes: Mapping[str, StepOutcome]) -> str:
    bad = [d for d in step.depends_on if d in outcomes and outcomes[d].status != "succeeded"]
    return "skipped: dependency " + ", ".join(f"{d} {outcomes[d].status}" for d in bad)


def _mark_skipped(
    plan: Plan,
    order: list[str],
    outcomes: dict[str, StepOutcome],
    emit: Callable[[StepEvent], None],
) -> None:
    """Mark not-yet-decided steps whose dependencies failed or were skipped."""
    for sid in order:
        if sid in outcomes:
            continue
        step = plan[sid]
        if any(
            d in outcomes and outcomes[d].status in ("failed", "skipped") for d in step.depends_on
        ):
            outcomes[sid] = StepOutcome(sid, "skipped", error=_skip_reason(step, outcomes))
            emit(StepEvent("skipped", sid, error=outcomes[sid].error))
