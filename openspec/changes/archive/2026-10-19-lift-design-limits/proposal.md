# Proposal

## Why

Two documented limits let a run do something the caller has explicitly forbidden. `disable(name)` only disables the latest version, so a step pinning an older version still runs a tool its operator switched off. And an orchestrator run snapshots tool state at its start: a fan-out member disabled mid-run is still invoked, and a named-tool step whose tool was replaced with different resource claims runs under the stale locks.

## What Changes

- tool-registry: `disable(name)` without a version becomes a name-level switch that disables **every** registered version, including versions registered while the name stays disabled, until `enable(name)`. `enable(name)` clears the name-level flag and every per-version flag; `enable(name, version)` clears only that version's own flag, so a name-level disable still wins. `is_enabled(name, version)` reflects both. Each enable/disable call emits exactly one event, naming the given version or, for a whole-name call, the latest version. **BREAKING**: an explicitly pinned older version of a name-disabled tool is now disabled (it was enabled before); `default_invoker` and the orchestrator refuse it.
- orchestrator-agent: right before every tool invocation (named step, each capability fallback candidate, each fan-out member) the orchestrator re-checks that the exact descriptor about to run is still registered and enabled. A tool that is not records a `ToolAttempt` with the new outcome `unavailable` and is never invoked: a named-tool step fails, a capability step moves to the next candidate (without spending its fallback budget or penalising router health), and a fan-out member counts as a failed member.
- orchestrator-agent: a named-tool step verifies, right before invoking, that the resource claims of the descriptor it invokes (merged with the step's own claims) are covered by the locks the step holds. If the tool was replaced or a newer version registered with claims the held locks do not cover, the attempt fails with the new outcome `claims_changed` and the tool is not invoked.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `tool-registry`: Enable and disable tools (version vs name level), a new name-level disablement requirement, and Registry change notifications (one event per call and which version it names).
- `orchestrator-agent`: Named-tool steps (pinned versions of a disabled name), plus new requirements for the per-invocation availability re-check and the invocation-time resource-claim check.

## Impact

- `src/orchestrator/registry.py`: name-level disablement applies to every version; `enable(name)` clears every per-version flag.
- `src/orchestrator/agent.py`: availability re-check before each invocation, claim coverage check for named-tool steps, attempt outcomes `unavailable` and `claims_changed`.
- `src/orchestrator/executor.py`: unchanged; `default_invoker` already asks `is_enabled` for the exact version and so refuses a pinned version of a name-disabled tool.
- Tests: `tests/test_registry.py`, `tests/test_agent.py`.
- README: the registry example asserting that `1.0.0` stays enabled after `disable("web.search")`, the registry reference table, the attempt-outcome list and the two limitation bullets need updating (done separately).
