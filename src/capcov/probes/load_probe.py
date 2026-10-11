"""The load / ELT probe: a contract stub with the seam left open.

Some systems are neither an HTTP surface nor a test suite -- a headless data
pipeline extracts from sources, transforms, and loads into tables and sinks. Its
"reality" is which sources, tables and sinks it actually touched under load, and
with what effect. This probe reports that on the same four-cell seam as every
other probe, by projecting the pipeline onto bindings:

    pipeline stage  -> surface       (``pipeline:<stage>``)
    table / sink    -> entity
    effect          -> operation     (extract=read, load=create, upsert=update, …)
    run id          -> test          (the exercise that produced the binding)

The real driver -- standing up the pipeline, replaying a load, and reading back
what moved -- is out of scope for this PR. What ships here is the CONTRACT: the
env it reads, the projection (``project_binding``, pinned by a test), the shared
freshness guard, and an ``observe`` that emits a VALID ``observed`` artifact.

The stub does not fabricate evidence. It observes nothing real yet, and it says
so: the artifact carries an ``unresolved`` entry naming the missing driver, so a
stub run can never masquerade as a clean, empty observation (the same refusal the
pytest probe makes when neither ORM nor framework loads).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

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

# Pipeline effect -> the CRUD operation reconcile speaks. Weak-but-declared, the
# same shape as the browser probe's HTTP-verb -> op: an effect is coarser than a
# SQL verb, and that coarseness is a documented limitation, not a hidden one.
EFFECT_TO_OP = {
    "extract": "read",
    "read": "read",
    "select": "read",
    "load": "create",
    "insert": "create",
    "append": "create",
    "upsert": "update",
    "merge": "update",
    "update": "update",
    "delete": "delete",
    "truncate": "delete",
}


def project_binding(*, stage: str, entity: str, effect: str, run_id: str) -> dict:
    """Project one headless-pipeline observation onto a four-cell binding.

    ``stage`` names the surface (``pipeline:<stage>``); ``entity`` is the table or
    sink; ``effect`` maps to a CRUD operation; ``run_id`` is the exercise, the
    pytest-nodeid analogue. The surface string is not ``test:`` prefixed, so it is
    gated against a static surface exactly like an HTTP route.
    """
    op = EFFECT_TO_OP.get(str(effect).lower(), str(effect).lower())
    return {
        "surface": f"pipeline:{stage}",
        "entity": str(entity),
        "operations": [op],
        "tests": [str(run_id)],
    }


def _drive_pipeline(evidence_path: Path, nonce: str, only: str | None) -> None:
    """Stub driver: write fresh, nonce-stamped run evidence and nothing false.

    The real driver replays a load and reads back which stages/tables/sinks moved,
    turning each into ``project_binding``. Until it exists the run produces no
    bindings -- honestly empty, carried as an ``unresolved`` boundary by the
    caller.
    """
    evidence_path.write_text(
        json.dumps(
            {
                "nonce": nonce,
                "only": only,
                "bindings": [],
                "excluded_surfaces": [],
                # The driver is the honest limit of this reading, named as such.
                "unresolved": [
                    {
                        "adapter": "load",
                        "kind": "driver",
                        "reason": (
                            "headless ELT load driver not yet implemented; this "
                            "probe ships the contract, not the observation"
                        ),
                    }
                ],
                "exercises": 0,
            }
        )
    )


def observe(
    *,
    source_root: Path | str,
    out: Path | str,
    nonce: str | None = None,
    only: str | None = None,
    target: Path | str | None = None,
    source_patterns: tuple[str, ...] = ("**/*.py",),
    source_snapshot=None,
    source_provenance: dict | None = None,
) -> dict:
    """Run the (stub) pipeline under the freshness guard and emit ``observed``.

    Mirrors ``flows run`` and ``outcomes.execute``: unlink the stale output,
    snapshot the tree, drive the exercise into PRIVATE nonce-stamped evidence,
    verify it, then write the final ``observed`` artifact. The nonce lives in the
    private evidence; the observed schema has no nonce field.
    """
    phase_started = time.perf_counter_ns()
    source = Path(source_root)
    out_path = Path(out)
    guard = FreshnessGuard(
        out_path, source, patterns=source_patterns, snapshot=source_snapshot
    )
    run_nonce = guard.begin(nonce)
    with tempfile.TemporaryDirectory(prefix="capcov-load-") as directory:
        evidence = Path(directory) / "run.json"
        _drive_pipeline(evidence, run_nonce, only)
        run = guard.verify_output(evidence)

    bindings = list(run.get("bindings", []))
    body = {
        "bindings": bindings,
        "exercises": int(run.get("exercises", 0)),
        **observed_carriers(
            excluded_surfaces=run.get("excluded_surfaces"),
            unresolved=run.get("unresolved"),
        ),
        "timing": {
            "observe_ms": min(
                max(0, (time.perf_counter_ns() - phase_started) // 1_000_000),
                86_400_000,
            ),
            "source_verification": guard.verification,
        },
    }
    if source_provenance is not None:
        derived_from = {
            **source_provenance,
            "extractor": "capcov load-probe (stub)",
        }
    else:
        derived_from = guard.snapshot.provenance(
            os.path.basename(str(source)), "capcov load-probe (stub)"
        )
    artifacts.write(
        out_path,
        "observed",
        derived_from,
        body,
    )
    return artifacts.read(out_path, "observed")


def main(argv: list[str] | None = None) -> int:
    """Entry the probe registry points at: drive the load probe from the env.

    Reads the unified observe env contract (``CAPCOV_OUT`` / ``CAPCOV_SOURCE_ROOT``
    / ``CAPCOV_TARGET`` / ``CAPCOV_NONCE`` / ``CAPCOV_ONLY``). Returns 0 on a fresh,
    valid observation.
    """
    out = os.environ.get(ENV_OUT)
    source_root = os.environ.get(ENV_SOURCE_ROOT)
    if not out or not source_root:
        print(
            f"capcov load-probe: {ENV_OUT} and {ENV_SOURCE_ROOT} must be set",
            file=sys.stderr,
        )
        return 2
    try:
        source_snapshot, source_provenance = source_provenance_from_env(source_root)
        observe(
            source_root=source_root,
            out=out,
            nonce=os.environ.get(ENV_NONCE),
            only=os.environ.get(ENV_ONLY),
            target=os.environ.get(ENV_TARGET),
            source_patterns=(
                source_snapshot.patterns
                if source_snapshot is not None
                else ("**/*.py",)
            ),
            source_snapshot=source_snapshot,
            source_provenance=source_provenance,
        )
    except (ValueError, OSError) as error:
        print(f"capcov load-probe: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
