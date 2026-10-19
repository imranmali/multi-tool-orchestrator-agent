"""Deterministic, network-free example tools and the runnable demonstration.

The tools here double as executable documentation: every one is an ``async``
function with fixed behaviour, so tests and the ``python3 -m orchestrator`` demo
behave identically on every run.

* ``search.flaky``, ``search.premium``, ``search.basic`` and ``search.contrarian``
  serve ``search``. The flaky tool is the best-ranked and fails exactly once per
  registration; the premium tool is expensive and needs ``search:premium``; the
  contrarian tool disagrees with the others.
* ``summarize`` serves ``summarize`` (scope ``docs:read``).
* ``calculator`` serves ``math`` and ``calc`` (no scope).
* ``profile.directory`` and ``profile.contacts`` serve ``profile`` with mergeable
  dicts (scope ``profile:read``).
* ``fs.write`` serves ``fs.write``, needs ``fs:write`` and declares a write claim
  on the ``reports`` resource; it writes to an in-memory :class:`MemoryFiles`.

:func:`example_policy` defines the ``analyst`` and ``admin`` roles,
:func:`build_orchestrator` returns an orchestrator populated with all of the
above, and :func:`run_demo` runs the demonstration and returns its summary.
"""

from __future__ import annotations

import asyncio
import numbers
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import (
    AccessDeniedError,
    PermissionPolicy,
    Plan,
    Principal,
    Ref,
    Resolution,
    Step,
    ToolRegistry,
    __version__,
)
from .agent import FanOut, Orchestrator, RunReport
from .core import Capability, ToolSpec, ValidationError

__all__ = [
    "DISSENTING_ANSWER",
    "SEARCH_ANSWER",
    "DemoResult",
    "MemoryFiles",
    "build_orchestrator",
    "collect_demo",
    "example_policy",
    "register_example_tools",
    "run_demo",
]

#: What every agreeing search tool answers for a question about orchestrators.
SEARCH_ANSWER = (
    "An orchestrator routes each request to the best permitted tool, runs independent "
    "steps in parallel and reconciles disagreeing answers."
)
#: What the contrarian search tool answers instead.
DISSENTING_ANSWER = "An orchestrator is a musician who arranges scores for an orchestra."

#: The demo's search question.
DEMO_QUERY = "what is an orchestrator?"


# --------------------------------------------------------------------------- policy


def example_policy(**options: Any) -> PermissionPolicy:
    """A policy with the demo roles.

    * ``analyst``: ``search:*`` (so also ``search:premium``), ``docs:read``,
      ``profile:read`` -- read-only work, no ``fs:write``;
    * ``admin``: ``*``.
    """
    return PermissionPolicy(
        {
            "analyst": ("search:*", "docs:read", "profile:read"),
            "admin": ("*",),
        },
        **options,
    )


# --------------------------------------------------------------------------- tools


class MemoryFiles:
    """In-memory file store used by ``fs.write``; tracks concurrent writers."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.writes: list[str] = []
        self.active = 0
        self.max_active = 0

    async def write(self, path: str, content: str, *, hold: float = 0.01) -> dict[str, Any]:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(hold)  # long enough that unserialized writers would overlap
            self.files[path] = content
            self.writes.append(path)
        finally:
            self.active -= 1
        return {"path": path, "bytes": len(content.encode())}


def _answer(query: str) -> str:
    if "orchestrat" in query.lower():
        return SEARCH_ANSWER
    return f"no results for {query!r}"


def _example_specs(files: MemoryFiles) -> list[ToolSpec]:
    calls = {"flaky": 0}

    async def flaky_search(query: str) -> str:
        """Cheapest, highest-priority search; fails on its first call only."""
        calls["flaky"] += 1
        await asyncio.sleep(0)
        if calls["flaky"] == 1:
            raise ConnectionError("upstream connection reset (simulated)")
        return _answer(query)

    async def premium_search(query: str) -> str:
        """Expensive search that needs the search:premium scope."""
        await asyncio.sleep(0)
        return _answer(query)

    async def basic_search(query: str) -> str:
        """Cheap, low-priority search."""
        await asyncio.sleep(0)
        return _answer(query)

    async def contrarian_search(query: str) -> str:
        """A search tool that disagrees with the others."""
        await asyncio.sleep(0)
        return DISSENTING_ANSWER if "orchestrat" in query.lower() else _answer(query)

    async def summarize(text: str, max_words: int = 12) -> str:
        """Keep the first ``max_words`` words of ``text``."""
        await asyncio.sleep(0)
        words = str(text).split()
        if len(words) <= max_words:
            return " ".join(words)
        return " ".join(words[:max_words]) + " ..."

    async def calculator(a: float, b: float, op: str = "add") -> float:
        """Basic arithmetic: add, sub, mul or div."""
        await asyncio.sleep(0)
        ops = {"add": a + b, "sub": a - b, "mul": a * b}
        if op == "div":
            return a / b
        if op not in ops:
            raise ValidationError(f"calculator: unknown op {op!r}")
        return ops[op]

    async def profile_directory(user: str) -> dict[str, str]:
        """Name and team from the staff directory."""
        await asyncio.sleep(0)
        return {"name": "Ada Lovelace", "team": "analytics"}

    async def profile_contacts(user: str) -> dict[str, str]:
        """Contact details; agrees with the directory on the team."""
        await asyncio.sleep(0)
        return {"email": "ada@example.org", "team": "analytics"}

    async def fs_write(path: str, content: str) -> dict[str, Any]:
        """Write a report to the in-memory store."""
        return await files.write(path, content)

    def spec(name: str, handler: Any, caps: Sequence[Capability], scopes: Iterable[str] = (),
             schema: Mapping[str, type] | None = None, **metadata: Any) -> ToolSpec:
        return ToolSpec(
            name=name,
            version="1.0.0",
            capabilities=tuple(caps),
            handler=handler,
            description=(handler.__doc__ or "").strip().splitlines()[0],
            required_scopes=frozenset(scopes),
            input_schema=dict(schema or {}),
            metadata=metadata,
        )

    query = {"query": str}
    return [
        spec("search.flaky", flaky_search,
             [Capability("search", cost=0.1, latency=0.1, priority=10)], ["search:read"], query),
        spec("search.premium", premium_search,
             [Capability("search", cost=5.0, latency=0.5, priority=5)],
             ["search:read", "search:premium"], query),
        spec("search.basic", basic_search,
             [Capability("search", cost=0.5, latency=0.5, priority=1)], ["search:read"], query),
        spec("search.contrarian", contrarian_search,
             [Capability("search", cost=1.0, latency=1.0, priority=0)], ["search:read"], query),
        spec("summarize", summarize, [Capability("summarize")], ["docs:read"], {"text": str}),
        spec("calculator", calculator, [Capability("math"), Capability("calc")], (),
             {"a": numbers.Real, "b": numbers.Real}),
        spec("profile.directory", profile_directory, [Capability("profile", priority=2)],
             ["profile:read"], {"user": str}),
        spec("profile.contacts", profile_contacts, [Capability("profile", priority=1)],
             ["profile:read"], {"user": str}),
        spec("fs.write", fs_write, [Capability("fs.write")], ["fs:write"],
             {"path": str, "content": str}, resources={"reports": "write"}),
    ]


def register_example_tools(
    target: Orchestrator | ToolRegistry, *, files: MemoryFiles | None = None
) -> list[ToolSpec]:
    """Register fresh instances of every example tool; returns their specs.

    Each call creates new handlers (the flaky tool gets its own call counter), so
    two registrations behave identically. ``fs.write`` writes to ``files``
    (a new :class:`MemoryFiles` when omitted).
    """
    if not isinstance(target, (Orchestrator, ToolRegistry)):
        raise ValidationError(
            f"expected an Orchestrator or a ToolRegistry, got {type(target).__name__}"
        )
    specs = _example_specs(files if files is not None else MemoryFiles())
    for spec in specs:
        target.register(spec)
    return specs


def build_orchestrator(
    *, files: MemoryFiles | None = None, policy: PermissionPolicy | None = None, **options: Any
) -> Orchestrator:
    """An :class:`Orchestrator` with :func:`example_policy` and every example tool."""
    orch = Orchestrator(policy=policy if policy is not None else example_policy(), **options)
    register_example_tools(orch, files=files)
    return orch


# --------------------------------------------------------------------------- demo


@dataclass
class DemoResult:
    """Everything the demo produced: the printed summary plus the raw reports."""

    text: str
    runs: dict[str, RunReport] = field(default_factory=dict)
    resolution: Resolution | None = None
    files: MemoryFiles = field(default_factory=MemoryFiles)
    checks: dict[str, bool] = field(default_factory=dict)


def _short(value: Any, width: int = 58) -> str:
    if isinstance(value, str):
        text = f'"{value}"'
        return text if len(text) <= width else text[: width - 5] + ' ..."'
    text = repr(value)
    return text if len(text) <= width else text[: width - 4] + " ..."


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]], indent: str = "  ") -> list[str]:
    cells = [[str(cell) for cell in row] for row in rows]
    widths = [max([len(h), *(len(r[i]) for r in cells)]) for i, h in enumerate(headers)]
    fmt = indent + "  ".join(f"{{:<{w}}}" for w in widths)
    out = [fmt.format(*headers).rstrip(), indent + "  ".join("-" * w for w in widths)]
    out.extend(fmt.format(*row).rstrip() for row in cells)
    return out


def _section(lines: list[str], title: str) -> None:
    if lines:
        lines.append("")
    lines.append(title)
    lines.append("-" * len(title))


def _step_rows(report: RunReport) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for sid, outcome in report.results.items():
        if outcome.ok:
            served = ", ".join(report.served_by.get(sid, ())) or "-"
            shown = _short(outcome.value)
        else:
            served = "-"
            shown = (outcome.error or "").split(" (")[0]
            shown = shown if len(shown) <= 58 else shown[:55] + "..."
        rows.append((sid, outcome.status, served, shown))
    return rows


def _trace(report: RunReport, step_id: str) -> str:
    parts = []
    for event in report.events:
        if event.step_id != step_id:
            continue
        parts.append(f"{event.kind}({event.tool})" if event.tool else event.kind)
    return " -> ".join(parts)


def _audit_line(report: RunReport) -> str:
    allowed = sum(1 for r in report.audit if r.allowed)
    denied = len(report.audit) - allowed
    return (
        f"audit: {len(report.audit)} checks in {report.run_id} "
        f"({allowed} allowed, {denied} denied)"
    )


async def collect_demo() -> DemoResult:
    """Run every demo phase on a fresh orchestrator and collect the results."""
    files = MemoryFiles()
    orch = build_orchestrator(files=files)
    policy = orch.policy
    result = DemoResult(text="", files=files)
    lines: list[str] = []
    title = f"Multi-tool orchestrator demo (orchestrator {__version__})"
    lines += [title, "=" * len(title)]

    # -- tools -----------------------------------------------------------------
    _section(lines, "Tools")
    lines += _table(
        ("tool", "capabilities", "scopes", "claims"),
        (
            (
                spec.name,
                ", ".join(c.name for c in spec.capabilities),
                ", ".join(sorted(spec.required_scopes)) or "-",
                ", ".join(f"{r}:{m}" for r, m in (spec.metadata.get("resources") or {}).items())
                or "-",
            )
            for spec in orch.registry.list()
        ),
    )
    lines.append("  roles: " + "; ".join(
        f"{name} = {', '.join(sorted(scopes))}" for name, scopes in policy.roles.items()
    ))

    try:
        # -- phase 1: one plan for the analyst ----------------------------------------
        ana = Principal("ana", roles=frozenset({"analyst"}))
        plan = Plan([
            Step("search", capability="search", args={"query": DEMO_QUERY}),
            Step("profile", capability="profile", args={"user": "ada"}),
            Step("stats", tool="calculator", args={"a": 40, "b": 2}),
            Step("summary", capability="summarize", args={"text": Ref("search"), "max_words": 8}),
            Step("save", tool="fs.write",
                 args={"path": "reports/brief.txt", "content": Ref("summary")}),
            Step("announce", tool="summarize", args={"text": Ref("save", "path")}),
        ])
        run1 = await orch.run(plan, ana, fanout={"profile": FanOut(n=2, strategy="merge")})
        result.runs["plan"] = run1
        _section(lines, f"1. Plan for analyst 'ana' ({run1.run_id})")
        lines.append("  levels: " + " | ".join(", ".join(level) for level in plan.levels()))
        for issue in run1.issues:
            lines.append(f"  pre-flight: {issue.step_id} ({issue.kind}): {issue.message}")
        lines += _table(("step", "status", "served by", "output / error"), _step_rows(run1))
        started_together = []
        for event in run1.events:
            if event.kind == "succeeded":
                break
            if event.kind == "started":
                started_together.append(event.step_id)
        lines.append(f"  parallel: {', '.join(started_together)} started before any step finished")
        lines.append(
            "  refs:     summary <- Ref('search'), save <- Ref('summary'), "
            "announce <- Ref('save', 'path')"
        )
        for event in run1.events:
            if event.kind == "fallback":
                lines.append(
                    f"  fallback: {event.step_id}: {event.tool} failed ({event.error}) "
                    f"-> {event.detail['next']}"
                )
        lines.append(f"  chain:    search = {' -> '.join(run1.chains['search'])}")
        lines.append(f"  trace:    search = {_trace(run1, 'search')}")
        for event in run1.events:
            if event.kind == "denied":
                lines.append(f"  denied:   {event.step_id}: {event.error}")
        for sid in run1.skipped:
            reason = (run1[sid].error or "").removeprefix("skipped: ")
            lines.append(f"  skipped:  {sid} ({reason})")
        merged = run1.resolutions["profile"]
        lines.append(f"  merge:    profile = {merged.value!r}")
        lines.append("  " + _audit_line(run1))

        # -- phase 2: fan-out majority --------------------------------------------------
        resolution = await orch.ask("search", {"query": DEMO_QUERY}, principal=ana, n=4,
                                    strategy="majority")
        result.resolution = resolution
        _section(lines, "2. Fan-out: ask 4 search tools, reconcile by majority (run-2)")
        lines.append(f"  answer:     {_short(resolution.value, 100)}")
        lines.append(f"  winners:    {', '.join(resolution.winners)}")
        lines.append(f"  dissenters: {', '.join(resolution.dissenters)}")
        lines.append(f"  confidence: {resolution.confidence:.2f}")

        # -- phase 3: delegation narrows what routing may pick -----------------------------
        orch.registry.disable("search.flaky")
        lead = Principal("lead", scopes=frozenset({"search:*", "docs:read", "profile:read"}))
        helper = lead.narrow(["search:read", "docs:read"], id="lead/helper")
        scout = helper.narrow(["search:read"], id="lead/helper/scout")
        _section(lines, "3. Delegation: Principal.narrow() changes what routing may pick")
        rows = []
        for who in (lead, helper, scout):
            eligible = policy.filter_tools(who, orch.registry.list(include_disabled=False))
            rows.append((
                who.id,
                ", ".join(sorted(policy.effective_scopes(who))),
                orch.router.route("search", eligible=policy.predicate(who)).name,
                len(eligible),
            ))
        lines.append(
            "  registry: search.flaky disabled for maintenance; routing reflects it at once"
        )
        lines += _table(("principal", "effective scopes", "search routes to", "usable tools"), rows)
        run3 = await orch.run(
            [Step("lookup", capability="search", args={"query": DEMO_QUERY}),
             Step("digest", capability="summarize", args={"text": Ref("lookup"), "max_words": 6})],
            helper,
        )
        result.runs["delegated"] = run3
        lines.append(
            f"  {run3.run_id} as lead/helper: lookup served by "
            f"{', '.join(run3.served_by['lookup'])}"
            f", digest = {_short(run3.outputs['digest'])}"
        )
        scout_issues = orch.check_plan(
            [Step("digest", capability="summarize", args={"text": "x"})], scout
        )
        for issue in scout_issues:
            lines.append(f"  lead/helper/scout pre-flight: {issue.step_id} ({issue.kind})")
        try:
            scout.narrow(["fs:write"])
        except AccessDeniedError as exc:
            lines.append(f"  refused:  {exc}")

        # -- phase 4: policy.delegate() hands role-granted scopes to a bot; its two writers
        # -- contend for the same resource lock --------------------------------------------
        admin = Principal("root", roles=frozenset({"admin"}))
        bot = policy.delegate(admin, ["fs:write", "docs:read"], id="root/report-bot")
        run4 = await orch.run(
            [Step("draft", capability="summarize", args={"text": SEARCH_ANSWER, "max_words": 10}),
             Step("save_txt", tool="fs.write",
                  args={"path": "reports/brief.txt", "content": Ref("draft")}),
             Step("save_md", tool="fs.write",
                  args={"path": "reports/brief.md", "content": Ref("draft")})],
            bot,
        )
        result.runs["contention"] = run4
        _section(
            lines,
            "4. Contention: policy.delegate(root) -> root/report-bot saves two reports "
            f"({run4.run_id})",
        )
        for warning in run4.warnings:
            lines.append(f"  warning: {warning}")
        lines += _table(("step", "status", "served by", "output / error"), _step_rows(run4))
        lines.append(f"  max concurrent writers on 'reports': {files.max_active}")
        lines.append(f"  files: {', '.join(sorted(files.files))}")
        lines.append("  " + _audit_line(run4))

        # -- summary ----------------------------------------------------------------------
        checks = {
            "parallel steps": len(started_together) >= 3,
            "output references": run1["summary"].ok and "save" in run1.results,
            "routing with fallback": run1.chains["search"][0] == "search.flaky"
            and run1["search"].ok,
            "permission denial": run1["save"].status == "failed",
            "skip propagation": run1.skipped == ["announce"],
            "fan-out merge": merged.value.get("email") == "ada@example.org",
            "fan-out majority": resolution.value == SEARCH_ANSWER
            and resolution.dissenters == ("search.contrarian",),
            "delegation": run3.ok and "search.premium" not in run3.served_by["lookup"],
            "contention warning": bool(run4.warnings) and files.max_active == 1,
        }
        result.checks = checks
        _section(lines, "Summary")
        lines += _table(("feature", "result"),
                        ((name, "ok" if passed else "FAILED") for name, passed in checks.items()))
        lines.append(
            f"  {len(orch.policy.audit_log)} audited decisions across 4 runs; "
            f"{sum(checks.values())}/{len(checks)} features demonstrated"
        )
    finally:
        orch.close()
    result.text = "\n".join(lines)
    return result


async def run_demo() -> str:
    """Run the demonstration and return its readable summary."""
    return (await collect_demo()).text
