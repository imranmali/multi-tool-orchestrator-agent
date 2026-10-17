import threading

import pytest

from conftest import make_tool
from orchestrator import Capability, NoRouteError, ToolRegistry, ValidationError
from orchestrator.router import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    STRATEGIES,
    CapabilityRouter,
    ToolStats,
)


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def cap(name="search", cost=1.0, latency=1.0, priority=0):
    return Capability(name, cost=cost, latency=latency, priority=priority)


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def router(registry, clock):
    return CapabilityRouter(registry, clock=clock)


def add(registry, name, *caps):
    return registry.register(make_tool(name, caps=caps or (cap(),)))


def names(specs):
    return [s.name for s in specs]


# -- candidate discovery ------------------------------------------------------


def test_only_offering_tools_are_candidates(registry, router):
    add(registry, "a", cap("search"))
    add(registry, "b", cap("search"))
    add(registry, "c", cap("math"))
    assert sorted(names(router.candidates("search"))) == ["a", "b"]


def test_disabled_tool_skipped(registry, router):
    add(registry, "a")
    add(registry, "b")
    registry.disable("a")
    assert names(router.candidates("search")) == ["b"]


def test_capability_nobody_offers(registry, router):
    add(registry, "a")
    assert router.candidates("teleport") == []


def test_latest_version_is_candidate(registry, router):
    registry.register(make_tool("a", version="1.0.0"))
    registry.register(make_tool("a", version="2.0.0"))
    assert [s.version for s in router.candidates("search")] == ["2.0.0"]


# -- eligibility and exclusion ------------------------------------------------


def test_predicate_rejects_tool(registry, router):
    add(registry, "a", cap(priority=9))
    add(registry, "b")
    eligible = lambda spec: spec.name != "a"  # noqa: E731
    assert names(router.candidates("search", eligible=eligible)) == ["b"]
    assert router.route("search", eligible=eligible).name == "b"


def test_excluded_tool_skipped(registry, router):
    add(registry, "a", cap(priority=9))
    add(registry, "b")
    assert router.route("search").name == "a"
    assert router.route("search", exclude=["a"]).name == "b"


def test_exclude_accepts_single_name_string(registry, router):
    add(registry, "a", cap(priority=9))
    add(registry, "b")
    assert router.route("search", exclude="a").name == "b"


def test_predicate_not_called_for_excluded_tools(registry, router):
    add(registry, "a")
    add(registry, "b")
    seen = []

    def eligible(spec):
        seen.append(spec.name)
        return True

    router.candidates("search", exclude={"a"}, eligible=eligible)
    assert seen == ["b"]


# -- route selection and errors ----------------------------------------------


def test_route_returns_best_candidate(registry, router):
    add(registry, "a", cap(priority=1))
    add(registry, "b", cap(priority=5))
    assert router.route("search", strategy="priority").name == "b"


def test_no_route_nobody_offers(registry, router):
    add(registry, "a")
    with pytest.raises(NoRouteError, match="no enabled tool offers capability 'teleport'"):
        router.route("teleport")


def test_no_route_offered_but_filtered(registry, router):
    add(registry, "a")
    add(registry, "b")
    with pytest.raises(NoRouteError) as info:
        router.route("search", exclude=["a"], eligible=lambda s: False)
    msg = str(info.value)
    assert "2 tools offer capability 'search'" in msg
    assert "a (excluded)" in msg
    assert "b (not eligible)" in msg


def test_no_route_message_mentions_open_circuit(registry, router):
    add(registry, "a")
    for _ in range(3):
        router.record_failure("a")
    with pytest.raises(NoRouteError, match=r"1 tool offers .*a \(circuit open\)"):
        router.route("search")


# -- strategies ---------------------------------------------------------------


def test_priority_strategy_priority_then_cost(registry, router):
    add(registry, "a", cap(priority=5, cost=9))
    add(registry, "b", cap(priority=5, cost=1))
    add(registry, "c", cap(priority=1, cost=0))
    assert names(router.candidates("search", strategy="priority")) == ["b", "a", "c"]


def test_priority_strategy_latency_breaks_cost_tie(registry, router):
    add(registry, "a", cap(priority=1, cost=1, latency=5))
    add(registry, "b", cap(priority=1, cost=1, latency=2))
    assert names(router.candidates("search", strategy="priority")) == ["b", "a"]


def test_cheapest_strategy(registry, router):
    add(registry, "a", cap(cost=3))
    add(registry, "b", cap(cost=1))
    add(registry, "c", cap(cost=2))
    assert names(router.candidates("search", strategy="cheapest")) == ["b", "c", "a"]


def test_cheapest_tie_broken_by_priority(registry, router):
    add(registry, "a", cap(cost=1, priority=0))
    add(registry, "b", cap(cost=1, priority=3))
    assert names(router.candidates("search", strategy="cheapest")) == ["b", "a"]


def test_fastest_strategy(registry, router):
    add(registry, "a", cap(latency=0.5))
    add(registry, "b", cap(latency=2.0))
    add(registry, "c", cap(latency=0.1))
    assert names(router.candidates("search", strategy="fastest")) == ["c", "a", "b"]


def test_default_strategy_is_balanced(registry):
    assert CapabilityRouter(registry).strategy == "balanced"
    assert STRATEGIES == {"priority", "cheapest", "fastest", "balanced"}


def test_balanced_dominating_tool_wins(registry, router):
    add(registry, "b", cap(priority=1, cost=5, latency=5))
    add(registry, "a", cap(priority=3, cost=1, latency=1))
    assert router.route("search").name == "a"


def test_balanced_failures_lower_score(registry, router):
    add(registry, "a")
    add(registry, "b")
    router.record_success("a")
    router.record_failure("a")
    router.record_success("b")
    assert router.route("search").name == "b"


def test_balanced_trades_off_priority_against_cost_and_latency(registry, router):
    # Documented formula: priority weight 0.4 exactly balances cost 0.2 + latency 0.2.
    add(registry, "a", cap(priority=1, cost=10, latency=10))
    add(registry, "b", cap(priority=0, cost=0, latency=0))
    explained = {e.tool: e.score for e in router.explain("search")}
    assert explained["a"] == pytest.approx(0.4 + 0.0 + 0.0 + 0.2)
    assert explained["b"] == pytest.approx(0.0 + 0.2 + 0.2 + 0.2)
    assert router.route("search").name == "a"  # 0.6 vs 0.6 tie -> name


def test_custom_strategy_higher_is_better(registry, router):
    add(registry, "a")
    add(registry, "b")
    scores = {"a": 1, "b": 10}
    seen = []

    def score(spec, capability, stats):
        seen.append((capability.name, type(stats)))
        return scores[spec.name]

    assert router.route("search", strategy=score).name == "b"
    assert seen and all(c == "search" and t is ToolStats for c, t in seen)


def test_custom_strategy_on_router(registry, clock):
    add(registry, "a")
    add(registry, "b")
    router = CapabilityRouter(registry, strategy=lambda s, c, st: -ord(s.name[0]), clock=clock)
    assert router.route("search").name == "a"


@pytest.mark.parametrize("bad", ["high", None, True, float("nan")])
def test_custom_strategy_non_numeric_rejected(registry, router, bad):
    add(registry, "a")
    with pytest.raises(ValidationError):
        router.route("search", strategy=lambda s, c, st: bad)


def test_per_call_strategy_override(registry, clock):
    add(registry, "a", cap(priority=9, cost=5))
    add(registry, "b", cap(priority=0, cost=1))
    router = CapabilityRouter(registry, strategy="priority", clock=clock)
    assert router.route("search").name == "a"
    assert router.route("search", strategy="cheapest").name == "b"


def test_unknown_strategy_rejected(registry):
    with pytest.raises(ValidationError, match="random"):
        CapabilityRouter(registry, strategy="random")


def test_unknown_per_call_strategy_rejected(registry, router):
    add(registry, "a")
    with pytest.raises(ValidationError):
        router.route("search", strategy="random")


def test_non_callable_strategy_rejected(registry):
    with pytest.raises(ValidationError):
        CapabilityRouter(registry, strategy=42)


@pytest.mark.parametrize("strategy", sorted(STRATEGIES) + ["custom"])
def test_ties_broken_by_name(registry, router, strategy):
    for n in ("zeta", "alpha", "mid"):
        add(registry, n, cap(cost=2, latency=3, priority=1))
    chosen = (lambda s, c, st: 1.0) if strategy == "custom" else strategy
    assert names(router.candidates("search", strategy=chosen)) == ["alpha", "mid", "zeta"]


# -- health statistics --------------------------------------------------------


def test_stats_counts_and_averages(router):
    router.record_success("a", latency=1.0)
    router.record_success("a", latency=3.0)
    router.record_failure("a")
    st = router.stats("a")
    assert (st.successes, st.failures, st.consecutive_failures) == (2, 1, 1)
    assert st.avg_latency == pytest.approx(2.0)
    assert st.success_rate == pytest.approx(2 / 3)


def test_stats_unobserved_tool(router):
    st = router.stats("nobody")
    assert (st.successes, st.failures, st.consecutive_failures) == (0, 0, 0)
    assert st.avg_latency is None
    assert st.success_rate == 1.0
    assert st.circuit == CLOSED


def test_success_resets_consecutive_failures(router):
    router.record_failure("a")
    router.record_failure("a")
    router.record_success("a")
    assert router.stats("a").consecutive_failures == 0
    assert router.stats("a").failures == 2


def test_success_without_latency_not_averaged(router):
    router.record_success("a")
    router.record_success("a", latency=4.0)
    assert router.stats("a").avg_latency == 4.0


def test_negative_latency_rejected(router):
    with pytest.raises(ValidationError):
        router.record_success("a", latency=-1)


def test_reset(router):
    router.record_failure("a")
    router.record_failure("b")
    router.reset("a")
    assert router.stats("a").failures == 0
    assert router.stats("b").failures == 1
    router.reset()
    assert router.stats("b").failures == 0


# -- circuit breaker ----------------------------------------------------------


def test_circuit_opens_at_threshold(registry, router):
    add(registry, "a")
    add(registry, "b")
    for _ in range(3):
        router.record_failure("a")
    assert router.circuit_state("a") == OPEN
    assert names(router.candidates("search")) == ["b"]


def test_circuit_stays_closed_below_threshold(registry, router):
    add(registry, "a")
    router.record_failure("a")
    router.record_failure("a")
    assert router.circuit_state("a") == CLOSED
    assert names(router.candidates("search")) == ["a"]


def test_custom_threshold(registry, clock):
    add(registry, "a")
    router = CapabilityRouter(registry, failure_threshold=1, clock=clock)
    router.record_failure("a")
    assert router.circuit_state("a") == OPEN


@pytest.mark.parametrize(
    "kwargs", [{"failure_threshold": 0}, {"cooldown": -1}, {"failure_threshold": 1.5}]
)
def test_invalid_configuration(registry, kwargs):
    with pytest.raises(ValidationError):
        CapabilityRouter(registry, **kwargs)


def _open(router, clock, name="a", at=0.0):
    clock.now = at
    for _ in range(router.failure_threshold):
        router.record_failure(name)


def test_half_open_after_cooldown(registry, router, clock):
    add(registry, "a")
    _open(router, clock)
    clock.now = 30.0
    assert router.circuit_state("a") == HALF_OPEN
    assert names(router.candidates("search")) == ["a"]


def test_still_open_before_cooldown(registry, router, clock):
    add(registry, "a")
    _open(router, clock)
    clock.now = 29.0
    assert router.circuit_state("a") == OPEN
    assert router.candidates("search") == []


def test_success_closes_half_open_circuit(registry, router, clock):
    add(registry, "a")
    _open(router, clock)
    clock.now = 31.0
    router.record_success("a")
    st = router.stats("a")
    assert st.circuit == CLOSED
    assert st.consecutive_failures == 0
    # Fully closed: needs a full threshold of failures to reopen.
    router.record_failure("a")
    assert router.circuit_state("a") == CLOSED


def test_failure_reopens_half_open_circuit(registry, router, clock):
    add(registry, "a")
    _open(router, clock)
    clock.now = 40.0
    assert router.circuit_state("a") == HALF_OPEN
    router.record_failure("a")
    assert router.circuit_state("a") == OPEN
    clock.now = 69.9
    assert router.candidates("search") == []
    clock.now = 70.0
    assert names(router.candidates("search")) == ["a"]


def test_zero_cooldown_is_immediately_half_open(registry, clock):
    add(registry, "a")
    router = CapabilityRouter(registry, cooldown=0, clock=clock)
    _open(router, clock)
    assert router.circuit_state("a") == HALF_OPEN


# -- multi-capability routing -------------------------------------------------


def test_route_many(registry, router):
    add(registry, "s", cap("search"))
    add(registry, "m", cap("math"))
    result = router.route_many(["search", "math", "search"])
    assert {k: v.name for k, v in result.items()} == {"search": "s", "math": "m"}
    assert list(result) == ["search", "math"]


def test_route_many_applies_filters(registry, router):
    add(registry, "s1", cap("search", priority=9), cap("math", priority=9))
    add(registry, "s2", cap("search"), cap("math"))
    result = router.route_many(iter(["search", "math"]), exclude=iter(["s1"]))
    assert {v.name for v in result.values()} == {"s2"}


def test_route_many_unroutable(registry, router):
    add(registry, "s", cap("search"))
    with pytest.raises(NoRouteError, match="teleport"):
        router.route_many(["search", "teleport"])


# -- explanation --------------------------------------------------------------


def test_explain_lists_ranked_and_rejected(registry, router):
    add(registry, "a", cap(priority=2))
    add(registry, "b", cap(priority=1))
    add(registry, "c", cap(priority=9))
    router.record_success("a", latency=0.5)
    out = router.explain("search", exclude=["c"], strategy="priority")
    assert [(e.tool, e.rank, e.reason) for e in out] == [
        ("a", 1, None),
        ("b", 2, None),
        ("c", None, "excluded"),
    ]
    assert out[0].selected and not out[1].selected
    assert out[0].score == 2.0 and out[1].score == 1.0
    assert out[0].key == (2.0, -1.0, -1.0)
    assert out[2].score is None and out[2].key is None
    assert out[0].stats.avg_latency == 0.5
    assert out[0].version == "1.0.0"


def test_explain_reports_circuit_and_eligibility(registry, router):
    add(registry, "a")
    add(registry, "b")
    add(registry, "c")
    for _ in range(3):
        router.record_failure("b")
    out = router.explain("search", eligible=lambda s: s.name != "c")
    reasons = {e.tool: e.reason for e in out}
    assert reasons == {"a": None, "b": "circuit open", "c": "not eligible"}
    assert {e.tool: e.stats.circuit for e in out}["b"] == OPEN


def test_explain_unknown_capability_is_empty(router):
    assert router.explain("teleport") == []


# -- thread safety ------------------------------------------------------------


def test_concurrent_recording_is_exact(registry):
    router = CapabilityRouter(registry, failure_threshold=10_000)
    n_threads, per_thread = 8, 500

    def work():
        for i in range(per_thread):
            if i % 2:
                router.record_success("a", latency=1.0)
            else:
                router.record_failure("a")

    threads = [threading.Thread(target=work) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st = router.stats("a")
    assert st.successes == st.failures == n_threads * per_thread // 2
    assert st.avg_latency == 1.0
