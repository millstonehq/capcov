"""capcov command line: discover, observe, reconcile, gate, report.

`discover` and `observe` both always run. There is no static-only mode and no
threshold that switches between them -- that was a rejected design, and what it
amounted to was a tool that sometimes does not work with the answer bolted on
beside it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import artifacts
from .adapters import load as load_adapter
from .adapters import merge as merge_adapters
from .adapters import source_patterns as _adapter_source_patterns
from .core import fixpoint
from .core import gate as gate_mod
from .core import reconcile as reconcile_mod
from .probes import probe_registry


def _resolve(
    target: Path, source: str | None, adapter: str | None
) -> tuple[Path, list[tuple[str, dict | None]]]:
    """Resolve the source root and the adapter specs to run.

    An adapter spec is ``(name, config)``: ``config`` is the ``[[adapters]]`` entry
    the promoted route/contract adapters read, or ``None`` when the adapter reads
    its own ``capcov.toml`` (the stack adapter, and the ``--adapter`` override).

    Resolution (design §1.1). ``adapter`` (a single string) and ``adapters`` (a
    list) are XOR:

    * ``--adapter X`` overrides everything -> one adapter X, self-reading its
      config (``config=None``) -- today's override, unchanged.
    * ``[capcov] adapter`` -> one adapter, self-reading -- byte-identical to the
      single-adapter world: one adapter, one (merge-of-one) dict.
    * ``[[adapters]]`` -> run each with its entry as config, and merge them.
    * both ``[capcov] adapter`` and ``[[adapters]]`` set -> refused (XOR).
    """
    data: dict = {}
    config_path = target / "capcov.toml"
    if config_path.exists():
        import tomllib

        data = tomllib.loads(config_path.read_text())
    capcov = data.get("capcov", {})
    adapters_list = data.get("adapters", [])
    source_dir = target / (source or capcov.get("source", "src"))

    specs: list[tuple[str, dict | None]]
    if adapter:
        specs = [(adapter, None)]
    elif capcov.get("adapter") and adapters_list:
        raise SystemExit(
            f"both [capcov] adapter and [[adapters]] are set in {config_path}; "
            "use one -- a single adapter, or the adapters list (they are XOR)"
        )
    elif capcov.get("adapter"):
        specs = [(capcov["adapter"], None)]
    elif adapters_list:
        specs = []
        for entry in adapters_list:
            name = entry.get("name")
            if not name:
                raise SystemExit(f"an [[adapters]] entry has no name in {config_path}")
            specs.append((name, entry))
    else:
        raise SystemExit(
            f"no adapter: pass --adapter or set [capcov] adapter or [[adapters]] "
            f"in {config_path}"
        )
    if not source_dir.is_dir():
        raise SystemExit(f"source root {source_dir} does not exist")
    return source_dir, specs


def _capcov_block(target: Path) -> dict:
    """The ``[capcov]`` config block, or ``{}`` when there is no capcov.toml."""
    config_path = target / "capcov.toml"
    if not config_path.exists():
        return {}
    import tomllib

    return tomllib.loads(config_path.read_text()).get("capcov", {})


def _run_adapter(
    adapter, source_dir: Path, target: Path, name_match: bool, config: dict | None
) -> dict:
    """Run one adapter, forwarding a per-``[[adapters]]`` config when it takes one.

    The stack adapter (``python-fastapi-sqlalchemy``) reads its own config from
    capcov.toml and has no ``config`` parameter; the promoted route/contract
    adapters accept the ``[[adapters]]`` entry directly. A lone ``adapter`` string
    passes ``config=None``, so the stack-adapter call is byte-identical to today.
    """
    import inspect

    params = inspect.signature(adapter.discover).parameters
    if config is not None and "config" in params:
        return adapter.discover(source_dir, target, name_match=name_match, config=config)
    return adapter.discover(source_dir, target, name_match=name_match)


def _source_patterns(specs: list[tuple[str, dict | None]]) -> tuple[str, ...]:
    """Use the configured adapter inputs for source-bound artifact provenance.

    The rule itself lives in `capcov.adapters` -- the one place that is allowed to
    know what an adapter reads -- because the browser probe resolves the same set
    and `reconcile` refuses two artifacts hashed over different trees.
    """
    try:
        return _adapter_source_patterns(specs)
    except ValueError as error:
        # A config the adapter layer refuses (an unknown language with no
        # declared globs) is a user error, not a crash.
        raise SystemExit(f"capcov: {error}") from None


def _emit(path: Path, doc: dict, check: bool) -> int:
    """Write, or in --check mode re-render and diff against what is on disk."""
    if not check:
        artifacts.write(path, doc.pop("kind"), doc.pop("derived_from"), doc)
        return 0
    if not path.exists():
        print(f"capcov: {path} does not exist; run without --check to create it")
        return 1
    on_disk = json.loads(path.read_text())
    fresh = {"schema_version": artifacts.SCHEMA_VERSION, **doc}
    if artifacts.normalise(on_disk) == artifacts.normalise(fresh):
        return 0
    print(
        f"capcov: {path} is stale. What this system can do has changed and the "
        "committed capability set has not. Re-run without --check and read the "
        "diff -- it is the list of capabilities this change adds or removes."
    )
    _diff(artifacts.normalise(on_disk), artifacts.normalise(fresh), str(path))
    return 1


def _diff(old: str, new: str, label: str) -> None:
    import difflib

    for line in list(
        difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), f"{label} (on disk)",
            f"{label} (re-derived)",
        )
    )[:80]:
        sys.stdout.write(line)


def _bounded_ms(value: object) -> int:
    return value if type(value) is int and 0 <= value <= 86_400_000 else 0


# --------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    phase_started = time.perf_counter_ns()
    target = Path(args.target).resolve()
    source_dir, specs = _resolve(target, args.source, args.adapter)
    # `plugin` (present only in an [[adapters]] entry that brings a bespoke reader)
    # routes `load` to import that callable instead of a registered module. Absent
    # from every existing config, so `plugin=None` and this stays today's load.
    adapters = [load_adapter(name, plugin=(cfg or {}).get("plugin")) for name, cfg in specs]
    name_match = not args.no_name_match

    # Run every adapter and MERGE their core dicts (design §1.1/§1.2). A lone
    # adapter merges to itself -- byte-identical to today. A duplicate obligation
    # id across adapters is the collision the four-cell cannot represent, so the
    # merge refuses it loudly rather than letting one silently clobber the other
    # (R6); it never fires for a single-adapter list.
    discoveries = [
        _run_adapter(adapter, source_dir, target, name_match, config)
        for adapter, (_, config) in zip(adapters, specs)
    ]
    try:
        raw = merge_adapters(discoveries)
    except ValueError as exc:
        raise SystemExit(f"capcov discover: {exc}")

    # A deep adapter block emits a node-keyed call-graph dict (carrying
    # `_node_locations` for the (file,line)-join) that ONLY the SCIP resolver can
    # bind; without --resolver scip its `_calls` are unresolved placeholders and
    # the fixpoint would bind nothing. Deep is opt-in and the resolver stays an
    # explicit flag, so a deep block reached here without it is a misconfiguration
    # named loudly, not a silently empty capability set.
    if getattr(args, "resolver", "ast") != "scip" and "_node_locations" in raw:
        raise SystemExit(
            "capcov discover: a deep adapter block emits a call graph only "
            "--resolver scip can bind; re-run with --resolver scip"
        )

    # Optional hybrid: rent SCIP as the resolver for the call graph, keep the AST
    # pass for entities, surfaces and the enumerated blind spots.
    # The AST adapter always runs first -- it is the fallback and the enumerator;
    # SCIP only re-sources `_calls` and adds its resolved-edges + residue. It
    # re-sources ONE adapter's graph, so it is undefined across an adapters merge.
    if getattr(args, "resolver", "ast") == "scip":
        if len(adapters) != 1:
            raise SystemExit(
                "capcov discover --resolver scip: the SCIP resolver re-sources one "
                "adapter's call graph; it is not defined across an [[adapters]] merge"
            )
        from .scip import resolve as scip_resolve

        # The LANGUAGE seam (design §1.4): a promoted adapter's SCIP language is
        # per-config (go/php/python vary per [[adapters]] entry), not a module
        # constant, so read the entry's `scip_language` first (from the CLI's spec,
        # or -- for a --adapter override with no spec config -- the deep dict the
        # adapter tagged), then fall back to the adapter module's LANGUAGE.
        spec_config = specs[0][1] or {}
        language = (
            spec_config.get("scip_language")
            or raw.get("scip_language")
            or getattr(adapters[0], "LANGUAGE", "python")
        )
        # `deep` is what the adapter actually emitted: a node-keyed dict carries
        # `_node_locations`. A deep block whose SCIP tooling was absent degraded to
        # the shallow dict inside the adapter and NAMED the reason
        # (`deep-unavailable`); honor that by not shelling out to the very tool the
        # adapter already reported missing.
        deep = "_node_locations" in raw
        degraded = any(
            u.get("kind") == "deep-unavailable" for u in raw.get("unresolved", [])
        )
        if not degraded:
            try:
                raw = scip_resolve.resolve(
                    source_dir, raw, language=language, deep=deep
                )
            except scip_resolve.ScipToolsUnavailable as exc:
                raise SystemExit(f"capcov discover --resolver scip: {exc}")
    direct, calls, ops = raw["_direct"], raw["_calls"], raw["_ops"]

    roots = [s["handler"] for s in raw["surfaces"]]
    known = set(direct) | set(calls)
    missing_roots = sorted(r for r in roots if r not in known)
    per_root, history = fixpoint.bind(roots, calls, direct)

    capabilities = []
    for surface in raw["surfaces"]:
        bound = per_root.get(surface["handler"], {})
        reach = fixpoint.distances(surface["handler"], calls)
        for entity, hops in sorted(bound.items()):
            observed_ops = set()
            for node in reach:
                observed_ops |= ops.get(node, {}).get(entity, set())
            path = fixpoint.chain(surface["handler"], entity, calls, direct)
            capabilities.append(
                {
                    "entity": entity,
                    "surface": surface["id"],
                    "operations": sorted(observed_ops),
                    "evidence": {
                        "hops": hops,
                        "chain": path,
                        "kind": "direct" if hops == 0 else f"call-chain:{hops}",
                    },
                }
            )
    capabilities.sort(key=lambda c: (c["entity"], c["surface"]))

    patterns = _source_patterns(specs)
    # The fast path.  A cache hit makes this digest PROVISIONAL, and the
    # artifact says so (`source_snapshot.verification`).  It cannot reach the
    # gate on its own: reconcile requires an EXACT observed digest over the
    # same tree to agree with it, which is what makes a cached discover
    # digest authoritative -- or refuses it.
    snapshot = artifacts.snapshot_tree(source_dir, patterns)
    blind = [b for b in raw["blind_spots"] if b["blind"]]
    extractor = "capcov " + (
        adapters[0].NAME
        if len(adapters) == 1
        else "+".join(a.NAME for a in adapters)
    )
    doc = {
        "kind": "capabilities",
        "derived_from": artifacts.provenance(
            str(source_dir.relative_to(target)),
            snapshot.digest,
            extractor,
            snapshot.files,
            patterns,
            snapshot=snapshot,
        ),
        "timing": {
            "discover_ms": min(
                max(0, (time.perf_counter_ns() - phase_started) // 1_000_000),
                86_400_000,
            ),
            "source_verification": snapshot.verification,
        },
        "entities": raw["entities"],
        "surfaces": raw["surfaces"],
        "capabilities": capabilities,
        "binding_history": history,
        "blind_spots": blind,
        "resolved_dynamic_access": [
            b for b in raw["blind_spots"] if not b["blind"]
        ],
        "residue": raw["residue"],
        "residue_summary": raw["residue_summary"],
        "unbound_entry_points": missing_roots,
    }
    # When SCIP resolved the call graph, carry both halves of the hybrid into the
    # artifact: the edges SCIP resolved AND the residue it stayed silent about,
    # so coverage never reports a bare number -- resolved-by-SCIP plus
    # unresolved-enumerated, each named with a file and a line.
    if raw.get("resolver") == "scip":
        doc["resolver"] = "scip"
        doc["scip_resolved_edges"] = raw["scip_resolved_edges"]
        doc["scip_entities"] = raw["scip_entities"]
        doc["scip_residue"] = raw["scip_residue"]
        doc["scip_residue_summary"] = raw["scip_residue_summary"]
    # Honest-denominator carriers (Property 3): the promoted route/contract
    # adapters surface what static reading SAW-but-filtered (`excluded_surfaces`,
    # verb-allowlist drops) and could-not-resolve (`unresolved`: dynamic paths,
    # boundary limits, adapters that found nothing). Written conditionally --
    # exactly the scip_* pattern -- so the python-fastapi-sqlalchemy path, which
    # emits neither, is byte-identical to before (backward compatible), while a
    # route/contract discovery never drops its narrowing or its limits silently.
    if "excluded_surfaces" in raw:
        doc["excluded_surfaces"] = raw["excluded_surfaces"]
    if "unresolved" in raw:
        doc["unresolved"] = raw["unresolved"]
    rc = _emit(Path(args.out), dict(doc), args.check)
    if not args.quiet:
        bound_entities = {c["entity"] for c in capabilities}
        print(
            f"capcov discover: {len(raw['entities'])} entities, "
            f"{len(raw['surfaces'])} surfaces, {len(bound_entities)} entities bound, "
            f"history {' -> '.join(map(str, history))}, "
            f"{len(blind)} blind spots"
        )
        res = raw["residue_summary"]
        print(
            f"capcov discover: call edges {res['resolved_by_import']} by import, "
            f"{res['resolved_by_name']} by name; residue {res['ambiguous']} ambiguous "
            f"({res['external']} external, {res['chained']} chained, "
            f"{res['builtin_shadowed']} builtin-shadowed; none of these are residue)"
        )
        if missing_roots:
            print(
                "capcov discover: entry points with no analysed body: "
                + ", ".join(missing_roots)
            )
        if raw.get("resolver") == "scip":
            rs = raw["scip_residue_summary"]
            print(
                f"capcov discover: resolver scip -- {rs['scip_resolved_edges']} "
                f"edges resolved ({rs['scip_rooted_edges']} rooted in-project); "
                f"{rs['unresolved_enumerated']} of {rs['ast_call_sites']} call "
                "sites enumerated as unresolved (named, not dropped)"
            )
    return rc


def cmd_observe(args: argparse.Namespace) -> int:
    """Run the target's exercises with the selected probe installed.

    The probe writes observed.json itself. capcov sets one unified env contract
    (the ``CAPCOV_*`` variables of design §1.3, including a per-run
    ``CAPCOV_NONCE``) and hands off. The default ``pytest`` probe is
    command-driven -- capcov runs the command the target already runs and the
    pytest11 plugin does the rest, byte-identical to the pre-consolidation
    observe. The other probes (``browser``, ``load``) read the same env and drive
    their own exercise in-process, writing observed.json under the strong
    freshness guard.
    """
    phase_started = time.perf_counter_ns()
    target = Path(args.target).resolve()
    source_dir, specs = _resolve(target, args.source, args.adapter)
    probe_name = args.probe or _capcov_block(target).get("probe") or "pytest"
    probe_registry.resolve(probe_name)  # validate the name; unknown probe raises.

    out = Path(args.out)
    patterns = _source_patterns(specs)
    snapshot = artifacts.snapshot_tree(source_dir, patterns)
    # Handed to the probe as PROVISIONAL whatever this capture believed: the
    # artifact is published after the exercise, and only this process's exact
    # walk afterwards (`_publish_verified`) may stamp it exact.
    carried = artifacts.mark_provisional(
        snapshot.provenance(str(source_dir.relative_to(target)), "capcov source-snapshot")
    )
    # A successful command must publish evidence from this invocation.  The
    # command-driven pytest path cannot rely on a probe hook being installed to
    # remove yesterday's output.
    out.unlink(missing_ok=True)
    observe_env = {
        probe_registry.ENV_OBSERVE: "1",
        probe_registry.ENV_OUT: str(out.resolve()),
        probe_registry.ENV_SOURCE_ROOT: str(source_dir),
        probe_registry.ENV_TARGET: str(target),
        probe_registry.ENV_NONCE: uuid.uuid4().hex,
        probe_registry.ENV_SOURCE_PROVENANCE: json.dumps(
            carried, sort_keys=True, separators=(",", ":")
        ),
    }
    only = getattr(args, "only", None)
    if only:
        observe_env[probe_registry.ENV_ONLY] = only

    if probe_name == "pytest":
        if not args.command:
            raise SystemExit("capcov observe: give the command after --, e.g. -- pytest -q")
        env = {**os.environ, **observe_env}
        print(f"capcov observe: {' '.join(args.command)}")
        proc = subprocess.run(args.command, cwd=target, env=env)
        if proc.returncode != 0:
            print(
                f"capcov observe: the exercise failed (exit {proc.returncode}). "
                "Observation from a failing run is not evidence of anything; fix the "
                "run first."
            )
            return proc.returncode
        if not out.exists():
            print(
                f"capcov observe: the command succeeded and wrote no {args.out}. "
                "The probe did not load -- check that capcov is installed in the "
                "same environment as the exercise."
            )
            return 1
        return _publish_verified(snapshot, out, phase_started)

    # In-process probes (browser, load). They read the unified env contract and
    # drive their own exercise; the env is set for the duration of the call and
    # restored after, so a probe run leaves the caller's environment untouched.
    probe = probe_registry.load(probe_name)
    saved = {key: os.environ.get(key) for key in {*observe_env, probe_registry.ENV_ONLY}}
    try:
        os.environ.pop(probe_registry.ENV_ONLY, None)
        os.environ.update(observe_env)
        rc = probe.main(list(args.command or []))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if rc == 0 and not out.exists():
        print(
            f"capcov observe: the {probe_name} probe returned success but wrote no "
            f"{args.out}."
        )
        return 1
    if rc == 0:
        return _publish_verified(snapshot, out, phase_started)
    return rc


def _publish_verified(snapshot, out: Path, phase_started: int) -> int:
    """The publication boundary: one exact walk, then stamp or discard.

    The probe wrote a PROVISIONAL artifact under a carried identity and walked
    nothing.  This is the run's single byte-level verification, done in the
    process the exercise could not reach, and its result is carried outward
    onto the artifact.  A failed verification removes the artifact rather than
    leaving one whose `artifact_sha256` names a tree that is not there.
    """
    try:
        verified = snapshot.verify()
    except (ValueError, OSError) as error:
        out.unlink(missing_ok=True)
        print(
            f"capcov observe: source changed during the exercise: {error}; "
            f"discarded {out}"
        )
        return 1
    try:
        document = json.loads(out.read_text())
    except (OSError, json.JSONDecodeError):
        out.unlink(missing_ok=True)
        print(f"capcov observe: the probe wrote an unreadable {out}; discarded it")
        return 1
    if not isinstance(document, dict):
        out.unlink(missing_ok=True)
        print(f"capcov observe: the probe wrote a malformed {out}; discarded it")
        return 1
    derived_from = document.get("derived_from")
    if derived_from is not None:
        # A probe that publishes no provenance makes no claim to stamp (and
        # reconcile refuses it downstream).  One that publishes a DIFFERENT
        # identity than it was handed is not describing this run.
        try:
            if not isinstance(derived_from, dict):
                raise ValueError("derived_from is not an object")
            document["derived_from"] = artifacts.mark_exact(derived_from, verified)
        except ValueError as error:
            out.unlink(missing_ok=True)
            print(f"capcov observe: {error}; discarded {out}")
            return 1
    existing_timing = document.get("timing")
    if not isinstance(existing_timing, dict):
        existing_timing = {}
    document["timing"] = {
        **existing_timing,
        "observe_ms": min(
            max(0, (time.perf_counter_ns() - phase_started) // 1_000_000),
            86_400_000,
        ),
        "source_verification": verified.verification,
    }
    artifacts.write_document(out, document)
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    phase_started = time.perf_counter_ns()
    capabilities = artifacts.read(Path(args.capabilities), "capabilities")
    observed = artifacts.read(Path(args.observed), "observed")
    same, why = artifacts.same_artifact(capabilities, observed)
    if not same and not args.allow_drift:
        raise SystemExit(f"capcov reconcile: {why}")
    # The join point where a provisional digest becomes evidence or does not.
    # The observed side must be exact -- read from bytes at its publication
    # boundary.  The static side may be a cached discover; agreeing with an
    # exact digest over the same tree is what confirms it.  `--allow-drift`
    # relaxes WHICH tree, never whether the tree was read.
    try:
        runtime_verification = artifacts.snapshot_verification_of(observed["derived_from"])
    except ValueError as error:
        raise SystemExit(f"capcov reconcile: observed artifact: {error}")
    if runtime_verification != "exact":
        raise SystemExit(
            "capcov reconcile: the observed artifact is provisional (its source "
            "digest was taken from the cache, not from bytes). Produce it through "
            "`capcov observe`, which verifies the source exactly before publishing."
        )

    result = reconcile_mod.reconcile(capabilities, observed)
    reconcile_ms = min(
        max(0, (time.perf_counter_ns() - phase_started) // 1_000_000), 86_400_000
    )
    capabilities_timing = capabilities.get("timing", {})
    observed_timing = observed.get("timing", {})
    discover_ms = _bounded_ms(
        capabilities_timing.get("discover_ms")
        if isinstance(capabilities_timing, dict)
        else None
    )
    observe_ms = _bounded_ms(
        observed_timing.get("observe_ms")
        if isinstance(observed_timing, dict)
        else None
    )
    derived_from = dict(capabilities["derived_from"])
    if same and "source_snapshot" in observed["derived_from"]:
        # The static digest is the exact observed one (they agree), so the
        # coverage artifact carries the confirmed identity, not a provisional
        # one.  Under --allow-drift the trees differ and nothing is confirmed.
        derived_from["source_snapshot"] = observed["derived_from"]["source_snapshot"]
    doc = {
        "kind": "coverage",
        "derived_from": {
            **derived_from,
            "extractor": "capcov reconcile",
            "static_from": capabilities["derived_from"]["extractor"],
            "runtime_from": observed["derived_from"]["extractor"],
        },
        **result,
        "blind_spots": capabilities.get("blind_spots", []),
        "timing": {
            "discover_ms": discover_ms,
            "observe_ms": observe_ms,
            "reconcile_ms": reconcile_ms,
            "total_ms": min(discover_ms + observe_ms + reconcile_ms, 86_400_000),
        },
    }
    # Carry the hybrid's SCIP evidence through the reconcile so coverage reports
    # resolved-by-SCIP AND unresolved-enumerated -- both survive to the gate and
    # the report, and neither is collapsed into a single coverage figure. Absent
    # for an AST-resolved run, so this is backward compatible.
    for key in ("resolver", "scip_resolved_edges", "scip_residue", "scip_residue_summary"):
        if key in capabilities:
            doc[key] = capabilities[key]
    rc = _emit(Path(args.out), dict(doc), args.check)
    if not args.quiet:
        s = result["summary"]
        print(
            "capcov reconcile: "
            + ", ".join(f"{k} {s[k]}" for k in reconcile_mod.CELLS)
        )
        rs = capabilities.get("scip_residue_summary")
        if rs is not None:
            print(
                f"capcov reconcile: scip resolver -- {rs['scip_resolved_edges']} "
                f"edges resolved, {rs['unresolved_enumerated']} call sites "
                "unresolved-enumerated (each named in scip_residue)"
            )
    return rc


def cmd_gate(args: argparse.Namespace) -> int:
    coverage = artifacts.read(Path(args.coverage), "coverage")
    exemptions = Path(args.exemptions) if args.exemptions else None
    failures = gate_mod.gate(coverage, exemptions)
    if not failures:
        s = coverage["summary"]
        print(
            f"capcov gate: PASS -- {s['both']} capabilities covered, "
            f"{sum(s[c] for c in reconcile_mod.FAILING_CELLS)} exempted with reasons"
        )
        return 0
    print(f"capcov gate: FAIL -- {len(failures)} unexplained\n")
    for failure in failures:
        print(f"  {failure}\n")
    where = args.exemptions or "an exemptions file (--exemptions)"
    print(
        "Each of these is a question with an answer: write the test, fix the "
        f"adapter, delete the dead entity, or record why in {where}."
    )
    return 1


def cmd_report(args: argparse.Namespace) -> int:
    coverage = artifacts.read(Path(args.coverage), "coverage")
    width = max(len(r["entity"]) for r in coverage["rows"])
    print(f"{'entity'.ljust(width)}  cell          tests  surfaces  operations")
    for row in coverage["rows"]:
        print(
            f"{row['entity'].ljust(width)}  {row['cell'].ljust(12)}  "
            f"{len(row['tests']):>5}  "
            f"{len(row['static_surfaces'] or row['runtime_surfaces']):>8}  "
            f"{','.join(row['runtime_operations'] or row['static_operations'])}"
        )
    s = coverage["summary"]
    print("\n" + ", ".join(f"{k}: {s[k]}" for k in reconcile_mod.CELLS))
    unexercised = coverage.get("unexercised_surfaces", [])
    if unexercised:
        print(f"\nsurfaces no exercise reached ({len(unexercised)}):")
        for surface in unexercised:
            print(f"  {surface}")
    if coverage["blind_spots"]:
        print(f"\nstatic blind spots ({len(coverage['blind_spots'])}):")
        for spot in coverage["blind_spots"]:
            print(f"  {spot['file']}:{spot['line']}  {spot['expr']}  ({spot['kind']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    actual = sys.argv[1:] if argv is None else argv
    if actual and actual[0] == "outcomes":
        from .outcomes import main as outcomes_main

        return outcomes_main(actual[1:])
    if actual and actual[0] == "flows":
        from .flows.cli import main as flows_main

        return flows_main(actual[1:])
    if actual and actual[0] == "features":
        from .features.cli import main as features_main

        return features_main(actual[1:])
    if actual and actual[0] == "experiment":
        # Experimental namespace (EXPERIMENT-PLAN section 18); production
        # commands and their argparse below are untouched.
        from .claims.cli import main as experiment_main

        return experiment_main(actual[1:])
    if actual and actual[0] == "faults":
        # Test support, loaded on demand: discover / reconcile / gate never import it.
        from .faults.cli import main as faults_main

        return faults_main(actual[1:])
    if actual and actual[0] == "capabilities":
        from .capabilities.cli import main as capabilities_main

        return capabilities_main(actual[1:])
    parser = argparse.ArgumentParser(prog="capcov", description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("flows", help="source obligations, flow planning, and browser outcome evidence")
    sub.add_parser("outcomes", help="scoped behavior obligations and fresh pytest evidence")
    sub.add_parser("features", help="FODA feature-model validation, configuration check, coverage rollup")
    sub.add_parser(
        "capabilities", help="reconcile specified, implemented and exercised capabilities"
    )
    sub.add_parser("faults", help="judge a recorded fault-injection run against the invariant pack")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--target", default=".", help="the project root")
        p.add_argument("--source", default=None, help="source root under --target")
        p.add_argument("--adapter", default=None)
        p.add_argument("--quiet", action="store_true")

    d = sub.add_parser("discover", help="static: entities, surfaces, bindings")
    common(d)
    d.add_argument("--out", default="capabilities.json")
    d.add_argument("--check", action="store_true", help="re-derive and diff")
    d.add_argument(
        "--no-name-match",
        action="store_true",
        help="resolve calls through imports only; report every name-match as residue",
    )
    d.add_argument(
        "--resolver",
        choices=("ast", "scip"),
        default="ast",
        help="call-graph resolver: 'ast' (default, hand-rolled, stdlib-only) or "
        "'scip' (type-aware cross-file; needs a SCIP indexer + the scip CLI). "
        "The AST pass runs either way and stays the blind-spot enumerator.",
    )
    d.set_defaults(func=cmd_discover)

    o = sub.add_parser("observe", help="runtime: run the exercises with the probe")
    common(o)
    o.add_argument("--out", default="observed.json")
    o.add_argument(
        "--probe",
        default=None,
        help="runtime-evidence probe: 'pytest' (default, unchanged) | 'browser' | "
        "'load' | 'har'. Falls back to [capcov] probe, then pytest.",
    )
    o.add_argument(
        "--only",
        default=None,
        help="optional inner-loop selector, passed to the probe as CAPCOV_ONLY",
    )
    o.add_argument("command", nargs=argparse.REMAINDER)
    o.set_defaults(func=cmd_observe)

    r = sub.add_parser("reconcile", help="the four-way diff")
    r.add_argument("capabilities")
    r.add_argument("observed")
    r.add_argument("--out", default="coverage.json")
    r.add_argument("--check", action="store_true")
    r.add_argument("--quiet", action="store_true")
    r.add_argument(
        "--allow-drift",
        action="store_true",
        help="compare artifacts derived from different trees (it is not a finding)",
    )
    r.set_defaults(func=cmd_reconcile)

    g = sub.add_parser("gate", help="fail on anything unexplained")
    g.add_argument("coverage")
    g.add_argument("--exemptions", default=None)
    g.add_argument("--quiet", action="store_true")
    g.set_defaults(func=cmd_gate)

    p = sub.add_parser("report", help="human-readable coverage table")
    p.add_argument("coverage")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    if args.command if hasattr(args, "command") else False:
        args.command = [a for a in args.command if a != "--"]
    return args.func(args)
