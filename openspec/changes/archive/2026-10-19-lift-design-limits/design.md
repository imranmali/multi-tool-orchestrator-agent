# Design

## Context

See proposal.md for the motivation. Relevant current state:

- `ToolRegistry` already keeps two sets: per-version flags `(name, version)` and name-level flags. A name-level flag is consulted only for the version that is the latest *now*, which is exactly the limit being lifted.
- `ParallelExecutor` acquires a step's `resources` locks *before* calling the invoker. The orchestrator fills a named-tool step's `resources` from the tool's metadata once, at run start (`_effective_plan`). The invoker receives that effective step, so it can see what is locked.
- The orchestrator invoker resolves a named step's tool afresh at invocation, and capability candidates come from a fresh `router.candidates()` call on every loop iteration. Fan-out members, however, are selected once and then started through `asyncio.gather`, so a member may start after another member's handler has already run. Listener callbacks (`on_event`) run synchronously between routing and invocation.
- `src/orchestrator/__init__.py` is not part of this change, so no new public names (error classes) can be exported.

## Goals / Non-Goals

**Goals:**

- A name-level disable is a hard off switch for every version of the name.
- No tool is invoked unless, at the moment its handler starts, its exact descriptor is registered and enabled.
- A named-tool step never runs a descriptor whose resource claims are not covered by the locks the step holds.

**Non-Goals:**

- Capability steps still do not inherit tool resource claims (the executor locks before routing picks a tool). That documented limit is unchanged.
- Cancelling a handler that is already running when its tool gets disabled. Disabling affects invocations that have not started.
- Re-snapshotting claims for later steps of a run; claims are still computed once per run, and a mismatch is detected rather than repaired.

## Decisions

### D1. Name-level disable is an override, not a bulk per-version operation

`disable(name)` sets only the name flag; `_enabled(spec)` becomes `key not in disabled and name not in disabled_names`. Versions registered later are therefore covered automatically, with no bookkeeping in `register`.

`enable(name)` clears the name flag *and* every per-version flag of that name, so "enable the tool" always leaves every version enabled. `enable(name, version)` clears only that version's own flag; while the name flag is set the version stays disabled. The alternative, letting a version-level enable punch a hole in a name-level disable, needs a third "explicitly enabled" set and makes the outcome of a sequence of calls depend on its order. Having the name flag simply win is easier to explain.

Unregistering every version still forgets both kinds of flag, as before: the name no longer exists, and re-registering it starts clean. Unregistering one version forgets that version's flag only.

*Alternative considered:* make `disable(name)` add a per-version flag for every current version and keep the old sticky-latest rule. Rejected: versions registered later would need special handling, and `enable(name)` would have to guess which flags it owns.

### D2. One event per call

`enable`/`disable` emit exactly one `RegistryEvent`, keeping the existing `(kind, name, version)` shape. A version-level call names that version; a whole-name call names the latest version, as it did before. Emitting one event per affected version was rejected: listeners (the orchestrator only cares about `unregistered`) would see N events for one operator action, and the README's event example would change for no benefit. The event reports the call, not the resulting state. `enable(name, version)` under a name-level disable still emits `enabled` for that version.

### D3. Availability check right before the handler starts

A single helper checks `registry.get(name, version) is spec and registry.is_enabled(name, version)`. It runs:

- for a named-tool step, once when the step starts (so a disabled tool is neither validated nor audited, as before) and again after the `routed` event;
- for a capability candidate and for a fan-out member, after the `routed` event.

In every path nothing suspends between the check, the audited authorization and the start of the handler, so the check is current. Requiring the *same object* (not just the same name@version) means a descriptor replaced in place (`register(..., replace=True)`) is also caught: the code never runs a descriptor other than the one it validated input against and routed.

A failed check records `ToolAttempt(outcome="unavailable")`, is not reported to the router (the tool did nothing wrong) and raises `ExecutionError`. What happens next follows the step kind:

- named-tool step: the attempt fails (executor retries apply as usual);
- capability step: treated like a denial: skip to the next candidate, no `fallback` event, no fallback budget spent. If nothing was invoked and no candidate remains, the step fails with `NoRouteError`, as it would when routed afresh;
- fan-out member: an `ok=False` member result, which the resolver already ignores when other members succeed.

`ExecutionError` is reused instead of a new `ToolUnavailableError`, because the package root (`__init__.py`) is outside this change and the existing disabled-step behaviour already raises `ExecutionError`.

### D4. Claim coverage check for named-tool steps (fail, do not re-lock)

At run start the step's own `resources` are remembered per step id. Right before invoking, the invoker computes `required = {**tool_claims(spec), **own_claims}` for the descriptor about to run and checks that every `(resource, mode)` in `required` is covered by the effective step's `resources`, which are exactly the locks held: same resource, and the held mode is `write` or equals the required mode. On a mismatch, or if the new descriptor's `metadata["resources"]` is malformed, the attempt is recorded with outcome `claims_changed` and fails with an `ExecutionError` naming the required and held claims. The tool is not invoked.

Why coverage rather than equality: holding a lock the tool no longer needs is harmless (it only serializes more), so failing on a narrowed claim would only reject safe runs.

Why fail rather than acquire the missing locks mid-step: the executor owns locking and has already acquired this step's locks in sorted order. Acquiring more from inside the invoker would break the ordering (deadlock risk against another step holding the new resource and waiting on one of ours), and the lock manager is not re-entrant for the same owner. Upgrading read to write in place has the same problems. A failed attempt is visible in the attempt chain, and a new run picks up the new claims at its start.

### D5. Attempt outcomes

`ATTEMPT_OUTCOMES` gains `unavailable` and `claims_changed`. No new event kinds: the `routed` event has already been emitted, and the attempt chain plus the step's `failed` event carry the reason.

## Risks / Trade-offs

- [Pinned older versions of a name-disabled tool now refuse to run] → This is the intended fix. It is called out as BREAKING in the proposal and covered by the README update.
- [A claims mismatch fails the step even when retries are configured, since every retry holds the same snapshot locks] → The error message says the claims changed after the run started; rerunning picks up the new claims.
- [A handler already running when its tool is disabled keeps running] → Out of scope (Non-Goals). Disabling governs new invocations only.
- [Listener-driven disables are a corner case] → They are the only way to change the registry between routing and invocation, and they make the check testable deterministically.

## Migration Plan

No data migration. Callers that relied on a pinned older version running while the tool name is disabled should disable only the specific version(s) with `disable(name, version)`.
