# Spec Delta

## Purpose

Composes the tool registry, capability routing, permission scoping, parallel execution and conflict resolution into a single agent that runs multi-step plans and one-shot calls on behalf of a principal, with fallback, fan-out reconciliation, pre-flight checks and a unified, observable report.

## ADDED Requirements

### Requirement: Orchestrator configuration
An orchestrator SHALL be created with optional collaborators (registry, permission policy, router, conflict resolver, resource lock manager) and execution settings (concurrency limit, fail-fast, default step timeout, per-tool timeout, maximum fallbacks). Missing collaborators MUST get defaults. Invalid settings, or a router bound to a different registry than the orchestrator's, MUST be rejected with a validation error.

#### Scenario: Defaults
- **WHEN** an orchestrator is created with no arguments
- **THEN** it exposes an empty registry, a policy, a router, a resolver and a lock manager

#### Scenario: Negative fallback limit
- **WHEN** an orchestrator is created with a maximum of -1 fallbacks
- **THEN** a validation error is raised

#### Scenario: Router bound to another registry
- **WHEN** an orchestrator is created with registry `A` and a router built on registry `B`
- **THEN** a validation error is raised

### Requirement: Tool registration
The orchestrator SHALL register tool descriptors into its registry, and SHALL register an async function as a tool through a decorator taking name, version, capabilities, required scopes and input schema. Omitted decorator values MUST default to the function's name for the tool name and the only capability, version `1.0.0`, and the docstring's first line as description. The decorated function MUST remain directly callable.

#### Scenario: Register a descriptor
- **WHEN** a tool descriptor `calc` is registered through the orchestrator
- **THEN** the orchestrator's registry returns it by name

#### Scenario: Decorate a function
- **WHEN** an async function is decorated as tool `math.add` with capability `calc` and scope `calc:use`
- **THEN** the registry holds `math.add` offering `calc` and requiring `calc:use`, and the function can still be awaited directly

#### Scenario: Decorator defaults
- **WHEN** an async function named `echo` with docstring `Echo input.` is decorated without arguments
- **THEN** tool `echo` version `1.0.0` offers capability `echo` and is described as `Echo input.`

### Requirement: Plan execution for a principal
The orchestrator SHALL run a plan, or a sequence of steps forming one, on behalf of a principal, with the dependency-driven parallelism, output references, timeouts, retries, failure propagation and resource locking of plan execution. Each step's tool invocations MUST be authorized for that principal.

#### Scenario: Independent steps overlap and outputs flow
- **WHEN** a plan has two independent steps that each wait for a signal only the other sends, and a third step referencing both outputs
- **THEN** all three succeed and the third step receives both outputs

#### Scenario: Sequence of steps accepted
- **WHEN** a list of steps is run instead of a plan
- **THEN** the steps are validated as a plan and executed

### Requirement: Named-tool steps
A step naming a tool SHALL invoke that tool, honouring a version pin, after validating the step's arguments against the tool's input schema. An unknown tool or version, a disabled tool, or invalid input MUST fail the step without invoking the tool. The step result MUST record the tool and version that served it.

#### Scenario: Pinned version
- **WHEN** versions `1.0.0` and `2.0.0` of `calc` are registered and a step pins `calc` to `1.0.0`
- **THEN** version `1.0.0` serves the step

#### Scenario: Disabled tool
- **WHEN** a step names tool `calc` that is disabled
- **THEN** the step fails and `calc` is not invoked

#### Scenario: Invalid input
- **WHEN** a step calls a tool whose input schema requires integer `a` with `a` set to `"x"`
- **THEN** the step fails with a validation error and the tool is not invoked

### Requirement: Permission enforcement
Before every tool invocation the orchestrator SHALL perform an audited access check of the run's principal against the tool. A denial MUST fail the step with a permission-denied error without invoking the tool, and steps depending on it MUST be skipped.

#### Scenario: Denied tool step
- **WHEN** a principal without scope `fs:write` runs a step naming tool `fs.write` that requires it, and a second step depends on it
- **THEN** the first step fails with a permission-denied error, `fs.write` is not invoked, and the second step is skipped

#### Scenario: Allowed invocation is audited
- **WHEN** a principal holding the required scopes runs a step naming a tool
- **THEN** the policy's audit log gains an allowed record for that principal and tool

### Requirement: Denials known at run start are not retried
A named-tool step whose tool is denied to the principal when the run starts SHALL be attempted exactly once, regardless of its declared retries, and no retry delay MUST be spent on it.

#### Scenario: Retries ignored for a denied step
- **WHEN** a denied named-tool step declaring 3 retries is run
- **THEN** the step fails after exactly 1 attempt and no retry event is emitted for it

### Requirement: Permission-aware capability routing
A step naming a capability SHALL be served by the best-ranked tool offering that capability among the tools the principal is permitted to use; tools the principal may not use MUST never be invoked. When no permitted tool offers the capability, the step MUST fail with a no-route error.

#### Scenario: Unpermitted better tool skipped
- **WHEN** tools `premium` (higher priority, requires scope `paid`) and `basic` offer `search` and a principal without `paid` runs a `search` step
- **THEN** `basic` serves the step and `premium` is not invoked

#### Scenario: No permitted tool
- **WHEN** the only tool offering `search` requires a scope the principal lacks
- **THEN** the step fails with a no-route error

### Requirement: Fallback on tool failure
When the tool serving a capability step raises, times out or returns an unsuccessful result, the orchestrator SHALL report the failure to the router and invoke the next-best permitted candidate not yet tried in this attempt, up to the configured maximum number of fallbacks. When every tried candidate fails, the step MUST fail with an execution error listing each attempted tool and its error.

#### Scenario: Fallback to the next candidate
- **WHEN** the best-ranked `search` tool raises and a second `search` tool succeeds
- **THEN** the step succeeds with the second tool's output, the first tool's failure count increases, and the attempt chain lists both tools in order

#### Scenario: Fallback limit
- **WHEN** three `search` tools all fail and the maximum number of fallbacks is 1
- **THEN** exactly two tools are invoked and the step fails with an execution error naming both

#### Scenario: Fallback disabled
- **WHEN** the maximum number of fallbacks is 0 and the best-ranked tool fails
- **THEN** no other tool is invoked and the step fails

### Requirement: Per-tool timeout
The orchestrator SHALL support a per-tool time budget applied to each individual tool invocation. A capability-step candidate exceeding it MUST count as a failed candidate and trigger fallback, while the step's own timeout continues to bound the whole step.

#### Scenario: Slow tool falls back
- **WHEN** the per-tool timeout is 0.05 seconds, the best-ranked `search` tool takes 1 second and a second `search` tool answers immediately
- **THEN** the step succeeds with the second tool's output and the attempt chain records a timeout for the first tool

### Requirement: Routing health feedback
Every completed tool invocation SHALL be reported to the router: a success with its measured latency, a failure or timeout as a failure. A candidate rejecting the step's arguments as invalid input MUST be skipped without counting against its health.

#### Scenario: Success recorded with latency
- **WHEN** a capability step is served successfully by tool `alpha`
- **THEN** the router's statistics for `alpha` show one success and an observed latency

#### Scenario: Invalid input does not penalise
- **WHEN** the best-ranked `search` tool's input schema rejects the arguments and the next candidate accepts them
- **THEN** the step succeeds via the next candidate and the first tool's failure count is unchanged

### Requirement: Fan-out reconciliation in plans
A run SHALL accept, per capability step, a fan-out setting (number of tools, conflict-resolution strategy and options). Such a step MUST invoke the top-ranked permitted tools up to that number in parallel, reconcile their successful results with the strategy, and use the reconciled value as the step output, keeping the resolution in the step result and run report.

#### Scenario: Majority across three tools
- **WHEN** a `search` step fans out to 3 tools with strategy `majority`, and two tools return `A` while the third returns `B`
- **THEN** the step output is `A` and the resolution names the two agreeing tools as winners and the third as dissenter

#### Scenario: Merge strategy
- **WHEN** a `profile` step fans out to 2 tools with strategy `merge`, returning `{"name": "Ada"}` and `{"email": "ada@example.org"}`
- **THEN** the step output is `{"name": "Ada", "email": "ada@example.org"}`

#### Scenario: Partial failures ignored
- **WHEN** a fan-out step invokes 3 tools and one raises
- **THEN** the step is resolved from the two successful results and the failure is reported to the router

### Requirement: Fan-out failures and validation
A fan-out step whose tools all fail, or whose results the strategy cannot reconcile, SHALL fail with an error. A fan-out setting for an unknown step, for a named-tool step, or with a tool count below 1 MUST be rejected with a validation error before any step runs.

#### Scenario: Unreconcilable results
- **WHEN** a fan-out step uses strategy `majority` with quorum 1.0 and its tools disagree
- **THEN** the step fails with a conflict error

#### Scenario: Fan-out on a named-tool step
- **WHEN** a run requests fan-out for a step naming a tool
- **THEN** a validation error is raised and no tool is invoked

### Requirement: One-shot calls
The orchestrator SHALL offer a single call that runs one tool, or one capability when the target is written `capability:<name>`, for a principal and returns the tool result, raising the step's original error on failure. It SHALL also offer a one-shot question that fans a capability out to N tools and returns the resolution.

#### Scenario: Call a capability
- **WHEN** `capability:calc` is called with `{"a": 2, "b": 3}` and an adding tool serves `calc`
- **THEN** a successful result with value `5` is returned, naming the serving tool

#### Scenario: Denied call raises
- **WHEN** a principal without the required scope calls a tool directly
- **THEN** a permission-denied error is raised

#### Scenario: Ask with fan-out
- **WHEN** a question on capability `search` fans out to 3 tools with strategy `majority`
- **THEN** a resolution with the majority value and its winners is returned

### Requirement: Run report
A run SHALL return a report giving every step's outcome and outputs, plus per step the tools that served it, the ordered chain of attempted tools with each attempt's error, any fan-out resolution, the audit records produced by this run only, the pre-flight issues, the contention warnings and the run's events.

#### Scenario: Report contents after fallback
- **WHEN** a capability step is served by its second candidate after the first fails
- **THEN** the report names the second candidate as serving the step and lists both in the step's attempt chain

#### Scenario: Audit records scoped to the run
- **WHEN** two runs are executed in sequence by the same orchestrator
- **THEN** each report contains only the audit records created during its own run

### Requirement: Pre-flight check
The orchestrator SHALL statically check a plan for a principal and list issues for steps naming an unknown or disabled tool, steps naming a tool the principal may not use, and capability steps no permitted tool can serve, without invoking tools or writing audit records. A run MUST NOT enforce the check by default; when asked to, it MUST raise an error carrying the issues before invoking any tool.

#### Scenario: Issues listed
- **WHEN** a plan has a step naming an unknown tool, a step naming a denied tool and a step on an unservable capability
- **THEN** the check lists one issue for each of those steps and the audit log is unchanged

#### Scenario: Clean plan
- **WHEN** every step of a plan can be routed and is permitted
- **THEN** the check returns no issues

#### Scenario: Enforced pre-flight
- **WHEN** a plan with a denied step is run with pre-flight enforcement
- **THEN** a pre-flight error listing the issue is raised and no tool is invoked

#### Scenario: Default run reports issues
- **WHEN** a plan with a denied step is run without pre-flight enforcement
- **THEN** the run completes and its report lists the pre-flight issue

### Requirement: Tool-declared resource claims
A named-tool step SHALL inherit the resource claims a tool declares in its metadata, with the step's own claim winning for a resource both declare, so the inherited claims are locked during execution and considered for contention warnings.

#### Scenario: Inherited write claim serializes
- **WHEN** a tool declares a write claim on resource `reports` and two independent steps name it
- **THEN** the two invocations never overlap

### Requirement: Contention warnings
A run SHALL warn about every pair of steps that are not ordered by dependency (directly or transitively) and whose resource claims conflict on a resource, because they will serialize. Pairs ordered by dependency MUST NOT be reported.

#### Scenario: Independent writers warned
- **WHEN** independent steps `a` and `b` both claim resource `db` for writing
- **THEN** the report contains a contention warning for `db` naming `a` and `b`

#### Scenario: Dependent writers not warned
- **WHEN** step `b` depends on step `a` and both claim resource `db` for writing
- **THEN** the report contains no contention warning

### Requirement: Dynamic registry reflection
Tools registered, enabled, disabled or unregistered while the orchestrator exists SHALL be reflected in its routing and pre-flight checks on the next routing decision. Unregistering a tool MUST discard its routing health history.

#### Scenario: Newly registered tool routed
- **WHEN** a better-ranked `search` tool is registered after a previous run
- **THEN** the next run's `search` step is served by the new tool

#### Scenario: Disabled tool bypassed
- **WHEN** the best-ranked `search` tool is disabled between two runs
- **THEN** the second run's `search` step is served by the next-best tool

#### Scenario: Health forgotten on unregister
- **WHEN** a tool with recorded failures is unregistered and registered again
- **THEN** its routing statistics show no recorded calls

### Requirement: Unified event stream
The orchestrator SHALL deliver, to an optional listener and in each run report, one ordered stream combining step lifecycle events with orchestrator events: a tool routed to a step, a fallback, a permission denial and a fan-out resolution. A failing listener MUST NOT affect the run.

#### Scenario: Fallback events
- **WHEN** a capability step falls back from tool `flaky` to tool `alpha`
- **THEN** the stream contains, for that step, `started`, `routed` to `flaky`, `fallback` from `flaky`, `routed` to `alpha` and `succeeded`, in that order

#### Scenario: Denial event
- **WHEN** a step is denied
- **THEN** the stream contains a `denied` event naming the step and tool

#### Scenario: Listener failure isolated
- **WHEN** the listener raises on every event
- **THEN** the run still completes successfully

### Requirement: Package interface
The package root SHALL export the public names of the registry, routing, permission, execution, conflict-resolution and orchestrator modules together with a version string.

#### Scenario: Root imports
- **WHEN** a user imports the orchestrator, router, policy, principal, plan, step, reference and resolver names from the package root
- **THEN** the imports succeed and the package version is `0.1.0`

### Requirement: Runnable demonstration
Running the package as a program SHALL execute a deterministic, network-free demonstration of parallel steps, output references, routing with fallback, a permission denial with skip propagation, fan-out reconciliation and delegation, print a readable summary, and exit successfully within two seconds.

#### Scenario: Demo runs
- **WHEN** the package is run as a program
- **THEN** it exits with status 0 within two seconds and its output mentions the fallback, the denial, the skipped step and the reconciled answer
