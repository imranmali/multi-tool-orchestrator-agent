# Tasks

## 1. Scope grammar

- [x] 1.1 Implement `validate_scope`, `scope_matches` and `scopes_cover` in `src/orchestrator/permissions.py` (segment regex, `*` and trailing `:*` grants only, no wildcards in required scopes); verify the "Scope syntax validation" and "Scope matching with wildcards" scenarios in `tests/test_permissions.py`

## 2. Principals and delegation

- [x] 2.1 Implement the frozen `Principal` dataclass (id/scope/role validation, iterable normalisation to frozensets, rejection of bare strings) and `AccessDeniedError`; verify the "Principals carry identity, scopes and roles" scenarios in `tests/test_permissions.py`
- [x] 2.2 Implement `Principal.narrow` (raises on uncovered scopes, default child id, parent link) and the exact scope-set intersection helper; verify the direct-delegation scenarios of "Least-privilege delegation" in `tests/test_permissions.py`

## 3. Policy evaluation

- [x] 3.1 Implement `PermissionPolicy` roles (`define_role`, constructor mapping), `effective_scopes` with recursive parent bounding, and `delegate`; verify the "Roles grant named scope bundles", policy-delegation and "Delegated authority never exceeds the delegator" scenarios in `tests/test_permissions.py`
- [x] 3.2 Implement explicit `deny` / `allow_tool` / `remove_rule` rules and `decide` (deny on self or ancestor, fail closed on malformed required scopes, scope coverage, parent-bounded allow); verify the "Scope-based tool access" and "Explicit tool rules" scenarios in `tests/test_permissions.py`
- [x] 3.3 Implement `check`, `allowed`, `filter_tools` and the non-raising `predicate`; verify the "Access decision forms" scenarios in `tests/test_permissions.py`

## 4. Audit and concurrency

- [x] 4.1 Implement `AuditRecord`, the bounded `deque` audit log with injectable clock, `audit_log` copy and `clear_audit_log`, auditing only from `check`/`allowed`; verify the "Decision audit trail" scenarios in `tests/test_permissions.py`
- [x] 4.2 Guard roles, rules and the audit log with an `RLock`; verify the "Concurrent use" scenario (8 threads × 50 queries → 400 records) in `tests/test_permissions.py`
- [x] 4.3 Document the module's public API and the predicate-does-not-audit choice in module/class docstrings; verify `python3 -c "import orchestrator.permissions as p; help(p)"` shows them and the full suite `python3 -m pytest -q tests/test_permissions.py` passes

## Workflow follow-up

- Integrator: export the public names from `orchestrator/__init__.py` and wire `policy.predicate(principal)` into the router's `eligible` filter and `policy.check` before execution.
- Archive the change with `openspec archive add-permission-scoping -y` once tests pass.
- Verify with `openspec list --specs` and `openspec validate permission-scoping --type spec --strict`.
