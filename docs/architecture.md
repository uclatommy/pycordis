# Architecture

## Design Principles

1. **Upstream fidelity**: each module mirrors one upstream module in `packages/core/src`, with
   the same responsibilities, state names, and algorithms. The
   [divergence table](#divergences-from-upstream) lists every intended difference. Any other
   difference in behavior is a bug.
2. **No runtime dependencies**: the library uses only the standard library.
3. **No event loop**: pycordis does not use asyncio. A scheduler that the host selects drives
   the lifecycle on one owner thread. Other threads can only submit work to the scheduler and
   complete a `Deferred`.
4. **Static typing**: services are reached through typed keys, not through dynamic attributes.
5. **Host neutrality**: the library does not know about a specific application or GUI toolkit.
   A host adapts pycordis through public contracts: the scheduler protocol, thread-safe submit,
   and read-only introspection.

## Background

The Cordis paper (arXiv `2608.25512`) describes the calculus that the upstream implementation
follows: revertible effects, reactive coeffects, fibers, and the proofs of recovery, ordering,
progress, and confluence. The paper explains **why** a rule exists. The upstream source and
tests at the tracked revision define **what** pycordis does.

## Divergences from Upstream

| Divergence | Upstream | pycordis | Reason |
|---|---|---|---|

The table has no rows yet. A port that cannot follow upstream adds a row here in the same
change.
