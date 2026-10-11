# Foundations

Each mechanism has a lineage in testing and program-analysis literature.
[ADR-0001](https://github.com/millstonehq/capcov/blob/main/docs/adr/ADR-0001-capcov-literature-foundations.md)
records the full map and the resolver decision. The anchors in brief:

- **Flow model = model-based testing.** Facts + guarded transitions form an
  EFSM/STRIPS model; `plan()` emits one shortest-prerequisite scenario per
  transition ("all-transitions" generation). Sources: Chow 1978; Utting & Legeard 2007;
  Lee & Yannakakis 1996.
- **Obligations = test requirements.** The obligation set is the denominator, and
  each branch outcome is a separate obligation, never collapsed. Sources: Ammann & Offutt
  (2008/2016).
- **Reachability + soundy resolution.** The backward-reachability fixpoint closes
  over *resolved* edges; the adapter resolves what it can syntactically and
  *enumerates* its blind spots rather than guessing. Sources: Livshits et al. 2015
  (soundiness); Shivers 1991 (k-CFA); Samhi et al. 2024; Néron et al. 2015;
  Creager & van Antwerpen 2023 (stack graphs: scale, no precision/recall figure).
- **Static-vs-runtime reconciliation = a Software Reflexion Model.** Declared vs
  observed maps convergence/divergence/absence onto `covered` / `runtime-only` /
  `dead`. Sources: Murphy, Notkin & Sullivan 1995.
- **Capability nouns descend from FODA.** Capabilities as a facet tree; adopting
  its variability structure is a future option. Sources: Kang et al. 1990.
