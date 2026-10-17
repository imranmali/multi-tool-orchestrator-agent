# Design

## Context

See proposal.md - Why. `orchestrator.core` already provides `ToolResult` and the `ConflictError` / `ValidationError` types; the registry is not needed here. Per the project context this module must not import the router, permission or executor modules, and the executor consumes the lock manager only through a structural protocol `hold(owner, resources) -> AsyncContextManager`. Requirements live in specs/conflict-resolution/spec.md.

## Goals / Non-Goals

**Goals:**
- Pure, deterministic result reconciliation: same inputs (including order) always give the same resolution.
- An asyncio lock manager that is deadlock-free for multi-resource requests, fair to writers, and leak-free under cancellation.

**Non-Goals:**
- Re-entrant locks or lock upgrades (read → write) for the same owner; such requests are rejected.
- Cross-thread or cross-process locking: one manager serves one event loop.
- Semantic (fuzzy) equality of results, e.g. treating `"Paris"` and `"paris"` as equal; callers use a custom strategy for that.

## Decisions

- **One module, `conflicts.py`, with two independent classes** (`ConflictResolver`, `ResourceLockManager`) plus a frozen `Resolution` dataclass. Both concerns are "conflict resolution" and share only the error types; splitting into two modules would add an import surface for no gain.
- **Structural equality via a canonical JSON form** — `json.dumps(value, sort_keys=True, default=repr)` gives a hashable key for dicts/lists regardless of key order; when serialisation fails (e.g. mixed-type dict keys that `sort_keys` cannot order) comparison falls back to `==`. Grouping is O(n·g) for n results and g groups, fine for fan-out sizes. Alternative: deep-freezing values into tuples/frozensets (rejected: loses list-vs-tuple distinctions and is more code). Consequence: `1` and `1.0` are different under majority.
- **Strategies as plain functions `fn(results, **options) -> Resolution | Any`**, kept in a per-resolver name → function table seeded with the built-ins. Built-ins validate their own option names so typos fail loudly. `register_strategy(name, fn, *, replace=False)` adds entries; custom callables share the same calling convention and are wrapped when they return a plain value.
- **Winners/dissenters by value equality** — for value-choosing strategies (first_success, majority, priority, custom) winners are the participants whose value equals the chosen value; confidence is `len(winners) / participants`. Merge: dissenters are tools whose value was overridden at some conflicting path. Numeric: every participant contributes, so all are winners and confidence is `1 - (max - min) / max(|max|, |min|)` clamped to [0, 1] (1.0 when unanimous); spread and per-tool values go in `details`.
- **Quorum is inclusive** (`share >= quorum`, quorum in (0, 1]) so `quorum=1.0` means unanimity; a strict majority is expressed as e.g. `0.51`.
- **Merge walks all contributions per path** rather than folding pairwise: at each path it looks at every tool's value, recursing when all are dicts, unioning when all are lists, accepting when all are equal, and otherwise recording one conflict for that path with every tool's value. This makes `error` reports complete (every path, every tool) and `priority`/`first` independent of fold order.
- **Lock state per resource: `holders: dict[owner, mode]` + FIFO `deque` of waiters with futures**. A grant pass pops from the head while the head request is compatible with current holders, so consecutive readers are granted together but a waiting writer blocks later readers (FIFO fairness → no writer starvation). Alternative: strict writer preference (rejected: can starve readers and is no simpler).
- **Sorted acquisition order** — `hold` sorts the requested resource names and acquires them one by one. With per-resource FIFO queues, every wait edge points either to a request on the same resource ahead in the queue or to a holder waiting on a strictly larger resource name, so no cycle can form.
- **Cleanup in one place** — every acquisition is wrapped so that on any exception (timeout, cancellation, re-entrancy error) the waiter is removed from its queue, already-acquired resources are released, and a grant pass runs on each touched resource so later waiters are not stranded. A future that was granted in the same tick as the cancellation is detected (`done()` and not cancelled) and released rather than leaked. Empty per-resource state is dropped to avoid unbounded growth.
- **Timeout uses one deadline for the whole request** via `asyncio.timeout`, converted to `ConflictError`; a timeout of 0 means "only if immediately available".
- **`hold` returns a small async context manager object** (not an `@asynccontextmanager` generator) so argument validation errors surface at call time and the object satisfies the executor's protocol.
- **`detect_write_conflicts` is a static method** (also exported as a module-level function) so plan validation can use it without a manager instance.

## Risks / Trade-offs

- [`default=repr` may treat distinct objects with identical reprs as equal] → Documented; tools are expected to return JSON-like values, and custom strategies cover exotic types.
- [Futures bind the manager to one event loop] → Documented in the class docstring; the orchestrator runs a single loop.
- [Sorted acquisition can hold early resources while waiting for later ones, reducing parallelism] → Accepted for deadlock freedom; `detect_write_conflicts` lets planners spot steps that will serialize.
- [Non-re-entrancy surprises callers that nest holds] → Fails fast with a conflict error naming the owner and resource instead of hanging.
