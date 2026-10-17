# Design

## Context

See proposal.md for motivation and specs/parallel-execution/spec.md for the behavior contract. The executor lives in `src/orchestrator/executor.py` and may depend only on `orchestrator.core` and `orchestrator.registry`. Routing (capability → tool), permission checks and resource-conflict handling are built in sibling modules in parallel; the top-level `Orchestrator` composes them, so the executor must expose hooks rather than import them. Runtime is stdlib-only, so concurrency is plain `asyncio`.

## Goals / Non-Goals

**Goals:**
- Maximal safe parallelism: a step starts the moment its last dependency succeeds, not when its topological layer is reached.
- Deterministic, observable behavior: every step ends with exactly one outcome; ordering decisions follow plan declaration order so tests and reports are stable.
- Zero coupling to routing/permissions/conflicts — everything flows through an `Invoker` callable and a duck-typed `LockManager`.

**Non-Goals:**
- Dynamic plans (adding steps while running), conditional branches or loops.
- Distributed or multi-process execution; everything runs on one event loop.
- Deadlock detection across resources: atomic multi-resource acquisition is the lock manager's job.
- Deep-copying outputs passed between steps: referenced values are passed by reference and tools must not mutate them.

## Decisions

- **Public model** — `Ref(step, path=None)`, `Step(...)`, `Plan(steps)`, `StepEvent`, `StepOutcome`, `ExecutionReport`, `ParallelExecutor`, `Invoker`, `LockManager`, `default_invoker(registry)`. All value objects are frozen dataclasses (matching `core.py`), so they are safe to share between tasks.
- **Implicit dependencies resolved in `Step.__post_init__`** — args are walked recursively (dicts, lists, tuples) for `Ref`s and their step ids are appended to `depends_on` (deduplicated, declaration order kept). Doing it on the step, not the plan, means `step.depends_on` is always the full truth for every consumer (`Plan.levels()`, the executor, the integrator). Alternative: compute in `Plan` (rejected: two sources of truth).
- **Step validation at construction** — exactly one of `tool`/`capability`; `retries` int ≥ 0; `retry_backoff` ≥ 0; `timeout` `None` or > 0; resource modes in `{"read", "write"}`. Raises `ValidationError` so malformed plans never reach the executor.
- **Plan validation** — unique ids and known dependencies checked first (`ValidationError`), then a white/grey/black DFS that, on hitting a grey node, slices the current DFS path to report the exact cycle (`CycleError: dependency cycle: a -> c -> b -> a`). Self-dependency is a cycle of length one. `levels()` assigns each step `1 + max(level of deps)` (longest-path layering, so steps sit as early as possible) and keeps declaration order within a layer; `order()` is the flattened layers.
- **Scheduler = task-per-step + `asyncio.wait(FIRST_COMPLETED)` loop**, not level barriers. After each completion batch the scheduler, in `plan.order()`: records outcomes, marks steps with any failed/skipped/cancelled dependency as `skipped` (a single pass in topological order makes propagation transitive), then starts every step whose dependencies have all succeeded. Alternative: per-step tasks awaiting dependency futures (rejected: harder to cancel cleanly and to make skip propagation deterministic).
- **Concurrency bound by `asyncio.Semaphore(max_concurrency)` around the invocation only** — Ref resolution, retry backoff sleeps and lock waits do not occupy a slot, so a step sleeping between retries never starves runnable steps.
- **Lock acquisition order: lock first, then semaphore.** For each attempt: `async with lock_manager.hold(step.id, step.resources): async with semaphore: await wait_for(invoker(...))`.
  - Semaphore-then-lock would let up to `max_concurrency - 1` slots sit idle waiting on a contended resource, collapsing throughput (with `max_concurrency=1` a single blocked waiter freezes the whole run only until the holder finishes, but under heavy contention all slots can be parked).
  - Lock-then-semaphore cannot deadlock between the two primitives: every task inside the semaphore already holds all of its locks and is only running the tool (bounded by its timeout), so it always releases its slot; a lock holder waiting for a slot therefore always progresses. The cost is that a lock may be held briefly while waiting for a slot; acceptable since slots free up as tools finish.
  - The lock is held per attempt, not across retries, so backoff sleeps never block other steps' resources. Deadlocks among multiple resources of one step and reader/writer fairness are delegated to the lock manager (it receives the full claim map in one call so it can acquire atomically or in a global order).
  - The timeout covers only the invocation, not lock or slot waiting, so contention is not misreported as a slow tool.
- **Timeouts** — `asyncio.wait_for(invoker(step, args), step.timeout or default_timeout)`; a bare `TimeoutError` becomes `ToolTimeoutError("step 'x' timed out after 0.01s")`. A `ToolTimeoutError` raised by the invoker itself is kept as-is.
- **Retries** — attempt loop of `retries + 1`; before retry *n* (0-based) the executor emits `retrying` and sleeps `retry_backoff * 2**n`. Any `Exception`, a timeout, or a `ToolResult(ok=False)` counts as a failed attempt; a non-`ToolResult` return from the invoker is an `ExecutionError`. All errors are retried uniformly (simple, predictable); callers set `retries=0` for non-idempotent tools. `CancelledError` is never swallowed.
- **Outcomes never raise** — the per-step runner converts every failure into a `StepOutcome(status="failed", error="<ExcType>: <msg>", exception=exc)`, so one bad tool cannot crash the scheduler. A failed `ToolResult` is kept in `result`. Ref resolution errors fail the step with 0 attempts, before the invoker is called.
- **Fail-fast** — on the first failed outcome the scheduler stops starting steps, cancels running tasks, awaits them (`gather(return_exceptions=True)`), and records them plus every not-yet-started, not-skipped step as `cancelled`. If `run()` itself is cancelled, a `finally` block cancels all child tasks so nothing leaks.
- **Events** — `on_event(StepEvent(kind, step_id, attempt, error, at))` called synchronously; exceptions are logged and swallowed (same isolation pattern as registry listeners).
- **Timing** — `time.monotonic()` for `started_at`/`finished_at` and report duration (immune to wall-clock jumps).
- **Default invoker** — `registry.get(step.tool, step.version)`, refuse disabled tools, `spec.validate_input(args)`, `await spec.handler(**args)`, wrap into `ToolResult(ok=True, value, duration)`. Capability-only steps raise `ExecutionError` explaining routing is required; the integrator supplies a routing-aware invoker.

## Risks / Trade-offs

- [Outputs shared by reference between steps] → Documented; tools must treat inputs as read-only. Copying would be costly and could break non-copyable values.
- [All exceptions retried, including deterministic ones like validation errors] → Wastes a few attempts at worst; retries default to 0 and the integrator can wrap the invoker to short-circuit permanent errors.
- [Cancelling a tool that ignores cancellation] → `wait_for` and task cancellation rely on the handler reaching an `await`; CPU-bound handlers cannot be interrupted. Documented as a tool-author constraint.
- [Lock held while waiting for a semaphore slot] → Bounded by the time for any running invocation to finish (itself bounded by timeouts); preferred over idle slots.
- [O(n) scan of the plan per completion batch] → O(n²) worst case; plans are tens of steps, so simplicity wins over an indexed ready-queue.
