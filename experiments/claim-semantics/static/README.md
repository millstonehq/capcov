# Static rule pack and adversarial static cases (EXPERIMENT-PLAN section 29)

This directory holds the reviewed program that turns SCIP and tree-sitter
observations into program claims, and a corpus of synthetic cases whose
verdicts a reviewer can decide from the facts and the rules alone.

* `rules-static-v1.json` - the rule pack in raw IR JSON wire form.
* `cases/NN-*.json` - one positive control (`00`) and the nine adversarial
  shapes of section 29 (`01`..`09`), with labelled variants for `02`, `04`
  and `09`.
* `expected.json` - the per-claim review tables duplicated from every case.

The corpus adapter in `tests/claim_semantics/adapter.py` cannot express
recursion, so the rules stay raw IR JSON and
`tests/claim_semantics/static_rules/adapter.py` merges pack + case into one
`Bundle` through `bundle_from_json(..., validate=True)`.  Tests live in
`tests/claim_semantics/test_static_corpus_schema.py` (evaluator independent)
and `test_static_corpus_evaluation.py` (Python reference evaluator and the
Python/Souffle differential).

## The rule pack

`primitives` is a byte-identical copy of
`src/capcov/claims/static/schema_static_v1.json` (`relations`); the test
asserts list equality, canonical digest equality and that the pretty-printed
bytes appear verbatim in the pack file.  `supplementary_primitives` declares
what section 29 names but the frozen file does not carry:

| relation | why |
|---|---|
| `runtime_route_observed(tenant, surface, event, run)` | runtime side of `runtime_route_without_static`; same shape as `tests/claim_semantics/corpus/schema-v1.json` |
| `generated_code_in_scope__accepted(index, path)` / `__rejected` | the case-04 assumptions named in the section-29 table |

`derived` declares the twenty section-29 relations plus the two projections
that the frozen completeness witnesses already point at
(`scip_documents_closed` completes `scip_document_path`,
`scip_definitions_closed` completes `scip_definition_site_at`).  All derived
relations are `binding=static`, `context_indices=["index"]`, except
`runtime_route_without_static` (runtime, negative polarity, context
`tenant/surface/run/index`).  Claim-modality relations: `static_capability`,
`affected_capability`, `runtime_route_without_static`,
`static_route_authorized`.  `static_route_authorized_closed` is a derived
completeness relation completing `static_route_authorized`.

Rules, in Datalog shorthand (`IX` is the index):

```text
scip_document_path(IX,P)        :- scip_document(IX,P,_,_,_,_).
scip_definition_site_at(IX,P,L) :- scip_definition_site(IX,P,L,_).
static_edge(IX,S,D)             :- scip_may_reference(IX,S,D,_,_,_,_).
static_route_declared_surface(IX,S) :- route_site(IX,S,_,_,_).
static_route_handler(IX,S,Sym)  :- route_handler_location(IX,S,F,L), scip_definition_site(IX,F,L,Sym),
                                   scip_symbol(IX,Sym,_,"callable",_).
static_root(IX,H)               :- static_route_handler(IX,_,H).
static_reaches(IX,R,D)          :- static_root(IX,R), static_edge(IX,R,D).
static_reaches(IX,R,D)          :- static_reaches(IX,R,M), static_edge(IX,M,D).
static_reaches_eq(IX,R,R)       :- static_root(IX,R).
static_reaches_eq(IX,R,D)       :- static_reaches(IX,R,D).
static_op_owner(IX,Sym,E,V)     :- static_op_site(IX,F,L,V,E), static_site_owner(IX,F,L,Sym).
static_path_to_storage(IX,S,E,V):- static_route_handler(IX,S,H), static_reaches_eq(IX,H,N), static_op_owner(IX,N,E,V).
static_index_current(IX)        :- scip_index_tree(IX,T,_,_), source_tree_observed(T).
scip_index_stale(IX,T,O)        :- scip_index_tree(IX,T,_,_), source_tree_observed(O), T != O.
static_capability_op(IX,S,E,V)  :- static_path_to_storage(IX,S,E,V), static_index_current(IX).
static_capability(IX,S,E,V)     :- static_capability_op(IX,S,E,V), scip_index(IX,_,_,_,_,_).
static_file_unindexed(IX,P)     :- static_source_file(IX,P,_), scip_documents_closed(IX), !scip_document_path(IX,P).
scip_duplicate_definition(IX,Sym,PA,LA,PB,LB) :- scip_definition_site(IX,PA,LA,Sym), scip_definition_site(IX,PB,LB,Sym),
                                   scip_symbol(IX,Sym,_,Cat,_), Cat != "other", PA != PB.
scip_duplicate_definition(IX,Sym,PA,LA,PB,LB) :- scip_definition_site(IX,PA,LA,Sym), scip_definition_site(IX,PB,LB,Sym),
                                   scip_symbol(IX,Sym,_,Cat,_), Cat != "other", LA != LB.
change_reaches(IX,C,U)          :- changed_symbol(IX,C,_), static_edge(IX,U,C).
change_reaches(IX,C,U)          :- change_reaches(IX,C,M), static_edge(IX,U,M).
affected_capability(IX,S,C)     :- static_route_handler(IX,S,C), changed_symbol(IX,C,_), static_index_current(IX).
affected_capability(IX,S,C)     :- static_route_handler(IX,S,H), change_reaches(IX,C,H), static_index_current(IX).
runtime_route_without_static(T,S,R,IX) :- runtime_route_observed(T,S,_,R), index_describes_run(IX,R),
                                   static_route_inventory_closed(IX), !static_route_declared_surface(IX,S).
static_route_authorized(IX,S)   :- static_route_handler(IX,S,H), static_reaches_eq(IX,H,N),
                                   authz_symbol__accepted(IX,N), static_index_current(IX).
static_route_authorized_closed(IX) :- static_reachability_closed(IX), static_route_inventory_closed(IX), static_index_current(IX).
static_route_authorization_gap(IX,S) :- static_route_declared_surface(IX,S), static_route_authorized_closed(IX),
                                   !static_route_authorized(IX,S).
```

Only `static_reaches` and `change_reaches` are recursive; neither SCC
contains negation.  `change_reaches(IX, changed, caller)` is reverse
reachability: the callers a change can affect.  Every negation goes through a
relation that a completeness witness names (`scip_document_path`,
`static_route_declared_surface`, `static_route_authorized`), and the witness
shares the index term.  The IR has no arithmetic, so hop counts are not in the
pack (section 29 keeps them in the go_app differential bundle only).

Reading the two gap relations: `runtime_route_without_static` has negative
polarity, so a derived row is refutation evidence and the claim reads
`refuted` (the static model does not account for the runtime route).
`static_route_authorization_gap` is a positive derived relation: `supported`
means a gap was proven.  Neither can become `supported`/`refuted` by absence
of facts; without the closure witnesses they stay `unresolved` with a visible
missing premise.

## Line frame

Every `line` / `start_line` / `end_line` value in a case is **1-based**, the
frame `claims/static/scip_facts.py` exports (`LINE_FRAME = "1-based"`: SCIP's
0-based range lines are lifted once, in the exporter) and the frame the
tree-sitter side and every human-facing capcov line use.  The joins
`route_handler_location(IX,S,F,L) ⋈ scip_definition_site(IX,F,L,Sym)` and
`static_op_site(IX,F,L,V,E) ⋈ static_site_owner(IX,F,L,Sym)` therefore need
no per-rule shift.  The control case `00-go-app-control` follows the
`tests/fixtures/go_app` source: `GetJob` is defined on `api/jobs.go:10` (SCIP
range line 9 in the golden `tests/fixtures/scip_go_app_index.json`), the route
call is on line 17, `Service.Fetch` on `internal/service/service.go:12`,
`Repo.Get` / `Repo.Write` on `internal/jobs/repo.go` lines 14 and 19 with the
`First` / `Create` sites on lines 15 and 20.  The control case is a
go_app-*shaped* synthetic (abbreviated symbols, an extra `internal/authz`
package for the authorization claims); the exporter's own output over the
golden is evaluated by `tests/claim_semantics/test_differential_static_*.py`
through `tests/claim_semantics/static_rules/go_app.py`.

## Combining the pack with exported facts

The exporter's bundle validates alone, so it declares rule-less stubs for the
relations the frozen primitives point at (`scip_document_path`,
`scip_definition_site_at`, `static_route_declared_surface`, `static_reaches`,
`runtime_route_observed`), byte-identical to the declarations here.
`claims/static/combine.combine(exported, pack_bundle(), ...)` merges the two
declaration sets by name and refuses a duplicate that is not identical, so the
pack stays the single authority on shapes and rules.

## Validator findings

An earlier revision of `validation._validate_context_joins` reported
`mixed-binding-join` for *every* rule that mentioned a static relation without
an `index` column, whether or not the rule read runtime evidence, and the pack
had to route `static_index_current` / `scip_index_stale` through an indexed
bridge `static_source_tree_observed(index, tree_digest)`.  The check is now
scoped to bodies that also contain a runtime atom (regression test in
`tests/claim_semantics/test_validation_section27.py`), the two rules read the
frozen context-free `source_tree_observed(T)` directly as section 29 writes
them, and the bridge is gone: each case carries one claim-time
`source_tree_observed` row with evidence id
`static:claim-time:source_tree_observed:<row12>`, and that id is the leaf the
support sets name.  A rule that joins `source_tree_observed` with runtime
evidence is still rejected.

No other rule needed changes: the `forall` domain, the projections behind
negation, the digest inequality comparison and the mixed static/runtime rule
all validate with the frozen primitives.

## How a reviewer decides a case

Each case file is self-contained: `facts` and `assumptions` are the only
inputs, `claims` name the rows in question, `review_notes` say why the table
holds, and `expected.claims` is the table.  To check a claim:

1. Find the claim relation's rules above and try to instantiate a body with
   the case's facts, chaining derived relations.  Derived rows never appear
   as facts; only primitive rows do (`primitive=true` in the pack).
2. `supported` (or `refuted` for a negative-polarity relation) means one
   ground instantiation exists.  `support_leaves` / `refutation_leaves` is
   the set of fact evidence ids used by that instantiation.  Cases are built
   so that exactly one instantiation exists, so the set is unambiguous.
   Negated atoms and the completeness witnesses that license them contribute
   the witness fact, never the absent row.
3. `unresolved` means no instantiation exists; `missing_premises` names the
   relation whose row is missing (`static_index_current`,
   `static_reachability_closed`, `scip_symbol_node`, ...).  These are declared
   as `missing_premise` output templates on the claim so the engines render
   the same items.
4. `operational_status` is `complete` unless a claim declares a diagnostic
   with another status whose trigger rows exist: `stale` on `scip_index_stale`
   (case 05), `out-of-scope` on `generated_code_in_scope__rejected` (case
   04-rejected).
5. `discrepancies` are `discrepancy` output templates whose `requires_all_evidence`
   rows exist and whose `excludes_evidence` rows do not; a per-claim
   `observation` diagnostic on the trigger relation makes the row relevant to
   the claim.  `forbidden_leaves` names evidence a derivation must not use
   (revoked/rejected assumptions and rows that depend on them, or a lying
   witness).  `observed_leaves` lists every input the reviewer weighed.
6. A `forall` claim quantifies a variable over `static_route_declared_surface`
   within the claim's index; it needs `static_route_inventory_closed(IX)` and
   one supported instance per domain row.  Its support leaves add the domain
   rows' evidence and the closure witness to each instance's leaves.

`seeded_fault` labels a case whose table records what the rules derive from
deliberately wrong input.  In `02-incomplete-indexer-lying-witness` the gap
claim is `supported` from a `static_reachability_closed` witness the exporter
should have withheld; that witness appears in both `support_leaves` and
`forbidden_leaves`, and the test requires such an overlap to carry a
`seeded_fault` label.  `05-stale-index` is labelled `stale-index`.

### Evidence ids and provenance

`scip:<index12>:<relation>:<row12>` for `scip_*` relations,
`static:<index12>:<relation>:<row12>` for tree-sitter, exporter and
assumption rows carrying an index, `runtime:<run>:<relation>:<row12>` for
runtime rows and `static:claim-time:<relation>:<row12>` for the context-free
`source_tree_observed`, where `row12 = sha256(canonical_json([relation,
row]))[:12]`.  The tests recompute every id from its row.  `depends_on`
follows section 29: edges depend on their document, the reference occurrence
(`external:scip-occurrence:<index12>:<occ12>`) and the caller's definition
site; tree-sitter sites depend on `scip_index_tree`; witnesses depend on their
document and the tree.  `Evidence.source` is the producer string.

## Case table

| case | seeded facts | reviewed expectation |
|---|---|---|
| 00 go_app control | full go_app shape, every witness, current tree, changed `Repo#Get`, runtime `POST /jobs` | `static_capability_op` = {(GET /jobs/{id}, jobs, read), (GET /jobs/{id}, audit_logs, create)}; route authorized (exists and forall); affected capability; `runtime_route_without_static` refuted |
| 01 unresolved symbol | edge to `local 3`, `scip_symbol_unrooted`, no `scip_symbol_node` | symbol-level capability supported; node-level `scip_symbol_node` claim unresolved, missing premise `scip_symbol_node`, discrepancy `unrooted-symbol` |
| 02 incomplete indexer | `static_source_file` for `internal/audit/hooks.go` without `scip_document`; `static_reachability_closed` withheld | capability supported; `static_file_unindexed` supported; gap claim unresolved. Variant `-lying-witness`: witness present, gap supported, labelled seeded fault |
| 03 dynamic dispatch | `static_blind_spot(interface_dispatch)` on the path; references/reachability withheld | capability supported with `blind-spot-on-path`; gap unresolved |
| 04 generated code | handler defined in `api/jobs_gen.go` (`scip_generated_site`) | forall supported with `handler-in-generated-code`; `-accepted` clears the discrepancy; `-rejected` forbids the definition site: unresolved, `out-of-scope` |
| 05 stale index | `scip_index_tree(IX,T1)`, `source_tree_observed(T2)` | `static_path_to_storage` supported; `static_capability` unresolved, `stale`, missing premise `static_index_current` |
| 06 synthesized enclosing | scip-php shape, `enclosing_synthesized=true`, edges `caller_synthesized=true`, no witnesses | capability supported with `caller-attribution-synthesized`; authorization gap and runtime gap unresolved |
| 07 constructor as type reference | `scip_type_reference(Repo#Get, models/Job#)`, no edge | capability supported through the op owner; `static_reaches(GetJob, Job#)` unresolved |
| 08 module-scope reference | `handle(...)` and `Repo#Seed` referenced at package scope | route -> handler supported; `static_op_owner(Seed)` supported; "init reaches storage" unresolved |
| 09 duplicate definitions | two `scip_definition_site` rows for `Handler#GetJob` | capability supported with `ambiguous-definition`; `scip_duplicate_definition` supported; gap unresolved |
| 09 package-symbol variant (`09-duplicate-definitions-package-symbol`) | `Handler#GetJob` defined once; the package symbol `api/` (category `other`) defined in `api/jobs.go:1` and `api/jobs_legacy.go:1`; every `scip_references_closed` witness and `static_reachability_closed` present | `scip_duplicate_definition` empty (claim unresolved, missing premise `scip_symbol`); capability supported with no discrepancy; gap supported, i.e. the witnesses are not withheld for a package symbol |
