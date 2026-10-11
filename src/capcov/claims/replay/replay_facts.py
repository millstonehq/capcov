"""Export a replay receipt directory as claim facts + evidence (Phase 4, judge side).

The replay harness is a *producer of runtime observations*, never an oracle.
This module turns one receipt directory -- written by the Go replay driver
after replaying a recorded request set against the PHP system, the Go system
and the Shen model, under one nonce and one snapshot -- into a validated claims
``Bundle`` whose every fact carries the receipt's ``run`` context (or the
``model`` context for model-scoped relations), whose every fact has an
``Evidence`` record with a content-derived id, an explicit ``depends_on`` chain
and a ``source`` naming the *producer class* the row is attributed to, and
whose completeness witnesses are emitted only when the receipt says the
corresponding table is closed.  The bundle's ``digest()`` is a pure function of
the receipt's content: permuting the rows of any file changes nothing.

It mirrors ``capcov.claims.static.scip_facts`` deliberately -- same
``ExportResult``, same ``_Facts`` accumulator, same identity/receipt
separation -- so a reader of one can read the other.

RECEIPT DIRECTORY CONTRACT
--------------------------
This is the contract the Go replay driver writes.  ``receipt.json``::

    {"version": 1,
     "run": "<run id>",                      # symbol; the bundle's run context
     "nonce": "<sha256>", "snapshot": "<sha256>", "model": "<sha256>",
     "php_commit": "<git sha>", "go_commit": "<git sha>",
     "closed": {"replay_requests": true, "php_effects": true, "go_effects": true,
                "php_post_states": true, "go_post_states": true,
                "model_admissible": true, "mutant_kills": true,
                "php_effect_seqs": true, "go_effect_seqs": true, "model_effect_seqs": true,
                "replay_request_seqs": true, "php_responses": true, "go_responses": true,
                "replay_stability": true},
     "model_writes_closed": [{"model": "<sha256>", "op": "<op>"}, ...],
     "mutants_closed":      [{"model": "<sha256>", "op": "<op>"}, ...],
     "receipts": {...}}                      # wall clock, paths, container ids

plus one JSON file per observation relation, named ``<relation>.json``::

    {"rows": [{"<column>": <value>, ...}, ...],
     "producer": "<class> <detail>"}         # optional, see EVIDENCE

for each of ``replay_request``, ``php_post_state``, ``go_post_state``,
``php_effect``, ``go_effect``, ``model_effect``, ``model_admissible``,
``model_writes``, ``mutant``, ``mutant_killed`` and -- the ordering,
cross-request and cross-run relations -- ``php_effect_seq``,
``go_effect_seq``, ``model_effect_seq``, ``replay_request_seq``,
``php_response``, ``go_response``, ``replay_stability`` and
``model_well_formed`` (``OBSERVATION_FILES``).  Row objects carry the
relation's columns by name; a
``run`` column may be omitted (it is the receipt's ``run``) and a ``model``
column may be omitted (it is the receipt's ``model``).  Values are typed by
the schema: ``symbol`` and ``digest`` columns are JSON strings, ``unsigned``
columns (``seq``, ``status``) are bare non-negative JSON integers -- a quoted
``"3"`` is ``invalid-input`` naming the column.  A row naming *another* run is
a leftover of a different replay and makes the export ``stale``; a row naming
another model, an unknown column, a missing column or a value of the wrong
type is ``invalid-input``.  A missing ``<relation>.json`` means zero rows --
and the corresponding ``*_closed`` witness is still emitted only if ``closed``
says so, because "no rows" and "no rows exist" are different statements.
Every key of ``closed`` defaults to ``false``; ``model_writes_closed`` /
``mutants_closed`` default to empty.  ``replay_run`` has no file: its one row
is the receipt header.

ORDERING, CROSS-REQUEST AND CROSS-RUN OBSERVATIONS.  ``php_effect_seq`` /
``go_effect_seq(run, req, seq, table, kind, pk)`` are the *per-statement*
effect sequences of a request in capture order (``seq`` 1-based); only SQL
tables captured with a timeline appear -- store tables (``redis``,
``mongo:*``) and the model host's sidecar pseudo-tables are snapshot diffs
without an order and never appear.  ``pk`` is the ``pk`` of the same event's
``php_effect`` / ``go_effect`` row.  ``model_effect_seq(run, model, req, seq,
table, kind, pk)`` is the model's declared effect order, lifted exactly like
``model_effect``.  ``replay_request_seq(run, req, seq, target)`` is the tape
order (``target`` = HTTP method and raw path); ``php_response`` /
``go_response(run, req, status)`` the HTTP status each system returned.
``replay_stability(run, run_a, run_b, side, stable)`` binds the receipt's run
to a selftest of the same oracle (two fresh runs ``run_a`` / ``run_b`` of the
same tape): ``stable`` is ``"true"`` iff every request agreed on status and
net SQL effects; the harness emits the row only when the selftest's
provenance matches the receipt's, so a row here *is* the binding.  The
closures are ``closed.php_effect_seqs`` / ``go_effect_seqs`` /
``model_effect_seqs`` / ``replay_request_seqs`` / ``php_responses`` /
``go_responses`` / ``replay_stability`` (witnesses ``<relation>s_closed`` --
``model_effect_seqs_closed(run, model)`` carries the model like
``model_admissible_closed``; ``replay_stability_closed(run)`` completes
``replay_stability``).

MODEL WELL-FORMEDNESS.  ``model_describes_run`` binds a run to a model; that
the model *typechecks* is a separate, positive premise of qualification, and
the model host cannot vouch for it.  The optional ``model_well_formed.json``
(``OBSERVATION_FILES``)::

    {"producer": "modelcheck <checker> <version> model:<digest12>",
     "rows": [{"model": "<sha256>", "checker": "<name>",
               "checker_version": "<version>", "checker_binary": "<sha256>",
               "certificate": "<semantic sha256>"}, ...]}

The separate ``model_operation_checked.json`` emits
``model_operation_checked(model, operation, checker, checker_version,
checker_binary, certificate)`` per passing operation. These rows are under
the producer class ``modelcheck`` (evidence-id prefix
``modelcheck``).  The class is the point: ``shen`` -- the model host -- is not
admitted for this relation, because a host cannot certify its own model's
well-formedness, and neither is ``reviewer``; a file that claims either is
refused at ingestion (``evidence-producer``) like any other producer lie.  A
row naming a model other than the receipt's certifies *another artifact* and
makes the export ``stale`` with its own message (``STALE_ON_FOREIGN_MODEL``)
rather than ``invalid-input``: the receipt is well formed, the certificate is
simply not this model's.  There is no closure relation -- nothing negates
well-formedness -- so a missing file is simply no certificate, and the judge
withholds qualification rather than inferring one.

Which exact checker result may be believed is the *reviewer's* word, not the
receipt's.  ``export_bundle(..., reviewer_admissions=[...])`` accepts external
entries of the form::

    {"producer": "reviewer <name>", "model": "<sha256>",
     "operation": "<op>", "checker": "<name>",
     "checker_version": "<version>", "checker_binary": "<sha256>",
     "certificate": "<semantic sha256>"}

An entry exports ``model_checker_admitted`` only when all six identity fields
match one unambiguous ``model_operation_checked`` row in this receipt. Its
evidence depends on that certificate row. Receipt-local admission remains
ignored.
The legacy receipt-local ``model_checkers.json`` (``CHECKERS_FILE``) is never
read as authority; its presence is reported and ignored.  With no external
admissions the relation remains empty, so qualification remains pending.

THE LEARN RECEIPT.  A *learn campaign* is a separate producer chain -- a tape
generator, the PHP oracle and the model host -- that replays generated tapes
against the oracle and against the model and reports where the model predicted
something the oracle did not do, and which ops it does not model at all.  It is
optional: a replay receipt with no ``learn/`` subdirectory carries no learn row,
and qualification is exactly what it was.  When one is present it lives in
``learn/`` beside ``receipt.json`` and its header is ``learn/learn_receipt.json``
(``LEARN_DIR`` / ``LEARN_RECEIPT_FILE``)::

    {"version": 1,
     "learn": "<sha256>",                 # the campaign's digest (plan + tape set)
     "model": "<sha256>",                 # optional; defaults to the receipt's model
     "closed": {"learn_predictions": true, "learn_observations": true,
                "learn_unmodeled": true},
     "receipts": {...}}                   # optional; wall clock, counts, paths

plus one ``{"rows": [...], "producer": "..."}`` file per relation, read exactly
like the top-level observation files (``LEARN_FILES``)::

    learn/learn_run.json          learn_run(run, campaign, model, learn, php_commit, snapshot)
    learn/learn_prediction.json   learn_prediction(run, model, learn, tape, req, op, predicted, state_digest)
    learn/learn_observation.json  learn_observation(run, tape, req, op, observed, state_digest)
    learn/learn_unmodeled.json    learn_unmodeled(model, learn, op)

``run`` is **this receipt's run** and may be omitted (a row naming another run
is ``stale``, as everywhere else); the campaign's *own* run id is the separate
``campaign`` column of ``learn_run``, which is not a context and is only
recorded.  ``model`` and ``learn`` may likewise be omitted and default to the
receipt's model and the learn header's digest; a row naming a different one is
``invalid-input``, and a header naming a different *model* is ``stale`` (the
campaign was run against another artifact and says nothing about this run).
``req`` is the tape-qualified request key (``<tape>/<request id>``, since a
request id repeats across tapes) and is the "step" the judge names in a
counterexample.  ``predicted`` / ``observed`` are the outcome class the model
and the oracle assign to that request (``ok``, ``forbidden``, ``missing``, ...);
``state_digest`` is recorded beside each, not compared -- the class is what the
campaign compares, and a digest comparison belongs to the post-state relations.
The reserved class ``"unknown"`` means *the model made no prediction here* and is
never a counterexample.  A prediction is a **set**: the model may admit several
post-states for one position, so ``learn_prediction`` is keyed by
``(run, model, learn, tape, req, state_digest)`` and what may not differ between
two rows of one position is the class.  One observation per ``(run, tape, req)``.
Producer classes: ``replay`` owns ``learn_run`` and ``learn_observations_closed``
(the harness ran the tapes), ``php`` owns ``learn_observation`` (the oracle
answered), ``shen`` owns ``learn_prediction``, ``learn_unmodeled``,
``learn_predictions_closed``, ``learn_unmodeled_closed`` and the compatibility
row ``learn_describes_model(learn, model)``, which the exporter emits from the
header and which binds the campaign to the model the judge is using.  Each learn
row depends on ``external:learn:<digest>`` as well as on the run row.

The judge reads three things from this.  ``learn_counterexample(run, tape, req,
op, predicted, observed)`` is a tape position where the two disagree;
``learn_consistent(run, op)`` is the claim that, under both closures, there is
none for that op.  ``learn_unmodeled_any(run, op)`` **downgrades**
``op_qualified_rt``: an op the campaign reported as unmodelled, under its closed
unmodelled list, does not qualify.  The downgrade can only take qualification
away -- absence of a learn receipt is not evidence that the model covers
everything, and a receipt without one qualifies as before.

REVIEWER SCOPE EXCLUSIONS.  Infrastructure tables the systems write around an
op (a session touch, job bookkeeping, a cache, an outbox) leave the
``undeclared_write`` judgement only through explicit, reviewer-owned facts,
never silently.  The optional ``model_scope_exclusions.json``
(``EXCLUSIONS_FILE``)::

    {"producer": "reviewer <name>",          # optional; first token must be reviewer
     "reviewer": "<name>", "reviewed_at": "<date>",
     "reviewed_against": {"model": "<sha256>", "run": "<run id>"},
     "rows": [{"model": "<sha256>", "table": "<table>", "reason": "<why>"}, ...]}

exports one ``model_scope_exclusion(model, table, reason)`` row per entry as
*assumption*-kind evidence whose source is ``"<producer or reviewer <name>>
<reviewed_at> model:<digest12> run:<run>"`` (a ``producer`` that already ends
with that suffix is carried verbatim), so a certificate's assumption leaf
points at who reviewed what.  ``reviewed_against.model`` must equal the
receipt's model (a mismatch is a *stale review* and ``invalid-input``);
``reviewed_against.run`` is recorded, not enforced, because an exclusion is
model-scoped.  ``closed.model_scope_exclusions`` (true iff the file is
authoritative for that model) emits ``model_scope_exclusions_closed(model)``;
without it the judge has no closed exclusion set and qualifies nothing.

One observation per write and per post-state (``UNIQUE_KEYS``): two
``php_effect`` / ``go_effect`` / ``model_effect`` rows sharing
``(run, [model,] req, table, kind, pk)`` with different ``cols_digest``, or
two ``php_post_state`` / ``go_post_state`` rows for one ``(run, req)`` with
different ``state_digest``, are contradictory reports of the same event and
make the export ``invalid-input`` naming the key -- the judge never carries
both as facts and lets a rule pick.  Likewise one event per sequence position
(``php_effect_seq`` / ``go_effect_seq`` keyed ``(run, req, seq)``,
``model_effect_seq`` keyed ``(run, model, req, seq)``), one tape position and
one status per request (``replay_request_seq``, ``php_response``,
``go_response`` keyed ``(run, req)``) and one verdict per selftest side
(``replay_stability`` keyed ``(run, run_a, run_b, side)``).  A row repeated
verbatim is one fact.  ``model_admissible`` is a set of admissible states per
request and is not constrained.

``closed.php_post_states`` / ``closed.go_post_states`` (witnesses
``php_post_states_closed`` / ``go_post_states_closed``) say that every replayed
request's post-state on that side was reported.  Without them a request whose
post-state the runner omitted is invisible to the disagreement check while
the disagreement closure still holds, so the judge treats an unwitnessed
post-state table as open: no ``*_disagreement_closed`` row, no
qualification.

IDENTITY
--------
The bundle's identity (metadata ``replay_digest``, kind
``REPLAY_IDENTITY = "replay-relations-v1"``) is a content digest of the
normalized relations the exporter emits, never of the files' bytes::

    replay_digest = sha256("replay-relations-v1:" + canonical_json({
        "relations": sorted(<every relation name the bundle declares>),
        "rows": {relation: sorted(canonical rows) for every exported primitive
                 relation with at least one row, except the compatibility
                 relations (``model_describes_run``, ``index_describes_replay``)}}))

Unlike the static exporter's ``index``, the ``run`` context is *not* derived
from the identity: it is the receipt's run id, content the harness reported,
and it takes part in the identity like every other column.  Compatibility
relations bind the run to *other* contexts (a model, a static index) and are
keyed by the identity rather than part of it: declaring that index ``i``
describes the replay does not make it a different replay.

Every fact is first built under a placeholder identity, the identity is
computed from the finished rows, then the evidence ids (which embed
``replay_digest[:12]`` and the row digest) are computed -- content digest, then
ids.  The ``receipts`` object of ``receipt.json`` (wall-clock timestamps, file
paths, container ids) and the directory the receipt was read from are *run
receipts*: carried in bundle metadata under ``RECEIPT_METADATA_KEYS`` and
excluded from ``bundle_digest``, so a receipt never becomes identity.

EVIDENCE
--------
Ids are ``<prefix>:<replay12>:<relation>:<row12>`` where ``prefix`` is the
evidence-id prefix of the relation's producer class (``EVIDENCE_PREFIXES``:
``replay``, ``php``, ``go``, ``shen``, ``mut``, ``reviewer``, ``modelcheck``;
the ``php-census`` class shares the ``php`` prefix).  Every relation of the
frozen schema is owned: the harness (``replay``) owns the request, effect, post-state and kill
closures and the sequence, response and stability closures, the model runner
(``shen``) owns ``model_admissible_closed``, ``model_effect_seqs_closed``,
``model_writes_closed`` and ``model_describes_run``, the mutation tool
(``mut``) owns ``mutants_closed``, the reviewer owns
``index_describes_replay`` and ``model_checker_admitted``, and the typed
well-formedness checker (``modelcheck``) owns ``model_well_formed`` -- so a
runner cannot emit another producer's closure and the validator refuses one
that tries (``evidence-producer``).
A relation that declared no class would use the ``replay`` prefix.  ``row12 =
sha256(canonical_json([relation, row]))[:12]``.

``Evidence.source`` is the producer string and its first whitespace-delimited
token is the *producer class* that ``claims.validation`` checks against the
relation's declared ``producer_classes`` (issue ``evidence-producer``).  The
default source for an observation row is ``"<class> capcov.claims.replay.replay_facts v1"``
(the class the schema attributes the relation to, then the transcriber); a
``<relation>.json`` may name its real producer in ``producer`` (for example
``"php target-cloud 1a2b3c"``) and that string is carried verbatim -- so a file
that claims a producer class the relation does not admit is rejected at
ingestion, which is the single boundary at which producer authority is
enforced.  Completeness witnesses use
``"<class> capcov.claims.replay.replay_facts <predicate>-v1"`` -- the producer
class the schema attributes the closure to (``witness_source``), then the
transcriber, then the predicate whose emission condition was checked; a
witness the schema leaves unowned carries no class token, and compatibility
rows use ``default_source`` like any other owned row.  Rows depend on the ``replay_run`` row,
on the ``replay_request`` row of their request when one is exported, and on
``external:model:<model>``, ``external:git-commit:<commit>``,
``external:snapshot:<snapshot>`` and ``external:index:<index>`` for the
identities the receipt only names; a ``replay_stability`` row also depends on
``external:run:<run_a>`` and ``external:run:<run_b>``, the two selftest runs
whose provenance the harness matched against this one.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..ir import (Atom, Bundle, Column, Constant, Context, Evidence,
                  RelationDecl, TypeName, canonical_json, digest as ir_digest)
from ..validation import ValidationError, assert_valid
from . import load_replay_schema

EXPORT_VERSION = "v1"
EXPORTER = "capcov.claims.replay.replay_facts"
PRODUCER = f"{EXPORTER} {EXPORT_VERSION}"

STATUS_COMPLETE = "complete"
STATUS_RESOURCE_EXHAUSTED = "resource-exhausted"
STATUS_INVALID_INPUT = "invalid-input"
STATUS_STALE = "stale"

RECEIPT_VERSION = 1
RECEIPT_FILE = "receipt.json"

# The identity scheme (module docstring, IDENTITY): the value of metadata
# ``replay_digest_kind``.
REPLAY_IDENTITY = "replay-relations-v1"
_IDENTITY_PREFIX = REPLAY_IDENTITY + ":"
# Facts are assembled under this identity and re-keyed once the real one is
# known; it never appears in an exported bundle.
_PLACEHOLDER_IDENTITY = "0" * 64
# Run receipts carried in bundle metadata but excluded from ``bundle_digest``:
# the receipt.json ``receipts`` object (wall clock, file paths, container ids)
# and the directory the receipt was read from.
RECEIPT_METADATA_KEYS = ("receipts", "receipt_dir")

# Producer classes the schema attributes rows to, and the evidence-id prefix
# of each (module docstring, EVIDENCE).
EVIDENCE_PREFIXES = {
    "replay": "replay", "php": "php", "go": "go", "shen": "shen", "mut": "mut",
    "reviewer": "reviewer", "php-census": "php", "modelcheck": "modelcheck",
}
_UNOWNED_PREFIX = "replay"

# The observation relations read from ``<relation>.json``; ``replay_run`` is
# the receipt header and has no file.
OBSERVATION_FILES = (
    "replay_request", "php_post_state", "go_post_state", "php_effect", "go_effect",
    "model_effect", "model_admissible", "model_writes", "mutant", "mutant_killed",
    # ordering, cross-request and cross-run observations (module docstring)
    "php_effect_seq", "go_effect_seq", "model_effect_seq", "replay_request_seq",
    "php_response", "go_response", "replay_stability",
    # the typed well-formedness certificate of the model the judge binds to
    "model_well_formed", "model_operation_checked",
)

# Relations whose rows are *about* one model rather than merely scoped to it: a
# row naming another model is a certificate for a different artifact, i.e. a
# leftover of another check, and is ``stale`` rather than ``invalid-input``
# (module docstring, MODEL WELL-FORMEDNESS).
STALE_ON_FOREIGN_MODEL = frozenset({"model_well_formed", "model_operation_checked"})

# Relations that admit one observation per key (module docstring, RECEIPT
# DIRECTORY CONTRACT): relation -> the columns that identify the event; the
# remaining columns are the observed value and may not differ between rows.
UNIQUE_KEYS = {
    "php_effect": ("run", "req", "table", "kind", "pk"),
    "go_effect": ("run", "req", "table", "kind", "pk"),
    "model_effect": ("run", "model", "req", "table", "kind", "pk"),
    "php_post_state": ("run", "req"),
    "go_post_state": ("run", "req"),
    "php_effect_seq": ("run", "req", "seq"),
    "go_effect_seq": ("run", "req", "seq"),
    "model_effect_seq": ("run", "model", "req", "seq"),
    "replay_request_seq": ("run", "req"),
    "php_response": ("run", "req"),
    "go_response": ("run", "req"),
    "replay_stability": ("run", "run_a", "run_b", "side"),
    "model_well_formed": ("model", "checker", "checker_version", "checker_binary"),
    "model_operation_checked": ("model", "operation", "checker", "checker_version", "checker_binary"),
    # the model's prediction for a tape position is a *set* of admissible post-states
    # (like ``model_admissible``), so the state digest is part of the key; what may not
    # differ is the class the model assigns to one of them.  The oracle answered once.
    "learn_prediction": ("run", "model", "learn", "tape", "req", "state_digest"),
    "learn_observation": ("run", "tape", "req"),
}

# The witnesses' predicate versions. Each Evidence.source names one of these so
# a reviewer knows which emission contract was checked.
WITNESS_REQUESTS = "requests-closed-v1"
WITNESS_PHP_EFFECTS = "php-effects-closed-v1"
WITNESS_GO_EFFECTS = "go-effects-closed-v1"
WITNESS_PHP_POST_STATES = "php-post-states-closed-v1"
WITNESS_GO_POST_STATES = "go-post-states-closed-v1"
WITNESS_MODEL_ADMISSIBLE = "model-admissible-closed-v1"
WITNESS_MUTANT_KILLS = "mutant-kills-closed-v1"
WITNESS_MODEL_WRITES = "model-writes-closed-v1"
WITNESS_MUTANTS = "mutants-closed-v1"
WITNESS_SCOPE_EXCLUSIONS = "model-scope-exclusions-closed-v1"
WITNESS_PHP_EFFECT_SEQS = "php-effect-seqs-closed-v1"
WITNESS_GO_EFFECT_SEQS = "go-effect-seqs-closed-v1"
WITNESS_MODEL_EFFECT_SEQS = "model-effect-seqs-closed-v1"
WITNESS_REQUEST_SEQS = "replay-request-seqs-closed-v1"
WITNESS_PHP_RESPONSES = "php-responses-closed-v1"
WITNESS_GO_RESPONSES = "go-responses-closed-v1"
WITNESS_STABILITY = "replay-stability-closed-v1"

# The old receipt-local admission file is retained only so its presence can be
# diagnosed.  It is deliberately never read as reviewer authority.
CHECKERS_FILE = "model_checkers.json"
CHECKERS_RELATION = "model_checker_admitted"
_REVIEWER_ADMISSION_KEYS = frozenset({
    "producer", "model", "operation", "checker", "checker_version",
    "checker_binary", "certificate",
})
UNSIGNED_REVIEWER_TOKENS = frozenset({
    "unassigned", "unsigned", "none", "nobody", "tbd", "pending", "placeholder",
})
_REVIEWER_NAME_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _reviewer_name_is_placeholder(name: str) -> bool:
    """Reject reserved placeholder words without matching inside real names."""
    normalized = unicodedata.normalize("NFKC", name).casefold()
    tokens = _REVIEWER_NAME_TOKEN_RE.findall(normalized)
    return not tokens or bool(UNSIGNED_REVIEWER_TOKENS.intersection(tokens))

# The learn campaign (module docstring, LEARN RECEIPT).  A subdirectory, because the
# learn files are a *different producer chain* -- a tape generator, the oracle and the
# model host -- bound to this run through the model, and because a receipt that has
# none is the normal case.
LEARN_DIR = "learn"
LEARN_RECEIPT_FILE = "learn_receipt.json"
LEARN_VERSION = 1
LEARN_FILES = ("learn_run", "learn_prediction", "learn_observation", "learn_unmodeled")
_LEARN_RECEIPT_KEYS = frozenset({"version", "model", "learn", "closed", "receipts"})
# learn_receipt.json ``closed`` key -> (witness relation, predicate version)
_LEARN_WITNESSES = {
    "learn_predictions": ("learn_predictions_closed", "learn-predictions-closed-v1"),
    "learn_observations": ("learn_observations_closed", "learn-observations-closed-v1"),
    "learn_unmodeled": ("learn_unmodeled_closed", "learn-unmodeled-closed-v1"),
}

# The reviewer's scope exclusions (module docstring, REVIEWER SCOPE EXCLUSIONS).
EXCLUSIONS_FILE = "model_scope_exclusions.json"
_EXCLUSIONS_KEYS = frozenset({"producer", "reviewer", "reviewed_at", "reviewed_against", "rows"})

# receipt.json ``closed`` key -> (witness relation, predicate version).
_RUN_WITNESSES = {
    "replay_requests": ("replay_requests_closed", WITNESS_REQUESTS),
    "php_effects": ("php_effects_closed", WITNESS_PHP_EFFECTS),
    "go_effects": ("go_effects_closed", WITNESS_GO_EFFECTS),
    "php_post_states": ("php_post_states_closed", WITNESS_PHP_POST_STATES),
    "go_post_states": ("go_post_states_closed", WITNESS_GO_POST_STATES),
    "model_admissible": ("model_admissible_closed", WITNESS_MODEL_ADMISSIBLE),
    "mutant_kills": ("mutant_kills_closed", WITNESS_MUTANT_KILLS),
    "model_scope_exclusions": ("model_scope_exclusions_closed", WITNESS_SCOPE_EXCLUSIONS),
    "php_effect_seqs": ("php_effect_seqs_closed", WITNESS_PHP_EFFECT_SEQS),
    "go_effect_seqs": ("go_effect_seqs_closed", WITNESS_GO_EFFECT_SEQS),
    "model_effect_seqs": ("model_effect_seqs_closed", WITNESS_MODEL_EFFECT_SEQS),
    "replay_request_seqs": ("replay_request_seqs_closed", WITNESS_REQUEST_SEQS),
    "php_responses": ("php_responses_closed", WITNESS_PHP_RESPONSES),
    "go_responses": ("go_responses_closed", WITNESS_GO_RESPONSES),
    "replay_stability": ("replay_stability_closed", WITNESS_STABILITY),
}
# Witnesses keyed by the model rather than the run.
_MODEL_KEYED_WITNESSES = frozenset({"model_scope_exclusions_closed"})
# Run-keyed witnesses that also name the model (``(run, model)`` columns).
_RUN_AND_MODEL_WITNESSES = frozenset({"model_admissible_closed", "model_effect_seqs_closed"})
_RECEIPT_KEYS = frozenset({
    "version", "run", "nonce", "snapshot", "model", "php_commit", "go_commit",
    "closed", "model_writes_closed", "mutants_closed", "receipts",
})

# Relations the frozen primitive schema points at but does not declare: the
# derived ``completes`` target of ``mutant_kills_closed`` and the derived
# compatibility target of ``index_describes_replay``.  They are declared here
# as rule-less stubs only so an exported bundle validates on its own; the
# replay rule pack owns their rules and declares them again, byte-identically
# (``combine.combine`` refuses a non-identical duplicate).  ``mutant_killed_in``
# is the (run, mutant) projection of ``mutant_killed``; ``op_qualified_rt`` is
# the (index, run, op) join of a statically declared op with a replayed request
# for it, which is why it carries both contexts and is runtime-bound.
_DERIVED_TARGET_DECLS = (
    RelationDecl("mutant_killed_in",
                 (Column("run", "symbol", True), Column("mutant", "symbol")),
                 modality="derived", binding="runtime", primitive=False,
                 context_indices=("run",)),
    RelationDecl("op_qualified_rt",
                 (Column("index", "digest", True), Column("run", "symbol", True),
                  Column("op", "symbol")),
                 modality="derived", binding="runtime", primitive=False,
                 context_indices=("index", "run")),
)
STUB_RELATIONS = frozenset(decl.name for decl in _DERIVED_TARGET_DECLS)


# ---------------------------------------------------------------------------
# Public parameter / result types


@dataclass(frozen=True)
class ExportLimits:
    """Finite execution bars. Tripping one is ``resource-exhausted``, never an
    exception and never silently raised. ``rows`` mirrors the Soufflé kernel's
    100k-row cap over every relation, inputs included; ``file_bytes`` bounds
    what is read from any one ``<relation>.json``."""
    rows: int = 100_000
    file_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True)
class ExportResult:
    status: str
    bundle: Bundle | None
    counts: dict[str, int] = field(default_factory=dict)
    messages: tuple[str, ...] = ()


class ExportInputError(ValueError):
    """A receipt row or header broke an exporter precondition."""


class StaleReceiptError(ExportInputError):
    """A row file names a run other than the receipt's."""


# ---------------------------------------------------------------------------
# Identity helpers


def row_digest(relation: str, row: list[Any]) -> str:
    return hashlib.sha256(canonical_json([relation, list(row)]).encode("utf-8")).hexdigest()


def evidence_prefix(relation: RelationDecl) -> str:
    """The evidence-id prefix of a relation's producer class (module docstring, EVIDENCE)."""
    if relation.producer_classes:
        return EVIDENCE_PREFIXES.get(relation.producer_classes[0], _UNOWNED_PREFIX)
    return _UNOWNED_PREFIX


def evidence_id(replay_digest: str, relation: RelationDecl, row: list[Any]) -> str:
    """``<prefix>:<replay12>:<relation>:<row12>``."""
    return (f"{evidence_prefix(relation)}:{replay_digest[:12]}:{relation.name}:"
            f"{row_digest(relation.name, row)[:12]}")


def replay_relations_identity(rows_by_relation: Mapping[str, Iterable[list[Any]]],
                              relation_names: Iterable[str]) -> str:
    """The ``replay-relations-v1`` identity of a set of exported rows.

    ``rows_by_relation`` maps a relation to its full rows; only relations with
    at least one row take part. Row order and relation order do not matter;
    the declared relation names do.
    """
    payload = {
        "relations": sorted(set(relation_names)),
        "rows": {
            relation: sorted((list(row) for row in rows), key=canonical_json)
            for relation, rows in rows_by_relation.items() if rows
        },
    }
    return hashlib.sha256((_IDENTITY_PREFIX + canonical_json(payload)).encode("utf-8")).hexdigest()


def replay_relations() -> tuple[RelationDecl, ...]:
    """The frozen primitive declarations plus the derived-target stubs."""
    frozen = tuple(_relation_from_json(item) for item in load_replay_schema()["relations"])
    return frozen + _DERIVED_TARGET_DECLS


def primitive_relations() -> tuple[RelationDecl, ...]:
    """The frozen primitive declarations alone (what the exporter produces rows for)."""
    return tuple(_relation_from_json(item) for item in load_replay_schema()["relations"])


def _relation_from_json(raw: dict) -> RelationDecl:
    return RelationDecl(
        raw["name"],
        tuple(Column(c["name"], c["type"], c["context"]) for c in raw["columns"]),
        raw["modality"], raw["polarity"], raw["binding"], raw["primitive"],
        tuple(raw["producer_classes"]), tuple(raw["context_indices"]), raw["completes"],
        raw["finite"], raw["nonempty"], tuple(raw["compatibility_targets"]),
        tuple(raw["compatibility_context_indices"]),
    )


def default_source(relation: RelationDecl) -> str:
    """``"<class> <PRODUCER>"`` for an owned relation, ``PRODUCER`` otherwise."""
    if relation.producer_classes:
        return f"{relation.producer_classes[0]} {PRODUCER}"
    return PRODUCER


def witness_source(relation: RelationDecl, predicate: str) -> str:
    """``"<class> <EXPORTER> <predicate>"`` for an owned witness, else without the class."""
    if relation.producer_classes:
        return f"{relation.producer_classes[0]} {EXPORTER} {predicate}"
    return f"{EXPORTER} {predicate}"


# ---------------------------------------------------------------------------
# Row accumulator


class _Facts:
    """Deduplicating fact/evidence accumulator.

    A row is identified by ``(relation, values)``; adding it twice merges the
    dependency sets (the union is sorted by ``Evidence``), so a row reported
    twice yields one fact with one content-derived evidence id.
    """

    def __init__(self, replay_digest: str, relations: dict[str, RelationDecl]) -> None:
        self.replay_digest = replay_digest
        self.relations = relations
        self.rows: dict[tuple[str, tuple], dict] = {}

    def add(self, relation: str, values: dict[str, Any], *, source: str,
            depends_on: Iterable[str] = (), kind: str = "fact") -> str:
        decl = self.relations[relation]
        row: list[Any] = []
        for column in decl.columns:
            if column.name not in values:
                raise ExportInputError(f"{relation}: missing column {column.name!r}")
            row.append(_checked(relation, column, values[column.name]))
        key = (relation, tuple(row))
        eid = evidence_id(self.replay_digest, decl, row)
        entry = self.rows.get(key)
        if entry is None:
            entry = {"id": eid, "row": row, "source": source, "kind": kind,
                     "depends_on": set()}
            self.rows[key] = entry
        entry["depends_on"].update(d for d in depends_on if d and d != eid)
        return eid

    def count(self, relation: str) -> int:
        return sum(1 for rel, _ in self.rows if rel == relation)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for rel, _ in self.rows:
            out[rel] = out.get(rel, 0) + 1
        return dict(sorted(out.items()))

    def __len__(self) -> int:
        return len(self.rows)

    def identity(self) -> str:
        """``replay_relations_identity`` over every accumulated row of a
        non-compatibility relation (module docstring, IDENTITY)."""
        by_relation: dict[str, list[list[Any]]] = {}
        for (relation, _), entry in self.rows.items():
            if self.relations[relation].modality.value == "compatibility":
                continue
            by_relation.setdefault(relation, []).append(list(entry["row"]))
        return replay_relations_identity(by_relation, self.relations)

    def rebase(self, replay_digest: str) -> None:
        """Recompute every evidence id under ``replay_digest``.

        No column is rewritten -- the ``run`` context is receipt content, not a
        placeholder -- but every id embeds the identity, so ``depends_on``
        entries are rewritten through the old->new id map and every external
        id is left alone.
        """
        rename: dict[str, str] = {}
        rebased: dict[tuple[str, tuple], dict] = {}
        for (relation, _), entry in self.rows.items():
            row = list(entry["row"])
            eid = evidence_id(replay_digest, self.relations[relation], row)
            rename[entry["id"]] = eid
            rebased[(relation, tuple(row))] = {**entry, "id": eid, "row": row}
        for entry in rebased.values():
            entry["depends_on"] = {rename.get(d, d) for d in entry["depends_on"]} - {entry["id"]}
        self.rows = rebased
        self.replay_digest = replay_digest

    def materialize(self) -> tuple[tuple[Atom, ...], tuple[Evidence, ...]]:
        facts: list[Atom] = []
        evidence: list[Evidence] = []
        for (relation, _), entry in self.rows.items():
            decl = self.relations[relation]
            atom = Atom(relation, tuple(
                Constant(value, column.type)
                for value, column in zip(entry["row"], decl.columns)))
            context = {
                name: entry["row"][[c.name for c in decl.columns].index(name)]
                for name in decl.context_indices
            }
            facts.append(atom)
            evidence.append(Evidence(
                entry["id"], atom, Context.from_mapping(context), entry["source"],
                tuple(sorted(entry["depends_on"])), entry["kind"]))
        return tuple(facts), tuple(evidence)


def _checked(relation: str, column: Column, value: Any) -> Any:
    t = column.type
    if t in (TypeName.SYMBOL, TypeName.DIGEST):
        if not isinstance(value, str):
            raise ExportInputError(f"{relation}.{column.name}: expected str, got {value!r}")
        return value
    if t == TypeName.UNSIGNED:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ExportInputError(f"{relation}.{column.name}: expected unsigned, got {value!r}")
        return value
    if t == TypeName.BOOLEAN:
        if not isinstance(value, bool):
            raise ExportInputError(f"{relation}.{column.name}: expected bool, got {value!r}")
        return value
    raise ExportInputError(f"{relation}.{column.name}: unsupported column type {t}")


# ---------------------------------------------------------------------------
# Receipt reading


def _require_str(receipt: Mapping[str, Any], key: str) -> str:
    value = receipt.get(key)
    if not isinstance(value, str) or not value:
        raise ExportInputError(f"{RECEIPT_FILE}: {key!r} must be a non-empty string")
    return value


def _read_json(path: Path, limit: int) -> Any:
    size = path.stat().st_size
    if size > limit:
        raise ExportInputError(f"{path.name}: {size} bytes exceed the {limit}-byte file limit")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportInputError(f"{path.name}: not valid JSON ({exc})") from exc


def _read_receipt(receipt_dir: Path, run: str, limits: ExportLimits) -> dict[str, Any]:
    path = receipt_dir / RECEIPT_FILE
    if not receipt_dir.is_dir():
        raise ExportInputError(f"receipt directory {str(receipt_dir)!r} does not exist")
    if not path.is_file():
        raise ExportInputError(f"{RECEIPT_FILE} is missing from {str(receipt_dir)!r}")
    receipt = _read_json(path, limits.file_bytes)
    if not isinstance(receipt, Mapping):
        raise ExportInputError(f"{RECEIPT_FILE}: must be an object")
    unknown = set(receipt) - _RECEIPT_KEYS
    if unknown:
        raise ExportInputError(f"{RECEIPT_FILE}: unknown keys {sorted(unknown)}")
    version = receipt.get("version")
    if isinstance(version, bool) or version != RECEIPT_VERSION:
        raise ExportInputError(f"{RECEIPT_FILE}: version must be {RECEIPT_VERSION}, got {version!r}")
    header = {key: _require_str(receipt, key)
              for key in ("run", "nonce", "snapshot", "model", "php_commit", "go_commit")}
    if header["run"] != run:
        raise ExportInputError(f"{RECEIPT_FILE}: receipt is for run {header['run']!r}, "
                               f"caller asked for {run!r}")
    closed_raw = receipt.get("closed", {})
    if not isinstance(closed_raw, Mapping):
        raise ExportInputError(f"{RECEIPT_FILE}: 'closed' must be an object")
    unknown = set(closed_raw) - set(_RUN_WITNESSES)
    if unknown:
        raise ExportInputError(f"{RECEIPT_FILE}: unknown 'closed' keys {sorted(unknown)}")
    closed = {}
    for key in _RUN_WITNESSES:
        value = closed_raw.get(key, False)
        if not isinstance(value, bool):
            raise ExportInputError(f"{RECEIPT_FILE}: closed.{key} must be a boolean")
        closed[key] = value
    scoped = {}
    for key in ("model_writes_closed", "mutants_closed"):
        entries = receipt.get(key, [])
        if not isinstance(entries, list):
            raise ExportInputError(f"{RECEIPT_FILE}: {key!r} must be an array")
        rows = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping) or set(entry) - {"model", "op"}:
                raise ExportInputError(f"{RECEIPT_FILE}: {key}[{index}] must be {{model?, op}}")
            model = entry.get("model", header["model"])
            if model != header["model"]:
                raise ExportInputError(f"{RECEIPT_FILE}: {key}[{index}] names model "
                                       f"{str(model)[:12]!r}, the receipt's model is "
                                       f"{header['model'][:12]!r}")
            if not isinstance(entry.get("op"), str) or not entry["op"]:
                raise ExportInputError(f"{RECEIPT_FILE}: {key}[{index}].op must be a non-empty string")
            rows.append({"model": model, "op": entry["op"]})
        scoped[key] = rows
    receipts = receipt.get("receipts", {})
    if not isinstance(receipts, Mapping):
        raise ExportInputError(f"{RECEIPT_FILE}: 'receipts' must be an object")
    return {**header, "closed": closed, **scoped, "receipts": dict(receipts)}


def _read_exclusions(receipt_dir: Path, header: Mapping[str, str],
                     limits: ExportLimits) -> tuple[str | None, list[dict[str, Any]]]:
    """``(source, rows)`` of ``model_scope_exclusions.json``; ``(None, [])`` when absent."""
    path = receipt_dir / EXCLUSIONS_FILE
    if not path.is_file():
        return None, []
    document = _read_json(path, limits.file_bytes)
    if not isinstance(document, Mapping) or set(document) - _EXCLUSIONS_KEYS:
        raise ExportInputError(f"{EXCLUSIONS_FILE}: must be {{reviewer, reviewed_at, reviewed_against, rows, producer?}}")
    for key in ("reviewer", "reviewed_at"):
        if not isinstance(document.get(key), str) or not document[key].strip():
            raise ExportInputError(f"{EXCLUSIONS_FILE}: {key!r} must be a non-empty string")
    against = document.get("reviewed_against")
    if (not isinstance(against, Mapping) or set(against) != {"model", "run"}
            or not all(isinstance(against[k], str) and against[k] for k in ("model", "run"))):
        raise ExportInputError(f"{EXCLUSIONS_FILE}: 'reviewed_against' must be {{model, run}} with non-empty strings")
    if against["model"] != header["model"]:
        raise ExportInputError(f"{EXCLUSIONS_FILE}: stale review: reviewed against model "
                               f"{against['model'][:12]!r}, the receipt's model is {header['model'][:12]!r}")
    producer = document.get("producer")
    if producer is not None and (not isinstance(producer, str) or not producer.strip()):
        raise ExportInputError(f"{EXCLUSIONS_FILE}: 'producer' must be a non-empty string")
    prefix = producer.strip() if producer is not None else f"reviewer {document['reviewer'].strip()}"
    suffix = f"{document['reviewed_at'].strip()} model:{header['model'][:12]} run:{against['run']}"
    # a producer that already names what was reviewed is carried verbatim
    source = prefix if prefix.endswith(suffix) else f"{prefix} {suffix}"
    raw_rows = document.get("rows", [])
    if not isinstance(raw_rows, list):
        raise ExportInputError(f"{EXCLUSIONS_FILE}: 'rows' must be an array")
    rows = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, Mapping) or set(raw) - {"model", "table", "reason"}:
            raise ExportInputError(f"{EXCLUSIONS_FILE}: rows[{index}] must be {{model?, table, reason}}")
        row = {"model": raw.get("model", header["model"]), "table": raw.get("table"), "reason": raw.get("reason")}
        if row["model"] != header["model"]:
            raise ExportInputError(f"{EXCLUSIONS_FILE}: rows[{index}] names model {str(row['model'])[:12]!r}, "
                                   f"the receipt's model is {header['model'][:12]!r}")
        for key in ("table", "reason"):
            if not isinstance(row[key], str) or not row[key]:
                raise ExportInputError(f"{EXCLUSIONS_FILE}: rows[{index}].{key} must be a non-empty string")
        rows.append(row)
    return source, rows


def _read_learn_receipt(receipt_dir: Path, header: Mapping[str, str],
                        limits: ExportLimits) -> dict[str, Any] | None:
    """The ``learn/learn_receipt.json`` header, or ``None`` when no learn campaign is bound.

    A run with no ``learn/`` directory is the normal case and is not an error:
    the learn relations stay empty, no closure is emitted and the qualification
    path is exactly the one a receipt without them always had.
    """
    path = receipt_dir / LEARN_DIR / LEARN_RECEIPT_FILE
    if not path.is_file():
        return None
    document = _read_json(path, limits.file_bytes)
    if not isinstance(document, Mapping):
        raise ExportInputError(f"{LEARN_DIR}/{LEARN_RECEIPT_FILE}: must be an object")
    unknown = set(document) - _LEARN_RECEIPT_KEYS
    if unknown:
        raise ExportInputError(f"{LEARN_DIR}/{LEARN_RECEIPT_FILE}: unknown keys {sorted(unknown)}")
    version = document.get("version")
    if isinstance(version, bool) or version != LEARN_VERSION:
        raise ExportInputError(f"{LEARN_DIR}/{LEARN_RECEIPT_FILE}: version must be "
                               f"{LEARN_VERSION}, got {version!r}")
    learn = document.get("learn")
    if not isinstance(learn, str) or not learn:
        raise ExportInputError(f"{LEARN_DIR}/{LEARN_RECEIPT_FILE}: 'learn' must be a non-empty string")
    model = document.get("model", header["model"])
    if model != header["model"]:
        # the campaign was run against another model: it says nothing about this run
        raise StaleReceiptError(f"{LEARN_DIR}/{LEARN_RECEIPT_FILE}: the learn campaign was run "
                                f"against model {str(model)[:12]!r}, the receipt's model is "
                                f"{header['model'][:12]!r}")
    closed_raw = document.get("closed", {})
    if not isinstance(closed_raw, Mapping):
        raise ExportInputError(f"{LEARN_DIR}/{LEARN_RECEIPT_FILE}: 'closed' must be an object")
    unknown = set(closed_raw) - set(_LEARN_WITNESSES)
    if unknown:
        raise ExportInputError(f"{LEARN_DIR}/{LEARN_RECEIPT_FILE}: unknown 'closed' keys {sorted(unknown)}")
    closed = {}
    for key in _LEARN_WITNESSES:
        value = closed_raw.get(key, False)
        if not isinstance(value, bool):
            raise ExportInputError(f"{LEARN_DIR}/{LEARN_RECEIPT_FILE}: closed.{key} must be a boolean")
        closed[key] = value
    receipts = document.get("receipts", {})
    if not isinstance(receipts, Mapping):
        raise ExportInputError(f"{LEARN_DIR}/{LEARN_RECEIPT_FILE}: 'receipts' must be an object")
    return {"learn": learn, "model": model, "closed": closed, "receipts": dict(receipts)}


def _read_rows(receipt_dir: Path, relation: RelationDecl, header: Mapping[str, str],
               limits: ExportLimits, filename: str | None = None,
               defaults: Mapping[str, str] | None = None) -> tuple[str | None, list[dict[str, Any]]]:
    """``(producer, rows)`` of ``<relation>.json`` (or ``filename``); ``(None, [])`` when absent.

    ``defaults`` names further columns a row may omit because the enclosing
    receipt already fixes them (the learn digest of ``learn/learn_receipt.json``);
    a row that names a *different* value is refused, as for ``run`` and ``model``.
    """
    path = receipt_dir / (filename or f"{relation.name}.json")
    if not path.is_file():
        return None, []
    document = _read_json(path, limits.file_bytes)
    if not isinstance(document, Mapping) or set(document) - {"rows", "producer"}:
        raise ExportInputError(f"{path.name}: must be {{rows, producer?}}")
    producer = document.get("producer")
    if producer is not None and (not isinstance(producer, str) or not producer.strip()):
        raise ExportInputError(f"{path.name}: 'producer' must be a non-empty string")
    raw_rows = document.get("rows", [])
    if not isinstance(raw_rows, list):
        raise ExportInputError(f"{path.name}: 'rows' must be an array")
    names = [column.name for column in relation.columns]
    rows = []
    for index, raw in enumerate(raw_rows):
        if not isinstance(raw, Mapping):
            raise ExportInputError(f"{path.name}: rows[{index}] must be an object")
        unknown = set(raw) - set(names)
        if unknown:
            raise ExportInputError(f"{path.name}: rows[{index}] has unknown columns {sorted(unknown)}")
        row = dict(raw)
        if "run" in names:
            row.setdefault("run", header["run"])
            if row["run"] != header["run"]:
                raise StaleReceiptError(f"{path.name}: rows[{index}] names run {row['run']!r}, "
                                        f"the receipt is for {header['run']!r}")
        if "model" in names:
            row.setdefault("model", header["model"])
            if row["model"] != header["model"]:
                if relation.name in STALE_ON_FOREIGN_MODEL:
                    raise StaleReceiptError(
                        f"{path.name}: rows[{index}] certifies model {str(row['model'])[:12]!r}, "
                        f"the receipt's model is {header['model'][:12]!r}: the certificate is for "
                        f"another model and does not describe this run")
                raise ExportInputError(f"{path.name}: rows[{index}] names model "
                                       f"{str(row['model'])[:12]!r}, the receipt's model is "
                                       f"{header['model'][:12]!r}")
        for name, value in (defaults or {}).items():
            if name not in names:
                continue
            row.setdefault(name, value)
            if row[name] != value:
                raise ExportInputError(f"{path.name}: rows[{index}] names {name} "
                                       f"{str(row[name])[:12]!r}, the receipt's {name} is "
                                       f"{str(value)[:12]!r}")
        missing = [name for name in names if name not in row]
        if missing:
            raise ExportInputError(f"{path.name}: rows[{index}] lacks columns {missing}")
        rows.append(row)
    key_columns = UNIQUE_KEYS.get(relation.name)
    if key_columns:
        seen: dict[tuple, tuple[int, dict[str, Any]]] = {}
        value_columns = [name for name in names if name not in key_columns]
        for index, row in enumerate(rows):
            key = tuple(canonical_json(row[name]) for name in key_columns)
            previous = seen.setdefault(key, (index, row))
            if previous[1] != row:
                raise ExportInputError(
                    f"{path.name}: rows[{previous[0]}] and rows[{index}] report the same "
                    f"{dict(zip(key_columns, (row[name] for name in key_columns)))} with different "
                    f"{value_columns}: one observation per {relation.name} key")
    return producer, rows


def _validated_reviewer_admissions(
    raw_admissions: Iterable[Mapping[str, Any]],
    header: Mapping[str, str],
    certificate_eids: Mapping[tuple[str, str, str, str, str, str], str],
    messages: list[str],
) -> list[tuple[str, dict[str, str], str]]:
    """Validate external reviewer authority and bind it to receipt certificates.

    Returns ``(source, exact operation admission row, certificate evidence id)``.
    Admission is pinned to the exact model, operation, checker, version, binary,
    and semantic certificate digest.
    """
    if isinstance(raw_admissions, (str, bytes, Mapping)):
        raise ExportInputError("reviewer_admissions must be an iterable of admission objects")
    try:
        admissions = list(raw_admissions)
    except TypeError as exc:
        raise ExportInputError("reviewer_admissions must be an iterable of admission objects") from exc

    validated: list[tuple[str, dict[str, str], str]] = []
    projected: set[tuple[str, str]] = set()
    for index, admission in enumerate(admissions):
        label = f"reviewer_admissions[{index}]"
        if not isinstance(admission, Mapping):
            raise ExportInputError(f"{label}: must be an object")
        if set(admission) != _REVIEWER_ADMISSION_KEYS:
            raise ExportInputError(
                f"{label}: must have exactly {sorted(_REVIEWER_ADMISSION_KEYS)}")
        values: dict[str, str] = {}
        for key in sorted(_REVIEWER_ADMISSION_KEYS):
            value = admission[key]
            if not isinstance(value, str) or not value.strip():
                raise ExportInputError(f"{label}.{key}: must be a non-empty string")
            values[key] = value
        if values["producer"].split(None, 1)[0] != "reviewer":
            raise ExportInputError(f"{label}.producer: must use the reviewer producer class")
        producer_parts = values["producer"].split(None, 1)
        reviewer = producer_parts[1].strip() if len(producer_parts) == 2 else ""
        if _reviewer_name_is_placeholder(reviewer):
            messages.append(
                f"{label}: reviewer is {reviewer!r}, a placeholder; this admission contributes no authority")
            continue
        for key in ("model", "checker_binary", "certificate"):
            if not _SHA256_RE.fullmatch(values[key]):
                raise ExportInputError(f"{label}.{key}: must be a lowercase sha256 digest")
        if values["model"] != header["model"]:
            raise ExportInputError(
                f"{label}: model {values['model'][:12]!r} does not match the receipt model "
                f"{header['model'][:12]!r}")

        certificate_key = (
            values["model"], values["operation"], values["checker"],
            values["checker_version"], values["checker_binary"], values["certificate"],
        )
        certificate_eid = certificate_eids.get(certificate_key)
        if certificate_eid is None:
            raise ExportInputError(
                f"{label}: no model_operation_checked row matches model, operation, checker, "
                "checker_version, checker_binary, and certificate")
        relation_key = certificate_key
        if relation_key in projected:
            raise ExportInputError(
                f"{label}: duplicate admission for operation {values['operation']!r} "
                f"under checker {values['checker']!r} {values['checker_version']!r}")
        projected.add(relation_key)
        validated.append((values["producer"], {
            key: values[key] for key in ("model", "operation", "checker", "checker_version",
                                         "checker_binary", "certificate")
        }, certificate_eid))
    return validated


# ---------------------------------------------------------------------------
# The exporter


def export_bundle(
    receipt_dir: str | Path,
    *,
    run: str,
    describes_indexes: Iterable[str] = (),
    reviewer_admissions: Iterable[Mapping[str, Any]] = (),
    limits: ExportLimits | None = None,
) -> ExportResult:
    """Export one receipt directory as a validated replay-facts ``Bundle``.

    ``run`` is the run the caller expects the receipt to be for; a receipt for
    another run is ``invalid-input``.  ``describes_indexes`` adds one
    ``index_describes_replay(index, run)`` row per static index digest the
    caller vouches for. ``reviewer_admissions`` is external reviewer authority:
    every entry must pin this receipt's model and one exact
    ``model_operation_checked`` tuple. Receipt-local
    ``model_checkers.json`` is ignored.  Returns ``ExportResult`` with status ``complete``
    (bundle present), ``resource-exhausted`` (a limit tripped; no bundle),
    ``stale`` (a row file names another run; no bundle) or ``invalid-input``
    (a receipt precondition or bundle validation failed; no bundle).  Never
    raises for a limit or a malformed receipt.
    """
    receipt_dir = Path(receipt_dir)
    limits = limits or ExportLimits()
    messages: list[str] = []
    if not isinstance(run, str) or not run:
        return ExportResult(STATUS_INVALID_INPUT, None, {}, ("run must be a non-empty string",))

    relations = {decl.name: decl for decl in replay_relations()}
    facts = _Facts(_PLACEHOLDER_IDENTITY, relations)
    try:
        header = _read_receipt(receipt_dir, run, limits)
        model = header["model"]
        model_ext = f"external:model:{model}"

        # --- header row ------------------------------------------------------
        run_eid = facts.add("replay_run", {
            "run": header["run"], "nonce": header["nonce"], "php_commit": header["php_commit"],
            "go_commit": header["go_commit"], "snapshot": header["snapshot"], "model": model,
        }, source=default_source(relations["replay_run"]), depends_on=[
            f"external:git-commit:{header['php_commit']}",
            f"external:git-commit:{header['go_commit']}",
            f"external:snapshot:{header['snapshot']}", model_ext,
        ])

        # --- observation files ---------------------------------------------------
        producers: dict[str, str] = {"replay_run": default_source(relations["replay_run"])}
        request_eids: dict[str, str] = {}
        certificate_eids: dict[tuple[str, str, str, str, str, str], str] = {}
        for name in OBSERVATION_FILES:
            decl = relations[name]
            producer, rows = _read_rows(receipt_dir, decl, header, limits)
            source = producer if producer is not None else default_source(decl)
            producers[name] = source
            if producer is None and not rows:
                messages.append(f"{name}.json absent: zero rows")
            names = [column.name for column in decl.columns]
            for row in rows:
                deps = []
                if "run" in names:
                    deps.append(run_eid)
                if "model" in names:
                    deps.append(model_ext)
                if "req" in names and name != "replay_request":
                    req_eid = request_eids.get(row["req"])
                    if req_eid is not None:
                        deps.append(req_eid)
                if name == "replay_stability":
                    # the two selftest runs are outside this receipt: name them, so a
                    # certificate that rests on cross-run stability says whose runs it rests on
                    deps.extend(f"external:run:{row[column]}" for column in ("run_a", "run_b"))
                eid = facts.add(name, row, source=source, depends_on=deps)
                if name == "replay_request":
                    request_eids[row["req"]] = eid
                elif name == "model_operation_checked":
                    certificate_eids[(row["model"], row["operation"], row["checker"],
                                      row["checker_version"], row["checker_binary"],
                                      row["certificate"])] = eid
            if len(facts) > limits.rows:
                return ExportResult(STATUS_RESOURCE_EXHAUSTED, None, facts.counts(),
                                    (f"rows {len(facts)} exceed limit {limits.rows}",))

        # --- the learn campaign, when one is bound to this run ----------------------
        learn_header = _read_learn_receipt(receipt_dir, header, limits)
        if learn_header is None:
            messages.append(f"{LEARN_DIR}/{LEARN_RECEIPT_FILE} absent: no learn campaign is bound to this run")
        else:
            learn = learn_header["learn"]
            learn_ext = f"external:learn:{learn}"
            learn_defaults = {"learn": learn}
            for name in LEARN_FILES:
                decl = relations[name]
                producer, rows = _read_rows(receipt_dir / LEARN_DIR, decl, header, limits,
                                            defaults=learn_defaults)
                source = producer if producer is not None else default_source(decl)
                producers[name] = source
                if producer is None and not rows:
                    messages.append(f"{LEARN_DIR}/{name}.json absent: zero rows")
                names = [column.name for column in decl.columns]
                for row in rows:
                    deps = [learn_ext]
                    if "run" in names:
                        deps.append(run_eid)
                    if "model" in names:
                        deps.append(model_ext)
                    facts.add(name, row, source=source, depends_on=deps)
            facts.add("learn_describes_model", {"learn": learn, "model": model},
                      source=default_source(relations["learn_describes_model"]),
                      depends_on=[learn_ext, model_ext])
            for key, (relation, predicate) in _LEARN_WITNESSES.items():
                if not learn_header["closed"][key]:
                    messages.append(f"{LEARN_DIR} closed.{key} is false: no {relation} witness")
                    continue
                if relation == "learn_unmodeled_closed":
                    values, deps = {"model": model, "learn": learn}, [model_ext, learn_ext]
                elif relation == "learn_predictions_closed":
                    values, deps = {"run": header["run"], "model": model}, [run_eid, model_ext]
                else:
                    values, deps = {"run": header["run"]}, [run_eid]
                facts.add(relation, values, source=witness_source(relations[relation], predicate),
                          depends_on=deps)
            if len(facts) > limits.rows:
                return ExportResult(STATUS_RESOURCE_EXHAUSTED, None, facts.counts(),
                                    (f"rows {len(facts)} exceed limit {limits.rows}",))

        # --- externally supplied reviewer admissions --------------------------------
        checkers = relations[CHECKERS_RELATION]
        admissions = _validated_reviewer_admissions(
            reviewer_admissions, header, certificate_eids, messages)
        if (receipt_dir / CHECKERS_FILE).is_file():
            messages.append(
                f"{CHECKERS_FILE} ignored: reviewer admissions must be supplied by the caller")
        if not admissions:
            messages.append("no caller-supplied reviewer admissions: zero admitted checkers")
        for source, row, certificate_eid in admissions:
            producers[CHECKERS_RELATION] = source
            facts.add(CHECKERS_RELATION, row, source=source,
                      depends_on=[certificate_eid])

        # --- reviewer scope exclusions (assumption-kind evidence) --------------------
        source, rows = _read_exclusions(receipt_dir, header, limits)
        if source is None:
            messages.append(f"{EXCLUSIONS_FILE} absent: zero exclusions")
        else:
            producers["model_scope_exclusion"] = source
            for row in rows:
                facts.add("model_scope_exclusion", row, source=source, depends_on=[model_ext], kind="assumption")

        # --- compatibility rows --------------------------------------------------
        facts.add("model_describes_run", {"model": model, "run": header["run"]},
                  source=default_source(relations["model_describes_run"]), depends_on=[run_eid, model_ext])
        for index in sorted(set(describes_indexes)):
            if not isinstance(index, str) or not index:
                raise ExportInputError("describes_indexes must contain non-empty digest strings")
            facts.add("index_describes_replay", {"index": index, "run": header["run"]},
                      source=default_source(relations["index_describes_replay"]),
                      depends_on=[run_eid, f"external:index:{index}"])

        # --- completeness witnesses ----------------------------------------------
        for key, (relation, predicate) in _RUN_WITNESSES.items():
            if not header["closed"][key]:
                messages.append(f"closed.{key} is false: no {relation} witness")
                continue
            if relation in _MODEL_KEYED_WITNESSES:
                values, deps = {"model": model}, [model_ext]
            else:
                values = {"run": header["run"]}
                deps = [run_eid]
                if relation in _RUN_AND_MODEL_WITNESSES:
                    values["model"] = model
                    deps.append(model_ext)
            facts.add(relation, values, source=witness_source(relations[relation], predicate),
                      depends_on=deps)
        for entry in header["model_writes_closed"]:
            facts.add("model_writes_closed", entry,
                      source=witness_source(relations["model_writes_closed"], WITNESS_MODEL_WRITES),
                      depends_on=[model_ext])
        for entry in header["mutants_closed"]:
            facts.add("mutants_closed", entry,
                      source=witness_source(relations["mutants_closed"], WITNESS_MUTANTS),
                      depends_on=[model_ext])
    except StaleReceiptError as exc:
        return ExportResult(STATUS_STALE, None, facts.counts(), (str(exc),))
    except ExportInputError as exc:
        return ExportResult(STATUS_INVALID_INPUT, None, facts.counts(), (str(exc),))
    except OSError as exc:
        return ExportResult(STATUS_INVALID_INPUT, None, facts.counts(),
                            (f"receipt directory unreadable: {exc}",))

    # --- bounds, assembly, validation ----------------------------------------
    if len(facts) > limits.rows:
        return ExportResult(STATUS_RESOURCE_EXHAUSTED, None, facts.counts(),
                            (f"rows {len(facts)} exceed limit {limits.rows}",))
    # content digest -> evidence ids (module docstring, IDENTITY)
    identity = facts.identity()
    facts.rebase(identity)
    messages.append(f"replay identity {identity} ({REPLAY_IDENTITY}); the receipt directory "
                    f"{str(receipt_dir)!r} and its receipts object are run receipts, not identity")
    fact_atoms, evidence = facts.materialize()
    metadata = {
        "export_version": EXPORT_VERSION,
        "exporter": EXPORTER,
        "run": header["run"],
        "replay_digest": identity,
        "replay_digest_kind": REPLAY_IDENTITY,
        "nonce": header["nonce"],
        "snapshot": header["snapshot"],
        "model": header["model"],
        "php_commit": header["php_commit"],
        "go_commit": header["go_commit"],
        "closed": header["closed"],
        "producers": {**producers, "exporter": PRODUCER},
        "describes_indexes": sorted(set(describes_indexes)),
        "row_counts": facts.counts(),
        "receipts": header["receipts"],
        "receipt_dir": str(receipt_dir),
    }
    bundle = Bundle(tuple(relations.values()), fact_atoms, (), (), tuple(metadata.items()),
                    evidence=evidence)
    try:
        assert_valid(bundle)
    except ValidationError as exc:
        return ExportResult(STATUS_INVALID_INPUT, None, facts.counts(),
                            tuple(f"{i.code}: {i.message} at {i.path}" for i in exc.issues)
                            if hasattr(exc, "issues") else (str(exc),))
    return ExportResult(STATUS_COMPLETE, bundle, facts.counts(), tuple(messages))


def _without_receipts(items: Any) -> Any:
    """``items`` (a mapping or a frozen sequence of pairs) minus the receipt keys."""
    pairs = list(items.items()) if isinstance(items, Mapping) else list(items)
    return tuple((k, v) for k, v in pairs if k not in RECEIPT_METADATA_KEYS)


def bundle_digest(bundle: Bundle) -> str:
    """The canonical digest of an exported bundle (``claims.ir.digest``) with
    the run receipts ``RECEIPT_METADATA_KEYS`` removed from the metadata, so
    the digest is a function of the exported content and not of when, where
    or in which container the replay happened. ``combine.combine`` nests each
    input's metadata under ``source_<i>``; the receipts are removed there too,
    so a combined bundle's digest is receipt-free as well."""
    metadata = []
    for key, value in _without_receipts(bundle.metadata):
        if key.startswith("source_") and isinstance(value, (Mapping, tuple, list)) \
                and all(isinstance(item, (tuple, list)) and len(item) == 2 for item in
                        (value.items() if isinstance(value, Mapping) else value)):
            value = _without_receipts(value)
        metadata.append((key, value))
    return ir_digest(replace(bundle, metadata=tuple(metadata)))


__all__ = [
    "EXPORT_VERSION", "EXPORTER", "PRODUCER", "REPLAY_IDENTITY", "RECEIPT_VERSION",
    "RECEIPT_FILE", "RECEIPT_METADATA_KEYS", "EVIDENCE_PREFIXES", "OBSERVATION_FILES",
    "STATUS_COMPLETE", "STATUS_RESOURCE_EXHAUSTED", "STATUS_INVALID_INPUT", "STATUS_STALE", "UNIQUE_KEYS",
    "EXCLUSIONS_FILE", "WITNESS_SCOPE_EXCLUSIONS", "CHECKERS_FILE", "CHECKERS_RELATION",
    "STALE_ON_FOREIGN_MODEL", "LEARN_DIR", "LEARN_RECEIPT_FILE", "LEARN_FILES", "LEARN_VERSION",
    "ExportLimits", "ExportResult", "ExportInputError", "StaleReceiptError",
    "evidence_id", "evidence_prefix", "row_digest", "replay_relations_identity",
    "replay_relations", "primitive_relations", "STUB_RELATIONS", "default_source", "witness_source",
    "export_bundle", "bundle_digest",
]
