# Feature models (FODA)

A **feature model** records a system's capabilities as a *tree of features* plus
the rules for which combinations of them are legal. A **feature** is a
user-recognisable capability (a noun a user or stakeholder would name); the tree
says how features decompose; **cross-tree constraints** capture the dependencies
the tree cannot. A **configuration** is a chosen subset of features — one concrete
product built from the model — and the model's whole job is to decide which
configurations are *valid*.

This is capcov's variability structure for the "capabilities and their facets"
idea. The lineage is Feature-Oriented Domain Analysis: **Kang et al. 1990**
(*Feature-Oriented Domain Analysis (FODA) Feasibility Study*, Technical Report
CMU/SEI-90-TR-21, Software Engineering Institute). The full citation and capcov's
other literature anchors live in `ADR-0001-capcov-literature-foundations`, in the
[Synapse](https://github.com/millstonehq/synapse) repository under
`content/90_Architecture/ADRs/`; ADR-0001 §E is the FODA anchor.

The canonical worked example is
[`examples/feature-model-authentication.json`](../../../examples/feature-model-authentication.json)
(repo path `examples/feature-model-authentication.json`),
regenerated from `capcov features example` so it always matches this code.

## Shape

`features/model.py::validate(model)` accepts one JSON object and raises on any
violation. Top level:

| key | type | meaning |
| --- | --- | --- |
| `version` | int | `1`. |
| `root` | string | id of the single root feature. |
| `features` | array | the feature records, below. |
| `constraints` | array | cross-tree constraints, below (may be empty). |

Each **feature record**:

| key | type | meaning |
| --- | --- | --- |
| `id` | string | unique, stable identifier — the coverage key. |
| `name` | string | the human-readable capability name. |
| `parent` | string \| null | parent `id`; `null` for the root only. |
| `decomposition` | `"mandatory"` \| `"optional"` \| null | how a *solitary* child relates to its parent. |
| `group` | `"alternative"` \| `"or"` \| null | the kind of group this feature's **children** form. |

The `group` field lives on the **parent** of a group — the feature whose children
*are* the group's members. So a group member carries `decomposition: null` and
`group: null`; the member's relationship to its parent is expressed once, by the
parent's `group` kind, not repeated on each child. A single feature may be **both**
a solitary child (via `decomposition`, describing how it hangs off *its* parent)
**and** a group's parent (via `group`, describing how *its* children combine) —
`secondFactor` in the canonical model is exactly this: an `optional` child of
Password that also declares an `alternative` group over its own children.

So each non-root feature is:

- a **solitary child** iff its parent declares no group — then it *must* carry a
  `decomposition`; or
- a **group member** iff its parent declares a `group` — then it *must not* carry a
  `decomposition`.

The root carries `parent`, `decomposition`, and `group` all `null` — it is in
every configuration by definition, so it needs no relationship to a parent.

Each **constraint record**: `{ "type": "requires" | "excludes", "a": id, "b": id }`
where `a` and `b` are distinct feature ids.

`validate` enforces: `version == 1`; a single declared root; unique, non-empty ids
and a name on every feature; every `parent` resolves and the parent chains reach
the root without cycles (it is a tree); the solitary-carries-decomposition /
member-carries-none rule above; every feature that declares a `group` has at least
one child; every constraint references two distinct, declared features; and no pair
of features is declared both `requires` and `excludes`. Structural validity is not
configuration validity — see below.

## The four facets

The facets are the four ways a child can depend on its parent. The first two are
**solitary** (a single child, judged on its own); the last two are **group**
cardinalities the parent declares over *all* its children (a set of siblings,
judged together).

- **mandatory** — a solitary child, present in a configuration *iff* its parent is.
  `{"decomposition": "mandatory", "group": null}`. Example: **Password** under
  Authentication.
- **optional** — a solitary child, *may* be present when its parent is; never
  required. `{"decomposition": "optional", "group": null}`. Example: **MFA** under
  Authentication, and **SecondFactor** under Password.
- **alternative** — declared on a parent; when that parent is present, **exactly
  one** of its children is chosen (xor). `secondFactor` carries
  `{"decomposition": "optional", "group": "alternative"}`, and its children
  **SMS | AuthenticatorApp | Passkey** are the members.
- **or** — declared on a parent; when that parent is present, **one or more** of its
  children are chosen. *(Not exercised by the canonical model.)* Illustration — an
  `export` capability that must offer at least one format:

  ```json
  { "id": "export", "name": "Export", "parent": "editor",
    "decomposition": "mandatory", "group": "or" }
  ```

  with `pdf` and `html` as its children — the `or` group's members.

A group's cardinality is checked **only when its parent feature is selected**.
Because SecondFactor is *optional*, a configuration may leave it out entirely
(Password with no second factor); when it *is* selected, its alternative group
forces exactly one of the three factors.

## The two constraint types

Constraints are *cross-tree*: they express dependencies between features that the
parent/child tree cannot, typically across different subtrees.

- **requires** — `a` implies `b`: if `a` is selected, `b` must be too (one-way).
  Illustration (not in the canonical model): SMS one-time codes depend on a
  delivery capability —

  ```json
  { "type": "requires", "a": "sms", "b": "sms_gateway" }
  ```

- **excludes** — `a` and `b` are mutually exclusive: no configuration contains
  both. The canonical model declares `{"type": "excludes", "a": "passkey",
  "b": "sms"}`.

Honest note on the canonical `excludes`: Passkey and SMS are members of the *same*
alternative group, so the "exactly one" cardinality already forbids selecting both
— the constraint removes no configuration the group did not already remove. It is
kept because the task specifies it and because it shows the `excludes` form on
recognisable features. A cross-tree `excludes` **earns its keep** when the two
features live in different subtrees, where no group cardinality relates them (e.g.
excluding a `passkey` second factor against an `sms_recovery` feature hanging off
MFA).

## When is a configuration valid?

A configuration — a subset of feature ids — is **valid** iff all hold
(`is_valid_configuration(model, selected)` returns `(ok, reasons)`):

1. The root is selected.
2. Every selected feature's parent is selected (the selection is a connected
   subtree from the root).
3. Every **mandatory** solitary feature whose parent is selected is itself
   selected.
4. For every parent that declares an **alternative** group and is selected:
   **exactly one** child is selected. For every selected parent declaring an **or**
   group: **at least one** child is selected.
5. Every **requires** holds (if `a` selected then `b` selected) and every
   **excludes** holds (`a` and `b` not both selected).

Optional features — and *whether* the optional SecondFactor is taken at all — are
the free choices, alongside which member of each active group is picked.

The canonical model has **eight** valid configurations. Password is mandatory, so
it and Authentication are in all of them; MFA is a free on/off; and SecondFactor is
optional — omit it, or take it with exactly one factor:

| # | second factor | MFA |
| - | ------------- | --- |
| 1 | (none)           | off |
| 2 | (none)           | on  |
| 3 | SMS              | off |
| 4 | AuthenticatorApp | off |
| 5 | Passkey          | off |
| 6 | SMS              | on  |
| 7 | AuthenticatorApp | on  |
| 8 | Passkey          | on  |

## How coverage rolls up

The feature model gives capcov's coverage a *shape*. Each feature is a capability
that can carry evidence obligations (per ADR-0001 §B, the obligation set is the
denominator, and each distinct outcome is its own obligation). Coverage of a
*parent* aggregates its children through the same facet rules that decide validity:

- a **mandatory** child must be covered for its parent to be covered;
- an **optional** child contributes only when the configuration selects it;
- an **alternative**/**or** group contributes only its **selected** member(s) — the
  branches not taken are neither covered nor owed;
- a **requires**/**excludes** violation is a coverage gap, not a silent pass.

Two roll-ups read this shape, and both refuse to collapse to one percentage
(ADR-0001 §D, the Software Reflexion Model frame — a feature can be *declared*,
*selected*, and still *unproven*, three different denominators):

- `model.py::coverage_rollup(model, selected, covered)` — the **binary** view: is
  every selected feature proven, all the way up? A single uncovered leaf propagates
  a gap to the root.
- `coverage.py::rollup(model, feature_obligations, selected)` — the **numeric
  completeness vector** `{mandatory_covered, mandatory_total, optional_covered,
  optional_total, optional_assessed, optional_unassessed, tree_rows}`. A mandatory
  gap lowers the score; an *unselected* optional is excluded from the denominator
  rather than counted against it; and with `selected=None` the mandatory skeleton
  is assessed while every optional and group choice is reported as
  *optional-unassessed* — never silently covered, never silently failed.

## CLI

```sh
capcov features example                                   # emit the canonical model
capcov features validate examples/feature-model-authentication.json
capcov features check    <model.json> --select auth,password,secondFactor,passkey
capcov features rollup   <model.json> --select <ids> --covered <ids>
capcov features coverage <model.json> examples/feature-obligations-authentication.json \
                         --selected auth,password,secondFactor,passkey,mfa
```

`validate` checks structure, then prints the annotated feature tree; `check`
applies the five configuration rules above to a chosen subset; `rollup` is the
binary view — whether a configuration's coverage rolls up whole. `coverage` reads a
`feature-obligations.json` (a map `id -> {covered, total}`) and prints the numeric
completeness vector from `coverage.py::rollup`; omit `--selected` to assess the
mandatory skeleton alone, leaving every optional and group choice *unassessed*.

## Deriving the obligations map: `capcov features map`

`features coverage` consumes `{feature_id: {covered, total}}`. `features map`
derives it from discovery instead of a hand-written file:

    capcov features map model.json mapping.json capabilities.json \
        [--coverage coverage.json] --out obligations.json [--report report.json]

`mapping.json` says which surfaces each feature claims, by id glob, by tag, or by
the file that declares the surface:

    {"version": 1, "features": {
       "contacts": {"surfaces": ["http:* /contacts", "http:* /contacts/*"]},
       "billing":  {"tags": ["Billing"]},
       "invoicing": {"files": ["app/Http/Controllers/Invoicing/*.php"]}}}

A rule needs at least one of `surfaces`, `tags`, `files`; any other key is
refused. `files` globs match against the surface's declaring file with
`fnmatch.fnmatchcase`, the same as `surfaces` matches the id -- a `*` crosses
`/` freely (it is a shell-style glob, not a path-segment matcher), and matching
is case-sensitive on every platform, including a case-insensitive filesystem.

`total` is the number of discovered surfaces a feature claims. `covered` is how
many of those a reconciliation saw reached at runtime, and `exercised` (in the
CLI summary and the `--report` file) is how many *inventory* surfaces that
covers; **without `--coverage` every `covered` is zero, `exercised` is zero, and
the report says `assurance: static-only`** -- a declared route is not an
exercised one. Passing a `--coverage` file with no `rows` list (a
capabilities.json where a reconcile output belongs, say) is refused rather than
silently read as zero rows.

The report also lists: `unassigned` surfaces (no feature claims them: assign or
name why not); `contested` surfaces (several features claim them: resolve the
overlap); `unmapped_features` (declared in the model but given no rule at all);
`empty_rules` (a feature's rule matched no surface -- a typo'd glob reads as
zero, not silence); and forwards discovery's `excluded_surfaces` and
`unresolved` counts so the denominator is never read narrower than it is.
### Evidence-derived acceptance

An outcome map's existing `capability` field may name a feature id:

```sh
capcov features reconcile model.json --outcomes-map capcov.outcomes.json \
  --inventory capabilities.json --run outcome-run.json --target . \
  --selected root,feature --out feature-evidence.json
```

The report carries exact `own_outcome_ids` and `demonstrated_outcome_ids`, an
acceptance status for every rolled subtree, and current model, map, inventory,
engine, and input fingerprints. `behavioral_complete` and
`discovery_accounted` remain separate; unknown or unresolved discovery prevents
the combined verdict from becoming green. A selected effective leaf with no
owned outcomes is an explicit gap.

A test-free required outcome can declare exact browser evidence as
`"flow_bindings": [{"transition": "save", "assertion": "receipt"}]`. Every
binding must be attested. `capcov flows run` accepts `--outcomes-map`,
`--capability-inventory`, and `--evidence-target`, fingerprints those inputs
before and after the runner, and embeds the fingerprint. Feature reconciliation
then consumes the raw flow model, plan, inventory, and run together. A unified
capability inventory can serve directly as the flow inventory; its current
source-tree digest is checked without a parallel discovery configuration.
