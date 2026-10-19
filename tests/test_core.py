import pytest

from orchestrator import Capability, ToolSpec, ValidationError


async def _h(**_):
    return None


def test_valid_descriptor():
    spec = ToolSpec("web.search", "1.2.0", (Capability("search"),), _h)
    assert spec.name == "web.search"
    assert spec.version == "1.2.0"
    assert spec.capability_names == {"search"}
    assert spec.version_tuple == (1, 2, 0)


def test_capability_strings_are_coerced():
    spec = ToolSpec("t", "1.0.0", ("search",), _h)
    assert spec.capabilities == (Capability("search"),)


def test_missing_capabilities():
    with pytest.raises(ValidationError):
        ToolSpec("t", "1.0.0", (), _h)


def test_bad_version():
    with pytest.raises(ValidationError):
        ToolSpec("t", "1.2", ("search",), _h)


@pytest.mark.parametrize("name", ["", "Bad", "-x", "has space"])
def test_bad_name(name):
    with pytest.raises(ValidationError):
        ToolSpec(name, "1.0.0", ("search",), _h)


def test_sync_handler_rejected():
    with pytest.raises(ValidationError):
        ToolSpec("t", "1.0.0", ("search",), lambda: None)


def test_default_hints():
    cap = Capability("summarize")
    assert (cap.cost, cap.latency, cap.priority) == (1.0, 1.0, 0)


def test_negative_cost_rejected():
    with pytest.raises(ValidationError):
        Capability("x", cost=-1)


def test_negative_latency_rejected():
    with pytest.raises(ValidationError):
        Capability("x", latency=-0.1)


def test_validate_input():
    spec = ToolSpec("t", "1.0.0", ("search",), _h, input_schema={"q": str})
    spec.validate_input({"q": "hi"})
    with pytest.raises(ValidationError):
        spec.validate_input({})
    with pytest.raises(ValidationError):
        spec.validate_input({"q": 1})


# -- regression tests from the spec audit -------------------------------------


@pytest.mark.parametrize("name", ["calc\n", "calc\n\n", "١calc", "Calc"])
def test_tool_name_must_fully_match(name):
    with pytest.raises(ValidationError):
        ToolSpec(name, "1.0.0", ("search",), _h)


@pytest.mark.parametrize(
    "version", ["1.0.0\n", "01.0.0", "1.00.0", "1.0.01", "1.0.١", "1.0", 100]
)
def test_version_must_be_strict_semver(version):
    with pytest.raises(ValidationError):
        ToolSpec("calc", version, ("search",), _h)


def test_zero_version_parts_allowed():
    assert ToolSpec("calc", "0.10.0", ("search",), _h).version_tuple == (0, 10, 0)


def test_capability_name_must_fully_match():
    with pytest.raises(ValidationError):
        Capability("search\n")


@pytest.mark.parametrize("field", ["cost", "latency"])
@pytest.mark.parametrize("bad", [float("nan"), "a", None, True, -0.1, float("-inf")])
def test_capability_hints_must_be_non_negative_numbers(field, bad):
    with pytest.raises(ValidationError):
        Capability("search", **{field: bad})


def test_capability_infinite_hint_is_non_negative():
    assert Capability("search", cost=float("inf")).cost == float("inf")


def test_capability_priority_rejects_bool():
    with pytest.raises(ValidationError):
        Capability("search", priority=True)


# -- clarify-edge-cases -----------------------------------------------------------


@pytest.mark.parametrize("field", ["cost", "latency"])
def test_capability_accepts_infinite_hint(field):
    capability = Capability("search", **{field: float("inf")})
    assert getattr(capability, field) == float("inf")


@pytest.mark.parametrize("priority", [True, False, 1.0, "1", None])
def test_capability_priority_must_be_a_non_bool_int(priority):
    with pytest.raises(ValidationError):
        Capability("search", priority=priority)


@pytest.mark.parametrize("bad", ["search\n", "search\n\n"])
def test_capability_name_with_trailing_newline_rejected_in_descriptor(bad):
    with pytest.raises(ValidationError):
        ToolSpec("calc", "1.0.0", (bad,), _h)
