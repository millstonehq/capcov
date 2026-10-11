---
id: ADR-0001
type: adr
title: capcov's literature foundations
status: accepted
owner: cpb
created: '2026-09-13T00:00:00.000Z'
updated: '2026-09-15T00:00:00.000Z'
tags:
  - adr
  - capabilities
  - testing
summary: Anchors each capcov mechanism in named testing and program-analysis literature and adopts Software Reflexion Models as the explicit frame for static-vs-runtime reconciliation.
---

## Context

Capcov combines established program-analysis and testing techniques. Its artifact
schemas, reconciliation categories and progress presentation are an engineering
synthesis, not a new formal verification method. This document distinguishes
implemented mechanisms, academic lineage and the limits of the correspondence.

## Decision

**A. Flows use model-based testing and fact-state planning.**
`flows/model.py` represents facts, guarded transitions and add/remove effects.
Its breadth-first planner emits shortest-prerequisite scenarios for reachable
transitions within a state budget. This draws on model-based testing (Utting &
Legeard 2007), FSM testing (Chow 1978; Lee & Yannakakis 1996) and STRIPS-style
operators (Fikes & Nilsson 1971). It is not an implementation of Chow's W-method
and does not inherit its conformance guarantees. Transition coverage is scoped to
the authored model, reachable states, bindings and observed execution. A passing
scenario does not establish all traces or the completeness of the authored model.

**B. Obligations specialize test requirements.**
A coverage criterion defines requirements to exercise (Ammann & Offutt). Capcov
uses explicit identities for behavioral outcomes and source findings, retaining
missing and unresolved material. Its use of one umbrella term, `obligation`, for
these different findings is a local schema choice. Structural discovery and
behavioral acceptance have distinct denominators; finding or touching a surface
does not demonstrate its expected effects. The particular completeness vector is
capcov's reporting design, not a universal or academically validated measure of
whole-system correctness. No numerical confidence follows from an "80/20" policy.

**C. Reachability closes over the supplied call graph.**
`core/fixpoint.py` propagates entity references backward along resolved calls.
Convergence means no further changes over those edges, not that every real edge
was discovered. The practical resolver documents unsupported/dynamic boundaries;
this follows the soundiness discipline (Livshits et al. 2015), without establishing
that the implementation has a proven sound core or that every blind spot is known.
Shivers 1991 and Samhi et al. 2024 motivate care around control flow and empirical
call-graph accuracy; their algorithms or measurements do not transfer to capcov.
SCIP integration is implemented in `scip/`; indexer-provided resolution and local
blind-spot findings remain separate evidence with explicit limits. References to
name-resolution research describe lineage, not an accuracy certification.

**D. Reconciliation adapts Software Reflexion Models.**
Murphy, Notkin & Sullivan 1995 compare high-level architectural relationships with
relationships extracted from source. Their convergence/divergence/absence framing
motivates capcov's comparisons, but the correspondence is not exact: capcov also
compares static and runtime evidence at entity and surface granularity.

- `both`: static and runtime accounts overlap at the recorded granularity. This
  is structural agreement, not behavioral correctness or matching route evidence.
- `static_only`: static support without observation in this execution scope.
- `runtime_only`: observation without corresponding static support; discovery
  may be incomplete or behavior dynamic.
- `neither`: a declaration without support in either account. It is not proof of
  dead code, deletion, or absence from all possible executions.

Outcome demonstration requires its own assertions and provenance. A coverage gate
applies a stated policy to scoped results; it does not transform structural
agreement into a behavioral proof. Exemptions preserve their reasons and scope.

**E. Features implement product variability.**
`features/model.py` implements a feature tree with mandatory/optional children,
alternative/or groups and requires/excludes constraints, grounded in FODA (Kang
et al. 1990) and feature-modeling practice. Feature selection describes a product
configuration, not implementation scheduling. Treating a capability as a feature
is capcov's modeling choice. A correct tree does not establish discovery breadth
or behavioral acceptance. Outcome `capability` references connect acceptance
requirements to this existing identity; no additional work-unit entity is needed.

**F. Source-to-feature-to-outcome-to-evidence links are traceability.**
Their exact schema, context matching, ownership rules and set-based rollups are
capcov engineering choices. Preserve source findings separately from expected
behavior, derive acceptance from identified evidence, and expose unmapped material.
The evidence adapter establishes execution results; a model's assertion cannot
validate its own business semantics. Hashes bind artifacts, not the truth of an
external observation. Context declarations must be grounded by their runner.

**G. Testing techniques and workflow policy are distinct.**
Mutation testing and differential testing are established techniques. A mutation
declaration is not execution of a fault. A reference comparison establishes
agreement for the exercised cases, not universal equivalence. "Quasi-formality"
and breadth-first burn-down are local workflow descriptions; worker assignments,
effort and forecasts are planning metadata. Sutton's Bitter Lesson is an essay
informing a build-vs-reuse judgment, not a proof that a particular resolver is the
right choice. Datalog, truth maintenance and learned state machines are not
implied implementations merely because they appear in research discussions.

## Consequences

Use established terms when they fit and explicitly label capcov-specific schemas
and adaptations. Do not cite an algorithm to imply guarantees the implementation
does not meet. Feature configuration, discovery accounting, behavioral evidence,
implementation declarations and release policy remain distinct dimensions.

This correction supersedes the earlier exact Reflexion Model correspondence,
"dead" inference, and statements that feature models and SCIP were unimplemented.
It does not rename the four structural cells or silently change their gate policy.

## References

**A. Model-based testing / FSM transition coverage**

- T. S. Chow, "Testing Software Design Modeled by Finite-State Machines," *IEEE
  Transactions on Software Engineering* SE-4(3):178–187, 1978.
  https://doi.org/10.1109/TSE.1978.231496
- Mark Utting & Bruno Legeard, *Practical Model-Based Testing: A Tools Approach*,
  Morgan Kaufmann, 2007.
- D. Lee & M. Yannakakis, "Principles and Methods of Testing Finite State Machines
  — A Survey," *Proceedings of the IEEE* 84(8):1090–1123, 1996.
  https://doi.org/10.1109/5.533956

**B. Coverage criteria as test requirements**

- Paul Ammann & Jeff Offutt, *Introduction to Software Testing*, Cambridge
  University Press, 1st ed. 2008 / 2nd ed. 2016.

**C. Name resolution, call-graph reachability, and soundiness**

- Pierre Néron, Andrew Tolmach, Eelco Visser & Guido Wachsmuth, "A Theory of Name
  Resolution," *ESOP 2015*, LNCS 9032, pp. 205–231.
  https://doi.org/10.1007/978-3-662-46669-8_9
- Douglas A. Creager & Hendrik van Antwerpen, "Stack graphs: Name resolution at
  scale," arXiv:2211.01224 (2022); *Eelco Visser Commemorative Symposium (EVCS
  2023)*, OASIcs vol. 109, art. 8. https://arxiv.org/abs/2211.01224 — reports
  scale and incrementality; **no precision/recall figure**.
- Ben Livshits, Manu Sridharan, Yannis Smaragdakis, et al., "In Defense of
  Soundiness: A Manifesto," *Communications of the ACM* 58(2):44–46, 2015.
  https://doi.org/10.1145/2644805
- Olin Shivers, "Control-Flow Analysis of Higher-Order Languages" (k-CFA), PhD
  thesis, Carnegie Mellon University, 1991.
- Jordan Samhi, René Just, Tegawendé F. Bissyandé, Michael D. Ernst & Jacques
  Klein, "Call Graph Soundness in Android Static Analysis," *ISSTA 2024*.
  https://arxiv.org/abs/2407.07804

**D. Model-vs-reality reconciliation**

- Gail C. Murphy, David Notkin & Kevin Sullivan, "Software Reflexion Models:
  Bridging the Gap between Source and High-Level Models," *ACM SIGSOFT FSE 1995*.
  https://doi.org/10.1145/222124.222136

**E. Capability decomposition**

- Kyo C. Kang, Sholom G. Cohen, James A. Hess, William E. Novak & A. Spencer
  Peterson, "Feature-Oriented Domain Analysis (FODA) Feasibility Study," Technical
  Report CMU/SEI-90-TR-21, Software Engineering Institute, 1990.

**F. Build-vs-rent (essay, not peer-reviewed)**

- Richard Sutton, "The Bitter Lesson," 2019.
  http://www.incompleteideas.net/IncIdeas/BitterLesson.html

- Richard E. Fikes & Nils J. Nilsson, "STRIPS: A New Approach to the Application of Theorem Proving to Problem Solving," *Artificial Intelligence* 2(3–4):189–208, 1971. https://doi.org/10.1016/0004-3702(71)90010-5
