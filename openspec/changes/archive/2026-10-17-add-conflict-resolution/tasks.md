# Tasks

## 1. Result reconciliation core

- [x] 1.1 Create `src/orchestrator/conflicts.py` with the frozen `Resolution` record, structural-equality helper, priority lookup and `ConflictResolver` dispatch (participant filtering, default strategy, unknown strategy/option rejection, `register_strategy`, custom-callable wrapping); verify the participation, resolution-record, default-strategy, custom-strategy and invalid-request scenarios in `tests/test_conflicts.py`
- [x] 1.2 Implement the `first_success`, `majority` (grouping, quorum, tie-breaking) and `priority` strategies; verify their scenarios and the priority-source scenarios in `tests/test_conflicts.py`

## 2. Structured and numeric strategies

- [x] 2.1 Implement the `merge` strategy (recursive dict merge, list union, conflict policies `error`/`priority`/`first`, conflicting paths in details, non-dict handling); verify the deep-merge, conflict-policy and non-dictionary scenarios in `tests/test_conflicts.py`
- [x] 2.2 Implement the `numeric` strategy (`mean`/`median`/`min`/`max`, confidence from spread, non-numeric and boolean rejection); verify the numeric-aggregation scenarios in `tests/test_conflicts.py`

## 3. Resource locking

- [x] 3.1 Implement `ResourceLockManager.hold` with per-resource readers-writer state, FIFO grant pass, sorted multi-resource acquisition, mode validation and re-entrancy rejection; verify the shared/exclusive, FIFO-fairness, deadlock-freedom (bounded by `asyncio.wait_for`) and re-entrancy scenarios in `tests/test_conflicts.py`
- [x] 3.2 Add release on exception and cancellation (holding and waiting), the whole-request timeout converted to a conflict error, and `holders`/`waiting` introspection; verify the release, timeout and introspection scenarios in `tests/test_conflicts.py`
- [x] 3.3 Implement `detect_write_conflicts` as a static method and module-level function; verify the static-detection scenario and invalid-mode rejection in `tests/test_conflicts.py`

## 4. Integration check

- [x] 4.1 Confirm `conflicts.py` imports only `orchestrator.core` and the standard library, and run `python3 -m pytest -q` so the full suite passes with the new tests

## Workflow follow-up

- Archive the change with `openspec archive add-conflict-resolution -y` once tests pass.
- Verify with `openspec list --specs` and `openspec validate conflict-resolution --type spec --strict`.
