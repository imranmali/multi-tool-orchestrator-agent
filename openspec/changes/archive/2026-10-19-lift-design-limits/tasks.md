# Tasks

## 1. Registry: name-level disablement covers every version

- [x] 1.1 Make `ToolRegistry._enabled` honour the name-level flag for every version, and make `enable(name)` clear every per-version flag of that name; verify with `tests/test_registry.py` scenarios for a pinned older version, a later-registered version, enabling the name clearing all flags, a version enable not overriding a name disable, and a full unregister forgetting the flags
- [x] 1.2 Keep one `enabled`/`disabled` event per call naming the given version or the latest one; verify with a registry test that a whole-tool disable over two versions emits exactly one event naming the latest
- [x] 1.3 Verify `default_invoker` refuses a pinned older version of a name-disabled tool with a test in `tests/test_registry.py`

## 2. Orchestrator: availability re-check before every invocation

- [x] 2.1 Add attempt outcome `unavailable` and a helper that checks the exact descriptor is still registered and enabled; call it at named-step start and after every `routed` event (named step, capability candidate, fan-out member); verify with `tests/test_agent.py` tests for a pinned version of a disabled tool, a tool disabled once routed, and the existing disabled-tool test
- [x] 2.2 Treat an unavailable capability candidate like a denial (skip, no fallback budget, no router report) and an unavailable fan-out member as a failed member; verify with tests for an unavailable candidate with `max_fallbacks=0`, a fallback candidate disabled mid-step, and a fan-out member disabled mid-run reconciled by majority

## 3. Orchestrator: resource claims verified at invocation

- [x] 3.1 Remember each step's own claims per run, add attempt outcome `claims_changed`, and check before invoking a named-tool step that the held locks cover the invoked descriptor's merged claims; verify with tests for a tool replaced with a new write claim (fails, not invoked) and one replaced with a narrower read claim (runs)

## 4. Integration

- [x] 4.1 Run `python3 -m pytest -q` and `openspec validate lift-design-limits --strict`; the validation and every test pass except the README "Tool registry" example, whose `assert registry.is_enabled("web.search", "1.0.0")` encodes the lifted limit and must become `assert not registry.is_enabled("web.search", "1.0.0")` in the README follow-up (the patched block was run and passes)

## Workflow follow-up

- Archive the change with `openspec archive lift-design-limits -y`.
- Run `openspec validate --specs --strict`.
- README (updated separately): registry example and reference table, `ToolAttempt.outcome` list, and the two limitation bullets.
