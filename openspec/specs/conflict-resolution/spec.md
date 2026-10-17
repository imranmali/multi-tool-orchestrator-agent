# conflict-resolution Specification

## Purpose
Reconciles divergent results returned by several tools answering the same request into one explainable decision, and coordinates concurrent access to shared named resources so parallel work cannot corrupt it or deadlock.

## Requirements

### Requirement: Only successful results participate
Result reconciliation SHALL consider only successful tool results; failed results MUST be ignored. When no successful result is supplied, reconciliation MUST fail with a conflict error whose message includes each failed tool's name and error.

#### Scenario: Failed results ignored
- **WHEN** tool `a` fails and tools `b` and `c` both succeed with value `42`
- **THEN** the resolution value is `42` and only `b` and `c` are counted as participants

#### Scenario: Nothing succeeded
- **WHEN** tools `a` and `b` both fail with errors `boom` and `down`
- **THEN** a conflict error is raised whose message mentions `a`, `boom`, `b` and `down`

### Requirement: Resolution record
Every reconciliation SHALL produce a resolution recording the chosen value, the strategy name used, the tools that agreed with the outcome (winners), the participating tools that did not (dissenters), a confidence between 0 and 1, and strategy-specific details. Unless a strategy states otherwise, confidence MUST be the share of participants that are winners.

#### Scenario: Unanimous agreement
- **WHEN** tools `a`, `b` and `c` all succeed with value `{"x": 1}`
- **THEN** the resolution has value `{"x": 1}`, winners `a`, `b`, `c`, no dissenters and confidence 1.0

#### Scenario: Partial agreement
- **WHEN** tools `a` and `b` return `1` and tool `c` returns `2` under majority vote
- **THEN** winners are `a` and `b`, the dissenter is `c` and confidence is 2/3

### Requirement: Default strategy
The resolver SHALL use a configurable default strategy, majority vote unless configured otherwise, whenever reconciliation is requested without naming a strategy. Configuring an unknown default strategy MUST fail with a validation error.

#### Scenario: Majority by default
- **WHEN** a resolver is created without a default strategy and reconciles results without naming one
- **THEN** the resolution's strategy is `majority`

#### Scenario: Unknown default rejected
- **WHEN** a resolver is created with default strategy `coin-flip`
- **THEN** a validation error is raised

### Requirement: First-success strategy
The first-success strategy SHALL choose the value of the first successful result in the order the results were supplied.

#### Scenario: Earliest success wins
- **WHEN** results are supplied in order `a` (failed), `b` (value `"x"`), `c` (value `"y"`)
- **THEN** the value is `"x"`, winners are `b` and the dissenter is `c`

### Requirement: Majority vote by structural equality
The majority strategy SHALL group successful results whose values are structurally equal, including unhashable values such as dictionaries and lists, with dictionary key order not affecting equality, and SHALL choose the value of the largest group.

#### Scenario: Unhashable values grouped
- **WHEN** tools `a` and `b` return `{"k": [1, 2], "z": 0}` and `{"z": 0, "k": [1, 2]}` and tool `c` returns `{"k": [3]}`
- **THEN** the value is `{"k": [1, 2], "z": 0}` with winners `a` and `b`

### Requirement: Deterministic majority tie-breaking
When several groups tie for largest under majority vote, the group containing the highest-priority tool SHALL win; if priorities also tie, the group containing the alphabetically smallest tool name MUST win.

#### Scenario: Tie broken by priority
- **WHEN** tool `a` (priority 1) returns `"x"` and tool `b` (priority 5) returns `"y"`
- **THEN** the value is `"y"`

#### Scenario: Tie broken by name
- **WHEN** tools `zed` and `alpha` with equal priority return `"z"` and `"a"`
- **THEN** the value is `"a"`

### Requirement: Majority quorum
The majority strategy SHALL accept an optional quorum, a fraction greater than 0 and at most 1. The quorum is met when the winning group's size divided by the number of participants is greater than or equal to it; otherwise reconciliation MUST fail with a conflict error. A quorum outside that range MUST be rejected with a validation error.

#### Scenario: Quorum met
- **WHEN** two of three participants agree and the quorum is 0.6
- **THEN** the majority value is returned

#### Scenario: Quorum not met
- **WHEN** two of four participants agree and the quorum is 0.75
- **THEN** a conflict error is raised

#### Scenario: Invalid quorum
- **WHEN** the quorum is 1.5
- **THEN** a validation error is raised

### Requirement: Tool priority source
A tool's priority for reconciliation SHALL be taken from the resolver's configured per-tool-name priorities when present, otherwise from the result's `priority` metadata, otherwise 0.

#### Scenario: Configured priority overrides metadata
- **WHEN** the resolver is configured with priority 10 for tool `a` and tool `b`'s result carries metadata priority 5
- **THEN** tool `a` is treated as higher priority than `b`

#### Scenario: Metadata priority used
- **WHEN** no priorities are configured and tool `b`'s result carries metadata priority 5 while tool `a`'s carries none
- **THEN** tool `b` is treated as higher priority than `a`

### Requirement: Priority strategy
The priority strategy SHALL choose the value of the highest-priority successful tool, breaking priority ties by the alphabetically smallest tool name.

#### Scenario: Highest priority wins over majority
- **WHEN** tools `a` and `b` (priority 0) return `1` and tool `c` (priority 9) returns `2`
- **THEN** the value is `2`, the winner is `c` and the dissenters are `a` and `b`

### Requirement: Deep merge of structured results
The merge strategy SHALL combine dictionary values recursively: keys from every tool are included in first-seen order, nested dictionaries are merged, lists at the same key are unioned in first-seen order without duplicates, and equal values are kept once.

#### Scenario: Disjoint and nested keys merged
- **WHEN** tool `a` returns `{"name": "x", "meta": {"lang": "en"}}` and tool `b` returns `{"meta": {"size": 3}, "tags": ["t1"]}`
- **THEN** the value is `{"name": "x", "meta": {"lang": "en", "size": 3}, "tags": ["t1"]}` with confidence 1.0

#### Scenario: Lists unioned
- **WHEN** tool `a` returns `{"tags": ["x", "y"]}` and tool `b` returns `{"tags": ["y", "z"]}`
- **THEN** the merged `tags` value is `["x", "y", "z"]`

### Requirement: Merge conflict policy
When tools supply different non-mergeable values at the same key path, the merge strategy SHALL apply the conflict policy: `error` (default) MUST fail with a conflict error naming every conflicting path and the tools involved; `priority` keeps the highest-priority tool's value; `first` keeps the first-supplied value. Conflicting paths MUST be recorded in the details and tools whose value was overridden are dissenters.

#### Scenario: Conflict raises by default
- **WHEN** tool `a` returns `{"meta": {"lang": "en"}}` and tool `b` returns `{"meta": {"lang": "fr"}}` with no policy
- **THEN** a conflict error is raised naming path `meta.lang` and tools `a` and `b`

#### Scenario: Priority policy
- **WHEN** the same results are merged with policy `priority` and `b` has the higher priority
- **THEN** `meta.lang` is `"fr"`, `meta.lang` is recorded as a conflicting path and `a` is a dissenter

#### Scenario: First policy
- **WHEN** the same results are merged with policy `first`
- **THEN** `meta.lang` is `"en"` and `b` is a dissenter

### Requirement: Merge of non-dictionary results
The merge strategy SHALL require every participating value to be a dictionary unless all participating values are equal, in which case that value is returned; otherwise it MUST fail with a conflict error.

#### Scenario: Equal scalars accepted
- **WHEN** tools `a` and `b` both return `7` under merge
- **THEN** the value is `7`

#### Scenario: Different scalars rejected
- **WHEN** tool `a` returns `7` and tool `b` returns `{"x": 1}` under merge
- **THEN** a conflict error is raised

### Requirement: Numeric aggregation
The numeric strategy SHALL aggregate numeric values with `mean` (default), `median`, `min` or `max`. Every participant is a winner; confidence MUST be 1.0 when all values are equal and lower when they differ. Non-numeric values, including booleans, MUST fail with a conflict error, and an unknown aggregation with a validation error.

#### Scenario: Mean of values
- **WHEN** tools return `1`, `2` and `6` with aggregation `mean`
- **THEN** the value is `3`

#### Scenario: Median, min and max
- **WHEN** tools return `1`, `2` and `6`
- **THEN** `median` yields `2`, `min` yields `1` and `max` yields `6`

#### Scenario: Non-numeric value
- **WHEN** one tool returns `"many"` under the numeric strategy
- **THEN** a conflict error is raised

### Requirement: Custom strategies
Callers SHALL be able to supply a custom strategy inline or register one under a name. A custom strategy receives the successful results; a returned resolution is used as-is, while any other returned value MUST be wrapped in a resolution whose winners are the participants whose value equals it.

#### Scenario: Inline callable returning a plain value
- **WHEN** a callable returning the longest string value is used and tools return `"ab"` and `"abc"`
- **THEN** the value is `"abc"` and the winner is the tool that returned it

#### Scenario: Registered named strategy
- **WHEN** a strategy is registered as `longest` and reconciliation names `longest`
- **THEN** the registered strategy decides the value and the resolution's strategy is `longest`

### Requirement: Invalid strategy requests rejected
Naming a strategy that is neither built in nor registered, passing an option a built-in strategy does not accept, or registering a strategy under an existing name without requesting replacement MUST fail with a validation error.

#### Scenario: Unknown strategy name
- **WHEN** reconciliation names strategy `coin-flip`
- **THEN** a validation error is raised

#### Scenario: Unknown option
- **WHEN** the first-success strategy is given option `quorum`
- **THEN** a validation error is raised

#### Scenario: Duplicate registration
- **WHEN** a strategy is registered under the name `majority`
- **THEN** a validation error is raised

### Requirement: Shared and exclusive resource locks
The lock manager SHALL let an owner hold named resources in `read` or `write` mode: any number of readers MAY hold a resource together, while a writer MUST hold it exclusively. Any other mode MUST be rejected with a validation error.

#### Scenario: Readers run concurrently
- **WHEN** two owners request `read` on resource `db` at the same time
- **THEN** both hold it simultaneously

#### Scenario: Writer is exclusive
- **WHEN** owner `w` holds `write` on `db` and owner `r` requests `read` on `db`
- **THEN** `r` waits until `w` releases

#### Scenario: Invalid mode
- **WHEN** an owner requests mode `append` on `db`
- **THEN** a validation error is raised

### Requirement: FIFO fairness
Lock requests on a resource SHALL be granted in arrival order: a request MUST NOT be granted while an earlier incompatible request on the same resource is still waiting, so a stream of readers cannot starve a waiting writer. Consecutive waiting readers are granted together.

#### Scenario: Writer not starved by readers
- **WHEN** a reader holds `db`, a writer then requests `db`, and further readers keep requesting `db`
- **THEN** the writer acquires `db` before any reader that arrived after it

### Requirement: Deadlock-free multi-resource acquisition
An owner SHALL be able to hold several resources at once, acquired in a single global order regardless of the order the request lists them, so concurrent owners requesting overlapping resources in opposite orders MUST NOT deadlock.

#### Scenario: Opposite orderings
- **WHEN** owner `p` requests write on `a` then `b` while owner `q` concurrently requests write on `b` then `a`, repeatedly
- **THEN** all requests complete within a bounded time

### Requirement: Release on exit, error and cancellation
Held resources SHALL be released when the holding scope exits normally, raises, or is cancelled. A request cancelled while waiting MUST leave no trace in the waiting queue and MUST release any resources it already acquired, and its departure MUST let later compatible waiters proceed.

#### Scenario: Release on exception
- **WHEN** an owner's scope holding `write` on `db` raises an error
- **THEN** `db` has no holders afterwards

#### Scenario: Release on cancellation while holding
- **WHEN** a task holding `db` is cancelled
- **THEN** `db` has no holders afterwards

#### Scenario: Cancellation while waiting
- **WHEN** a task already holding `a` is cancelled while waiting for `b`
- **THEN** it holds neither `a` nor `b` and is no longer counted as waiting

### Requirement: Acquisition timeout
A lock request SHALL accept an optional timeout in seconds covering all its resources; if the resources cannot all be acquired in time, it MUST fail with a conflict error and leave nothing held or queued. A negative timeout MUST be rejected with a validation error.

#### Scenario: Timeout while resource is busy
- **WHEN** `db` is held for writing and another owner requests it with a 0.05 second timeout
- **THEN** a conflict error is raised and the requester holds nothing and is not waiting

### Requirement: Re-entrant requests rejected
Locks SHALL NOT be re-entrant: when an owner requests a resource it already holds or is already waiting for, the request MUST fail immediately with a conflict error instead of deadlocking.

#### Scenario: Same owner requests a held resource
- **WHEN** owner `p` holds `db` and requests `db` again
- **THEN** a conflict error is raised and `p` still holds `db` from the first request

### Requirement: Lock introspection
The lock manager SHALL report, for any resource name, its current holders with their modes and the number of requests waiting for it. Unknown resources MUST report no holders and zero waiters.

#### Scenario: Holders and waiters reported
- **WHEN** owner `w` holds `write` on `db` and two other owners are waiting for `db`
- **THEN** the holders of `db` are `{"w": "write"}` and its waiting count is 2

### Requirement: Static claim conflict detection
The system SHALL report, from a mapping of owners to their resource claims, every pair of owners whose claims on the same resource conflict (at least one claims `write`), as resource and owner-pair triples with owner names in alphabetical order, sorted by resource then owners. Invalid modes MUST be rejected with a validation error.

#### Scenario: Conflicts reported deterministically
- **WHEN** owner `s2` claims write on `db`, owner `s1` claims read on `db` and read on `cache`, and owner `s3` claims read on `cache`
- **THEN** the result is exactly `[("db", "s1", "s2")]`
