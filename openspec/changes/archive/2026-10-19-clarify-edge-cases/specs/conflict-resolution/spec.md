## MODIFIED Requirements

### Requirement: Majority vote by structural equality
The majority strategy SHALL group successful results whose values are structurally equal, including unhashable values such as dictionaries and lists, with dictionary key order not affecting equality, and SHALL choose the value of the largest group. Equality MUST be type-aware: an integer equals a float of the same value, a boolean equals only a boolean, strings and bytes equal only their own type, a list never equals a tuple, dictionary keys compare by these same rules, and NaN equals NaN.

#### Scenario: Unhashable values grouped
- **WHEN** tools `a` and `b` return `{"k": [1, 2], "z": 0}` and `{"z": 0, "k": [1, 2]}` and tool `c` returns `{"k": [3]}`
- **THEN** the value is `{"k": [1, 2], "z": 0}` with winners `a` and `b`

#### Scenario: Integer equals float of the same value
- **WHEN** tool `a` returns `42` and tool `b` returns `42.0` under majority vote
- **THEN** winners are `a` and `b` and confidence is 1.0

#### Scenario: Boolean is not a number
- **WHEN** tools `a` and `c` return `1` and tool `b` returns `True` under majority vote
- **THEN** winners are `a` and `c` and the dissenter is `b`, and `1.0` likewise does not group with `True`

#### Scenario: String is not a number
- **WHEN** tool `a` returns `1` and tool `b` returns `"1"` under majority vote
- **THEN** they form separate groups

#### Scenario: List is not a tuple
- **WHEN** tool `a` returns `[1]` and tool `b` returns `(1,)` under majority vote
- **THEN** they form separate groups

#### Scenario: Dictionary keys compared type-aware
- **WHEN** tools return `{1: "x"}`, `{1.0: "x"}`, `{"1": "x"}` and `{True: "x"}` under majority vote
- **THEN** `{1: "x"}` and `{1.0: "x"}` form one group while `{"1": "x"}` and `{True: "x"}` each form their own group

#### Scenario: NaN groups with NaN
- **WHEN** tools `a` and `b` both return NaN and tool `c` returns `0.0` under majority vote
- **THEN** winners are `a` and `b`

### Requirement: Deterministic majority tie-breaking
When several groups tie for largest under majority vote, the group containing the highest-priority tool SHALL win; if priorities also tie, the group containing the alphabetically smallest tool name MUST win. Both comparisons MUST consider every member of each tied group, not only its first-supplied member, and the tie MUST be recorded in the details.

#### Scenario: Tie broken by priority
- **WHEN** tool `a` (priority 1) returns `"x"` and tool `b` (priority 5) returns `"y"`
- **THEN** the value is `"y"`

#### Scenario: Tie broken by name
- **WHEN** tools `zed` and `alpha` with equal priority return `"z"` and `"a"`
- **THEN** the value is `"a"`

#### Scenario: Name tie-break uses every group member
- **WHEN** tools `m` (priority 5) and `a` (priority 0) return `"X"` and tools `b` (priority 5) and `y` (priority 0) return `"Y"`
- **THEN** the value is `"X"` because its group contains `a`, and the details record that a tie was broken

#### Scenario: Priority tie-break precedes name
- **WHEN** tools `a` and `b` (priority 0) return `"X"` and tools `z` (priority 9) and `c` (priority 0) return `"Y"`
- **THEN** the value is `"Y"`

### Requirement: Numeric aggregation
The numeric strategy SHALL aggregate numeric values with `mean` (default), `median`, `min` or `max`. Every participant is a winner; confidence MUST be 1.0 when all values are equal and lower when they differ. Non-numeric values, including booleans, and non-finite values (`inf`, `-inf`, NaN) MUST fail with a conflict error, as MUST an aggregate or a largest-minus-smallest spread that is not a finite number. An unknown aggregation MUST fail with a validation error.

#### Scenario: Mean of values
- **WHEN** tools return `1`, `2` and `6` with aggregation `mean`
- **THEN** the value is `3`

#### Scenario: Median, min and max
- **WHEN** tools return `1`, `2` and `6`
- **THEN** `median` yields `2`, `min` yields `1` and `max` yields `6`

#### Scenario: Non-numeric value
- **WHEN** one tool returns `"many"` under the numeric strategy
- **THEN** a conflict error is raised

#### Scenario: Boolean value
- **WHEN** one tool returns `True` under the numeric strategy
- **THEN** a conflict error is raised

#### Scenario: Non-finite value
- **WHEN** one tool returns `inf`, `-inf` or NaN under the numeric strategy
- **THEN** a conflict error is raised mentioning non-finite values

#### Scenario: Aggregate overflows
- **WHEN** tools return `1e308` and `1e308` with aggregation `mean` or `median`, or `10**400` and `1` with aggregation `mean` or `median`
- **THEN** a conflict error is raised

#### Scenario: Spread overflows
- **WHEN** tools return `-1.7e308` and `1.7e308`, or `10**400` and `1.5`, under any aggregation
- **THEN** a conflict error is raised

#### Scenario: Huge equal integers aggregate exactly
- **WHEN** tools `a` and `b` both return `10**400` with aggregation `mean`
- **THEN** the value is exactly `10**400` and confidence is 1.0
