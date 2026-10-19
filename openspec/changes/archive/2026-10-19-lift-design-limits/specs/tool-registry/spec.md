## MODIFIED Requirements

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

## ADDED Requirements

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
