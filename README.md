# Multi-Tool Orchestrator Agent

A dependency-free Python library that coordinates many tools behind one agent:

- **Dynamic tool registry** — register, version, enable/disable and observe tools at runtime.
- **Capability-based routing** — pick the best tool for a capability, with fallbacks.
- **Permission scoping** — principals, scopes and policies decide who can call what.
- **Parallel execution** — dependency-aware plans run concurrently with timeouts and retries.
- **Conflict resolution** — reconcile disagreeing results and contended resources.

Specifications live in [`openspec/specs`](openspec/specs) and were developed spec-first with [OpenSpec](https://github.com/Fission-AI/OpenSpec); completed changes are in `openspec/changes/archive`.

## Install & test

```bash
pip install -e '.[dev]'
python3 -m pytest -q
```

## Tool registry

```python
from orchestrator import Capability, ToolRegistry, ToolSpec

async def search(query: str):
    return [f"result for {query}"]

registry = ToolRegistry()
registry.subscribe(lambda event: print(event.kind, event.name, event.version))
registry.register(ToolSpec(
    name="web.search",
    version="1.2.0",
    capabilities=(Capability("search", cost=0.5, latency=2.0, priority=1),),
    handler=search,
    required_scopes={"net:read"},
    input_schema={"query": str},
))

registry.get("web.search")                 # latest version
registry.get("web.search", "1.2.0")        # pinned version
registry.find_by_capability("search")      # enabled tools offering a capability
registry.disable("web.search")             # hidden from discovery, still retrievable
registry.unregister("web.search")
```
