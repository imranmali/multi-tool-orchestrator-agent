# Proposal

## Why

An earlier audit hardened several edge cases (non-finite numbers, type-aware equality, infinite routing hints, trailing-newline identifiers), but the main specs never stated the resulting behaviour, so it could regress silently. Re-reading the code also turned up two numeric gaps: `median` of two huge floats returned `inf`, and a float spread that overflowed was reported as `inf` while the same overflow with an int was rejected.

## What Changes

- conflict-resolution, numeric strategy: state that booleans are not numbers, that `inf`, `-inf` and NaN are rejected with a conflict error, and that an aggregate or a smallest-to-largest spread that cannot be represented as a finite number is rejected with a conflict error. **Behaviour fix**: `median` producing `inf` from finite inputs and a float spread overflowing to `inf` are now conflict errors (both were previously accepted silently).
- conflict-resolution, majority: state the type-aware equality rules (`1` equals `1.0`; `True` equals neither `1` nor `1.0`; `1` differs from `"1"`; lists never equal tuples; dictionary keys `1` and `"1"` differ while `1` and `1.0` match; NaN equals NaN) and that the tie-break name is the smallest tool name among all members of each tied group, after the highest member priority.
- capability-routing: state that infinite cost/latency hints rank after every finite hint under `priority`, `cheapest` and `fastest`, and that the `balanced` score stays finite (an infinite hint scores the worst normalised component).
- tool-registry: state that NaN cost/latency, boolean or non-integer priority are rejected, that infinite cost/latency is accepted, and that tool names, capability names and versions with a trailing newline are rejected.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `conflict-resolution`: Majority vote by structural equality, Deterministic majority tie-breaking, and Numeric aggregation gain precise edge-case wording and scenarios.
- `capability-routing`: Priority, Cheapest, Fastest and Balanced default strategies state how infinite hints rank.
- `tool-registry`: Tool descriptor validation and Capability declarations carry routing hints state the rejected and accepted edge values.

## Impact

- `src/orchestrator/conflicts.py`: `ConflictResolver._numeric` rejects a non-finite aggregate or spread.
- Tests: `tests/test_conflicts.py`, `tests/test_router.py`, `tests/test_core.py` gain scenarios for each clarified behaviour.
- No public API changes; callers relying on `median` returning `inf`, or on an `inf` spread in details, now get a `ConflictError`.
