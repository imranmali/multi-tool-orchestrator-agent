# Tasks

## 1. Conflict resolution

- [x] 1.1 Reject a non-finite numeric aggregate (e.g. `median` of `[1e308, 1e308]`) with a conflict error in `src/orchestrator/conflicts.py`
- [x] 1.2 Reject a non-finite numeric spread (e.g. `[-1.7e308, 1.7e308]`) with a conflict error in `src/orchestrator/conflicts.py`
- [x] 1.3 Test numeric scenarios: boolean value, `inf`/`-inf`/NaN values, aggregate overflow for `mean` and `median` (float and huge int), spread overflow under every aggregation, huge equal ints aggregate exactly
- [x] 1.4 Test majority type-aware equality scenarios: int equals float, `True` vs `1` and `1.0`, `1` vs `"1"`, list vs tuple, dictionary keys `1`/`1.0`/`"1"`/`True`, NaN groups with NaN
- [x] 1.5 Test majority tie-break scenarios: priority, name, name over every group member (with `tie_broken` recorded), priority before name

## 2. Capability routing

- [x] 2.1 Test `priority` ranking with infinite cost and latency hints
- [x] 2.2 Test `cheapest` ranking puts an infinite cost after `1e300` and a finite cost
- [x] 2.3 Test `fastest` and `balanced` rankings put an infinite latency last
- [x] 2.4 Test balanced scores stay finite with infinite cost, infinite latency, and every hint infinite

## 3. Tool registry descriptors

- [x] 3.1 Test trailing-newline tool names, versions and capability names are rejected
- [x] 3.2 Test NaN cost/latency rejected, infinite cost/latency accepted, boolean and float priority rejected

## 4. Verification

- [x] 4.1 `openspec validate clarify-edge-cases --strict` passes
- [x] 4.2 Full test suite passes with `python3 -m pytest -q`

## Workflow follow-up

- Archive with `openspec archive clarify-edge-cases -y`, then run `openspec validate --specs --strict`.
