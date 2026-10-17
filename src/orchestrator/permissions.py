"""Permission scoping: decide whether a principal may invoke a tool.

Scopes are ``resource:action`` strings made of ``:``-separated segments, e.g.
``fs:read`` or ``net:http:get``. *Granted* scopes may be wildcards: ``*`` covers
everything and ``<prefix>:*`` covers every scope strictly beneath ``<prefix>``
at a segment boundary (``fs:*`` covers ``fs:read`` and ``fs:read:secret`` but
not ``fsx:read`` nor ``fs`` itself). Scopes a tool *requires* never contain
wildcards.

Public API:

* :func:`validate_scope`, :func:`scope_matches`, :func:`scopes_cover`
* :class:`Principal` - immutable identity with scopes, roles and an optional
  delegating ``parent``; :meth:`Principal.narrow` derives a least-privilege child.
* :class:`PermissionPolicy` - roles, explicit per-tool allow/deny rules, access
  decisions (:meth:`~PermissionPolicy.check`, :meth:`~PermissionPolicy.allowed`,
  :meth:`~PermissionPolicy.filter_tools`, :meth:`~PermissionPolicy.predicate`)
  and a bounded audit log of :class:`AuditRecord` entries.
* :class:`AccessDeniedError` - a :class:`~orchestrator.core.PermissionDeniedError`
  carrying the principal, tool and missing scopes.

Auditing: only :meth:`PermissionPolicy.check` and :meth:`PermissionPolicy.allowed`
record decisions. :meth:`PermissionPolicy.predicate` and
:meth:`PermissionPolicy.filter_tools` do not, because routers evaluate every
candidate tool on every request and would flood the bounded log.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from .core import PermissionDeniedError, ToolSpec, ValidationError

__all__ = [
    "AccessDeniedError",
    "AuditRecord",
    "Decision",
    "PermissionPolicy",
    "Principal",
    "scope_matches",
    "scopes_cover",
    "validate_scope",
]

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
WILDCARD = "*"


# --------------------------------------------------------------------------- scopes


def validate_scope(scope: str, *, allow_wildcard: bool = True) -> str:
    """Return ``scope`` unchanged if well formed, else raise :class:`ValidationError`.

    With ``allow_wildcard=False`` (required scopes) ``*`` and ``prefix:*`` are rejected.
    """
    if not isinstance(scope, str) or not scope:
        raise ValidationError(f"invalid scope {scope!r}: must be a non-empty string")
    if scope == WILDCARD:
        if not allow_wildcard:
            raise ValidationError("wildcard scope '*' is not allowed here")
        return scope
    segments = scope.split(":")
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        if segment == WILDCARD and index == last:
            if not allow_wildcard:
                raise ValidationError(f"wildcard scope {scope!r} is not allowed here")
            continue
        if not segment:
            raise ValidationError(f"invalid scope {scope!r}: empty segment")
        if not _SEGMENT_RE.match(segment):
            raise ValidationError(
                f"invalid scope {scope!r}: segment {segment!r} must match [A-Za-z0-9_.-]+"
            )
    return scope


def _is_valid(scope: str, *, allow_wildcard: bool) -> bool:
    try:
        validate_scope(scope, allow_wildcard=allow_wildcard)
    except ValidationError:
        return False
    return True


def _covers(granted: str, other: str) -> bool:
    """Whether ``granted`` covers ``other``; both pre-validated, either may be a wildcard."""
    if granted == WILDCARD:
        return True
    if other == WILDCARD:
        return False
    if granted.endswith(":*"):
        prefix = granted[:-2]
        if other.endswith(":*"):
            other_prefix = other[:-2]
            return other_prefix == prefix or other_prefix.startswith(prefix + ":")
        return other.startswith(prefix + ":")
    return granted == other


def scope_matches(granted: str, required: str) -> bool:
    """Whether the granted scope (wildcards allowed) covers the required scope.

    Raises :class:`ValidationError` if either is malformed or ``required`` is a wildcard.
    """
    validate_scope(granted, allow_wildcard=True)
    validate_scope(required, allow_wildcard=False)
    return _covers(granted, required)


def scopes_cover(granted: Iterable[str], required: str) -> bool:
    """Whether any scope in ``granted`` covers ``required``."""
    return any(scope_matches(g, required) for g in granted)


def _intersect(a: Iterable[str], b: Iterable[str]) -> frozenset[str]:
    """Exact intersection of two grant sets.

    Under this grammar two grants are either nested or disjoint, so the
    intersection is every grant of one side that the other side covers.
    """
    a, b = frozenset(a), frozenset(b)
    return frozenset(x for x in a if any(_covers(y, x) for y in b)) | frozenset(
        y for y in b if any(_covers(x, y) for x in a)
    )


def _normalize_scopes(scopes: Iterable[str], what: str = "scopes") -> frozenset[str]:
    if isinstance(scopes, str):
        raise ValidationError(f"{what} must be an iterable of strings, not a single string")
    result = frozenset(scopes)
    for scope in result:
        validate_scope(scope, allow_wildcard=True)
    return result


def _normalize_names(names: Iterable[str], what: str) -> frozenset[str]:
    if isinstance(names, str):
        raise ValidationError(f"{what} must be an iterable of strings, not a single string")
    result = frozenset(names)
    for name in result:
        if not isinstance(name, str) or not name:
            raise ValidationError(f"invalid {what} entry {name!r}: must be a non-empty string")
    return result


def _fmt(scopes: Iterable[str]) -> str:
    return ", ".join(sorted(scopes))


# --------------------------------------------------------------------------- errors


class AccessDeniedError(PermissionDeniedError):
    """Permission denied, with structured details about the decision."""

    def __init__(
        self,
        message: str,
        *,
        principal_id: str,
        tool: str | None = None,
        version: str | None = None,
        missing: Iterable[str] = (),
        reason: str = "",
    ) -> None:
        super().__init__(message)
        self.principal_id = principal_id
        self.tool = tool
        self.version = version
        self.missing = frozenset(missing)
        self.reason = reason


# --------------------------------------------------------------------------- principal


@dataclass(frozen=True)
class Principal:
    """An immutable identity (user, agent or sub-agent) holding scopes and roles.

    ``parent`` is the delegating principal, if any; a principal's effective
    authority is always bounded by its parent's (see
    :meth:`PermissionPolicy.effective_scopes`).
    """

    id: str
    scopes: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    parent: Principal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValidationError(f"invalid principal id {self.id!r}: must be a non-empty string")
        object.__setattr__(self, "scopes", _normalize_scopes(self.scopes))
        object.__setattr__(self, "roles", _normalize_names(self.roles, "roles"))
        if self.parent is not None and not isinstance(self.parent, Principal):
            raise ValidationError("principal parent must be a Principal or None")

    @property
    def chain(self) -> tuple[Principal, ...]:
        """This principal followed by its delegating ancestors, nearest first."""
        out: list[Principal] = []
        node: Principal | None = self
        while node is not None:
            out.append(node)
            node = node.parent
        return tuple(out)

    @property
    def delegable_scopes(self) -> frozenset[str]:
        """Own scopes bounded by every ancestor's own scopes (roles not considered)."""
        scopes = self.scopes
        if self.parent is not None:
            scopes = _intersect(scopes, self.parent.delegable_scopes)
        return scopes

    def narrow(self, scopes: Iterable[str], *, id: str | None = None) -> Principal:
        """Delegate a subset of this principal's *own* scopes to a new child principal.

        Raises :class:`AccessDeniedError` if any requested scope is not covered.
        Use :meth:`PermissionPolicy.delegate` to delegate role-granted scopes too.
        """
        return _make_child(self, scopes, self.delegable_scopes, id)


def _make_child(
    parent: Principal, scopes: Iterable[str], authority: frozenset[str], child_id: str | None
) -> Principal:
    requested = _normalize_scopes(scopes)
    uncovered = frozenset(s for s in requested if not any(_covers(g, s) for g in authority))
    if uncovered:
        reason = f"cannot delegate scopes not held by {parent.id!r}: {_fmt(uncovered)}"
        raise AccessDeniedError(
            f"principal {parent.id!r} {reason}",
            principal_id=parent.id,
            missing=uncovered,
            reason=reason,
        )
    return Principal(
        id=child_id if child_id is not None else f"{parent.id}/delegate",
        scopes=requested,
        parent=parent,
    )


# --------------------------------------------------------------------------- policy


@dataclass(frozen=True)
class Decision:
    """Outcome of evaluating one principal against one tool."""

    allowed: bool
    missing: frozenset[str] = frozenset()
    reason: str = ""


@dataclass(frozen=True)
class AuditRecord:
    """Immutable record of one audited access decision."""

    principal_id: str
    tool: str
    version: str
    allowed: bool
    missing: frozenset[str] = field(default_factory=frozenset)
    reason: str = ""
    timestamp: float = 0.0


class PermissionPolicy:
    """Roles, explicit tool rules, access decisions and a bounded audit log.

    Decision order for ``(principal, tool)``:

    1. an explicit deny matching the principal or any delegating ancestor -> deny;
    2. a malformed (or wildcard) required scope on the tool -> deny (fail closed);
    3. every required scope covered by the effective scopes -> allow;
    4. an explicit allow matching the principal, and its parent (if any) is
       allowed the tool -> allow;
    5. otherwise deny, reporting the uncovered scopes as ``missing``.

    Rule subjects match a principal whose ``id`` equals the subject **or** who
    holds a role with that name (one shared namespace). Rules key on tool name,
    so they apply to every version of the tool.

    Only :meth:`check` and :meth:`allowed` write to the audit log;
    :meth:`predicate`, :meth:`filter_tools` and :meth:`decide` do not.
    All mutable state is guarded by a re-entrant lock.
    """

    def __init__(
        self,
        roles: Mapping[str, Iterable[str]] | None = None,
        *,
        audit_limit: int = 1000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if isinstance(audit_limit, bool) or not isinstance(audit_limit, int) or audit_limit < 0:
            raise ValidationError("audit_limit must be an int >= 0")
        self._lock = threading.RLock()
        self._roles: dict[str, frozenset[str]] = {}
        self._deny: set[tuple[str, str]] = set()
        self._allow: set[tuple[str, str]] = set()
        self._audit: deque[AuditRecord] = deque(maxlen=audit_limit)
        self._clock = clock
        for name, scopes in (roles or {}).items():
            self.define_role(name, scopes)

    # -- roles -----------------------------------------------------------------

    def define_role(self, name: str, scopes: Iterable[str]) -> None:
        """Define (or replace) a role granting ``scopes``."""
        if not isinstance(name, str) or not name:
            raise ValidationError(f"invalid role name {name!r}: must be a non-empty string")
        normalized = _normalize_scopes(scopes)
        with self._lock:
            self._roles[name] = normalized

    @property
    def roles(self) -> dict[str, frozenset[str]]:
        """A copy of the role definitions."""
        with self._lock:
            return dict(self._roles)

    def effective_scopes(self, principal: Principal) -> frozenset[str]:
        """Own scopes ∪ role scopes, bounded by every delegating ancestor's effective scopes."""
        _require_principal(principal)
        with self._lock:
            roles = dict(self._roles)
        return _effective(principal, roles)

    def delegate(
        self, principal: Principal, scopes: Iterable[str], *, id: str | None = None
    ) -> Principal:
        """Like :meth:`Principal.narrow` but the delegator's role scopes count as authority."""
        return _make_child(principal, scopes, self.effective_scopes(principal), id)

    # -- explicit rules --------------------------------------------------------

    def deny(self, subject: str, tool_name: str) -> None:
        """Explicitly deny ``tool_name`` to a principal id or role. Deny always wins."""
        with self._lock:
            self._deny.add(_rule_key(subject, tool_name))

    def allow_tool(self, subject: str, tool_name: str) -> None:
        """Explicitly allow ``tool_name`` to a principal id or role regardless of scopes."""
        with self._lock:
            self._allow.add(_rule_key(subject, tool_name))

    def remove_rule(self, subject: str, tool_name: str) -> bool:
        """Remove any allow/deny rule for ``(subject, tool_name)``; return whether one existed."""
        key = _rule_key(subject, tool_name)
        with self._lock:
            found = key in self._deny or key in self._allow
            self._deny.discard(key)
            self._allow.discard(key)
        return found

    # -- decisions -------------------------------------------------------------

    def decide(self, principal: Principal, tool: ToolSpec) -> Decision:
        """Evaluate access without auditing or raising on denial."""
        _require_principal(principal)
        with self._lock:
            roles = dict(self._roles)
            deny = frozenset(self._deny)
            allow = frozenset(self._allow)
        return _decide(principal, tool, roles, deny, allow)

    def check(self, principal: Principal, tool: ToolSpec) -> None:
        """Audit the decision and raise :class:`AccessDeniedError` if access is denied."""
        decision = self.decide(principal, tool)
        self._record(principal, tool, decision)
        if not decision.allowed:
            message = f"principal {principal.id!r} may not use tool {tool.name!r}: {decision.reason}"
            raise AccessDeniedError(
                message,
                principal_id=principal.id,
                tool=tool.name,
                version=tool.version,
                missing=decision.missing,
                reason=decision.reason,
            )

    def allowed(self, principal: Principal, tool: ToolSpec) -> bool:
        """Audit the decision and return whether access is allowed."""
        decision = self.decide(principal, tool)
        self._record(principal, tool, decision)
        return decision.allowed

    def filter_tools(self, principal: Principal, tools: Iterable[ToolSpec]) -> list[ToolSpec]:
        """Return the tools ``principal`` may use, in input order. Not audited."""
        keep = self.predicate(principal)
        return [tool for tool in tools if keep(tool)]

    def predicate(self, principal: Principal) -> Callable[[ToolSpec], bool]:
        """Return a non-raising, non-auditing ``tool -> bool`` filter for routers."""

        def eligible(tool: ToolSpec) -> bool:
            try:
                return self.decide(principal, tool).allowed
            except Exception:  # fail closed on any unexpected input
                return False

        return eligible

    # -- audit -----------------------------------------------------------------

    @property
    def audit_limit(self) -> int:
        return self._audit.maxlen or 0

    @property
    def audit_log(self) -> list[AuditRecord]:
        """A copy of the audit log, oldest first."""
        with self._lock:
            return list(self._audit)

    def clear_audit_log(self) -> None:
        with self._lock:
            self._audit.clear()

    def _record(self, principal: Principal, tool: ToolSpec, decision: Decision) -> None:
        record = AuditRecord(
            principal_id=principal.id,
            tool=str(getattr(tool, "name", "")),
            version=str(getattr(tool, "version", "")),
            allowed=decision.allowed,
            missing=decision.missing,
            reason=decision.reason,
            timestamp=self._clock(),
        )
        with self._lock:
            self._audit.append(record)


# --------------------------------------------------------------------------- helpers


def _require_principal(principal: object) -> None:
    if not isinstance(principal, Principal):
        raise ValidationError(f"expected a Principal, got {type(principal).__name__}")


def _rule_key(subject: str, tool_name: str) -> tuple[str, str]:
    if not isinstance(subject, str) or not subject:
        raise ValidationError(f"invalid rule subject {subject!r}: must be a non-empty string")
    if not isinstance(tool_name, str) or not tool_name:
        raise ValidationError(f"invalid tool name {tool_name!r}: must be a non-empty string")
    return subject, tool_name


def _effective(principal: Principal, roles: Mapping[str, frozenset[str]]) -> frozenset[str]:
    scopes = set(principal.scopes)
    for role in principal.roles:
        scopes |= roles.get(role, frozenset())
    if principal.parent is not None:
        return _intersect(scopes, _effective(principal.parent, roles))
    return frozenset(scopes)


def _rule_hit(principal: Principal, tool_name: str, rules: frozenset[tuple[str, str]]) -> str | None:
    if (principal.id, tool_name) in rules:
        return principal.id
    for role in sorted(principal.roles):
        if (role, tool_name) in rules:
            return role
    return None


def _decide(
    principal: Principal,
    tool: ToolSpec,
    roles: Mapping[str, frozenset[str]],
    deny: frozenset[tuple[str, str]],
    allow: frozenset[tuple[str, str]],
) -> Decision:
    name = tool.name
    for node in principal.chain:
        subject = _rule_hit(node, name, deny)
        if subject is not None:
            reason = f"tool {name!r} is explicitly denied for {subject!r}"
            if node is not principal:
                reason += f" (inherited from delegating principal {node.id!r})"
            return Decision(False, frozenset(), reason)

    required = frozenset(tool.required_scopes)
    malformed = frozenset(s for s in required if not _is_valid(s, allow_wildcard=False))
    if malformed:
        return Decision(
            False, malformed, f"tool {name!r} declares malformed required scopes: {_fmt(malformed)}"
        )

    effective = _effective(principal, roles)
    missing = frozenset(r for r in required if not any(_covers(g, r) for g in effective))
    if not missing:
        return Decision(True, frozenset(), "all required scopes granted" if required else "tool requires no scopes")

    subject = _rule_hit(principal, name, allow)
    if subject is not None:
        if principal.parent is None or _decide(principal.parent, tool, roles, deny, allow).allowed:
            return Decision(True, frozenset(), f"tool {name!r} is explicitly allowed for {subject!r}")
    return Decision(False, missing, f"missing scopes: {_fmt(missing)}")
