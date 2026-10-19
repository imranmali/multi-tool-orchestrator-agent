## MODIFIED Requirements

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
