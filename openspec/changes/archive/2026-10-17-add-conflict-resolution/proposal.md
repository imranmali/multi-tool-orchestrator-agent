# Proposal

## Why

When the orchestrator fans one request out to several tools, their answers can disagree, and when parallel plan steps touch the same external resource (a file, a record, an index) they can corrupt each other's work. The agent needs a deterministic, explainable way to reconcile divergent tool results and to serialize conflicting resource access before parallel execution can be trusted.

## What Changes

- Introduce result reconciliation: given several tool results for the same request, produce a single resolution recording the chosen value, the strategy used, which tools agreed (winners) and disagreed (dissenters), a confidence score, and strategy-specific details.
- Built-in reconciliation strategies: first success, majority vote (with optional quorum and deterministic priority-based tie-breaking), highest priority, deep merge of structured values (with a configurable policy for conflicting leaves), and numeric aggregation (mean, median, min, max).
- Support caller-supplied custom strategies, both inline and registered by name; unknown strategy names are rejected.
- Introduce resource locking: readers-writer locks per named resource with FIFO fairness, multi-resource acquisition that cannot deadlock, release on error and cancellation, acquisition timeouts, and introspection of current holders and waiters.
- Introduce static detection of conflicting resource claims so plans can warn about steps that will be serialized.

## Capabilities

### New Capabilities
- `conflict-resolution`: Reconciling divergent results from multiple tools answering the same request, and coordinating concurrent access to shared named resources.

### Modified Capabilities

## Impact

- New module `src/orchestrator/conflicts.py` depending only on `orchestrator.core` (`ToolResult`, `ConflictError`, `ValidationError`); it does not import routing, permission or execution modules.
- The execution module can depend on the lock manager through a structural protocol (`hold(owner, resources)` returning an async context manager) without importing this module.
- New tests in `tests/test_conflicts.py`. No new runtime dependencies.
