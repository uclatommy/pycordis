# Architecture

## Design Principles

1. **The semantic contract decides correctness**: pycordis implements the programming model of
   the Cordis paper. The [semantic contract](#semantic-contract) states the paper's guarantees in
   Python terms, and each guarantee has tests. Code that breaks a contract property is wrong,
   also when upstream has the same code.
2. **Upstream is the reference implementation**: the Cordis core package (`packages/core`) at
   the tracked revision supplies the observable behavior, the state names, the event names, and
   the error messages. The [conformance ledger](#conformance) records how each upstream test and
   symbol maps to pycordis, and an automatic check finds every upstream change that has not been
   reviewed.
3. **Python first at the surface**: where a Python feature gives a better API or a stronger
   guarantee, pycordis uses it and records the difference as a
   [divergence](#divergences-from-upstream). pycordis does not copy upstream code structure for
   its own sake.
4. **No runtime dependencies**: the library uses only the standard library.
5. **No event loop**: pycordis does not use asyncio. A scheduler that the host selects drives
   the lifecycle on one owner thread. Other threads can only submit work to the scheduler and
   complete a `Deferred`.
6. **Static typing**: services are reached through typed keys, not through dynamic attributes.
7. **Host neutrality**: the library does not know about a specific application or GUI toolkit.
   A host adapts pycordis through public contracts: the scheduler protocol, thread-safe submit,
   and read-only introspection.

## Background

The paper *A Programming Paradigm for Spatiotemporal Composability* (arXiv `2608.25512`) defines
the model. A component has **temporal composability** when the runtime can revert all of its
side effects when the component is removed. A component has **spatial composability** when it
declares the services it depends on and the runtime loads and unloads it as those services
change. The paper gives a calculus for both and proves its properties in Section 4.3. Section
4.4 extends the calculus with asynchrony, failure, configuration, and isolation. Cordis is the
reference implementation of the calculus (Section 5).

## Semantic Contract

Each row is a guarantee that pycordis keeps. The ID is stable: tests refer to it with
`@pytest.mark.contract("<ID>")`, and divergence rows refer to it. Every ID has at least one
test.

| ID | Guarantee | Paper |
|---|---|---|
| `EFF-REVERT` | Reverting a fiber's effects runs their inverses in reverse order of registration, and each revert restores the context state that its effect ran against. | Theorem 16, Theorem 68, Corollary 69 |
| `EFF-ISOLATE` | Operations at different keys are independent: the effects of different components can interleave, and the result is the same as if they ran one after the other. | Theorem 45, Theorem 47, Lemma 66 |
| `REG-WELLFORMED` | After every step, the fiber registry is well formed: a key has at most one provider in a realm, and each parent link names a live fiber. | Theorem 64 |
| `LIFE-ORDER` | A fiber starts to load only when every key it requires has a provider. A provider does not revert its effects while an installed dependent resolves a key to it. | Theorem 70 |
| `LIFE-COHERENT` | Every iteration of one load runs against one resolution of the fiber's dependencies. A change in that resolution during the load sends the fiber through unloading before it loads again. | Theorem 71 |
| `LIFE-PROGRESS` | With acyclic dependencies, every schedule reaches quiescence, and no fiber waits forever while a lifecycle step can run. | Theorem 73 |
| `LIFE-CONFLUENT` | Two schedules that make the same `use` and `dispose` calls reach the same quiescent state: the same fiber tree, the same provider for each key, and the same effects per fiber. A failed fiber is outside this guarantee. | Theorem 80 |
| `LIFE-FAILURE` | An `apply` that raises leaves its fiber with no installed effects and with the error recorded on the fiber. The error does not go to the parent or to siblings, and the fiber is not retried until its component or configuration changes. | Section 4.4, "Failure" |

## Conformance

pycordis measures its agreement with upstream at the level of behavior, in three layers:

| Layer | Question | Evidence |
|---|---|---|
| Semantic contract | Does pycordis keep the paper's guarantees? | Tests marked `contract`, including the seeded random-schedule suite |
| Upstream conformance | Does pycordis give the upstream result in each upstream test scenario? | Tests marked `upstream`, and the conformance ledger |
| Python surface | Is the API correct and clear for a Python developer? | The docstrings, the user guide, and their tests |

The conformance data is in `conformance/`:

- `upstream.json` is a generated inventory of the tracked upstream revision. It lists each
  upstream test case, each exported symbol, and the error messages, event names, and state
  names, with a hash of each item's normalized source. It holds no upstream code.
- `ledger.toml` has one entry for each inventory item. An entry gives the item's status
  (`ported`, `adapted`, `diverged`, or `not-applicable`), the pycordis symbol or test that
  covers it, the divergence ID where there is one, and the upstream hash at the time of review.

A test in the suite checks this data with no network access. It fails when:

- an inventory item has no ledger entry;
- a ledger hash differs from the inventory hash, which means that upstream changed the item
  after its review;
- a ledger entry names a pycordis symbol or test that does not exist, or a `ported` test case
  has no test with its `upstream` marker that runs;
- an upstream error message, event name, or state name does not occur in pycordis and has no
  divergence;
- a contract ID has no test, or a divergence row does not have the evidence its kind requires.

pycordis copies upstream control flow only where a contract proof depends on the exact
algorithm: the fiber state machine and the epoch digest (`LIFE-ORDER`, `LIFE-COHERENT`). For
these items the ledger entry sets `mirror = true`, and an upstream change to them is ported
with the same structure.

## Divergences from Upstream

A divergence is an intended difference from upstream in observable behavior or public surface.
Each row has a kind, and each kind needs its own evidence:

| Kind | Use when | Required evidence |
|---|---|---|
| `adaptation` | A Python construct replaces a TypeScript construct, and every upstream scenario gives the same result. | The affected upstream tests pass. |
| `improvement` | A Python feature gives a stronger guarantee or a clearer API, and some upstream scenario gives a different result. | The contract IDs it keeps, and a test marked `divergence("<ID>")`. |
| `correction` | Upstream breaks a contract property, and pycordis keeps the property. | The contract ID, a link to the upstream issue or pull request, and a test marked `divergence("<ID>")` that fails with the upstream behavior. |
| `omission` | pycordis does not port an upstream feature. | The reason. |

A new divergence needs the maintainer's approval before it is implemented. When an upstream
change removes the reason for a divergence, remove the divergence and its row.

| ID | Kind | Upstream | pycordis | Contract | Reason and evidence |
|---|---|---|---|---|---|

The table has no rows yet. A change that adds a divergence adds its row in the same change.
