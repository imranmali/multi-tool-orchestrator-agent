# Design

## Context

`ToolSpec.required_scopes` (a `frozenset[str]`) and `PermissionDeniedError` already exist in `orchestrator.core`, but nothing evaluates them. See proposal.md (Why). Per the project context, this capability lives in its own module (`permissions.py`) and depends only on `orchestrator.core`; it must not import the router or executor. The integrator wires it in: `policy.predicate(principal)` becomes the router's `eligible` filter and `policy.check(principal, tool)` guards execution.

## Goals / Non-Goals

**Goals:**
- A pure, deterministic decision function over immutable inputs (principal, tool) plus a small amount of mutable policy state (roles, rules, audit log).
- Least privilege by construction: delegated principals can never be more powerful than their delegators, even if built by hand rather than via delegation.
- Fail closed on anything malformed.

**Non-Goals:**
- Authentication (who the principal really is) — principals are trusted inputs.
- Argument-level authorization (e.g. which paths `fs:read` may touch) — scopes gate tools, not payloads.
- Role inheritance/hierarchies, deny-by-scope rules, rule persistence, or time-limited grants.

## Decisions

- **Public API** (`orchestrator.permissions`):
  - `validate_scope(scope, *, allow_wildcard=True) -> str`, `scope_matches(granted, required) -> bool`, `scopes_cover(granted, required) -> bool`.
  - `@dataclass(frozen=True) Principal(id, scopes=frozenset(), roles=frozenset(), parent=None)` with `narrow(scopes, *, id=None) -> Principal` and a `chain` helper (self → root).
  - `PermissionPolicy(roles=None, *, audit_limit=1000, clock=time.time)` with `define_role`, `roles`, `effective_scopes`, `delegate(principal, scopes, *, id=None)`, `deny`, `allow_tool`, `remove_rule`, `decide`, `check`, `allowed`, `filter_tools`, `predicate`, `audit_log`, `clear_audit_log`.
  - `AuditRecord` (frozen dataclass) and `Decision` (frozen; `allowed`, `missing`, `reason`).
  - `AccessDeniedError(PermissionDeniedError)` carrying `principal_id`, `tool`, `version`, `missing`, `reason`; raised by `check` and by delegation. Subclassing keeps `except PermissionDeniedError` working while giving callers structured fields, without touching `core.py`.
- **Scope grammar via regex on segments** — segment `[A-Za-z0-9_.-]+`, joined by `:`. Wildcards are only `*` or a trailing `:*`, which keeps matching a simple prefix test at a segment boundary (`required.startswith(prefix + ":")`) and makes the set algebra below exact. Alternative: fnmatch-style globs (rejected: `fs*` would match `fsx:read`, and intersection of arbitrary globs is not closed).
- **Required scopes never contain wildcards** — a tool demanding `fs:*` is ambiguous ("any fs scope" vs "all fs scopes"); treating it as malformed and failing closed avoids guessing.
- **Delegation bound computed as a scope-set intersection** — with this grammar, two grants are either nested (one covers the other) or disjoint, so `intersect(A, B) = {a ∈ A covered by B} ∪ {b ∈ B covered by A}` is exact. `effective_scopes(child) = intersect(own ∪ roles, effective_scopes(parent))`, applied recursively. This bounds hand-built principals with a `parent`, not just those made by `narrow`. Alternative: only check at `narrow` time (rejected: roles added to a child, or a manually constructed child, would escape the bound).
- **`narrow` raises rather than silently dropping** — an escalation attempt is a bug in the delegating agent and should surface immediately. `Principal.narrow` can only see the delegator's own scopes (no policy), so `PermissionPolicy.delegate` exists to validate against effective scopes including roles. Child ids default to `"<parent>/delegate"`; children start with no roles.
- **Decision order** in `decide`: (1) explicit deny on the principal or any ancestor → deny; (2) malformed required scope → deny; (3) all required scopes covered by effective scopes → allow; (4) explicit allow for the principal and the parent (if any) is allowed → allow; else deny with the uncovered scopes as `missing`. Rule subjects match id or role name in a single namespace — simple, and documented; callers who need disambiguation can prefix role names.
- **Auditing only on `check`/`allowed`** — `predicate` and `filter_tools` call the non-auditing `decide` because the router evaluates every candidate on every request and would flush meaningful entries out of the bounded log. A `collections.deque(maxlen=audit_limit)` gives O(1) bounded appends; `audit_limit=0` disables auditing. `clock` is injectable for deterministic timestamps.
- **`threading.RLock`** guards roles, rules and the deque. `decide` snapshots role/rule state under the lock and never calls user code while holding it; principals and tools are immutable so they need no locking.
- **`predicate` wraps `decide` in `try/except Exception: return False`** — the router contract requires a plain bool function, and any unexpected input (e.g. a non-`ToolSpec`) fails closed.

## Risks / Trade-offs

- [Id/role namespace collision in rules: a role named like a principal id matches both] → Documented in the API; the integrator can adopt a `role:` naming convention.
- [Effective scopes are recomputed per decision (walks the delegation chain)] → Chains are short and scope sets tiny; caching would complicate invalidation when roles change.
- [Explicit allow can grant tools beyond scopes for root principals] → Intentional escape hatch; it is still bounded for delegates and always overridden by deny.
- [Audit log is in-memory only] → Bounded and copyable; persistence can subscribe later by polling `audit_log`.
