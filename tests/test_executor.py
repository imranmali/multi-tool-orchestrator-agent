"""Tests for the parallel-execution capability (src/orchestrator/executor.py)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import pytest

from orchestrator.core import (
    CycleError,
    ExecutionError,
    ToolResult,
    ToolTimeoutError,
    ValidationError,
)
from orchestrator.executor import (
    ExecutionReport,
    ParallelExecutor,
    Plan,
    Ref,
    Step,
    StepEvent,
    StepOutcome,
    default_invoker,
)
from orchestrator.registry import ToolRegistry

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FnInvoker:
    """Invoker dispatching ``step.tool`` to an async function; records calls."""

    def __init__(self, **fns: Any) -> None:
        self.fns = fns
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, step: Step, args: dict[str, Any]) -> ToolResult:
        self.calls.append((step.id, args))
        value = await self.fns[step.tool](**args)
        if isinstance(value, ToolResult):
            return value
        return ToolResult(tool=step.tool, version="1.0.0", ok=True, value=value)

    def called(self, step_id: str) -> bool:
        return any(sid == step_id for sid, _ in self.calls)


async def ok(**kw: Any) -> Any:
    return kw.get("x", "ok")


async def boom(**kw: Any) -> Any:
    raise RuntimeError("boom")


def run_plan(invoker, steps, **kw) -> ExecutionReport:
    return asyncio.run(ParallelExecutor(invoker, **kw).run(Plan(steps)))


# ---------------------------------------------------------------------------
# Requirement: Step definition
# ---------------------------------------------------------------------------


class TestStepDefinition:
    def test_tool_step(self):
        step = Step("a", tool="calc", args={"x": 1})
        assert (step.id, step.tool, step.args, step.capability) == ("a", "calc", {"x": 1}, None)

    def test_neither_tool_nor_capability(self):
        with pytest.raises(ValidationError):
            Step("a")

    def test_both_tool_and_capability(self):
        with pytest.raises(ValidationError):
            Step("a", tool="calc", capability="math")

    def test_invalid_resource_mode(self):
        with pytest.raises(ValidationError):
            Step("a", tool="calc", resources={"db": "append"})

    @pytest.mark.parametrize(
        "kw", [{"retries": -1}, {"retry_backoff": -0.1}, {"timeout": 0}, {"id": ""}]
    )
    def test_invalid_numbers(self, kw):
        params = {"id": "a", "tool": "calc", **kw}
        with pytest.raises(ValidationError):
            Step(**params)

    def test_version_requires_tool(self):
        with pytest.raises(ValidationError):
            Step("a", capability="math", version="1.0.0")


# ---------------------------------------------------------------------------
# Requirement: Plan validation / ordering
# ---------------------------------------------------------------------------


class TestPlan:
    def test_duplicate_step_id(self):
        with pytest.raises(ValidationError, match="duplicate"):
            Plan([Step("a", tool="t"), Step("a", tool="t")])

    def test_unknown_dependency(self):
        with pytest.raises(ValidationError, match="zzz"):
            Plan([Step("b", tool="t", depends_on=("zzz",))])

    def test_cycle_detected_and_named(self):
        steps = [
            Step("a", tool="t", depends_on=("c",)),
            Step("b", tool="t", depends_on=("a",)),
            Step("c", tool="t", depends_on=("b",)),
        ]
        with pytest.raises(CycleError) as info:
            Plan(steps)
        msg = str(info.value)
        assert all(name in msg for name in ("a", "b", "c"))
        assert "a -> c -> b -> a" in msg

    def test_cycle_is_validation_error_and_self_loop(self):
        with pytest.raises(ValidationError, match="a -> a"):
            Plan([Step("a", tool="t", depends_on=("a",))])

    def test_diamond_layers_and_order(self):
        plan = Plan(
            [
                Step("d", tool="t", depends_on=("b", "c")),
                Step("a", tool="t"),
                Step("c", tool="t", depends_on=("a",)),
                Step("b", tool="t", depends_on=("a",)),
            ]
        )
        assert plan.levels() == [["a"], ["c", "b"], ["d"]]
        assert plan.order() == ["a", "c", "b", "d"]

    def test_diamond_layers_declaration_order(self):
        plan = Plan(
            [
                Step("a", tool="t"),
                Step("b", tool="t", depends_on=("a",)),
                Step("c", tool="t", depends_on=("a",)),
                Step("d", tool="t", depends_on=("b", "c")),
            ]
        )
        assert plan.levels() == [["a"], ["b", "c"], ["d"]]
        assert plan.order() == ["a", "b", "c", "d"]
        assert plan.dependents("a") == ["b", "c"]
        assert len(plan) == 4 and "d" in plan and plan["d"].depends_on == ("b", "c")

    def test_non_step_rejected(self):
        with pytest.raises(ValidationError):
            Plan([{"id": "a"}])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Requirement: Output references
# ---------------------------------------------------------------------------


class TestRefs:
    def test_whole_output_reference(self):
        async def five(**_):
            return 5

        inv = FnInvoker(five=five, ok=ok)
        report = run_plan(inv, [Step("a", tool="five"), Step("b", tool="ok", args={"x": Ref("a")})])
        assert report.ok
        assert dict(inv.calls)["b"] == {"x": 5}
        assert report.outputs == {"a": 5, "b": 5}

    def test_nested_dotted_reference(self):
        async def items(**_):
            return {"items": [{"title": "T"}, {"title": "U"}]}

        async def echo(**kw):
            return kw

        inv = FnInvoker(items=items, echo=echo)
        report = run_plan(
            inv,
            [
                Step("a", tool="items"),
                Step(
                    "b",
                    tool="echo",
                    args={
                        "x": {"titles": [Ref("a", "items.0.title")]},
                        "pair": (Ref("a", "items.1.title"), 3),
                    },
                ),
            ],
        )
        assert report.ok, report.results
        assert report.outputs["b"] == {"x": {"titles": ["T"]}, "pair": ("U", 3)}

    def test_implicit_dependency(self):
        step = Step("b", tool="ok", args={"x": [{"y": Ref("a")}]}, depends_on=("c",))
        assert step.depends_on == ("c", "a")
        order: list[str] = []

        async def rec(**kw):
            await asyncio.sleep(0)
            return kw

        inv = FnInvoker(rec=rec)

        async def recording(step, args):
            order.append(step.id)
            return await inv(step, args)

        plan = Plan([Step("b", tool="rec", args={"x": Ref("a")}), Step("a", tool="rec")])
        assert plan.levels() == [["a"], ["b"]]
        report = asyncio.run(ParallelExecutor(recording).run(plan))
        assert report.ok and order == ["a", "b"]

    def test_unresolvable_path(self):
        async def obj(**_):
            return {"present": 1}

        inv = FnInvoker(obj=obj, ok=ok)
        report = run_plan(
            inv, [Step("a", tool="obj"), Step("b", tool="ok", args={"x": Ref("a", "missing")})]
        )
        out = report["b"]
        assert out.status == "failed" and out.attempts == 0
        assert "missing" in out.error
        assert not inv.called("b")

    def test_bad_index_unresolvable(self):
        async def lst(**_):
            return [1]

        inv = FnInvoker(lst=lst, ok=ok)
        report = run_plan(
            inv, [Step("a", tool="lst"), Step("b", tool="ok", args={"x": Ref("a", "5")})]
        )
        assert report["b"].status == "failed"


# ---------------------------------------------------------------------------
# Requirement: Dependency-driven parallel execution / concurrency limit
# ---------------------------------------------------------------------------


class TestParallelism:
    async def test_independent_steps_overlap(self):
        ev_a, ev_b = asyncio.Event(), asyncio.Event()

        async def ping(**_):  # a sets its event and waits for b's
            ev_a.set()
            await ev_b.wait()
            return "a"

        async def pong(**_):
            ev_b.set()
            await ev_a.wait()
            return "b"

        executor = ParallelExecutor(FnInvoker(ping=ping, pong=pong), default_timeout=1.0)
        report = await executor.run(Plan([Step("a", tool="ping"), Step("b", tool="pong")]))
        assert report.ok and report.outputs == {"a": "a", "b": "b"}

    async def test_wall_clock_speedup(self):
        async def nap(**_):
            await asyncio.sleep(0.05)
            return True

        executor = ParallelExecutor(FnInvoker(nap=nap))
        start = time.perf_counter()
        report = await executor.run(Plan([Step(s, tool="nap") for s in "abc"]))
        elapsed = time.perf_counter() - start
        assert report.ok
        assert elapsed < 0.12
        assert report.duration < 0.12

    async def test_dependency_ordering(self):
        log: list[str] = []

        async def first(**_):
            log.append("a:start")
            await asyncio.sleep(0.01)
            log.append("a:end")

        async def second(**_):
            log.append("b:start")

        inv = FnInvoker(first=first, second=second)
        report = await ParallelExecutor(inv).run(
            Plan([Step("b", tool="second", depends_on=("a",)), Step("a", tool="first")])
        )
        assert report.ok
        assert log == ["a:start", "a:end", "b:start"]
        assert report["b"].started_at >= report["a"].finished_at

    async def test_no_layer_barrier(self):
        c_done = asyncio.Event()

        async def slow(**_):  # cannot finish until c has finished
            await c_done.wait()
            return "a"

        async def fast(**_):
            return "b"

        async def after(**_):
            c_done.set()
            return "c"

        inv = FnInvoker(slow=slow, fast=fast, after=after)
        plan = Plan(
            [
                Step("a", tool="slow"),
                Step("b", tool="fast"),
                Step("c", tool="after", depends_on=("b",)),
            ]
        )
        assert plan.levels() == [["a", "b"], ["c"]]
        report = await ParallelExecutor(inv, default_timeout=1.0).run(plan)
        assert report.ok
        assert report["c"].finished_at <= report["a"].finished_at

    async def test_concurrency_limit_respected(self):
        in_flight = 0
        peak = 0

        async def work(**_):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return True

        report = await ParallelExecutor(FnInvoker(work=work), max_concurrency=2).run(
            Plan([Step(f"s{i}", tool="work") for i in range(6)])
        )
        assert report.ok and len(report.succeeded) == 6
        assert peak == 2

    def test_invalid_executor_options(self):
        with pytest.raises(ValidationError):
            ParallelExecutor(FnInvoker(), max_concurrency=0)
        with pytest.raises(ValidationError):
            ParallelExecutor(FnInvoker(), default_timeout=0)

    async def test_empty_plan(self):
        report = await ParallelExecutor(FnInvoker()).run(Plan([]))
        assert report.ok and report.results == {}


# ---------------------------------------------------------------------------
# Requirement: Step timeout / retries
# ---------------------------------------------------------------------------


async def sleepy(**_):
    await asyncio.sleep(1)
    return "late"


class TestTimeoutsAndRetries:
    async def test_step_times_out(self):
        report = await ParallelExecutor(FnInvoker(sleepy=sleepy)).run(
            Plan([Step("a", tool="sleepy", timeout=0.01)])
        )
        out = report["a"]
        assert out.status == "failed"
        assert isinstance(out.exception, ToolTimeoutError)
        assert "timed out" in out.error

    async def test_default_timeout_applies(self):
        start = time.perf_counter()
        report = await ParallelExecutor(FnInvoker(sleepy=sleepy), default_timeout=0.01).run(
            Plan([Step("a", tool="sleepy")])
        )
        assert isinstance(report["a"].exception, ToolTimeoutError)
        assert time.perf_counter() - start < 0.5

    async def test_step_timeout_overrides_default(self):
        async def nap(**_):
            await asyncio.sleep(0.03)
            return "done"

        report = await ParallelExecutor(FnInvoker(nap=nap), default_timeout=0.01).run(
            Plan([Step("a", tool="nap", timeout=1.0)])
        )
        assert report.ok

    async def test_success_after_failure(self):
        calls = 0

        async def flaky(**_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient")
            return "fine"

        report = await ParallelExecutor(FnInvoker(flaky=flaky)).run(
            Plan([Step("a", tool="flaky", retries=2)])
        )
        assert report["a"].status == "succeeded"
        assert report["a"].attempts == 2
        assert report.outputs == {"a": "fine"}

    async def test_retries_exhausted(self):
        report = await ParallelExecutor(FnInvoker(boom=boom)).run(
            Plan([Step("a", tool="boom", retries=1)])
        )
        out = report["a"]
        assert out.status == "failed" and out.attempts == 2
        assert "RuntimeError: boom" in out.error

    async def test_unsuccessful_result_retried(self):
        calls = 0

        async def soft(**_):
            nonlocal calls
            calls += 1
            if calls == 1:
                return ToolResult(tool="soft", version="1.0.0", ok=False, error="rate limited")
            return "yay"

        report = await ParallelExecutor(FnInvoker(soft=soft)).run(
            Plan([Step("a", tool="soft", retries=1)])
        )
        assert report["a"].status == "succeeded" and report["a"].attempts == 2

    async def test_unsuccessful_result_kept_on_failure(self):
        async def soft(**_):
            return ToolResult(tool="soft", version="1.0.0", ok=False, error="nope")

        report = await ParallelExecutor(FnInvoker(soft=soft)).run(Plan([Step("a", tool="soft")]))
        out = report["a"]
        assert out.status == "failed" and out.error == "nope"
        assert out.result is not None and out.result.ok is False

    async def test_timeout_is_retried(self):
        calls = 0

        async def slow_once(**_):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(1)
            return "ok"

        report = await ParallelExecutor(FnInvoker(slow_once=slow_once)).run(
            Plan([Step("a", tool="slow_once", timeout=0.02, retries=1)])
        )
        assert report.ok and report["a"].attempts == 2

    async def test_exponential_backoff(self, monkeypatch):
        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay, *a, **k):
            sleeps.append(delay)
            await real_sleep(0)

        monkeypatch.setattr("orchestrator.executor.asyncio.sleep", fake_sleep)
        report = await ParallelExecutor(FnInvoker(boom=boom)).run(
            Plan([Step("a", tool="boom", retries=3, retry_backoff=0.1)])
        )
        assert report["a"].attempts == 4
        assert sleeps == pytest.approx([0.1, 0.2, 0.4])

    async def test_non_toolresult_from_invoker_fails(self):
        async def bad_invoker(step, args):
            return "not a ToolResult"

        report = await ParallelExecutor(bad_invoker).run(Plan([Step("a", tool="t")]))
        assert report["a"].status == "failed"
        assert isinstance(report["a"].exception, ExecutionError)


# ---------------------------------------------------------------------------
# Requirement: Failure propagation / fail-fast
# ---------------------------------------------------------------------------


class TestFailures:
    async def test_dependents_skipped(self):
        inv = FnInvoker(boom=boom, ok=ok)
        report = await ParallelExecutor(inv).run(
            Plan(
                [
                    Step("a", tool="boom"),
                    Step("b", tool="ok", depends_on=("a",)),
                    Step("c", tool="ok", depends_on=("b",)),
                    Step("d", tool="ok"),
                ]
            )
        )
        assert report.failed == ["a"]
        assert report.skipped == ["b", "c"]
        assert report["d"].status == "succeeded"
        assert not inv.called("b") and not inv.called("c")
        assert report["b"].attempts == 0 and "a failed" in report["b"].error

    async def test_diamond_partial_failure_skips_join(self):
        async def late(**_):
            await asyncio.sleep(0.01)
            return 1

        inv = FnInvoker(boom=boom, late=late, ok=ok)
        report = await ParallelExecutor(inv).run(
            Plan(
                [
                    Step("b", tool="boom"),
                    Step("c", tool="late"),
                    Step("d", tool="ok", depends_on=("b", "c")),
                ]
            )
        )
        assert report["c"].status == "succeeded"
        assert report["d"].status == "skipped"
        assert not inv.called("d")

    async def test_fail_fast_cancels_running_and_pending(self):
        finished = False

        async def slow(**_):
            nonlocal finished
            await asyncio.sleep(1)
            finished = True
            return "slow"

        async def quick_boom(**_):
            await asyncio.sleep(0.01)
            raise RuntimeError("boom")

        inv = FnInvoker(slow=slow, quick_boom=quick_boom, ok=ok)
        start = time.perf_counter()
        report = await ParallelExecutor(inv, fail_fast=True).run(
            Plan(
                [
                    Step("a", tool="quick_boom"),
                    Step("slow", tool="slow"),
                    Step("after", tool="ok", depends_on=("slow",)),
                    Step("dep_a", tool="ok", depends_on=("a",)),
                ]
            )
        )
        assert time.perf_counter() - start < 0.5
        assert report["a"].status == "failed"
        assert report["slow"].status == "cancelled"
        assert report["slow"].started_at is not None and report["slow"].attempts == 1
        assert report["after"].status == "cancelled" and report["after"].started_at is None
        assert report["dep_a"].status == "skipped"
        assert report.cancelled == ["slow", "after"]
        assert finished is False
        assert not inv.called("after")

    async def test_without_fail_fast_independent_branch_completes(self):
        async def slowish(**_):
            await asyncio.sleep(0.02)
            return "s"

        report = await ParallelExecutor(FnInvoker(slowish=slowish, boom=boom)).run(
            Plan([Step("a", tool="boom"), Step("s", tool="slowish")])
        )
        assert report["s"].status == "succeeded" and report["a"].status == "failed"

    async def test_run_cancelled_externally_cancels_children(self):
        cancelled = asyncio.Event()

        async def hang(**_):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(
            ParallelExecutor(FnInvoker(hang=hang)).run(Plan([Step("a", tool="hang")]))
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()


# ---------------------------------------------------------------------------
# Requirement: Resource locking hook
# ---------------------------------------------------------------------------


class FakeLockManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.held: set[str] = set()

    @contextlib.asynccontextmanager
    async def _hold(self, owner, resources):
        self.calls.append((owner, dict(resources)))
        self.held.add(owner)
        try:
            yield
        finally:
            self.held.discard(owner)

    def hold(self, owner, resources):
        return self._hold(owner, resources)


class TestLocking:
    async def test_lock_held_around_invocation(self):
        locks = FakeLockManager()
        seen_held: list[bool] = []

        async def write(**_):
            seen_held.append("w" in locks.held)
            return "written"

        report = await ParallelExecutor(FnInvoker(write=write), lock_manager=locks).run(
            Plan([Step("w", tool="write", resources={"db": "write"})])
        )
        assert report.ok
        assert locks.calls == [("w", {"db": "write"})]
        assert seen_held == [True]
        assert locks.held == set()

    async def test_no_claims_no_lock(self):
        locks = FakeLockManager()
        report = await ParallelExecutor(FnInvoker(ok=ok), lock_manager=locks).run(
            Plan([Step("a", tool="ok")])
        )
        assert report.ok and locks.calls == []

    async def test_lock_reacquired_per_attempt(self):
        locks = FakeLockManager()
        report = await ParallelExecutor(FnInvoker(boom=boom), lock_manager=locks).run(
            Plan([Step("a", tool="boom", retries=1, resources={"f": "read"})])
        )
        assert report["a"].attempts == 2
        assert locks.calls == [("a", {"f": "read"}), ("a", {"f": "read"})]

    async def test_lock_wait_does_not_hold_semaphore_slot(self):
        """A step blocked on a lock must not occupy the only concurrency slot."""
        gate = asyncio.Lock()
        release = asyncio.Event()

        class GateLocks:
            @contextlib.asynccontextmanager
            async def hold(self, owner, resources):
                async with gate:
                    yield

        async def holder(**_):
            await release.wait()
            return "h"

        async def free(**_):
            release.set()
            return "f"

        # "h1" takes the gate and the slot; "h2" waits on the gate (not the slot);
        # "free" (no claims) must still get a slot after h1 ... but h1 needs free
        # to finish, so with max_concurrency=2 this only completes if h2 isn't
        # parked inside the semaphore.
        report = await ParallelExecutor(
            FnInvoker(holder=holder, free=free),
            lock_manager=GateLocks(),
            max_concurrency=2,
            default_timeout=1.0,
        ).run(
            Plan(
                [
                    Step("h1", tool="holder", resources={"r": "write"}),
                    Step("h2", tool="holder", resources={"r": "write"}),
                    Step("free", tool="free"),
                ]
            )
        )
        assert report.ok, report.results


# ---------------------------------------------------------------------------
# Requirement: Pluggable invocation (default invoker)
# ---------------------------------------------------------------------------


class TestDefaultInvoker:
    def _registry(self, *tools):
        reg = ToolRegistry()
        for tool in tools:
            reg.register(tool)
        return reg

    async def test_default_invoker_calls_registry_tool(self, tool_factory):
        async def add(a, b):
            return a + b

        reg = self._registry(
            tool_factory("calc", "1.0.0", caps=("math",), handler=add),
            tool_factory("calc", "2.0.0", caps=("math",), result="v2"),
        )
        executor = ParallelExecutor(default_invoker(reg))
        report = await executor.run(
            Plan(
                [
                    Step("a", tool="calc", version="1.0.0", args={"a": 2, "b": 3}),
                    Step("b", tool="calc"),
                ]
            )
        )
        assert report.ok
        assert report.outputs == {"a": 5, "b": "v2"}
        assert report["a"].result.tool == "calc" and report["a"].result.version == "1.0.0"
        assert report["b"].result.version == "2.0.0"

    async def test_default_invoker_validates_input(self, tool_factory):
        reg = self._registry(tool_factory("calc", input_schema={"a": int}))
        report = await ParallelExecutor(default_invoker(reg)).run(Plan([Step("a", tool="calc")]))
        assert report["a"].status == "failed"
        assert "ValidationError" in report["a"].error and "'a'" in report["a"].error

    async def test_default_invoker_refuses_disabled_tool(self, tool_factory):
        called = False

        async def handler(**_):
            nonlocal called
            called = True

        reg = self._registry(tool_factory("calc", handler=handler))
        reg.disable("calc")
        report = await ParallelExecutor(default_invoker(reg)).run(Plan([Step("a", tool="calc")]))
        assert report["a"].status == "failed" and "disabled" in report["a"].error
        assert called is False

    async def test_capability_step_without_routing(self, tool_factory):
        reg = self._registry(tool_factory("calc", caps=("math",)))
        report = await ParallelExecutor(default_invoker(reg)).run(
            Plan([Step("a", capability="math")])
        )
        out = report["a"]
        assert out.status == "failed"
        assert isinstance(out.exception, ExecutionError)
        assert "routing" in out.error

    async def test_unknown_tool(self):
        report = await ParallelExecutor(default_invoker(ToolRegistry())).run(
            Plan([Step("a", tool="nope")])
        )
        assert "ToolNotFoundError" in report["a"].error

    async def test_custom_invoker_receives_resolved_args(self):
        seen: list[tuple[Step, dict]] = []

        async def invoker(step, args):
            seen.append((step, args))
            return ToolResult(tool="x", version="1.0.0", ok=True, value=step.id)

        await ParallelExecutor(invoker).run(
            Plan([Step("a", capability="search"), Step("b", tool="t", args={"q": Ref("a")})])
        )
        assert seen[1][1] == {"q": "a"} and seen[1][0].id == "b"


# ---------------------------------------------------------------------------
# Requirement: Execution events
# ---------------------------------------------------------------------------


class TestEvents:
    async def test_event_sequence(self):
        events: list[StepEvent] = []
        calls = 0

        async def flaky(**_):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("once")
            return "ok"

        await ParallelExecutor(FnInvoker(flaky=flaky), on_event=events.append).run(
            Plan([Step("a", tool="flaky", retries=1)])
        )
        assert [e.kind for e in events] == ["started", "retrying", "succeeded"]
        assert events[1].error == "RuntimeError: once" and events[2].attempt == 2

    async def test_failed_and_skipped_events(self):
        events: list[StepEvent] = []
        await ParallelExecutor(FnInvoker(boom=boom, ok=ok), on_event=events.append).run(
            Plan([Step("a", tool="boom"), Step("b", tool="ok", depends_on=("a",))])
        )
        assert [(e.kind, e.step_id) for e in events] == [
            ("started", "a"),
            ("failed", "a"),
            ("skipped", "b"),
        ]

    async def test_cancelled_events(self):
        events: list[StepEvent] = []

        async def slow(**_):
            await asyncio.sleep(1)

        await ParallelExecutor(
            FnInvoker(slow=slow, boom=boom), fail_fast=True, on_event=events.append
        ).run(Plan([Step("a", tool="boom"), Step("s", tool="slow")]))
        assert ("cancelled", "s") in [(e.kind, e.step_id) for e in events]

    async def test_callback_errors_ignored(self):
        def explode(event):
            raise RuntimeError("listener broke")

        report = await ParallelExecutor(FnInvoker(ok=ok), on_event=explode).run(
            Plan([Step("a", tool="ok"), Step("b", tool="ok", depends_on=("a",))])
        )
        assert report.ok


# ---------------------------------------------------------------------------
# Requirement: Execution report
# ---------------------------------------------------------------------------


class TestReport:
    async def test_successful_report(self):
        report = await ParallelExecutor(FnInvoker(ok=ok)).run(
            Plan([Step("a", tool="ok", args={"x": 1}), Step("b", tool="ok", args={"x": 2})])
        )
        assert report.ok
        assert report.outputs == {"a": 1, "b": 2}
        assert report.failed == [] and report.skipped == []
        assert report.duration >= 0
        report.raise_for_failure()
        out = report["a"]
        assert isinstance(out, StepOutcome)
        assert out.attempts == 1 and out.error is None and out.duration >= 0

    async def test_failed_report_raises(self):
        report = await ParallelExecutor(FnInvoker(boom=boom, ok=ok)).run(
            Plan([Step("a", tool="boom"), Step("b", tool="ok", depends_on=("a",))])
        )
        assert not report.ok
        assert report.failed == ["a"]
        with pytest.raises(ExecutionError, match="a: RuntimeError: boom") as info:
            report.raise_for_failure()
        assert "skipped: b" in str(info.value)

    async def test_results_in_plan_order(self):
        async def nap(**kw):
            await asyncio.sleep(kw["d"])

        report = await ParallelExecutor(FnInvoker(nap=nap)).run(
            Plan(
                [
                    Step("slow", tool="nap", args={"d": 0.02}),
                    Step("fast", tool="nap", args={"d": 0}),
                ]
            )
        )
        assert list(report.results) == ["slow", "fast"]


# ---------------------------------------------------------------------------
# Regression tests from the spec audit of the archived capability
# ---------------------------------------------------------------------------


class TestAuditRegressions:
    @pytest.mark.parametrize("path", ["items.--1", "items.²", "items.-", "items.1_0"])
    async def test_malformed_index_fails_only_the_referencing_step(self, path):
        async def obj(**_):
            return {"items": [1, 2, 3]}

        inv = FnInvoker(obj=obj, ok=ok)
        report = await ParallelExecutor(inv).run(
            Plan(
                [
                    Step("a", tool="obj"),
                    Step("b", tool="ok", args={"x": Ref("a", path)}),
                    Step("c", tool="ok"),
                ]
            )
        )
        assert report["b"].status == "failed"
        assert isinstance(report["b"].exception, ValidationError)
        assert not inv.called("b")
        assert report["a"].ok and report["c"].ok

    async def test_negative_index_still_resolves(self):
        async def obj(**_):
            return {"items": [1, 2, 3]}

        inv = FnInvoker(obj=obj, ok=ok)
        report = await ParallelExecutor(inv).run(
            Plan([Step("a", tool="obj"), Step("b", tool="ok", args={"x": Ref("a", "items.-1")})])
        )
        assert report["b"].value == 3

    async def test_cancelled_error_raised_by_tool_is_a_step_failure(self):
        async def cancels(**_):
            child = asyncio.ensure_future(asyncio.sleep(10))
            child.cancel()
            await child  # re-raises CancelledError inside the tool

        async def slow(**_):
            await asyncio.sleep(0.02)
            return "done"

        inv = FnInvoker(cancels=cancels, slow=slow, ok=ok)
        report = await ParallelExecutor(inv).run(
            Plan(
                [
                    Step("a", tool="cancels", retries=1),
                    Step("b", tool="slow"),
                    Step("c", tool="ok", depends_on=("a",)),
                ]
            )
        )
        assert report["a"].status == "failed"
        assert report["a"].attempts == 2
        assert "CancelledError" in report["a"].error
        assert report["b"].ok and report["b"].value == "done"
        assert report["c"].status == "skipped"

    async def test_external_cancellation_still_propagates(self):
        async def sleepy(**_):
            await asyncio.sleep(10)

        task = asyncio.create_task(
            ParallelExecutor(FnInvoker(sleepy=sleepy)).run(Plan([Step("a", tool="sleepy")]))
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_tool_raised_timeout_error_is_not_an_executor_timeout(self):
        async def gateway(**_):
            raise TimeoutError("upstream gateway timeout")

        report = await ParallelExecutor(FnInvoker(gateway=gateway)).run(
            Plan([Step("a", tool="gateway")])
        )
        out = report["a"]
        assert out.status == "failed"
        assert not isinstance(out.exception, ToolTimeoutError)
        assert isinstance(out.exception, TimeoutError)
        assert out.error == "TimeoutError: upstream gateway timeout"

    async def test_tool_raised_timeout_error_with_timeout_configured(self):
        async def gateway(**_):
            raise TimeoutError("upstream gateway timeout")

        report = await ParallelExecutor(FnInvoker(gateway=gateway)).run(
            Plan([Step("a", tool="gateway", timeout=5)])
        )
        assert not isinstance(report["a"].exception, ToolTimeoutError)
        assert "upstream gateway timeout" in report["a"].error

    @pytest.mark.parametrize("reverse", [False, True])
    def test_long_chain_does_not_hit_recursion_limit(self, reverse):
        n = 3000
        steps = [Step("s0", tool="t")] + [
            Step(f"s{i}", tool="t", depends_on=(f"s{i - 1}",)) for i in range(1, n)
        ]
        if reverse:
            steps.reverse()
        plan = Plan(steps)
        assert plan.levels() == [[f"s{i}"] for i in range(n)]
        assert plan.order() == [f"s{i}" for i in range(n)]

    def test_long_cycle_detected_iteratively(self):
        n = 3000
        steps = [Step(f"s{i}", tool="t", depends_on=(f"s{(i + 1) % n}",)) for i in range(n)]
        with pytest.raises(CycleError) as info:
            Plan(steps)
        message = str(info.value)
        assert message.startswith("dependency cycle: s0 -> s1 -> ")
        assert message.endswith(" -> s0")
