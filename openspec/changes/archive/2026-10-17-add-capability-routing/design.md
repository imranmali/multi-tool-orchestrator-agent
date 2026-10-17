# Design

## Context

The registry (`tool-registry` spec) already answers "which enabled tools declare capability X" via `find_by_capability`, returning the latest version per name, and every `Capability` carries `cost`, `latency` and `priority` hints. Motivation is in proposal.md - Why; required behavior is in `specs/capability-routing/spec.md`. Project constraints: stdlib only, deterministic tests (no sleeping, no network), modules depend only on `core` and `registry`.

## Goals / Non-Goals

**Goals:**
- One `CapabilityRouter` class that ranks, selects and explains, and owns health/circuit state.
- Pluggable ranking with a deterministic total order.
- Health tracking that is cheap, thread-safe and testable with an injected clock.

**Non-Goals:**
- Invoking tools or measuring latency itself: the caller (executor/agent) reports outcomes via `record_success` / `record_failure`.
- Permission logic: eligibility arrives as an opaque predicate, so the router never imports the permissions module.
- Using *observed* latency in ranking: `fastest` and `balanced` use the declared latency hint only, because observed seconds and declared hints are not guaranteed to share units. Observed latency is reported in stats and explanations.
- Per-version health: stats are keyed by tool name; routing only ever sees the latest version per name.
- Persisting stats across process restarts, or dropping stats when a tool is unregistered (a re-registered tool inherits its name's history).

## Decisions

- **Public API** (in `src/orchestrator/router.py`):
  - `CapabilityRouter(registry, *, strategy="balanced", failure_threshold=3, cooldown=30.0, clock=time.monotonic)`
  - `candidates(capability, *, eligible=None, exclude=(), strategy=None) -> list[ToolSpec]`
  - `route(capability, **same) -> ToolSpec`, `route_many(capabilities, **same) -> dict[str, ToolSpec]`
  - `explain(capability, **same) -> list[RouteExplanation]`
  - `record_success(name, latency=None)`, `record_failure(name)`, `stats(name) -> ToolStats`, `circuit_state(name) -> str`, `reset(name=None)`
  - `ToolStats` (frozen snapshot: `name, successes, failures, consecutive_failures, avg_latency, success_rate, circuit`), `RouteExplanation` (`tool, version, rank, score, key, reason, stats`), `RoutingStrategy` (Protocol for the custom callable), `STRATEGIES` (built-in names), circuit constants `CLOSED`, `OPEN`, `HALF_OPEN`.
- **Uniform "higher is better" ranking keys.** Every strategy produces, per candidate, a tuple of numbers compared lexicographically, higher first; the final sort key is `(negated tuple, tool name)` so ties always break by name ascending. The first element is reported as the candidate's `score` in explanations.
  - `priority`: `(priority, -cost, -latency)`
  - `cheapest`: `(-cost, priority, -latency)`
  - `fastest`: `(-latency, priority, -cost)`
  - `balanced`: `(score,)` - see below
  - custom callable `(ToolSpec, Capability, ToolStats) -> float`: `(float(result),)`. **Higher is better**, matching `priority` and `balanced`. Results that are not `int`/`float` (or are `bool` or NaN) raise `ValidationError`. Alternative considered: lower-is-better like a cost function - rejected so that all scores reported by `explain` read the same way.
- **Balanced formula.** Over the *filtered* candidate set for one request:
  - `prio_n = (p - p_min) / (p_max - p_min)`, or `1.0` when all priorities are equal
  - `cheap_n = 1 - cost / cost_max`, or `1.0` when `cost_max == 0`
  - `fast_n = 1 - latency / latency_max`, or `1.0` when `latency_max == 0`
  - `health = stats.success_rate` (`1.0` with no observations - optimistic, so new tools get traffic)
  - `score = 0.4·prio_n + 0.2·cheap_n + 0.2·fast_n + 0.2·health` (weights in `BALANCED_WEIGHTS`; score ∈ [0, 1])
  Normalising within the request keeps hints of different magnitudes comparable without global configuration. Alternative: absolute weighted sum - rejected because a cost of 100 would swamp everything else.
- **Strategy resolution** happens at construction (fail fast on unknown names) and again for each per-call override. A string must be in `STRATEGIES`; anything else must be callable; otherwise `ValidationError`.
- **Filter order and reasons.** Start from `registry.find_by_capability(cap)`; for each tool, the first failing check sets the reason: `excluded` (name in `exclude`), `not eligible` (predicate false), `circuit open`. The predicate is not called for excluded tools, so permission checks are skipped when unnecessary.
- **No-route messages.** Empty discovery → `no enabled tool offers capability 'x'`. Otherwise → `N tool(s) offer capability 'x' but none are usable: a (excluded), b (not eligible)`.
- **Circuit breaker as a timestamp, not a state machine.** Each name has `opened_at: float | None`. State is derived from the injected clock: `None` → closed; `now - opened_at < cooldown` → open; otherwise half-open. `record_failure` increments `consecutive_failures`; if the circuit is open or half-open it sets `opened_at = now` (reopen / extend), else it opens when `consecutive_failures >= failure_threshold`. `record_success` zeroes `consecutive_failures` and clears `opened_at`. Deriving state from time avoids a background timer and makes tests clock-driven. Half-open admits any number of concurrent requests (no single-probe limiting) - simpler, and the first reported failure reopens it.
- **Thread safety.** A `threading.Lock` guards the stats dict; ranking takes immutable `ToolStats` snapshots under the lock, then evaluates predicates and strategies outside it so user code never runs while the lock is held.
- **Config validation.** `failure_threshold` must be an int ≥ 1 and `cooldown` ≥ 0, else `ValidationError`.

## Risks / Trade-offs

- [Balanced scores are relative to the current candidate set, so the same tool's score changes as peers come and go] → Documented; `explain` exposes the score per request for debugging.
- [Half-open lets a burst of requests through to a still-broken tool] → The first reported failure reopens the circuit; single-probe gating can be added later without spec changes to closed/open behavior.
- [Health is only as good as the caller's reporting] → The agent change must call `record_success` / `record_failure` after each invocation; noted in the proposal's Impact.
- [Custom scoring functions may raise] → Exceptions propagate unchanged to the caller rather than being silently converted.
