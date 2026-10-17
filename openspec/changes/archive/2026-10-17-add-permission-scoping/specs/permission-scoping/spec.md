# Spec Delta

## Purpose

Decides whether a principal (a user, agent or delegated sub-agent) may invoke a tool, based on the permission scopes the tool requires, the scopes and roles granted to the principal, explicit per-tool rules and least-privilege delegation, and keeps an audit trail of those decisions.

## ADDED Requirements

### Requirement: Scope syntax validation
A scope SHALL be a string of one or more `:`-separated segments, each segment non-empty and made of letters, digits, `_`, `.` or `-` (e.g. `fs:read`, `net:http:get`). A granted scope MAY additionally be `*` or end with a final `*` segment (e.g. `fs:*`); a wildcard anywhere else, or in a scope a tool requires, is malformed. Malformed or empty scopes MUST be rejected with a validation error.

#### Scenario: Well-formed scopes accepted
- **WHEN** scopes `fs:read`, `net:http:get`, `fs:*` and `*` are granted to a principal
- **THEN** the principal is created with exactly those scopes

#### Scenario: Empty scope rejected
- **WHEN** a principal is granted the scope `` (empty string)
- **THEN** a validation error is raised

#### Scenario: Empty segment rejected
- **WHEN** a principal is granted the scope `fs::read` or `fs:`
- **THEN** a validation error is raised

#### Scenario: Misplaced wildcard rejected
- **WHEN** a principal is granted the scope `fs:*:read` or `f*`
- **THEN** a validation error is raised

#### Scenario: Wildcard not allowed as a required scope
- **WHEN** a required scope `fs:*` is validated as a requirement
- **THEN** a validation error is raised

### Requirement: Scope matching with wildcards
A granted scope SHALL cover a required scope when they are equal, when the grant is `*`, or when the grant is `<prefix>:*` and the required scope starts with `<prefix>:` at a segment boundary. A grant MUST NOT cover any other scope; in particular a non-wildcard grant does not cover longer scopes beneath it.

#### Scenario: Exact match
- **WHEN** grant `fs:read` is matched against required scope `fs:read`
- **THEN** the grant covers it

#### Scenario: Global wildcard
- **WHEN** grant `*` is matched against required scope `net:http:get`
- **THEN** the grant covers it

#### Scenario: Prefix wildcard covers nested scopes
- **WHEN** grant `fs:*` is matched against required scopes `fs:read` and `fs:read:secret`
- **THEN** the grant covers both

#### Scenario: Prefix wildcard respects segment boundaries
- **WHEN** grant `fs:*` is matched against required scopes `fsx:read` and `fs`
- **THEN** the grant covers neither

#### Scenario: Exact grant does not cover sub-scopes
- **WHEN** grant `fs:read` is matched against required scope `fs:read:secret`
- **THEN** the grant does not cover it

### Requirement: Principals carry identity, scopes and roles
A principal SHALL have a non-empty identifier, a set of directly granted scopes and a set of role names, and MUST be immutable once created. An empty identifier or empty role name MUST be rejected with a validation error.

#### Scenario: Principal with scopes and roles
- **WHEN** a principal `alice` is created with scope `fs:read` and role `analyst`
- **THEN** it exposes identifier `alice`, scopes `{fs:read}` and roles `{analyst}`

#### Scenario: Principal is immutable
- **WHEN** code attempts to replace the scopes of an existing principal
- **THEN** the attempt fails and the principal is unchanged

#### Scenario: Empty identifier rejected
- **WHEN** a principal is created with an empty identifier
- **THEN** a validation error is raised

### Requirement: Roles grant named scope bundles
The policy SHALL allow named roles to be defined, at construction or later, each mapping to a set of valid scopes; redefining a role replaces its scopes. A principal's effective scopes MUST be its own scopes plus the scopes of every role it holds; roles that are not defined contribute nothing.

#### Scenario: Role scopes are effective
- **WHEN** role `reader` is defined with `fs:read` and principal `bob` holds role `reader` and own scope `net:http:get`
- **THEN** bob's effective scopes are `{fs:read, net:http:get}`

#### Scenario: Undefined role contributes nothing
- **WHEN** principal `carol` holds role `ghost` which is not defined
- **THEN** carol's effective scopes are only her own scopes

#### Scenario: Redefining a role
- **WHEN** role `reader` is redefined with `db:read` only
- **THEN** a principal holding `reader` no longer has `fs:read` from it and now has `db:read`

### Requirement: Least-privilege delegation
A principal SHALL be able to delegate a subset of its authority to a child principal. Delegation MUST be rejected with a permission-denied error when any requested scope is not covered by the delegator's authority: its own scopes when delegating directly, or its effective scopes (including roles) when delegating through the policy. The child records the delegator as its parent.

#### Scenario: Narrowing to a covered subset
- **WHEN** a principal holding `fs:*` delegates `fs:read`
- **THEN** a child principal is returned whose scopes are `{fs:read}` and whose parent is the delegator

#### Scenario: Escalation rejected
- **WHEN** a principal holding only `fs:read` delegates `fs:write`
- **THEN** a permission-denied error is raised naming `fs:write`

#### Scenario: Delegation through the policy uses role scopes
- **WHEN** a principal with no own scopes but role `reader` (`fs:read`) delegates `fs:read` through the policy
- **THEN** the child principal is created with scope `fs:read`

#### Scenario: Default child identifier
- **WHEN** principal `planner` delegates without naming the child
- **THEN** the child's identifier is derived from `planner` and differs from it

### Requirement: Delegated authority never exceeds the delegator
The effective scopes of a principal that has a parent SHALL be bounded by its parent's effective scopes, recursively up the delegation chain, so that no scope, role or wildcard held by the child grants access its ancestors lack.

#### Scenario: Child wildcard bounded by parent
- **WHEN** a child principal holding `*` has a parent whose effective scopes are `{fs:read}`
- **THEN** the child's effective scopes are `{fs:read}`

#### Scenario: Child role bounded by parent
- **WHEN** a child holds role `admin` (`*`) but its parent's effective scopes are `{fs:*}`
- **THEN** the child can use a tool requiring `fs:write` but not a tool requiring `net:http:get`

#### Scenario: Multi-level chain
- **WHEN** a grandchild is delegated from a child that was narrowed to `fs:read`
- **THEN** the grandchild cannot be granted `fs:write`, directly or through the policy

### Requirement: Scope-based tool access
A principal SHALL be allowed to use a tool exactly when every scope the tool requires is covered by at least one of the principal's effective scopes, unless an explicit rule decides otherwise. A tool that requires no scopes MUST be allowed for any principal that is not explicitly denied. A tool with a malformed required scope MUST be denied.

#### Scenario: All required scopes covered
- **WHEN** principal with `fs:*` asks to use a tool requiring `fs:read` and `fs:write`
- **THEN** access is allowed

#### Scenario: Missing scope
- **WHEN** principal with `fs:read` asks to use a tool requiring `fs:read` and `net:http:get`
- **THEN** access is denied and the missing scopes are exactly `{net:http:get}`

#### Scenario: Tool with no required scopes
- **WHEN** a principal with no scopes asks to use a tool that requires none
- **THEN** access is allowed

#### Scenario: Malformed required scope fails closed
- **WHEN** a principal with `*` asks to use a tool requiring `fs::read`
- **THEN** access is denied

### Requirement: Explicit tool rules
The policy SHALL accept explicit allow and deny rules naming a tool and a subject, where a subject matches a principal whose identifier equals it or who holds a role of that name. An explicit deny MUST win over every scope grant and allow rule and MUST also apply to principals delegated from a denied principal. An explicit allow MUST grant a tool regardless of scopes, but only if the principal's parent, if any, is also allowed that tool.

#### Scenario: Deny overrides scopes
- **WHEN** principal `alice` holds `*` and tool `shell` is explicitly denied for `alice`
- **THEN** alice is denied `shell` with a reason stating it was explicitly denied

#### Scenario: Deny by role
- **WHEN** tool `shell` is denied for role `intern` and principal `dave` holds role `intern`
- **THEN** dave is denied `shell`

#### Scenario: Deny wins over allow
- **WHEN** tool `shell` is both explicitly allowed and explicitly denied for `alice`
- **THEN** alice is denied `shell`

#### Scenario: Deny is inherited by delegates
- **WHEN** tool `shell` is denied for `alice` and a child is delegated from alice
- **THEN** the child is denied `shell`

#### Scenario: Explicit allow bypasses scopes
- **WHEN** tool `report` requires `db:read`, principal `erin` has no scopes, and `report` is explicitly allowed for `erin`
- **THEN** erin is allowed `report`

#### Scenario: Explicit allow bounded by parent
- **WHEN** `report` is explicitly allowed for a child principal whose parent is not allowed `report`
- **THEN** the child is denied `report`

### Requirement: Access decision forms
The policy SHALL offer a raising check that fails with a permission-denied error naming the principal, the tool and the missing scopes; a boolean query; a filter returning only the permitted tools of a sequence in their original order; and a predicate for a given principal that returns a boolean for any tool and MUST NOT raise.

#### Scenario: Check raises with details
- **WHEN** a principal with `fs:read` is checked against tool `writer` requiring `fs:write`
- **THEN** a permission-denied error is raised whose message names `writer` and `fs:write`

#### Scenario: Check passes silently
- **WHEN** a principal with `fs:read` is checked against a tool requiring `fs:read`
- **THEN** no error is raised

#### Scenario: Filter tools
- **WHEN** tools `a` (needs `fs:read`), `b` (needs `net:http:get`) and `c` (needs nothing) are filtered for a principal with `fs:read`
- **THEN** the result is `a` and `c` in that order

#### Scenario: Predicate never raises
- **WHEN** a principal's predicate is applied to a tool it may not use, including one with a malformed required scope
- **THEN** it returns false without raising

### Requirement: Decision audit trail
Every decision made by the raising check or the boolean query SHALL be appended to an audit log as an immutable record of the principal identifier, tool name and version, the outcome, the missing scopes, a reason and a timestamp. The log MUST hold at most a configurable number of records, dropping the oldest first; reading it MUST return a copy. Predicate and filter decisions MUST NOT be recorded.

#### Scenario: Denial recorded
- **WHEN** a principal `bob` with no scopes is checked against tool `writer` version `1.0.0` requiring `fs:write`
- **THEN** the last audit record names `bob`, `writer`, `1.0.0`, outcome denied and missing `{fs:write}`

#### Scenario: Allow recorded
- **WHEN** the boolean query allows `bob` to use a tool requiring nothing
- **THEN** an audit record with outcome allowed and no missing scopes is appended

#### Scenario: Bounded log drops oldest
- **WHEN** the log limit is 2 and three decisions are made
- **THEN** the log holds only the last two records in order

#### Scenario: Log is a copy
- **WHEN** the caller clears the list returned by reading the log
- **THEN** a later read still returns all recorded entries

#### Scenario: Predicate does not audit
- **WHEN** a predicate and a filter are applied to several tools
- **THEN** the audit log is unchanged

### Requirement: Concurrent use
The policy SHALL remain consistent when roles, rules and decisions are made concurrently from multiple threads; no audit record may be lost or corrupted below the log limit.

#### Scenario: Concurrent decisions
- **WHEN** 8 threads each make 50 boolean queries against a policy with a log limit of 1000
- **THEN** the audit log holds exactly 400 records
