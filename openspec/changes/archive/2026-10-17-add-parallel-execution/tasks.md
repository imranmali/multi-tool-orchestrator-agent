# Tasks

## 1. Plan model

- [x] 1.1 Implement `Ref` and `Step` in `src/orchestrator/executor.py` with construction-time validation (exactly one of tool/capability, retries/backoff/timeout bounds, resource modes) and implicit dependencies collected from nested `Ref`s; verify the step-definition and implicit-dependency scenarios in `tests/test_executor.py`
- [x] 1.2 Implement `Plan` with duplicate-id, unknown-dependency and cycle detection (cycle path in the message) plus `levels()` and `order()`; verify the plan-validation and plan-ordering scenarios in `tests/test_executor.py`

## 2. Executor core

- [x] 2.1 Implement `StepEvent`, `StepOutcome` and `ExecutionReport` (`ok`, `outputs`, `failed`, `skipped`, `cancelled`, `duration`, `raise_for_failure()`); verify the execution-report scenarios in `tests/test_executor.py`
- [x] 2.2 Implement `ParallelExecutor.run` with the completion-driven scheduler, semaphore-bounded invocation and output-reference resolution (whole, nested, dotted, unresolvable); verify the overlap, wall-clock speedup, dependency-ordering, no-layer-barrier, concurrency-limit and output-reference scenarios in `tests/test_executor.py`
- [x] 2.3 Add per-step/default timeouts and retries with exponential backoff (exceptions, timeouts and unsuccessful results); verify the timeout and retry scenarios, including attempt counts, in `tests/test_executor.py`
- [x] 2.4 Add failure propagation (transitive skip) and fail-fast cancellation; verify the dependents-skipped and fail-fast scenarios in `tests/test_executor.py`

## 3. Hooks

- [x] 3.1 Define the `LockManager` protocol and hold the lock (lock outside the semaphore, per attempt) for steps with resource claims; verify with a fake lock manager that claims are passed with the step id, the invocation happens inside the hold, and claim-less steps never call it
- [x] 3.2 Implement `default_invoker(registry)` (version pinning, disabled-tool refusal, input validation, capability-only error); verify the pluggable-invocation scenarios in `tests/test_executor.py`
- [x] 3.3 Implement the `on_event` callback with failure isolation; verify the event-sequence and callback-error scenarios in `tests/test_executor.py`

## 4. Integration check

- [x] 4.1 Run the full suite with `python3 -m pytest -q` and `openspec validate add-parallel-execution --strict`; verify both pass

## Workflow follow-up

- Archive the change with `openspec archive add-parallel-execution -y` once tests pass.
- Verify with `openspec list --specs` and `openspec validate parallel-execution --type spec --strict`.
- The integrator exports the executor's public names from `orchestrator/__init__.py` and composes routing, permissions and the lock manager through the invoker and lock-manager hooks.
