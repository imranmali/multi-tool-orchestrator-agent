# Proposal

## Why

Every tool descriptor already declares the permission scopes it requires, but nothing enforces them: any caller can invoke any registered tool. An orchestrator that delegates work to sub-agents needs a least-privilege guard that decides, before routing or execution, whether a given principal may use a given tool, and that leaves an audit trail of those decisions.

## What Changes

- Introduce a `resource:action` scope grammar (e.g. `fs:read`, `net:http:get`) with validation, plus wildcard grants: `*` (everything) and prefix wildcards such as `fs:*` that match only at segment boundaries.
- Introduce immutable principals carrying their own scopes and roles, and delegation (`narrow`) that derives a child principal whose authority can never exceed its parent's; requesting a scope the parent does not hold is rejected.
- Introduce a permission policy holding named roles, computing a principal's effective scopes (own ∪ role scopes, bounded by every delegating ancestor), and evaluating tool access.
- Add explicit per-tool allow and deny rules keyed by principal id or role, where an explicit deny always wins over any scope grant.
- Add access decisions in three forms: a raising check that names the tool and the missing scopes, a boolean query, and a non-raising, non-auditing predicate suitable as a router eligibility filter; plus filtering a list of tools.
- Record every check/boolean decision as an immutable audit record in a bounded, size-configurable log that drops the oldest entries first.

## Capabilities

### New Capabilities
- `permission-scoping`: Scope grammar and wildcard matching, principals with roles and least-privilege delegation, explicit tool allow/deny rules, access decisions, and the access-decision audit trail.

### Modified Capabilities

## Impact

- New module `src/orchestrator/permissions.py` depending only on `orchestrator.core` (`ToolSpec`, `ValidationError`, `PermissionDeniedError`).
- New tests `tests/test_permissions.py`.
- No changes to existing public APIs; the agent/router integrator can pass the policy's predicate as a routing eligibility filter and call the raising check before execution. No new dependencies.
