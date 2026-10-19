import pytest

from orchestrator import DuplicateToolError, RegistryEvent, ToolNotFoundError, ToolRegistry


def test_register_then_lookup(tool_factory):
    reg = ToolRegistry()
    spec = reg.register(tool_factory("calc", caps=("math",)))
    assert reg.get("calc") is spec
    assert "calc" in reg and len(reg) == 1


def test_duplicate_registration(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc"))
    with pytest.raises(DuplicateToolError):
        reg.register(tool_factory("calc"))


def test_replace_registration(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc"))
    new = tool_factory("calc", description="v2")
    reg.register(new, replace=True)
    assert reg.get("calc") is new


def test_unregister(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc"))
    reg.unregister("calc")
    with pytest.raises(ToolNotFoundError):
        reg.get("calc")
    with pytest.raises(ToolNotFoundError):
        reg.unregister("calc")


def test_version_resolution(tool_factory):
    reg = ToolRegistry()
    for v in ("1.0.0", "1.10.0", "1.2.0"):
        reg.register(tool_factory("calc", version=v))
    assert reg.get("calc").version == "1.10.0"
    assert reg.get("calc", "1.0.0").version == "1.0.0"
    assert reg.versions("calc") == ["1.0.0", "1.2.0", "1.10.0"]
    with pytest.raises(ToolNotFoundError):
        reg.get("calc", "9.9.9")
    reg.unregister("calc", "1.10.0")
    assert reg.get("calc").version == "1.2.0"


def test_find_by_capability(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("a", caps=("search",)))
    reg.register(tool_factory("b", caps=("search", "summarize")))
    reg.register(tool_factory("c", caps=("math",)))
    assert {s.name for s in reg.find_by_capability("search")} == {"a", "b"}
    assert reg.find_by_capability("teleport") == []
    assert reg.capabilities() == {"search", "summarize", "math"}


def test_enable_disable(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("a", caps=("search",)))
    reg.disable("a")
    assert reg.find_by_capability("search") == []
    assert reg.get("a").name == "a"
    assert not reg.is_enabled("a")
    reg.enable("a")
    assert [s.name for s in reg.find_by_capability("search")] == ["a"]


def test_listener_receives_events(tool_factory):
    reg = ToolRegistry()
    events: list[RegistryEvent] = []
    unsubscribe = reg.subscribe(events.append)
    reg.register(tool_factory("a", version="2.0.0"))
    reg.disable("a")
    reg.enable("a")
    reg.unregister("a")
    assert [e.kind for e in events] == ["registered", "disabled", "enabled", "unregistered"]
    assert events[0] == RegistryEvent("registered", "a", "2.0.0")
    unsubscribe()
    reg.register(tool_factory("b"))
    assert len(events) == 4


def test_failing_listener_isolated(tool_factory):
    reg = ToolRegistry()
    seen = []

    def boom(event):
        raise RuntimeError("listener failure")

    reg.subscribe(boom)
    reg.subscribe(seen.append)
    reg.register(tool_factory("a"))
    assert "a" in reg
    assert len(seen) == 1


# -- regression tests from the spec audit -------------------------------------


def test_name_disable_survives_new_version(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc", "1.0.0", caps=("math",)))
    reg.disable("calc")
    reg.register(tool_factory("calc", "1.1.0", caps=("math",)))
    assert reg.find_by_capability("math") == []
    assert not reg.is_enabled("calc")
    assert not reg.is_enabled("calc", "1.1.0")
    assert reg.list(include_disabled=False) == []
    reg.enable("calc")
    assert [s.version for s in reg.find_by_capability("math")] == ["1.1.0"]
    assert reg.is_enabled("calc")


def test_name_disable_survives_unregistering_latest(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc", "1.0.0", caps=("math",)))
    reg.register(tool_factory("calc", "2.0.0", caps=("math",)))
    reg.disable("calc")
    reg.unregister("calc", "2.0.0")
    assert reg.find_by_capability("math") == []
    assert not reg.is_enabled("calc")


def test_version_disable_is_version_scoped(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc", "1.0.0", caps=("math",)))
    reg.disable("calc", "1.0.0")
    reg.register(tool_factory("calc", "1.1.0", caps=("math",)))
    assert [s.version for s in reg.find_by_capability("math")] == ["1.1.0"]
    assert not reg.is_enabled("calc", "1.0.0")


def test_name_disable_forgotten_after_full_unregister(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc", caps=("math",)))
    reg.disable("calc")
    reg.unregister("calc")
    reg.register(tool_factory("calc", caps=("math",)))
    assert reg.is_enabled("calc")
    assert [s.name for s in reg.find_by_capability("math")] == ["calc"]


def test_newline_suffixed_version_cannot_dodge_duplicate_check(tool_factory):
    from orchestrator import ValidationError

    reg = ToolRegistry()
    reg.register(tool_factory("calc", "1.0.0"))
    with pytest.raises(ValidationError):
        reg.register(tool_factory("calc", "1.0.0\n"))
    assert reg.versions("calc") == ["1.0.0"]


# -- lift-design-limits: name-level disablement covers every version ----------


def test_name_disable_covers_pinned_older_version(tool_factory):
    reg = ToolRegistry()
    old = reg.register(tool_factory("calc", "1.0.0", caps=("math",)))
    reg.register(tool_factory("calc", "2.0.0", caps=("math",)))
    reg.disable("calc")
    assert not reg.is_enabled("calc", "1.0.0")
    assert not reg.is_enabled("calc", "2.0.0")
    assert not reg.is_enabled("calc")
    assert reg.get("calc", "1.0.0") is old  # still retrievable
    assert reg.find_by_capability("math") == []


def test_name_disable_covers_later_version(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc", "1.0.0", caps=("math",)))
    reg.disable("calc")
    reg.register(tool_factory("calc", "3.0.0", caps=("math",)))
    assert not reg.is_enabled("calc", "3.0.0")
    assert not reg.is_enabled("calc", "1.0.0")
    assert reg.find_by_capability("math") == []
    assert reg.capabilities() == set()


def test_version_disable_leaves_other_versions_enabled(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc", "1.0.0", caps=("math",)))
    reg.register(tool_factory("calc", "2.0.0", caps=("math",)))
    reg.disable("calc", "1.0.0")
    assert not reg.is_enabled("calc", "1.0.0")
    assert reg.is_enabled("calc", "2.0.0")
    assert [s.version for s in reg.find_by_capability("math")] == ["2.0.0"]


def test_version_enable_does_not_override_name_disable(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc", "1.0.0", caps=("math",)))
    reg.register(tool_factory("calc", "2.0.0", caps=("math",)))
    reg.disable("calc")
    reg.enable("calc", "1.0.0")
    assert not reg.is_enabled("calc", "1.0.0")
    assert not reg.is_enabled("calc", "2.0.0")
    reg.enable("calc")
    assert reg.is_enabled("calc", "1.0.0") and reg.is_enabled("calc", "2.0.0")


def test_name_enable_clears_every_version_flag(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc", "1.0.0", caps=("math",)))
    reg.register(tool_factory("calc", "2.0.0", caps=("math",)))
    reg.register(tool_factory("other", "1.0.0", caps=("math",)))
    reg.disable("calc", "1.0.0")
    reg.disable("calc", "2.0.0")
    reg.disable("other", "1.0.0")
    reg.disable("calc")
    reg.enable("calc")
    assert reg.is_enabled("calc", "1.0.0") and reg.is_enabled("calc", "2.0.0")
    assert not reg.is_enabled("other", "1.0.0")  # other names keep their flags


def test_full_unregister_forgets_name_and_version_flags(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc", "1.0.0", caps=("math",)))
    reg.disable("calc", "1.0.0")
    reg.disable("calc")
    reg.unregister("calc")
    reg.register(tool_factory("calc", "1.0.0", caps=("math",)))
    assert reg.is_enabled("calc", "1.0.0")


def test_whole_tool_toggle_emits_one_event_naming_latest(tool_factory):
    reg = ToolRegistry()
    reg.register(tool_factory("calc", "1.0.0"))
    reg.register(tool_factory("calc", "2.0.0"))
    events: list[RegistryEvent] = []
    reg.subscribe(events.append)
    reg.disable("calc")
    reg.enable("calc")
    reg.disable("calc", "1.0.0")
    reg.enable("calc", "1.0.0")
    assert events == [
        RegistryEvent("disabled", "calc", "2.0.0"),
        RegistryEvent("enabled", "calc", "2.0.0"),
        RegistryEvent("disabled", "calc", "1.0.0"),
        RegistryEvent("enabled", "calc", "1.0.0"),
    ]


async def test_default_invoker_refuses_pinned_version_of_disabled_name(tool_factory):
    from orchestrator import ExecutionError, ParallelExecutor, Plan, Step, default_invoker

    reg = ToolRegistry()
    calls: list[str] = []

    async def handler(**kwargs):
        calls.append("1.0.0")
        return "v1"

    reg.register(tool_factory("calc", "1.0.0", handler=handler))
    reg.register(tool_factory("calc", "2.0.0"))
    reg.disable("calc")
    report = await ParallelExecutor(default_invoker(reg)).run(
        Plan([Step("a", tool="calc", version="1.0.0")])
    )
    assert report["a"].status == "failed"
    assert isinstance(report["a"].exception, ExecutionError)
    assert "calc@1.0.0 is disabled" in report["a"].error
    assert calls == []
