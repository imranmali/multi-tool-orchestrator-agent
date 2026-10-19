"""The orchestrator agent: one entry point composing every other module.

:class:`Orchestrator` owns (or is given) a :class:`~orchestrator.registry.ToolRegistry`,
a :class:`~orchestrator.permissions.PermissionPolicy`, a
:class:`~orchestrator.router.CapabilityRouter`, a
:class:`~orchestrator.conflicts.ConflictResolver` and a resource lock manager, and
runs plans of :class:`~orchestrator.executor.Step` objects on behalf of a
:class:`~orchestrator.permissions.Principal`:

* named-tool steps are looked up (honouring version pins), validated, authorized
  with an audited check and invoked;
* capability steps are routed among the tools the principal may use, with health
  feedback to the router and bounded fallback to the next-best candidate;
* fan-out steps are served by the top-N permitted tools in parallel and reconciled
  by a conflict-resolution strategy.

Every run returns a :class:`RunReport` combining the executor's report with who
served each step, the attempt chains, fan-out resolutions, the run's audit
records, pre-flight issues, contention warnings and one ordered event stream.

This is the only module allowed to depend on every other module.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncContextManager, Callable, Iterable, Mapping

from .conflicts import (
    ConflictResolver,
    Resolution,
    ResourceLockManager,
    detect_write_conflicts,
)
from .core import (
    Capability,
    ExecutionError,
    NoRouteError,
    OrchestratorError,
    ToolNotFoundError,
    ToolResult,
    ToolSpec,
    ToolTimeoutError,
    ValidationError,
)
from .executor import (
    ExecutionReport,
    LockManager,
    ParallelExecutor,
    Plan,
    Step,
    StepEvent,
    StepOutcome,
)
from .permissions import AccessDeniedError, AuditRecord, PermissionPolicy, Principal
from .registry import RegistryEvent, ToolRegistry
from .router import CapabilityRouter

logger = logging.getLogger(__name__)

__all__ = [
    "FallbackExhaustedError",
    "FanOut",
    "Orchestrator",
    "OrchestratorEvent",
    "PlanIssue",
    "PreflightError",
    "RunReport",
    "ToolAttempt",
]

CAPABILITY_PREFIX = "capability:"

ATTEMPT_OUTCOMES = frozenset({"ok", "error", "timeout", "invalid_input", "denied", "cancelled"})
ISSUE_KINDS = frozenset({"unknown_tool", "disabled", "denied", "no_route"})
EVENT_KINDS = frozenset(
    {
        # executor step lifecycle
        "started",
        "succeeded",
        "failed",
        "retrying",
        "skipped",
        "cancelled",
        # orchestrator
        "routed",
        "fallback",
        "denied",
        "resolved",
    }
)

EventListener = Callable[["OrchestratorEvent"], None]


# --------------------------------------------------------------------------- value types


@dataclass(frozen=True)
class FanOut:
    """Fan-out setting for one capability step: ``n`` tools reconciled with ``strategy``."""

    n: int = 3
    strategy: str | Callable[..., Any] | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.n, bool) or not isinstance(self.n, int) or self.n < 1:
            raise ValidationError(f"fan-out n must be an int >= 1, got {self.n!r}")
        if self.strategy is not None and not isinstance(self.strategy, str) and not callable(
            self.strategy
        ):
            raise ValidationError(
                f"fan-out strategy must be a name, a callable or None, got {self.strategy!r}"
            )
        if not isinstance(self.options, Mapping):
            raise ValidationError(f"fan-out options must be a mapping, got {self.options!r}")
        object.__setattr__(self, "options", dict(self.options))


@dataclass(frozen=True)
class ToolAttempt:
    """One tool tried for a step: which tool, how it ended, and in which executor attempt."""

    tool: str
    version: str
    outcome: str
    error: str | None = None
    duration: float = 0.0
    attempt: int = 1

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


@dataclass(frozen=True)
class PlanIssue:
    """A pre-flight problem with one step."""

    step_id: str
    kind: str
    target: str
    message: str


@dataclass(frozen=True)
class OrchestratorEvent:
    """One entry of a run's unified event stream."""

    kind: str
    step_id: str
    tool: str | None = None
    attempt: int = 0
    error: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class RunReport:
    """Everything that happened in one orchestrator run."""

    run_id: str
    execution: ExecutionReport
    served_by: dict[str, tuple[str, ...]] = field(default_factory=dict)
    attempts: dict[str, tuple[ToolAttempt, ...]] = field(default_factory=dict)
    resolutions: dict[str, Resolution] = field(default_factory=dict)
    audit: tuple[AuditRecord, ...] = ()
    issues: tuple[PlanIssue, ...] = ()
    contentions: tuple[tuple[str, str, str], ...] = ()
    events: tuple[OrchestratorEvent, ...] = ()

    # -- derived ------------------------------------------------------------

    @property
    def chains(self) -> dict[str, tuple[str, ...]]:
        """Per step, the names of the tools attempted, in order."""
        return {sid: tuple(a.tool for a in atts) for sid, atts in self.attempts.items()}

    @property
    def warnings(self) -> list[str]:
        """Human-readable contention warnings."""
        return [
            f"steps {a!r} and {b!r} have conflicting claims on resource {resource!r} "
            "and will serialize"
            for resource, a, b in self.contentions
        ]

    # -- delegation to the execution report ----------------------------------

    @property
    def ok(self) -> bool:
        return self.execution.ok

    @property
    def outputs(self) -> dict[str, Any]:
        return self.execution.outputs

    @property
    def results(self) -> dict[str, StepOutcome]:
        return self.execution.results

    @property
    def succeeded(self) -> list[str]:
        return self.execution.succeeded

    @property
    def failed(self) -> list[str]:
        return self.execution.failed

    @property
    def skipped(self) -> list[str]:
        return self.execution.skipped

    @property
    def cancelled(self) -> list[str]:
        return self.execution.cancelled

    @property
    def duration(self) -> float:
        return self.execution.duration

    def __getitem__(self, step_id: str) -> StepOutcome:
        return self.execution[step_id]

    def raise_for_failure(self) -> None:
        self.execution.raise_for_failure()


# --------------------------------------------------------------------------- errors


class PreflightError(OrchestratorError):
    """A run with enforced pre-flight found issues; nothing was invoked."""

    def __init__(self, issues: Iterable[PlanIssue]) -> None:
        self.issues: tuple[PlanIssue, ...] = tuple(issues)
        listed = "; ".join(f"{i.step_id} ({i.kind}): {i.message}" for i in self.issues)
        super().__init__(f"pre-flight check failed: {listed}")


class FallbackExhaustedError(ExecutionError):
    """Every tool tried for a capability step failed."""

    def __init__(
        self,
        step_id: str,
        capability: str,
        attempts: Iterable[ToolAttempt],
        message: str | None = None,
    ) -> None:
        self.step_id = step_id
        self.capability = capability
        self.attempts: tuple[ToolAttempt, ...] = tuple(attempts)
        if message is None:
            listed = "; ".join(f"{a.tool}: {a.error}" for a in self.attempts)
            message = (
                f"step {step_id!r}: every tool tried for capability {capability!r} failed: "
                f"{listed}"
            )
        super().__init__(message)


# --------------------------------------------------------------------------- internals


class _RunLocks:
    """Lock-manager adapter giving each run its own owner namespace."""

    def __init__(self, manager: LockManager, run_id: str) -> None:
        self._manager = manager
        self._run_id = run_id

    def hold(self, owner: str, resources: Mapping[str, str]) -> AsyncContextManager[Any]:
        return self._manager.hold(f"{self._run_id}/{owner}", resources)


class _RunContext:
    """Mutable per-run state shared by the run's invoker and event listener."""

    def __init__(
        self,
        run_id: str,
        principal: Principal,
        fanout: Mapping[str, FanOut],
        listener: EventListener | None,
    ) -> None:
        self.run_id = run_id
        self.principal = principal
        self.fanout = dict(fanout)
        self.listener = listener
        self.invocations: dict[str, int] = {}
        self.attempts: dict[str, list[ToolAttempt]] = {}
        self.served: dict[str, tuple[str, ...]] = {}
        self.resolutions: dict[str, Resolution] = {}
        self.audit: list[AuditRecord] = []
        self.events: list[OrchestratorEvent] = []

    def next_invocation(self, step_id: str) -> int:
        n = self.invocations.get(step_id, 0) + 1
        self.invocations[step_id] = n
        return n

    def add_attempt(self, step_id: str, attempt: ToolAttempt) -> None:
        self.attempts.setdefault(step_id, []).append(attempt)

    def emit(self, event: OrchestratorEvent) -> None:
        self.events.append(event)
        if self.listener is None:
            return
        try:
            self.listener(event)
        except Exception:  # noqa: BLE001 - observers must never break a run
            logger.exception("orchestrator event listener failed for %s", event)

    def on_step_event(self, event: StepEvent) -> None:
        self.emit(
            OrchestratorEvent(
                kind=event.kind,
                step_id=event.step_id,
                attempt=event.attempt,
                error=event.error,
                at=event.at,
            )
        )


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _accepts(spec: ToolSpec, args: Mapping[str, Any]) -> bool:
    try:
        spec.validate_input(args)
    except ValidationError:
        return False
    return True


def _being_cancelled() -> bool:
    """Whether the current task has a pending cancellation request (step timeout,
    fail-fast, run cancellation), as opposed to a tool raising CancelledError itself."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _positive_or_none(name: str, value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValidationError(f"{name} must be None or a number > 0, got {value!r}")
    return float(value)


def _int_at_least(name: str, value: Any, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{name} must be an int >= {minimum}, got {value!r}")
    return value


def _require_principal(principal: Any) -> Principal:
    if not isinstance(principal, Principal):
        raise ValidationError(f"expected a Principal, got {type(principal).__name__}")
    return principal


def _as_plan(plan: Plan | Iterable[Step]) -> Plan:
    if isinstance(plan, Plan):
        return plan
    if isinstance(plan, (str, bytes, Step)) or not isinstance(plan, Iterable):
        raise ValidationError(f"expected a Plan or an iterable of Steps, got {plan!r}")
    return Plan(plan)


def _first_doc_line(fn: Callable[..., Any]) -> str:
    for line in (fn.__doc__ or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _tool_claims(spec: ToolSpec) -> dict[str, str]:
    claims = (spec.metadata or {}).get("resources")
    if claims is None:
        return {}
    if not isinstance(claims, Mapping):
        raise ValidationError(
            f"tool {spec.name!r}: metadata 'resources' must be a mapping of resource -> "
            f"'read'|'write', got {claims!r}"
        )
    for resource, mode in claims.items():
        if not isinstance(resource, str) or not resource:
            raise ValidationError(
                f"tool {spec.name!r}: resource names must be non-empty strings, got {resource!r}"
            )
        if mode not in ("read", "write"):
            raise ValidationError(
                f"tool {spec.name!r}: resource {resource!r} mode must be 'read' or 'write', "
                f"got {mode!r}"
            )
    return dict(claims)


def _ancestors(plan: Plan) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for sid in plan.order():  # dependencies always come first
        deps = plan[sid].depends_on
        acc: set[str] = set(deps)
        for dep in deps:
            acc |= result[dep]
        result[sid] = frozenset(acc)
    return result


# --------------------------------------------------------------------------- orchestrator


class Orchestrator:
    """Runs plans and one-shot calls on behalf of a principal."""

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        policy: PermissionPolicy | None = None,
        router: CapabilityRouter | None = None,
        resolver: ConflictResolver | None = None,
        lock_manager: LockManager | None = None,
        max_concurrency: int = 8,
        fail_fast: bool = False,
        default_timeout: float | None = None,
        tool_timeout: float | None = None,
        max_fallbacks: int = 2,
        on_event: EventListener | None = None,
    ) -> None:
        self._max_concurrency = _int_at_least("max_concurrency", max_concurrency, 1)
        if not isinstance(fail_fast, bool):
            raise ValidationError(f"fail_fast must be a bool, got {fail_fast!r}")
        self._fail_fast = fail_fast
        self._default_timeout = _positive_or_none("default_timeout", default_timeout)
        self._tool_timeout = _positive_or_none("tool_timeout", tool_timeout)
        self._max_fallbacks = _int_at_least("max_fallbacks", max_fallbacks, 0)
        if on_event is not None and not callable(on_event):
            raise ValidationError("on_event must be callable or None")
        self._on_event = on_event

        if registry is None:
            registry = getattr(router, "_registry", None) if router is not None else None
            if registry is None:
                registry = ToolRegistry()
        elif router is not None and getattr(router, "_registry", registry) is not registry:
            raise ValidationError(
                "router is bound to a different registry than the orchestrator's"
            )
        self._registry = registry
        self._policy = policy if policy is not None else PermissionPolicy()
        self._router = router if router is not None else CapabilityRouter(registry)
        self._resolver = resolver if resolver is not None else ConflictResolver()
        self._locks: LockManager = lock_manager if lock_manager is not None else ResourceLockManager()
        self._run_ids = itertools.count(1)
        self._unsubscribe: Callable[[], None] | None = registry.subscribe(self._on_registry_event)

    # -- read-only configuration ---------------------------------------------

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def policy(self) -> PermissionPolicy:
        return self._policy

    @property
    def router(self) -> CapabilityRouter:
        return self._router

    @property
    def resolver(self) -> ConflictResolver:
        return self._resolver

    @property
    def lock_manager(self) -> LockManager:
        return self._locks

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def fail_fast(self) -> bool:
        return self._fail_fast

    @property
    def default_timeout(self) -> float | None:
        return self._default_timeout

    @property
    def tool_timeout(self) -> float | None:
        return self._tool_timeout

    @property
    def max_fallbacks(self) -> int:
        return self._max_fallbacks

    @property
    def on_event(self) -> EventListener | None:
        return self._on_event

    # -- registration and lifecycle -------------------------------------------

    def register(self, spec: ToolSpec, *, replace: bool = False) -> ToolSpec:
        """Register a tool descriptor in the orchestrator's registry."""
        if not isinstance(spec, ToolSpec):
            raise ValidationError(f"expected a ToolSpec, got {type(spec).__name__}")
        return self._registry.register(spec, replace=replace)

    def tool(
        self,
        fn: Callable[..., Any] | None = None,
        /,
        *,
        name: str | None = None,
        version: str = "1.0.0",
        capabilities: Iterable[str | Capability] | str | Capability | None = None,
        scopes: Iterable[str] | str = (),
        input_schema: Mapping[str, type] | None = None,
        description: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        replace: bool = False,
    ) -> Any:
        """Decorator registering an async function as a tool; returns it unchanged.

        Usable bare (``@orch.tool``) or with arguments (``@orch.tool(name=...)``).
        """

        def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name if name is not None else getattr(func, "__name__", "")
            caps: Any = (tool_name,) if capabilities is None else capabilities
            if isinstance(caps, (str, Capability)):
                caps = (caps,)
            scope_set = (scopes,) if isinstance(scopes, str) else tuple(scopes)
            spec = ToolSpec(
                name=tool_name,
                version=version,
                capabilities=tuple(caps),
                handler=func,
                description=description if description is not None else _first_doc_line(func),
                required_scopes=frozenset(scope_set),
                input_schema=dict(input_schema or {}),
                metadata=dict(metadata or {}),
            )
            self.register(spec, replace=replace)
            return func

        if fn is not None:
            return decorate(fn)
        return decorate

    def close(self) -> None:
        """Stop reflecting registry changes into the router. Idempotent."""
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def _on_registry_event(self, event: RegistryEvent) -> None:
        if event.kind == "unregistered":
            self._router.reset(event.name)

    # -- pre-flight -------------------------------------------------------------

    def check_plan(self, plan: Plan | Iterable[Step], principal: Principal) -> list[PlanIssue]:
        """Static issues of ``plan`` for ``principal``; never invokes or audits anything."""
        principal = _require_principal(principal)
        plan = _as_plan(plan)
        eligible = self._policy.predicate(principal)
        issues: list[PlanIssue] = []
        for step in plan:
            issue = self._step_issue(step, principal, eligible)
            if issue is not None:
                issues.append(issue)
        return issues

    def _step_issue(
        self, step: Step, principal: Principal, eligible: Callable[[ToolSpec], bool]
    ) -> PlanIssue | None:
        if step.tool is not None:
            try:
                spec = self._registry.get(step.tool, step.version)
                enabled = self._registry.is_enabled(spec.name, spec.version)
            except ToolNotFoundError:
                return PlanIssue(
                    step.id, "unknown_tool", step.target, f"tool {step.target!r} is not registered"
                )
            if not enabled:
                return PlanIssue(
                    step.id, "disabled", step.target, f"tool {spec.name}@{spec.version} is disabled"
                )
            decision = self._policy.decide(principal, spec)
            if not decision.allowed:
                return PlanIssue(
                    step.id,
                    "denied",
                    step.target,
                    f"principal {principal.id!r} may not use tool {spec.name!r}: {decision.reason}",
                )
            return None
        assert step.capability is not None
        try:
            if self._router.candidates(step.capability, eligible=eligible):
                return None
            self._router.route(step.capability, eligible=eligible)
            message = f"no permitted tool serves capability {step.capability!r}"
        except NoRouteError as exc:
            message = str(exc)
        except Exception as exc:  # noqa: BLE001 - e.g. a custom routing strategy failing
            # Routing itself is broken for this step: report it, and let the run fail
            # this step (the invoker hits the same error) instead of aborting the run.
            message = f"routing capability {step.capability!r} failed: {_describe(exc)}"
        return PlanIssue(step.id, "no_route", step.target, message)

    # -- running ------------------------------------------------------------------

    async def run(
        self,
        plan: Plan | Iterable[Step],
        principal: Principal,
        *,
        fanout: Mapping[str, FanOut | int] | None = None,
        preflight: bool = False,
    ) -> RunReport:
        """Run ``plan`` on behalf of ``principal`` and return a :class:`RunReport`."""
        principal = _require_principal(principal)
        plan = _as_plan(plan)
        fan = self._validate_fanout(plan, fanout)
        issues = self.check_plan(plan, principal)
        if preflight and issues:
            raise PreflightError(issues)
        effective = self._effective_plan(plan, issues)
        contentions = self._contentions(effective)

        run_id = f"run-{next(self._run_ids)}"
        ctx = _RunContext(run_id, principal, fan, self._on_event)

        async def invoker(step: Step, args: dict[str, Any]) -> ToolResult:
            return await self._invoke_step(ctx, step, args)

        executor = ParallelExecutor(
            invoker,
            max_concurrency=self._max_concurrency,
            fail_fast=self._fail_fast,
            default_timeout=self._default_timeout,
            lock_manager=_RunLocks(self._locks, run_id),
            on_event=ctx.on_step_event,
        )
        execution = await executor.run(effective)
        succeeded = set(execution.succeeded)
        return RunReport(
            run_id=run_id,
            execution=execution,
            served_by={sid: tools for sid, tools in ctx.served.items() if sid in succeeded},
            attempts={sid: tuple(atts) for sid, atts in ctx.attempts.items()},
            resolutions=dict(ctx.resolutions),
            audit=tuple(ctx.audit),
            issues=tuple(issues),
            contentions=tuple(contentions),
            events=tuple(ctx.events),
        )

    async def call(
        self,
        target: str,
        args: Mapping[str, Any] | None = None,
        *,
        principal: Principal,
        version: str | None = None,
        timeout: float | None = None,
    ) -> ToolResult:
        """Run one tool (or ``capability:<name>``) and return its result; raise on failure."""
        if not isinstance(target, str) or not target:
            raise ValidationError(f"call target must be a non-empty string, got {target!r}")
        if target.startswith(CAPABILITY_PREFIX):
            if version is not None:
                raise ValidationError("version can only be pinned when calling a tool by name")
            step = Step(
                "call",
                capability=target[len(CAPABILITY_PREFIX):],
                args=dict(args or {}),
                timeout=timeout,
            )
        else:
            step = Step("call", tool=target, version=version, args=dict(args or {}), timeout=timeout)
        report = await self.run([step], principal)
        outcome = report["call"]
        if outcome.ok:
            assert outcome.result is not None
            return outcome.result
        _raise_outcome(outcome)
        raise AssertionError("unreachable")  # pragma: no cover

    async def ask(
        self,
        capability: str,
        args: Mapping[str, Any] | None = None,
        *,
        principal: Principal,
        n: int = 3,
        strategy: str | Callable[..., Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> Resolution:
        """Fan ``capability`` out to ``n`` permitted tools and return the reconciliation."""
        setting = FanOut(n=n, strategy=strategy, options=options or {})
        step = Step("ask", capability=capability, args=dict(args or {}))
        report = await self.run([step], principal, fanout={"ask": setting})
        outcome = report["ask"]
        if outcome.ok:
            return report.resolutions["ask"]
        _raise_outcome(outcome)
        raise AssertionError("unreachable")  # pragma: no cover

    # -- run preparation ------------------------------------------------------------

    def _validate_fanout(
        self, plan: Plan, fanout: Mapping[str, FanOut | int] | None
    ) -> dict[str, FanOut]:
        if fanout is None:
            return {}
        if not isinstance(fanout, Mapping):
            raise ValidationError(f"fanout must be a mapping of step id -> FanOut, got {fanout!r}")
        result: dict[str, FanOut] = {}
        for step_id, setting in fanout.items():
            if step_id not in plan:
                raise ValidationError(f"fan-out requested for unknown step {step_id!r}")
            step = plan[step_id]
            if step.tool is not None:
                raise ValidationError(
                    f"fan-out requested for step {step_id!r}, which names tool {step.target!r}; "
                    "fan-out applies to capability steps only"
                )
            if isinstance(setting, FanOut):
                result[step_id] = setting
            elif isinstance(setting, int) and not isinstance(setting, bool):
                result[step_id] = FanOut(n=setting)
            else:
                raise ValidationError(
                    f"fan-out setting for step {step_id!r} must be a FanOut or an int, "
                    f"got {setting!r}"
                )
        return result

    def _effective_plan(self, plan: Plan, issues: Iterable[PlanIssue]) -> Plan:
        denied = {i.step_id for i in issues if i.kind == "denied"}
        steps: list[Step] = []
        changed = False
        for step in plan:
            new = step
            if step.tool is not None:
                try:
                    spec = self._registry.get(step.tool, step.version)
                except ToolNotFoundError:
                    spec = None
                if spec is not None:
                    claims = _tool_claims(spec)
                    if claims:
                        new = dataclasses.replace(new, resources={**claims, **step.resources})
                if step.id in denied and step.retries:
                    new = dataclasses.replace(new, retries=0)
            changed = changed or new is not step
            steps.append(new)
        return Plan(steps) if changed else plan

    @staticmethod
    def _contentions(plan: Plan) -> list[tuple[str, str, str]]:
        claims = {step.id: step.resources for step in plan if step.resources}
        if len(claims) < 2:
            return []
        ancestors = _ancestors(plan)
        return [
            (resource, a, b)
            for resource, a, b in detect_write_conflicts(claims)
            if a not in ancestors[b] and b not in ancestors[a]
        ]

    # -- the per-run invoker -----------------------------------------------------------

    async def _invoke_step(self, ctx: _RunContext, step: Step, args: dict[str, Any]) -> ToolResult:
        attempt = ctx.next_invocation(step.id)
        if step.id in ctx.fanout:
            return await self._fanout_step(ctx, step, args, attempt, ctx.fanout[step.id])
        if step.tool is not None:
            return await self._named_step(ctx, step, args, attempt)
        return await self._capability_step(ctx, step, args, attempt)

    async def _named_step(
        self, ctx: _RunContext, step: Step, args: dict[str, Any], attempt: int
    ) -> ToolResult:
        assert step.tool is not None
        spec = self._registry.get(step.tool, step.version)
        if not self._registry.is_enabled(spec.name, spec.version):
            raise ExecutionError(f"tool {spec.name}@{spec.version} is disabled")
        try:
            spec.validate_input(args)
        except ValidationError as exc:
            ctx.add_attempt(
                step.id,
                ToolAttempt(spec.name, spec.version, "invalid_input", _describe(exc), 0.0, attempt),
            )
            raise
        ctx.emit(
            OrchestratorEvent("routed", step.id, tool=spec.name, attempt=attempt,
                              detail={"version": spec.version})
        )
        self._authorize(ctx, step, spec, attempt)
        result = await self._attempt_tool(ctx, step, spec, args, attempt)
        if result.ok:
            ctx.served[step.id] = (spec.name,)
        return result

    async def _capability_step(
        self, ctx: _RunContext, step: Step, args: dict[str, Any], attempt: int
    ) -> ToolResult:
        capability = step.capability
        assert capability is not None
        eligible = self._policy.predicate(ctx.principal)
        excluded: list[str] = []
        tried: list[ToolAttempt] = []
        failures = 0
        invoked = False
        first_invalid: ValidationError | None = None
        last_error: BaseException | None = None

        def record(entry: ToolAttempt) -> None:
            tried.append(entry)
            ctx.add_attempt(step.id, entry)

        while True:
            candidates = self._router.candidates(capability, eligible=eligible, exclude=excluded)
            if not candidates:
                if not invoked and first_invalid is not None:
                    raise first_invalid
                if not invoked:
                    self._router.route(capability, eligible=eligible, exclude=excluded)
                    raise NoRouteError(f"no permitted tool serves capability {capability!r}")
                raise FallbackExhaustedError(step.id, capability, tried) from last_error
            spec = candidates[0]
            excluded.append(spec.name)

            try:
                spec.validate_input(args)
            except ValidationError as exc:
                record(ToolAttempt(spec.name, spec.version, "invalid_input", _describe(exc),
                                   0.0, attempt))
                if first_invalid is None:
                    first_invalid = exc
                continue

            ctx.emit(
                OrchestratorEvent("routed", step.id, tool=spec.name, attempt=attempt,
                                  detail={"capability": capability, "version": spec.version})
            )
            try:
                self._authorize(ctx, step, spec, attempt, record=record)
            except AccessDeniedError as exc:  # defensive: predicate and check normally agree
                last_error = exc
                continue

            invoked = True
            try:
                result = await self._attempt_tool(ctx, step, spec, args, attempt, record=record)
            except Exception as exc:  # noqa: BLE001 - every tool failure may fall back
                last_error = exc
            else:
                if result.ok:
                    ctx.served[step.id] = (spec.name,)
                    return result
                last_error = ExecutionError(
                    result.error or f"tool {spec.name}@{spec.version} returned ok=False"
                )

            failures += 1
            if failures > self._max_fallbacks:
                raise FallbackExhaustedError(step.id, capability, tried) from last_error
            # Announce a fallback only to a candidate that will actually be routed: the
            # loop skips candidates rejecting the arguments without routing them.
            following = next(
                (
                    cand
                    for cand in self._router.candidates(
                        capability, eligible=eligible, exclude=excluded
                    )
                    if _accepts(cand, args)
                ),
                None,
            )
            if following is not None:
                ctx.emit(
                    OrchestratorEvent(
                        "fallback",
                        step.id,
                        tool=spec.name,
                        attempt=attempt,
                        error=_describe(last_error),
                        detail={"capability": capability, "next": following.name},
                    )
                )

    async def _fanout_step(
        self,
        ctx: _RunContext,
        step: Step,
        args: dict[str, Any],
        attempt: int,
        setting: FanOut,
    ) -> ToolResult:
        capability = step.capability
        assert capability is not None
        eligible = self._policy.predicate(ctx.principal)
        selected: list[ToolSpec] = []
        excluded: list[str] = []
        first_invalid: ValidationError | None = None
        for spec in self._router.candidates(capability, eligible=eligible):
            if len(selected) >= setting.n:
                break
            excluded.append(spec.name)
            try:
                spec.validate_input(args)
            except ValidationError as exc:
                ctx.add_attempt(
                    step.id,
                    ToolAttempt(spec.name, spec.version, "invalid_input", _describe(exc), 0.0,
                                attempt),
                )
                if first_invalid is None:
                    first_invalid = exc
                continue
            selected.append(spec)
        if not selected:
            if first_invalid is not None:
                raise first_invalid
            self._router.route(capability, eligible=eligible, exclude=excluded)
            raise NoRouteError(f"no permitted tool serves capability {capability!r}")

        started = time.perf_counter()
        denials: list[AccessDeniedError] = []

        async def one(spec: ToolSpec) -> ToolResult | None:
            # Route, authorize and start the handler with no suspension point in between:
            # a member that ran earlier in this fan-out may have changed the policy, so
            # each member is checked right before its own invocation.
            ctx.emit(
                OrchestratorEvent("routed", step.id, tool=spec.name, attempt=attempt,
                                  detail={"capability": capability, "version": spec.version,
                                          "fanout": setting.n})
            )
            try:
                self._authorize(ctx, step, spec, attempt)
            except AccessDeniedError as exc:
                denials.append(exc)
                return None
            cap = spec.capability(capability)
            priority = cap.priority if cap is not None else 0
            try:
                result = await self._attempt_tool(ctx, step, spec, args, attempt)
            except Exception as exc:  # noqa: BLE001 - a failed member is just ok=False
                return ToolResult(
                    spec.name, spec.version, ok=False, error=_describe(exc),
                    metadata={"priority": priority},
                )
            return dataclasses.replace(
                result, metadata={**(result.metadata or {}), "priority": priority}
            )

        # return_exceptions: an unexpected error in one member must not leave the other
        # members running unobserved after this step has failed.
        outcomes = await asyncio.gather(*(one(spec) for spec in selected), return_exceptions=True)
        for item in outcomes:
            if isinstance(item, BaseException):
                raise item
        results = [item for item in outcomes if item is not None]
        if not results:
            raise denials[0]
        resolution = self._resolver.resolve(results, setting.strategy, **setting.options)
        ctx.resolutions[step.id] = resolution
        ctx.served[step.id] = tuple(resolution.winners)
        ctx.emit(
            OrchestratorEvent(
                "resolved",
                step.id,
                attempt=attempt,
                detail={
                    "capability": capability,
                    "strategy": resolution.strategy,
                    "winners": resolution.winners,
                    "dissenters": resolution.dissenters,
                    "confidence": resolution.confidence,
                },
            )
        )
        return ToolResult(
            tool=f"fanout:{capability}",
            version="",
            ok=True,
            value=resolution.value,
            duration=time.perf_counter() - started,
            metadata={"resolution": resolution},
        )

    # -- authorization and invocation -----------------------------------------------------

    def _authorize(
        self,
        ctx: _RunContext,
        step: Step,
        spec: ToolSpec,
        attempt: int,
        *,
        record: Callable[[ToolAttempt], None] | None = None,
    ) -> None:
        """Audited check; captures this run's audit record; emits ``denied`` on denial."""
        before = self._policy.audit_log
        last_before = before[-1] if before else None
        try:
            self._policy.check(ctx.principal, spec)
        except AccessDeniedError as exc:
            self._capture_audit(ctx, spec, last_before)
            entry = ToolAttempt(spec.name, spec.version, "denied", _describe(exc), 0.0, attempt)
            (record or (lambda a: ctx.add_attempt(step.id, a)))(entry)
            ctx.emit(
                OrchestratorEvent("denied", step.id, tool=spec.name, attempt=attempt,
                                  error=_describe(exc), detail={"version": spec.version})
            )
            raise
        self._capture_audit(ctx, spec, last_before)

    def _capture_audit(
        self, ctx: _RunContext, spec: ToolSpec, last_before: AuditRecord | None
    ) -> None:
        after = self._policy.audit_log
        if not after:
            return
        newest = after[-1]
        if (
            newest is not last_before
            and newest.principal_id == ctx.principal.id
            and newest.tool == spec.name
            and newest.version == spec.version
        ):
            ctx.audit.append(newest)

    async def _attempt_tool(
        self,
        ctx: _RunContext,
        step: Step,
        spec: ToolSpec,
        args: dict[str, Any],
        attempt: int,
        *,
        record: Callable[[ToolAttempt], None] | None = None,
    ) -> ToolResult:
        """Invoke ``spec`` once, recording the attempt and feeding router health."""
        add = record or (lambda a: ctx.add_attempt(step.id, a))
        started = time.perf_counter()
        try:
            result = await self._invoke_handler(spec, args)
        except asyncio.CancelledError as exc:
            if _being_cancelled():
                add(ToolAttempt(spec.name, spec.version, "cancelled", "cancelled",
                                time.perf_counter() - started, attempt))
                raise
            # Nobody cancelled this task: the tool itself raised CancelledError (e.g. it
            # awaited a future someone else cancelled). Letting it propagate would make
            # the executor treat the step as cancelled and abort the whole run, so it is
            # a tool failure like any other.
            error = ExecutionError(f"tool {spec.name}@{spec.version} raised CancelledError")
            self._router.record_failure(spec.name)
            add(ToolAttempt(spec.name, spec.version, "error", _describe(error),
                            time.perf_counter() - started, attempt))
            raise error from exc
        except ToolTimeoutError as exc:
            self._router.record_failure(spec.name)
            add(ToolAttempt(spec.name, spec.version, "timeout", _describe(exc),
                            time.perf_counter() - started, attempt))
            raise
        except Exception as exc:
            self._router.record_failure(spec.name)
            add(ToolAttempt(spec.name, spec.version, "error", _describe(exc),
                            time.perf_counter() - started, attempt))
            raise
        if result.ok:
            self._router.record_success(spec.name, latency=result.duration)
            add(ToolAttempt(spec.name, spec.version, "ok", None, result.duration, attempt))
        else:
            self._router.record_failure(spec.name)
            error = result.error or f"tool {spec.name}@{spec.version} returned ok=False"
            add(ToolAttempt(spec.name, spec.version, "error", error, result.duration, attempt))
        return result

    async def _invoke_handler(self, spec: ToolSpec, args: dict[str, Any]) -> ToolResult:
        """Call the handler under ``tool_timeout`` and wrap its value in a ToolResult."""
        started = time.perf_counter()
        deadline = asyncio.timeout(self._tool_timeout)
        try:
            async with deadline:
                value = await spec.handler(**args)
        except TimeoutError as exc:
            if deadline.expired():
                raise ToolTimeoutError(
                    f"tool {spec.name}@{spec.version} timed out after {self._tool_timeout}s"
                ) from None
            if isinstance(exc, ToolTimeoutError):
                raise
            # The tool's own TimeoutError (e.g. an upstream client timing out). Passed on
            # bare, the executor would take it for the *step* timing out and replace it
            # with a misleading "step timed out" error, so wrap it as a tool failure.
            raise ExecutionError(
                f"tool {spec.name}@{spec.version} failed: {_describe(exc)}"
            ) from exc
        duration = time.perf_counter() - started
        if isinstance(value, ToolResult):
            return dataclasses.replace(
                value, tool=spec.name, version=spec.version, duration=duration
            )
        return ToolResult(spec.name, spec.version, ok=True, value=value, duration=duration)


def _raise_outcome(outcome: StepOutcome) -> None:
    if outcome.exception is not None:
        raise outcome.exception
    raise ExecutionError(outcome.error or f"step {outcome.step_id!r} {outcome.status}")
