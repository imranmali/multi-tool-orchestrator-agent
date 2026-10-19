# capability-routing Specification

## Purpose
Decides which registered tool should serve a requested capability by ranking the enabled, eligible and healthy tools that offer it, and keeps failing tools out of rotation with a circuit breaker.

## Requirements

### Requirement: Candidate discovery
The router SHALL return, for a requested capability, every enabled tool (latest version per name) that declares that capability and passes all filters, ordered best-first. Disabled tools and tools that do not declare the capability MUST NOT be returned.

#### Scenario: Only offering tools are candidates
- **WHEN** tools `a` and `b` declare `search`, tool `c` declares only `math`, and candidates for `search` are requested
- **THEN** exactly `a` and `b` are returned

#### Scenario: Disabled tool skipped
- **WHEN** tools `a` and `b` declare `search` and `a` is disabled in the registry
- **THEN** candidates for `search` contain only `b`

#### Scenario: Capability nobody offers
- **WHEN** candidates are requested for `teleport` and no tool declares it
- **THEN** an empty list is returned

### Requirement: Caller eligibility filtering
The router SHALL accept an optional caller-supplied eligibility predicate evaluated per tool, and MUST drop every tool for which it returns false. The router MUST NOT itself depend on how eligibility is decided.

#### Scenario: Predicate rejects a tool
- **WHEN** tools `a` and `b` declare `search` and the predicate returns false for `a`
- **THEN** candidates for `search` contain only `b` and routing selects `b`

### Requirement: Explicit exclusion
The router SHALL accept a collection of tool names to exclude, and MUST NOT return or select any excluded tool, for example when a caller retries after a tool failed.

#### Scenario: Excluded tool skipped
- **WHEN** tools `a` and `b` declare `search`, `a` would rank first, and `a` is excluded
- **THEN** routing `search` selects `b`

### Requirement: Route selection
Routing a capability SHALL return the first-ranked candidate under the effective strategy.

#### Scenario: Best candidate returned
- **WHEN** tools `a` (priority 1) and `b` (priority 5) declare `search` and `search` is routed with the `priority` strategy
- **THEN** `b` is returned

### Requirement: Descriptive no-route errors
Routing MUST fail with a no-route error when no candidate remains. The message MUST state that no enabled tool offers the capability when none does, and otherwise MUST state how many tools offer it and why each was rejected (excluded, not eligible, circuit open).

#### Scenario: Nobody offers the capability
- **WHEN** `teleport` is routed and no enabled tool declares it
- **THEN** a no-route error is raised whose message says no enabled tool offers `teleport`

#### Scenario: Offered but filtered out
- **WHEN** two tools declare `search`, one is excluded and the other fails the eligibility predicate, and `search` is routed
- **THEN** a no-route error is raised whose message says 2 tools offer `search` and names each tool with its rejection reason

### Requirement: Priority strategy
The `priority` strategy SHALL rank candidates by the capability's priority (highest first), then by cost (lowest first), then by latency hint (lowest first). An infinite cost or latency hint MUST rank after every finite one at that step.

#### Scenario: Priority then cost
- **WHEN** `a` has priority 5 cost 9, `b` has priority 5 cost 1, and `c` has priority 1 cost 0 for `search`
- **THEN** the `priority` ranking is `b`, `a`, `c`

#### Scenario: Infinite hints break priority ties last
- **WHEN** `a` has priority 9 cost infinite latency 1, `d` has priority 9 cost infinite latency infinite, and `c` has priority 0 cost 5 for `search`
- **THEN** the `priority` ranking is `a`, `d`, `c`

### Requirement: Cheapest strategy
The `cheapest` strategy SHALL rank candidates by the capability's cost hint, lowest first, breaking ties by highest priority then lowest latency hint. A candidate with an infinite cost MUST rank after every candidate with a finite cost.

#### Scenario: Lowest cost first
- **WHEN** `a` has cost 3, `b` cost 1 and `c` cost 2 for `search`
- **THEN** the `cheapest` ranking is `b`, `c`, `a`

#### Scenario: Infinite cost ranks last
- **WHEN** `a` has an infinite cost and priority 9, `b` has cost `1e300` and `c` has cost 5 for `search`
- **THEN** the `cheapest` ranking is `c`, `b`, `a`

### Requirement: Fastest strategy
The `fastest` strategy SHALL rank candidates by the capability's latency hint, lowest first, breaking ties by highest priority then lowest cost. A candidate with an infinite latency hint MUST rank after every candidate with a finite latency hint.

#### Scenario: Lowest latency first
- **WHEN** `a` has latency 0.5, `b` latency 2.0 and `c` latency 0.1 for `search`
- **THEN** the `fastest` ranking is `c`, `a`, `b`

#### Scenario: Infinite latency ranks last
- **WHEN** `a` has an infinite latency hint and `b` has latency 5.0 for `search`
- **THEN** the `fastest` ranking is `b`, `a`

### Requirement: Balanced default strategy
The default `balanced` strategy SHALL rank candidates by a single score, higher first, that increases with priority and observed success rate and decreases with cost and latency hints, each normalised across the candidate set. The score MUST always be a finite number: an infinite cost or latency hint scores the worst value for that component, and while any candidate's hint is infinite every finite hint for that component scores the best value.

#### Scenario: Default strategy is balanced
- **WHEN** a router is created without naming a strategy
- **THEN** its strategy is `balanced`

#### Scenario: Dominating tool wins
- **WHEN** `a` has higher priority, lower cost and lower latency than `b` for `search` and both have equal health
- **THEN** balanced routing selects `a`

#### Scenario: Failures lower the score
- **WHEN** `a` and `b` have identical hints for `search` and `a` has recorded failures while `b` has recorded only successes
- **THEN** balanced routing selects `b`

#### Scenario: Infinite hints keep the score finite
- **WHEN** `a` has an infinite cost and priority 0 and `b` has cost 1.0 and priority 5 for `search`, or `a` has an infinite latency hint and `b` has latency 5.0
- **THEN** balanced routing ranks `b` before `a` and every explained score is a finite number

#### Scenario: Every hint infinite
- **WHEN** `a` and `b` both have infinite cost and infinite latency hints and equal priority and health for `search`
- **THEN** both explained scores are equal finite numbers and the ranking is `a`, `b`

### Requirement: Custom scoring strategy
The router SHALL accept a caller-supplied scoring function receiving the tool, its matching capability declaration and its health statistics, and returning a number where higher is better. A non-numeric score MUST be rejected with a validation error.

#### Scenario: Custom score ranks candidates
- **WHEN** a scoring function returns 10 for `b` and 1 for `a`
- **THEN** routing selects `b`

#### Scenario: Non-numeric score rejected
- **WHEN** a scoring function returns the string `high`
- **THEN** routing fails with a validation error

### Requirement: Strategy selection
A strategy SHALL be configurable per router and overridable per call. An unknown strategy name or a value that is neither a known name nor a callable MUST be rejected with a validation error.

#### Scenario: Per-call override
- **WHEN** a router configured with `priority` routes `search` with strategy `cheapest`
- **THEN** the cheapest candidate is selected

#### Scenario: Unknown strategy rejected
- **WHEN** a router is created with strategy `random`
- **THEN** a validation error is raised

### Requirement: Deterministic tie-breaking
Candidates that rank equally under the effective strategy SHALL be ordered by tool name ascending, so identical inputs always produce the same ranking.

#### Scenario: Equal tools ordered by name
- **WHEN** tools `zeta`, `alpha` and `mid` declare `search` with identical hints and health
- **THEN** every strategy ranks them `alpha`, `mid`, `zeta`

### Requirement: Health statistics
The router SHALL record per-tool-name invocation outcomes reported by the caller and expose successes, failures, consecutive failures, average observed latency and success rate. A tool with no recorded outcomes MUST report zero counts, no average latency and a success rate of 1.0. Negative latencies MUST be rejected.

#### Scenario: Counts and averages
- **WHEN** tool `a` records successes with latencies 1.0 and 3.0 and then one failure
- **THEN** its stats show 2 successes, 1 failure, 1 consecutive failure, average latency 2.0 and success rate 2/3

#### Scenario: Unobserved tool
- **WHEN** stats are requested for a tool with no recorded outcomes
- **THEN** counts are zero, average latency is absent and success rate is 1.0

#### Scenario: Success resets consecutive failures
- **WHEN** tool `a` records two failures and then a success
- **THEN** its consecutive failure count is 0

### Requirement: Circuit opens after repeated failures
When a tool records the configured threshold of consecutive failures, its circuit SHALL open and the tool MUST be excluded from candidates until the configured cooldown has elapsed. The threshold MUST be at least 1 and the cooldown non-negative.

#### Scenario: Threshold reached
- **WHEN** the threshold is 3 and tool `a` records 3 consecutive failures
- **THEN** `a` is no longer a candidate and its circuit state is open

#### Scenario: Below threshold
- **WHEN** the threshold is 3 and tool `a` records 2 consecutive failures
- **THEN** `a` is still a candidate

#### Scenario: Invalid configuration
- **WHEN** a router is created with a failure threshold of 0 or a negative cooldown
- **THEN** a validation error is raised

### Requirement: Circuit half-open recovery
Once the cooldown has elapsed after a circuit opened, the tool SHALL become a candidate again in a half-open state. A success MUST close the circuit; a failure MUST reopen it immediately and restart the cooldown.

#### Scenario: Eligible after cooldown
- **WHEN** `a`'s circuit opened at time 0 with cooldown 30 and candidates are requested at time 30
- **THEN** `a` is a candidate and its circuit state is half-open

#### Scenario: Still open before cooldown
- **WHEN** `a`'s circuit opened at time 0 with cooldown 30 and candidates are requested at time 29
- **THEN** `a` is not a candidate

#### Scenario: Success closes the circuit
- **WHEN** a half-open tool records a success
- **THEN** its circuit state is closed and its consecutive failure count is 0

#### Scenario: Failure reopens the circuit
- **WHEN** a half-open tool records a single failure at time 40
- **THEN** its circuit is open again and it is excluded until time 70

### Requirement: Multi-capability routing
The router SHALL route several capabilities in one call, returning a mapping from each requested capability to its selected tool, applying the same filters and strategy to each. If any capability has no route, the call MUST fail with a no-route error for that capability.

#### Scenario: Route several capabilities
- **WHEN** `search` and `math` are routed together and each is offered by some tool
- **THEN** a mapping with both capabilities and their selected tools is returned

#### Scenario: One capability unroutable
- **WHEN** `search` and `teleport` are routed together and nothing offers `teleport`
- **THEN** a no-route error mentioning `teleport` is raised

### Requirement: Routing explanation
The router SHALL explain a routing decision by listing every enabled tool offering the capability with its rank and score when it is a candidate, or its rejection reason (excluded, not eligible, circuit open) when it is not, along with its health statistics.

#### Scenario: Explanation lists ranked and rejected tools
- **WHEN** `a` and `b` declare `search`, `c` declares `search` but is excluded, and an explanation for `search` is requested
- **THEN** `a` and `b` appear with ranks 1 and 2 and scores, and `c` appears with reason excluded and no rank
