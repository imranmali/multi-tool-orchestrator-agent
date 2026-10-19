# Multi-Tool Orchestrator Agent

[![CI](https://github.com/imranmali/multi-tool-orchestrator-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/imranmali/multi-tool-orchestrator-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

`orchestrator` is a small, dependency-free Python 3.11+ library for running many
async tools behind one agent. You register tools with the capabilities they
offer and the scopes they need. You then describe the work as a plan of steps,
each naming either a tool or a capability, and run it on behalf of a principal.
The orchestrator picks the best tool the principal may use for each step and runs
independent steps in parallel. It falls back to the next tool when one fails,
asks several tools and reconciles their answers when you ask it to, and
serializes steps that write the same resource. It returns one report saying who
served what, which permission decisions were made and what happened, in order.
Everything is deterministic and works without a network, and every Python example
in this README runs as part of the test suite.

**Features**

- **Dynamic tool registry**: register, version, pin, enable, disable and unregister
  tools at runtime, and listen to registry events.
- **Capability-based routing**: rank the tools offering a capability with
  `priority`, `cheapest`, `fastest`, `balanced` or a custom strategy. The router
  tracks the health of each tool, has a per-tool circuit breaker and can explain
  every decision.
- **Permission scoping**: `resource:action` scopes with wildcards, roles,
  delegation that only ever narrows authority, explicit allow and deny rules, and
  a bounded audit log.
- **Parallel execution**: plans form a dependency graph and pass outputs between
  steps with `Ref`s. Runs have a concurrency limit, per-step timeouts, retries
  with exponential backoff, skip propagation, fail-fast, events and resource
  locks.
- **Conflict resolution**: reconcile the answers of several tools with
  `first_success`, `majority` (with a quorum), `priority`, `merge`, `numeric` or
  your own strategy. A FIFO-fair readers-writer lock manager guards shared
  resources.
- **Orchestrator agent**: composes all of the above. It adds permission-aware
  routing with fallback, fan-out, pre-flight checks, tool-declared resource
  claims, contention warnings, a `RunReport`, one ordered event stream and the
  one-shot `call` and `ask` helpers.

## Contents

- [Architecture](#architecture)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [Tool registry](#tool-registry)
- [Capability routing](#capability-routing)
- [Permission scoping](#permission-scoping)
- [Parallel execution](#parallel-execution)
- [Conflict resolution](#conflict-resolution)
- [Orchestrator agent](#orchestrator-agent)
- [Running the demo](#running-the-demo)
- [Error reference](#error-reference)
- [Project layout](#project-layout)
- [Spec-driven development with OpenSpec](#spec-driven-development-with-openspec)
- [Testing](#testing)
  - [Continuous integration](#continuous-integration)
- [Design decisions and limitations](#design-decisions-and-limitations)
- [Contributing](#contributing)
- [License](#license)

## Architecture

Each capability lives in its own module. The modules depend only on `core.py`
(and, for the router and executor, on `registry.py`), never on each other.
`agent.py` is the only module that composes them all.

| Module | Role | Depends on |
| --- | --- | --- |
| `core.py` | `ToolSpec`, `Capability`, `ToolResult`, `parse_version`, the error hierarchy | standard library |
| `registry.py` | `ToolRegistry`, `RegistryEvent` | `core` |
| `router.py` | `CapabilityRouter`, strategies, `ToolStats`, circuit breaker, `RouteExplanation` | `core`, `registry` |
| `permissions.py` | scopes, `Principal`, `PermissionPolicy`, `AuditRecord`, `AccessDeniedError` | `core` |
| `executor.py` | `Step`, `Plan`, `Ref`, `ParallelExecutor`, `ExecutionReport`, events | `core`, `registry` |
| `conflicts.py` | `ConflictResolver`, `Resolution`, `ResourceLockManager`, `detect_write_conflicts` | `core` |
| `agent.py` | `Orchestrator`, `FanOut`, `RunReport`, `OrchestratorEvent`, `PlanIssue`, agent errors | all of the above |
| `examples.py`, `__main__.py` | deterministic example tools and the `python3 -m orchestrator` demo | `agent`, `core`, package root |

The package root (`import orchestrator`) re-exports the public names of every
module, so `from orchestrator import ...` is all you need.

```text
+--------------------------------------------------------------------------------------+
|  Orchestrator  (agent.py)                                                            |
|  register / @tool . check_plan . run . call . ask . RunReport . on_event             |
|                                                                                      |
|  +--------------+    +------------------+    +------------------+    +-------------+ |
|  | ToolRegistry | -> | CapabilityRouter | -> | ParallelExecutor | -> | Conflict-   | |
|  | registry.py  |    | router.py        |    | executor.py      |    | Resolver    | |
|  |              |    |                  |    |                  |    | conflicts.py| |
|  | name@version |    | rank candidates  |    | DAG scheduling   |    |             | |
|  | enable /     |    | health stats     |    | concurrency cap  |    | first_succ. | |
|  | disable      |    | circuit breaker  |    | timeouts         |    | majority    | |
|  | events       |    | explain()        |    | retries, events  |    | priority    | |
|  |              |    |                  |    |                  |    | merge       | |
|  |              |    |   eligible=      |    |   lock_manager=  |    | numeric     | |
|  +--------------+    +-------^----------+    +-------^----------+    +-------------+ |
|                              |                       |                               |
|                     +--------+---------+   +---------+-----------+                   |
|                     | PermissionPolicy |   | ResourceLockManager |                   |
|                     | .predicate(who)  |   | (conflicts.py)      |                   |
|                     | permissions.py   |   | readers / writer    |                   |
|                     +------------------+   +---------------------+                   |
+--------------------------------------------------------------------------------------+
        core.py: ToolSpec . Capability . ToolResult . parse_version . errors
```

The arrows give the order in which a request meets each component. In the code,
the executor drives a run: for every step attempt it calls the invoker that the
orchestrator creates for that run, and the invoker does the routing,
authorization, invocation, fallback and fan-out.

```text
run(plan, principal, fanout=..., preflight=...)
  |
  +- validate fan-out settings ............ ValidationError, nothing invoked
  +- pre-flight: check_plan() ............. issues -> report.issues (PreflightError if preflight=True)
  +- claims: merge tool metadata["resources"] into named-tool steps;
  |          drop retries of statically denied steps; detect contentions
  +- ParallelExecutor(invoker, lock_manager=<run-scoped>, on_event=<run stream>)
  |    for each ready step, per attempt:  lock resources -> concurrency slot -> step timeout
  |      invoker(step, args)
  |        named tool: lookup (version pin) -> available? -> validate input -> claims held?
  |                    -> routed -> available? -> audited policy.check
  |                    -> handler under tool_timeout -> health
  |        capability: candidates(eligible=policy.predicate) -> validate -> routed -> available?
  |                    -> audited check -> invoke -> on failure: fallback(next) ... max_fallbacks
  |        fan-out:    top-n candidates -> routed -> available? -> audited checks
  |                    -> invoke all concurrently -> resolver.resolve(strategy) -> resolved
  +- RunReport(run_id, execution, served_by, attempts, resolutions, audit,
               issues, contentions, events)
```

### The life of one step

Take the step `Step("answer", capability="search", args={"query": "q"})`, run
by principal `ana`:

1. **Pre-flight.** Before anything runs, `check_plan` asks the router whether
   any tool that `ana` is permitted to use offers `search`. It uses
   `policy.predicate`, which writes no audit records. Problems are recorded in
   `report.issues` and do not stop the run unless you pass `preflight=True`.
2. **Scheduling.** When all of the step's dependencies have succeeded, the
   executor starts it. The step first takes its resource locks (if it claims any)
   and then a concurrency slot. Its timeout (`Step.timeout`, or the
   orchestrator's `default_timeout`) starts once the slot is held and covers
   every tool call of the attempt, fallbacks included.
3. **Routing.** The invoker asks the router for the candidates that offer
   `search`. A candidate must be enabled, permitted for `ana` and not have an
   open circuit, and the candidates are ranked by the router's strategy. The
   best candidate whose input schema accepts the args is chosen, and a `routed`
   event is emitted.
4. **Availability and permission check.** The invoker re-checks that the exact
   descriptor it chose is still registered and enabled (the `available?` box
   above). A tool that was disabled, unregistered or replaced in the meantime
   is never invoked: it is recorded with the attempt outcome `unavailable`, and
   the invoker moves on to the next candidate without spending the fallback
   budget. Then `policy.check(ana, tool)` writes an audit record, which is also
   copied into `report.audit`. A denial raises `AccessDeniedError` and emits a
   `denied` event.
5. **Invocation.** The handler is awaited under `tool_timeout`. Success and
   failure are reported to the router (`record_success` with the call's latency,
   or `record_failure`), so the health of each tool builds up across runs.
6. **Fallback.** If the tool raises, times out or returns `ok=False`, the
   invoker emits a `fallback` event naming the next candidate (one whose input
   schema accepts the args) and tries it, up
   to `max_fallbacks` extra tools. When every candidate has failed, or the
   fallback budget is used up, the step fails with `FallbackExhaustedError`.
   (A step that names a tool has no fallback: its tool's failure is the
   attempt's failure.)
7. **Fan-out.** When the run was given `fanout={"answer": FanOut(n=3, ...)}`,
   the top three permitted tools are invoked concurrently instead. The
   `ConflictResolver` reconciles their results, and a `resolved` event is
   emitted.
8. **Report and events.** The step's outcome, the tools that served it, the
   chain of tools it tried, any resolution, the run's audit records and the
   ordered events (`started -> routed -> ... -> succeeded`) all end up in the
   `RunReport`. Each event is also passed to the `on_event` listener, if one
   was given.

## Installation

Requirements: Python 3.11 or newer. There are **no runtime dependencies**; the
library uses only the standard library. The `dev` extra installs the
development tools: `pytest`, `pytest-asyncio` and `ruff`. Building the package
needs `setuptools>=77`, the first release that understands the PEP 639 license
expression (`license = "MIT"`) used in `pyproject.toml`; pip fetches it
automatically.

```bash
git clone https://github.com/imranmali/multi-tool-orchestrator-agent.git
cd multi-tool-orchestrator-agent
pip install -e '.[dev]'       # the library plus pytest, pytest-asyncio and ruff
python3 -m pytest -q          # run the test suite
python3 -m orchestrator       # run the demo
```

Without installing, put `src` on the path: `PYTHONPATH=src python3 -m orchestrator`.
The test suite needs no install either, because `pyproject.toml` sets
`pythonpath = ["src"]` and `asyncio_mode = "auto"` for pytest.

## Quickstart

Create an orchestrator with a permission policy, register two tools, and run a
two-step plan in which the second step consumes the first step's output.

```python
import asyncio

from orchestrator import Orchestrator, PermissionPolicy, Plan, Principal, Ref, Step

# A policy with one role; the orchestrator creates its own registry, router,
# conflict resolver and lock manager.
orch = Orchestrator(policy=PermissionPolicy({"reader": ["web:read"]}))


@orch.tool(capabilities="search", scopes="web:read", input_schema={"query": str})
async def web_search(query: str) -> list[str]:
    """Search the web (simulated)."""
    return [f"{query} result one", f"{query} result two"]


@orch.tool  # bare form: name and capability "word_count", no scopes, version 1.0.0
async def word_count(items: list) -> int:
    """Count the words in a list of strings."""
    return sum(len(item.split()) for item in items)


async def main() -> None:
    alice = Principal("alice", roles={"reader"})
    plan = Plan([
        Step("find", capability="search", args={"query": "python"}),   # routed
        Step("count", tool="word_count", args={"items": Ref("find")}),  # named tool
    ])
    report = await orch.run(plan, principal=alice)

    assert report.ok
    assert report.outputs == {
        "find": ["python result one", "python result two"],
        "count": 6,
    }
    assert report.served_by == {"find": ("web_search",), "count": ("word_count",)}
    assert [(r.tool, r.allowed) for r in report.audit] == [
        ("web_search", True),
        ("word_count", True),
    ]
    assert [e.kind for e in report.events if e.step_id == "find"] == [
        "started", "routed", "succeeded",
    ]
    assert report.run_id == "run-1"


asyncio.run(main())
```

What happened:

- `@orch.tool` built a `ToolSpec` and registered it. A function used bare is
  named after itself, offers one capability of the same name and has version
  `1.0.0`. The decorator returns the function unchanged, so you can still call
  it directly.
- `"find"` names a capability, so the router chose `web_search`. `"count"` names
  a tool, so it was looked up directly. `Ref("find")` made `count` depend on
  `find` and passed it `find`'s output.
- `alice` holds the `web:read` scope through the `reader` role. Both permission
  checks were audited, and `report.audit` contains only this run's records.

## Tool registry

A tool is an immutable `ToolSpec`. It has a name, a semantic version, the
capabilities it offers (each carrying routing hints), an **async** handler, the
scopes it requires, a simple input schema and free-form metadata.
`ToolRegistry` stores tools by name and then by version. It is thread-safe and
notifies listeners of every change.

```python
import asyncio

from orchestrator import (
    Capability,
    DuplicateToolError,
    RegistryEvent,
    ToolNotFoundError,
    ToolRegistry,
    ToolSpec,
    ValidationError,
)


async def search_v1(query: str) -> list[str]:
    return [f"v1: {query}"]


async def search_v2(query: str) -> list[str]:
    return [f"v2: {query}"]


registry = ToolRegistry()
events: list[RegistryEvent] = []
unsubscribe = registry.subscribe(events.append)

for version, handler in (("1.0.0", search_v1), ("1.10.0", search_v2)):
    registry.register(ToolSpec(
        name="web.search",
        version=version,
        capabilities=(Capability("search", cost=0.5, latency=2.0, priority=1), "lookup"),
        handler=handler,
        description="Search the web",
        required_scopes={"net:read"},
        input_schema={"query": str},
        metadata={"vendor": "example"},
    ))

# Versions are compared numerically, so 1.10.0 is newer than 1.0.0.
assert registry.versions("web.search") == ["1.0.0", "1.10.0"]
assert registry.get("web.search").version == "1.10.0"           # latest
assert registry.get("web.search", "1.0.0").handler is search_v1  # pinned

spec = registry.get("web.search")
assert spec.capability_names == {"search", "lookup"}  # plain strings become Capability("lookup")
assert spec.capability("search").cost == 0.5
assert spec.required_scopes == frozenset({"net:read"})
assert spec.key == ("web.search", "1.10.0")
assert asyncio.run(spec.handler(query="q")) == ["v2: q"]

# Discovery only sees enabled tools (the latest version of each name).
assert [s.name for s in registry.find_by_capability("search")] == ["web.search"]
assert registry.capabilities() == {"search", "lookup"}

registry.disable("web.search")                          # the whole name: every version
assert not registry.is_enabled("web.search")
assert registry.find_by_capability("search") == []      # 1.0.0 does not stand in for it
assert not registry.is_enabled("web.search", "1.0.0")  # every version is disabled
assert registry.get("web.search").version == "1.10.0"  # still retrievable
assert [s.name for s in registry.list(include_disabled=False)] == []
registry.enable("web.search")

# Duplicates are rejected unless replace=True.
try:
    registry.register(spec)
except DuplicateToolError as exc:
    assert "web.search@1.10.0 is already registered" in str(exc)
else:
    raise AssertionError("expected DuplicateToolError")
registry.register(spec, replace=True)

# Input validation: required keys with isinstance checks.
spec.validate_input({"query": "ok"})
try:
    spec.validate_input({"query": 42})
except ValidationError as exc:
    assert str(exc) == "tool 'web.search': input 'query' must be str"
else:
    raise AssertionError("expected ValidationError")

# Removing one version, then all of them.
registry.unregister("web.search", "1.0.0")
assert registry.versions("web.search") == ["1.10.0"]
registry.unregister("web.search")
assert "web.search" not in registry and len(registry) == 0
try:
    registry.get("web.search")
except ToolNotFoundError:
    pass
else:
    raise AssertionError("expected ToolNotFoundError")

unsubscribe()
assert [(e.kind, e.version) for e in events] == [
    ("registered", "1.0.0"),
    ("registered", "1.10.0"),
    ("disabled", "1.10.0"),
    ("enabled", "1.10.0"),
    ("registered", "1.10.0"),   # the replace
    ("unregistered", "1.0.0"),
    ("unregistered", "1.10.0"),
]
```

**Version-level and name-level disabling.** `disable(name, version)` switches
off one version. `disable(name)` switches off the whole tool name: every
version, including a version pinned by a step and versions registered while the
name stays disabled. A version counts as enabled only when neither it nor its
name is disabled, so `enable(name, version)` cannot override a name-level
disable; `enable(name)` clears the name-level flag and every per-version flag of
that name. Each call emits exactly one event, naming the given version or, for
a whole-name call, the latest version. Unregistering every version of a name
forgets its flags.

```python
from orchestrator import RegistryEvent, ToolRegistry, ToolSpec


async def calc(a: int) -> int:
    return a


registry = ToolRegistry()
for version in ("1.0.0", "2.0.0"):
    registry.register(ToolSpec(name="calc", version=version, capabilities=("calc",), handler=calc))
events: list[RegistryEvent] = []
registry.subscribe(events.append)

registry.disable("calc", "1.0.0")                     # one version only
assert not registry.is_enabled("calc", "1.0.0") and registry.is_enabled("calc", "2.0.0")
assert [s.version for s in registry.find_by_capability("calc")] == ["2.0.0"]

registry.disable("calc")                              # the whole name
registry.register(ToolSpec(name="calc", version="3.0.0", capabilities=("calc",), handler=calc))
assert not any(registry.is_enabled("calc", v) for v in registry.versions("calc"))
assert registry.find_by_capability("calc") == []

registry.enable("calc", "2.0.0")                      # does not override the name-level disable
assert not registry.is_enabled("calc", "2.0.0")
registry.enable("calc")                               # clears every flag of the name
assert all(registry.is_enabled("calc", v) for v in registry.versions("calc"))

assert [(e.kind, e.version) for e in events] == [
    ("disabled", "1.0.0"),
    ("disabled", "2.0.0"),     # whole-name call: names the latest version at the time
    ("registered", "3.0.0"),
    ("enabled", "2.0.0"),
    ("enabled", "3.0.0"),
]
```

Descriptors are validated when they are created. Names and versions must match
in full, so a trailing newline is rejected too:

```python
from orchestrator import Capability, ToolSpec, ValidationError, parse_version


async def handler() -> None:
    return None


def sync_handler() -> None:
    return None


assert parse_version("2.10.3") == (2, 10, 3)

bad_specs = [
    dict(name="Bad Name", version="1.0.0", capabilities=("x",), handler=handler),
    dict(name="ok\n", version="1.0.0", capabilities=("x",), handler=handler),
    dict(name="ok", version="1.0", capabilities=("x",), handler=handler),
    dict(name="ok", version="1.0.0\n", capabilities=("x",), handler=handler),
    dict(name="ok", version="1.0.0", capabilities=(), handler=handler),
    dict(name="ok", version="1.0.0", capabilities=("x\n",), handler=handler),
    dict(name="ok", version="1.0.0", capabilities=("x",), handler=sync_handler),
]
for kwargs in bad_specs:
    try:
        ToolSpec(**kwargs)
    except ValidationError:
        pass
    else:
        raise AssertionError(f"accepted {kwargs}")

bad_capabilities = [
    dict(name="UPPER"),
    dict(name="x", cost=-1),
    dict(name="x", latency=-1),
    dict(name="x", cost=float("nan")),
    dict(name="x", latency=True),       # booleans are not numbers here
    dict(name="x", priority=True),      # priority must be a real int...
    dict(name="x", priority=1.0),       # ...not a bool or a float
]
for bad in bad_capabilities:
    try:
        Capability(**bad)
    except ValidationError:
        pass
    else:
        raise AssertionError(f"accepted {bad}")

# An infinite cost or latency is allowed: it means "avoid unless nothing else is left".
assert Capability("x", cost=float("inf")).cost == float("inf")
```

**Reference**

| `ToolSpec` field | Meaning |
| --- | --- |
| `name` | lowercase ASCII `[a-z0-9][a-z0-9_.-]*`, matched in full (no trailing newline) |
| `version` | `MAJOR.MINOR.PATCH` (no leading zeros, no trailing newline); compared numerically |
| `capabilities` | `Capability` objects or plain names (converted); at least one |
| `handler` | an `async def` function called as `handler(**args)` |
| `description` | free text |
| `required_scopes` | scopes the caller needs; stored as a `frozenset`. Not validated here: a malformed or wildcard scope makes the policy deny the tool |
| `input_schema` | `{key: type}`; `validate_input` requires each key and checks `isinstance` |
| `metadata` | free-form; the orchestrator reads `metadata["resources"]` (resource claims) |

| `Capability` field | Default | Used by |
| --- | --- | --- |
| `name` | | capability lookup (same naming rule as tools) |
| `cost` | `1.0` | `cheapest`, `balanced` (a non-negative int or float; NaN and booleans are rejected, `float("inf")` is accepted) |
| `latency` | `1.0` | `fastest`, `balanced` (same rules as `cost`) |
| `priority` | `0` | `priority`, `balanced`, fan-out `metadata["priority"]` (a real `int`; `True` or `1.0` is rejected) |

| `ToolRegistry` method | Behaviour |
| --- | --- |
| `register(spec, *, replace=False)` | adds `name@version`; `DuplicateToolError` unless `replace` |
| `unregister(name, version=None)` | removes one version or all of them; returns the removed specs. Removing every version forgets the name's enable/disable flags |
| `enable(name, version=None)` / `disable(...)` | with a version, toggles that version; without one, disables or enables the whole tool name, meaning **every** version, including versions registered later. `enable(name)` also clears every per-version flag of the name, while `enable(name, version)` does not override a name-level disable. Each call emits exactly one event naming the given version, or the latest version for a whole-name call. Disabled tools stay retrievable |
| `get(name, version=None)` | the pinned version, or the latest; `ToolNotFoundError` |
| `is_enabled(name, version=None)` | whether that version (default: the latest) **and** its tool name are enabled |
| `list(*, include_disabled=True)` | latest version of every name, sorted by name |
| `versions(name)` | all versions, oldest first; `ToolNotFoundError` for an unknown name |
| `find_by_capability(cap)` | tools offering `cap` whose latest version is enabled (an older enabled version never stands in) |
| `capabilities()` | every capability offered by the latest version of an enabled tool |
| `subscribe(listener)` | calls `listener(RegistryEvent(kind, name, version))`; returns an unsubscribe function |
| `name in registry` / `len(registry)` | whether any version of `name` is registered / number of tool names |

Event kinds are `registered`, `unregistered`, `enabled` and `disabled`.
Listeners run synchronously on the thread that changed the registry. An
exception raised by a listener is logged and never breaks the registry.

## Capability routing

`CapabilityRouter(registry, *, strategy="balanced", failure_threshold=3,
cooldown=30.0, clock=time.monotonic)` answers the question "which tool should
serve capability X?". It ranks the enabled tools that offer the capability,
drops those the caller excludes or marks as not eligible, skips tools whose
circuit is open, and returns the best one. Higher scores win. Ties are broken by
tool name in ascending order. `candidates`, `route`, `route_many` and `explain`
all take the keyword arguments `eligible=None` (a `ToolSpec -> bool`
predicate), `exclude=()` (tool names; a single string also works) and
`strategy=None` (overrides the router's strategy for that call). An unknown
strategy name, a `failure_threshold` that is not an int `>= 1` or a negative
`cooldown` raises `ValidationError`.

```python
from orchestrator import (
    BALANCED_WEIGHTS,
    STRATEGIES,
    Capability,
    CapabilityRouter,
    NoRouteError,
    ToolRegistry,
    ToolSpec,
)


def make_tool(name: str, **hints) -> ToolSpec:
    async def handler(**_):
        return name
    return ToolSpec(name=name, version="1.0.0",
                    capabilities=(Capability("search", **hints),), handler=handler)


registry = ToolRegistry()
registry.register(make_tool("fast", cost=2.0, latency=0.1, priority=1))
registry.register(make_tool("cheap", cost=0.1, latency=3.0, priority=1))
registry.register(make_tool("best", cost=5.0, latency=1.0, priority=10))
router = CapabilityRouter(registry)

assert STRATEGIES == {"priority", "cheapest", "fastest", "balanced"}
assert router.route("search", strategy="priority").name == "best"
assert router.route("search", strategy="cheapest").name == "cheap"
assert router.route("search", strategy="fastest").name == "fast"
assert router.route("search").name == "best"  # the default strategy is balanced
assert [s.name for s in router.candidates("search")] == ["best", "fast", "cheap"]

# Caller filters: exclude by name, or an eligibility predicate (e.g. permissions).
assert router.route("search", exclude=["best"]).name == "fast"
assert router.route("search", eligible=lambda spec: spec.name != "fast",
                    strategy="fastest").name == "best"

# Custom strategy: any callable (spec, capability, stats) -> number.
def cheapest_healthy(spec, cap, stats):
    return stats.success_rate - cap.cost

assert router.route("search", strategy=cheapest_healthy).name == "cheap"
assert CapabilityRouter(registry, strategy=cheapest_healthy).route("search").name == "cheap"

# Several capabilities at once (duplicates collapse).
assert {c: s.name for c, s in router.route_many(["search", "search"]).items()} == {"search": "best"}

try:
    router.route("translate")
except NoRouteError as exc:
    assert str(exc) == "no enabled tool offers capability 'translate'"
else:
    raise AssertionError("expected NoRouteError")

try:
    router.route("search", eligible=lambda spec: False)
except NoRouteError as exc:
    assert str(exc).startswith("3 tools offer capability 'search' but none are usable")
else:
    raise AssertionError("expected NoRouteError")

assert BALANCED_WEIGHTS == {"priority": 0.4, "cost": 0.2, "latency": 0.2, "health": 0.2}
```

| Strategy | Ranking key (higher first, then name) |
| --- | --- |
| `priority` | `(priority, -cost, -latency)` |
| `cheapest` | `(-cost, priority, -latency)` |
| `fastest` | `(-latency, priority, -cost)` |
| `balanced` (default) | weighted score, below; always finite |
| callable | `fn(spec, capability, stats) -> float`; a non-numeric or NaN score raises `ValidationError` |

**The balanced formula.** The components are normalised over the *current
candidate set*, after exclusions, eligibility and open circuits have been
applied:

```text
priority_n = (priority - min_priority) / (max_priority - min_priority)   # 1.0 if all equal
cheap_n    = 1 - cost / max_cost                                          # 1.0 if max_cost == 0
fast_n     = 1 - latency / max_latency                                    # 1.0 if max_latency == 0
score      = 0.4 * priority_n + 0.2 * cheap_n + 0.2 * fast_n + 0.2 * success_rate
```

`success_rate` is `successes / calls`, or `1.0` while nothing has been observed.
A tool's score is relative to its peers, so it changes as tools come and go.
`explain()` shows the score used for each request.

**Infinite hints.** A capability may declare `cost=float("inf")` or
`latency=float("inf")`. Under `priority`, `cheapest` and `fastest` an infinite
hint ranks after every finite one (it becomes `-inf` in the ranking key). The
balanced score always stays finite: when the largest cost (or latency) among the
candidates is infinite, an infinite hint scores `0.0` for that component and a
finite one `1.0`, instead of the NaN that `inf / inf` would produce.

```python
import math

from orchestrator import Capability, CapabilityRouter, ToolRegistry, ToolSpec


def make_tool(name: str, **hints) -> ToolSpec:
    async def handler(**_):
        return name
    return ToolSpec(name=name, version="1.0.0",
                    capabilities=(Capability("search", **hints),), handler=handler)


registry = ToolRegistry()
registry.register(make_tool("metered", cost=float("inf"), latency=float("inf")))
registry.register(make_tool("normal", cost=50.0, latency=9.0))
router = CapabilityRouter(registry)

for strategy in ("priority", "cheapest", "fastest", "balanced"):
    assert [s.name for s in router.candidates("search", strategy=strategy)] == [
        "normal", "metered",
    ]
rows = router.explain("search")
assert all(math.isfinite(row.score) for row in rows)
assert [(row.tool, round(row.score, 2)) for row in rows] == [("normal", 1.0), ("metered", 0.6)]
```

**Health and the circuit breaker.** Callers report outcomes with
`record_success(name, latency=None)` and `record_failure(name)`; the
orchestrator does this for you. When a tool reaches `failure_threshold`
consecutive failures, its circuit **opens** and routing skips the tool. After
`cooldown` seconds the circuit becomes **half-open** and the tool is routable
again. The next reported failure reopens it at once, and a success **closes** it.
Any failure reported while the circuit is open or half-open restarts the
cooldown.

```python
from orchestrator import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    Capability,
    CapabilityRouter,
    ToolRegistry,
    ToolSpec,
)


def make_tool(name: str, **hints) -> ToolSpec:
    async def handler(**_):
        return name
    return ToolSpec(name=name, version="1.0.0",
                    capabilities=(Capability("search", **hints),), handler=handler)


registry = ToolRegistry()
registry.register(make_tool("fast", cost=2.0, latency=0.1, priority=1))
registry.register(make_tool("cheap", cost=0.1, latency=3.0, priority=1))
registry.register(make_tool("best", cost=5.0, latency=1.0, priority=10))

now = [0.0]  # a controllable clock
router = CapabilityRouter(registry, failure_threshold=2, cooldown=10.0, clock=lambda: now[0])

router.record_success("best", latency=0.2)
router.record_failure("best")
assert router.circuit_state("best") == CLOSED       # 1 consecutive failure < threshold
router.record_failure("best")
assert router.circuit_state("best") == OPEN
assert router.route("search").name == "fast"        # best is skipped while open

stats = router.stats("best")
assert (stats.successes, stats.failures, stats.consecutive_failures) == (1, 2, 2)
assert stats.calls == 3 and round(stats.success_rate, 3) == 0.333
assert stats.avg_latency == 0.2

# explain(): ranked candidates first (rank 1 is selected), then rejected tools by name.
rows = router.explain("search")
assert [(r.tool, r.rank, r.reason) for r in rows] == [
    ("fast", 1, None),
    ("cheap", 2, None),
    ("best", None, "circuit open"),
]
# Without best, the priorities are equal (priority_n = 1.0), max_cost = 2.0, max_latency = 3.0:
#   fast:  0.4*1 + 0.2*(1 - 2.0/2) + 0.2*(1 - 0.1/3) + 0.2*1 = 0.7933
#   cheap: 0.4*1 + 0.2*(1 - 0.1/2) + 0.2*(1 - 3.0/3) + 0.2*1 = 0.79
assert rows[0].selected and round(rows[0].score, 4) == 0.7933
assert round(rows[1].score, 4) == 0.79 and rows[1].key == (rows[1].score,)
assert rows[2].stats.circuit == OPEN

now[0] = 10.0                                      # cooldown elapsed
assert router.circuit_state("best") == HALF_OPEN
assert router.route("search").name == "best"       # probing again
router.record_failure("best")                      # a half-open failure reopens at once
assert router.circuit_state("best") == OPEN
now[0] = 20.0
router.record_success("best")                      # a success closes the circuit
assert router.circuit_state("best") == CLOSED

router.reset("best")                               # forget one tool (or all with reset())
assert router.stats("best").calls == 0
```

| Reason in `explain()` | Meaning |
| --- | --- |
| `excluded` | named in `exclude` |
| `not eligible` | the `eligible` predicate returned `False` |
| `circuit open` | the tool's circuit breaker is open |

The router's configuration is readable through the `strategy`,
`failure_threshold` and `cooldown` properties.

`RouteExplanation` fields: `tool`, `version`, `rank` (`None` when rejected),
`score`, `key` (the full ranking tuple), `reason`, `stats` (a `ToolStats`
snapshot) and the `selected` property. `ToolStats` fields: `name`, `successes`,
`failures`, `consecutive_failures`, `avg_latency`, `circuit`, and the `calls` and
`success_rate` properties.

## Permission scoping

**Scopes** are `:`-separated segments such as `fs:read` or `net:http:get`. Each
segment matches `[A-Za-z0-9_.-]+`. A *granted* scope may be a wildcard: `*`
covers everything, and `prefix:*` covers every scope strictly beneath `prefix`,
at a segment boundary. Scopes that a tool *requires* never contain wildcards.

```python
from orchestrator import ValidationError, scope_matches, scopes_cover, validate_scope

assert scope_matches("fs:read", "fs:read")
assert scope_matches("fs:*", "fs:read")
assert scope_matches("fs:*", "fs:read:secret")   # any depth beneath fs
assert not scope_matches("fs:*", "fsx:read")     # segment boundary
assert not scope_matches("fs:*", "fs")           # strictly beneath
assert scope_matches("*", "anything:at:all")
assert scopes_cover({"net:read", "fs:*"}, "fs:write")

assert validate_scope("net:http:get") == "net:http:get"
for bad in ("", "fs::read", "fs:re ad", "*:read"):
    try:
        validate_scope(bad)
    except ValidationError:
        pass
    else:
        raise AssertionError(bad)
try:
    scope_matches("*", "fs:*")      # a required scope may not be a wildcard
except ValidationError:
    pass
else:
    raise AssertionError("expected ValidationError")
```

**Principals and policies.** A `Principal(id, scopes=frozenset(), roles=frozenset(),
parent=None)` is an immutable identity for a user, an agent or a sub-agent.
`scopes` and `roles` take any iterable of strings (a bare string is rejected),
and `parent` is the principal that delegated to it. A
`PermissionPolicy(roles=None, *, audit_limit=1000, clock=time.time)` maps role
names to scopes, holds explicit rules and makes the decisions. Its audit log
keeps the newest `audit_limit` records (an int `>= 0`).

```python
from orchestrator import (
    AccessDeniedError,
    Capability,
    PermissionPolicy,
    Principal,
    ToolSpec,
)


def tool(name: str, *scopes: str) -> ToolSpec:
    async def handler(**_):
        return name
    return ToolSpec(name=name, version="1.0.0", capabilities=(Capability("x"),),
                    handler=handler, required_scopes=frozenset(scopes))


read_file = tool("fs.read", "fs:read")
write_file = tool("fs.write", "fs:write")
fetch = tool("net.fetch", "net:http:get")
clock = iter(range(100))
policy = PermissionPolicy(
    {"reader": ["fs:read"], "admin": ["*"]},
    audit_limit=100,
    clock=lambda: float(next(clock)),
)
policy.define_role("crawler", ["net:*"])

ana = Principal("ana", scopes={"net:http:get"}, roles={"reader"})
assert policy.effective_scopes(ana) == {"net:http:get", "fs:read"}

# decide(): no audit, never raises.
decision = policy.decide(ana, write_file)
assert (decision.allowed, decision.missing, decision.reason) == (
    False, frozenset({"fs:write"}), "missing scopes: fs:write",
)

# check(): audited, raises AccessDeniedError. allowed(): audited, returns a bool.
policy.check(ana, read_file)
assert policy.allowed(ana, fetch)
try:
    policy.check(ana, write_file)
except AccessDeniedError as exc:
    assert str(exc) == "principal 'ana' may not use tool 'fs.write': missing scopes: fs:write"
    assert (exc.principal_id, exc.tool, exc.version) == ("ana", "fs.write", "1.0.0")
    assert exc.missing == {"fs:write"}
else:
    raise AssertionError("expected AccessDeniedError")

# filter_tools() and predicate() are not audited (routers call them for every candidate).
assert [t.name for t in policy.filter_tools(ana, [read_file, write_file, fetch])] == [
    "fs.read", "net.fetch",
]
assert policy.predicate(ana)(write_file) is False

log = policy.audit_log
assert [(r.principal_id, r.tool, r.allowed) for r in log] == [
    ("ana", "fs.read", True),
    ("ana", "net.fetch", True),
    ("ana", "fs.write", False),
]
assert log[2].missing == {"fs:write"} and log[2].timestamp == 2.0
policy.clear_audit_log()
assert policy.audit_log == [] and policy.audit_limit == 100
```

**Rules and decision order.** `policy.deny(subject, tool_name)` and
`policy.allow_tool(subject, tool_name)` take a principal id **or** a role name
as the subject; ids and roles share one namespace. A rule applies to every
version of the tool, and `remove_rule(subject, tool_name)` removes it again. For
each `(principal, tool)` pair, the policy decides as follows:

1. If an explicit **deny** matches the principal or any principal that
   delegated to it, the tool is **denied**. A deny always wins.
2. If the tool declares a malformed or wildcard required scope, it is **denied**
   (the policy fails closed).
3. If the effective scopes cover every required scope, the tool is **allowed**.
4. If an explicit **allow** matches the principal, and its parent (if it has
   one) is itself allowed the tool, the tool is **allowed**.
5. Otherwise the tool is **denied**, and the uncovered scopes are reported as
   `missing`.

```python
from orchestrator import PermissionPolicy, Principal, ToolSpec


async def handler(**_):
    return None


deploy = ToolSpec(name="deploy", version="1.0.0", capabilities=("deploy",),
                  handler=handler, required_scopes={"ops:deploy"})
policy = PermissionPolicy({"admin": ["*"], "oncall": []})

root = Principal("root", roles={"admin"})
bob = Principal("bob", roles={"oncall"})

assert policy.decide(root, deploy).allowed                   # rule 3: scopes cover
assert not policy.decide(bob, deploy).allowed                # rule 5: missing ops:deploy

policy.allow_tool("oncall", "deploy")                        # rule 4: allow by role
assert policy.decide(bob, deploy).reason == "tool 'deploy' is explicitly allowed for 'oncall'"

policy.deny("root", "deploy")                                # rule 1: deny wins over '*'
assert policy.decide(root, deploy).reason == "tool 'deploy' is explicitly denied for 'root'"

# A deny on a delegator is inherited by its delegates.
child = policy.delegate(root, ["ops:deploy"], id="root/ci")
assert policy.decide(child, deploy).reason == (
    "tool 'deploy' is explicitly denied for 'root' (inherited from delegating principal 'root')"
)
assert policy.remove_rule("root", "deploy") is True
assert policy.decide(child, deploy).allowed
```

**Delegation.** Delegation only ever narrows authority. A delegate's effective
scopes are always intersected with those of every principal up its delegation
chain. `Principal.narrow(scopes, *, id=None)` delegates scopes the principal
holds **directly**. `policy.delegate(principal, scopes, *, id=None)` can also
delegate scopes the principal gets **from its roles**. If no id is given, the
child's id is `<parent id>/delegate`. A delegate holds no roles of its own.
`principal.chain` lists the principal and its delegators, nearest first, and
`principal.delegable_scopes` is what `narrow` may hand on (own scopes bounded by
every ancestor's own scopes).

```python
from orchestrator import AccessDeniedError, PermissionPolicy, Principal

policy = PermissionPolicy({"analyst": ["search:*", "docs:read"]})

lead = Principal("lead", scopes={"search:*", "docs:read", "fs:write"})
helper = lead.narrow(["search:read", "docs:read"], id="lead/helper")
scout = helper.narrow(["search:read"])
assert scout.id == "lead/helper/delegate"
assert [p.id for p in scout.chain] == ["lead/helper/delegate", "lead/helper", "lead"]
assert policy.effective_scopes(scout) == {"search:read"}

try:
    scout.narrow(["fs:write"])          # cannot delegate what it does not hold
except AccessDeniedError as exc:
    assert exc.missing == {"fs:write"}
    assert str(exc) == (
        "principal 'lead/helper/delegate' cannot delegate scopes not held by "
        "'lead/helper/delegate': fs:write"
    )
else:
    raise AssertionError("expected AccessDeniedError")

# Role-granted scopes: narrow() refuses them, policy.delegate() accepts them.
ana = Principal("ana", roles={"analyst"})
try:
    ana.narrow(["search:read"])
except AccessDeniedError:
    pass
else:
    raise AssertionError("expected AccessDeniedError")
bot = policy.delegate(ana, ["search:read"], id="ana/bot")
assert bot.parent is ana and policy.effective_scopes(bot) == {"search:read"}

# Effective scopes stay bounded by the parent, even if the child claims more.
rogue = Principal("rogue", scopes={"*"}, parent=helper)
assert policy.effective_scopes(rogue) == {"search:read", "docs:read"}
```

| `PermissionPolicy` member | Audited | Purpose |
| --- | --- | --- |
| `define_role(name, scopes)` / `roles` | | define or replace a role / copy of all roles |
| `effective_scopes(principal)` | | own and role scopes, bounded by every delegator |
| `delegate(principal, scopes, *, id=None)` | | child principal; the delegator's role scopes count |
| `deny` / `allow_tool` / `remove_rule` | | explicit per-tool rules for an id or a role |
| `decide(principal, tool)` | no | returns `Decision(allowed, missing, reason)` |
| `check(principal, tool)` | **yes** | raises `AccessDeniedError` on denial |
| `allowed(principal, tool)` | **yes** | returns a bool |
| `filter_tools(principal, tools)` | no | the permitted tools, in input order |
| `predicate(principal)` | no | a non-raising `tool -> bool` filter for routers; fails closed |
| `audit_log` / `clear_audit_log()` / `audit_limit` | | bounded log of `AuditRecord`s, oldest first |

`AuditRecord` fields: `principal_id`, `tool`, `version`, `allowed`, `missing`,
`reason` and `timestamp` (taken from the policy's `clock`).

## Parallel execution

A `Plan` is a validated DAG of `Step`s. `ParallelExecutor(invoker, *,
max_concurrency=8, fail_fast=False, default_timeout=None, lock_manager=None,
on_event=None)` starts each step as soon as all of its dependencies have
succeeded. The executor never routes and never checks permissions; it calls an
**invoker** for every attempt. `default_invoker(registry)` calls
`registry.get(step.tool, step.version)` directly. The `Orchestrator` supplies a
routing, permission-checking invoker instead.

```python
import asyncio

from orchestrator import (
    CycleError,
    ParallelExecutor,
    Plan,
    Ref,
    Step,
    ToolRegistry,
    ToolSpec,
    default_invoker,
)

active = {"now": 0, "max": 0}


async def fetch(url: str) -> dict:
    active["now"] += 1
    active["max"] = max(active["max"], active["now"])
    await asyncio.sleep(0.01)
    active["now"] -= 1
    return {"url": url, "items": [{"title": f"title of {url}"}]}


async def join(parts: list) -> str:
    return " | ".join(parts)


registry = ToolRegistry()
for name, handler in (("fetch", fetch), ("join", join)):
    registry.register(ToolSpec(name=name, version="1.0.0", capabilities=(name,), handler=handler))

plan = Plan([
    Step("a", tool="fetch", args={"url": "a.example"}),
    Step("b", tool="fetch", args={"url": "b.example"}),
    Step("c", tool="fetch", args={"url": "c.example"}),
    # Refs anywhere in args (nested in lists/dicts too) add dependencies.
    Step("all", tool="join", args={"parts": [
        Ref("a", "items.0.title"), Ref("b", "items.0.title"), Ref("c", "url"),
    ]}),
])
assert plan["all"].depends_on == ("a", "b", "c")
assert plan.levels() == [["a", "b", "c"], ["all"]]
assert plan.order() == ["a", "b", "c", "all"]
assert plan.dependents("a") == ["all"]


async def main() -> None:
    events = []
    executor = ParallelExecutor(default_invoker(registry), max_concurrency=2,
                                on_event=events.append)
    report = await executor.run(plan)
    assert report.ok and report.succeeded == ["a", "b", "c", "all"]
    assert report["all"].value == "title of a.example | title of b.example | c.example"
    assert active["max"] == 2                       # bounded by max_concurrency
    assert report["a"].attempts == 1 and report["a"].duration > 0
    assert report["a"].result.tool == "fetch"
    kinds = [e.kind for e in events]
    assert kinds.count("started") == 4 and kinds.count("succeeded") == 4
    assert kinds.index("started") < kinds.index("succeeded")


asyncio.run(main())

try:
    Plan([Step("x", tool="fetch", depends_on=["y"]), Step("y", tool="fetch", depends_on=["x"])])
except CycleError as exc:
    assert str(exc) == "dependency cycle: x -> y -> x"
else:
    raise AssertionError("expected CycleError")
```

**Timeouts, retries, skips and fail-fast.**

```python
import asyncio

from orchestrator import (
    ExecutionError,
    ParallelExecutor,
    Plan,
    Step,
    ToolRegistry,
    ToolSpec,
    ToolTimeoutError,
    default_invoker,
)

calls = {"flaky": 0}


async def flaky() -> str:
    calls["flaky"] += 1
    if calls["flaky"] < 3:
        raise ConnectionError(f"attempt {calls['flaky']} failed")
    return "finally"


async def slow() -> str:
    await asyncio.sleep(1)
    return "too late"


async def boom() -> str:
    raise RuntimeError("boom")


async def echo(value: str = "") -> str:
    return value


registry = ToolRegistry()
for name, fn in (("flaky", flaky), ("slow", slow), ("boom", boom), ("echo", echo)):
    registry.register(ToolSpec(name=name, version="1.0.0", capabilities=(name,), handler=fn))


async def main() -> None:
    events = []
    executor = ParallelExecutor(default_invoker(registry), on_event=events.append)
    report = await executor.run(Plan([
        # Up to 2 retries; the delays are retry_backoff * 2 ** (retry - 1): 0.01s, then 0.02s.
        Step("retry", tool="flaky", retries=2, retry_backoff=0.01),
        # The timeout applies to each attempt (default_timeout applies when it is None).
        Step("timeout", tool="slow", timeout=0.05),
        Step("fail", tool="boom"),
        Step("after_fail", tool="echo", depends_on=["fail"]),
        Step("after_skip", tool="echo", depends_on=["after_fail"]),
    ]))

    assert report["retry"].ok and report["retry"].attempts == 3
    retrying = [(e.attempt, e.error) for e in events if e.kind == "retrying"]
    assert retrying == [(1, "ConnectionError: attempt 1 failed"),
                        (2, "ConnectionError: attempt 2 failed")]

    assert report["timeout"].status == "failed"
    assert report["timeout"].error == "ToolTimeoutError: step 'timeout' timed out after 0.05s"
    assert isinstance(report["timeout"].exception, ToolTimeoutError)

    assert report["fail"].error == "RuntimeError: boom"
    assert report["after_fail"].error == "skipped: dependency fail failed"
    assert report["after_skip"].error == "skipped: dependency after_fail skipped"
    assert report.failed == ["timeout", "fail"]
    assert report.skipped == ["after_fail", "after_skip"]
    assert report.outputs == {"retry": "finally"}
    try:
        report.raise_for_failure()
    except ExecutionError as exc:
        assert str(exc).startswith("plan execution failed; timeout: ToolTimeoutError")
    else:
        raise AssertionError("expected ExecutionError")

    # fail_fast: the first failure cancels running steps and everything not yet started.
    fast = ParallelExecutor(default_invoker(registry), fail_fast=True)
    report = await fast.run(Plan([
        Step("fail", tool="boom"),
        Step("long", tool="slow"),
        Step("after_long", tool="echo", depends_on=["long"]),
        Step("after_fail", tool="echo", depends_on=["fail"]),
    ]))
    assert report.failed == ["fail"]
    assert report.cancelled == ["long", "after_long"]
    assert report.skipped == ["after_fail"]
    assert report["long"].error == "cancelled (fail-fast)"


asyncio.run(main())
```

**Locks and custom invokers.** Steps declare `resources={"name": "read" | "write"}`.
If the executor has a `lock_manager`, each attempt takes the step's locks
*before* it takes a concurrency slot, so no slot sits idle waiting for a lock. An
invoker is any `async (step, args) -> ToolResult`.

```python
import asyncio

from orchestrator import ParallelExecutor, Plan, ResourceLockManager, Step, ToolResult

state = {"db_writers": 0, "max_db_writers": 0, "max_running": 0, "running": 0}


async def invoker(step: Step, args: dict) -> ToolResult:
    """A custom invoker with no registry at all: it doubles args["n"]."""
    writes_db = step.resources.get("db") == "write"
    state["running"] += 1
    state["db_writers"] += writes_db
    state["max_running"] = max(state["max_running"], state["running"])
    state["max_db_writers"] = max(state["max_db_writers"], state["db_writers"])
    await asyncio.sleep(0.01)
    state["running"] -= 1
    state["db_writers"] -= writes_db
    return ToolResult(tool=step.target, version="0", ok=True, value=args["n"] * 2)


async def main() -> None:
    plan = Plan([
        Step("w1", tool="writer", args={"n": 1}, resources={"db": "write"}),
        Step("w2", tool="writer", args={"n": 2}, resources={"db": "write"}),
        Step("r1", capability="reader", args={"n": 3}, resources={"cache": "read"}),
    ])
    report = await ParallelExecutor(invoker, lock_manager=ResourceLockManager()).run(plan)
    assert report.outputs == {"w1": 2, "w2": 4, "r1": 6}
    assert state["max_db_writers"] == 1      # w1 and w2 never overlap...
    assert state["max_running"] == 2         # ...but r1 runs alongside one of them
    assert report["r1"].result.tool == "capability:reader"


asyncio.run(main())
```

| `Step` field | Default | Meaning |
| --- | --- | --- |
| `id` | | unique within the plan |
| `tool` / `capability` | | exactly one must be set |
| `version` | `None` | pins the tool version (only with `tool`) |
| `args` | `{}` | keyword arguments; `Ref`s are resolved before each attempt |
| `depends_on` | `()` | extra dependencies; `Ref`s add theirs automatically |
| `timeout` | `None` | seconds per attempt; falls back to the executor's `default_timeout` |
| `retries` | `0` | extra attempts after a failure (exception or `ok=False`) |
| `retry_backoff` | `0.0` | the delay before retry *k* is `retry_backoff * 2 ** (k - 1)` |
| `resources` | `{}` | `{resource: "read" \| "write"}` claims passed to the lock manager |

`Step` rejects (with `ValidationError`) a step that sets both or neither of
`tool` and `capability`, a `version` without `tool`, a negative or non-int
`retries`, a negative `retry_backoff`, a `timeout <= 0` and a resource mode
other than `read` or `write`. `Plan(steps)` rejects duplicate ids, dependencies
on unknown steps and cycles (`CycleError`, which names the cycle). A plan
supports `plan[step_id]`, `in`, iteration, `len`, `steps`, `levels()` (the
topological layers), `order()` and `dependents(step_id)`. `ParallelExecutor`
rejects a `max_concurrency` below 1 and a `default_timeout <= 0`.

`step.target` gives a readable name for the target, such as `calc@1.0.0` or
`capability:search`. `Ref(step, path=None)` resolves to the whole output, or
walks a dotted `path` of mapping keys and sequence indexes such as
`"items.0.title"`. An unresolvable path fails the step with `ValidationError`.

| Executor event (`StepEvent.kind`) | When |
| --- | --- |
| `started` | the step begins (before its locks and its slot) |
| `retrying` | before a retry; `attempt` = attempts so far, `error` = last error |
| `succeeded` / `failed` | final outcome |
| `skipped` | a dependency failed or was skipped |
| `cancelled` | cancelled by fail-fast |

A `StepEvent` has `kind`, `step_id`, `attempt`, `error` and `at` (a
`time.monotonic()` value). An `on_event` listener that raises is logged and
never breaks the run.

`ExecutionReport` gives you `results` (step id to `StepOutcome`, in plan order),
`ok`, `outputs` (succeeded steps only), `succeeded`, `failed`, `skipped`,
`cancelled`, `duration`, `report[step_id]` and `raise_for_failure()`.
`StepOutcome` has `step_id`, `status`, `result`, `error`, `attempts`,
`started_at`, `finished_at`, `exception`, `ok`, `value` and `duration`.

## Conflict resolution

When several tools answer the same request, `ConflictResolver(default_strategy=
"majority", *, priorities=None)` reconciles their `ToolResult`s into a single
`Resolution(value, strategy, winners, dissenters, confidence, details)`. Only
results with `ok=True` take part. Values are compared structurally and
type-aware, and the order of dict keys does not matter:

- numbers compare by value, so `1` equals `1.0`, but a boolean only equals a
  boolean (`True` is not `1`);
- strings, bytes and numbers never equal each other (`"1"` is not `1`, and
  `b"a"` is not `"a"`);
- a list equals a list and a tuple a tuple, element by element, but a list
  never equals a tuple;
- dict keys follow the same rules (`{1: x}` matches `{1.0: x}` but not
  `{"1": x}`);
- NaN equals NaN, so identical NaN answers group together.

```python
from orchestrator import ConflictError, ConflictResolver, ToolResult, ValidationError


def result(tool, value=None, ok=True, priority=0):
    return ToolResult(tool=tool, version="1.0.0", ok=ok, value=value,
                      error=None if ok else "boom", metadata={"priority": priority})


answers = [
    result("a", 42, priority=1),
    result("b", 42, priority=2),
    result("c", 41, priority=9),
    result("d", ok=False),                 # failures never take part
]
resolver = ConflictResolver()
assert resolver.default_strategy == "majority"
assert resolver.strategies == ("first_success", "majority", "merge", "numeric", "priority")

# majority: the largest group of equal values wins; ties go to the group containing
# the highest priority among all its members, then to the group containing the
# smallest tool name among all its members.
res = resolver.resolve(answers)
assert (res.value, res.winners, res.dissenters) == (42, ("a", "b"), ("c",))
assert round(res.confidence, 3) == 0.667
assert res.details == {"groups": [["a", "b"], ["c"]], "tie_broken": False, "quorum": None}
try:
    resolver.resolve(answers, "majority", quorum=0.75)   # 2/3 < 0.75
except ConflictError as exc:
    assert str(exc).startswith("majority quorum 0.75 not met")
else:
    raise AssertionError("expected ConflictError")

# first_success: the first successful value, in the order supplied.
assert resolver.resolve(answers, "first_success").value == 42

# priority: the highest priority wins (configured priorities beat metadata["priority"]).
res = resolver.resolve(answers, "priority")
assert (res.value, res.details) == (41, {"selected": "c", "priority": 9})
ranked = ConflictResolver(priorities={"a": 100})
assert ranked.resolve(answers, "priority").details["selected"] == "a"

# numeric: mean (default), median, min or max; confidence = 1 - spread / max(|low|, |high|).
res = resolver.resolve(answers, "numeric")
assert round(res.value, 3) == 41.667 and round(res.confidence, 4) == 0.9762
assert resolver.resolve(answers, "numeric", agg="median").value == 42
assert resolver.resolve(answers, "numeric", agg="min").value == 41

# Nothing successful to reconcile, or an unknown option, is an error.
try:
    resolver.resolve([result("d", ok=False)])
except ConflictError as exc:
    assert str(exc) == "no successful results to reconcile (d: boom)"
else:
    raise AssertionError("expected ConflictError")
try:
    resolver.resolve(answers, "first_success", quorum=1)
except ValidationError:
    pass
else:
    raise AssertionError("expected ValidationError")
```

**Equality, tie-breaks and numeric edge cases.**

```python
from orchestrator import ConflictError, ConflictResolver, ToolResult


def result(tool, value, priority=0):
    return ToolResult(tool=tool, version="1.0.0", ok=True, value=value,
                      metadata={"priority": priority})


resolver = ConflictResolver()

# Type-aware equality: 1 == 1.0, but True is not 1; lists, tuples and strings differ.
res = resolver.resolve([result("a", 1), result("b", 1.0), result("c", True)])
assert (res.value, res.winners, res.dissenters) == (1, ("a", "b"), ("c",))
res = resolver.resolve([result("a", [1]), result("b", (1,)), result("c", "1")])
assert res.details["groups"] == [["a"], ["b"], ["c"]]       # three singletons
res = resolver.resolve([result("a", {1: "x"}), result("b", {1.0: "x"}), result("c", {"1": "x"})])
assert res.winners == ("a", "b")
res = resolver.resolve([result("a", float("nan")), result("b", float("nan")), result("c", 0)])
assert res.winners == ("a", "b")                             # NaN groups with NaN

# Tie-break: two groups of two. "B" contains the highest priority of any member (bob: 9).
res = resolver.resolve([
    result("zed", "A", priority=5), result("amy", "B", priority=1),
    result("bob", "B", priority=9), result("cat", "A", priority=1),
])
assert (res.value, res.winners) == ("B", ("amy", "bob"))
assert res.details == {"groups": [["amy", "bob"], ["zed", "cat"]], "tie_broken": True,
                       "quorum": None}

# numeric: booleans, inf, -inf and NaN are rejected, and so is an overflowing
# aggregate or spread; the strategy never returns inf.
bad_inputs = [
    ([True, 1], "mean"),
    ([float("inf"), 1.0], "mean"),
    ([float("nan"), 1.0], "max"),
    ([1e308, 1e308], "median"),          # (a + b) / 2 would overflow to inf
    ([-1.7e308, 1.7e308], "min"),        # the spread high - low overflows
]
for values, agg in bad_inputs:
    try:
        resolver.resolve([result(f"t{i}", v) for i, v in enumerate(values)], "numeric", agg=agg)
    except ConflictError:
        pass
    else:
        raise AssertionError(f"accepted {values} with agg={agg}")

# Equal huge integers are aggregated exactly, without going through float.
res = resolver.resolve([result("a", 10**400), result("b", 10**400)], "numeric")
assert res.value == 10**400 and res.confidence == 1.0
```

**Merging and custom strategies.**

```python
from orchestrator import ConflictError, ConflictResolver, Resolution, ToolResult


def result(tool, value, priority=0):
    return ToolResult(tool=tool, version="1.0.0", ok=True, value=value,
                      metadata={"priority": priority})


directory = result("directory", {"name": "Ada", "team": "analytics", "tags": ["math"]}, 2)
contacts = result("contacts", {"email": "ada@example.org", "team": "research",
                               "tags": ["math", "poetry"]}, 1)
resolver = ConflictResolver()

# merge: dicts merge recursively, lists are unioned, equal scalars agree.
# A genuine conflict (here "team") raises, or is settled with on_conflict.
try:
    resolver.resolve([directory, contacts], "merge")
except ConflictError as exc:
    assert str(exc) == "merge conflict at team (contacts, directory)"
else:
    raise AssertionError("expected ConflictError")

res = resolver.resolve([contacts, directory], "merge", on_conflict="priority")
assert res.value == {"email": "ada@example.org", "team": "analytics",
                     "tags": ["math", "poetry"], "name": "Ada"}
assert (res.winners, res.dissenters, res.confidence) == (("directory",), ("contacts",), 0.5)
assert res.details["conflicting_paths"] == ["team"]
assert res.details["conflicts"][0]["chosen"] == "directory"
first = resolver.resolve([contacts, directory], "merge", on_conflict="first")
assert first.value["team"] == "research"

# Custom strategy: fn(results, **options) returning a Resolution or a plain value.
# A plain value is wrapped: the winners are the participants whose value equals it.
def longest(results, **options):
    return max((r.value for r in results), key=len)

words = [result("x", "short"), result("y", "much longer"), result("z", "much longer")]
res = resolver.resolve(words, longest)
assert (res.strategy, res.value, res.winners, res.dissenters) == (
    "longest", "much longer", ("y", "z"), ("x",),
)

def unanimous(results, **options):
    values = {r.value for r in results}
    return Resolution(value=values.pop() if len(values) == 1 else None, strategy="unanimous",
                      winners=tuple(r.tool for r in results), dissenters=(),
                      confidence=1.0 if len(values) == 0 else 0.0)

resolver.register_strategy("unanimous", unanimous)
assert resolver.resolve(words[1:], "unanimous").value == "much longer"
assert "unanimous" in resolver.strategies
```

| Strategy | Options | Value | Winners / confidence |
| --- | --- | --- | --- |
| `first_success` | none | first successful value | equal-valued participants / share |
| `majority` (default) | `quorum` in `(0, 1]` | largest group's value (ties: highest member priority, then smallest member name; `details["tie_broken"]`) | that group / its share; `ConflictError` if share < quorum |
| `priority` | none | value of the highest-priority result (ties: lowest tool name) | equal-valued participants / share; `details["selected"]`, `details["priority"]` |
| `merge` | `on_conflict`: `error` (default), `priority`, `first` | recursive dict merge, list union (non-dict values must all be equal) | non-dissenters / share; `details["conflicts"]`, `details["conflicting_paths"]` |
| `numeric` | `agg`: `mean` (default), `median`, `min`, `max` | aggregate of finite `int`/`float` values (exact for ints); booleans, `inf`, `-inf`, NaN and an overflowing aggregate or spread raise `ConflictError` | all / `1 - spread / scale` |
| callable or registered name | any | returned value or `Resolution` | computed as for `first_success` when a plain value is returned |

`resolve(results, strategy=None, **options)` uses the resolver's
`default_strategy` when `strategy` is `None`. An unknown strategy name or an
option the strategy does not accept raises `ValidationError`.
`register_strategy(name, fn, *, replace=False)` adds a named strategy (an
existing name needs `replace=True`), and `strategies` lists every name. A
callable passed directly is reported under its `__name__` (`"custom"` for a
lambda).

A result's priority (`resolver.priority_of(result)`) is the one configured in
`priorities` for its tool name if there is one, otherwise `metadata["priority"]`,
otherwise 0. The orchestrator
sets `metadata["priority"]` on every fan-out result from the tool's capability
priority.

**Resource locks.** `ResourceLockManager` provides readers-writer locks over
named resources, for steps that run concurrently.

- Many readers, *or* one writer, may hold a resource at a time.
- Requests are granted in strict FIFO order. Consecutive readers at the head of
  the queue are granted together, and a waiting writer blocks readers that
  arrive after it, so writers are never starved.
- A request for several resources acquires them in sorted name order, which
  rules out deadlocks.
- Locks are **not re-entrant**. An owner that requests a resource it already
  holds, or is already waiting for, gets a `ConflictError` immediately.
- `hold(owner, resources, *, timeout=None)` returns a single-use async context
  manager. If the locks cannot be acquired within `timeout`, it raises
  `ConflictError`. Locks are released on normal exit, on exceptions and on
  cancellation.
- `holders(resource)` returns the current `{owner: mode}` holders, and
  `waiting(resource)` the number of queued requests.
- A manager belongs to one event loop and is not thread-safe.

`detect_write_conflicts(claims)` is a static check over `{owner: {resource:
mode}}` claims. It returns a sorted `(resource, owner_a, owner_b)` triple, with
the owners in alphabetical order, for every pair that shares a resource where at
least one of them writes.

```python
import asyncio

from orchestrator import LOCK_MODES, ConflictError, ResourceLockManager, detect_write_conflicts


async def main() -> None:
    locks = ResourceLockManager()
    order = []

    async def use(owner, mode):
        async with locks.hold(owner, {"db": mode}):
            order.append(owner)
            await asyncio.sleep(0.01)

    async with locks.hold("reader-1", {"db": "read"}), locks.hold("reader-2", {"db": "read"}):
        assert locks.holders("db") == {"reader-1": "read", "reader-2": "read"}

        # A writer waits for the readers; it gives up after the timeout.
        try:
            async with locks.hold("impatient", {"db": "write"}, timeout=0.01):
                pass
        except ConflictError as exc:
            assert str(exc) == "owner 'impatient' could not acquire db:write within 0.01s"
        else:
            raise AssertionError("expected ConflictError")
        assert locks.waiting("db") == 0  # a timed-out request leaves nothing queued

        writer = asyncio.create_task(use("writer", "write"))
        await asyncio.sleep(0)
        late_reader = asyncio.create_task(use("late-reader", "read"))
        await asyncio.sleep(0)
        assert locks.waiting("db") == 2   # the late reader queues behind the writer

    await asyncio.gather(writer, late_reader)
    assert order == ["writer", "late-reader"]   # FIFO: no writer starvation
    assert locks.holders("db") == {}

    async with locks.hold("job", {"db": "write", "cache": "read"}) as held:
        assert held.resources == {"cache": "read", "db": "write"}
        try:
            async with locks.hold("job", {"db": "read"}):   # not re-entrant
                pass
        except ConflictError as exc:
            assert "locks are not re-entrant" in str(exc)
        else:
            raise AssertionError("expected ConflictError")


asyncio.run(main())

# Static analysis: which claims conflict (at least one writer on a shared resource)?
claims = {
    "save_a": {"reports": "write"},
    "save_b": {"reports": "write"},
    "read_c": {"reports": "read", "db": "read"},
    "read_d": {"db": "read"},
}
assert detect_write_conflicts(claims) == [
    ("reports", "read_c", "save_a"),
    ("reports", "read_c", "save_b"),
    ("reports", "save_a", "save_b"),
]
assert LOCK_MODES == ("read", "write")
```

## Orchestrator agent

`Orchestrator` composes everything above into one entry point that acts on
behalf of a principal. Every argument is keyword-only, and the values are
validated eagerly (`ValidationError`):

| Option | Default | Meaning |
| --- | --- | --- |
| `registry` | new `ToolRegistry` (or the given router's registry) | where tools live |
| `policy` | `PermissionPolicy()` (no roles) | permission decisions and audit log |
| `router` | `CapabilityRouter(registry)` | must be bound to the same registry |
| `resolver` | `ConflictResolver()` | fan-out reconciliation |
| `lock_manager` | `ResourceLockManager()` | shared by every run |
| `max_concurrency` | `8` | steps running at once (int `>= 1`) |
| `fail_fast` | `False` | cancel the run on the first failed step |
| `default_timeout` | `None` | per-attempt step timeout when `Step.timeout` is `None` |
| `tool_timeout` | `None` | per-tool-call timeout; counts as a tool failure, and capability steps fall back |
| `max_fallbacks` | `2` | extra tools tried after a failure (int `>= 0`) |
| `on_event` | `None` | `callable(OrchestratorEvent)`, called for every event of every run |

Each of these is also available as a read-only property, for example
`orch.registry`, `orch.policy` and `orch.max_fallbacks`.

```python
from orchestrator import (
    CapabilityRouter,
    ConflictResolver,
    Orchestrator,
    PermissionPolicy,
    ResourceLockManager,
    ToolRegistry,
    ValidationError,
)

orch = Orchestrator()
assert isinstance(orch.registry, ToolRegistry)
assert isinstance(orch.policy, PermissionPolicy)
assert isinstance(orch.router, CapabilityRouter)
assert isinstance(orch.resolver, ConflictResolver)
assert isinstance(orch.lock_manager, ResourceLockManager)
assert (orch.max_concurrency, orch.fail_fast, orch.max_fallbacks) == (8, False, 2)
assert (orch.default_timeout, orch.tool_timeout, orch.on_event) == (None, None, None)

registry = ToolRegistry()
router = CapabilityRouter(registry, strategy="cheapest")
assert Orchestrator(router=router).registry is registry      # adopts the router's registry

for bad in (dict(max_fallbacks=-1), dict(max_concurrency=0), dict(tool_timeout=0),
            dict(registry=ToolRegistry(), router=router)):
    try:
        Orchestrator(**bad)
    except ValidationError:
        pass
    else:
        raise AssertionError(bad)
```

### Registering tools

`orch.register(spec, *, replace=False)` registers a `ToolSpec`. The decorator
`orch.tool(fn=None, /, *, name=None, version="1.0.0", capabilities=None,
scopes=(), input_schema=None, description=None, metadata=None, replace=False)`
also registers a tool, and works either bare or with arguments. `capabilities`
and `scopes` each accept a single value or an iterable, and a capability can be
given as a name or as a `Capability`. When they are omitted, the name defaults
to the function's name, the capabilities to `(name,)` and the description to
the first line of the docstring.

```python
import asyncio

from orchestrator import Capability, Orchestrator, ToolSpec

orch = Orchestrator()


@orch.tool
async def ping() -> str:
    """Answer pong.

    More details here are ignored by the description.
    """
    return "pong"


@orch.tool(name="geo.lookup", version="2.1.0",
           capabilities=[Capability("geocode", cost=0.2, priority=3), "places"],
           scopes=["geo:read"], input_schema={"address": str},
           metadata={"owner": "maps-team"})
async def lookup(address: str) -> tuple[float, float]:
    return (51.5, -0.12)


async def legacy(**_) -> str:
    return "legacy"

orch.register(ToolSpec(name="legacy", version="0.9.0", capabilities=("ping",), handler=legacy))

spec = orch.registry.get("ping")
assert (spec.version, spec.description) == ("1.0.0", "Answer pong.")
assert spec.capability_names == {"ping"}
geo = orch.registry.get("geo.lookup")
assert geo.capability_names == {"geocode", "places"} and geo.required_scopes == {"geo:read"}
assert asyncio.run(ping()) == "pong"     # the decorated function is returned unchanged
assert [s.name for s in orch.registry.find_by_capability("ping")] == ["legacy", "ping"]
```

### Named-tool steps, capability steps and fallback

A step that names a `tool` (optionally with a `version` pin) is looked up
directly. If the tool is unknown, the step fails with `ToolNotFoundError`; if it
is disabled, with `ExecutionError`; if the args fail its input schema, with
`ValidationError`; and if the principal may not use it, with
`AccessDeniedError`. A named-tool step never falls back to another tool.
"Disabled" includes a pinned older version of a tool whose whole name was
disabled with `disable(name)`: such a step fails with `ExecutionError`
(`calc@1.0.0 is disabled`) instead of running the old version. To switch off
just one version, use `disable(name, version)`.

A step that names a `capability` is routed among the tools the principal may
use. If the chosen tool raises, returns
`ok=False` or exceeds `tool_timeout`, the orchestrator tries the next candidate,
up to `max_fallbacks` extra tools. The step then fails with
`FallbackExhaustedError`, chained to the last tool error. A candidate whose input
schema rejects the args is skipped without counting as a failure, and so is a
candidate found unavailable right before invocation (see
[Invocation-time checks](#invocation-time-checks)). If no tool was invoked at
all, the step fails with `NoRouteError`, or with the input's `ValidationError`
when every candidate rejected the args.

```python
import asyncio

from orchestrator import (
    Capability,
    FallbackExhaustedError,
    Orchestrator,
    PermissionPolicy,
    Principal,
    Step,
)

orch = Orchestrator(policy=PermissionPolicy({"user": ["search:read"]}))


@orch.tool(capabilities=Capability("search", priority=10), scopes="search:read")
async def flaky(query: str) -> str:
    raise ConnectionError("connection reset")


@orch.tool(capabilities=Capability("search", priority=5), scopes="search:read")
async def alpha(query: str) -> str:
    return f"alpha: {query}"


@orch.tool(capabilities=Capability("search", priority=1), scopes="search:read")
async def beta(query: str) -> str:
    return f"beta: {query}"


async def main() -> None:
    ana = Principal("ana", roles={"user"})
    report = await orch.run([Step("s", capability="search", args={"query": "q"})], principal=ana)

    assert report.outputs == {"s": "alpha: q"}
    assert report.served_by == {"s": ("alpha",)}
    assert report.chains == {"s": ("flaky", "alpha")}
    assert [(a.tool, a.outcome, a.error) for a in report.attempts["s"]] == [
        ("flaky", "error", "ConnectionError: connection reset"),
        ("alpha", "ok", None),
    ]
    trace = [f"{e.kind}({e.tool})" if e.tool else e.kind for e in report.events]
    assert trace == ["started", "routed(flaky)", "fallback(flaky)", "routed(alpha)", "succeeded"]
    fallback = report.events[2]
    assert fallback.detail["next"] == "alpha" and fallback.error.startswith("ConnectionError")
    assert orch.router.stats("flaky").failures == 1          # health feedback
    assert orch.router.stats("alpha").successes == 1

    # With max_fallbacks=0 the first failure is final.
    strict = Orchestrator(registry=orch.registry, policy=orch.policy, max_fallbacks=0)
    report = await strict.run([Step("s", capability="search", args={"query": "q"})], principal=ana)
    error = report["s"].exception
    assert isinstance(error, FallbackExhaustedError)
    assert (error.step_id, error.capability, [a.tool for a in error.attempts]) == (
        "s", "search", ["flaky"],
    )
    assert isinstance(error.__cause__, ConnectionError)
    assert str(error) == (
        "step 's': every tool tried for capability 'search' failed: "
        "flaky: ConnectionError: connection reset"
    )


asyncio.run(main())
```

### `tool_timeout` and step timeouts

There are two different timeouts:

- `tool_timeout` limits **each tool call**. A tool that exceeds it gets the
  attempt outcome `"timeout"` and counts as a router failure. A capability step
  then falls back to the next candidate; a named-tool step fails the attempt
  with `ToolTimeoutError`.
- The step timeout (`Step.timeout` or `default_timeout`) limits **the whole
  attempt**, fallbacks included. When it expires, the step fails with
  `ToolTimeoutError`, and the tool that was interrupted is recorded as
  `"cancelled"`. A cancelled tool is not reported to the router.

```python
import asyncio

from orchestrator import Capability, Orchestrator, Principal, Step, ToolTimeoutError

orch = Orchestrator(tool_timeout=0.05)


@orch.tool(capabilities=Capability("quote", priority=9))
async def slow_quote() -> float:
    await asyncio.sleep(1)
    return 1.0


@orch.tool(capabilities=Capability("quote", priority=1))
async def quick_quote() -> float:
    return 2.0


async def main() -> None:
    anyone = Principal("anyone")
    report = await orch.run([Step("q", capability="quote")], principal=anyone)
    assert report.outputs == {"q": 2.0}
    assert [(a.tool, a.outcome) for a in report.attempts["q"]] == [
        ("slow_quote", "timeout"), ("quick_quote", "ok"),
    ]
    assert orch.router.stats("slow_quote").failures == 1

    # A step timeout shorter than tool_timeout cancels the attempt instead.
    no_limit = Orchestrator(registry=orch.registry)
    report = await no_limit.run([Step("q", capability="quote", timeout=0.05)], principal=anyone)
    assert isinstance(report["q"].exception, ToolTimeoutError)
    assert [(a.tool, a.outcome) for a in report.attempts["q"]] == [("slow_quote", "cancelled")]


asyncio.run(main())
```

### Fan-out reconciliation

Pass `fanout={step_id: FanOut(n=3, strategy=None, options={})}` to `run()`. An
int `n` is accepted as shorthand for `FanOut(n=n)`. The top `n` permitted
candidates for that capability step are invoked concurrently, and their results
are reconciled by the orchestrator's `ConflictResolver`.

- A tool that fails is left out of the reconciliation, and the failure is
  reported to the router. A member found unavailable right before invocation
  (disabled, unregistered or replaced after the members were chosen) counts as
  a failed member too, but is not reported to the router. If every member
  fails, the step fails with `ConflictError` (`no successful results to
  reconcile`).
- The step's output is the reconciled value. Its result has
  `tool="fanout:<capability>"` and `metadata["resolution"]`.
- `report.resolutions[step_id]` holds the `Resolution`, and
  `report.served_by[step_id]` lists the winners.
- Fan-out on an unknown step or on a named-tool step, or with an invalid
  setting, raises `ValidationError` before anything runs.

```python
import asyncio

from orchestrator import (
    Capability,
    ConflictError,
    FanOut,
    Orchestrator,
    Principal,
    Step,
    ValidationError,
)

orch = Orchestrator()
ANSWERS = {"north": "Paris", "south": "Paris", "east": "Lyon", "west": None}


def oracle(name: str, priority: int):
    async def handler(question: str) -> str:
        if ANSWERS[name] is None:
            raise RuntimeError("offline")
        return ANSWERS[name]
    handler.__name__ = name
    orch.tool(handler, capabilities=Capability("capital", priority=priority))


for i, name in enumerate(["north", "south", "east", "west"]):
    oracle(name, priority=10 - i)


async def main() -> None:
    who = Principal("who")
    plan = [Step("ask", capability="capital", args={"question": "capital of France?"})]

    report = await orch.run(plan, principal=who, fanout={"ask": FanOut(n=4, strategy="majority")})
    res = report.resolutions["ask"]
    assert report.outputs == {"ask": "Paris"}
    assert (res.winners, res.dissenters, round(res.confidence, 3)) == (
        ("north", "south"), ("east",), 0.667,
    )
    assert report.served_by == {"ask": ("north", "south")}
    assert report["ask"].result.tool == "fanout:capital"
    assert report["ask"].result.metadata["resolution"] is res
    assert sorted((a.tool, a.outcome) for a in report.attempts["ask"]) == [
        ("east", "ok"), ("north", "ok"), ("south", "ok"), ("west", "error"),
    ]
    assert [e.kind for e in report.events if e.tool is None] == ["started", "resolved", "succeeded"]

    # Shorthand n, and options passed to the strategy.
    report = await orch.run(plan, principal=who, fanout={"ask": 2})
    assert report.resolutions["ask"].winners == ("north", "south")
    report = await orch.run(plan, principal=who,
                            fanout={"ask": FanOut(n=3, options={"quorum": 1.0})})
    assert isinstance(report["ask"].exception, ConflictError)    # 2/3 do not agree

    try:
        await orch.run(plan, principal=who, fanout={"nope": 2})
    except ValidationError as exc:
        assert str(exc) == "fan-out requested for unknown step 'nope'"
    else:
        raise AssertionError("expected ValidationError")


asyncio.run(main())
```

### One-shot `call` and `ask`

`call(target, args=None, *, principal, version=None, timeout=None)` runs a
single step and returns its `ToolResult`. The target is either a tool name or
`"capability:<name>"` (a `version` pin is only allowed with a tool name). On
failure, `call` re-raises the original exception; for a result with `ok=False`
it raises `ExecutionError`. `ask(capability,
args=None, *, principal, n=3, strategy=None, options=None)` fans the capability
out to `n` tools and returns the `Resolution`; it raises the resolver's error
(for example `ConflictError` when no tool succeeded) if reconciliation fails.
Both are thin wrappers around `run()`, using the step ids `"call"` and `"ask"`,
so they also emit events and write audit records.

```python
import asyncio

from orchestrator import AccessDeniedError, Capability, Orchestrator, PermissionPolicy, Principal

orch = Orchestrator(policy=PermissionPolicy({"math": ["calc:use"]}))


@orch.tool(capabilities=["calc"], scopes="calc:use", input_schema={"a": int, "b": int})
async def add(a: int, b: int) -> int:
    return a + b


@orch.tool(name="add.v2", capabilities=Capability("calc", priority=-1), scopes="calc:use")
async def add_slowly(a: int, b: int) -> int:
    return b + a


async def main() -> None:
    mia = Principal("mia", roles={"math"})
    result = await orch.call("capability:calc", {"a": 2, "b": 3}, principal=mia)
    assert (result.value, result.tool, result.version, result.ok) == (5, "add", "1.0.0", True)
    assert (await orch.call("add", {"a": 1, "b": 1}, principal=mia, version="1.0.0")).value == 2

    resolution = await orch.ask("calc", {"a": 20, "b": 22}, principal=mia, n=2)
    assert resolution.value == 42 and resolution.confidence == 1.0
    assert resolution.winners == ("add", "add.v2")

    try:
        await orch.call("add", {"a": 1, "b": 2}, principal=Principal("guest"))
    except AccessDeniedError as exc:
        assert exc.missing == {"calc:use"}
    else:
        raise AssertionError("expected AccessDeniedError")


asyncio.run(main())
```

### Pre-flight checks

`check_plan(plan, principal)` returns a list of `PlanIssue(step_id, kind,
target, message)`. It never invokes a tool and never writes an audit record.
Every `run()` performs the check and stores the result in `report.issues`.
With `run(..., preflight=True)`, any issue raises `PreflightError` before
anything is invoked. A step that is statically denied fails without invoking
its tool and is not retried, and the steps that depend on it are skipped.

| Issue kind | Step type | Meaning |
| --- | --- | --- |
| `unknown_tool` | named tool | no such tool or version |
| `disabled` | named tool | the tool version, or its whole name, is disabled |
| `denied` | named tool | the principal may not use the tool |
| `no_route` | capability | no enabled, permitted tool with a closed or half-open circuit offers it |

```python
import asyncio

from orchestrator import Orchestrator, PermissionPolicy, PreflightError, Principal, Ref, Step

orch = Orchestrator(policy=PermissionPolicy({"viewer": ["docs:read"]}))
invoked = []


@orch.tool(scopes="docs:read")
async def read_doc(name: str) -> str:
    invoked.append("read_doc")
    return f"contents of {name}"


@orch.tool(scopes="docs:write")
async def write_doc(name: str, text: str) -> str:
    invoked.append("write_doc")
    return name


@orch.tool(capabilities="premium", scopes="billing:pay")
async def premium_feature() -> str:
    return "paid"


async def main() -> None:
    vic = Principal("vic", roles={"viewer"})
    plan = [
        Step("read", tool="read_doc", args={"name": "a"}),
        Step("write", tool="write_doc", args={"name": "b", "text": Ref("read")}, retries=3),
        Step("notify", tool="read_doc", args={"name": Ref("write")}),
        Step("extra", capability="premium"),
        Step("ghost", tool="calc", version="9.9.9"),
    ]
    audit_before = len(orch.policy.audit_log)
    issues = orch.check_plan(plan, vic)
    assert [(i.step_id, i.kind, i.target) for i in issues] == [
        ("write", "denied", "write_doc"),
        ("extra", "no_route", "capability:premium"),
        ("ghost", "unknown_tool", "calc@9.9.9"),
    ]
    assert issues[0].message == (
        "principal 'vic' may not use tool 'write_doc': missing scopes: docs:write"
    )
    assert len(orch.policy.audit_log) == audit_before and invoked == []

    try:
        await orch.run(plan, principal=vic, preflight=True)
    except PreflightError as exc:
        assert exc.issues == tuple(issues)
        assert str(exc).startswith("pre-flight check failed: write (denied): ")
    else:
        raise AssertionError("expected PreflightError")
    assert invoked == []

    report = await orch.run(plan, principal=vic)               # report, don't block
    assert report.issues == tuple(issues)
    assert report.succeeded == ["read"]
    assert report.failed == ["write", "extra", "ghost"]
    assert report.skipped == ["notify"]
    assert report["write"].attempts == 1                        # retries suppressed
    assert invoked == ["read_doc"]                              # write_doc never ran
    assert [e.kind for e in report.events if e.step_id == "write"] == [
        "started", "routed", "denied", "failed",
    ]


asyncio.run(main())
```

### Resource claims, contention warnings and run-scoped locks

A tool can declare resource claims in `metadata={"resources": {...}}`. Every
named-tool step that uses the tool inherits them, and the step's own
`resources` take precedence. Steps that claim the same resource with at least
one `write` serialize through the shared lock manager. When neither step depends
on the other, the run reports the pair in `report.contentions` and
`report.warnings`, because they will serialize. Lock owners are scoped to the
run as `<run_id>/<step_id>`, so concurrent runs whose steps share an id never
collide. A malformed `metadata["resources"]` declaration (not a mapping, or a
mode other than `read` or `write`) raises `ValidationError` when a run that
names the tool starts. The claims are computed when the run starts and are
verified again right before the tool is invoked (see
[Invocation-time checks](#invocation-time-checks)).

```python
import asyncio

from orchestrator import Orchestrator, Principal, Step

orch = Orchestrator()
seen = []


@orch.tool(metadata={"resources": {"reports": "write"}})
async def save(path: str) -> str:
    seen.append(orch.lock_manager.holders("reports"))
    await asyncio.sleep(0.01)
    return path


async def main() -> None:
    who = Principal("writer")
    report = await orch.run([
        Step("a", tool="save", args={"path": "a.txt"}),
        Step("b", tool="save", args={"path": "b.txt"}),
        Step("c", tool="save", args={"path": "c.txt"}, depends_on=["a"]),  # ordered after a
    ], principal=who)

    assert report.ok
    assert report.contentions == (("reports", "a", "b"), ("reports", "b", "c"))
    assert report.warnings[0] == (
        "steps 'a' and 'b' have conflicting claims on resource 'reports' and will serialize"
    )
    assert seen == [{"run-1/a": "write"}, {"run-1/b": "write"}, {"run-1/c": "write"}]
    assert orch.lock_manager.holders("reports") == {}


asyncio.run(main())
```

### The run report and the event stream

`RunReport` has the following fields:

| Field | Content |
| --- | --- |
| `run_id` | `"run-1"`, `"run-2"`, ... per orchestrator |
| `execution` | the executor's `ExecutionReport` |
| `served_by` | step id to the tool names that served it (fan-out: the winners); succeeded steps only |
| `attempts` | step id to `ToolAttempt(tool, version, outcome, error, duration, attempt)` tuples |
| `resolutions` | step id to `Resolution`, for fan-out steps |
| `audit` | the `AuditRecord`s written by this run (not by other runs or by `check_plan`) |
| `issues` | the pre-flight `PlanIssue`s |
| `contentions` | `(resource, step_a, step_b)` pairs of independent steps whose claims conflict |
| `events` | every `OrchestratorEvent` of the run, in order |

It also provides `chains` (step id to the names of the tools tried) and
`warnings`, and it delegates `ok`, `outputs`, `results`, `succeeded`, `failed`,
`skipped`, `cancelled`, `duration`, `report[step_id]` and `raise_for_failure()`
to `execution`. Its `attempt` field is the executor attempt number (1 for the
first try, 2 for the first retry). A `ToolAttempt.outcome` is one of:

| Outcome | Meaning |
| --- | --- |
| `ok` | the tool returned a successful result |
| `error` | the tool raised or returned `ok=False` |
| `timeout` | the tool exceeded `tool_timeout` |
| `invalid_input` | the args failed the tool's input schema; not invoked |
| `denied` | the principal may not use the tool; not invoked |
| `cancelled` | a step timeout, fail-fast or run cancellation interrupted the tool; not reported to the router |
| `unavailable` | the tool was no longer registered, was replaced or was disabled right before invocation; not invoked and not reported to the router |
| `claims_changed` | a named-tool step's tool needs resource claims (merged with the step's own) that the locks the step holds do not cover, for example because the tool was replaced mid-run with a new write claim; not invoked, and the step fails with `ExecutionError` |

| `OrchestratorEvent.kind` | Source | `tool` | Detail |
| --- | --- | --- | --- |
| `started`, `succeeded`, `failed`, `retrying`, `skipped`, `cancelled` | executor | `None` | `attempt`, `error` |
| `routed` | orchestrator | the chosen tool | `version`, plus `capability` (and `fanout`) for routed steps |
| `fallback` | orchestrator | the tool that failed | `error`; `detail["capability"]`, and `detail["next"]` = the next candidate whose input schema accepts the args |
| `denied` | orchestrator | the denied tool | `error`, `detail["version"]` |
| `resolved` | orchestrator | `None` | `capability`, `strategy`, `winners`, `dissenters`, `confidence` |

```python
import asyncio

from orchestrator import Orchestrator, OrchestratorEvent, Principal, Step

received: list[OrchestratorEvent] = []


def listener(event: OrchestratorEvent) -> None:
    received.append(event)
    raise RuntimeError("listener bugs are logged and swallowed")


orch = Orchestrator(on_event=listener)


@orch.tool
async def hello(name: str) -> str:
    return f"hello {name}"


async def main() -> None:
    report = await orch.run([Step("greet", tool="hello", args={"name": "ada"})],
                            principal=Principal("p"))
    assert report.ok and report["greet"].value == "hello ada"
    assert received == list(report.events)
    assert [(e.kind, e.tool) for e in report.events] == [
        ("started", None), ("routed", "hello"), ("succeeded", None),
    ]
    attempt = report.attempts["greet"][0]
    assert (attempt.tool, attempt.version, attempt.outcome, attempt.attempt) == (
        "hello", "1.0.0", "ok", 1,
    )
    assert attempt.ok and report.duration >= 0


asyncio.run(main())
```

### Dynamic registry

The orchestrator reads the registry on every request, so registering,
disabling, enabling or unregistering a tool takes effect on the next routing
decision, pre-flight check or invocation, even in the middle of a run.
Unregistering a tool also clears its router health. `orch.close()` stops that
subscription, and calling it twice is safe.

```python
import asyncio

from orchestrator import Capability, Orchestrator, Principal, Step

orch = Orchestrator()


@orch.tool(capabilities=Capability("ocr", priority=1))
async def tesseract(image: str) -> str:
    return "text (tesseract)"


async def main() -> None:
    who = Principal("p")
    step = [Step("read", capability="ocr", args={"image": "scan.png"})]
    assert (await orch.run(step, principal=who)).served_by == {"read": ("tesseract",)}

    @orch.tool(capabilities=Capability("ocr", priority=5))   # registered mid-life
    async def cloud_ocr(image: str) -> str:
        return "text (cloud)"

    assert (await orch.run(step, principal=who)).served_by == {"read": ("cloud_ocr",)}

    orch.registry.disable("cloud_ocr")
    assert (await orch.run(step, principal=who)).served_by == {"read": ("tesseract",)}
    orch.registry.enable("cloud_ocr")

    assert orch.router.stats("cloud_ocr").successes == 1
    orch.registry.unregister("cloud_ocr")
    assert orch.router.stats("cloud_ocr").calls == 0           # health forgotten
    issues = orch.check_plan([Step("x", tool="cloud_ocr")], who)
    assert [i.kind for i in issues] == ["unknown_tool"]

    orch.close()
    orch.close()                                                # idempotent


asyncio.run(main())
```

### Invocation-time checks

Routing, pre-flight and claim merging happen before a tool runs, and the
registry can change in between: an event listener, another task or an earlier
step may disable, unregister or replace a tool. So right before **every** tool
invocation (a named-tool step, each capability candidate including fallbacks,
and each fan-out member) the orchestrator re-checks that the exact descriptor it
is about to call is still registered under its name and version, and is
enabled. A tool that fails the check is never invoked, is not reported to the
router, and is recorded with the attempt outcome `unavailable`:

- a **named-tool step** fails that attempt with `ExecutionError`;
- a **capability step** skips the candidate and moves to the next one, without
  using up `max_fallbacks` and without a `fallback` event. If no candidate is
  left and none ran, the step fails with `NoRouteError`;
- a **fan-out member** counts as a failed member, and the step is reconciled
  from the other members' results.

A named-tool step also re-verifies its **resource claims**. They were computed
when the run started (the tool's `metadata["resources"]` merged with the step's
own `resources`) and locked before the attempt began. If the tool has since been
replaced, or a newer version registered, with claims that the held locks do not
cover (a held `write` covers a `read`), the tool is not invoked: the attempt
fails with `ExecutionError` and is recorded as `claims_changed`. The step is not
re-locked mid-run; a new run picks up the new claims.

A handler that is already running when its tool is disabled or unregistered
keeps running; the checks only stop invocations that have not started yet.

```python
import asyncio

from orchestrator import Capability, Orchestrator, Principal, Step, ToolSpec


def switch_off_alpha(event) -> None:
    if event.kind == "routed" and event.tool == "alpha":
        orch.registry.disable("alpha")          # between routing and invocation


orch = Orchestrator(max_fallbacks=0, on_event=switch_off_alpha)


@orch.tool(capabilities=Capability("search", priority=9))
async def alpha(query: str) -> str:
    return "alpha"


@orch.tool(capabilities=Capability("search", priority=1))
async def beta(query: str) -> str:
    return "beta"


async def calc_v1(a: int) -> int:
    return a


async def calc_v2(a: int) -> int:
    return 2 * a


for version, handler in (("1.0.0", calc_v1), ("2.0.0", calc_v2)):
    orch.register(ToolSpec(name="calc", version=version, capabilities=("calc",), handler=handler))


@orch.tool
async def report() -> str:
    return "old report"


async def new_report() -> str:
    return "new report"


@orch.tool
async def upgrade() -> str:
    """Replace `report` with a version that writes to the database."""
    orch.register(ToolSpec(name="report", version="1.0.0", capabilities=("report",),
                           handler=new_report, metadata={"resources": {"db": "write"}}),
                  replace=True)
    return "upgraded"


async def main() -> None:
    who = Principal("p")

    # alpha is disabled after it was routed: skipped without using the fallback budget.
    run = await orch.run([Step("s", capability="search", args={"query": "q"})], principal=who)
    assert run.outputs == {"s": "beta"}
    assert [(a.tool, a.outcome) for a in run.attempts["s"]] == [
        ("alpha", "unavailable"), ("beta", "ok"),
    ]
    assert "fallback" not in [e.kind for e in run.events]
    assert orch.router.stats("alpha").calls == 0              # not reported to the router
    orch.registry.enable("alpha")

    # A pinned older version of a name-disabled tool does not run.
    orch.registry.disable("calc")
    run = await orch.run([Step("c", tool="calc", version="1.0.0", args={"a": 1})], principal=who)
    assert run["c"].error == "ExecutionError: tool calc@1.0.0 is disabled"
    assert [i.kind for i in run.issues] == ["disabled"]

    # report is replaced mid-run with a write claim on "db" that step "r" never locked.
    run = await orch.run([Step("u", tool="upgrade"), Step("r", tool="report", depends_on=["u"])],
                         principal=who)
    assert [(a.tool, a.outcome) for a in run.attempts["r"]] == [("report", "claims_changed")]
    assert run["r"].error.startswith("ExecutionError: tool report@1.0.0 needs resource claims")

    # The next run locks the new claims up front and invokes the new descriptor.
    run = await orch.run([Step("r", tool="report")], principal=who)
    assert run.outputs == {"r": "new report"}


asyncio.run(main())
```

## Running the demo

`python3 -m orchestrator` runs a deterministic demo that needs no network. It
uses the example tools in `src/orchestrator/examples.py` (four `search` tools,
one of which fails exactly once, plus `summarize`, `calculator`, two `profile`
tools whose answers can be merged, and an `fs.write` tool that claims the
`reports` resource) and walks through four scenarios. It exits with status 0
only if every feature it demonstrates worked.

```bash
pip install -e .            # or: export PYTHONPATH=src
python3 -m orchestrator
```

A trimmed sample of the real output (`tests/test_readme.py` checks that every
line shown here appears, in this order, in the real output):

```text
Multi-tool orchestrator demo (orchestrator 0.1.0)
=================================================

Tools
-----
  tool               capabilities  scopes                       claims
  -----------------  ------------  ---------------------------  -------------
  calculator         math, calc    -                            -
  fs.write           fs.write      fs:write                     reports:write
  ...
  search.flaky       search        search:read                  -
  search.premium     search        search:premium, search:read  -
  summarize          summarize     docs:read                    -
  roles: analyst = docs:read, profile:read, search:*; admin = *

1. Plan for analyst 'ana' (run-1)
---------------------------------
  levels: search, profile, stats | summary | save | announce
  pre-flight: save (denied): principal 'ana' may not use tool 'fs.write': missing scopes: fs:write
  step      status     served by                            output / error
  --------  ---------  -----------------------------------  ----------------------------------------------------------
  search    succeeded  search.premium                       "An orchestrator routes each request to the best perm ..."
  profile   succeeded  profile.directory, profile.contacts  {'name': 'Ada Lovelace', 'team': 'analytics', 'email': ...
  stats     succeeded  calculator                           42
  summary   succeeded  summarize                            "An orchestrator routes each request to the best ..."
  save      failed     -                                    AccessDeniedError: principal 'ana' may not use tool 'fs...
  announce  skipped    -                                    skipped: dependency save failed
  parallel: search, profile, stats started before any step finished
  refs:     summary <- Ref('search'), save <- Ref('summary'), announce <- Ref('save', 'path')
  fallback: search: search.flaky failed (ConnectionError: upstream connection reset (simulated)) -> search.premium
  chain:    search = search.flaky -> search.premium
  trace:    search = started -> routed(search.flaky) -> fallback(search.flaky) -> routed(search.premium) -> succeeded
  ...

2. Fan-out: ask 4 search tools, reconcile by majority (run-2)
-------------------------------------------------------------
  answer:     "An orchestrator routes each request to the best permitted tool, runs independent steps in para ..."
  winners:    search.flaky, search.basic, search.premium
  dissenters: search.contrarian
  confidence: 0.75

3. Delegation: Principal.narrow() changes what routing may pick
---------------------------------------------------------------
  ...
  principal          effective scopes                   search routes to  usable tools
  -----------------  ---------------------------------  ----------------  ------------
  lead               docs:read, profile:read, search:*  search.premium    7
  lead/helper        docs:read, search:read             search.basic      4
  lead/helper/scout  search:read                        search.basic      3
  ...

4. Contention: policy.delegate(root) -> root/report-bot saves two reports (run-4)
---------------------------------------------------------------------------------
  warning: steps 'save_md' and 'save_txt' have conflicting claims on resource 'reports' and will serialize
  ...
  max concurrent writers on 'reports': 1

Summary
-------
  feature                result
  ---------------------  ------
  parallel steps         ok
  output references      ok
  routing with fallback  ok
  permission denial      ok
  skip propagation       ok
  fan-out merge          ok
  fan-out majority       ok
  delegation             ok
  contention warning     ok
  16 audited decisions across 4 runs; 9/9 features demonstrated
```

You can use the example tools in your own experiments:

```python
import asyncio

from orchestrator import Principal, Step
from orchestrator.examples import SEARCH_ANSWER, build_orchestrator, run_demo

orch = build_orchestrator()
ana = Principal("ana", roles={"analyst"})
report = asyncio.run(orch.run(
    [Step("s", capability="search", args={"query": "what is an orchestrator?"})], principal=ana,
))
assert report.outputs == {"s": SEARCH_ANSWER}
assert report.chains == {"s": ("search.flaky", "search.premium")}   # the flaky tool fails once

text = asyncio.run(run_demo())
assert "9/9 features demonstrated" in text
```

## Error reference

Every error derives from `OrchestratorError`, and all of them can be imported
from the package root.

| Error | Base classes | Raised when |
| --- | --- | --- |
| `OrchestratorError` | `Exception` | base class of all library errors |
| `ValidationError` | `OrchestratorError`, `ValueError` | a malformed descriptor, version, scope, principal, step, plan, `Ref` path, tool input, resource claim, strategy, option or setting |
| `CycleError` | `ValidationError` | a `Plan`'s dependency graph contains a cycle |
| `ToolNotFoundError` | `OrchestratorError`, `LookupError` | `get`, `versions`, `is_enabled`, `unregister`, `enable` or `disable` of an unknown name or version; a named step whose tool does not exist |
| `DuplicateToolError` | `OrchestratorError` | registering an existing `name@version` without `replace=True` |
| `PermissionDeniedError` | `OrchestratorError` | base class for permission failures |
| `AccessDeniedError` | `PermissionDeniedError` | `policy.check` denies access (and so does a step's authorization), or `narrow` or `delegate` asks for scopes beyond the delegator's authority; carries `principal_id`, `tool`, `version`, `missing` and `reason` |
| `NoRouteError` | `OrchestratorError` | no enabled, eligible tool with a closed or half-open circuit offers the capability; a capability step with no permitted candidate, or whose every candidate was unavailable at invocation |
| `ExecutionError` | `OrchestratorError` | a disabled tool is invoked (including a pinned version of a name-disabled tool); a named step's tool is unavailable at invocation or its claims changed; `raise_for_failure()` on a failed run; `call` gets `ok=False`; `default_invoker` gets a capability step; an invoker returns something other than a `ToolResult` |
| `ToolTimeoutError` | `ExecutionError`, `TimeoutError` | a step attempt exceeds its timeout, or a tool exceeds `tool_timeout` |
| `FallbackExhaustedError` | `ExecutionError` | every tool tried for a capability step failed, or `max_fallbacks` was exceeded; carries `step_id`, `capability` and `attempts` |
| `ConflictError` | `OrchestratorError` | there is nothing successful to reconcile, the majority quorum is not met, a merge conflicts under `on_conflict="error"` (or gets unequal non-dict values), or `numeric` gets non-numbers, booleans, `inf`, `-inf` or NaN, or its aggregate or spread overflows; a lock is requested re-entrantly, times out or is entered twice |
| `PreflightError` | `OrchestratorError` | `run(..., preflight=True)` finds issues; carries `issues` |

```python
from orchestrator import (
    AccessDeniedError,
    ConflictError,
    CycleError,
    ExecutionError,
    FallbackExhaustedError,
    NoRouteError,
    OrchestratorError,
    PermissionDeniedError,
    PreflightError,
    ToolNotFoundError,
    ToolTimeoutError,
    ValidationError,
)

assert issubclass(CycleError, ValidationError) and issubclass(ValidationError, ValueError)
assert issubclass(ToolNotFoundError, LookupError)
assert issubclass(AccessDeniedError, PermissionDeniedError)
assert issubclass(ToolTimeoutError, ExecutionError) and issubclass(ToolTimeoutError, TimeoutError)
assert issubclass(FallbackExhaustedError, ExecutionError)
for error in (ConflictError, NoRouteError, PreflightError, PermissionDeniedError, ExecutionError):
    assert issubclass(error, OrchestratorError)
```

## Project layout

```text
.
├── .claude/                       OpenSpec slash commands (commands/opsx) and skills for Claude Code
├── .github/workflows/ci.yml       CI: tests on Python 3.11-3.13, ruff, OpenSpec validation
├── LICENSE                        MIT license
├── README.md                      this guide (its Python blocks are tested)
├── pyproject.toml                 package metadata, MIT license, ruff and pytest settings; no runtime dependencies
├── openspec/
│   ├── config.yaml                OpenSpec project context (schema: spec-driven)
│   ├── specs/<capability>/spec.md the current, archived specifications
│   └── changes/
│       └── archive/                  completed changes, one dated folder each
├── src/orchestrator/
│   ├── __init__.py                public API re-exports and __version__
│   ├── __main__.py                python3 -m orchestrator
│   ├── core.py                    data model and error hierarchy
│   ├── registry.py                ToolRegistry
│   ├── router.py                  CapabilityRouter
│   ├── permissions.py             scopes, Principal, PermissionPolicy
│   ├── executor.py                Step, Plan, Ref, ParallelExecutor
│   ├── conflicts.py               ConflictResolver, ResourceLockManager
│   ├── agent.py                   Orchestrator
│   └── examples.py                deterministic example tools and the demo
└── tests/
    ├── conftest.py
    ├── test_core.py  test_registry.py  test_router.py  test_permissions.py
    ├── test_executor.py  test_conflicts.py  test_agent.py
    ├── test_demo.py  test_integration.py
    └── test_readme.py             runs every python block of this README
```

## Spec-driven development with OpenSpec

The library was built spec-first with [OpenSpec](https://github.com/Fission-AI/OpenSpec)
(CLI 1.14, schema `spec-driven`). No code was written for a capability until an
OpenSpec change had described it. Each capability went through the same cycle,
driven by the OpenSpec slash commands for Claude Code that ship in
[.claude/commands/opsx](.claude/commands/opsx) (`/opsx:propose`, `/opsx:apply`,
`/opsx:archive`, plus `explore`, `sync` and `update`) and the matching skills in
[.claude/skills](.claude/skills):

1. **Propose.** `openspec/changes/<change>/` gets a `proposal.md` (why and
   what), a delta spec `specs/<capability>/spec.md` (requirements written with
   SHALL and MUST, each with `#### Scenario:` blocks), a `design.md` (decisions,
   rejected alternatives and risks) and a `tasks.md` (a checklist, where every
   task names the scenarios it verifies).
2. **Apply.** The tasks are implemented in order, each with tests for the
   scenarios it names, and ticked off in `tasks.md`.
3. **Archive.** Once the change validates and every task is done,
   `openspec archive` merges its delta spec into `openspec/specs/` and moves the
   change to `openspec/changes/archive/<date>-<change>/`.

```bash
openspec list                          # active changes and task progress
openspec list --specs                  # current specs and requirement counts
openspec show <change>                 # read a change
openspec validate <change> --strict    # check a change before applying or archiving
openspec archive <change> -y           # merge its specs and archive it
openspec validate <spec> --type spec --strict
```

**Specifications**

| Capability | Requirements | Spec | Code |
| --- | --- | --- | --- |
| Tool registry | 8 | [openspec/specs/tool-registry/spec.md](openspec/specs/tool-registry/spec.md) | `core.py`, `registry.py` |
| Capability routing | 17 | [openspec/specs/capability-routing/spec.md](openspec/specs/capability-routing/spec.md) | `router.py` |
| Permission scoping | 11 | [openspec/specs/permission-scoping/spec.md](openspec/specs/permission-scoping/spec.md) | `permissions.py` |
| Parallel execution | 14 | [openspec/specs/parallel-execution/spec.md](openspec/specs/parallel-execution/spec.md) | `executor.py` |
| Conflict resolution | 23 | [openspec/specs/conflict-resolution/spec.md](openspec/specs/conflict-resolution/spec.md) | `conflicts.py` |
| Orchestrator agent | 24 | [openspec/specs/orchestrator-agent/spec.md](openspec/specs/orchestrator-agent/spec.md) | `agent.py`, `examples.py`, `__main__.py` |

The counts come from `openspec list --specs`, and `tests/test_readme.py` checks
every count against the spec files, so the table cannot silently drift. Every
spec passes `openspec validate --specs --strict` (CI enforces this, together
with `openspec validate --all --strict`), and once a change is archived,
`openspec list` reports no active changes.

**Changes**

| Change | Status | Introduced |
| --- | --- | --- |
| [add-tool-registry](openspec/changes/archive/2026-10-17-add-tool-registry) | archived 2026-10-17 | core model, `ToolRegistry` |
| [add-capability-routing](openspec/changes/archive/2026-10-17-add-capability-routing) | archived 2026-10-17 | `CapabilityRouter`, circuit breaker |
| [add-permission-scoping](openspec/changes/archive/2026-10-17-add-permission-scoping) | archived 2026-10-17 | scopes, principals, policy, audit |
| [add-conflict-resolution](openspec/changes/archive/2026-10-17-add-conflict-resolution) | archived 2026-10-17 | `ConflictResolver`, `ResourceLockManager` |
| [add-parallel-execution](openspec/changes/archive/2026-10-17-add-parallel-execution) | archived 2026-10-17 | `Plan`, `Step`, `Ref`, `ParallelExecutor` |
| [add-orchestrator-agent](openspec/changes/archive/2026-10-19-add-orchestrator-agent) | archived 2026-10-19 | `Orchestrator`, package API, demo, this README |
| [clarify-edge-cases](openspec/changes/archive/2026-10-19-clarify-edge-cases) | archived 2026-10-19 | edge-case wording and scenarios: numeric strategy rejects booleans and non-finite values (an overflowing `median` or spread is now a `ConflictError`), type-aware majority equality and tie-break, infinite cost/latency ranked last, NaN hints and trailing-newline names rejected |
| [lift-design-limits](openspec/changes/archive/2026-10-19-lift-design-limits) | archived 2026-10-19 | name-level `disable(name)` covering every version (including later ones); invocation-time availability (`unavailable`) and resource-claim (`claims_changed`) checks in the orchestrator |

## Testing

```bash
python3 -m pytest -q                        # everything
python3 -m pytest -q tests/test_readme.py   # only the README examples
python3 -m pytest -q -k fallback            # by keyword
```

The tests exercise the requirement scenarios of every spec in `openspec/specs/`
(each change's `tasks.md` names the scenarios its tests verify). They are
deterministic and need no network. The async tests use `pytest-asyncio` in
auto mode, and concurrency is checked with events, counters and controllable
clocks rather than timing assumptions. Run `python3 -m pytest -q` to see the
current number of tests; this README deliberately does not quote it, so it
cannot go stale.

| Test module | Covers |
| --- | --- |
| `test_core.py` | descriptor and capability validation (including NaN, boolean and infinite hints, and trailing-newline names and versions), version parsing, input validation |
| `test_registry.py` | registration, duplicates and replacement, versions, version-level and name-level enable/disable (including versions registered later), events, listener isolation |
| `test_router.py` | the four strategies, the balanced formula, infinite hints, custom strategies, filters, health, circuit breaker states, `route_many`, `explain`, concurrent recording |
| `test_permissions.py` | the scope grammar and wildcards, roles, rules and decision order, delegation, the audit log |
| `test_executor.py` | DAG validation, scheduling, the concurrency limit, timeouts, retries and backoff, skips, fail-fast, events, locks |
| `test_conflicts.py` | every resolution strategy, type-aware equality and tie-breaks, numeric edge cases (non-finite values, overflow, huge integers), custom strategies, lock semantics (FIFO, readers/writer, timeouts, cancellation) |
| `test_agent.py` | every orchestrator-agent requirement and scenario, including the invocation-time availability and claim checks, and the package interface |
| `test_demo.py` | the example tools, `collect_demo`/`run_demo`, `python3 -m orchestrator` in a subprocess |
| `test_integration.py` | end-to-end runs through the package root only: fallback, denial, fan-out, contention, delegation, health across runs, registry changes, concurrent runs |
| `test_readme.py` | every python block in this README (one test each), per-section coverage, relative links, table-of-contents anchors, the spec index and its requirement counts, the demo sample output, and the absence of hard-coded test counts |

### Continuous integration

The `CI` workflow ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs on
every push to `main` and on every pull request. It has read-only permissions
(`contents: read`), and a newer run on the same branch cancels the older one.
It has three jobs:

| Job | Runs on | Steps |
| --- | --- | --- |
| `test` | Python 3.11, 3.12 and 3.13 (matrix) | `pip install -e '.[dev]'`, `python -m pytest -q`, then a smoke run of the demo with `python -m orchestrator` |
| `lint` | Python 3.13 | `ruff check src tests` |
| `specs` | Node 22 with the OpenSpec CLI (`@fission-ai/openspec@1.14.3`, `OPENSPEC_TELEMETRY=0`) | `openspec validate --specs --strict` and `openspec validate --all --strict` |

Ruff is configured in `[tool.ruff]` in `pyproject.toml`: target `py311`, line
length 100, and the rule groups `E`, `F`, `W`, `I`, `UP`, `B`, `SIM` and `RUF`.
`RUF001` (ambiguous characters) is turned off for `tests/**`, which use
look-alike characters on purpose, and the `∪` sign is allowed everywhere.
`ruff format` is not enforced yet.

## Design decisions and limitations

Each change's `design.md` records its decisions in full. The ones that matter
most when you use the library are these:

- **Standard library only, asyncio-native.** Handlers must be `async def`. A
  timeout or cancellation can only interrupt a handler at an `await`, so a
  CPU-bound handler cannot be interrupted.
- **One composer.** Modules never import each other except `core` and
  `registry`. Only `agent.py` wires them together, so each module can be used,
  and tested, on its own.
- **Executor per run, shared learning.** Every `run()` builds a fresh
  `ParallelExecutor` and invoker. Router health, the audit log and the lock
  manager are shared across runs, so a run benefits from what earlier runs
  learned.
- **Routing is permission-aware but quiet.** Candidates are filtered with the
  non-auditing `policy.predicate`. Only the tools that are actually tried get an
  audited `check`, which keeps the bounded audit log meaningful.
- **Balanced scores are relative** to the current candidate set. Use
  `explain()` to see the score used for a request.
- **Half-open lets requests through.** A half-open circuit does not limit
  traffic to a single probe. The first failure reported for the tool reopens the
  circuit.
- **Cancelled tools are not penalised.** A tool cut off by a *step* timeout is
  recorded as `cancelled` and not reported to the router. Set `tool_timeout` so
  that slow tools count as failures.
- **Retries cover every exception,** including deterministic ones. Retries
  default to 0. Denials that are known at the start of a run are never retried.
  A denial that only arises during the run (for example after a policy change)
  is retried, re-authorized and audited on each retry, and the tool is never
  invoked.
- **Fan-out uses one concurrency slot** for the whole step, even though it
  invokes N tools. `max_concurrency` limits steps, not tool calls.
- **Only named-tool steps inherit tool resource claims,** because the executor
  locks before routing happens. Put `resources` on a capability step yourself
  if it needs locks. A named-tool step's claims are computed when the run
  starts and re-verified right before invocation: if the tool was replaced, or
  a newer version registered, with claims the held locks do not cover, the
  attempt fails with outcome `claims_changed`. There is no mid-step re-locking;
  a new run picks up the new claims.
- **Pre-flight is a snapshot.** If the registry or policy changes after the
  check, the change is still enforced when the tool is invoked.
- **Availability is checked at invocation, not during it.** A tool disabled or
  unregistered mid-run is never invoked afterwards (outcome `unavailable`), but
  a handler that is already running keeps running to completion.
- **Disabling a name is total.** `disable(name)` disables every version,
  including versions pinned by a step and versions registered later, until
  `enable(name)`. Use `disable(name, version)` to switch off a single version.
- **Outputs are shared by reference** between steps, so tools must treat their
  inputs as read-only.
- **Locks are bound to one event loop and are not re-entrant.** A nested hold by
  the same owner fails at once instead of hanging. Acquiring locks in sorted
  order can hold an early resource while waiting for a later one.
- **State lives in memory.** Router health, the audit log and the registry are
  not persisted. `input_schema` is a flat `isinstance` check, not a full
  JSON-Schema validator.
- **Rule subjects share one namespace,** so a role named like a principal id
  matches both. Use a convention such as a `role:` prefix if that matters to
  you.

## Contributing

New behaviour goes through an OpenSpec change before any code is written:

1. **Propose.** Create `openspec/changes/<verb>-<capability>/`, either with
   `/opsx:propose` or by hand. Write `proposal.md`, a delta
   spec under `specs/<capability>/spec.md` (use `## ADDED Requirements`, or
   `MODIFIED`/`REMOVED` when changing an existing capability, and give every
   requirement at least one `#### Scenario:`), a `design.md` and a `tasks.md`.
   Run `openspec validate <change> --strict`.
2. **Apply.** Implement the tasks in order. Put each capability in its own
   module that depends only on `core` and `registry`, and wire it into
   `agent.py` if the orchestrator should use it. Export its public names from
   `__init__.py`, write tests per scenario, and tick each task as you finish it.
   Keep everything deterministic and runnable without a network.
3. **Document.** Add a section with a runnable `python` example to this README.
   `tests/test_readme.py` executes every block, so use `text` or `bash` fences
   for anything that should not run.
4. **Verify.** `openspec validate <change> --strict` must succeed, and so must
   the local checks below.
5. **Archive.** Run `openspec archive <change> -y` (or `/opsx:archive`), then
   `openspec list --specs`, and update the specification table above.

Before you push, run the same checks that CI runs (see
[Continuous integration](#continuous-integration)):

```bash
pip install -e '.[dev]'                 # pytest, pytest-asyncio and ruff
python -m pytest -q                     # the full test suite, README examples included
python -m orchestrator                  # demo smoke run; exits 0 only if every feature worked
ruff check src tests                    # lint
openspec validate --specs --strict      # the main specs
openspec validate --all --strict        # every spec and change
```

The `openspec` commands need the OpenSpec CLI
(`npm install -g @fission-ai/openspec@1.14.3`, Node 22 in CI); set
`OPENSPEC_TELEMETRY=0` to match CI.

## License

This project is licensed under the MIT License; see [LICENSE](LICENSE).
Copyright (c) 2026 imranmali. `pyproject.toml` declares it as a PEP 639 license
expression (`license = "MIT"`, `license-files = ["LICENSE"]`), so the installed
package metadata reports `License-Expression: MIT`.
