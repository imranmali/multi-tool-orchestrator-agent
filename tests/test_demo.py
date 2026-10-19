"""Example tools (``orchestrator.examples``) and the runnable demo (``python3 -m orchestrator``)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import orchestrator
from orchestrator import (
    Orchestrator,
    PermissionPolicy,
    Principal,
    Step,
    ToolRegistry,
    ValidationError,
)
from orchestrator.__main__ import main
from orchestrator.examples import (
    DISSENTING_ANSWER,
    SEARCH_ANSWER,
    MemoryFiles,
    build_orchestrator,
    collect_demo,
    example_policy,
    register_example_tools,
    run_demo,
)

ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TOOLS = {
    "calculator",
    "fs.write",
    "profile.contacts",
    "profile.directory",
    "search.basic",
    "search.contrarian",
    "search.flaky",
    "search.premium",
    "summarize",
}


# --------------------------------------------------------------------------- example tools


def test_register_into_registry_and_orchestrator():
    registry = ToolRegistry()
    specs = register_example_tools(registry)
    assert {s.name for s in specs} == EXPECTED_TOOLS
    assert {s.name for s in registry.list()} == EXPECTED_TOOLS

    orch = Orchestrator()
    specs = register_example_tools(orch)
    assert {s.name for s in orch.registry.list()} == EXPECTED_TOOLS
    assert [s.name for s in orch.registry.find_by_capability("search")] == [
        "search.basic",
        "search.contrarian",
        "search.flaky",
        "search.premium",
    ]


def test_register_rejects_other_targets():
    with pytest.raises(ValidationError):
        register_example_tools(object())  # type: ignore[arg-type]


def test_tool_declarations():
    specs = {s.name: s for s in register_example_tools(ToolRegistry())}
    assert specs["fs.write"].required_scopes == {"fs:write"}
    assert specs["fs.write"].metadata["resources"] == {"reports": "write"}
    assert specs["search.premium"].required_scopes == {"search:read", "search:premium"}
    assert specs["calculator"].capability_names == {"math", "calc"}
    assert specs["calculator"].required_scopes == frozenset()
    for name in ("search.flaky", "search.basic", "search.contrarian"):
        assert specs[name].required_scopes == {"search:read"}
    assert all(s.description for s in specs.values())
    flaky = specs["search.flaky"].capability("search")
    premium = specs["search.premium"].capability("search")
    basic = specs["search.basic"].capability("search")
    assert flaky.priority > premium.priority > basic.priority
    assert premium.cost > basic.cost > flaky.cost


async def test_two_fresh_registrations_behave_identically():
    def sequence_of(specs):
        return {s.name: s for s in specs}["search.flaky"].handler

    async def outcomes(handler):
        out = []
        for _ in range(3):
            try:
                out.append(await handler(query="orchestrator"))
            except ConnectionError as exc:
                out.append(f"error: {exc}")
        return out

    first = await outcomes(sequence_of(register_example_tools(ToolRegistry())))
    second = await outcomes(sequence_of(register_example_tools(ToolRegistry())))
    assert first == second
    assert first[0].startswith("error: ")
    assert first[1:] == [SEARCH_ANSWER, SEARCH_ANSWER]

    reports = []
    for _ in range(2):
        orch = build_orchestrator()
        reports.append(
            await orch.run(
                [Step("s", capability="search", args={"query": "orchestrator"})],
                Principal("ana", roles={"analyst"}),
            )
        )
    assert reports[0].chains == reports[1].chains == {"s": ("search.flaky", "search.premium")}
    assert reports[0].outputs == reports[1].outputs == {"s": SEARCH_ANSWER}


async def test_tool_behaviour():
    tools = {s.name: s.handler for s in register_example_tools(ToolRegistry())}
    assert await tools["calculator"](a=40, b=2) == 42
    assert await tools["calculator"](a=6, b=7, op="mul") == 42
    assert await tools["calculator"](a=1, b=4, op="div") == 0.25
    with pytest.raises(ValidationError):
        await tools["calculator"](a=1, b=2, op="pow")
    assert await tools["summarize"](text="one two three", max_words=2) == "one two ..."
    assert await tools["summarize"](text="one two") == "one two"
    assert await tools["search.contrarian"](query="orchestrator") == DISSENTING_ANSWER
    assert await tools["search.basic"](query="weather") == "no results for 'weather'"
    assert (await tools["profile.directory"](user="ada"))["team"] == "analytics"
    assert (await tools["profile.contacts"](user="ada"))["email"] == "ada@example.org"


async def test_fs_write_uses_supplied_store():
    files = MemoryFiles()
    tools = {s.name: s.handler for s in register_example_tools(ToolRegistry(), files=files)}
    assert await tools["fs.write"](path="reports/a.txt", content="héllo") == {
        "path": "reports/a.txt",
        "bytes": 6,
    }
    assert files.files == {"reports/a.txt": "héllo"}
    assert files.writes == ["reports/a.txt"]
    assert files.active == 0 and files.max_active == 1


def test_example_policy_roles():
    policy = example_policy()
    assert isinstance(policy, PermissionPolicy)
    assert policy.roles == {
        "analyst": frozenset({"search:*", "docs:read", "profile:read"}),
        "admin": frozenset({"*"}),
    }
    specs = {s.name: s for s in register_example_tools(ToolRegistry())}
    analyst = Principal("ana", roles={"analyst"})
    admin = Principal("root", roles={"admin"})
    assert not policy.decide(analyst, specs["fs.write"]).allowed
    assert policy.decide(analyst, specs["search.premium"]).allowed
    assert all(policy.decide(admin, spec).allowed for spec in specs.values())
    assert example_policy(audit_limit=5).audit_limit == 5


def test_build_orchestrator_options():
    files = MemoryFiles()
    policy = example_policy()
    orch = build_orchestrator(files=files, policy=policy, max_fallbacks=1)
    try:
        assert orch.policy is policy
        assert orch.max_fallbacks == 1
        assert {s.name for s in orch.registry.list()} == EXPECTED_TOOLS
    finally:
        orch.close()


# --------------------------------------------------------------------------- the demo


async def test_collect_demo_exercises_every_feature():
    result = await collect_demo()
    assert result.checks and all(result.checks.values()), result.checks

    plan = result.runs["plan"]
    assert plan.run_id == "run-1"
    # parallel steps: the three independent steps form the first level
    assert [e.step_id for e in plan.events if e.kind == "started"][:3] == [
        "search",
        "profile",
        "stats",
    ]
    # output reference
    assert plan.outputs["summary"] == "An orchestrator routes each request to the best ..."
    # routing with fallback
    assert plan.chains["search"] == ("search.flaky", "search.premium")
    assert plan.served_by["search"] == ("search.premium",)
    assert [e.detail["next"] for e in plan.events if e.kind == "fallback"] == ["search.premium"]
    # denial with a skipped dependent
    assert [i.kind for i in plan.issues] == ["denied"]
    assert plan["save"].status == "failed"
    assert plan.skipped == ["announce"]
    assert [a.outcome for a in plan.attempts["save"]] == ["denied"]
    # fan-out merge
    assert plan.outputs["profile"] == {
        "name": "Ada Lovelace",
        "team": "analytics",
        "email": "ada@example.org",
    }
    # majority reconciliation
    assert result.resolution is not None
    assert result.resolution.value == SEARCH_ANSWER
    assert result.resolution.dissenters == ("search.contrarian",)
    assert result.resolution.confidence == pytest.approx(0.75)
    # delegated principal: narrowed scopes exclude the premium tool
    delegated = result.runs["delegated"]
    assert delegated.ok
    assert delegated.served_by["lookup"] == ("search.basic",)
    assert {r.principal_id for r in delegated.audit} == {"lead/helper"}
    # contention: two independent writers, serialized
    contention = result.runs["contention"]
    assert contention.contentions == (("reports", "save_md", "save_txt"),)
    assert result.files.max_active == 1
    assert sorted(result.files.files) == ["reports/brief.md", "reports/brief.txt"]


async def test_run_demo_is_deterministic():
    first = await run_demo()
    second = await run_demo()
    assert first == second
    header = f"Multi-tool orchestrator demo (orchestrator {orchestrator.__version__})"
    assert first.startswith(header)


def _assert_key_lines(out: str) -> None:
    assert "1. Plan for analyst 'ana' (run-1)" in out
    assert "parallel: search, profile, stats started before any step finished" in out
    assert (
        "fallback: search: search.flaky failed (ConnectionError: upstream connection reset "
        "(simulated)) -> search.premium"
    ) in out
    assert "chain:    search = search.flaky -> search.premium" in out
    assert (
        "trace:    search = started -> routed(search.flaky) -> fallback(search.flaky) -> "
        "routed(search.premium) -> succeeded"
    ) in out
    assert "denied:   save: AccessDeniedError: principal 'ana' may not use tool 'fs.write'" in out
    assert "skipped:  announce (dependency save failed)" in out
    assert (
        "merge:    profile = {'name': 'Ada Lovelace', 'team': 'analytics', "
        "'email': 'ada@example.org'}"
    ) in out
    assert "winners:    search.flaky, search.basic, search.premium" in out
    assert "dissenters: search.contrarian" in out
    assert "confidence: 0.75" in out
    assert "run-3 as lead/helper: lookup served by search.basic" in out
    assert (
        "warning: steps 'save_md' and 'save_txt' have conflicting claims on resource "
        "'reports' and will serialize"
    ) in out
    assert "max concurrent writers on 'reports': 1" in out
    assert "9/9 features demonstrated" in out
    assert "FAILED" not in out


def test_main_prints_summary(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    _assert_key_lines(out)


def test_python_dash_m_orchestrator():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "orchestrator"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert elapsed < 2.0
    _assert_key_lines(proc.stdout)
