## MODIFIED Requirements

### Requirement: Tool descriptor validation
A tool descriptor SHALL declare a non-empty name matching `[a-z0-9][a-z0-9_.-]*`, a semantic version `MAJOR.MINOR.PATCH`, at least one capability, and an async handler. Invalid descriptors MUST be rejected with a validation error at construction time. Names, capability names and versions MUST match their whole pattern, so a trailing newline or non-ASCII digit is rejected.

#### Scenario: Valid descriptor
- **WHEN** a tool is defined with name `web.search`, version `1.2.0`, capability `search` and an async handler
- **THEN** the descriptor is created and exposes those values

#### Scenario: Missing capabilities
- **WHEN** a tool is defined with an empty capability list
- **THEN** a validation error is raised

#### Scenario: Bad version string
- **WHEN** a tool is defined with version `1.2`
- **THEN** a validation error is raised

#### Scenario: Trailing newline in name rejected
- **WHEN** a tool is defined with name `"calc\n"`
- **THEN** a validation error is raised

#### Scenario: Trailing newline in version rejected
- **WHEN** a tool is defined with version `"1.0.0\n"`
- **THEN** a validation error is raised

#### Scenario: Trailing newline in capability name rejected
- **WHEN** a capability is declared with name `"search\n"`
- **THEN** a validation error is raised

### Requirement: Capability declarations carry routing hints
Each declared capability SHALL carry a name plus non-negative cost and latency hints and an integer priority, with defaults of cost 1.0, latency 1.0 and priority 0 when omitted. A cost or latency that is negative, NaN, boolean or not a number MUST be rejected with a validation error, while positive infinity MUST be accepted. A priority that is a boolean or not an integer MUST be rejected with a validation error.

#### Scenario: Default hints
- **WHEN** a capability is declared only by name `summarize`
- **THEN** its cost is 1.0, latency is 1.0 and priority is 0

#### Scenario: Negative cost rejected
- **WHEN** a capability is declared with cost -1
- **THEN** a validation error is raised

#### Scenario: NaN hint rejected
- **WHEN** a capability is declared with cost NaN or latency NaN
- **THEN** a validation error is raised

#### Scenario: Infinite hint accepted
- **WHEN** a capability is declared with an infinite cost or an infinite latency
- **THEN** the capability is created with that infinite hint

#### Scenario: Boolean priority rejected
- **WHEN** a capability is declared with priority `True` or priority `1.0`
- **THEN** a validation error is raised
