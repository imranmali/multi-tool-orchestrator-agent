import pytest

from orchestrator import Capability, ToolSpec


def make_tool(name="tool", version="1.0.0", caps=("search",), scopes=(), result=None, **kw):
    async def handler(**kwargs):
        return result if result is not None else {"tool": name, "args": kwargs}

    return ToolSpec(
        name=name,
        version=version,
        capabilities=tuple(c if isinstance(c, Capability) else Capability(c) for c in caps),
        handler=kw.pop("handler", handler),
        required_scopes=frozenset(scopes),
        **kw,
    )


@pytest.fixture
def tool_factory():
    return make_tool
