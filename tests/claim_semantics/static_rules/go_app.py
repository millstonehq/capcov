"""The go_app differential bundle: golden SCIP index + hand-built tree-sitter side + rule pack.

Section 29 evaluates the exported facts of the committed golden
``tests/fixtures/scip_go_app_index.json`` (scip-go 0.2.7 over
``tests/fixtures/go_app``, canonicalized with ``tests/scip/canonicalize.jq``)
against the reviewed rule pack in both kernels.  Tree-sitter is not in the
devShell, so the tree-sitter side (route site, handler location, op sites,
entities, blind spots, residue) is written here by hand from the fixture
source; it is a review of five short Go files, not recognizer output, and the
census it asserts (no residue, no blind spots) is that review's finding.

Lines are 1-based throughout (the exporter's ``LINE_FRAME``): ``GetJob`` is
defined on ``api/jobs.go:10`` (SCIP range line 9), the route call sits on line
17, the ``First`` and ``Create`` sites on ``internal/jobs/repo.go`` lines 15
and 20.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

SRC = Path(__file__).resolve().parents[3] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from capcov import artifacts  # noqa: E402
from capcov.claims import (Atom, Bundle, Claim, Column, Constant, Context, Evidence,  # noqa: E402
                           RelationDecl, Rule, Variable)
from capcov.claims.static import scip_facts  # noqa: E402
from capcov.claims.static.combine import combine  # noqa: E402
from capcov.scip import runner  # noqa: E402

try:
    from .adapter import pack_bundle
except ImportError:  # unittest discover -s imports this directory as top-level
    from static_rules.adapter import pack_bundle

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
GO_APP = FIXTURES / "go_app"
GOLDEN = FIXTURES / "scip_go_app_index.json"
MODULE = "github.com/example/jobsvc"
LANGUAGE = "go"
SURFACE = "http:GET /jobs/{id}"          # the deep adapter's surface id: http:<METHOD> <path>
RUN = "run-1"
TENANT = "tenant-a"
UNDECLARED_SURFACE = "http:POST /jobs"     # observed at runtime, absent from the route inventory

_PREFIX = f"scip-go gomod {MODULE} . "
GET_JOB = _PREFIX + f"`{MODULE}/api`/GetJob()."
REGISTER = _PREFIX + f"`{MODULE}/api`/Register()."
HANDLE = _PREFIX + f"`{MODULE}/api`/handle()."
FETCH = _PREFIX + f"`{MODULE}/internal/service`/Service#Fetch()."
REPO_GET = _PREFIX + f"`{MODULE}/internal/jobs`/Repo#Get()."
REPO_WRITE = _PREFIX + f"`{MODULE}/internal/jobs`/Repo#Write()."
DB_FIRST = _PREFIX + f"`{MODULE}/gorm`/DB#First()."
DB_CREATE = _PREFIX + f"`{MODULE}/gorm`/DB#Create()."

HANDLER_NODE = f"{MODULE}/api:GetJob"
GET_JOB_LINE = 10
ROUTE_LINE = 17
REPO_GET_OP_LINE = 15
REPO_WRITE_OP_LINE = 20
MAX_HOPS = 64


def golden_raw() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def normalized(raw: dict | None = None) -> dict:
    return runner.normalize_scip_json(raw if raw is not None else golden_raw(), retain=True)


def json_file_digest(raw: dict | None = None) -> str:
    """The golden's file receipt: ``sha256("scip-json:" + canonical_json(raw))``."""
    return runner.json_index_digest(raw if raw is not None else golden_raw())


_INDEX: dict[str, str] = {}


def index_digest(raw: dict | None = None) -> str:
    """The ``static-relations-v1`` identity of the exported golden (metadata ``index_digest``).

    It is a content digest of the exported relations, so it is the same for a
    permuted copy of the golden and independent of the file receipt.
    """
    if raw is not None:
        return dict(export(raw).bundle.metadata)["index_digest"]
    if "golden" not in _INDEX:
        _INDEX["golden"] = dict(export().bundle.metadata)["index_digest"]
    return _INDEX["golden"]


def ast_raw() -> dict:
    """The tree-sitter side of go_app, hand-built from the fixture source."""
    return {
        "surfaces": [{
            "id": SURFACE, "kind": "http", "method": "GET", "path": "/jobs/{id}",
            "handler": HANDLER_NODE, "handler_symbol": "GetJob",
            "file": "api/jobs.go", "line": ROUTE_LINE, "mounted": True,
        }],
        "_node_locations": {HANDLER_NODE: ["api/jobs.go", GET_JOB_LINE]},
        "excluded_surfaces": {"count": 0, "surfaces": []},
        "entities": [
            {"name": "jobs", "symbol": "Job", "module": "", "file": "models/models.go", "line": 7},
            {"name": "audit_logs", "symbol": "AuditLog", "module": "", "file": "models/models.go", "line": 13},
        ],
        "op_sites": [
            {"file": "internal/jobs/repo.go", "line": REPO_GET_OP_LINE, "entity": "jobs", "crud": "read"},
            {"file": "internal/jobs/repo.go", "line": REPO_WRITE_OP_LINE, "entity": "audit_logs", "crud": "create"},
        ],
        # Hand census: every call in the five files is a direct, statically
        # bound call; there is no interface dispatch, reflection or dynamic
        # handler table.  The empty lists assert that review, not a tool run.
        "blind_spots": [],
        "scip_residue": [],
        "unresolved": [],
    }


def export(raw: dict | None = None, *, ast: dict | None = None, file_digest: str | None = None,
           describes_runs: Iterable[str] = ()) -> scip_facts.ExportResult:
    """Export ``raw`` (default: the golden); ``file_digest`` is the run receipt.

    The receipt defaults to the raw dict's JSON digest, which is order-sensitive
    by construction (it hashes the canonical bytes of the committed file). The
    exported identity and evidence ids do not depend on it: they are the
    ``static-relations-v1`` content digest of the exported rows.
    """
    raw = raw if raw is not None else golden_raw()
    return scip_facts.export_bundle(
        normalized(raw), ast_raw=ast if ast is not None else ast_raw(), source_root=GO_APP,
        language=LANGUAGE, index_digest=file_digest if file_digest is not None else json_file_digest(raw),
        index_digest_kind=runner.INDEX_DIGEST_JSON, describes_runs=describes_runs,
    )


def tree_digest() -> str:
    return artifacts.tree_sha256(GO_APP, artifacts.patterns_for(LANGUAGE))[0]


# ---------------------------------------------------------------------------
# claim-time additions


def _fact(relation: str, decl: RelationDecl, values: dict[str, Any], evidence_id: str, source: str,
          depends_on: Iterable[str] = ()) -> tuple[Atom, Evidence]:
    atom = Atom(relation, tuple(Constant(values[column.name], column.type) for column in decl.columns))
    context = {name: values[name] for name in decl.context_indices}
    return atom, Evidence(evidence_id, atom, Context.from_mapping(context), source, tuple(depends_on))


def claim_time_id(relation: str, row: list[Any]) -> str:
    return f"static:claim-time:{relation}:{scip_facts.row_digest(relation, row)[:12]}"


def runtime_id(run: str, relation: str, row: list[Any]) -> str:
    return f"runtime:{run}:{relation}:{scip_facts.row_digest(relation, row)[:12]}"


def source_tree_fact(decls: dict[str, RelationDecl], digest_value: str) -> tuple[Atom, Evidence]:
    decl = decls["source_tree_observed"]
    return _fact("source_tree_observed", decl, {"tree_digest": digest_value},
                 claim_time_id("source_tree_observed", [digest_value]), "reviewer claim-time observation")


def runtime_route_facts(decls: dict[str, RelationDecl], surfaces: Iterable[tuple[str, str]],
                        run: str = RUN, tenant: str = TENANT) -> list[tuple[Atom, Evidence]]:
    decl = decls["runtime_route_observed"]
    out = []
    for surface, event in surfaces:
        row = [tenant, surface, event, run]
        out.append(_fact("runtime_route_observed", decl,
                         {"tenant": tenant, "surface": surface, "event": event, "run": run},
                         runtime_id(run, "runtime_route_observed", row), "capcov runtime probe",
                         [f"external:run:{run}"]))
    return out


# --- hop-count cross-check relations (go_app differential bundle only) -------

HOP_SUCC = RelationDecl("hop_succ", (Column("index", "digest", True), Column("hop", "unsigned"),
                                     Column("next", "unsigned")),
                        binding="static", context_indices=("index",))
REACHES_WITHIN = RelationDecl("static_reaches_within",
                              (Column("index", "digest", True), Column("root", "symbol"),
                               Column("dst", "symbol"), Column("hops", "unsigned")),
                              modality="derived", binding="static", primitive=False,
                              context_indices=("index",))
HOP_RULES = (
    Rule(Atom("static_reaches_within", (Variable("IX"), Variable("Root"), Variable("Dst"),
                                        Constant(1, "unsigned"))),
         (Atom("static_root", (Variable("IX"), Variable("Root"))),
          Atom("static_edge", (Variable("IX"), Variable("Root"), Variable("Dst")))),
         name="static_reaches_within_base"),
    Rule(Atom("static_reaches_within", (Variable("IX"), Variable("Root"), Variable("Dst"), Variable("H2"))),
         (Atom("static_reaches_within", (Variable("IX"), Variable("Root"), Variable("Mid"), Variable("H1"))),
          Atom("static_edge", (Variable("IX"), Variable("Mid"), Variable("Dst"))),
          Atom("hop_succ", (Variable("IX"), Variable("H1"), Variable("H2")))),
         name="static_reaches_within_step"),
)


def hop_facts(index: str, max_hops: int = MAX_HOPS) -> list[tuple[Atom, Evidence]]:
    out = []
    for hop in range(1, max_hops):
        row = [index, hop, hop + 1]
        out.append(_fact("hop_succ", HOP_SUCC, {"index": index, "hop": hop, "next": hop + 1},
                         scip_facts.evidence_id(index, "hop_succ", row), "capcov hop counter"))
    return out


# ---------------------------------------------------------------------------
# claims


def claims_for(index: str, *, runtime: bool) -> tuple[Claim, ...]:
    ix = Constant(index, "digest")
    ctx = Context.from_mapping({"index": index})
    sym = lambda s: Constant(s, "symbol")  # noqa: E731
    claims = [
        Claim("static_capability", (ix, sym(SURFACE), sym("jobs"), sym("read")), ctx, id="claim-cap-jobs-read"),
        Claim("static_capability", (ix, sym(SURFACE), sym("audit_logs"), sym("create")), ctx,
              id="claim-cap-audit-create"),
        Claim("static_reaches", (ix, sym(GET_JOB), sym(REPO_GET)), ctx, id="claim-reaches-repo-get"),
        Claim("static_reaches", (ix, sym(GET_JOB), sym(REPO_WRITE)), ctx, id="claim-reaches-repo-write"),
        Claim("static_reaches", (ix, sym(GET_JOB), sym(REGISTER)), ctx, id="claim-reaches-register"),
        Claim("static_route_authorized", (ix, Variable("surface")), ctx, quantifier="forall",
              domain="static_route_declared_surface", id="claim-all-routes-authorized"),
    ]
    if runtime:
        claims.append(Claim(
            "runtime_route_without_static",
            (sym(TENANT), sym(UNDECLARED_SURFACE), sym(RUN), ix),
            Context.from_mapping({"tenant": TENANT, "surface": UNDECLARED_SURFACE, "run": RUN, "index": index}),
            id="claim-runtime-route-gap"))
    return tuple(claims)


def go_app_bundle(raw: dict | None = None, *, ast: dict | None = None, file_digest: str | None = None,
                  runtime: bool = True, hops: bool = True,
                  with_claims: bool = True) -> tuple[Bundle, scip_facts.ExportResult]:
    """Exported golden facts + rule pack + claim-time facts (+ hop cross-check) + claims."""
    exported = export(raw, ast=ast, file_digest=file_digest, describes_runs=(RUN,) if runtime else ())
    if exported.status != scip_facts.STATUS_COMPLETE:
        raise AssertionError(f"export failed: {exported.status} {exported.messages}")
    pack = pack_bundle()
    decls = {decl.name: decl for decl in (*exported.bundle.relations, *pack.relations)}
    index = dict(exported.bundle.metadata)["index_digest"]
    additions = [source_tree_fact(decls, dict(dict(exported.bundle.metadata)["tree"])["digest"])]
    if runtime:
        additions.extend(runtime_route_facts(decls, [(SURFACE, "evt-1"), (UNDECLARED_SURFACE, "evt-2")]))
    extra_bundles = []
    if hops:
        hop_rows = hop_facts(index)
        extra_bundles.append(Bundle((HOP_SUCC, REACHES_WITHIN), facts=tuple(a for a, _ in hop_rows),
                                    rules=HOP_RULES, evidence=tuple(e for _, e in hop_rows)))
    bundle = combine(
        exported.bundle, pack, *extra_bundles,
        facts=[atom for atom, _ in additions], evidence=[record for _, record in additions],
        claims=claims_for(index, runtime=runtime) if with_claims else (),
        metadata={"experiment": "section-29 go_app differential", "surface": SURFACE},
    )
    return bundle, exported


__all__ = [name for name in dir() if not name.startswith("_")]
