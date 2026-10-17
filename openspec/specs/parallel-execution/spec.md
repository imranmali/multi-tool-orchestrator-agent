# parallel-execution Specification

## Purpose
Runs a plan of interdependent tool calls concurrently and safely: steps start as soon as their inputs are available, within concurrency, timeout, retry and resource-locking limits, and every step's outcome is reported.

## Requirements

### Requirement: Step definition
A plan step SHALL have an identifier and target exactly one of a named tool (optionally pinned to a version) or a capability, together with arguments, dependencies, an optional timeout, a non-negative retry count and backoff, and optional resource claims whose access mode is `read` or `write`. A step violating these rules MUST be rejected with a validation error.

#### Scenario: Tool step
- **WHEN** a step `a` is defined targeting tool `calc` with arguments `{"x": 1}`
- **THEN** the step is created and exposes its id, tool and arguments

#### Scenario: Neither tool nor capability
- **WHEN** a step is defined without a tool and without a capability
- **THEN** a validation error is raised

#### Scenario: Both tool and capability
- **WHEN** a step is defined with tool `calc` and capability `math`
- **THEN** a validation error is raised

#### Scenario: Invalid resource mode
- **WHEN** a step declares resource `db` with mode `append`
- **THEN** a validation error is raised

#### Scenario: Negative retries
- **WHEN** a step is defined with retries -1
- **THEN** a validation error is raised

### Requirement: Plan validation
A plan SHALL be validated when it is created. Duplicate step ids and dependencies on unknown steps MUST raise a validation error, and a dependency cycle MUST raise a cycle error whose message names the steps forming the cycle.

#### Scenario: Duplicate step id
- **WHEN** a plan is created with two steps both named `a`
- **THEN** a validation error is raised

#### Scenario: Unknown dependency
- **WHEN** a plan contains step `b` depending on step `zzz` that is not in the plan
- **THEN** a validation error is raised

#### Scenario: Cycle detected
- **WHEN** a plan contains steps `a` depending on `c`, `b` depending on `a`, and `c` depending on `b`
- **THEN** a cycle error is raised whose message names `a`, `b` and `c`

### Requirement: Plan ordering
A valid plan SHALL expose its topological layers, where every step appears in a later layer than all of its dependencies and as early as possible, and a flat execution order consistent with those layers. Steps within a layer MUST keep plan declaration order.

#### Scenario: Diamond layers
- **WHEN** a plan has `a`, `b` and `c` depending on `a`, and `d` depending on `b` and `c`
- **THEN** its layers are `[a]`, `[b, c]`, `[d]` and its order is `a, b, c, d`

### Requirement: Output references
Step arguments SHALL accept references to an earlier step's output, optionally narrowed by a dotted path of mapping keys and sequence indexes, nested anywhere inside argument lists and mappings. A referenced step MUST be treated as a dependency even if not declared. A path that cannot be resolved MUST fail the referencing step.

#### Scenario: Whole output reference
- **WHEN** step `b` has argument `x` referencing the output of step `a`, which returned `5`
- **THEN** `b` is invoked with `x` equal to `5`

#### Scenario: Nested dotted reference
- **WHEN** step `a` returns `{"items": [{"title": "T"}]}` and step `b` has argument `{"titles": [ref(a, "items.0.title")]}`
- **THEN** `b` is invoked with `{"titles": ["T"]}`

#### Scenario: Implicit dependency
- **WHEN** step `b` references step `a` without listing `a` in its dependencies
- **THEN** the plan treats `a` as a dependency of `b` and runs `a` first

#### Scenario: Unresolvable path
- **WHEN** step `b` references path `missing` of step `a` whose output has no such key
- **THEN** step `b` is reported as failed and is not invoked

### Requirement: Dependency-driven parallel execution
The executor SHALL start each step as soon as all of its dependencies have succeeded, without waiting for unrelated steps or whole layers, so independent steps run concurrently. A step MUST NOT start before every dependency has succeeded.

#### Scenario: Independent steps overlap
- **WHEN** two independent steps each wait for a signal that only the other one sends
- **THEN** both complete successfully, proving they ran concurrently

#### Scenario: Wall-clock speedup
- **WHEN** three independent steps each take 50 ms
- **THEN** the whole plan completes in well under 150 ms

#### Scenario: Dependency ordering
- **WHEN** step `b` depends on step `a`
- **THEN** `b` starts only after `a` has finished successfully

#### Scenario: No layer barrier
- **WHEN** step `a` is slow, step `b` is fast, and step `c` depends only on `b`
- **THEN** `c` starts and finishes before `a` finishes

### Requirement: Concurrency limit
The executor SHALL never have more step invocations in flight at once than its configured concurrency limit.

#### Scenario: Limit respected
- **WHEN** six independent steps run with a concurrency limit of 2
- **THEN** at most two invocations are in flight at any moment and all six succeed

### Requirement: Step timeout
Each step invocation SHALL be bounded by the step's own timeout or, if unset, the executor's default timeout. An invocation exceeding it MUST be abandoned and recorded as a timeout failure.

#### Scenario: Step times out
- **WHEN** a step with a 10 ms timeout invokes a tool that takes 1 s
- **THEN** the step is reported as failed with a timeout error

#### Scenario: Default timeout applies
- **WHEN** a step without its own timeout runs on an executor whose default timeout is 10 ms and the tool takes 1 s
- **THEN** the step is reported as failed with a timeout error

### Requirement: Step retries
A failed attempt SHALL be retried up to the step's retry count, waiting the step's backoff multiplied by two to the power of the zero-based retry number before each retry. Raised errors, timeouts and unsuccessful tool results MUST all count as failed attempts. The report MUST record the number of attempts made.

#### Scenario: Success after failure
- **WHEN** a step with 2 retries fails on its first attempt and succeeds on its second
- **THEN** the step is reported as succeeded with 2 attempts

#### Scenario: Retries exhausted
- **WHEN** a step with 1 retry fails on every attempt
- **THEN** the step is reported as failed with 2 attempts

#### Scenario: Unsuccessful result retried
- **WHEN** a step with 1 retry first receives an unsuccessful tool result and then a successful one
- **THEN** the step is reported as succeeded with 2 attempts

### Requirement: Failure propagation
When a step fails, every step that depends on it directly or transitively SHALL be reported as skipped and MUST NOT be invoked. By default, steps that do not depend on the failed step MUST still run to completion.

#### Scenario: Dependents skipped
- **WHEN** step `a` fails, `b` depends on `a`, `c` depends on `b`, and `d` is independent
- **THEN** `b` and `c` are skipped and never invoked, and `d` succeeds

### Requirement: Fail-fast mode
In fail-fast mode, the first step failure SHALL cancel all steps still running and mark them, and every step not yet started that is not a dependent of a failed step, as cancelled.

#### Scenario: Running and pending steps cancelled
- **WHEN** in fail-fast mode step `a` fails quickly while independent step `slow` is running and `after` depends on `slow`
- **THEN** `a` is failed, `slow` and `after` are cancelled, and `slow` does not run to completion

### Requirement: Resource locking hook
When a resource lock manager is configured and a step declares resource claims, the executor SHALL hold the lock manager's lock for those claims, identified by the step id, for the whole duration of each invocation attempt. Steps without claims MUST NOT touch the lock manager.

#### Scenario: Lock held around invocation
- **WHEN** a step with claim `db: write` runs on an executor with a lock manager
- **THEN** the lock manager is asked to hold `{db: write}` for that step id and the invocation happens while the lock is held

#### Scenario: No claims, no lock
- **WHEN** a step without resource claims runs on an executor with a lock manager
- **THEN** the lock manager is not called

### Requirement: Pluggable invocation
The executor SHALL delegate every tool call to a configurable invoker that receives the step and its resolved arguments. The default invoker MUST look the tool up in the tool registry (honoring a pinned version), refuse disabled tools, validate the arguments against the tool's input contract, call it and wrap its return value in a successful tool result; capability-only steps MUST fail under the default invoker because routing is not performed by the executor.

#### Scenario: Default invoker calls registry tool
- **WHEN** tool `calc@1.0.0` is registered and a step targets `calc` with valid arguments using the default invoker
- **THEN** the step succeeds and its output is the tool's return value

#### Scenario: Default invoker validates input
- **WHEN** a step's arguments miss a key required by the tool's input contract
- **THEN** the step fails with a validation message

#### Scenario: Default invoker refuses disabled tool
- **WHEN** a step targets a tool that is registered but disabled, using the default invoker
- **THEN** the step fails and the tool is not called

#### Scenario: Capability step without routing
- **WHEN** a capability-only step runs with the default invoker
- **THEN** the step fails with an execution error stating routing is required

### Requirement: Execution events
The executor SHALL notify an optional event callback when a step starts, succeeds, fails, is retried, is skipped or is cancelled. A failing callback MUST NOT affect execution.

#### Scenario: Event sequence
- **WHEN** a step fails once and then succeeds on retry
- **THEN** the callback receives started, retrying and succeeded events for that step in that order

#### Scenario: Callback errors ignored
- **WHEN** the event callback raises on every event
- **THEN** the plan still completes successfully

### Requirement: Execution report
Running a plan SHALL return a report containing, for every step, its status (succeeded, failed, skipped or cancelled), tool result, error message, attempt count and start/finish times, plus overall success, the outputs of succeeded steps, the failed and skipped step ids and total duration. The report MUST be able to raise an execution error summarizing failures.

#### Scenario: Successful report
- **WHEN** a plan of two steps both succeed
- **THEN** the report is ok, its outputs map each step id to its value, and raising on failure does nothing

#### Scenario: Failed report raises
- **WHEN** a step `a` fails
- **THEN** the report is not ok, lists `a` as failed, and raising on failure raises an execution error naming `a`
