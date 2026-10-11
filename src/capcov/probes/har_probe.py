"""The `har` probe: runtime bindings harvested from an existing browser run.

Some targets have no Python test suite to hook and no planner flow models yet,
but they already run browser suites -- Playwright can record every request of a
run as a HAR file. This probe reads those HARs and projects each request onto
the static surface it reached:

    request METHOD + URL path  -> surface   (matched against the static
                                            inventory's path templates)
    the same surface           -> entity    (route readers self-bind, so the
                                            entity IS the surface id)
    HTTP verb                  -> operation (GET=read, POST=create, PUT/PATCH=
                                            update, DELETE=delete -- weak but
                                            declared, the browser probe's rule)
    HAR file name              -> test      (the exercise that produced it)

Honesty: a request no static surface matches is reported as an `unresolved`
entry (`gating: False`, runtime-side, counted with samples), never dropped.
Static surfaces the run never reached are simply absent, and land in
`static_only` at reconcile -- which is the point.

What the matcher models, and what it does not:

* A path parameter -- `{x}` or `:x` -- is exactly ONE required segment. An
  optional parameter (`:id?`, `{id?}`) and a `*` catch-all are not modelled:
  such a surface only matches a request with the parameter present, so a
  request that omits it is reported unmatched and the surface reads as
  `static_only`. That is the honest limit of the reading, named here rather
  than papered over with a looser regex.
* Trailing slashes are ignored on both sides (`/jobs/` is `/jobs`); a surface
  path without a leading slash is rooted (`admin/tows` is `/admin/tows`).
* A literal path beats a template. Two templates that both match one request
  (`/x/{a}` and `/{region}/zones` for `/x/zones`) bind nothing: the request is
  reported as `ambiguous-match`, because guessing would credit one route with
  evidence that may belong to the other. Two LITERALS that normalise onto one
  key (`admin/tows` beside `/admin/tows`, `/jobs` beside `/jobs/`) are the same
  ambiguity and get the same treatment, plus a `colliding-surfaces` entry
  naming the inventory defect itself -- keeping the first would make the second
  unbindable forever and read `static_only` with no cause on record.
* Matching uses the URL path only. `[capcov] har_strip_prefixes` removes a
  deployment mount before matching, and strips only `prefix + "/"`: a prefix of
  `/v2` does not touch `/v2beta/...`.
* HEAD, OPTIONS and verbs outside the CRUD map BIND the surface they reached
  and claim no operation -- the browser probe's rule (`_crud_for_surface`
  returns `[]` for such a verb AFTER the surface is bound). The verbs are named
  under `non-binding-verbs`, which reports the missing operation, not a missing
  binding. Refusing the binding instead would leave a declared `HEAD` route in
  `static_only` however hard the run hammered it, and the gate would demand a
  test that cannot exist: the only way to exercise a HEAD route is to send
  HEAD. A non-CRUD verb that matches no surface is `unmatched-requests` like
  any other. Non-http(s) URLs (`data:`, `blob:`, `about:`) are counted per
  scheme in the `unmatched-requests` entry as `non_http`, so a `wss:` upgrade
  of the target's own socket route is legible as something other than an
  inline image. An entry with no URL or no method is `malformed-entries`.
  Nothing reaches the `/` surface by accident.

The static inventory is ``<target>/capcov.capabilities.json`` unless
``[capcov] har_surfaces`` in ``capcov.toml`` names another file. The tree hash
keeps the package's ``**/*.py`` default deliberately: ``capcov discover``
hashes the source the same way and ``reconcile`` refuses a pair whose hashes
differ, so a probe-local pattern would make every reconcile a drift refusal.

Scale: the inventory is indexed once (literals by exact key, templates
compiled once), and each distinct ``(method, path)`` is resolved once per run.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .. import artifacts
from .probe_registry import (
    ENV_NONCE,
    ENV_ONLY,
    ENV_OUT,
    ENV_SOURCE_ROOT,
    ENV_TARGET,
    FreshnessGuard,
    observed_carriers,
    source_provenance_from_env,
)

# HTTP verb -> CRUD: the SAME weak-but-declared map the browser probe uses.
# Verbs outside the set (HEAD, OPTIONS, PROPFIND, a typo) claim no operation
# rather than guess one -- but they still bind the surface they reached, which
# is the browser probe's rule at browser_probe._crud_for_surface.
VERB_TO_OP = {
    "GET": "read",
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}

# A path parameter in either template dialect: `{id}` (OpenAPI, Laravel) or
# `:id` (Express, Rails). Exactly one path segment each.
_PARAM = re.compile(r"\{[^/{}]+\}|:[A-Za-z_][A-Za-z0-9_]*")

# How many distinct requests an `unresolved` entry names verbatim. The count is
# always exact; the samples are the legible part.
SAMPLE_LIMIT = 25

# The URL schemes a request can carry and still be a route the target served.
HTTP_SCHEMES = ("http", "https")

# The index `index_surfaces` builds: exact literals keyed by (METHOD, path) --
# a LIST, because two declarations can normalise onto one key -- and the
# templated surfaces with their regex compiled once.
Index = tuple[
    dict[tuple[str, str], list[str]], list[tuple[str, "re.Pattern[str]", str]]
]


def template_regex(path: str) -> re.Pattern[str]:
    """`/jobs/{id}` and `/jobs/:id` both match exactly one non-empty segment."""
    out: list[str] = []
    last = 0
    for match in _PARAM.finditer(path):
        out.append(re.escape(path[last:match.start()]))
        out.append("[^/]+")
        last = match.end()
    out.append(re.escape(path[last:]))
    return re.compile("".join(out))


def _normalise_path(path: str) -> str:
    """Root the path and drop a trailing slash: `admin/tows/` -> `/admin/tows`."""
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/"


def index_surfaces(surfaces: list[dict]) -> Index:
    """Index the static inventory once: literals by exact key, templates compiled.

    A surface path is normalised as the matcher normalises a request path (a
    missing leading slash is added, a trailing slash dropped), so the two sides
    compare on the same footing. That normalisation is exactly what lets two
    declarations land on one key, so a key holds a LIST in declaration order:
    keeping only the first would make the rest unbindable and silently wrong.
    """
    literals: dict[tuple[str, str], list[str]] = {}
    templates: list[tuple[str, re.Pattern[str], str]] = []
    for surface in surfaces:
        method = (surface.get("method") or "").upper()
        spath = _normalise_path(surface.get("path") or "")
        if _PARAM.search(spath):
            templates.append((method, template_regex(spath), surface["id"]))
        else:
            literals.setdefault((method, spath), []).append(surface["id"])
    return literals, templates


def colliding_literals(index: Index) -> list[str]:
    """`METHOD path -> [ids]` for every literal key more than one surface claims.

    A static-inventory defect, not a runtime one: no request can ever tell the
    colliding surfaces apart, so none of them can bind. Named here so the cause
    is on record even when the run never reached the path at all.
    """
    literals, _ = index
    return [
        f"{method} {path} -> [{', '.join(ids)}]"
        for (method, path), ids in sorted(literals.items())
        if len(ids) > 1
    ]


def candidates_indexed(method: str, path: str, index: Index) -> list[str]:
    """Every surface id a request could be: one literal, or all matching templates.

    A literal hit beats every template and returns alone -- unless several
    surfaces claim that one literal key, in which case all of them are returned
    and the request reads ambiguous. Otherwise every template that matches is
    returned, in inventory order, so the caller can tell one hit from an
    ambiguity instead of silently taking the first.
    """
    literals, templates = index
    method = method.upper()
    path = _normalise_path(path)
    literal = literals.get((method, path))
    if literal:
        return list(literal)
    return [
        sid
        for template_method, pattern, sid in templates
        if template_method == method and pattern.fullmatch(path)
    ]


def _request_path(path: str, strip_prefixes: list[str]) -> str:
    """A request's URL path with a configured mount prefix removed.

    Strips only `prefix + "/"`, so `/v2` removes the mount from `/v2/items` and
    leaves `/v2beta/items` alone. The first prefix that applies wins.
    """
    path = path or "/"
    for prefix in strip_prefixes:
        prefix = prefix.rstrip("/")
        if prefix and path.startswith(prefix + "/"):
            path = path[len(prefix):]
            break
    return _normalise_path(path)


def _labels(hars: dict[str, dict]) -> dict[str, str]:
    """Given path -> the basename used as the `tests` label; collisions refused.

    `ci/smoke.har` and `local/smoke.har` are two different runs. Labelling both
    `smoke.har` would merge their evidence under one name, so the collision is
    an error naming both paths, not a quiet fold.
    """
    by_label: dict[str, list[str]] = {}
    for given in hars:
        by_label.setdefault(Path(given).name, []).append(given)
    clashes = {label: paths for label, paths in by_label.items() if len(paths) > 1}
    if clashes:
        detail = "; ".join(
            f"{label!r} from {', '.join(paths)}" for label, paths in sorted(clashes.items())
        )
        raise ValueError(
            f"HAR files share a basename and would be merged under one "
            f"test label: {detail}"
        )
    return {given: Path(given).name for given in hars}


def _sample(method: str, host: str, path: str) -> str:
    """`METHOD host/path`, host empty when the URL had none (a relative URL)."""
    return f"{method} {host}{path}"


def _unresolved_entry(kind: str, samples: list[str], reason: str, **extra: object) -> dict:
    distinct = sorted(set(samples))
    return {
        "adapter": "har",
        "kind": kind,
        "gating": False,
        "count": len(distinct),
        "samples": distinct[:SAMPLE_LIMIT],
        "reason": reason,
        **extra,
    }


def project_har(
    hars: dict[str, dict], surfaces: list[dict], strip_prefixes: list[str]
) -> dict:
    """`{given_path: har_document}` -> `{bindings, unresolved, requests}`.

    One binding per surface reached, its ``tests`` the HAR basenames that
    reached it. A surface is bound by the request that reached it whatever the
    verb; the verb only decides whether an OPERATION is claimed. Every entry is
    counted in ``requests``; what did not bind is grouped into non-gating
    ``unresolved`` entries by cause: matched no surface (with non-http URLs
    counted per scheme alongside), matched several, or an entry with no method
    or URL. A verb outside the CRUD map is named too, as the missing operation
    it is, and so is a literal key several surfaces claim.
    """
    labels = _labels(hars)
    index = index_surfaces(surfaces)
    memo: dict[tuple[str, str], list[str]] = {}
    rows: dict[str, dict] = {}
    unmatched: list[str] = []
    ambiguous: list[str] = []
    non_binding: list[str] = []
    malformed: list[str] = []
    non_http: dict[str, int] = {}
    requests = 0
    for given in sorted(hars):
        label = labels[given]
        for position, entry in enumerate(hars[given].get("log", {}).get("entries", [])):
            requests += 1
            request = entry.get("request") or {}
            method = str(request.get("method") or "").upper()
            url = str(request.get("url") or "")
            if not method or not url:
                malformed.append(f"{label}#{position}")
                continue
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            if scheme and scheme not in HTTP_SCHEMES:
                non_http[scheme] = non_http.get(scheme, 0) + 1
                continue
            host = parts.netloc
            path = _request_path(parts.path, strip_prefixes)
            key = (method, path)
            if key not in memo:
                memo[key] = candidates_indexed(method, path, index)
            candidates = memo[key]
            if not candidates:
                unmatched.append(_sample(method, host, path))
                continue
            if len(candidates) > 1:
                ambiguous.append(
                    f"{_sample(method, host, path)} -> [{', '.join(candidates)}]"
                )
                continue
            # The verb is decided BELOW the match, so the surface binds either
            # way: a HEAD route a run actually hammered lands in `both` with no
            # operation claimed, instead of `static_only` with a test demanded
            # that no test could ever satisfy.
            row = rows.setdefault(candidates[0], {"operations": set(), "tests": set()})
            op = VERB_TO_OP.get(method)
            if op:
                row["operations"].add(op)
            else:
                non_binding.append(_sample(method, host, path))
            row["tests"].add(label)

    bindings = [
        {
            "surface": sid,
            "entity": sid,
            "operations": sorted(row["operations"]),
            "tests": sorted(row["tests"]),
        }
        for sid, row in sorted(rows.items())
    ]
    unresolved = []
    if unmatched or non_http:
        unresolved.append(
            _unresolved_entry(
                "unmatched-requests",
                unmatched,
                "requests in the run matched no static surface (assets, third-party, "
                "or routes discovery missed); non_http counts non-http(s) URLs "
                "(data:, blob:, about:, wss:) per scheme",
                non_http=dict(sorted(non_http.items())),
            )
        )
    if ambiguous:
        unresolved.append(
            _unresolved_entry(
                "ambiguous-match",
                ambiguous,
                "more than one surface matched -- several templates, or literal "
                "paths that normalise onto one key; nothing bound rather than "
                "credit the wrong route",
            )
        )
    collisions = colliding_literals(index)
    if collisions:
        unresolved.append(
            _unresolved_entry(
                "colliding-surfaces",
                collisions,
                "two or more static surfaces normalise onto one METHOD + path, so "
                "no request can tell them apart and none of them can bind; fix the "
                "inventory (a leading or trailing slash) rather than the run",
            )
        )
    if non_binding:
        unresolved.append(
            _unresolved_entry(
                "non-binding-verbs",
                non_binding,
                "HEAD, OPTIONS and verbs outside the CRUD map bound the surface "
                "they reached with NO operation claimed; the request is evidence "
                "the route ran, not evidence of what it did",
            )
        )
    if malformed:
        unresolved.append(
            _unresolved_entry(
                "malformed-entries",
                malformed,
                "HAR entries with no request method or URL (named as file#index)",
            )
        )
    return {"bindings": bindings, "unresolved": unresolved, "requests": requests}


def _capcov_block(target: Path) -> dict:
    """The ``[capcov]`` config block, or ``{}`` when there is no capcov.toml."""
    config = target / "capcov.toml"
    if not config.exists():
        return {}
    import tomllib

    return tomllib.loads(config.read_text()).get("capcov", {})


def _surfaces_path(target: Path) -> Path:
    override = _capcov_block(target).get("har_surfaces")
    return target / override if override else target / "capcov.capabilities.json"


def _strip_prefixes(target: Path) -> list[str]:
    return [str(p) for p in _capcov_block(target).get("har_strip_prefixes", [])]


def observe(
    *,
    source_root: Path | str,
    out: Path | str,
    target: Path | str,
    har_paths: list[Path | str],
    nonce: str | None = None,
    only: str | None = None,
    source_patterns: tuple[str, ...] = ("**/*.py",),
    source_snapshot=None,
    source_provenance: dict | None = None,
) -> dict:
    """Project the named HAR files and write the ``observed`` artifact.

    Mirrors the load probe under the shared freshness guard: unlink the stale
    output, snapshot the tree, build nonce-stamped evidence, verify it, then
    write. The HARs are the exercise here (already run), so the evidence is the
    projection itself; the guard still refuses a tree that changed while it was
    being read. ``only`` is accepted for the env contract and unused: a HAR is
    one recorded run, there is no inner loop to scope.
    """
    phase_started = time.perf_counter_ns()
    source = Path(source_root)
    out_path = Path(out)
    target_dir = Path(target)
    surfaces_file = _surfaces_path(target_dir)
    if not surfaces_file.exists():
        raise ValueError(
            f"no static inventory at {surfaces_file}; run `capcov discover` "
            "first or set [capcov] har_surfaces in capcov.toml"
        )
    surfaces = json.loads(surfaces_file.read_text()).get("surfaces", [])
    if not har_paths:
        raise ValueError("name at least one .har file after `--`")
    given = [str(p) for p in har_paths]
    repeated = sorted({p for p in given if given.count(p) > 1})
    if repeated:
        # The dict below would fold the repeat and under-report `exercises`,
        # the quiet opposite of the loud basename-clash rule in `_labels`.
        raise ValueError(
            "the same HAR path was named more than once and would be counted "
            f"as one exercise: {', '.join(repeated)}"
        )
    hars = {p: json.loads(Path(p).read_text()) for p in given}

    guard = FreshnessGuard(
        out_path, source, patterns=source_patterns, snapshot=source_snapshot
    )
    run_nonce = guard.begin(nonce)
    result = project_har(hars, surfaces, _strip_prefixes(target_dir))
    guard.verify({"nonce": run_nonce, **result})

    body = {
        "bindings": result["bindings"],
        "exercises": len(hars),
        "requests": result["requests"],
        **observed_carriers(excluded_surfaces=[], unresolved=result["unresolved"]),
    }
    body["timing"] = {
        "observe_ms": min(
            max(0, (time.perf_counter_ns() - phase_started) // 1_000_000),
            86_400_000,
        ),
        "source_verification": guard.verification,
    }
    if source_provenance is not None:
        derived_from = {**source_provenance, "extractor": "capcov har-probe"}
    else:
        derived_from = guard.snapshot.provenance(
            os.path.basename(str(source)), "capcov har-probe"
        )
    artifacts.write(
        out_path,
        "observed",
        derived_from,
        body,
    )
    return artifacts.read(out_path, "observed")


def main(argv: list[str] | None = None) -> int:
    """Entry the probe registry points at: project the HARs named in ``argv``.

    Reads the unified observe env contract (``CAPCOV_OUT`` / ``CAPCOV_SOURCE_ROOT``
    / ``CAPCOV_TARGET`` / ``CAPCOV_NONCE`` / ``CAPCOV_ONLY``). ``argv`` is what
    follows ``--`` on ``capcov observe --probe har -- a.har b.har``. Returns 0 on
    a fresh, valid observation.
    """
    har_paths = [Path(a) for a in (argv or [])]
    out = os.environ.get(ENV_OUT)
    source_root = os.environ.get(ENV_SOURCE_ROOT)
    target = os.environ.get(ENV_TARGET)
    if not out or not source_root or not target:
        print(
            f"capcov har-probe: {ENV_OUT}, {ENV_SOURCE_ROOT} and {ENV_TARGET} must be set",
            file=sys.stderr,
        )
        return 2
    if not har_paths:
        print("capcov har-probe: name at least one .har file after --", file=sys.stderr)
        return 2
    try:
        source_snapshot, source_provenance = source_provenance_from_env(source_root)
        observe(
            source_root=source_root,
            out=out,
            target=target,
            har_paths=har_paths,
            nonce=os.environ.get(ENV_NONCE),
            only=os.environ.get(ENV_ONLY),
            source_patterns=(
                source_snapshot.patterns
                if source_snapshot is not None
                else ("**/*.py",)
            ),
            source_snapshot=source_snapshot,
            source_provenance=source_provenance,
        )
    except (ValueError, OSError) as error:
        print(f"capcov har-probe: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
