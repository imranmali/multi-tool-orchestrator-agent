# Proposal

## Why

Real requests to the orchestrator fan out into several tool calls, many of which do not depend on one another (e.g. searching three sources before summarizing). Running them one at a time wastes wall-clock time, and running them naively in parallel ignores data dependencies, timeouts, retries and shared-resource access. The agent needs a dependency-aware executor that runs a plan of tool steps with maximal safe parallelism and reports exactly what happened to each step.

## What Changes

- Introduce an execution plan model: named steps that target either a specific tool (optionally version-pinned) or a capability, carry arguments, declare dependencies, and may reference earlier steps' outputs (including nested fields) as argument placeholders.
- Validate plans up front: duplicate step ids, unknown dependencies, malformed steps and dependency cycles are rejected before anything runs; expose the plan's topological layers and a valid execution order.
- Introduce a parallel executor that starts each step as soon as all of its dependencies have succeeded, bounded by a configurable concurrency limit.
- Per-step timeouts (with an executor-wide default), bounded retries with exponential backoff, and treatment of unsuccessful tool results as retryable failures.
- Failure semantics: dependents of a failed step are skipped; independent branches continue by default, or the whole run is cancelled in fail-fast mode.
- Pluggable hooks so the executor stays independent of routing, permissions and conflict handling: an invoker hook that performs each call (with a default registry-backed invoker), an optional resource-lock manager hook for steps that declare read/write resource claims, and an optional event callback for observability.
- An execution report with per-step status, result, error, attempt count and timings, plus aggregate views (overall success, outputs, failed and skipped steps, total duration) and a way to raise on failure.

## Capabilities

### New Capabilities
- `parallel-execution`: Describing multi-step tool plans with dependencies and output references, validating them, and executing them concurrently with timeouts, retries, failure propagation, optional resource locking and a per-step execution report.

### Modified Capabilities

## Impact

- New module `src/orchestrator/executor.py`; new tests `tests/test_executor.py`.
- Depends only on `orchestrator.core` (models, errors) and `orchestrator.registry` (for the default invoker). Does not import routing, permission or conflict modules; the top-level `Orchestrator` composes them through the invoker and lock-manager hooks.
- No new runtime dependencies (uses `asyncio` from the standard library).
- The package's public exports (`__init__.py`) are left to the integrator.
