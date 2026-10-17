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
