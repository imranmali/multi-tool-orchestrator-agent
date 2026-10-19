import ast
import asyncio
import pathlib

import pytest

from orchestrator.conflicts import (
    ConflictResolver,
    Resolution,
    ResourceLockManager,
    detect_write_conflicts,
)
from orchestrator.core import ConflictError, ToolResult, ValidationError


def ok(tool, value, priority=None):
    meta = {} if priority is None else {"priority": priority}
    return ToolResult(tool=tool, version="1.0.0", ok=True, value=value, metadata=meta)


def fail(tool, error):
    return ToolResult(tool=tool, version="1.0.0", ok=False, error=error)


@pytest.fixture
def resolver():
    return ConflictResolver()


# --------------------------------------------------------------------------- participation


def test_failed_results_ignored(resolver):
    res = resolver.resolve([fail("a", "boom"), ok("b", 42), ok("c", 42)])
    assert res.value == 42
    assert res.winners == ("b", "c")
    assert res.dissenters == ()
    assert res.confidence == 1.0


def test_nothing_succeeded_raises_with_errors(resolver):
    with pytest.raises(ConflictError) as info:
        resolver.resolve([fail("a", "boom"), fail("b", "down")])
    msg = str(info.value)
    for part in ("a", "boom", "b", "down"):
        assert part in msg


def test_empty_results_raise(resolver):
    with pytest.raises(ConflictError):
        resolver.resolve([])


# --------------------------------------------------------------------------- resolution record


def test_unanimous_agreement(resolver):
    res = resolver.resolve([ok("a", {"x": 1}), ok("b", {"x": 1}), ok("c", {"x": 1})])
    assert isinstance(res, Resolution)
    assert res.value == {"x": 1}
    assert res.winners == ("a", "b", "c")
    assert res.dissenters == ()
    assert res.confidence == 1.0
    assert res.strategy == "majority"


def test_partial_agreement(resolver):
    res = resolver.resolve([ok("a", 1), ok("b", 1), ok("c", 2)], "majority")
    assert res.winners == ("a", "b")
    assert res.dissenters == ("c",)
    assert res.confidence == pytest.approx(2 / 3)


def test_resolution_is_frozen(resolver):
    res = resolver.resolve([ok("a", 1)])
    with pytest.raises(AttributeError):
        res.value = 2


# --------------------------------------------------------------------------- default strategy


def test_majority_by_default(resolver):
    assert resolver.resolve([ok("a", 1), ok("b", 2), ok("c", 2)]).strategy == "majority"


def test_configured_default_strategy():
    res = ConflictResolver("first_success").resolve([ok("a", 1), ok("b", 2), ok("c", 2)])
    assert res.strategy == "first_success"
    assert res.value == 1


def test_unknown_default_rejected():
    with pytest.raises(ValidationError):
        ConflictResolver("coin-flip")


# --------------------------------------------------------------------------- first success


def test_first_success(resolver):
    res = resolver.resolve([fail("a", "x"), ok("b", "x"), ok("c", "y")], "first_success")
    assert res.value == "x"
    assert res.winners == ("b",)
    assert res.dissenters == ("c",)
    assert res.confidence == 0.5


# --------------------------------------------------------------------------- majority


def test_majority_groups_unhashable_values(resolver):
    res = resolver.resolve(
        [
            ok("a", {"k": [1, 2], "z": 0}),
            ok("b", {"z": 0, "k": [1, 2]}),
            ok("c", {"k": [3]}),
        ]
    )
    assert res.value == {"k": [1, 2], "z": 0}
    assert res.winners == ("a", "b")
    assert res.dissenters == ("c",)


def test_majority_falls_back_to_equality_for_unserialisable_values(resolver):
    # Mixed-type keys make json.dumps(sort_keys=True) fail; == is used instead.
    v1, v2 = {1: "a", "b": 2}, {"b": 2, 1: "a"}
    res = resolver.resolve([ok("a", v1), ok("b", v2), ok("c", {1: "z", "b": 2})])
    assert res.winners == ("a", "b")


def test_majority_tie_broken_by_priority(resolver):
    res = resolver.resolve([ok("a", "x", priority=1), ok("b", "y", priority=5)])
    assert res.value == "y"
    assert res.details["tie_broken"] is True


def test_majority_tie_broken_by_name(resolver):
    res = resolver.resolve([ok("zed", "z"), ok("alpha", "a")])
    assert res.value == "a"
    assert res.winners == ("alpha",)


def test_quorum_met(resolver):
    res = resolver.resolve([ok("a", 1), ok("b", 1), ok("c", 2)], "majority", quorum=0.6)
    assert res.value == 1


def test_quorum_is_inclusive(resolver):
    res = resolver.resolve([ok("a", 1), ok("b", 1), ok("c", 2), ok("d", 3)], quorum=0.5)
    assert res.value == 1


def test_quorum_not_met(resolver):
    with pytest.raises(ConflictError):
        resolver.resolve([ok("a", 1), ok("b", 1), ok("c", 2), ok("d", 3)], quorum=0.75)


@pytest.mark.parametrize("quorum", [1.5, 0, -0.1, "half"])
def test_invalid_quorum(resolver, quorum):
    with pytest.raises(ValidationError):
        resolver.resolve([ok("a", 1)], quorum=quorum)


# --------------------------------------------------------------------------- priority


def test_configured_priority_overrides_metadata():
    r = ConflictResolver(priorities={"a": 10})
    ra, rb = ok("a", 1, priority=0), ok("b", 2, priority=5)
    assert r.priority_of(ra) == 10
    assert r.priority_of(rb) == 5
    assert r.resolve([ra, rb], "priority").value == 1


def test_metadata_priority_used(resolver):
    ra, rb = ok("a", 1), ok("b", 2, priority=5)
    assert resolver.priority_of(ra) == 0
    assert resolver.priority_of(rb) == 5
    assert resolver.resolve([ra, rb], "priority").value == 2


def test_priority_beats_majority(resolver):
    res = resolver.resolve([ok("a", 1), ok("b", 1), ok("c", 2, priority=9)], "priority")
    assert res.value == 2
    assert res.winners == ("c",)
    assert res.dissenters == ("a", "b")
    assert res.details["selected"] == "c"


def test_priority_tie_by_name(resolver):
    assert resolver.resolve([ok("m", 1), ok("b", 2)], "priority").value == 2


# --------------------------------------------------------------------------- merge


def test_merge_disjoint_and_nested(resolver):
    res = resolver.resolve(
        [
            ok("a", {"name": "x", "meta": {"lang": "en"}}),
            ok("b", {"meta": {"size": 3}, "tags": ["t1"]}),
        ],
        "merge",
    )
    assert res.value == {"name": "x", "meta": {"lang": "en", "size": 3}, "tags": ["t1"]}
    assert list(res.value) == ["name", "meta", "tags"]
    assert res.confidence == 1.0
    assert res.winners == ("a", "b")
    assert res.details["conflicting_paths"] == []


def test_merge_lists_unioned(resolver):
    res = resolver.resolve(
        [ok("a", {"tags": ["x", "y"]}), ok("b", {"tags": ["y", "z"]})], "merge"
    )
    assert res.value["tags"] == ["x", "y", "z"]


def test_merge_list_union_dedupes_unhashable_items(resolver):
    res = resolver.resolve(
        [ok("a", {"l": [{"k": 1}]}), ok("b", {"l": [{"k": 1}, {"k": 2}]})], "merge"
    )
    assert res.value["l"] == [{"k": 1}, {"k": 2}]


def test_merge_equal_leaves_ok(resolver):
    res = resolver.resolve([ok("a", {"x": 1, "y": 2}), ok("b", {"x": 1})], "merge")
    assert res.value == {"x": 1, "y": 2}


CONFLICTING = [ok("a", {"meta": {"lang": "en"}}), ok("b", {"meta": {"lang": "fr"}}, priority=3)]


def test_merge_conflict_raises_by_default(resolver):
    with pytest.raises(ConflictError) as info:
        resolver.resolve(CONFLICTING, "merge")
    msg = str(info.value)
    assert "meta.lang" in msg and "a" in msg and "b" in msg


def test_merge_conflict_lists_every_path(resolver):
    with pytest.raises(ConflictError) as info:
        resolver.resolve(
            [ok("a", {"x": 1, "y": {"z": 1}}), ok("b", {"x": 2, "y": {"z": 2}})], "merge"
        )
    assert "x" in str(info.value) and "y.z" in str(info.value)


def test_merge_priority_policy(resolver):
    res = resolver.resolve(CONFLICTING, "merge", on_conflict="priority")
    assert res.value == {"meta": {"lang": "fr"}}
    assert res.details["conflicting_paths"] == ["meta.lang"]
    assert res.details["conflicts"][0]["chosen"] == "b"
    assert res.dissenters == ("a",)
    assert res.winners == ("b",)
    assert res.confidence == 0.5


def test_merge_first_policy(resolver):
    res = resolver.resolve(CONFLICTING, "merge", on_conflict="first")
    assert res.value == {"meta": {"lang": "en"}}
    assert res.dissenters == ("b",)


def test_merge_type_mismatch_is_conflict(resolver):
    res = resolver.resolve(
        [ok("a", {"x": [1]}), ok("b", {"x": {"y": 1}})], "merge", on_conflict="first"
    )
    assert res.value == {"x": [1]}
    assert res.details["conflicting_paths"] == ["x"]


def test_merge_invalid_policy(resolver):
    with pytest.raises(ValidationError):
        resolver.resolve(CONFLICTING, "merge", on_conflict="last")


def test_merge_equal_scalars(resolver):
    res = resolver.resolve([ok("a", 7), ok("b", 7)], "merge")
    assert res.value == 7
    assert res.confidence == 1.0


def test_merge_non_dict_rejected(resolver):
    with pytest.raises(ConflictError):
        resolver.resolve([ok("a", 7), ok("b", {"x": 1})], "merge")


# --------------------------------------------------------------------------- numeric


NUMS = [ok("a", 1), ok("b", 2), ok("c", 6)]


def test_numeric_mean(resolver):
    res = resolver.resolve(NUMS, "numeric", agg="mean")
    assert res.value == 3
    assert res.winners == ("a", "b", "c")
    assert res.dissenters == ()
    assert 0 <= res.confidence < 1
    assert res.details["spread"] == 5


def test_numeric_default_is_mean(resolver):
    assert resolver.resolve(NUMS, "numeric").value == 3


@pytest.mark.parametrize("agg,expected", [("median", 2), ("min", 1), ("max", 6)])
def test_numeric_other_aggregations(resolver, agg, expected):
    assert resolver.resolve(NUMS, "numeric", agg=agg).value == expected


def test_numeric_unanimous_confidence(resolver):
    res = resolver.resolve([ok("a", 2.5), ok("b", 2.5)], "numeric")
    assert res.value == 2.5
    assert res.confidence == 1.0


def test_numeric_closer_values_more_confident(resolver):
    near = resolver.resolve([ok("a", 100), ok("b", 101)], "numeric")
    far = resolver.resolve([ok("a", 1), ok("b", 100)], "numeric")
    assert far.confidence < near.confidence < 1.0


@pytest.mark.parametrize("bad", ["many", True, None, [1]])
def test_numeric_non_numeric(resolver, bad):
    with pytest.raises(ConflictError):
        resolver.resolve([ok("a", 1), ok("b", bad)], "numeric")


def test_numeric_unknown_agg(resolver):
    with pytest.raises(ValidationError):
        resolver.resolve(NUMS, "numeric", agg="mode")


# --------------------------------------------------------------------------- custom strategies


def longest(results):
    return max((r.value for r in results), key=len)


def test_inline_callable_plain_value(resolver):
    res = resolver.resolve([ok("a", "ab"), ok("b", "abc")], longest)
    assert res.value == "abc"
    assert res.winners == ("b",)
    assert res.dissenters == ("a",)
    assert res.strategy == "longest"


def test_inline_callable_receives_only_successes(resolver):
    seen = []

    def spy(results):
        seen.extend(r.tool for r in results)
        return results[0].value

    resolver.resolve([fail("x", "e"), ok("a", 1)], spy)
    assert seen == ["a"]


def test_callable_returning_resolution_used_as_is(resolver):
    custom = Resolution("v", "mine", ("a",), (), 0.42, {"why": "because"})
    assert resolver.resolve([ok("a", 1)], lambda results: custom) is custom


def test_registered_named_strategy(resolver):
    resolver.register_strategy("longest", longest)
    res = resolver.resolve([ok("a", "ab"), ok("b", "abc")], "longest")
    assert res.value == "abc"
    assert res.strategy == "longest"
    assert "longest" in resolver.strategies


def test_registered_strategy_receives_options(resolver):
    resolver.register_strategy("pick", lambda results, index=0: results[index].value)
    assert resolver.resolve([ok("a", 1), ok("b", 2)], "pick", index=1).value == 2


def test_register_replace(resolver):
    with pytest.raises(ValidationError):
        resolver.register_strategy("majority", longest)
    resolver.register_strategy("majority", longest, replace=True)
    assert resolver.resolve([ok("a", "ab"), ok("b", "abc")]).value == "abc"


# --------------------------------------------------------------------------- invalid requests


def test_unknown_strategy_name(resolver):
    with pytest.raises(ValidationError):
        resolver.resolve([ok("a", 1)], "coin-flip")


def test_unknown_option(resolver):
    with pytest.raises(ValidationError):
        resolver.resolve([ok("a", 1)], "first_success", quorum=0.5)


def test_non_callable_strategy(resolver):
    with pytest.raises(ValidationError):
        resolver.resolve([ok("a", 1)], 42)


# =========================================================================== resource locks


async def until(predicate, timeout=1.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout)


async def test_readers_run_concurrently():
    locks = ResourceLockManager()
    both_in = asyncio.Event()
    inside = 0

    async def reader(name):
        nonlocal inside
        async with locks.hold(name, {"db": "read"}):
            inside += 1
            if inside == 2:
                both_in.set()
            await asyncio.wait_for(both_in.wait(), 1)

    await asyncio.wait_for(asyncio.gather(reader("r1"), reader("r2")), 1)
    assert locks.holders("db") == {}


async def test_writer_is_exclusive():
    locks = ResourceLockManager()
    order = []
    async with locks.hold("w", {"db": "write"}):

        async def reader():
            async with locks.hold("r", {"db": "read"}):
                order.append("r")

        task = asyncio.create_task(reader())
        await until(lambda: locks.waiting("db") == 1)
        assert locks.holders("db") == {"w": "write"}
        assert order == []
        order.append("w-done")
    await asyncio.wait_for(task, 1)
    assert order == ["w-done", "r"]


async def test_writers_exclude_each_other():
    locks = ResourceLockManager()
    active = 0
    peak = 0

    async def writer(name):
        nonlocal active, peak
        async with locks.hold(name, {"db": "write"}):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.wait_for(asyncio.gather(*(writer(f"w{i}") for i in range(5))), 1)
    assert peak == 1


async def test_invalid_mode():
    with pytest.raises(ValidationError):
        ResourceLockManager().hold("p", {"db": "append"})


async def test_invalid_owner_and_resource():
    locks = ResourceLockManager()
    with pytest.raises(ValidationError):
        locks.hold("", {"db": "read"})
    with pytest.raises(ValidationError):
        locks.hold("p", {"": "read"})


async def test_writer_not_starved_by_readers():
    locks = ResourceLockManager()
    order = []
    first_reader = locks.hold("r0", {"db": "read"})
    await first_reader.__aenter__()

    async def acquire(name, mode):
        async with locks.hold(name, {"db": mode}):
            order.append(name)
            await asyncio.sleep(0)

    writer = asyncio.create_task(acquire("w", "write"))
    await until(lambda: locks.waiting("db") == 1)
    late_readers = [asyncio.create_task(acquire(f"r{i}", "read")) for i in range(1, 4)]
    await until(lambda: locks.waiting("db") == 4)
    # Late readers are compatible with r0 but must queue behind the writer.
    assert locks.holders("db") == {"r0": "read"}
    await first_reader.__aexit__(None, None, None)
    await asyncio.wait_for(asyncio.gather(writer, *late_readers), 1)
    assert order[0] == "w"
    assert sorted(order[1:]) == ["r1", "r2", "r3"]


async def test_consecutive_waiting_readers_granted_together():
    locks = ResourceLockManager()
    w = locks.hold("w", {"db": "write"})
    await w.__aenter__()
    gate = asyncio.Event()

    async def reader(name):
        async with locks.hold(name, {"db": "read"}):
            await gate.wait()

    tasks = [asyncio.create_task(reader(f"r{i}")) for i in range(3)]
    await until(lambda: locks.waiting("db") == 3)
    await w.__aexit__(None, None, None)
    await until(lambda: len(locks.holders("db")) == 3)
    gate.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 1)


async def test_opposite_orderings_do_not_deadlock():
    locks = ResourceLockManager()

    async def worker(owner, first, second):
        for _ in range(50):
            # dict insertion order expresses the requested order
            async with locks.hold(owner, {first: "write", second: "write"}):
                await asyncio.sleep(0)

    await asyncio.wait_for(
        asyncio.gather(
            worker("p", "a", "b"),
            worker("q", "b", "a"),
            worker("r", "a", "b"),
            worker("s", "b", "a"),
        ),
        2,
    )
    assert locks.holders("a") == {} and locks.holders("b") == {}


async def test_release_on_exception():
    locks = ResourceLockManager()
    with pytest.raises(RuntimeError):
        async with locks.hold("p", {"db": "write", "cache": "read"}):
            assert locks.holders("db") == {"p": "write"}
            raise RuntimeError("boom")
    assert locks.holders("db") == {}
    assert locks.holders("cache") == {}


async def test_release_on_cancellation_while_holding():
    locks = ResourceLockManager()
    entered = asyncio.Event()

    async def holder():
        async with locks.hold("p", {"db": "write"}):
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(holder())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert locks.holders("db") == {}


async def test_cancellation_while_waiting_releases_everything():
    locks = ResourceLockManager()
    blocker = locks.hold("blocker", {"b": "write"})
    await blocker.__aenter__()

    async def waiter():
        async with locks.hold("p", {"a": "write", "b": "write"}):
            pass  # pragma: no cover - never reached

    task = asyncio.create_task(waiter())
    await until(lambda: locks.waiting("b") == 1)
    assert locks.holders("a") == {"p": "write"}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert locks.holders("a") == {}
    assert locks.waiting("b") == 0
    assert locks.holders("b") == {"blocker": "write"}
    await blocker.__aexit__(None, None, None)
    assert locks.holders("b") == {}


async def test_cancelled_waiter_unblocks_later_compatible_waiters():
    locks = ResourceLockManager()
    reader = locks.hold("r0", {"db": "read"})
    await reader.__aenter__()

    async def acquire(owner, mode):
        async with locks.hold(owner, {"db": mode}):
            await asyncio.sleep(10)

    writer = asyncio.create_task(acquire("w", "write"))
    await until(lambda: locks.waiting("db") == 1)
    late = asyncio.create_task(acquire("r1", "read"))
    await until(lambda: locks.waiting("db") == 2)
    writer.cancel()
    await until(lambda: "r1" in locks.holders("db"))
    assert locks.waiting("db") == 0
    late.cancel()
    await asyncio.gather(writer, late, return_exceptions=True)
    await reader.__aexit__(None, None, None)
    assert locks.holders("db") == {}


async def test_timeout_raises_conflict_error():
    locks = ResourceLockManager()
    async with locks.hold("w", {"db": "write"}):
        with pytest.raises(ConflictError):
            async with locks.hold("p", {"aaa": "write", "db": "read"}, timeout=0.05):
                pass  # pragma: no cover
        assert locks.holders("aaa") == {}
        assert "p" not in locks.holders("db")
        assert locks.waiting("db") == 0


async def test_timeout_zero_succeeds_when_free():
    locks = ResourceLockManager()
    async with locks.hold("p", {"db": "write"}, timeout=0):
        assert locks.holders("db") == {"p": "write"}


async def test_negative_timeout_rejected():
    with pytest.raises(ValidationError):
        ResourceLockManager().hold("p", {"db": "read"}, timeout=-1)


async def test_reentrant_request_rejected():
    locks = ResourceLockManager()
    async with locks.hold("p", {"db": "read"}):
        with pytest.raises(ConflictError):
            async with locks.hold("p", {"db": "read"}):
                pass  # pragma: no cover
        assert locks.holders("db") == {"p": "read"}
    assert locks.holders("db") == {}


async def test_reentrant_partial_request_releases_acquired():
    locks = ResourceLockManager()
    async with locks.hold("p", {"db": "write"}):
        with pytest.raises(ConflictError):
            async with locks.hold("p", {"aaa": "write", "db": "write"}):
                pass  # pragma: no cover
        assert locks.holders("aaa") == {}
        assert locks.holders("db") == {"p": "write"}


async def test_introspection():
    locks = ResourceLockManager()
    assert locks.holders("nothing") == {}
    assert locks.waiting("nothing") == 0
    async with locks.hold("w", {"db": "write"}):

        async def wait_for_db(owner):
            async with locks.hold(owner, {"db": "read"}):
                pass

        tasks = [asyncio.create_task(wait_for_db(o)) for o in ("x", "y")]
        await until(lambda: locks.waiting("db") == 2)
        assert locks.holders("db") == {"w": "write"}
        holders = locks.holders("db")
        holders["intruder"] = "write"  # returned mapping is a copy
        assert locks.holders("db") == {"w": "write"}
    await asyncio.wait_for(asyncio.gather(*tasks), 1)
    assert locks.waiting("db") == 0


async def test_empty_hold_is_noop():
    async with ResourceLockManager().hold("p", {}):
        pass


# --------------------------------------------------------------------------- static detection


def test_detect_write_conflicts():
    claims = {
        "s2": {"db": "write"},
        "s1": {"db": "read", "cache": "read"},
        "s3": {"cache": "read"},
    }
    assert detect_write_conflicts(claims) == [("db", "s1", "s2")]
    assert ResourceLockManager.detect_write_conflicts(claims) == [("db", "s1", "s2")]


def test_detect_write_conflicts_sorted_and_complete():
    claims = {
        "c": {"z": "write", "a": "write"},
        "b": {"z": "read", "a": "write"},
        "a": {"z": "read"},
    }
    assert detect_write_conflicts(claims) == [
        ("a", "b", "c"),
        ("z", "a", "c"),
        ("z", "b", "c"),
    ]


def test_detect_write_conflicts_invalid_mode():
    with pytest.raises(ValidationError):
        detect_write_conflicts({"s1": {"db": "rw"}})


# --------------------------------------------------------------------------- module boundaries


def test_module_imports_only_core_and_stdlib():
    import orchestrator.conflicts as mod

    tree = ast.parse(pathlib.Path(mod.__file__).read_text())
    local = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (
            node.level or (node.module or "").startswith("orchestrator")
        ):
            local.add(node.module)
    assert local <= {"core", "orchestrator.core"}


# --------------------------------------------------------------------------- audit regressions


def test_majority_int_and_str_keys_differ(resolver):
    res = resolver.resolve([ok("a", {1: "x"}), ok("b", {"1": "x"}), ok("c", {1: "x"})], "majority")
    assert res.value == {1: "x"}
    assert res.winners == ("a", "c")
    assert res.dissenters == ("b",)


def test_majority_list_and_tuple_differ(resolver):
    res = resolver.resolve([ok("a", [1, 2]), ok("b", (1, 2)), ok("c", [1, 2])], "majority")
    assert res.winners == ("a", "c")


def test_majority_repr_string_is_not_the_object(resolver):
    res = resolver.resolve([ok("a", "Ellipsis"), ok("b", ...), ok("c", "Ellipsis")], "majority")
    assert res.value == "Ellipsis"
    assert res.dissenters == ("b",)


def test_majority_int_equals_float(resolver):
    res = resolver.resolve([ok("a", 42), ok("b", 42.0)], "majority")
    assert res.winners == ("a", "b")
    assert res.confidence == 1.0


def test_majority_bool_is_not_int(resolver):
    res = resolver.resolve([ok("a", 1), ok("b", True), ok("c", 1)], "majority")
    assert res.winners == ("a", "c")
    assert res.dissenters == ("b",)


def test_majority_nested_structural_equality(resolver):
    a = {"k": [1, {"z": 2.0, "y": (3,)}], "n": None}
    b = {"n": None, "k": [1.0, {"y": (3,), "z": 2}]}
    res = resolver.resolve([ok("a", a), ok("b", b)], "majority")
    assert res.winners == ("a", "b")


def test_merge_int_and_float_leaves_are_equal(resolver):
    res = resolver.resolve([ok("a", {"n": 1}), ok("b", {"n": 1.0})], "merge")
    assert res.value == {"n": 1}
    assert res.details["conflicts"] == []


def test_merge_int_and_str_keys_stay_distinct(resolver):
    res = resolver.resolve([ok("a", {"m": {1: "x"}}), ok("b", {"m": {"1": "y"}})], "merge")
    assert res.value == {"m": {1: "x", "1": "y"}}
    assert res.details["conflicts"] == []


def test_merge_list_union_distinguishes_list_and_tuple(resolver):
    res = resolver.resolve([ok("a", {"l": [[1], (1,)]}), ok("b", {"l": [(1,), [1]]})], "merge")
    assert res.value == {"l": [[1], (1,)]}


def test_numeric_infinite_value_is_conflict(resolver):
    with pytest.raises(ConflictError, match="non-finite"):
        resolver.resolve([ok("a", 1), ok("b", float("inf"))], "numeric")
    with pytest.raises(ConflictError):
        resolver.resolve([ok("a", float("nan")), ok("b", 1)], "numeric")


@pytest.mark.parametrize("agg", ["mean", "median"])
def test_numeric_huge_ints_overflow_is_conflict(resolver, agg):
    with pytest.raises(ConflictError):
        resolver.resolve([ok("a", 10**400), ok("b", 1)], "numeric", agg=agg)


def test_numeric_huge_int_mixed_with_float_is_conflict(resolver):
    with pytest.raises(ConflictError):
        resolver.resolve([ok("a", 10**400), ok("b", 1.5)], "numeric", agg="max")


def test_numeric_huge_equal_ints(resolver):
    res = resolver.resolve([ok("a", 10**400), ok("b", 10**400)], "numeric")
    assert res.value == 10**400
    assert res.confidence == 1.0


def test_numeric_int_mean_is_exact(resolver):
    big = 10**17
    res = resolver.resolve([ok("a", big + 1), ok("b", big + 3)], "numeric")
    assert res.value == big + 2


def test_numeric_nearly_equal_values_below_full_confidence(resolver):
    res = resolver.resolve([ok("a", 1e18), ok("b", 1e18 + 256)], "numeric")
    assert 0.0 <= res.confidence < 1.0


def test_majority_name_tie_uses_smallest_name_in_group(resolver):
    res = resolver.resolve(
        [
            ok("m", "X", priority=5),
            ok("a", "X", priority=0),
            ok("b", "Y", priority=5),
            ok("y", "Y", priority=0),
        ],
        "majority",
    )
    assert res.value == "X"
    assert res.winners == ("m", "a")
    assert res.details["tie_broken"] is True


def test_majority_priority_tie_break_still_precedes_name(resolver):
    res = resolver.resolve(
        [
            ok("a", "X", priority=0),
            ok("z", "Y", priority=9),
            ok("b", "X", priority=0),
            ok("c", "Y"),
        ],
        "majority",
    )
    assert res.value == "Y"


# -- clarify-edge-cases: numeric overflow and type-aware majority -------------


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_numeric_each_non_finite_value_is_conflict(resolver, bad):
    with pytest.raises(ConflictError, match="non-finite"):
        resolver.resolve([ok("a", 1), ok("b", bad)], "numeric")


@pytest.mark.parametrize("agg", ["mean", "median"])
@pytest.mark.parametrize("values", [[1e308, 1e308], [10**400, 1]])
def test_numeric_aggregate_overflow_is_conflict(resolver, agg, values):
    results = [ok(f"t{i}", v) for i, v in enumerate(values)]
    with pytest.raises(ConflictError, match="overflowed"):
        resolver.resolve(results, "numeric", agg=agg)


def test_numeric_median_never_returns_inf(resolver):
    # (a + b) / 2 overflows silently for floats; it must not leak out as inf.
    with pytest.raises(ConflictError, match="median"):
        resolver.resolve([ok("a", 1.7e308), ok("b", 1.7e308)], "numeric", agg="median")


@pytest.mark.parametrize("agg", ["mean", "median", "min", "max"])
@pytest.mark.parametrize("values", [[-1.7e308, 1.7e308], [10**400, 1.5]])
def test_numeric_spread_overflow_is_conflict(resolver, agg, values):
    results = [ok(f"t{i}", v) for i, v in enumerate(values)]
    with pytest.raises(ConflictError, match="overflowed"):
        resolver.resolve(results, "numeric", agg=agg)


@pytest.mark.parametrize("agg", ["min", "max"])
def test_numeric_huge_finite_floats_without_overflow(resolver, agg):
    res = resolver.resolve([ok("a", 1e308), ok("b", 1e308)], "numeric", agg=agg)
    assert res.value == 1e308
    assert res.confidence == 1.0
    assert res.details["spread"] == 0


def test_majority_float_does_not_group_with_true(resolver):
    res = resolver.resolve([ok("a", 1.0), ok("b", True), ok("c", 1.0)], "majority")
    assert res.winners == ("a", "c")
    assert res.dissenters == ("b",)


@pytest.mark.parametrize("first,second", [(1, "1"), ([1], (1,)), (b"a", "a"), (0, False)])
def test_majority_type_aware_values_form_separate_groups(resolver, first, second):
    res = resolver.resolve([ok("a", first), ok("b", second)], "majority")
    assert res.details["groups"] == [["a"], ["b"]]
    assert res.winners == ("a",)


def test_majority_dictionary_keys_compared_type_aware(resolver):
    res = resolver.resolve(
        [ok("a", {1: "x"}), ok("b", {1.0: "x"}), ok("c", {"1": "x"}), ok("d", {True: "x"})],
        "majority",
    )
    assert res.details["groups"] == [["a", "b"], ["c"], ["d"]]
    assert res.value == {1: "x"}
    assert res.winners == ("a", "b")


def test_majority_nan_groups_with_nan(resolver):
    first, second = float("nan"), float("nan")
    assert first is not second  # exercise equality, not the identity shortcut
    res = resolver.resolve([ok("a", first), ok("b", second), ok("c", 0.0)], "majority")
    assert res.winners == ("a", "b")
    assert res.dissenters == ("c",)
