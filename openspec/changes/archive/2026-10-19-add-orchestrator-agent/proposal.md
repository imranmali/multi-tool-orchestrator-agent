# Proposal

## Why

The five building blocks (tool registry, capability routing, permission scoping, parallel execution, conflict resolution) exist and are tested in isolation, but nothing composes them: a caller who wants "run this multi-step plan as this principal" must hand-wire a routing-aware, permission-checking invoker, health feedback, fallbacks, fan-out and locking. The library needs the top-level orchestrator agent its project context promises, so the capabilities behave as one system with a single, observable entry point.

## What Changes

- Introduce an orchestrator agent that owns (or is given) a registry, permission policy, router, conflict resolver and resource lock manager, and offers tool registration (directly or by decorating an async function).
- Run a plan of steps on behalf of a principal: named-tool steps are looked up (honouring version pins), authorized with an audited check and invoked; capability steps are routed among the tools the principal may use, with health feedback to the router and automatic fallback to the next-best candidate on failure or per-tool timeout, up to a bounded number of fallbacks.
- Permission denials fail the step with a permission error without invoking the tool, are not retried when known at run start, and propagate as skips to dependent steps.
- Fan-out: a capability step (or a one-shot question) can be served by the top-N permitted tools in parallel, with their answers reconciled by a conflict-resolution strategy; the reconciled value becomes the step output and the resolution is kept for inspection.
- A run report that extends the execution report with, per step, the tool(s) that served it, the chain of attempted tools, fan-out resolutions, the permission audit records produced by the run, and warnings for independent steps whose resource claims conflict and will therefore serialize.
- Static pre-flight check that lists steps that cannot be routed or are not permitted, without invoking or auditing anything; optionally enforced before a run.
- Tool resource claims declared in a tool's metadata are applied to named-tool steps; locking uses a shared resource lock manager.
- Registry changes made while the orchestrator lives (register, unregister, disable, enable) are reflected immediately in routing and pre-flight checks; an unregistered tool's routing health is forgotten.
- Observability: one event stream combining executor step events with orchestrator events (routed, fallback, denied, resolved).
- Package-level exports of every module's public API and a package version; deterministic example tools; a runnable demo (`python3 -m orchestrator`); README documentation of the architecture and each capability.

## Capabilities

### New Capabilities
- `orchestrator-agent`: Composing registry, routing, permissions, parallel execution and conflict resolution into a single agent that runs plans and one-shot calls on behalf of a principal, with fallback, fan-out reconciliation, pre-flight checks, contention warnings and a unified run report and event stream.

### Modified Capabilities

## Impact

- New modules `src/orchestrator/agent.py`, `src/orchestrator/examples.py`, `src/orchestrator/__main__.py`; `src/orchestrator/__init__.py` now exports the public API of every module plus `__version__`.
- New tests `tests/test_agent.py`, `tests/test_integration.py`, `tests/test_readme.py`.
- `README.md` extended with architecture, quickstart, per-capability sections, demo and OpenSpec spec index.
- `agent.py` is the one module allowed to depend on every other module. Existing modules' behavior is unchanged; no new runtime dependencies.
