# Proposal

## Why

The registry can tell the orchestrator which tools *offer* a capability, but not which one it *should use*. Callers need a single, deterministic decision point that weighs each tool's priority, cost and latency hints, respects caller-specific eligibility (such as permissions), and steers traffic away from tools that keep failing — otherwise every caller re-implements selection and a broken tool keeps receiving requests.

## What Changes

- Introduce a capability router that, given a capability name, returns the ranked list of usable tools or the single best one.
- Provide built-in ranking strategies — `priority`, `cheapest`, `fastest` and a default `balanced` weighted score — plus support for a caller-supplied scoring function; strategies can be chosen per router or per call.
- Allow callers to narrow the candidate set with an eligibility predicate (the integration point for permission checks, without the router depending on permissions) and an explicit exclusion list.
- Track per-tool health (successes, failures, consecutive failures, average observed latency, success rate) fed by the caller after each invocation.
- Add a circuit breaker: a tool that fails a configurable number of times in a row is skipped until a cooldown elapses, then is retried (half-open) and either recovers on success or is skipped again on failure.
- Raise a no-route error that distinguishes "no tool offers this capability" from "tools offer it but none are eligible or healthy".
- Offer multi-capability routing and a per-candidate explanation of scores and exclusion reasons for observability.

## Capabilities

### New Capabilities
- `capability-routing`: Selecting and ranking the registered tool that should serve a requested capability, including ranking strategies, eligibility and exclusion filtering, health tracking and circuit breaking.

### Modified Capabilities

## Impact

- New module `src/orchestrator/router.py` depending only on `orchestrator.core` and `orchestrator.registry`.
- New tests in `tests/test_router.py`.
- The top-level orchestrator (`orchestrator/agent.py`, separate change) is expected to pass its permission check as the eligibility predicate and to report invocation outcomes back to the router.
- No new runtime dependencies; no changes to existing modules or their specs.
