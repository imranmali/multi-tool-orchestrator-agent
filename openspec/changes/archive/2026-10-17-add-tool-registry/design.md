# Design

## Context

Greenfield repository. See proposal.md for motivation. Constraints come from the project context: Python 3.11+, stdlib-only runtime, async handlers, deterministic tests.

## Goals / Non-Goals

**Goals:**
- Small, immutable data model that every later module can depend on without circular imports.
- A registry that is safe to mutate while other coroutines/threads read it.

**Non-Goals:**
- Persisting the registry or discovering tools over the network (plugins can register at runtime via the API).
- JSON-Schema-complete input validation: inputs are checked only for required keys and simple Python types.

## Decisions

- **Frozen dataclasses for `Capability`, `ToolSpec`, `ToolResult`** — immutability makes specs safe to share across concurrent tasks. Alternative: pydantic (rejected: runtime dependency).
- **Errors in `core.py`** — one hierarchy rooted at `OrchestratorError` (`ValidationError`, `ToolNotFoundError`, `DuplicateToolError`, `PermissionDeniedError`, `NoRouteError`, `ExecutionError`, `ToolTimeoutError`, `ConflictError`, `CycleError`) so later modules raise shared types without importing each other.
- **Versions parsed to tuples** — `"1.10.0"` → `(1,10,0)` for correct ordering; string comparison would put `1.10.0` below `1.2.0`.
- **`threading.RLock` guarding a `dict[name, dict[version, entry]]`** — works from both sync and async code; operations are O(1)/O(n) and never await while holding the lock.
- **Listeners are sync callables invoked after the lock is released**, each wrapped in try/except, so a slow or failing listener cannot deadlock or break the registry.
- **`input_schema` is a `dict[str, type]` of required keys** with `ToolSpec.validate_input(dict)` — enough for routing/execution tests without a schema library.

## Risks / Trade-offs

- [Simple input contract can't express nested schemas] → Keep `validate_input` overridable; richer schemas can come in a later change.
- [Listeners run synchronously on the mutating thread] → Documented; listeners must be cheap.
