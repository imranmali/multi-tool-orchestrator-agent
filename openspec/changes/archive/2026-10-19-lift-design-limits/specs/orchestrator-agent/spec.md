## MODIFIED Requirements

### Requirement: Named-tool steps
A step naming a tool SHALL invoke that tool, honouring a version pin, after validating the step's arguments against the tool's input schema. An unknown tool or version, a disabled tool or version (including a pinned version of a tool disabled as a whole), or invalid input MUST fail the step without invoking the tool. The step result MUST record the tool and version that served it.

#### Scenario: Pinned version
- **WHEN** versions `1.0.0` and `2.0.0` of `calc` are registered and a step pins `calc` to `1.0.0`
- **THEN** version `1.0.0` serves the step

#### Scenario: Disabled tool
- **WHEN** a step names tool `calc` that is disabled
- **THEN** the step fails and `calc` is not invoked

#### Scenario: Pinned version of a disabled tool
- **WHEN** versions `1.0.0` and `2.0.0` of `calc` are registered, `calc` is disabled without a version, and a step pins `calc` to `1.0.0`
- **THEN** the step fails with an execution error and version `1.0.0` is not invoked

#### Scenario: Invalid input
- **WHEN** a step calls a tool whose input schema requires integer `a` with `a` set to `"x"`
- **THEN** the step fails with a validation error and the tool is not invoked

## ADDED Requirements

### Requirement: Availability re-checked before every invocation
Immediately before invoking a tool for a named-tool step, a capability-step candidate or a fan-out member, the orchestrator SHALL re-check that the exact descriptor about to run is still registered under its name and version and is enabled. A tool failing this check MUST NOT be invoked or reported to the router, and MUST be recorded in the step's attempt chain with outcome `unavailable`.

#### Scenario: Tool disabled once routed
- **WHEN** an event listener disables tool `calc` as soon as it is routed to a named-tool step
- **THEN** `calc` is not invoked, the step fails with an execution error and its attempt chain records `calc` with outcome `unavailable`

#### Scenario: Fan-out member disabled mid-run
- **WHEN** a `search` step fans out to tools `a`, `b` and `c`, and `a`'s handler disables `c` before `c` has started
- **THEN** `c` is not invoked and its attempt has outcome `unavailable`

#### Scenario: Fallback candidate disabled mid-step
- **WHEN** the best-ranked `search` tool disables the second-ranked `search` tool and then fails, and a third `search` tool succeeds
- **THEN** the second-ranked tool is never invoked and the third tool serves the step

### Requirement: Handling of an unavailable tool
A tool found unavailable at invocation SHALL fail the current attempt of a named-tool step with an execution error. In a capability step it SHALL be skipped in favour of the next candidate without counting toward the fallback limit or against its routing health. In a fan-out step it SHALL count as a failed member, so the step is reconciled from the other members' results.

#### Scenario: Unavailable candidate skipped
- **WHEN** the maximum number of fallbacks is 0 and an event listener disables tool `alpha` as soon as it is routed to a `search` step that tool `beta` can also serve
- **THEN** `beta` serves the step, `alpha` is not invoked and `alpha`'s routing statistics record no call

#### Scenario: Fan-out reconciled without the unavailable member
- **WHEN** a `search` fan-out to 3 tools with strategy `majority` loses its third member to a mid-run disable and the other two members both return `A`
- **THEN** the step output is `A` and the third member is not among the winners

### Requirement: Resource claims verified at invocation
Immediately before invoking the tool of a named-tool step, the orchestrator SHALL merge the resource claims of the descriptor about to run with the step's own claims and verify that the locks the step holds cover them, a held write covering a read. If they do not, the tool MUST NOT be invoked and the attempt MUST fail with an execution error recorded with outcome `claims_changed`.

#### Scenario: Tool replaced with a new claim
- **WHEN** a named-tool step targets tool `report`, which declares no resource claims when the run starts, and an earlier step replaces `report` with a descriptor declaring a write claim on resource `db`
- **THEN** `report` is not invoked, the step fails with an execution error and its attempt has outcome `claims_changed`

#### Scenario: Covered claims still run
- **WHEN** a named-tool step targets tool `report`, which declares a write claim on `db` when the run starts, and an earlier step replaces it with a descriptor declaring a read claim on `db`
- **THEN** the replacement descriptor is invoked and the step succeeds
