# Spec Delta

## Purpose

Provides the authoritative, runtime-mutable catalog of tools the orchestrator can use, including each tool's declared capabilities, required permission scopes and version.

## ADDED Requirements

### Requirement: Tool descriptor validation
A tool descriptor SHALL declare a non-empty name matching `[a-z0-9][a-z0-9_.-]*`, a semantic version `MAJOR.MINOR.PATCH`, at least one capability, and an async handler. Invalid descriptors MUST be rejected with a validation error at construction time.

#### Scenario: Valid descriptor
- **WHEN** a tool is defined with name `web.search`, version `1.2.0`, capability `search` and an async handler
- **THEN** the descriptor is created and exposes those values

#### Scenario: Missing capabilities
- **WHEN** a tool is defined with an empty capability list
- **THEN** a validation error is raised

#### Scenario: Bad version string
- **WHEN** a tool is defined with version `1.2`
- **THEN** a validation error is raised

### Requirement: Capability declarations carry routing hints
Each declared capability SHALL carry a name plus non-negative cost and latency hints and an integer priority, with defaults of cost 1.0, latency 1.0 and priority 0 when omitted.

#### Scenario: Default hints
- **WHEN** a capability is declared only by name `summarize`
- **THEN** its cost is 1.0, latency is 1.0 and priority is 0

#### Scenario: Negative cost rejected
- **WHEN** a capability is declared with cost -1
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
The registry SHALL allow a tool to be disabled without unregistering it. Disabled tools MUST be excluded from capability discovery but remain retrievable by explicit lookup, and can be re-enabled.

#### Scenario: Disabled tool hidden from discovery
- **WHEN** tool `a` declaring `search` is disabled
- **THEN** querying capability `search` does not return `a`

#### Scenario: Re-enable
- **WHEN** a disabled tool `a` is enabled again
- **THEN** querying capability `search` returns `a`

### Requirement: Registry change notifications
The registry SHALL notify subscribed listeners of register, unregister, enable and disable events with the event kind and tool identity. A failing listener MUST NOT prevent the registry operation or other listeners.

#### Scenario: Listener receives registration
- **WHEN** a listener is subscribed and tool `a` is registered
- **THEN** the listener receives a `registered` event naming `a` and its version

#### Scenario: Failing listener isolated
- **WHEN** one listener raises an exception during a registration
- **THEN** the tool is still registered and the other listeners are still notified
