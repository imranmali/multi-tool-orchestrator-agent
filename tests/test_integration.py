"""End-to-end tests of the composed system, using only the package root's public API."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator import (
    OPEN,
    AccessDeniedError,
    Capability,
    CapabilityRouter,
    FanOut,
    Orchestrator,
    PermissionPolicy,
    PlanIssue,
    PreflightError,
    Principal,
    Ref,
    ResourceLockManager,
    Step,
    ToolRegistry,
    ToolSpec,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Overlap:
    """Counts, per resource, how many invocations of a claiming tool run at once."""

    def __init__(self) -> None:
        self.active: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.calls: list[str] = []

    @property
    def max_active(self) -> int:
        return max(self.peak.values(), default=0)

    async def hold(self, resource: str, label: str) -> None:
        self.active[resource] = self.active.get(resource, 0) + 1
        self.peak[resource] = max(self.peak.get(resource, 0), self.active[resource])
        try:
            await asyncio.sleep(0.01)
            self.calls.append(label)
        finally:
            self.active[resource] -= 1


def build(*, failure_threshold: int = 3, clock: FakeClock | None = None):
    """An orchestrator with priority routing and a small, fully deterministic tool set."""
    registry = ToolRegistry()
    router = CapabilityRouter(
        registry,
        strategy="priority",
        failure_threshold=failure_threshold,
        cooldown=60.0,
        clock=clock or FakeClock(),
    )
    policy = PermissionPolicy(
        {
            "analyst": ["search:read", "docs:read"],
            "admin": ["*"],
        }
    )
    events = []
    orch = Orchestrator(registry=registry, router=router, policy=policy, on_event=events.append)
    overlap = Overlap()

    @orch.tool(name="search.flaky", capabilities=Capability("search", priority=10),
               scopes="search:read", input_schema={"query": str})
    async def flaky(query):
        """Always down."""
        await asyncio.sleep(0)
        raise ConnectionError("upstream down")

    @orch.tool(name="search.alpha", capabilities=Capability("search", priority=5),
               scopes="search:read", input_schema={"query": str})
    async def alpha(query):
        await asyncio.sleep(0)
        return f"answer:{query}"

    @orch.tool(name="search.beta", capabilities=Capability("search", priority=1),
               scopes="search:read", input_schema={"query": str})
    async def beta(query):
        await asyncio.sleep(0)
        return f"answer:{query}"

    @orch.tool(name="search.gamma", capabilities=Capability("search", priority=0),
               scopes="search:read", input_schema={"query": str})
    async def gamma(query):
        await asyncio.sleep(0)
        return "something else"

    @orch.tool(name="calc", capabilities=("calc", "math"))
    async def calc(a, b):
        return a + b

    @orch.tool(name="render", capabilities="render", scopes="docs:read")
    async def render(answer, total, consensus):
        return f"{answer} | total={total} | consensus={consensus}"

    @orch.tool(name="fs.write", capabilities="fs.write", scopes="fs:write",
               metadata={"resources": {"store": "write"}})
    async def fs_write(text):
        await overlap.hold("store", f"fs.write:{text}")
        return len(text)

    @orch.tool(name="logger", capabilities="log", metadata={"resources": {"log": "write"}})
    async def logger(line):
        await overlap.hold("log", f"log:{line}")
        return line

    return orch, events, overlap


def per_step(events, step_id):
    return [f"{e.kind}({e.tool})" if e.tool else e.kind for e in events if e.step_id == step_id]


# --------------------------------------------------------------------------- one full plan


async def test_full_plan_report():
    orch, events, overlap = build()
    ana = Principal("ana", roles={"analyst"})
    # an audited decision made before the run must not leak into the run's report
    assert orch.policy.allowed(ana, orch.registry.get("calc"))
    prior_audit = len(orch.policy.audit_log)

    plan = [
        Step("find", capability="search", args={"query": "q"}),
        Step("vote", capability="search", args={"query": "q"}),
        Step("add", tool="calc", args={"a": 2, "b": 3}),
        Step("report", tool="render",
             args={"answer": Ref("find"), "total": Ref("add"), "consensus": Ref("vote")}),
        Step("save", tool="fs.write", args={"text": Ref("report")}),
        Step("publish", tool="logger", args={"line": Ref("save")}),
        Step("log_a", tool="logger", args={"line": "a"}),
        Step("log_b", tool="logger", args={"line": "b"}),
    ]
    report = await orch.run(plan, ana, fanout={"vote": FanOut(n=4, strategy="majority")})

    # outcomes and outputs
    assert report.run_id == "run-1"
    assert not report.ok
    assert report.succeeded == ["find", "vote", "add", "report", "log_a", "log_b"]
    assert report.failed == ["save"]
    assert report.skipped == ["publish"]
    assert report["publish"].error == "skipped: dependency save failed"
    assert isinstance(report["save"].exception, AccessDeniedError)
    assert report.outputs == {
        "find": "answer:q",
        "vote": "answer:q",
        "add": 5,
        "report": "answer:q | total=5 | consensus=answer:q",
        "log_a": "a",
        "log_b": "b",
    }

    # who served what, and in which order tools were tried
    assert report.served_by == {
        "find": ("search.alpha",),
        "vote": ("search.alpha", "search.beta"),
        "add": ("calc",),
        "report": ("render",),
        "log_a": ("logger",),
        "log_b": ("logger",),
    }
    assert report.chains == {
        "find": ("search.flaky", "search.alpha"),
        "vote": ("search.flaky", "search.alpha", "search.beta", "search.gamma"),
        "add": ("calc",),
        "report": ("render",),
        "save": ("fs.write",),
        "log_a": ("logger",),
        "log_b": ("logger",),
    }
    assert [a.outcome for a in report.attempts["find"]] == ["error", "ok"]
    assert report.attempts["find"][0].error == "ConnectionError: upstream down"
    assert report.attempts["save"][0].outcome == "denied"

    # fan-out resolution: flaky's failure ignored, gamma dissents
    resolution = report.resolutions["vote"]
    assert set(report.resolutions) == {"vote"}
    assert resolution.strategy == "majority"
    assert resolution.winners == ("search.alpha", "search.beta")
    assert resolution.dissenters == ("search.gamma",)
    assert resolution.confidence == pytest.approx(2 / 3)
    assert report["vote"].result.tool == "fanout:search"
    assert report["vote"].result.metadata["resolution"] is resolution

    # audit: only this run's checks, one per tool considered
    assert len(orch.policy.audit_log) == prior_audit + len(report.audit)
    assert [(r.tool, r.allowed) for r in report.audit if not r.allowed] == [("fs.write", False)]
    assert {r.principal_id for r in report.audit} == {"ana"}
    assert sorted(r.tool for r in report.audit) == sorted(
        [
            *["search.flaky", "search.alpha"],
            *["search.flaky", "search.alpha", "search.beta", "search.gamma"],
            *["calc", "render", "fs.write", "logger", "logger"],
        ]
    )

    # pre-flight issue and contention warnings
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert (issue.step_id, issue.kind, issue.target) == ("save", "denied", "fs.write")
    assert report.contentions == (
        ("log", "log_a", "log_b"),
        ("log", "log_a", "publish"),
        ("log", "log_b", "publish"),
    )
    assert report.warnings[0] == (
        "steps 'log_a' and 'log_b' have conflicting claims on resource 'log' and will serialize"
    )
    assert overlap.max_active == 1
    assert sorted(overlap.calls) == ["log:a", "log:b"]
    assert orch.lock_manager.holders("log") == {}

    # one ordered event stream, also delivered to the listener
    assert tuple(events) == report.events
    assert per_step(report.events, "find") == [
        "started", "routed(search.flaky)", "fallback(search.flaky)", "routed(search.alpha)",
        "succeeded",
    ]
    assert per_step(report.events, "vote") == [
        "started", "routed(search.flaky)", "routed(search.alpha)", "routed(search.beta)",
        "routed(search.gamma)", "resolved", "succeeded",
    ]
    assert per_step(report.events, "save") == [
        "started", "routed(fs.write)", "denied(fs.write)", "failed",
    ]
    assert per_step(report.events, "publish") == ["skipped"]
    resolved = next(e for e in report.events if e.kind == "resolved")
    assert resolved.detail["winners"] == ("search.alpha", "search.beta")

    # independent first-level steps overlapped: all started before any finished
    first_done = next(i for i, e in enumerate(report.events) if e.kind == "succeeded")
    started_early = {e.step_id for e in report.events[:first_done] if e.kind == "started"}
    assert {"find", "vote", "add"} <= started_early

    # the same plan under enforced pre-flight invokes nothing
    calls_before = len(overlap.calls)
    with pytest.raises(PreflightError) as excinfo:
        await orch.run(plan, ana, fanout={"vote": 4}, preflight=True)
    assert excinfo.value.issues == (issue,)
    assert len(overlap.calls) == calls_before


async def test_admin_run_of_the_same_plan_succeeds():
    orch, _, overlap = build()
    root = Principal("root", roles={"admin"})
    report = await orch.run(
        [
            Step("find", capability="search", args={"query": "q"}),
            Step("save", tool="fs.write", args={"text": Ref("find")}),
            Step("publish", tool="logger", args={"line": Ref("save")}),
        ],
        root,
    )
    assert report.ok and report.issues == ()
    assert report.outputs == {"find": "answer:q", "save": 8, "publish": 8}
    assert overlap.calls == ["fs.write:answer:q", "log:8"]
    # dependency-ordered claims are never reported as contention
    assert report.contentions == ()


# --------------------------------------------------------------------------- delegation


async def test_delegated_principal_narrowed_scopes_change_routing():
    orch, _, _ = build()

    @orch.tool(name="search.premium", capabilities=Capability("search", priority=50),
               scopes=("search:read", "search:premium"))
    async def premium(query):
        return f"premium:{query}"

    lead = Principal("lead", scopes={"search:read", "search:premium", "docs:read"})
    helper = lead.narrow(["search:read"], id="lead/helper")
    step = [Step("s", capability="search", args={"query": "q"})]

    lead_report = await orch.run(step, lead)
    helper_report = await orch.run(step, helper)
    assert lead_report.served_by == {"s": ("search.premium",)}
    assert helper_report.chains == {"s": ("search.flaky", "search.alpha")}
    assert helper_report.served_by == {"s": ("search.alpha",)}
    assert {r.principal_id for r in helper_report.audit} == {"lead/helper"}
    assert "search.premium" not in {r.tool for r in helper_report.audit}

    # role-granted scopes are delegable through the policy, not through narrow()
    ana = Principal("ana", roles={"analyst"})
    with pytest.raises(AccessDeniedError):
        ana.narrow(["search:read"])
    bot = orch.policy.delegate(ana, ["search:read"], id="ana/bot")
    issues = orch.check_plan(
        [Step("s", capability="search", args={"query": "q"}),
         Step("r", tool="render", args={"answer": 1, "total": 2, "consensus": 3})],
        bot,
    )
    assert issues == [
        PlanIssue("r", "denied", "render", issues[0].message),
    ]
    assert "docs:read" in issues[0].message
    with pytest.raises(AccessDeniedError):
        orch.policy.delegate(ana, ["fs:write"])


# --------------------------------------------------------------------------- health across runs


async def test_router_health_accumulates_until_circuit_opens():
    clock = FakeClock()
    orch, _, _ = build(failure_threshold=2, clock=clock)
    ana = Principal("ana", roles={"analyst"})
    step = [Step("s", capability="search", args={"query": "q"})]

    first = await orch.run(step, ana)
    second = await orch.run(step, ana)
    assert first.chains["s"] == second.chains["s"] == ("search.flaky", "search.alpha")
    assert orch.router.circuit_state("search.flaky") == OPEN
    assert orch.router.stats("search.flaky").failures == 2

    # traffic shifts: the open circuit keeps the flaky tool out of the next runs
    third = await orch.run(step, ana)
    assert third.chains["s"] == ("search.alpha",)
    assert not any(e.kind == "fallback" for e in third.events)
    assert orch.check_plan(step, ana) == []

    # after the cooldown the circuit is half-open and the tool is probed again
    clock.now += 61
    fourth = await orch.run(step, ana)
    assert fourth.chains["s"] == ("search.flaky", "search.alpha")
    assert orch.router.circuit_state("search.flaky") == OPEN


# --------------------------------------------------------------------------- registry changes


async def test_registry_changes_between_runs():
    orch, _, _ = build()
    ana = Principal("ana", roles={"analyst"})
    step = [Step("s", capability="search", args={"query": "q"})]

    async def fresh(query):
        return f"fresh:{query}"

    orch.register(ToolSpec("search.fresh", "1.0.0", (Capability("search", priority=99),), fresh,
                           required_scopes=frozenset({"search:read"})))
    report = await orch.run(step, ana)
    assert report.served_by == {"s": ("search.fresh",)}
    assert orch.router.stats("search.fresh").successes == 1

    orch.registry.disable("search.fresh")
    report = await orch.run(step, ana)
    assert report.chains["s"] == ("search.flaky", "search.alpha")
    assert "search.fresh" not in {r.tool for r in report.audit}

    orch.registry.enable("search.fresh")
    report = await orch.run(step, ana)
    assert report.served_by == {"s": ("search.fresh",)}
    assert orch.router.stats("search.fresh").successes == 2

    orch.registry.unregister("search.fresh")
    assert orch.router.stats("search.fresh").calls == 0
    report = await orch.run([*step, Step("n", tool="search.fresh", args={"query": "q"})], ana)
    assert report.served_by["s"] == ("search.alpha",)
    assert [(i.step_id, i.kind) for i in report.issues] == [("n", "unknown_tool")]
    assert report.failed == ["n"]

    # a new version pinned by a named step
    async def fresh2(query):
        return f"v2:{query}"

    orch.register(ToolSpec("search.fresh", "2.0.0", (Capability("search", priority=99),), fresh2,
                           required_scopes=frozenset({"search:read"})))
    report = await orch.run(
        [Step("n", tool="search.fresh", version="2.0.0", args={"query": "q"})], ana
    )
    assert report.outputs == {"n": "v2:q"}
    orch.close()


# --------------------------------------------------------------------------- concurrent runs


async def test_two_concurrent_runs_share_one_orchestrator():
    orch, events, overlap = build()
    root = Principal("root", roles={"admin"})
    ana = Principal("ana", roles={"analyst"})

    def plan(tag):
        return [
            Step("find", capability="search", args={"query": tag}),
            Step("write", tool="fs.write", args={"text": Ref("find")}),
            Step("log", tool="logger", args={"line": tag}),
        ]

    lock_manager = orch.lock_manager
    assert isinstance(lock_manager, ResourceLockManager)
    first, second = await asyncio.gather(orch.run(plan("x"), root), orch.run(plan("y"), ana))

    assert {first.run_id, second.run_id} == {"run-1", "run-2"}
    assert first.ok
    assert first.outputs == {"find": "answer:x", "write": 8, "log": "x"}
    # the analyst's write is denied; its own run is unaffected otherwise
    assert second.failed == ["write"] and second.outputs["log"] == "y"
    assert {r.principal_id for r in first.audit} == {"root"}
    assert {r.principal_id for r in second.audit} == {"ana"}
    # identically named claiming steps in both runs serialize on the shared locks
    assert overlap.peak == {"store": 1, "log": 1}
    assert sorted(overlap.calls) == ["fs.write:answer:x", "log:x", "log:y"]
    assert lock_manager.holders("store") == {} and lock_manager.holders("log") == {}
    # each run's report holds only its own events; the listener saw both
    assert len(events) == len(first.events) + len(second.events)
    assert {id(e) for e in first.events}.isdisjoint(id(e) for e in second.events)
    assert sorted(map(id, events)) == sorted(map(id, first.events + second.events))
