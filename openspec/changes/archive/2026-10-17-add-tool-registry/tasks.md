# Tasks

## 1. Project setup

- [x] 1.1 Create `pyproject.toml` (src layout, pytest config with `asyncio_mode = "auto"`, `pythonpath = ["src"]`) and `src/orchestrator/__init__.py`; verify `python3 -m pytest -q` collects without errors

## 2. Core model

- [x] 2.1 Implement error hierarchy, `Capability`, `ToolSpec` (name/version/capability validation, `validate_input`) and `ToolResult` in `src/orchestrator/core.py`; verify `tests/test_core.py` covers the descriptor and capability-hint scenarios

## 3. Registry

- [x] 3.1 Implement `ToolRegistry` register/unregister/get with version resolution and replace flag in `src/orchestrator/registry.py`; verify registration, duplicate and version scenarios in `tests/test_registry.py`
- [x] 3.2 Implement capability discovery, `capabilities()`, enable/disable; verify discovery and enable/disable scenarios in `tests/test_registry.py`
- [x] 3.3 Implement change listeners with failure isolation; verify notification scenarios in `tests/test_registry.py`
- [x] 3.4 Document the registry API in `README.md`; verify the README example matches the tested API

## Workflow follow-up

- Archive the change with `openspec archive add-tool-registry -y` once tests pass.
