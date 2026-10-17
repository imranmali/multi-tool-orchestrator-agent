# Tasks

## 1. Health tracking and circuit breaker

- [x] 1.1 Implement `ToolStats`, `CapabilityRouter.__init__` config validation, `record_success`, `record_failure`, `stats`, `circuit_state` and `reset` with an injected clock in `src/orchestrator/router.py`; verify the health-statistics, invalid-configuration, circuit-open and half-open scenarios in `tests/test_router.py` using a fake clock (no sleeping)

## 2. Candidate discovery and filtering

- [x] 2.1 Implement `candidates()` on top of `registry.find_by_capability` with the eligibility predicate, exclusion list and open-circuit filtering; verify the candidate-discovery, eligibility, exclusion and circuit-filtering scenarios in `tests/test_router.py`
- [x] 2.2 Implement `route()` and descriptive `NoRouteError` messages (nobody offers vs. offered-but-filtered with per-tool reasons); verify route-selection and no-route scenarios in `tests/test_router.py`

## 3. Ranking strategies

- [x] 3.1 Implement `priority`, `cheapest`, `fastest` and the default `balanced` strategy with name tie-breaking; verify each strategy's ordering scenario, the balanced scenarios and the deterministic tie-break scenario in `tests/test_router.py`
- [x] 3.2 Implement custom callable strategies (higher is better, non-numeric rejected), strategy resolution with per-call override and unknown-name rejection; verify the custom-scoring and strategy-selection scenarios in `tests/test_router.py`

## 4. Convenience and observability

- [x] 4.1 Implement `route_many()` and `explain()` returning per-tool rank, score, reason and stats; verify the multi-capability and explanation scenarios in `tests/test_router.py`
- [x] 4.2 Verify thread-safety by recording outcomes from several threads concurrently and asserting exact totals in `tests/test_router.py`; run the full suite with `python3 -m pytest -q`

## Workflow follow-up

- Archive the change with `openspec archive add-capability-routing -y` once tests pass.
- Verify the archived spec with `openspec validate capability-routing --type spec --strict`.
- Integrator: export the router API from `orchestrator/__init__.py` and wire permission checks in as the `eligible` predicate (outside this change's files).
