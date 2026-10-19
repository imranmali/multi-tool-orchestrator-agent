"""Tests for the orchestrator-agent capability (src/orchestrator/agent.py)."""

from __future__ import annotations

import asyncio
import dataclasses
import tomllib
from pathlib import Path
from typing import Any

import pytest

import orchestrator
from orchestrator import agent as agent_mod
from orchestrator import conflicts as conflicts_mod
from orchestrator import executor as executor_mod
from orchestrator import permissions as permissions_mod
from orchestrator import router as router_mod
from orchestrator.agent import (
    FallbackExhaustedError,
    FanOut,
    Orchestrator,
    OrchestratorEvent,
    PlanIssue,
    PreflightError,
    RunReport,
    ToolAttempt,
)
from orchestrator.conflicts import ConflictResolver, Resolution, ResourceLockManager
from orchestrator.core import (
    Capability,
    ConflictError,
    ExecutionError,
    NoRouteError,
    OrchestratorError,
    ToolNotFoundError,
    ToolResult,
    ToolSpec,
    ToolTimeoutError,
    ValidationError,
)
from orchestrator.executor import ExecutionReport, Plan, Ref, Step, StepOutcome
from orchestrator.permissions import AccessDeniedError, PermissionPolicy, Principal
from orchestrator.registry import ToolRegistry
from orchestrator.router import CapabilityRouter

ROOT = Path(__file__).resolve().parent.parent

USER = Principal("user")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make(
    name: str,
    caps: tuple[str, ...] = ("search",),
    *,
    priority: int = 0,
    scopes: tuple[str, ...] = (),
    value: Any = None,
    fn: Any = None,
    version: str = "1.0.0",
    schema: dict[str, type] | None = None,
    metadata: dict[str, Any] | None = None,
    calls: list[str] | None = None,
) -> ToolSpec:
    """A tool recording its invocations in ``calls`` and returning ``value`` (or its name)."""

    async def handler(**kwargs: Any) -> Any:
        if calls is not None:
            calls.append(name)
        if fn is not None:
            return await fn(**kwargs)
        return name if value is None else value

    return ToolSpec(
        name=name,
        version=version,
        capabilities=tuple(Capability(c, priority=priority) for c in caps),
        handler=handler,
        required_scopes=frozenset(scopes),
        input_schema=schema or {},
        metadata=metadata or {},
    )


async def boom(**kwargs: Any) -> Any:
    raise RuntimeError("boom")


async def slow(**kwargs: Any) -> Any:
    await asyncio.sleep(1)
    return "slow"


def make_orch(*tools: ToolSpec, **kwargs: Any) -> Orchestrator:
    """Orchestrator with a priority-ranked router (deterministic ordering)."""
    registry = ToolRegistry()
    for spec in tools:
        registry.register(spec)
    router = CapabilityRouter(registry, strategy="priority")
    return Orchestrator(registry=registry, router=router, **kwargs)


def step_events(report: RunReport, step_id: str) -> list[tuple[str, str | None]]:
    return [(e.kind, e.tool) for e in report.events if e.step_id == step_id]


# ---------------------------------------------------------------------------
# 1.1 value types and errors
# ---------------------------------------------------------------------------


class TestValueTypes:
    def test_fanout_defaults_and_options_copied(self):
        options = {"quorum": 0.5}
        setting = FanOut(options=options)
        options["quorum"] = 0.9
        assert setting.n == 3 and setting.strategy is None
        assert setting.options == {"quorum": 0.5}

    @pytest.mark.parametrize("n", [0, -1, True, 2.0, "3"])
    def test_fanout_rejects_bad_n(self, n):
        with pytest.raises(ValidationError):
            FanOut(n=n)

    def test_fanout_rejects_bad_strategy_and_options(self):
        with pytest.raises(ValidationError):
            FanOut(strategy=5)
        with pytest.raises(ValidationError):
            FanOut(options=[("quorum", 1)])

    def test_fanout_is_frozen(self):
        with pytest.raises(AttributeError):
            FanOut().n = 2  # type: ignore[misc]

    def test_tool_attempt_ok(self):
        assert ToolAttempt("a", "1.0.0", "ok", None, 0.1, 1).ok
        for outcome in ("error", "timeout", "invalid_input", "denied", "cancelled"):
            assert not ToolAttempt("a", "1.0.0", outcome, "x", 0.1, 1).ok

    def test_event_defaults(self):
        event = OrchestratorEvent("routed", "s", tool="a")
        assert event.attempt == 0 and event.error is None and event.detail == {}
        assert event.at > 0

    def _report(self) -> RunReport:
        execution = ExecutionReport(
            results={
                "a": StepOutcome("a", "succeeded", result=ToolResult("t", "1.0.0", True, 1)),
                "b": StepOutcome("b", "failed", error="RuntimeError: boom"),
                "c": StepOutcome("c", "skipped", error="skipped: dependency b failed"),
                "d": StepOutcome("d", "cancelled", error="cancelled"),
            },
            started_at=1.0,
            finished_at=3.5,
        )
        return RunReport(
            run_id="run-1",
            execution=execution,
            served_by={"a": ("t",)},
            attempts={
                "a": (
                    ToolAttempt("x", "1.0.0", "error", "boom", 0.1, 1),
                    ToolAttempt("t", "1.0.0", "ok", None, 0.1, 1),
                )
            },
            contentions=(("db", "a", "b"),),
        )

    def test_run_report_delegation(self):
        report = self._report()
        assert not report.ok
        assert report.outputs == {"a": 1}
        assert report.results is report.execution.results
        assert report.succeeded == ["a"]
        assert report.failed == ["b"]
        assert report.skipped == ["c"]
        assert report.cancelled == ["d"]
        assert report.duration == pytest.approx(2.5)
        assert report["a"].value == 1
        with pytest.raises(ExecutionError, match="b: RuntimeError: boom"):
            report.raise_for_failure()

    def test_run_report_chains_and_warnings(self):
        report = self._report()
        assert report.chains == {"a": ("x", "t")}
        assert len(report.warnings) == 1
        assert "'a'" in report.warnings[0] and "'b'" in report.warnings[0]
        assert "'db'" in report.warnings[0] and "serialize" in report.warnings[0]

    def test_preflight_error(self):
        issue = PlanIssue("s", "denied", "fs.write", "principal 'u' may not use tool")
        err = PreflightError([issue])
        assert isinstance(err, OrchestratorError)
        assert err.issues == (issue,)
        assert "s (denied)" in str(err)

    def test_fallback_exhausted_error(self):
        attempts = [
            ToolAttempt("flaky", "1.0.0", "error", "RuntimeError: boom", 0.0, 1),
            ToolAttempt("beta", "1.0.0", "timeout", "ToolTimeoutError: late", 0.0, 1),
        ]
        err = FallbackExhaustedError("s", "search", attempts)
        assert isinstance(err, ExecutionError)
        assert err.step_id == "s" and err.capability == "search"
        assert err.attempts == tuple(attempts)
        assert "flaky: RuntimeError: boom" in str(err)
        assert "beta: ToolTimeoutError: late" in str(err)

    def test_module_all(self):
        assert sorted(agent_mod.__all__) == sorted(
            [
                "Orchestrator",
                "FanOut",
                "RunReport",
                "ToolAttempt",
                "PlanIssue",
                "OrchestratorEvent",
                "PreflightError",
                "FallbackExhaustedError",
            ]
        )


# ---------------------------------------------------------------------------
# 1.2 configuration, registration, lifecycle
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_defaults(self):
        orch = Orchestrator()
        assert isinstance(orch.registry, ToolRegistry) and len(orch.registry) == 0
        assert isinstance(orch.policy, PermissionPolicy)
        assert isinstance(orch.router, CapabilityRouter)
        assert isinstance(orch.resolver, ConflictResolver)
        assert isinstance(orch.lock_manager, ResourceLockManager)
        assert orch.router._registry is orch.registry
        assert orch.max_concurrency == 8
        assert orch.fail_fast is False
        assert orch.default_timeout is None and orch.tool_timeout is None
        assert orch.max_fallbacks == 2
        assert orch.on_event is None

    def test_negative_fallback_limit(self):
        with pytest.raises(ValidationError):
            Orchestrator(max_fallbacks=-1)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_concurrency": 0},
            {"max_concurrency": True},
            {"max_concurrency": 2.0},
            {"default_timeout": 0},
            {"default_timeout": -1.0},
            {"default_timeout": True},
            {"tool_timeout": 0},
            {"tool_timeout": "1"},
            {"max_fallbacks": True},
            {"max_fallbacks": 1.5},
            {"fail_fast": "yes"},
            {"on_event": 3},
        ],
    )
    def test_invalid_settings(self, kwargs):
        with pytest.raises(ValidationError):
            Orchestrator(**kwargs)

    def test_router_bound_to_another_registry(self):
        a, b = ToolRegistry(), ToolRegistry()
        with pytest.raises(ValidationError):
            Orchestrator(registry=a, router=CapabilityRouter(b))

    def test_router_registry_adopted(self):
        registry = ToolRegistry()
        router = CapabilityRouter(registry)
        orch = Orchestrator(router=router)
        assert orch.registry is registry and orch.router is router

    def test_collaborators_used(self):
        registry = ToolRegistry()
        policy, resolver, locks = PermissionPolicy(), ConflictResolver(), ResourceLockManager()
        orch = Orchestrator(registry=registry, policy=policy, resolver=resolver, lock_manager=locks)
        assert orch.registry is registry and orch.policy is policy
        assert orch.resolver is resolver and orch.lock_manager is locks

    def test_properties_read_only(self):
        orch = Orchestrator()
        for attr in ("registry", "policy", "router", "max_fallbacks", "tool_timeout"):
            with pytest.raises(AttributeError):
                setattr(orch, attr, None)


class TestRegistration:
    def test_register_descriptor(self):
        orch = Orchestrator()
        spec = make("calc", ("calc",))
        assert orch.register(spec) is spec
        assert orch.registry.get("calc") is spec

    def test_register_rejects_non_spec(self):
        with pytest.raises(ValidationError):
            Orchestrator().register("calc")  # type: ignore[arg-type]

    async def test_decorate_function(self):
        orch = Orchestrator()

        async def add(a: int, b: int) -> int:
            return a + b

        decorated = orch.tool(name="math.add", capabilities=["calc"], scopes=["calc:use"])(add)
        assert decorated is add
        spec = orch.registry.get("math.add")
        assert spec.capability_names == {"calc"}
        assert spec.required_scopes == {"calc:use"}
        assert await add(2, 3) == 5

    async def test_decorator_defaults(self):
        orch = Orchestrator()

        @orch.tool
        async def echo(**kwargs: Any) -> Any:
            """Echo input.

            Longer description.
            """
            return kwargs

        spec = orch.registry.get("echo")
        assert spec.version == "1.0.0"
        assert spec.capability_names == {"echo"}
        assert spec.description == "Echo input."
        assert await echo(x=1) == {"x": 1}

    def test_decorator_options(self):
        orch = Orchestrator()

        @orch.tool(
            name="writer",
            version="2.1.0",
            capabilities=[Capability("write", priority=4)],
            scopes="fs:write",
            input_schema={"path": str},
            description="Writes.",
            metadata={"resources": {"fs": "write"}},
        )
        async def write(path: str) -> str:
            return path

        spec = orch.registry.get("writer", "2.1.0")
        assert spec.capability("write").priority == 4
        assert spec.required_scopes == {"fs:write"}
        assert spec.input_schema == {"path": str}
        assert spec.description == "Writes."
        assert spec.metadata == {"resources": {"fs": "write"}}

    def test_decorator_rejects_sync_function_and_duplicates(self):
        orch = Orchestrator()
        with pytest.raises(ValidationError):

            @orch.tool
            def sync_tool() -> None:  # pragma: no cover - never called
                pass

        @orch.tool
        async def dup() -> int:
            return 1

        with pytest.raises(orchestrator.DuplicateToolError):

            @orch.tool(name="dup")
            async def dup2() -> int:  # pragma: no cover - never called
                return 2

        @orch.tool(name="dup", replace=True)
        async def dup3() -> int:
            return 3

        assert orch.registry.get("dup").handler is dup3


# ---------------------------------------------------------------------------
# 1.3 plan execution, named-tool steps, permissions, audit, events
# ---------------------------------------------------------------------------


class TestPlanExecution:
    async def test_independent_steps_overlap_and_outputs_flow(self):
        sig_a, sig_b = asyncio.Event(), asyncio.Event()

        async def ping() -> str:
            sig_a.set()
            await asyncio.wait_for(sig_b.wait(), 1)
            return "ping"

        async def pong() -> str:
            sig_b.set()
            await asyncio.wait_for(sig_a.wait(), 1)
            return "pong"

        async def join(x: str, y: str) -> str:
            return f"{x}+{y}"

        orch = make_orch(
            make("ping", ("p",), fn=ping),
            make("pong", ("p",), fn=pong),
            make("join", ("j",), fn=join),
        )
        plan = Plan(
            [
                Step("a", tool="ping"),
                Step("b", tool="pong"),
                Step("c", tool="join", args={"x": Ref("a"), "y": Ref("b")}),
            ]
        )
        report = await orch.run(plan, USER)
        assert report.ok
        assert report.outputs == {"a": "ping", "b": "pong", "c": "ping+pong"}
        assert report.served_by == {"a": ("ping",), "b": ("pong",), "c": ("join",)}

    async def test_sequence_of_steps_accepted(self):
        orch = make_orch(make("calc", ("calc",), value=1))
        report = await orch.run([Step("one", tool="calc"), Step("two", tool="calc")], USER)
        assert report.ok and report.outputs == {"one": 1, "two": 1}
        assert isinstance(report, RunReport) and report.run_id.startswith("run-")

    async def test_invalid_plans_rejected(self):
        orch = make_orch(make("calc", ("calc",)))
        with pytest.raises(ValidationError):
            await orch.run([Step("a", tool="calc"), Step("a", tool="calc")], USER)
        with pytest.raises(ValidationError):
            await orch.run(Step("a", tool="calc"), USER)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            await orch.run([Step("a", tool="calc")], "user")  # type: ignore[arg-type]

    async def test_run_ids_unique(self):
        orch = make_orch(make("calc", ("calc",)))
        r1 = await orch.run([Step("a", tool="calc")], USER)
        r2 = await orch.run([Step("a", tool="calc")], USER)
        assert r1.run_id != r2.run_id


class TestNamedToolSteps:
    async def test_pinned_version(self):
        orch = make_orch(
            make("calc", ("calc",), value="v1", version="1.0.0"),
            make("calc", ("calc",), value="v2", version="2.0.0"),
        )
        report = await orch.run(
            [Step("pinned", tool="calc", version="1.0.0"), Step("latest", tool="calc")], USER
        )
        assert report["pinned"].result.version == "1.0.0"
        assert report["pinned"].value == "v1"
        assert report["latest"].result.version == "2.0.0"
        assert report["pinned"].result.tool == "calc"

    async def test_disabled_tool(self):
        calls: list[str] = []
        orch = make_orch(make("calc", ("calc",), calls=calls))
        orch.registry.disable("calc")
        report = await orch.run([Step("s", tool="calc")], USER)
        assert report.failed == ["s"]
        assert isinstance(report["s"].exception, ExecutionError)
        assert "disabled" in report["s"].error
        assert calls == []

    async def test_invalid_input(self):
        calls: list[str] = []
        orch = make_orch(make("calc", ("calc",), schema={"a": int}, calls=calls))
        report = await orch.run([Step("s", tool="calc", args={"a": "x"})], USER)
        assert report.failed == ["s"]
        assert isinstance(report["s"].exception, ValidationError)
        assert calls == []
        assert [a.outcome for a in report.attempts["s"]] == ["invalid_input"]
        assert report.audit == () and orch.policy.audit_log == []
        assert orch.router.stats("calc").calls == 0

    async def test_unknown_tool(self):
        orch = make_orch()
        report = await orch.run([Step("s", tool="nope")], USER)
        assert isinstance(report["s"].exception, ToolNotFoundError)

    async def test_handler_returning_unsuccessful_result(self):
        async def fail(**kwargs: Any) -> ToolResult:
            return ToolResult("ignored", "0.0.0", ok=False, error="upstream said no")

        orch = make_orch(make("calc", ("calc",), fn=fail))
        report = await orch.run([Step("s", tool="calc")], USER)
        assert report.failed == ["s"]
        assert report["s"].error == "upstream said no"
        assert report["s"].result.tool == "calc" and report["s"].result.version == "1.0.0"
        assert orch.router.stats("calc").failures == 1
        assert report.attempts["s"][0].outcome == "error"

    async def test_handler_returning_successful_result_passes_through(self):
        async def ok(**kwargs: Any) -> ToolResult:
            return ToolResult("x", "9.9.9", ok=True, value=7, metadata={"src": "cache"})

        orch = make_orch(make("calc", ("calc",), fn=ok))
        result = (await orch.run([Step("s", tool="calc")], USER))["s"].result
        assert (result.tool, result.version, result.value) == ("calc", "1.0.0", 7)
        assert result.metadata == {"src": "cache"}

    async def test_named_tool_feeds_router_health(self):
        orch = make_orch(make("calc", ("calc",)), make("bad", ("calc",), fn=boom))
        await orch.run([Step("a", tool="calc"), Step("b", tool="bad")], USER)
        assert orch.router.stats("calc").successes == 1
        assert orch.router.stats("calc").avg_latency is not None
        assert orch.router.stats("bad").failures == 1

    async def test_named_tool_timeout(self):
        orch = make_orch(make("slow", ("calc",), fn=slow), tool_timeout=0.02)
        report = await orch.run([Step("s", tool="slow")], USER)
        assert isinstance(report["s"].exception, ToolTimeoutError)
        assert report.attempts["s"][0].outcome == "timeout"
        assert orch.router.stats("slow").failures == 1


class TestPermissionEnforcement:
    async def test_denied_tool_step(self):
        calls: list[str] = []
        orch = make_orch(
            make("fs.write", ("write",), scopes=("fs:write",), calls=calls),
            make("after", ("other",), calls=calls),
        )
        plan = [Step("w", tool="fs.write"), Step("next", tool="after", depends_on=("w",))]
        report = await orch.run(plan, USER)
        assert report.failed == ["w"]
        assert isinstance(report["w"].exception, AccessDeniedError)
        assert report.skipped == ["next"]
        assert calls == []
        assert [a.outcome for a in report.attempts["w"]] == ["denied"]
        assert len(report.audit) == 1 and not report.audit[0].allowed

    async def test_allowed_invocation_is_audited(self):
        orch = make_orch(make("fs.write", ("write",), scopes=("fs:write",)))
        writer = Principal("writer", scopes={"fs:write"})
        report = await orch.run([Step("w", tool="fs.write")], writer)
        assert report.ok
        log = orch.policy.audit_log
        assert len(log) == 1
        assert (log[0].principal_id, log[0].tool, log[0].allowed) == ("writer", "fs.write", True)
        assert report.audit == (log[0],)

    async def test_role_granted_scope(self):
        policy = PermissionPolicy({"writers": ["fs:*"]})
        registry = ToolRegistry()
        registry.register(make("fs.write", ("write",), scopes=("fs:write",)))
        orch = Orchestrator(registry=registry, policy=policy)
        report = await orch.run([Step("w", tool="fs.write")], Principal("u", roles={"writers"}))
        assert report.ok

    async def test_audit_records_scoped_to_run(self):
        orch = make_orch(make("calc", ("calc",)))
        r1 = await orch.run([Step("a", tool="calc")], USER)
        r2 = await orch.run([Step("a", tool="calc"), Step("b", tool="calc")], USER)
        log = orch.policy.audit_log
        assert len(log) == 3
        assert r1.audit == (log[0],)
        assert set(map(id, r2.audit)) == {id(log[1]), id(log[2])}

    async def test_audit_not_captured_when_disabled(self):
        registry = ToolRegistry()
        registry.register(make("calc", ("calc",)))
        orch = Orchestrator(registry=registry, policy=PermissionPolicy(audit_limit=0))
        report = await orch.run([Step("a", tool="calc")], USER)
        assert report.ok and report.audit == ()

    async def test_denial_event(self):
        orch = make_orch(make("fs.write", ("write",), scopes=("fs:write",)))
        report = await orch.run([Step("w", tool="fs.write")], USER)
        denied = [e for e in report.events if e.kind == "denied"]
        assert len(denied) == 1
        assert denied[0].step_id == "w" and denied[0].tool == "fs.write"
        assert "AccessDeniedError" in denied[0].error

    async def test_listener_failure_isolated(self):
        seen: list[OrchestratorEvent] = []

        def listener(event: OrchestratorEvent) -> None:
            seen.append(event)
            raise RuntimeError("listener broke")

        orch = make_orch(make("calc", ("calc",)), on_event=listener)
        report = await orch.run([Step("a", tool="calc")], USER)
        assert report.ok
        assert tuple(seen) == report.events
        assert [e.kind for e in report.events] == ["started", "routed", "succeeded"]

    async def test_events_carry_executor_details(self):
        orch = make_orch(make("bad", ("calc",), fn=boom))
        report = await orch.run([Step("a", tool="bad", retries=1)], USER)
        kinds = [e.kind for e in report.events]
        assert kinds == ["started", "routed", "retrying", "routed", "failed"]
        retry = report.events[2]
        assert retry.attempt == 1 and "boom" in retry.error
        assert [a.attempt for a in report.attempts["a"]] == [1, 2]


# ---------------------------------------------------------------------------
# 1.4 capability routing, fallback, per-tool timeout, health
# ---------------------------------------------------------------------------


class TestCapabilityRouting:
    async def test_unpermitted_better_tool_skipped(self):
        calls: list[str] = []
        orch = make_orch(
            make("premium", priority=10, scopes=("paid",), calls=calls),
            make("basic", priority=1, calls=calls),
        )
        report = await orch.run([Step("s", capability="search")], USER)
        assert report.ok and report.served_by["s"] == ("basic",)
        assert calls == ["basic"]

    async def test_no_permitted_tool(self):
        calls: list[str] = []
        orch = make_orch(make("premium", scopes=("paid",), calls=calls))
        report = await orch.run([Step("s", capability="search")], USER)
        assert isinstance(report["s"].exception, NoRouteError)
        assert calls == []

    async def test_no_tool_at_all(self):
        report = await make_orch().run([Step("s", capability="search")], USER)
        assert isinstance(report["s"].exception, NoRouteError)


class TestFallback:
    async def test_fallback_to_next_candidate(self):
        calls: list[str] = []
        orch = make_orch(
            make("flaky", priority=10, fn=boom, calls=calls),
            make("alpha", priority=5, value="alpha-answer", calls=calls),
        )
        report = await orch.run([Step("s", capability="search")], USER)
        assert report.ok and report.outputs["s"] == "alpha-answer"
        assert calls == ["flaky", "alpha"]
        assert orch.router.stats("flaky").failures == 1
        # report contents after fallback
        assert report.served_by["s"] == ("alpha",)
        assert report.chains["s"] == ("flaky", "alpha")
        assert [a.outcome for a in report.attempts["s"]] == ["error", "ok"]
        assert "RuntimeError: boom" in report.attempts["s"][0].error
        assert report["s"].result.tool == "alpha"

    async def test_fallback_limit(self):
        calls: list[str] = []
        orch = make_orch(
            make("t1", priority=3, fn=boom, calls=calls),
            make("t2", priority=2, fn=boom, calls=calls),
            make("t3", priority=1, fn=boom, calls=calls),
            max_fallbacks=1,
        )
        report = await orch.run([Step("s", capability="search")], USER)
        assert calls == ["t1", "t2"]
        exc = report["s"].exception
        assert isinstance(exc, FallbackExhaustedError) and isinstance(exc, ExecutionError)
        assert "t1" in str(exc) and "t2" in str(exc) and "t3" not in str(exc)
        assert [a.tool for a in exc.attempts] == ["t1", "t2"]
        assert exc.step_id == "s" and exc.capability == "search"
        assert isinstance(exc.__cause__, RuntimeError)

    async def test_fallback_disabled(self):
        calls: list[str] = []
        orch = make_orch(
            make("t1", priority=3, fn=boom, calls=calls),
            make("t2", priority=2, calls=calls),
            max_fallbacks=0,
        )
        report = await orch.run([Step("s", capability="search")], USER)
        assert calls == ["t1"]
        assert report.failed == ["s"]
        assert not [e for e in report.events if e.kind == "fallback"]

    async def test_candidates_run_out(self):
        orch = make_orch(
            make("t1", priority=2, fn=boom), make("t2", priority=1, fn=boom), max_fallbacks=5
        )
        report = await orch.run([Step("s", capability="search")], USER)
        exc = report["s"].exception
        assert isinstance(exc, FallbackExhaustedError)
        assert [a.tool for a in exc.attempts] == ["t1", "t2"]

    async def test_unsuccessful_result_falls_back(self):
        async def not_ok(**kwargs: Any) -> ToolResult:
            return ToolResult("x", "1.0.0", ok=False, error="nope")

        orch = make_orch(make("t1", priority=2, fn=not_ok), make("t2", priority=1))
        report = await orch.run([Step("s", capability="search")], USER)
        assert report.served_by["s"] == ("t2",)
        assert report.attempts["s"][0].error == "nope"
        assert orch.router.stats("t1").failures == 1

    async def test_fallback_events(self):
        orch = make_orch(make("flaky", priority=10, fn=boom), make("alpha", priority=5))
        report = await orch.run([Step("s", capability="search")], USER)
        assert step_events(report, "s") == [
            ("started", None),
            ("routed", "flaky"),
            ("fallback", "flaky"),
            ("routed", "alpha"),
            ("succeeded", None),
        ]
        fallback = next(e for e in report.events if e.kind == "fallback")
        assert fallback.detail["next"] == "alpha"
        assert "boom" in fallback.error

    async def test_each_executor_retry_starts_fresh(self):
        calls: list[str] = []
        orch = make_orch(
            make("t1", priority=2, fn=boom, calls=calls),
            make("t2", priority=1, fn=boom, calls=calls),
            max_fallbacks=1,
        )
        report = await orch.run([Step("s", capability="search", retries=1)], USER)
        assert calls == ["t1", "t2", "t1", "t2"]
        assert [a.attempt for a in report.attempts["s"]] == [1, 1, 2, 2]


class TestPerToolTimeout:
    async def test_slow_tool_falls_back(self):
        orch = make_orch(
            make("sloth", priority=10, fn=slow), make("hare", priority=1, value="fast"),
            tool_timeout=0.05,
        )
        report = await orch.run([Step("s", capability="search")], USER)
        assert report.ok and report.outputs["s"] == "fast"
        first = report.attempts["s"][0]
        assert (first.tool, first.outcome) == ("sloth", "timeout")
        assert "ToolTimeoutError" in first.error
        assert orch.router.stats("sloth").failures == 1

    async def test_step_timeout_cancels_without_penalty(self):
        orch = make_orch(make("sloth", priority=10, fn=slow), make("hare", priority=1))
        report = await orch.run([Step("s", capability="search", timeout=0.05)], USER)
        assert report.failed == ["s"]
        assert isinstance(report["s"].exception, ToolTimeoutError)
        assert [a.outcome for a in report.attempts["s"]] == ["cancelled"]
        assert orch.router.stats("sloth").calls == 0

    async def test_tool_raising_timeout_error_itself_is_an_error(self):
        async def raises_timeout(**kwargs: Any) -> Any:
            raise TimeoutError("own timeout")

        orch = make_orch(make("t1", priority=2, fn=raises_timeout), make("t2"), tool_timeout=5)
        report = await orch.run([Step("s", capability="search")], USER)
        assert report.ok
        assert report.attempts["s"][0].outcome == "error"


class TestRoutingHealth:
    async def test_success_recorded_with_latency(self):
        orch = make_orch(make("alpha"))
        await orch.run([Step("s", capability="search")], USER)
        stats = orch.router.stats("alpha")
        assert stats.successes == 1 and stats.failures == 0
        assert stats.avg_latency is not None and stats.avg_latency >= 0

    async def test_invalid_input_does_not_penalise(self):
        orch = make_orch(
            make("strict", priority=10, schema={"q": int}), make("loose", priority=1)
        )
        report = await orch.run([Step("s", capability="search", args={"q": "text"})], USER)
        assert report.ok and report.served_by["s"] == ("loose",)
        assert orch.router.stats("strict").failures == 0
        assert orch.router.stats("strict").calls == 0
        assert [a.outcome for a in report.attempts["s"]] == ["invalid_input", "ok"]
        assert [e.tool for e in report.events if e.kind == "routed"] == ["loose"]

    async def test_invalid_input_everywhere_raises_validation_error(self):
        calls: list[str] = []
        orch = make_orch(make("strict", schema={"q": int}, calls=calls))
        report = await orch.run([Step("s", capability="search", args={"q": "x"})], USER)
        assert isinstance(report["s"].exception, ValidationError)
        assert calls == []

    async def test_invalid_input_skips_do_not_consume_fallback_budget(self):
        orch = make_orch(
            make("strict", priority=10, schema={"q": int}),
            make("bad", priority=5, fn=boom),
            make("good", priority=1),
            max_fallbacks=1,
        )
        report = await orch.run([Step("s", capability="search", args={"q": "x"})], USER)
        assert report.ok and report.chains["s"] == ("strict", "bad", "good")


# ---------------------------------------------------------------------------
# 1.5 pre-flight and retry suppression
# ---------------------------------------------------------------------------


class TestPreflight:
    def _orch(self, calls: list[str]) -> Orchestrator:
        orch = make_orch(
            make("calc", ("calc",), calls=calls),
            make("fs.write", ("write",), scopes=("fs:write",), calls=calls),
            make("old", ("legacy",), calls=calls),
            make("paid", ("premium",), scopes=("paid",), calls=calls),
        )
        orch.registry.disable("old")
        return orch

    def test_issues_listed(self):
        calls: list[str] = []
        orch = self._orch(calls)
        plan = [
            Step("ok", tool="calc"),
            Step("unknown", tool="nope"),
            Step("pinned", tool="calc", version="9.9.9"),
            Step("denied", tool="fs.write"),
            Step("off", tool="old"),
            Step("unservable", capability="premium"),
            Step("routable", capability="calc"),
        ]
        issues = orch.check_plan(plan, USER)
        assert [(i.step_id, i.kind) for i in issues] == [
            ("unknown", "unknown_tool"),
            ("pinned", "unknown_tool"),
            ("denied", "denied"),
            ("off", "disabled"),
            ("unservable", "no_route"),
        ]
        assert issues[1].target == "calc@9.9.9"
        assert issues[4].target == "capability:premium"
        assert "paid" in issues[4].message
        assert orch.policy.audit_log == []
        assert calls == []

    def test_clean_plan(self):
        orch = self._orch([])
        plan = Plan([Step("a", tool="calc"), Step("b", capability="calc")])
        assert orch.check_plan(plan, USER) == []

    def test_check_plan_validates_principal(self):
        with pytest.raises(ValidationError):
            self._orch([]).check_plan([Step("a", tool="calc")], None)  # type: ignore[arg-type]

    async def test_enforced_preflight(self):
        calls: list[str] = []
        orch = self._orch(calls)
        plan = [Step("a", tool="calc"), Step("w", tool="fs.write")]
        with pytest.raises(PreflightError) as info:
            await orch.run(plan, USER, preflight=True)
        assert [(i.step_id, i.kind) for i in info.value.issues] == [("w", "denied")]
        assert calls == []
        assert orch.policy.audit_log == []

    async def test_enforced_preflight_clean_plan_runs(self):
        orch = self._orch([])
        report = await orch.run([Step("a", tool="calc")], USER, preflight=True)
        assert report.ok and report.issues == ()

    async def test_default_run_reports_issues(self):
        orch = self._orch([])
        report = await orch.run([Step("a", tool="calc"), Step("w", tool="fs.write")], USER)
        assert report.succeeded == ["a"] and report.failed == ["w"]
        assert [(i.step_id, i.kind) for i in report.issues] == [("w", "denied")]

    async def test_retries_ignored_for_denied_step(self):
        orch = self._orch([])
        plan = [Step("w", tool="fs.write", retries=3, retry_backoff=30.0)]
        report = await asyncio.wait_for(orch.run(plan, USER), 2)
        assert report["w"].attempts == 1
        assert not [e for e in report.events if e.kind == "retrying"]
        assert report.duration < 1
        assert len(orch.policy.audit_log) == 1

    async def test_allowed_step_keeps_retries(self):
        calls: list[str] = []
        orch = make_orch(make("bad", ("x",), fn=boom, calls=calls))
        report = await orch.run([Step("s", tool="bad", retries=2)], USER)
        assert report["s"].attempts == 3 and len(calls) == 3


# ---------------------------------------------------------------------------
# 1.6 fan-out
# ---------------------------------------------------------------------------


class TestFanOut:
    async def test_majority_across_three_tools(self):
        orch = make_orch(
            make("a", priority=3, value="A"),
            make("b", priority=2, value="A"),
            make("c", priority=1, value="B"),
        )
        report = await orch.run(
            [Step("s", capability="search")], USER, fanout={"s": FanOut(3, "majority")}
        )
        assert report.ok and report.outputs["s"] == "A"
        resolution = report.resolutions["s"]
        assert isinstance(resolution, Resolution)
        assert set(resolution.winners) == {"a", "b"} and resolution.dissenters == ("c",)
        result = report["s"].result
        assert result.tool == "fanout:search" and result.version == ""
        assert result.metadata["resolution"] is resolution
        assert set(report.served_by["s"]) == {"a", "b"}
        resolved = [e for e in report.events if e.kind == "resolved"]
        assert len(resolved) == 1 and resolved[0].detail["strategy"] == "majority"
        assert set(report.chains["s"]) == {"a", "b", "c"}
        assert len(report.audit) == 3

    async def test_merge_strategy(self):
        orch = make_orch(
            make("p1", ("profile",), value={"name": "Ada"}),
            make("p2", ("profile",), value={"email": "ada@example.org"}),
        )
        report = await orch.run(
            [Step("s", capability="profile")], USER, fanout={"s": FanOut(2, "merge")}
        )
        assert report.outputs["s"] == {"name": "Ada", "email": "ada@example.org"}

    async def test_partial_failures_ignored(self):
        orch = make_orch(
            make("a", priority=3, value="A"),
            make("b", priority=2, fn=boom),
            make("c", priority=1, value="A"),
        )
        report = await orch.run([Step("s", capability="search")], USER, fanout={"s": 3})
        assert report.outputs["s"] == "A"
        assert set(report.resolutions["s"].winners) == {"a", "c"}
        assert orch.router.stats("b").failures == 1
        assert orch.router.stats("a").successes == 1
        outcomes = {a.tool: a.outcome for a in report.attempts["s"]}
        assert outcomes == {"a": "ok", "b": "error", "c": "ok"}

    async def test_member_tool_timeout_counts_as_failure(self):
        orch = make_orch(
            make("sloth", priority=3, fn=slow),
            make("a", priority=2, value="A"),
            make("b", priority=1, value="A"),
            tool_timeout=0.02,
        )
        report = await orch.run([Step("s", capability="search")], USER, fanout={"s": 3})
        assert report.ok and report.outputs["s"] == "A"
        assert set(report.resolutions["s"].winners) == {"a", "b"}
        outcomes = {a.tool: a.outcome for a in report.attempts["s"]}
        assert outcomes == {"sloth": "timeout", "a": "ok", "b": "ok"}
        assert orch.router.stats("sloth").failures == 1

    async def test_runs_in_parallel(self):
        gate = asyncio.Event()
        arrived: list[str] = []

        def waiter(name: str):
            async def fn(**kwargs: Any) -> str:
                arrived.append(name)
                if len(arrived) == 2:
                    gate.set()
                await asyncio.wait_for(gate.wait(), 1)
                return "same"

            return fn

        orch = make_orch(make("a", fn=waiter("a")), make("b", fn=waiter("b")))
        report = await orch.run([Step("s", capability="search")], USER, fanout={"s": 2})
        assert report.ok and report.outputs["s"] == "same"

    async def test_only_top_n_invoked(self):
        calls: list[str] = []
        orch = make_orch(
            make("a", priority=3, calls=calls, value=1),
            make("b", priority=2, calls=calls, value=1),
            make("c", priority=1, calls=calls, value=1),
        )
        await orch.run([Step("s", capability="search")], USER, fanout={"s": 2})
        assert sorted(calls) == ["a", "b"]

    async def test_unpermitted_and_invalid_candidates_skipped(self):
        calls: list[str] = []
        orch = make_orch(
            make("paid", priority=9, scopes=("paid",), calls=calls, value="P"),
            make("strict", priority=8, schema={"q": int}, calls=calls, value="S"),
            make("a", priority=3, calls=calls, value="A"),
            make("b", priority=2, calls=calls, value="A"),
        )
        report = await orch.run(
            [Step("s", capability="search", args={"q": "x"})], USER, fanout={"s": 2}
        )
        assert sorted(calls) == ["a", "b"]
        assert report.attempts["s"][0].outcome == "invalid_input"

    async def test_priority_metadata_drives_priority_strategy(self):
        orch = make_orch(make("hi", priority=7, value="H"), make("lo", priority=1, value="L"))
        report = await orch.run(
            [Step("s", capability="search")], USER, fanout={"s": FanOut(2, "priority")}
        )
        assert report.outputs["s"] == "H"

    async def test_options_passed_to_strategy(self):
        orch = make_orch(make("a", value=1), make("b", value=3))
        report = await orch.run(
            [Step("s", capability="search")],
            USER,
            fanout={"s": FanOut(2, "numeric", {"agg": "max"})},
        )
        assert report.outputs["s"] == 3

    async def test_fanout_output_referenced(self):
        async def echo(x: Any) -> Any:
            return x

        orch = make_orch(make("a", value="A"), make("b", value="A"), make("echo", ("e",), fn=echo))
        plan = [Step("s", capability="search"), Step("t", tool="echo", args={"x": Ref("s")})]
        report = await orch.run(plan, USER, fanout={"s": 2})
        assert report.outputs["t"] == "A"


class TestFanOutFailures:
    async def test_unreconcilable_results(self):
        orch = make_orch(make("a", value="A"), make("b", value="B"))
        report = await orch.run(
            [Step("s", capability="search")],
            USER,
            fanout={"s": FanOut(2, "majority", {"quorum": 1.0})},
        )
        assert report.failed == ["s"]
        assert isinstance(report["s"].exception, ConflictError)
        assert "s" not in report.resolutions and "s" not in report.served_by

    async def test_all_tools_fail(self):
        orch = make_orch(make("a", fn=boom), make("b", fn=boom))
        report = await orch.run([Step("s", capability="search")], USER, fanout={"s": 2})
        assert isinstance(report["s"].exception, ConflictError)

    async def test_no_route(self):
        orch = make_orch(make("paid", scopes=("paid",)))
        report = await orch.run([Step("s", capability="search")], USER, fanout={"s": 2})
        assert isinstance(report["s"].exception, NoRouteError)

    async def test_fanout_on_named_tool_step(self):
        calls: list[str] = []
        orch = make_orch(make("calc", ("calc",), calls=calls))
        with pytest.raises(ValidationError):
            await orch.run([Step("s", tool="calc")], USER, fanout={"s": 2})
        assert calls == [] and orch.policy.audit_log == []

    @pytest.mark.parametrize("fanout", [{"missing": 2}, {"s": 0}, {"s": True}, {"s": "3"}, ["s"]])
    async def test_invalid_fanout_settings(self, fanout):
        calls: list[str] = []
        orch = make_orch(make("a", calls=calls))
        with pytest.raises(ValidationError):
            await orch.run([Step("s", capability="search")], USER, fanout=fanout)
        assert calls == []


# ---------------------------------------------------------------------------
# 1.7 resource claims, run-scoped locks, contention warnings
# ---------------------------------------------------------------------------


def tracking_tool(name: str, state: dict[str, int], **kwargs: Any) -> ToolSpec:
    async def fn(**_: Any) -> str:
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        for _ in range(3):
            await asyncio.sleep(0)
        state["active"] -= 1
        return name

    return make(name, ("report",), fn=fn, **kwargs)


class TestResourceClaims:
    async def test_inherited_write_claim_serializes(self):
        state = {"active": 0, "max": 0}
        orch = make_orch(
            tracking_tool("report", state, metadata={"resources": {"reports": "write"}})
        )
        report = await orch.run([Step("a", tool="report"), Step("b", tool="report")], USER)
        assert report.ok
        assert state["max"] == 1
        assert report.contentions == (("reports", "a", "b"),)

    async def test_without_claims_steps_overlap(self):
        state = {"active": 0, "max": 0}
        orch = make_orch(tracking_tool("report", state))
        await orch.run([Step("a", tool="report"), Step("b", tool="report")], USER)
        assert state["max"] == 2

    async def test_step_claim_wins(self):
        state = {"active": 0, "max": 0}
        orch = make_orch(
            tracking_tool("report", state, metadata={"resources": {"reports": "write"}})
        )
        plan = [
            Step("a", tool="report", resources={"reports": "read"}),
            Step("b", tool="report", resources={"reports": "read"}),
        ]
        report = await orch.run(plan, USER)
        assert report.ok and report.contentions == ()
        assert state["max"] == 2

    @pytest.mark.parametrize("declared", [["reports"], {"reports": "exclusive"}, {"": "write"}])
    async def test_malformed_declaration_rejected(self, declared):
        calls: list[str] = []
        orch = make_orch(make("report", ("report",), metadata={"resources": declared}, calls=calls))
        with pytest.raises(ValidationError):
            await orch.run([Step("a", tool="report")], USER)
        assert calls == []

    async def test_capability_steps_keep_own_claims(self):
        orch = make_orch(make("report", ("report",), metadata={"resources": {"reports": "write"}}))
        report = await orch.run(
            [Step("a", capability="report"), Step("b", capability="report")], USER
        )
        assert report.ok and report.contentions == ()

    async def test_independent_writers_warned(self):
        orch = make_orch(make("calc", ("calc",)))
        plan = [
            Step("a", tool="calc", resources={"db": "write"}),
            Step("b", capability="calc", resources={"db": "write"}),
        ]
        report = await orch.run(plan, USER)
        assert report.contentions == (("db", "a", "b"),)
        assert len(report.warnings) == 1 and "'db'" in report.warnings[0]

    async def test_dependent_writers_not_warned(self):
        orch = make_orch(make("calc", ("calc",)))
        plan = [
            Step("a", tool="calc", resources={"db": "write"}),
            Step("m", tool="calc"),
            Step("b", tool="calc", resources={"db": "write"}, depends_on=("a",)),
            Step("c", tool="calc", resources={"db": "write"}, depends_on=("m",)),
            Step("d", tool="calc", resources={"db": "read"}, depends_on=("c",)),
        ]
        report = await orch.run(plan, USER)
        # a-b ordered directly, c-d ordered directly; a-c, a-d, b-c, b-d independent
        assert set(report.contentions) == {
            ("db", "a", "c"),
            ("db", "a", "d"),
            ("db", "b", "c"),
            ("db", "b", "d"),
        }

    async def test_transitive_dependency_not_warned(self):
        orch = make_orch(make("calc", ("calc",)))
        plan = [
            Step("a", tool="calc", resources={"db": "write"}),
            Step("m", tool="calc", depends_on=("a",)),
            Step("b", tool="calc", resources={"db": "write"}, depends_on=("m",)),
        ]
        assert (await orch.run(plan, USER)).contentions == ()

    async def test_readers_not_warned(self):
        orch = make_orch(make("calc", ("calc",)))
        plan = [
            Step("a", tool="calc", resources={"db": "read"}),
            Step("b", tool="calc", resources={"db": "read"}),
        ]
        assert (await orch.run(plan, USER)).contentions == ()

    async def test_concurrent_runs_with_same_step_ids(self):
        holders: list[dict[str, str]] = []
        orch: Orchestrator

        async def write(**_: Any) -> str:
            holders.append(orch.lock_manager.holders("db"))
            await asyncio.sleep(0)
            return "done"

        orch = make_orch(make("writer", ("write",), fn=write))
        plan = [Step("write", tool="writer", resources={"db": "write"})]
        r1, r2 = await asyncio.gather(orch.run(plan, USER), orch.run(plan, USER))
        assert r1.ok and r2.ok
        owners = {owner for h in holders for owner in h}
        assert owners == {f"{r1.run_id}/write", f"{r2.run_id}/write"}
        assert orch.lock_manager.holders("db") == {}


# ---------------------------------------------------------------------------
# 1.8 dynamic registry reflection
# ---------------------------------------------------------------------------


class TestDynamicRegistry:
    async def test_newly_registered_tool_routed(self):
        orch = make_orch(make("basic", priority=1))
        r1 = await orch.run([Step("s", capability="search")], USER)
        assert r1.served_by["s"] == ("basic",)
        orch.register(make("better", priority=9))
        r2 = await orch.run([Step("s", capability="search")], USER)
        assert r2.served_by["s"] == ("better",)

    async def test_disabled_tool_bypassed(self):
        orch = make_orch(make("best", priority=9), make("next", priority=1))
        r1 = await orch.run([Step("s", capability="search")], USER)
        assert r1.served_by["s"] == ("best",)
        orch.registry.disable("best")
        r2 = await orch.run([Step("s", capability="search")], USER)
        assert r2.served_by["s"] == ("next",)
        orch.registry.enable("best")
        r3 = await orch.run([Step("s", capability="search")], USER)
        assert r3.served_by["s"] == ("best",)

    async def test_health_forgotten_on_unregister(self):
        orch = make_orch(make("bad", fn=boom))
        await orch.run([Step("s", capability="search")], USER)
        assert orch.router.stats("bad").failures == 1
        orch.registry.unregister("bad")
        orch.register(make("bad", fn=boom))
        assert orch.router.stats("bad").calls == 0

    async def test_close_stops_reset_and_is_idempotent(self):
        orch = make_orch(make("bad", fn=boom))
        await orch.run([Step("s", capability="search")], USER)
        orch.close()
        orch.close()
        orch.registry.unregister("bad")
        assert orch.router.stats("bad").failures == 1

    def test_preflight_reflects_registry(self):
        orch = make_orch()
        plan = [Step("s", capability="search")]
        assert [i.kind for i in orch.check_plan(plan, USER)] == ["no_route"]
        orch.register(make("alpha"))
        assert orch.check_plan(plan, USER) == []
        orch.registry.disable("alpha")
        assert [i.kind for i in orch.check_plan(plan, USER)] == ["no_route"]


# ---------------------------------------------------------------------------
# 1.9 one-shot calls
# ---------------------------------------------------------------------------


async def add(a: int, b: int) -> int:
    return a + b


class TestOneShot:
    async def test_call_capability(self):
        orch = make_orch(make("adder", ("calc",), fn=add, schema={"a": int, "b": int}))
        result = await orch.call("capability:calc", {"a": 2, "b": 3}, principal=USER)
        assert isinstance(result, ToolResult)
        assert result.ok and result.value == 5 and result.tool == "adder"

    async def test_call_tool_with_version(self):
        orch = make_orch(
            make("calc", ("calc",), value="v1", version="1.0.0"),
            make("calc", ("calc",), value="v2", version="2.0.0"),
        )
        assert (await orch.call("calc", principal=USER)).value == "v2"
        assert (await orch.call("calc", principal=USER, version="1.0.0")).value == "v1"

    async def test_denied_call_raises(self):
        calls: list[str] = []
        orch = make_orch(make("fs.write", ("write",), scopes=("fs:write",), calls=calls))
        with pytest.raises(AccessDeniedError):
            await orch.call("fs.write", {}, principal=USER)
        assert calls == []

    async def test_call_original_error_reraised(self):
        orch = make_orch(make("t1", priority=2, fn=boom), max_fallbacks=0)
        with pytest.raises(FallbackExhaustedError):
            await orch.call("capability:search", principal=USER)
        with pytest.raises(RuntimeError, match="boom"):
            await orch.call("t1", principal=USER)
        with pytest.raises(NoRouteError):
            await orch.call("capability:missing", principal=USER)

    async def test_call_unsuccessful_result_raises_execution_error(self):
        async def not_ok(**kwargs: Any) -> ToolResult:
            return ToolResult("x", "1.0.0", ok=False, error="nope")

        orch = make_orch(make("t", fn=not_ok))
        with pytest.raises(ExecutionError, match="nope"):
            await orch.call("t", principal=USER)

    async def test_call_timeout(self):
        orch = make_orch(make("sloth", fn=slow))
        with pytest.raises(ToolTimeoutError):
            await orch.call("sloth", principal=USER, timeout=0.02)

    async def test_call_validation(self):
        orch = make_orch(make("calc", ("calc",)))
        with pytest.raises(ValidationError):
            await orch.call("capability:calc", principal=USER, version="1.0.0")
        with pytest.raises(ValidationError):
            await orch.call("", principal=USER)

    async def test_ask_with_fanout(self):
        orch = make_orch(
            make("a", priority=3, value="A"),
            make("b", priority=2, value="A"),
            make("c", priority=1, value="B"),
        )
        resolution = await orch.ask("search", {"q": "x"}, principal=USER, n=3, strategy="majority")
        assert isinstance(resolution, Resolution)
        assert resolution.value == "A"
        assert set(resolution.winners) == {"a", "b"} and resolution.dissenters == ("c",)

    async def test_ask_failure_raises(self):
        orch = make_orch(make("a", value="A"), make("b", value="B"))
        with pytest.raises(ConflictError):
            await orch.ask(
                "search", principal=USER, n=2, strategy="majority", options={"quorum": 1.0}
            )
        with pytest.raises(ValidationError):
            await orch.ask("search", principal=USER, n=0)


# ---------------------------------------------------------------------------
# 2.1 package interface
# ---------------------------------------------------------------------------


class TestPackageInterface:
    @pytest.mark.parametrize(
        "module", [router_mod, permissions_mod, executor_mod, conflicts_mod, agent_mod]
    )
    def test_module_names_exported(self, module):
        for name in module.__all__:
            assert getattr(orchestrator, name) is getattr(module, name)
            assert name in orchestrator.__all__

    def test_core_and_registry_classes_exported(self):
        for name in (
            "Capability",
            "ToolSpec",
            "ToolResult",
            "OrchestratorError",
            "ValidationError",
            "ToolNotFoundError",
            "DuplicateToolError",
            "PermissionDeniedError",
            "NoRouteError",
            "ExecutionError",
            "ToolTimeoutError",
            "ConflictError",
            "CycleError",
            "ToolRegistry",
            "RegistryEvent",
            "parse_version",
        ):
            assert hasattr(orchestrator, name) and name in orchestrator.__all__

    def test_all_names_resolve(self):
        for name in orchestrator.__all__:
            assert hasattr(orchestrator, name), name
        assert len(set(orchestrator.__all__)) == len(orchestrator.__all__)

    def test_root_imports(self):
        from orchestrator import (  # noqa: F401
            CapabilityRouter,
            ConflictResolver,
            Orchestrator,
            PermissionPolicy,
            Plan,
            Principal,
            Ref,
            Step,
        )

        assert orchestrator.__version__ == "0.1.0"
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
        assert pyproject["project"]["version"] == orchestrator.__version__


# ---------------------------------------------------------------------------
# adversarial review: regressions for bugs found in agent.py
# ---------------------------------------------------------------------------


async def spontaneous_cancel(**kwargs: Any) -> Any:
    """A tool that raises CancelledError although nobody cancelled the caller."""
    future = asyncio.get_running_loop().create_future()
    future.cancel()
    await future


class TestAdversarial:
    async def test_named_tool_raising_cancelled_error_fails_only_its_step(self):
        orch = make_orch(make("t", ("t",), fn=spontaneous_cancel), make("ok", ("ok",)))
        report = await asyncio.wait_for(
            orch.run([Step("s", tool="t"), Step("x", tool="ok")], USER), 2
        )
        assert report.failed == ["s"] and report.succeeded == ["x"]
        assert isinstance(report["s"].exception, ExecutionError)
        assert [a.outcome for a in report.attempts["s"]] == ["error"]
        assert orch.router.stats("t").failures == 1

    async def test_capability_tool_raising_cancelled_error_falls_back(self):
        orch = make_orch(make("a", priority=2, fn=spontaneous_cancel), make("b", priority=1))
        report = await asyncio.wait_for(orch.run([Step("s", capability="search")], USER), 2)
        assert report.ok and report.outputs["s"] == "b"
        assert report.chains["s"] == ("a", "b")
        assert report.attempts["s"][0].outcome == "error"
        assert orch.router.stats("a").failures == 1

    async def test_fanout_member_raising_cancelled_error_is_a_failed_member(self):
        finished: list[str] = []

        async def late(**kwargs: Any) -> str:
            await asyncio.sleep(0.05)
            finished.append("c")
            return "C"

        orch = make_orch(
            make("a", priority=3, fn=spontaneous_cancel),
            make("b", priority=2, value="C"),
            make("c", priority=1, fn=late),
        )
        report = await asyncio.wait_for(
            orch.run([Step("s", capability="search")], USER, fanout={"s": 3}), 2
        )
        assert finished == ["c"]  # no member is left running after the run returns
        assert report.ok and report.outputs["s"] == "C"
        assert set(report.resolutions["s"].winners) == {"b", "c"}
        assert orch.router.stats("a").failures == 1

    async def test_fanout_member_revoked_mid_fanout_is_not_invoked(self):
        calls: list[str] = []
        orch = make_orch()

        async def revoker(**kwargs: Any) -> str:
            orch.policy.deny(USER.id, "b")  # revoked before 'b' is invoked
            await asyncio.sleep(0)
            return "X"

        orch.register(make("a", priority=2, fn=revoker, calls=calls))
        orch.register(make("b", priority=1, value="X", calls=calls))
        report = await orch.run([Step("s", capability="search")], USER, fanout={"s": 2})
        assert calls == ["a"]
        assert report.ok and report.outputs["s"] == "X"
        assert ("denied", "b") in step_events(report, "s")
        assert any(r.tool == "b" and not r.allowed for r in report.audit)
        assert {a.tool: a.outcome for a in report.attempts["s"]}["b"] == "denied"

    async def test_fanout_all_members_revoked_raises_denial(self):
        calls: list[str] = []
        orch = make_orch(make("a", calls=calls), make("b", calls=calls))
        orch.policy.deny(USER.id, "a")
        real_check = orch.policy.check

        def check(principal: Principal, tool: ToolSpec) -> None:
            orch.policy.deny(USER.id, "b")  # predicate allowed 'b'; the check does not
            real_check(principal, tool)

        orch.policy.check = check  # type: ignore[method-assign]
        report = await orch.run([Step("s", capability="search")], USER, fanout={"s": 2})
        assert calls == []
        assert isinstance(report["s"].exception, AccessDeniedError)

    async def test_fanout_authorization_error_leaves_no_member_running(self):
        finished: list[str] = []

        async def late(**kwargs: Any) -> str:
            await asyncio.sleep(0.05)
            finished.append("a")
            return "A"

        calls: list[str] = []
        orch = make_orch(make("a", priority=2, fn=late), make("b", priority=1, calls=calls))
        real_check = orch.policy.check

        def check(principal: Principal, tool: ToolSpec) -> None:
            if tool.name == "b":
                raise RuntimeError("policy backend down")
            real_check(principal, tool)

        orch.policy.check = check  # type: ignore[method-assign]
        report = await orch.run([Step("s", capability="search")], USER, fanout={"s": 2})
        assert finished == ["a"] and calls == []
        assert isinstance(report["s"].exception, RuntimeError)

    async def test_fallback_event_names_the_tool_actually_routed_next(self):
        orch = make_orch(
            make("flaky", priority=3, fn=boom),
            make("strict", priority=2, schema={"q": int}),
            make("alpha", priority=1),
        )
        report = await orch.run([Step("s", capability="search", args={"q": "x"})], USER)
        assert report.ok and report.served_by["s"] == ("alpha",)
        fallback = [e for e in report.events if e.kind == "fallback"]
        assert len(fallback) == 1 and fallback[0].detail["next"] == "alpha"

    async def test_no_fallback_event_when_remaining_candidates_reject_input(self):
        orch = make_orch(
            make("flaky", priority=3, fn=boom),
            make("strict", priority=2, schema={"q": int}),
        )
        report = await orch.run([Step("s", capability="search", args={"q": "x"})], USER)
        assert isinstance(report["s"].exception, FallbackExhaustedError)
        assert [k for k, _ in step_events(report, "s")] == ["started", "routed", "failed"]

    async def test_routing_strategy_error_fails_step_not_run(self):
        registry = ToolRegistry()
        registry.register(make("calc", ("calc",)))
        registry.register(make("weird"))
        router = CapabilityRouter(registry, strategy=lambda spec, cap, st: float("nan"))
        orch = Orchestrator(registry=registry, router=router)
        plan = [Step("n", tool="calc"), Step("c", capability="search")]
        issues = orch.check_plan(plan, USER)
        assert [(i.step_id, i.kind) for i in issues] == [("c", "no_route")]
        report = await orch.run(plan, USER)
        assert report.succeeded == ["n"] and report.failed == ["c"]
        assert isinstance(report["c"].exception, ValidationError)

    async def test_tool_raising_timeout_error_is_not_reported_as_step_timeout(self):
        async def own_timeout(**kwargs: Any) -> Any:
            raise TimeoutError("upstream API timed out")

        orch = make_orch(make("t", ("t",), fn=own_timeout))
        with pytest.raises(ExecutionError) as info:
            await orch.call("t", principal=USER)
        assert not isinstance(info.value, ToolTimeoutError)
        assert "upstream API timed out" in str(info.value)
        assert isinstance(info.value.__cause__, TimeoutError)
        report = await orch.run([Step("s", tool="t")], USER)
        assert "upstream API timed out" in report["s"].error
        assert [a.outcome for a in report.attempts["s"]] == ["error"]


# ---------------------------------------------------------------------------
# lift-design-limits: availability and resource claims re-checked at invocation
# ---------------------------------------------------------------------------


def disable_when_routed(holder: dict[str, Orchestrator], tool: str):
    """An event listener disabling ``tool`` as soon as it is routed to a step."""

    def listener(event: OrchestratorEvent) -> None:
        if event.kind == "routed" and event.tool == tool:
            holder["orch"].registry.disable(tool)

    return listener


class TestAvailabilityAtInvocation:
    async def test_pinned_version_of_disabled_tool(self):
        calls: list[str] = []
        orch = make_orch(
            make("calc", ("calc",), version="1.0.0", calls=calls),
            make("calc", ("calc",), version="2.0.0", calls=calls),
        )
        orch.registry.disable("calc")
        report = await orch.run([Step("s", tool="calc", version="1.0.0")], USER)
        assert report.failed == ["s"]
        assert isinstance(report["s"].exception, ExecutionError)
        assert "calc@1.0.0 is disabled" in report["s"].error
        assert calls == []
        assert [(a.tool, a.version, a.outcome) for a in report.attempts["s"]] == [
            ("calc", "1.0.0", "unavailable")
        ]
        assert report.audit == ()
        assert [i.kind for i in report.issues] == ["disabled"]

    async def test_disabled_tool_records_unavailable_attempt(self):
        orch = make_orch(make("calc", ("calc",)))
        orch.registry.disable("calc")
        report = await orch.run([Step("s", tool="calc")], USER)
        assert [a.outcome for a in report.attempts["s"]] == ["unavailable"]
        assert "routed" not in [e.kind for e in report.events]

    async def test_named_tool_disabled_once_routed(self):
        calls: list[str] = []
        holder: dict[str, Orchestrator] = {}
        orch = make_orch(
            make("calc", ("calc",), calls=calls), on_event=disable_when_routed(holder, "calc")
        )
        holder["orch"] = orch
        report = await orch.run([Step("s", tool="calc")], USER)
        assert report.failed == ["s"]
        assert isinstance(report["s"].exception, ExecutionError)
        assert "is disabled" in report["s"].error
        assert calls == []
        assert [(a.tool, a.outcome) for a in report.attempts["s"]] == [("calc", "unavailable")]
        assert report.audit == ()  # never authorized
        assert orch.router.stats("calc").calls == 0

    async def test_named_tool_replaced_once_routed(self):
        calls: list[str] = []
        orch: Orchestrator

        def listener(event: OrchestratorEvent) -> None:
            if event.kind == "routed":
                orch.register(make("calc", ("calc",), value="new", calls=calls), replace=True)

        orch = make_orch(make("calc", ("calc",), calls=calls), on_event=listener)
        report = await orch.run([Step("s", tool="calc")], USER)
        assert report.failed == ["s"] and "was replaced" in report["s"].error
        assert calls == []
        assert [a.outcome for a in report.attempts["s"]] == ["unavailable"]

    async def test_unavailable_candidate_skipped_without_budget(self):
        calls: list[str] = []
        holder: dict[str, Orchestrator] = {}
        orch = make_orch(
            make("alpha", priority=9, calls=calls),
            make("beta", priority=1, calls=calls),
            max_fallbacks=0,
            on_event=disable_when_routed(holder, "alpha"),
        )
        holder["orch"] = orch
        report = await orch.run([Step("s", capability="search")], USER)
        assert report.ok and report.served_by["s"] == ("beta",)
        assert calls == ["beta"]
        assert [(a.tool, a.outcome) for a in report.attempts["s"]] == [
            ("alpha", "unavailable"),
            ("beta", "ok"),
        ]
        assert orch.router.stats("alpha").calls == 0
        assert "fallback" not in [e.kind for e in report.events]
        assert [r.tool for r in report.audit] == ["beta"]

    async def test_only_candidate_unavailable_is_no_route(self):
        holder: dict[str, Orchestrator] = {}
        orch = make_orch(make("alpha"), on_event=disable_when_routed(holder, "alpha"))
        holder["orch"] = orch
        report = await orch.run([Step("s", capability="search")], USER)
        assert report.failed == ["s"]
        assert isinstance(report["s"].exception, NoRouteError)
        assert [a.outcome for a in report.attempts["s"]] == ["unavailable"]

    async def test_unavailable_after_invoked_candidate_exhausts(self):
        holder: dict[str, Orchestrator] = {}
        orch = make_orch(
            make("first", priority=9, fn=boom),
            make("second", priority=1),
            on_event=disable_when_routed(holder, "second"),
        )
        holder["orch"] = orch
        report = await orch.run([Step("s", capability="search")], USER)
        exc = report["s"].exception
        assert isinstance(exc, FallbackExhaustedError)
        assert [(a.tool, a.outcome) for a in exc.attempts] == [
            ("first", "error"),
            ("second", "unavailable"),
        ]

    async def test_fallback_candidate_disabled_mid_step(self):
        calls: list[str] = []
        orch: Orchestrator

        async def disable_then_fail(**_: Any) -> Any:
            orch.registry.disable("second")
            raise RuntimeError("first failed")

        orch = make_orch(
            make("first", priority=9, fn=disable_then_fail, calls=calls),
            make("second", priority=5, calls=calls),
            make("third", priority=1, calls=calls),
        )
        report = await orch.run([Step("s", capability="search")], USER)
        assert report.ok and report.served_by["s"] == ("third",)
        assert calls == ["first", "third"]
        assert report.chains["s"] == ("first", "third")

    async def test_fanout_member_disabled_mid_run(self):
        calls: list[str] = []
        orch: Orchestrator

        async def disable_c(**_: Any) -> str:
            orch.registry.disable("c")
            await asyncio.sleep(0)
            return "A"

        orch = make_orch(
            make("a", priority=3, fn=disable_c, calls=calls),
            make("b", priority=2, value="A", calls=calls),
            make("c", priority=1, value="B", calls=calls),
        )
        report = await orch.run(
            [Step("s", capability="search")], USER, fanout={"s": FanOut(3, "majority")}
        )
        assert report.ok and report.outputs["s"] == "A"
        assert "c" not in calls
        resolution = report.resolutions["s"]
        assert set(resolution.winners) == {"a", "b"} and "c" not in resolution.winners
        outcomes = {a.tool: a.outcome for a in report.attempts["s"]}
        assert outcomes == {"a": "ok", "b": "ok", "c": "unavailable"}
        assert orch.router.stats("c").calls == 0
        assert {r.tool for r in report.audit} == {"a", "b"}

    async def test_fanout_member_replaced_mid_run(self):
        calls: list[str] = []
        orch: Orchestrator

        async def replace_b(**_: Any) -> str:
            orch.register(make("b", priority=2, value="X", calls=calls), replace=True)
            return "A"

        orch = make_orch(
            make("a", priority=3, fn=replace_b, calls=calls),
            make("b", priority=2, value="A", calls=calls),
            make("c", priority=1, value="A", calls=calls),
        )
        report = await orch.run(
            [Step("s", capability="search")], USER, fanout={"s": FanOut(3, "majority")}
        )
        assert report.ok and report.outputs["s"] == "A"
        assert calls == ["a", "c"]
        assert {a.tool: a.outcome for a in report.attempts["s"]}["b"] == "unavailable"

    async def test_every_fanout_member_unavailable_fails(self):
        orch: Orchestrator

        def listener(event: OrchestratorEvent) -> None:
            if event.kind == "routed" and event.tool is not None:
                orch.registry.disable(event.tool)

        orch = make_orch(make("a", priority=2), make("b", priority=1), on_event=listener)
        report = await orch.run(
            [Step("s", capability="search")], USER, fanout={"s": FanOut(2, "majority")}
        )
        assert report.failed == ["s"]
        assert isinstance(report["s"].exception, ConflictError)
        assert [a.outcome for a in report.attempts["s"]] == ["unavailable", "unavailable"]


class TestClaimsAtInvocation:
    def _replacing_plan(self, orch_ref: dict[str, Orchestrator], replacement: ToolSpec):
        async def replace(**_: Any) -> str:
            orch_ref["orch"].register(replacement, replace=True)
            return "replaced"

        return make("setup", ("setup",), fn=replace), [
            Step("a", tool="setup"),
            Step("b", tool="report", depends_on=("a",)),
        ]

    async def test_tool_replaced_with_new_claim(self):
        calls: list[str] = []
        holder: dict[str, Orchestrator] = {}
        replacement = make(
            "report", ("report",), value="new", calls=calls,
            metadata={"resources": {"db": "write"}},
        )
        setup, plan = self._replacing_plan(holder, replacement)
        orch = make_orch(setup, make("report", ("report",), calls=calls))
        holder["orch"] = orch
        report = await orch.run(plan, USER)
        assert report.succeeded == ["a"] and report.failed == ["b"]
        assert isinstance(report["b"].exception, ExecutionError)
        assert "claims changed" in report["b"].error and "'db'" in report["b"].error
        assert calls == []
        assert [(a.tool, a.outcome) for a in report.attempts["b"]] == [
            ("report", "claims_changed")
        ]
        assert "routed" not in [e.kind for e in report.events if e.step_id == "b"]

    async def test_covered_claims_still_run(self):
        calls: list[str] = []
        holder: dict[str, Orchestrator] = {}
        replacement = make(
            "report", ("report",), value="new", calls=calls,
            metadata={"resources": {"db": "read"}},
        )
        setup, plan = self._replacing_plan(holder, replacement)
        orch = make_orch(
            setup,
            make("report", ("report",), value="old", metadata={"resources": {"db": "write"}}),
        )
        holder["orch"] = orch
        report = await orch.run(plan, USER)
        assert report.ok and report.outputs["b"] == "new"
        assert calls == ["report"]

    async def test_step_own_claim_covers_new_tool_claim(self):
        holder: dict[str, Orchestrator] = {}
        replacement = make(
            "report", ("report",), value="new", metadata={"resources": {"db": "write"}}
        )
        setup, plan = self._replacing_plan(holder, replacement)
        plan[1] = dataclasses.replace(plan[1], resources={"db": "read"})  # step claim wins
        orch = make_orch(setup, make("report", ("report",)))
        holder["orch"] = orch
        report = await orch.run(plan, USER)
        assert report.ok and report.outputs["b"] == "new"

    async def test_newer_version_with_new_claim(self):
        holder: dict[str, Orchestrator] = {}
        newer = make(
            "report", ("report",), version="2.0.0", metadata={"resources": {"db": "write"}}
        )
        setup, plan = self._replacing_plan(holder, newer)
        orch = make_orch(setup, make("report", ("report",)))
        holder["orch"] = orch
        report = await orch.run(plan, USER)
        assert report.failed == ["b"]
        assert [(a.version, a.outcome) for a in report.attempts["b"]] == [
            ("2.0.0", "claims_changed")
        ]

    async def test_malformed_replacement_claims_fail_attempt(self):
        holder: dict[str, Orchestrator] = {}
        replacement = make("report", ("report",), metadata={"resources": ["db"]})
        setup, plan = self._replacing_plan(holder, replacement)
        orch = make_orch(setup, make("report", ("report",)))
        holder["orch"] = orch
        report = await orch.run(plan, USER)
        assert report.failed == ["b"]
        assert isinstance(report["b"].exception, ExecutionError)
        assert [a.outcome for a in report.attempts["b"]] == ["claims_changed"]

    def test_new_outcomes_listed(self):
        assert {"unavailable", "claims_changed"} <= agent_mod.ATTEMPT_OUTCOMES
