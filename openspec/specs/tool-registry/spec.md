# tool-registry Specification

## Purpose
Provides the authoritative, runtime-mutable catalog of tools the orchestrator can use, including each tool's declared capabilities, required permission scopes and version.

## Requirements

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

### Requirement: Dynamic registration
The registry SHALL allow tools to be registered and unregistered at any time while the system runs. Registering the same name and version twice MUST fail with a duplicate-tool error unless replacement is explicitly requested.

#### Scenario: Register then look up
- **WHEN** tool `calc` version `1.0.0` is registered
- **THEN** looking up `calc` returns that tool

#### Scenario: Duplicate registration
- **WHEN** tool `calc` version `1.0.0` is registered twice without requesting replacement
- **THEN** the second registration fails with a duplicate-tool error

#### Scenario: Unregister
- **WHEN** tool `calc` is unregistered
- **THEN** looking up `calc` fails with a tool-not-found error

### Requirement: Version resolution
The registry SHALL hold multiple versions of a tool name concurrently. Lookup without a version MUST return the highest semantic version; lookup with an exact version MUST return that version or fail with tool-not-found.

#### Scenario: Latest version wins
- **WHEN** `calc` versions `1.0.0`, `1.10.0` and `1.2.0` are registered and `calc` is looked up without a version
- **THEN** version `1.10.0` is returned

#### Scenario: Pinned version
- **WHEN** `calc` is looked up with version `1.0.0`
- **THEN** version `1.0.0` is returned

#### Scenario: Unregister a single version
- **WHEN** only `calc` version `1.10.0` is unregistered
- **THEN** a lookup without a version returns `1.2.0`

### Requirement: Capability discovery
The registry SHALL return every enabled tool (latest version per name) that declares a given capability, and SHALL list all capability names currently offered.

#### Scenario: Find by capability
- **WHEN** tools `a` and `b` declare `search` and tool `c` declares `math`
- **THEN** querying capability `search` returns exactly `a` and `b`

#### Scenario: Unknown capability
- **WHEN** querying capability `teleport` that no tool declares
- **THEN** an empty result is returned

### Requirement: Enable and disable tools
The registry SHALL allow a tool to be disabled without unregistering it, either one version at a time or as a whole tool name. A version MUST count as enabled only when neither that version nor its tool name is disabled. Disabled tools MUST be excluded from capability discovery but remain retrievable by explicit lookup, and can be re-enabled.

#### Scenario: Disabled tool hidden from discovery
- **WHEN** tool `a` declaring `search` is disabled
- **THEN** querying capability `search` does not return `a`

#### Scenario: Re-enable
- **WHEN** a disabled tool `a` is enabled again
- **THEN** querying capability `search` returns `a`

#### Scenario: Version-level disable
- **WHEN** `calc` versions `1.0.0` and `2.0.0` are registered and only version `1.0.0` is disabled
- **THEN** version `1.0.0` reports disabled, version `2.0.0` reports enabled and is still discovered

#### Scenario: Enabling a version does not override a name-level disable
- **WHEN** `calc` versions `1.0.0` and `2.0.0` are registered, `calc` is disabled without a version, and then version `1.0.0` is enabled
- **THEN** both versions still report disabled until `calc` is enabled without a version

### Requirement: Registry change notifications
The registry SHALL notify subscribed listeners of register, unregister, enable and disable events with the event kind and tool identity. Each enable or disable call MUST emit exactly one event, naming the given version, or the latest version when the call names only the tool. A failing listener MUST NOT prevent the registry operation or other listeners.

#### Scenario: Listener receives registration
- **WHEN** a listener is subscribed and tool `a` is registered
- **THEN** the listener receives a `registered` event naming `a` and its version

#### Scenario: Failing listener isolated
- **WHEN** one listener raises an exception during a registration
- **THEN** the tool is still registered and the other listeners are still notified

#### Scenario: Whole-tool disable emits one event
- **WHEN** `calc` versions `1.0.0` and `2.0.0` are registered and `calc` is disabled without a version
- **THEN** the listener receives exactly one `disabled` event, naming `calc` and version `2.0.0`

### Requirement: Name-level disablement covers every version
Disabling a tool name without a version SHALL disable every version registered under that name, including versions registered while the name stays disabled. Enabling the name without a version SHALL clear the name-level disablement and every per-version disablement of that name. Unregistering every version of a name MUST forget both kinds of disablement for that name.

#### Scenario: Pinned older version disabled
- **WHEN** `calc` versions `1.0.0` and `2.0.0` are registered and `calc` is disabled without a version
- **THEN** looking up whether version `1.0.0` is enabled reports disabled, and a lookup of version `1.0.0` still returns it

#### Scenario: Later version stays disabled
- **WHEN** `calc` is disabled without a version and version `3.0.0` is registered afterwards
- **THEN** version `3.0.0` reports disabled and capability discovery does not return `calc`

#### Scenario: Enabling the name clears every flag
- **WHEN** version `1.0.0` of `calc` is disabled, then `calc` is disabled and enabled again without a version
- **THEN** every version of `calc`, including `1.0.0`, reports enabled

#### Scenario: Full unregister forgets the disablement
- **WHEN** `calc` is disabled without a version, every version is unregistered and `calc` is registered again
- **THEN** the newly registered `calc` reports enabled
