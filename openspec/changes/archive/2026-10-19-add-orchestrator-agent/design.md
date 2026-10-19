# Design

## Context

See proposal.md (Why) for motivation and specs/orchestrator-agent/spec.md for the behavior contract. The five composed modules were built independently and expose hooks for exactly this integration:

- `executor.ParallelExecutor(invoker, *, max_concurrency, fail_fast, default_timeout, lock_manager, on_event)` delegates every attempt to an `Invoker = (Step, resolved_args) -> Awaitable[ToolResult]`, wraps each invoker call in `asyncio.wait_for(step.timeout or default_timeout)`, retries every exception or `ok=False` result uniformly (no "non-retryable" signal), skips dependents of failed steps, and calls `lock_manager.hold(step.id, step.resources)` around each attempt *before* the invoker runs. `StepOutcome.exception` keeps the original exception.
- `router.CapabilityRouter(registry, ...)` offers `candidates(cap, *, eligible, exclude, strategy)` (ranked, never raises), `route(...)` (raises a descriptive `NoRouteError`), `explain`, `record_success(name, latency)`, `record_failure(name)`, `stats`, `reset(name)`. Health is keyed by tool *name*; tools with an open circuit are not candidates.
- `permissions.PermissionPolicy` offers `decide` / `predicate(principal)` (never audited) and `check(principal, tool)` (audited, raises `AccessDeniedError`, returns nothing). `audit_log` returns a copy of a bounded deque.
- `conflicts.ConflictResolver.resolve(results, strategy=None, **options)` ignores `ok=False` results and raises `ConflictError` when nothing can be reconciled; `ResourceLockManager.hold(owner, resources)` matches the executor's lock protocol but is **not re-entrant per owner**; `detect_write_conflicts({owner: claims})` lists conflicting pairs.
- `ToolSpec` has no resource field but carries free-form `metadata`; handlers are async and return plain values.

Per the project context, `orchestrator/agent.py` is the only module allowed to import all of them. Existing modules keep their behavior and public API unchanged.

## Goals / Non-Goals

**Goals:**
- One per-run invoker that does lookup → validate → authorize → invoke (named tools) or route → validate → authorize → invoke → fall back (capabilities), so the executor's scheduling, retry, timeout, skip and locking semantics apply unchanged.
- Everything that happened in a run is reconstructible from its report: who served each step, what was tried and why it failed, what was reconciled, which permission decisions were made, what was predicted to go wrong (pre-flight, contention).
- Concurrent runs on one orchestrator are isolated (own report, events, audit capture, lock owners) while sharing router health and resource locks.
- Deterministic, network-free examples and demo that double as executable documentation.

**Non-Goals:**
- Changing plans while they run, or re-planning after failures.
- Cross-step fan-out (fan-out reconciles the tools serving one step; it does not merge different steps).
- Locking resources declared by a *routed* tool: the executor acquires locks before the invoker runs, so only claims known before routing (step claims, and claims of a named tool) are lockable.
- Persisting audit logs, health or events; any synchronous (non-async) entry point.

## Decisions

### D1. Public API of `agent.py`

```python
__all__ = ["Orchestrator", "FanOut", "RunReport", "ToolAttempt", "PlanIssue",
           "OrchestratorEvent", "PreflightError", "FallbackExhaustedError"]

class Orchestrator:
    def __init__(self, *, registry=None, policy=None, router=None, resolver=None,
                 lock_manager=None, max_concurrency=8, fail_fast=False,
                 default_timeout=None, tool_timeout=None, max_fallbacks=2,
                 on_event=None): ...
    registry: ToolRegistry; policy: PermissionPolicy; router: CapabilityRouter
    resolver: ConflictResolver; lock_manager: LockManager      # read-only properties
    max_concurrency, fail_fast, default_timeout, tool_timeout, max_fallbacks  # read-only

    def register(self, spec: ToolSpec, *, replace: bool = False) -> ToolSpec
    def tool(self, fn=None, /, *, name=None, version="1.0.0", capabilities=None,
             scopes=(), input_schema=None, description=None, metadata=None,
             replace=False)                       # bare @orch.tool or @orch.tool(...)
    def check_plan(self, plan: Plan | Iterable[Step], principal: Principal) -> list[PlanIssue]
    async def run(self, plan: Plan | Iterable[Step], principal: Principal, *,
                  fanout: Mapping[str, FanOut | int] | None = None,
                  preflight: bool = False) -> RunReport
    async def call(self, target: str, args: Mapping | None = None, *, principal: Principal,
                   version: str | None = None, timeout: float | None = None) -> ToolResult
    async def ask(self, capability: str, args: Mapping | None = None, *, principal: Principal,
                  n: int = 3, strategy=None, options: Mapping | None = None) -> Resolution
    def close(self) -> None          # unsubscribes from the registry; idempotent

@dataclass(frozen=True) class FanOut:  n: int = 3; strategy: str | Callable | None = None; options: Mapping = {}
@dataclass(frozen=True) class ToolAttempt: tool; version; outcome; error: str | None; duration: float; attempt: int
                                     # outcome ∈ ok | error | timeout | invalid_input | denied | cancelled; .ok property
@dataclass(frozen=True) class PlanIssue: step_id; kind; target; message   # kind ∈ unknown_tool | disabled | denied | no_route
@dataclass(frozen=True) class OrchestratorEvent: kind; step_id; tool=None; attempt=0; error=None; detail={}; at=monotonic()
@dataclass(frozen=True) class RunReport: (see D10)
class PreflightError(OrchestratorError): issues: tuple[PlanIssue, ...]
class FallbackExhaustedError(ExecutionError): step_id; capability; attempts: tuple[ToolAttempt, ...]
```

All value objects are frozen dataclasses, consistent with the other modules. `principal` is positional-or-keyword in `run`/`check_plan` and keyword-only in `call`/`ask`, so `principal=` works everywhere (the README always uses it). *Alternative:* an optional default/anonymous principal. Rejected: silently running as "anonymous" hides permission bugs; every entry point names who acts.

### D2. Construction and validation

Missing collaborators get defaults: `ToolRegistry()`, `PermissionPolicy()`, `CapabilityRouter(registry)`, `ConflictResolver()`, `ResourceLockManager()`. Settings are validated eagerly in the constructor (not lazily at the first run) with `ValidationError`: `max_concurrency` int ≥ 1, `default_timeout`/`tool_timeout` `None` or > 0, `max_fallbacks` int ≥ 0; bools are rejected where ints are expected. A supplied router must route over the orchestrator's registry: the constructor compares `getattr(router, "_registry", registry) is registry` and raises `ValidationError` on mismatch, since routing over a different registry would invoke tools the orchestrator does not manage. When only a router is supplied, its registry is adopted. *Alternative:* re-binding the router. Rejected: mutating a caller's router behind their back.

### D3. Executor per run, invoker per run, shared learning

`run()` normalises the plan (`Plan(steps)` for a sequence), validates fan-out, applies preflight/claims (D7, D8), builds a `_RunContext` (run id, principal, fan-out map, per-step invocation counters, attempts, served-by, resolutions, audit capture, events) and a fresh `ParallelExecutor` whose invoker and `on_event` close over it. Router, policy, resolver and lock manager are shared across runs, which is what makes health learning and cross-run locking work. *Alternative:* one long-lived executor with per-run state in context variables. Rejected: the executor is cheap, and per-run construction makes concurrent runs isolated by construction.

### D4. Named-tool pipeline

`registry.get(name, version)` (`ToolNotFoundError`) → `registry.is_enabled` (`ExecutionError` "disabled") → `spec.validate_input(args)` (`ValidationError`) → emit `routed` → `policy.check(principal, spec)` (audited; on `AccessDeniedError` emit `denied`, record a `denied` attempt, re-raise) → `await handler(**args)` under `tool_timeout` (`ToolTimeoutError`) → wrap in `ToolResult(tool, version, ok=True, value, duration)`. If the handler itself returns a `ToolResult`, it is passed through with `tool`/`version`/`duration` normalised via `dataclasses.replace`; that is how a tool reports an unsuccessful result (`ok=False`) without raising. Validation precedes authorization so malformed calls fail without an audit record and are not mistaken for denials; both precede invocation. Completed invocations feed the router (`record_success(name, latency=duration)` / `record_failure(name)`), so named-tool usage also informs routing health. Unknown/disabled/invalid/denied never touch health. *Alternative:* reusing `default_invoker`. Rejected: it cannot authorize, time-box per tool or report health.

### D5. Capability pipeline with fallback

Per executor attempt (each executor retry starts fresh):

```
excluded = []; failures = 0; first_invalid = None
loop:
    cands = router.candidates(cap, eligible=policy.predicate(principal), exclude=excluded)
    if not cands:
        if nothing was invoked and first_invalid: raise first_invalid        # caller error
        if nothing was invoked: router.route(cap, eligible=..., exclude=excluded)  # raises NoRouteError
        raise FallbackExhaustedError(attempts, from last error)
    spec = cands[0]; excluded.append(spec.name)
    validate input  -> on ValidationError: attempt(invalid_input), no health change, continue
    emit routed; policy.check (audited) -> on denial: attempt(denied), emit denied, continue
    invoke under tool_timeout -> success: record_success, return result
    failure (raise / ToolTimeoutError / ok=False): record_failure, attempt(error|timeout)
    failures += 1
    if failures > max_fallbacks: raise FallbackExhaustedError(...)
    peek next candidate; if one exists emit fallback(tool=spec.name, detail={"next": ...})
```

Only invoked failures consume the fallback budget, so `max_fallbacks=1` invokes at most two tools, and `0` disables fallback. Skipping invalid-input candidates without health penalty keeps a caller's bad arguments from opening circuits on healthy tools. Denial at check time cannot normally happen (predicate and check are evaluated synchronously against the same policy), but is handled defensively by skipping. `FallbackExhaustedError` lists every attempt ("flaky: RuntimeError: boom; beta: ToolTimeoutError: …") and is chained to the last error. *Alternative:* one routing decision per executor retry (exclude the last failed tool on retry). Rejected: a single flaky tool would burn all retries and backoff; fallback inside one attempt is faster and keeps retries meaningful for transient whole-capability outages.

### D6. Per-tool timeout separate from step timeout

The executor's `wait_for` wraps the whole invoker call, so a step timeout alone would let a hung first candidate consume the entire budget and never fall back. `tool_timeout` (constructor setting, default `None`) bounds each individual handler call with `asyncio.wait_for` and raises `ToolTimeoutError` (recorded as outcome `timeout`, counted as a router failure). The step/default timeout still bounds the step. If the step timeout cancels an in-flight candidate, that attempt is recorded with outcome `cancelled` and **not** reported to the router (cancellation cannot be attributed to the tool, and fail-fast cancellation must not penalise healthy tools). `call(..., timeout=)` sets the one-shot step's timeout.

### D7. Permission denials known at run start are not retried

The executor retries every exception uniformly and exposes no "non-retryable" signal; adding one would change the parallel-execution spec. Instead, `run()` always computes the static, non-auditing pre-flight (D9) and replaces every named-tool step with a `denied` issue by `dataclasses.replace(step, retries=0)` before handing the plan to the executor. The step still goes through the invoker, so the denial is audited exactly once, emitted as a `denied` event, reported as a normal failed outcome whose `exception` is the `AccessDeniedError`, and the executor's skip propagation handles dependents. No `retrying` event or backoff occurs. Capability steps never see static denials (ineligible tools are filtered by routing) and keep their retry policy. *Alternatives:* (a) short-circuit denied steps without invoking the invoker. Rejected: the denial would not be audited and the step would need a hand-built outcome outside the executor. (b) Raise a special exception and teach the executor to stop retrying it. Rejected: modifies a finished capability.

### D8. Tool-declared resource claims

Tools declare claims as `metadata={"resources": {"reports": "write"}}`. At run start each named-tool step whose tool resolves (honouring its pin) gets `resources = {**tool_claims, **step.resources}` (step wins per resource) via `dataclasses.replace`, which re-runs `Step` validation. A malformed declaration (not a mapping, or a mode other than `read`/`write`) therefore fails the run with `ValidationError` before any step runs, rather than being silently dropped. Capability steps keep only their own claims (Non-Goals).

**Run-scoped lock owners.** `ResourceLockManager` is not re-entrant per owner, and owners are step ids, so two concurrent runs that both contain a step `write` would collide with a `ConflictError`. The executor is therefore given a thin adapter `_RunLocks(manager, run_id)` whose `hold(owner, resources)` delegates to `manager.hold(f"{run_id}/{owner}", resources)`. Locks are still shared across runs (one manager), but owner identities are unique. *Alternative:* passing the shared manager directly. Rejected for the collision above.

### D9. Pre-flight check

`check_plan()` walks the plan in order and reports at most one issue per step: named tool → `unknown_tool` (name or pinned version missing), else `disabled`, else `denied` (`policy.decide(...).allowed` is false); capability step → `no_route` when `router.candidates(cap, eligible=policy.predicate(principal))` is empty. It never invokes a tool and never writes an audit record (`decide`/`predicate` only). `run(preflight=False)` (default) stores the issues in the report and continues, matching the executor's non-fail-fast default where independent branches still deliver value; `run(preflight=True)` raises `PreflightError(issues)` (message lists each issue) before invoking anything when any issue exists. *Alternative:* enforcing by default. Rejected by the spec: a partially servable plan should still produce value, and the report already surfaces the issues.

### D10. Fan-out as a run option

`run(plan, principal, fanout={"step_id": FanOut(n=3, strategy="majority", options={"quorum": 0.6})})`; an `int` is shorthand for `FanOut(n=int)`. `FanOut` validates `n` (int ≥ 1, not bool) at construction; `run()` rejects, with `ValidationError` before any step runs, an unknown step id, a named-tool step, or a value that is neither `FanOut` nor int. Execution of a fan-out step: take candidates as in D5, skipping invalid-input ones, until `n` are selected (fewer if fewer exist; none → `NoRouteError`); authorize each (audited), emit `routed` per tool; invoke them concurrently with `asyncio.gather` (each under `tool_timeout`, each wrapped so an exception becomes `ToolResult(ok=False, error=...)`); record health per tool; then `resolver.resolve(results, fanout.strategy, **fanout.options)`. Each result carries `metadata={"priority": <capability priority>}` so the resolver's `priority` strategy and majority tie-breaks work without configuration. All-failed or unreconcilable results raise the resolver's `ConflictError` (step fails, normal retry policy applies). Success emits `resolved` (detail: strategy, winners, dissenters, confidence) and returns `ToolResult(tool=f"fanout:{cap}", version="", ok=True, value=resolution.value, metadata={"resolution": resolution})`. Fan-out does not fall back beyond the selected `n`. *Alternative:* a `fanout` field on `Step`. Rejected: it would modify the executor's model, and keeping it a run option lets the same plan run with or without reconciliation.

### D11. One-shot helpers reuse `run()`

`call(target, args, principal=...)` builds a one-step plan (`Step("call", capability=name)` for `capability:<name>`, else `Step("call", tool=target, version=version)`), runs it, returns the outcome's `ToolResult`, and on failure re-raises `outcome.exception` (or `ExecutionError(outcome.error)` for an `ok=False` result). `ask(capability, args, principal=..., n=3, strategy=None, options=None)` runs a one-step capability plan with `fanout={"ask": FanOut(n, strategy, options or {})}` and returns `report.resolutions["ask"]`, re-raising the original error on failure. All semantics (permissions, fallback, events, locking, audit) are therefore identical to plans. *Alternative:* a separate direct-invocation path. Rejected: two code paths for the same rules would drift.

### D12. Run report

`RunReport` (frozen) holds `run_id`, `execution: ExecutionReport`, `served_by: dict[str, tuple[str, ...]]` (succeeded steps only: the serving tool, or the resolution winners for fan-out), `attempts: dict[str, tuple[ToolAttempt, ...]]` (all executor attempts, in order), `resolutions: dict[str, Resolution]`, `audit: tuple[AuditRecord, ...]`, `issues: tuple[PlanIssue, ...]`, `contentions: tuple[tuple[str, str, str], ...]` (`(resource, step_a, step_b)`), `events: tuple[OrchestratorEvent, ...]`. Derived: `chains` (step → tuple of tool names from `attempts`), `warnings` (human-readable contention strings, e.g. `"steps 'a' and 'b' both claim 'db' (write) and will serialize"`), and delegation of `ok`, `outputs`, `results`, `succeeded`, `failed`, `skipped`, `cancelled`, `duration`, `__getitem__` and `raise_for_failure()` to `execution`. *Alternative:* subclassing `ExecutionReport`. Rejected: composition keeps the executor's report type untouched and makes the orchestrator-specific fields obvious.

### D13. Audit records of the run

`PermissionPolicy.check` does not return its record. Around each synchronous `check()` call the orchestrator reads `policy.audit_log` before and after (no `await` in between, so no other task on the loop can interleave) and keeps the newest record when it is a new object and matches the principal id, tool name and version. With `audit_limit=0` nothing is captured, which is correct. *Alternatives:* diffing log length over the whole run (breaks at the log bound and with concurrent runs) or wrapping/subclassing the policy (would change the object users pass in). Rejected.

### D14. Contention warnings

Computed from the effective plan (after D8) with `detect_write_conflicts({step.id: step.resources for steps with claims})`, then filtered to drop pairs where one step is a transitive dependency of the other (ancestor sets computed once per plan by walking `depends_on`). Results are stored in `contentions` and rendered in `warnings`; they never block the run.

### D15. Dynamic registry reflection

The router queries the registry on every decision, and pre-flight reads it at call time, so register/enable/disable are reflected for free. The orchestrator subscribes to registry events and calls `router.reset(name)` on `unregistered`, so a re-registered tool does not inherit stale health or circuit state. `close()` unsubscribes (idempotent).

### D16. Unified event stream

Executor `StepEvent`s are converted to `OrchestratorEvent(kind, step_id, attempt=..., error=..., at=...)`; orchestrator kinds are `routed` (tool chosen, after input validation, before authorization), `fallback` (failed tool, `detail["next"]`), `denied` (tool, error) and `resolved` (fan-out detail). Both sources emit synchronously on the event loop, so a single list preserves order, e.g. `started → routed(flaky) → fallback(flaky) → routed(alpha) → succeeded`. Each event is appended to the run context and delivered to the optional constructor `on_event` listener; listener exceptions are logged and swallowed, as in the registry and executor. *Alternative:* two separate streams (executor + orchestrator). Rejected by the spec; a merged stream is what consumers need to read a step's story.

### D17. Decorator

`orch.tool` works bare (`@orch.tool`) and with arguments. Defaults: name = `fn.__name__`, capabilities = `(name,)` (strings or `Capability` objects accepted), version `1.0.0`, description = first non-empty docstring line, `required_scopes = scopes`, `input_schema`, `metadata`. It builds a `ToolSpec` (whose own validation applies), registers it, and returns the original function unchanged, so it stays directly awaitable and testable.

### D18. Package interface, examples and demo

`__init__.py` re-exports every public name of `core`, `registry`, `router`, `permissions`, `executor`, `conflicts` and `agent` (each module's `__all__` where it has one) plus `__version__ = "0.1.0"`, and `__all__` lists them. `examples.py` (depends on `agent` and `core` only) exposes `register_example_tools(target)` (an `Orchestrator` or `ToolRegistry`; returns the registered specs), `example_policy()` and `async run_demo() -> str`. Example tools are deterministic: the flaky search tool is created per registration with its own counter, so each demo/test run behaves identically. `__main__.py` prints `run_demo()`'s summary and exits 0; the demo runs phases sequentially (plan with parallel steps, references, fallback, denial and skip; `ask` with majority; delegated principal with a contended writer) so the flaky tool's single failure is always consumed by the same step.

### D19. README as executable documentation

The README documents architecture and every capability with Python examples that are self-contained (`asyncio.run(main())`, `assert`s stating the claimed result). `tests/test_readme.py` extracts every fenced `python` block and executes each in a fresh namespace, so documentation cannot drift from the code. *Alternative:* doctest. Rejected: async examples and multi-line setup read better as plain blocks.

## Risks / Trade-offs

- [A tool denied only after the run started is retried per its retry policy] → Each retry is re-authorized and audited and never invokes the tool. Documented; static denials (the common case) are not retried.
- [Fan-out within one step occupies one concurrency slot while invoking N tools] → Documented; `max_concurrency` bounds steps, not tool calls.
- [Routed tools cannot contribute resource claims] → Executor locks before routing; plan authors put claims on capability steps when needed. Named-tool steps inherit tool claims.
- [Lock holders appear under run-scoped owner names (`run-3/write`)] → Documented in the README; introspection via `holders()` shows the run id, which is more informative, not less.
- [Audit capture reads the policy's log around each check] → Safe on one event loop; with a policy shared across threads a record may be interleaved, in which case it is skipped (it must be new and match principal and tool) rather than misattributed. Each read copies the bounded log (≤ `audit_limit`), acceptable for the default 1000.
- [Router identity check reads a private attribute] → Same package; guarded with `getattr` so foreign router implementations are accepted.
- [Pre-flight is a snapshot] → Registry/policy changes after it are still enforced at invocation time; pre-flight only reports, optionally blocks, and suppresses retries for static denials.
- [Cancelled candidates are not reported to the router] → A tool that only ever hangs past the step timeout is never penalised; mitigated by setting `tool_timeout`, which does count.

## Migration Plan

Additive only: new modules and exports, no behavior change in existing modules, no new runtime dependencies. Rollback is reverting the change.
