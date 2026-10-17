import dataclasses
import itertools
import threading

import pytest

from orchestrator.core import PermissionDeniedError, ValidationError
from orchestrator.permissions import (
    AccessDeniedError,
    AuditRecord,
    PermissionPolicy,
    Principal,
    scope_matches,
    scopes_cover,
    validate_scope,
)

from conftest import make_tool


# --------------------------------------------------------------- scope syntax


def test_well_formed_scopes_accepted():
    p = Principal("alice", scopes={"fs:read", "net:http:get", "fs:*", "*"})
    assert p.scopes == frozenset({"fs:read", "net:http:get", "fs:*", "*"})


@pytest.mark.parametrize("bad", ["", "fs::read", "fs:", ":fs", "fs:*:read", "f*", "fs read", "*:fs"])
def test_malformed_scopes_rejected(bad):
    with pytest.raises(ValidationError):
        validate_scope(bad)
    with pytest.raises(ValidationError):
        Principal("alice", scopes={bad})


def test_wildcard_not_allowed_as_required_scope():
    with pytest.raises(ValidationError):
        validate_scope("fs:*", allow_wildcard=False)
    with pytest.raises(ValidationError):
        validate_scope("*", allow_wildcard=False)
    with pytest.raises(ValidationError):
        scope_matches("*", "fs:*")


def test_single_string_scopes_rejected():
    with pytest.raises(ValidationError):
        Principal("alice", scopes="fs:read")


# --------------------------------------------------------------- matching


def test_exact_match():
    assert scope_matches("fs:read", "fs:read")


def test_global_wildcard():
    assert scope_matches("*", "net:http:get")


def test_prefix_wildcard_covers_nested():
    assert scope_matches("fs:*", "fs:read")
    assert scope_matches("fs:*", "fs:read:secret")
    assert scope_matches("net:http:*", "net:http:get")


def test_prefix_wildcard_respects_segment_boundaries():
    assert not scope_matches("fs:*", "fsx:read")
    assert not scope_matches("fs:*", "fs")


def test_exact_grant_does_not_cover_sub_scopes():
    assert not scope_matches("fs:read", "fs:read:secret")
    assert not scope_matches("fs:read", "fs:write")


def test_scopes_cover():
    assert scopes_cover({"db:read", "fs:*"}, "fs:write")
    assert not scopes_cover({"db:read"}, "fs:write")
    assert not scopes_cover(set(), "fs:write")


# --------------------------------------------------------------- principals


def test_principal_with_scopes_and_roles():
    p = Principal("alice", scopes=["fs:read"], roles=["analyst"])
    assert p.id == "alice"
    assert p.scopes == frozenset({"fs:read"})
    assert p.roles == frozenset({"analyst"})
    assert p.parent is None


def test_principal_is_immutable():
    p = Principal("alice", scopes={"fs:read"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.scopes = frozenset({"*"})
    assert p.scopes == frozenset({"fs:read"})


@pytest.mark.parametrize("kwargs", [{"id": ""}, {"id": "a", "roles": {""}}, {"id": "a", "roles": "admin"}])
def test_invalid_principal_rejected(kwargs):
    with pytest.raises(ValidationError):
        Principal(**kwargs)


def test_principal_is_hashable():
    assert Principal("a", {"fs:read"}) == Principal("a", frozenset({"fs:read"}))
    assert len({Principal("a", {"fs:read"}), Principal("a", {"fs:read"})}) == 1


# --------------------------------------------------------------- roles


def test_role_scopes_are_effective():
    policy = PermissionPolicy({"reader": ["fs:read"]})
    bob = Principal("bob", scopes={"net:http:get"}, roles={"reader"})
    assert policy.effective_scopes(bob) == frozenset({"fs:read", "net:http:get"})


def test_undefined_role_contributes_nothing():
    policy = PermissionPolicy()
    carol = Principal("carol", scopes={"fs:read"}, roles={"ghost"})
    assert policy.effective_scopes(carol) == frozenset({"fs:read"})


def test_redefining_role_replaces_scopes():
    policy = PermissionPolicy({"reader": ["fs:read"]})
    bob = Principal("bob", roles={"reader"})
    policy.define_role("reader", ["db:read"])
    assert policy.effective_scopes(bob) == frozenset({"db:read"})
    assert policy.roles == {"reader": frozenset({"db:read"})}


def test_define_role_validates():
    policy = PermissionPolicy()
    with pytest.raises(ValidationError):
        policy.define_role("", ["fs:read"])
    with pytest.raises(ValidationError):
        policy.define_role("bad", ["fs::read"])
    with pytest.raises(ValidationError):
        PermissionPolicy({"bad": ["fs:"]})


# --------------------------------------------------------------- delegation


def test_narrowing_to_covered_subset():
    parent = Principal("planner", scopes={"fs:*"})
    child = parent.narrow(["fs:read"], id="worker")
    assert child.id == "worker"
    assert child.scopes == frozenset({"fs:read"})
    assert child.parent is parent
    assert child.chain == (child, parent)


def test_narrowing_with_wildcard_subset():
    parent = Principal("root", scopes={"*"})
    child = parent.narrow(["fs:*"])
    assert child.narrow(["fs:read:secret"]).scopes == frozenset({"fs:read:secret"})


def test_escalation_rejected():
    parent = Principal("planner", scopes={"fs:read"})
    with pytest.raises(PermissionDeniedError) as info:
        parent.narrow(["fs:read", "fs:write"])
    assert "fs:write" in str(info.value)
    assert isinstance(info.value, AccessDeniedError)
    assert info.value.missing == frozenset({"fs:write"})
    # a wildcard request is broader than a specific grant
    with pytest.raises(PermissionDeniedError):
        parent.narrow(["fs:*"])


def test_delegation_through_policy_uses_role_scopes():
    policy = PermissionPolicy({"reader": ["fs:read"]})
    parent = Principal("p", roles={"reader"})
    with pytest.raises(PermissionDeniedError):
        parent.narrow(["fs:read"])  # direct narrowing only sees own scopes
    child = policy.delegate(parent, ["fs:read"])
    assert child.scopes == frozenset({"fs:read"})
    assert child.parent is parent
    assert policy.effective_scopes(child) == frozenset({"fs:read"})


def test_default_child_identifier():
    child = Principal("planner", scopes={"fs:read"}).narrow(["fs:read"])
    assert child.id != "planner"
    assert "planner" in child.id


def test_child_wildcard_bounded_by_parent():
    policy = PermissionPolicy()
    parent = Principal("p", scopes={"fs:read"})
    child = Principal("c", scopes={"*"}, parent=parent)
    assert policy.effective_scopes(child) == frozenset({"fs:read"})


def test_child_role_bounded_by_parent():
    policy = PermissionPolicy({"admin": ["*"]})
    parent = Principal("p", scopes={"fs:*"})
    child = Principal("c", roles={"admin"}, parent=parent)
    assert policy.allowed(child, make_tool("writer", scopes=["fs:write"]))
    assert not policy.allowed(child, make_tool("http", scopes=["net:http:get"]))


def test_intersection_keeps_narrower_side():
    policy = PermissionPolicy()
    parent = Principal("p", scopes={"fs:read:*", "db:read"})
    child = Principal("c", scopes={"fs:*", "db:*", "net:http:get"}, parent=parent)
    assert policy.effective_scopes(child) == frozenset({"fs:read:*", "db:read"})


def test_multi_level_chain():
    policy = PermissionPolicy({"writer": ["fs:write"]})
    root = Principal("root", scopes={"fs:*"})
    child = root.narrow(["fs:read"])
    with pytest.raises(PermissionDeniedError):
        child.narrow(["fs:write"])
    with pytest.raises(PermissionDeniedError):
        policy.delegate(child, ["fs:write"])
    grandchild = child.narrow(["fs:read"])
    # even a hand-built role grant on the grandchild cannot exceed the chain
    sneaky = Principal("g2", roles={"writer"}, parent=child)
    assert policy.effective_scopes(grandchild) == frozenset({"fs:read"})
    assert policy.effective_scopes(sneaky) == frozenset()
    assert not policy.allowed(sneaky, make_tool("w", scopes=["fs:write"]))


# --------------------------------------------------------------- scope-based access


def test_all_required_scopes_covered():
    policy = PermissionPolicy()
    assert policy.allowed(Principal("a", {"fs:*"}), make_tool(scopes=["fs:read", "fs:write"]))


def test_missing_scope():
    policy = PermissionPolicy()
    decision = policy.decide(Principal("a", {"fs:read"}), make_tool(scopes=["fs:read", "net:http:get"]))
    assert not decision.allowed
    assert decision.missing == frozenset({"net:http:get"})


def test_tool_with_no_required_scopes():
    policy = PermissionPolicy()
    assert policy.allowed(Principal("nobody"), make_tool())


def test_malformed_required_scope_fails_closed():
    policy = PermissionPolicy()
    root = Principal("root", {"*"})
    for bad in ("fs::read", "fs:*", "*"):
        tool = make_tool(scopes=[bad])
        assert not policy.allowed(root, tool)
        with pytest.raises(PermissionDeniedError):
            policy.check(root, tool)


# --------------------------------------------------------------- explicit rules


def test_deny_overrides_scopes():
    policy = PermissionPolicy()
    alice = Principal("alice", {"*"})
    policy.deny("alice", "shell")
    decision = policy.decide(alice, make_tool("shell"))
    assert not decision.allowed
    assert "explicitly denied" in decision.reason
    assert policy.allowed(alice, make_tool("other"))


def test_deny_by_role():
    policy = PermissionPolicy()
    policy.deny("intern", "shell")
    dave = Principal("dave", {"*"}, roles={"intern"})
    assert not policy.allowed(dave, make_tool("shell"))


def test_deny_wins_over_allow():
    policy = PermissionPolicy()
    policy.allow_tool("alice", "shell")
    policy.deny("alice", "shell")
    assert not policy.allowed(Principal("alice", {"*"}), make_tool("shell", scopes=["os:exec"]))


def test_deny_is_inherited_by_delegates():
    policy = PermissionPolicy()
    alice = Principal("alice", {"*"})
    policy.deny("alice", "shell")
    child = alice.narrow(["*"], id="helper")
    decision = policy.decide(child, make_tool("shell"))
    assert not decision.allowed
    assert "alice" in decision.reason


def test_explicit_allow_bypasses_scopes():
    policy = PermissionPolicy()
    report = make_tool("report", scopes=["db:read"])
    erin = Principal("erin")
    assert not policy.allowed(erin, report)
    policy.allow_tool("erin", "report")
    assert policy.allowed(erin, report)


def test_explicit_allow_by_role():
    policy = PermissionPolicy()
    policy.allow_tool("auditor", "report")
    assert policy.allowed(Principal("x", roles={"auditor"}), make_tool("report", scopes=["db:read"]))


def test_explicit_allow_bounded_by_parent():
    policy = PermissionPolicy()
    report = make_tool("report", scopes=["db:read"])
    parent = Principal("p", {"fs:read"})
    child = parent.narrow(["fs:read"], id="c")
    policy.allow_tool("c", "report")
    assert not policy.allowed(child, report)
    policy.allow_tool("p", "report")
    assert policy.allowed(child, report)


def test_remove_rule():
    policy = PermissionPolicy()
    policy.deny("alice", "shell")
    assert policy.remove_rule("alice", "shell")
    assert not policy.remove_rule("alice", "shell")
    assert policy.allowed(Principal("alice"), make_tool("shell"))


def test_rule_validation():
    policy = PermissionPolicy()
    with pytest.raises(ValidationError):
        policy.deny("", "shell")
    with pytest.raises(ValidationError):
        policy.allow_tool("alice", "")


# --------------------------------------------------------------- decision forms


def test_check_raises_with_details():
    policy = PermissionPolicy()
    writer = make_tool("writer", scopes=["fs:write"])
    with pytest.raises(PermissionDeniedError) as info:
        policy.check(Principal("bob", {"fs:read"}), writer)
    err = info.value
    assert "writer" in str(err) and "fs:write" in str(err) and "bob" in str(err)
    assert isinstance(err, AccessDeniedError)
    assert (err.principal_id, err.tool, err.version) == ("bob", "writer", "1.0.0")
    assert err.missing == frozenset({"fs:write"})


def test_check_passes_silently():
    policy = PermissionPolicy()
    assert policy.check(Principal("bob", {"fs:read"}), make_tool(scopes=["fs:read"])) is None


def test_filter_tools_preserves_order():
    policy = PermissionPolicy()
    a = make_tool("a", scopes=["fs:read"])
    b = make_tool("b", scopes=["net:http:get"])
    c = make_tool("c")
    assert policy.filter_tools(Principal("p", {"fs:read"}), [a, b, c]) == [a, c]


def test_predicate_never_raises():
    policy = PermissionPolicy()
    eligible = policy.predicate(Principal("p", {"fs:read"}))
    assert eligible(make_tool(scopes=["fs:read"])) is True
    assert eligible(make_tool(scopes=["fs:write"])) is False
    assert eligible(make_tool(scopes=["fs::read"])) is False
    assert eligible(object()) is False  # unexpected input fails closed


def test_decisions_reject_non_principal():
    policy = PermissionPolicy()
    with pytest.raises(ValidationError):
        policy.check("alice", make_tool())
    assert policy.predicate("alice")(make_tool()) is False


# --------------------------------------------------------------- audit


def test_denial_recorded():
    policy = PermissionPolicy(clock=lambda: 42.0)
    with pytest.raises(PermissionDeniedError):
        policy.check(Principal("bob"), make_tool("writer", version="1.0.0", scopes=["fs:write"]))
    record = policy.audit_log[-1]
    assert isinstance(record, AuditRecord)
    assert record == AuditRecord(
        principal_id="bob",
        tool="writer",
        version="1.0.0",
        allowed=False,
        missing=frozenset({"fs:write"}),
        reason=record.reason,
        timestamp=42.0,
    )
    assert "fs:write" in record.reason
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.allowed = True


def test_allow_recorded():
    policy = PermissionPolicy()
    assert policy.allowed(Principal("bob"), make_tool("free"))
    (record,) = policy.audit_log
    assert record.allowed and record.missing == frozenset() and record.tool == "free"


def test_bounded_log_drops_oldest():
    ticks = itertools.count()
    policy = PermissionPolicy(audit_limit=2, clock=lambda: float(next(ticks)))
    bob = Principal("bob")
    for name in ("t1", "t2", "t3"):
        policy.allowed(bob, make_tool(name))
    assert [r.tool for r in policy.audit_log] == ["t2", "t3"]
    assert [r.timestamp for r in policy.audit_log] == [1.0, 2.0]
    assert policy.audit_limit == 2


def test_audit_limit_zero_disables_and_negative_rejected():
    policy = PermissionPolicy(audit_limit=0)
    policy.allowed(Principal("bob"), make_tool())
    assert policy.audit_log == []
    with pytest.raises(ValidationError):
        PermissionPolicy(audit_limit=-1)


def test_log_is_a_copy():
    policy = PermissionPolicy()
    policy.allowed(Principal("bob"), make_tool())
    policy.allowed(Principal("bob"), make_tool())
    log = policy.audit_log
    log.clear()
    assert len(policy.audit_log) == 2
    policy.clear_audit_log()
    assert policy.audit_log == []


def test_predicate_and_filter_do_not_audit():
    policy = PermissionPolicy()
    p = Principal("p", {"fs:read"})
    tools = [make_tool("a", scopes=["fs:read"]), make_tool("b", scopes=["fs:write"])]
    eligible = policy.predicate(p)
    [eligible(t) for t in tools]
    policy.filter_tools(p, tools)
    policy.decide(p, tools[0])
    assert policy.audit_log == []


# --------------------------------------------------------------- concurrency


def test_concurrent_decisions():
    policy = PermissionPolicy({"reader": ["fs:read"]}, audit_limit=1000)
    tool = make_tool(scopes=["fs:read"])
    errors = []

    def worker(n):
        try:
            p = Principal(f"w{n}", roles={"reader"})
            for i in range(50):
                if i % 10 == 0:
                    policy.define_role(f"r{n}", ["db:read"])
                    policy.deny(f"w{n}", f"x{i}")
                assert policy.allowed(p, tool)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    log = policy.audit_log
    assert len(log) == 400
    assert all(r.allowed for r in log)
