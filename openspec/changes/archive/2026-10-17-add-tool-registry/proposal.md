# Proposal

## Why

The orchestrator agent needs a single, authoritative place to learn which tools exist and what they can do. Without a dynamic registry, routing, permission scoping, parallel execution and conflict resolution have nothing shared to build on, and tools cannot be added or retired while the agent runs.

## What Changes

- Introduce the core tool model: a tool descriptor carrying a name, semantic version, description, declared capabilities (with cost, latency and priority hints), required permission scopes, an input contract, and an async handler.
- Introduce a uniform tool result envelope and a shared error hierarchy used by every later capability.
- Introduce a dynamic, thread/async-safe tool registry supporting register, unregister, lookup by name (latest or pinned version), listing, capability queries, enable/disable, and change notifications.
- Set up the Python package (`src/orchestrator`), packaging metadata, and test harness.

## Capabilities

### New Capabilities
- `tool-registry`: Describing tools and dynamically registering, versioning, discovering, enabling/disabling and observing them at runtime.

### Modified Capabilities

## Impact

- New package `orchestrator` with modules `core.py` (models, errors) and `registry.py`.
- New `pyproject.toml`; dev dependencies `pytest`, `pytest-asyncio`. No runtime dependencies.
- All later capabilities (routing, permissions, execution, conflicts, agent) import from these modules.
