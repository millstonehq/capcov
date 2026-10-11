# Capability and evidence accounting

Read this when establishing breadth, assigning work or estimating remaining work.
Use capcov's existing feature, outcome, inventory and flow identities. No separate
work-unit inventory or parallel coverage engine is needed.

## Native identities and their roles

- Features are user-recognizable product capabilities. Decompose them into
  independently acceptable behavior while preserving the product hierarchy.
- Outcome obligations carry a `capability` reference to the owning feature and
  name required observable behavior, source references and executable bindings.
- Discovery inventories and flow catalogs ground source scope and expose unknown
  boundaries. Catalog roots are candidates, not automatically complete features.
- Transitions supply actor, prerequisites, state changes and assertions. Several
  transitions can demonstrate one outcome; several outcomes can qualify a feature.
- Native evidence records retain build/input identity, environment, execution
  completion and limitations. Structural `both` does not establish an outcome.

Inspect the pinned engine's supported contract before using a command or schema.
Use its feature/outcome reconciliation to validate identities and derive counts
from outcome evidence. Older consumers can retain scoped outcome reports while
migrating; manually supplied feature counts are not evidence-backed acceptance.
Follow the engine's maintained documentation rather than recreating schema or
aggregation rules in this skill. Do not weaken source/context checks to reuse an
old report.

When developing the engine and consumers in parallel, qualify downstream runs
against an immutable installed engine revision. A shared editable engine checkout
can change its fingerprint mid-run and invalidate otherwise useful execution.
Freeze and publish/build once, then run consumers on those exact bytes; do not
repeat expensive fixtures against a moving engine or restamp old receipts.

## Establish breadth, then refine acceptance

Start from incumbent entry points across the product: client actions, HTTP routes,
commands, schedules, workers and hooks. Map existing source identities to features
and required outcomes. Keep authored requirements source-anchored or explicitly
unresolved; don't invent expected results from candidate implementation alone.

Group aliases under the same feature when actors, prerequisites and observable
results agree. Keep refusal, retry and effect assertions as outcomes/cases of that
feature unless they represent independently required capabilities. Internal steps
and shared helpers can have implementation assignments without earning additional
capability completions. Shared outcome references must retain one identity.

For example, a report callback may demonstrate publication while retrieval remains
missing. Credit the publication outcome without claiming that an authorized user
can already obtain the report. Tests, routes and workers are not the denominator.

Every in-scope discovered finding remains mapped or visibly unmapped/unresolved.
Retain exclusions and their scope decisions. Feature configuration describes the
selected product, not permission to silently drop required functionality. A green
modeled subset does not establish whole-system coverage.

## Scheduling and progress

Attach worker ownership, candidate implementation pointers, effort and blockers to
existing feature/outcome IDs as planning metadata. Feature `requires` constrains
product configuration; flow prerequisites constrain execution. Neither is an
agent scheduling graph. Missing evidence alone does not mean missing code.

Choose a consistent reporting level of feature IDs; don't count an ancestor and
its descendants as separate deliverables. Report demonstrated capabilities,
partial outcome evidence and unknown areas alongside remaining release gates.
Credit useful scoped outcomes even while a broader feature remains incomplete.

Record model revisions and old-to-new IDs when splitting or merging features.
Recalculate comparisons consistently; renaming or regrouping earns no throughput.
Forecast from comparable newly completed capabilities with explicit complexity and
integration assumptions. Keep elapsed wall time, accounted goal time and summed
worker time distinct. Unknown scope is not zero remaining work.

## Academic boundary

Features/configurations, test requirements, state transitions and coverage criteria
have established foundations. Capcov's artifact schemas and rollup policies are
engineering choices. Reflexion Models inspire structural comparison; they do not
make entity agreement behavioral proof. Fixed-point convergence is over resolved
edges, and transition coverage is over the modeled reachable state space.
"Quasi-formality" is a workflow label, not a formal correctness guarantee or a
measured 80-percent confidence level.
