# Design

## Context

Most of the clarified behaviour already exists in `conflicts.py`, `router.py` and `core.py`; it was added by an audit and pinned by regression tests but never written into the main specs (see proposal.md, Why). Probing the code with edge inputs showed that the numeric strategy was only partly consistent with "overflow is a conflict":

| Input | `mean` | `median` | `min`/`max` |
| --- | --- | --- | --- |
| `[1e308, 1e308]` | ConflictError (fsum overflow) | **returned `inf`** | ok |
| `[-1.7e308, 1.7e308]` | `0.0`, **spread `inf`** | `0.0`, **spread `inf`** | ok value, **spread `inf`** |
| `[10**400, 1.5]` | ConflictError | ConflictError | ConflictError (spread) |

Float arithmetic overflows to `inf` silently, while int-to-float conversion raises `OverflowError`; that asymmetry produced the bold cells.

## Goals / Non-Goals

**Goals:**
- State the existing edge-case behaviour in the main specs with a test per scenario.
- Make the numeric strategy reject every non-finite aggregate or spread, whatever produced it.

**Non-Goals:**
- Changing how the `balanced` router normalises finite hints when an infinite hint is present (documented as-is).
- Validating required scope strings when a tool descriptor is built. Scope grammar belongs to permission-scoping, where a malformed required scope makes the permission decision fail closed; an existing test relies on the descriptor accepting such scopes.

## Decisions

- **Post-check the aggregate and the spread for finiteness** in `ConflictResolver._numeric` and raise `ConflictError`, instead of pre-checking magnitudes. A post-check catches every route to `inf` (fsum, `(a + b) / 2` in `median`, subtraction for the spread) without duplicating the aggregators' arithmetic. Alternative considered: computing in `fractions.Fraction`; rejected because it changes result types and costs more than the edge case is worth.
- **Treat an overflowing float spread like an overflowing int spread.** The int case already raised; reporting `spread: inf` with confidence 0 was the only path where the strategy returned a non-finite number in its details.
- **Infinite routing hints stay accepted.** "Unusable at any price" is a meaningful hint; ranking by negated keys already puts `-inf` last, and `_inverse_norm` already keeps the balanced score finite.

## Risks / Trade-offs

- [A caller relied on `median` returning `inf` or on `spread == inf`] → Both were results of overflow, not meaningful values; the error message names the aggregation and the values.
