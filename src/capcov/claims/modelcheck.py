"""Stage D: typed well-formedness of a Shen domain model (EXPERIMENT-PLAN section 18).

The replay judge signs every qualified verdict against a *model digest* and
requires, as a positive premise, that the model at that digest is well
formed: ``model_well_formed(model, checker, checker_version, certificate)``
from producer class ``modelcheck`` (``schema_replay_v1``).  This module is that
producer.  It does not judge replays and it does not trust the model host.

What "well formed" means here is decided by Shen's type checker, not by a
Python walker.  The pinned shen-go runtime loads the model untyped, asks it a
fixed set of questions (``shen/modelcheck/prelude.shen``), writes each answer
as a Shen literal into a one-form unit, and then, under ``(tc +)``, loads the
datatypes in ``shen/modelcheck/types/`` and each unit in turn.  A unit whose
literal does not inhabit its well-formedness type raises a type error, which is
trapped and reported as ``MC FAIL <id> <message>``; a unit that typechecks
prints ``MC PASS <id> verified``.  The judgements:

  writes:<op>     [Op Declared ObservedAsIs Vocabulary] : wf-writes
                  declared write-set is a distinct table list, every table is
                  one the effect vocabulary can write, and it equals (as a
                  set) the tables the as-is effects touch on the witnesses
  matrix:<op>     [Op Cells] : wf-matrix
                  admissibility under {committed, aborted, unknown} x
                  {live, nonlive} is total, disjoint, and has the documented
                  shape (refuse committed on a non-live target, ...)
  atlas:<ep>      [Endpoint Observed Required Failure] : wf-atlas
  registry:<id>   [Id Op Rule RuleIndex Endpoints Reach Witness] : wf-registry
  registry-ids    [Id ...] : id-list

Only ops with an as-is target on some live witness get ``writes``/``matrix``
judgements; the others are reported as skipped, never presumed.

The certificate binds the model digest (sha256 over the ``.shen`` sources in
``load.shen`` order, the model's own recipe), every source's sha256, the exact
literals judged, the checker sources, the runtime binary and the transcript.
Its digest is the ``certificate`` column of the fact.  ``recheck`` recomputes
what can be recomputed without a runtime.  Fail-closed throughout: a
transcript that does not account for every unit, a loader that loaded
different files than the recipe names, or a runtime error is a *checker
failure*, never a verdict.

Measured limits of the type checker that this design works around are listed
at the top of ``prelude.shen``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ir import canonical_json

CHECKER = "capcov-modelcheck"
CHECKER_VERSION = "1.0.0"
CERTIFICATE_KIND = "capcov-modelcheck-certificate-v1"
PRODUCER_CLASS = "modelcheck"
IMPL = "shen-go"
DEFAULT_TIMEOUT = 300.0

# Load order matters: a datatype may only mention datatypes loaded before it.
DATATYPES = ("table-list", "wf-writes", "cell", "cell-list", "wf-matrix",
             "wf-atlas", "wf-registry", "id-list")
JUDGES = ("judge-writes", "judge-matrix", "judge-atlas", "judge-registry", "judge-ids")
PRELUDE = "prelude.shen"

_LOAD_LINE = re.compile(r'^\(load "([^"]+)"\)\s*$')
_MC_LINE = re.compile(r"^MC (LOADED|SKIP|UNIT|JUDGE|PASS|FAIL|DONE) ?(.*)$")


class ModelcheckUnavailable(RuntimeError):
    """The pinned runtime or the checker sources cannot be used."""


class ModelcheckFailure(RuntimeError):
    """The checker could not produce a verdict; this is never a verdict."""


@dataclass(frozen=True)
class Runtime:
    bifrost: str
    shen_go: str
    shen_go_sha256: str
    modelcheck_dir: str
    sources: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        # Host paths are not evidence and would leak a machine's layout into a
        # published certificate; the binary's digest and the launcher's name are.
        return {"impl": IMPL, "bifrost": os.path.basename(self.bifrost), "shen_go": os.path.basename(self.shen_go),
                "shen_go_sha256": self.shen_go_sha256,
                "invocation": ["bifrost", "run", "--impl", IMPL, "--raw", "<driver.shen>"]}


@dataclass(frozen=True)
class Judgement:
    id: str
    verdict: str            # "pass" | "fail"
    message: str
    unit: str               # unit file name
    unit_sha256: str
    text: str               # the exact one-form unit that was typechecked


@dataclass(frozen=True)
class CheckResult:
    status: str             # "well-formed" | "ill-formed"
    model_digest: str
    model_files: tuple[tuple[str, str], ...]
    judgements: tuple[Judgement, ...]
    skipped: tuple[tuple[str, str], ...]
    certificate: dict[str, Any]
    fact: dict[str, Any] | None
    transcript_sha256: str
    elapsed_seconds: float
    workdir: str | None

    @property
    def failures(self) -> tuple[Judgement, ...]:
        return tuple(j for j in self.judgements if j.verdict != "pass")


@dataclass(frozen=True)
class RecheckResult:
    ok: bool
    problems: tuple[str, ...] = field(default_factory=tuple)
    unchecked: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# the model's own identity


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def model_files(model_dir: str | os.PathLike[str]) -> list[str]:
    """The ``.shen`` sources in load order: ``shen/load.shen`` then each file
    it loads, exactly as the model's own ``scripts/model-digest.sh`` reads them.
    Paths are relative to the model directory, posix, as written in load.shen."""
    root = Path(model_dir)
    loader = root / "shen" / "load.shen"
    if not loader.is_file():
        raise ModelcheckFailure(f"{loader} is not a file; a model directory holds shen/load.shen")
    files = ["shen/load.shen"]
    for line in loader.read_text(encoding="utf-8").splitlines():
        match = _LOAD_LINE.match(line.strip())
        if match:
            files.append(match.group(1))
    for relative in files:
        if not (root / relative).is_file():
            raise ModelcheckFailure(f"load.shen names {relative}, which is not a file under {root}")
    return files


def model_digest(model_dir: str | os.PathLike[str]) -> str:
    """sha256 over the concatenated bytes of ``model_files`` in order."""
    root = Path(model_dir)
    h = hashlib.sha256()
    for relative in model_files(root):
        h.update((root / relative).read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# runtime resolution


def modelcheck_dir() -> Path:
    override = os.environ.get("CAPCOV_MODELCHECK_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "shen" / "modelcheck"


def _source_files() -> list[str]:
    return [PRELUDE, *(f"types/{name}.shen" for name in DATATYPES), *(f"types/{name}.shen" for name in JUDGES)]


def runtime() -> Runtime:
    """Resolve the pinned runtime and the checker sources, or raise; never fall back."""
    bifrost = shutil.which("bifrost")
    if not bifrost:
        raise ModelcheckUnavailable("bifrost launcher is not on PATH")
    shen_go = os.environ.get("BIFROST_SHEN_GO", "")
    if not shen_go:
        raise ModelcheckUnavailable("BIFROST_SHEN_GO is not set; the pinned shen-go binary must be named explicitly")
    if not (os.path.isfile(shen_go) and os.access(shen_go, os.X_OK)):
        raise ModelcheckUnavailable(f"BIFROST_SHEN_GO={shen_go!r} is not an executable file")
    directory = modelcheck_dir()
    sources = []
    for relative in _source_files():
        path = directory / relative
        if not path.is_file():
            raise ModelcheckUnavailable(f"checker source {path} is missing")
        sources.append((relative, _sha256_file(path)))
    return Runtime(bifrost, shen_go, _sha256_file(shen_go), str(directory), tuple(sources))


# ---------------------------------------------------------------------------
# the driver


def _shen_path(path: Path) -> str:
    text = str(path)
    if '"' in text or "\n" in text:
        raise ModelcheckFailure(f"path cannot be written as a Shen string: {text!r}")
    return text


def render_driver(model_dir: Path, workdir: Path, checker_dir: Path) -> str:
    """The top-level script.  Its own forms run untyped; only the nested loads
    after ``(tc +)`` are typechecked, which is exactly the boundary wanted."""
    model = _shen_path(model_dir.resolve())
    gen = _shen_path((workdir / "gen").resolve())
    types = checker_dir / "types"
    lines = [
        "(tc -)",
        f'(load "{_shen_path(checker_dir / PRELUDE)}")',
        f'(cd "{model}/")',
        '(mc.quiet-load "shen/load.shen")',
        '(cd "")',
        f'(set mc.units (mc.reify "{gen}/"))',
        "(tc +)",
        *(f'(load "{_shen_path(types / (name + ".shen"))}")' for name in (*DATATYPES, *JUDGES)),
        "(mc.judge-all (value mc.units))",
        "(tc -)",
        '(output "MC DONE ~A~%" (length (value mc.units)))',
        "",
    ]
    return "\n".join(lines)


def _run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str, str, float]:
    started = time.monotonic()
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, cwd=str(cwd), start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
        raise ModelcheckFailure(f"modelcheck run exceeded {timeout:g}s and was killed; "
                                f"stdout tail: {out.decode('utf-8', 'replace')[-500:]!r}")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), time.monotonic() - started


# ---------------------------------------------------------------------------
# the transcript


def _parse_transcript(stdout: str) -> dict[str, Any]:
    loaded: list[str] = []
    skipped: list[tuple[str, str]] = []
    units: dict[str, str] = {}
    judges: list[str] = []
    verdicts: dict[str, tuple[str, str]] = {}
    done: int | None = None
    stray_errors: list[str] = []
    for raw in stdout.splitlines():
        line = raw.rstrip()
        match = _MC_LINE.match(line)
        if not match:
            if line.startswith("ERROR:") or line.startswith("!!! FATAL"):
                stray_errors.append(line)
            continue
        kind, rest = match.group(1), match.group(2)
        if kind == "LOADED":
            loaded.append(rest)
        elif kind == "SKIP":
            op, _, reason = rest.partition(" ")
            skipped.append((op, reason))
        elif kind == "UNIT":
            unit_id, _, path = rest.partition(" ")
            if unit_id in units:
                raise ModelcheckFailure(f"unit {unit_id} was generated twice")
            units[unit_id] = path
        elif kind == "JUDGE":
            judges.append(rest)
        elif kind in ("PASS", "FAIL"):
            unit_id, _, message = rest.partition(" ")
            if unit_id in verdicts:
                raise ModelcheckFailure(f"unit {unit_id} reported two verdicts")
            verdicts[unit_id] = ("pass" if kind == "PASS" else "fail", message.strip())
        elif kind == "DONE":
            done = int(rest.strip())
    if done is None:
        raise ModelcheckFailure("transcript has no MC DONE line; the run did not complete")
    if done != len(units):
        raise ModelcheckFailure(f"MC DONE counted {done} units but {len(units)} were generated")
    missing = sorted(set(units) - set(verdicts))
    extra = sorted(set(verdicts) - set(units))
    if missing or extra:
        raise ModelcheckFailure(f"verdicts do not cover the generated units (missing {missing}, unknown {extra})")
    if sorted(judges) != sorted(f"mc.{name}" for name in JUDGES):
        raise ModelcheckFailure(f"judgement functions loaded do not match the checker's: {judges}")
    if stray_errors:
        raise ModelcheckFailure("runtime errors outside the judgement protocol: " + " | ".join(stray_errors[:3]))
    return {"loaded": loaded, "skipped": skipped, "units": units, "verdicts": verdicts}


# ---------------------------------------------------------------------------
# certificate


def _strip_digest(certificate: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in certificate.items() if key != "certificate_sha256"}


def certificate_digest(certificate: dict[str, Any]) -> str:
    return _sha256_bytes(canonical_json(_strip_digest(certificate)).encode("utf-8"))


def well_formed_file(certificate: dict[str, Any]) -> dict[str, Any]:
    """The ``model_well_formed.json`` receipt file the replay exporter reads
    (replay_facts, MODEL WELL-FORMEDNESS).  Only a well-formed verdict has one."""
    if certificate.get("verdict") != "well-formed":
        raise ModelcheckFailure("only a well-formed verdict yields a model_well_formed fact")
    model = certificate["model"]
    return {
        "producer": f"{PRODUCER_CLASS} {CHECKER} {CHECKER_VERSION} model:{model[:12]}",
        "rows": [{"model": model, "checker": CHECKER, "checker_version": CHECKER_VERSION,
                  "certificate": certificate["certificate_sha256"]}],
    }


def check(model_dir: str | os.PathLike[str], *, out_dir: str | os.PathLike[str] | None = None,
          timeout: float | None = None, keep: bool = False) -> CheckResult:
    """Run the checker once against ``model_dir``; write the certificate (and,
    when well formed, ``model_well_formed.json``) under ``out_dir`` if given."""
    rt = runtime()
    root = Path(model_dir).resolve()
    files = model_files(root)
    digest = model_digest(root)
    file_hashes = tuple((relative, _sha256_file(root / relative)) for relative in files)
    workdir = Path(tempfile.mkdtemp(prefix="capcov-modelcheck-"))
    (workdir / "gen").mkdir()
    driver_text = render_driver(root, workdir, Path(rt.modelcheck_dir))
    driver = workdir / "driver.shen"
    driver.write_text(driver_text, encoding="utf-8")
    limit = timeout if timeout is not None else float(os.environ.get("CAPCOV_MODELCHECK_TIMEOUT") or DEFAULT_TIMEOUT)
    env = {**os.environ, "BIFROST_SHEN_GO": rt.shen_go}
    try:
        code, stdout, stderr, elapsed = _run([rt.bifrost, "run", "--impl", IMPL, "--raw", str(driver)],
                                             cwd=workdir, env=env, timeout=limit)
        if code != 0:
            raise ModelcheckFailure(f"runtime exited {code}; stdout tail {stdout[-400:]!r}; stderr {stderr[-300:]!r}")
        parsed = _parse_transcript(stdout)
        if parsed["loaded"] != files:
            raise ModelcheckFailure(f"the loader loaded {parsed['loaded']} but the digest recipe names {files}; "
                                    "the digest would not describe what was checked")
        judgements = []
        for unit_id, path in parsed["units"].items():
            unit_path = Path(path)
            if not unit_path.is_file():
                raise ModelcheckFailure(f"generated unit {path} for {unit_id} is missing")
            text = unit_path.read_text(encoding="utf-8")
            verdict, message = parsed["verdicts"][unit_id]
            judgements.append(Judgement(unit_id, verdict, message, unit_path.name, _sha256_bytes(text.encode("utf-8")), text))
        judgements.sort(key=lambda j: j.id)
        status = "well-formed" if all(j.verdict == "pass" for j in judgements) and judgements else "ill-formed"
        transcript_sha256 = _sha256_bytes(stdout.encode("utf-8"))
        certificate: dict[str, Any] = {
            "kind": CERTIFICATE_KIND,
            "checker": CHECKER,
            "checker_version": CHECKER_VERSION,
            "verdict": status,
            "model": digest,
            "model_digest_recipe": "sha256 over the concatenated bytes of shen/load.shen and the files it loads, in load order",
            "model_files": [{"path": relative, "sha256": sha} for relative, sha in file_hashes],
            "runtime": rt.as_dict(),
            "checker_sources": [{"path": relative, "sha256": sha} for relative, sha in rt.sources],
            "driver_sha256": _sha256_bytes(driver_text.encode("utf-8")),
            "judgements": [{"id": j.id, "verdict": j.verdict, "message": j.message, "unit": j.unit,
                            "unit_sha256": j.unit_sha256, "text": j.text} for j in judgements],
            "skipped": [{"op": op, "reason": reason} for op, reason in parsed["skipped"]],
            "transcript_sha256": transcript_sha256,
            "elapsed_seconds": round(elapsed, 3),
        }
        certificate["certificate_sha256"] = certificate_digest(certificate)
        fact = well_formed_file(certificate) if status == "well-formed" else None
        if out_dir is not None:
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "modelcheck-certificate.json").write_text(json.dumps(certificate, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            (out / "modelcheck-transcript.txt").write_text(stdout, encoding="utf-8")
            target = out / "model_well_formed.json"
            if fact is not None:
                target.write_text(json.dumps(fact, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            elif target.exists():
                target.unlink()  # never leave a stale certificate beside an ill-formed verdict
        return CheckResult(status, digest, file_hashes, tuple(judgements), tuple(parsed["skipped"]), certificate, fact,
                           transcript_sha256, elapsed, str(workdir) if keep else None)
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def preflight(model_dir: str | os.PathLike[str], *, out_dir: str | os.PathLike[str] | None = None,
              timeout: float | None = None) -> dict[str, Any]:
    """The one call a producer profile makes.  Never raises.

    ``status`` is ``well-formed`` / ``ill-formed`` (a verdict of the type
    checker), ``unavailable`` (the pinned runtime or the checker sources are
    missing -- a named refusal for the caller to surface, never a downgrade),
    or ``failed`` (the checker could not reach a verdict).  Only ``well-formed``
    carries a ``fact`` row.
    """
    try:
        result = check(model_dir, out_dir=out_dir, timeout=timeout)
    except ModelcheckUnavailable as exc:
        return {"status": "unavailable", "model": None, "fact": None, "certificate_sha256": None,
                "failures": [], "error": str(exc)}
    except ModelcheckFailure as exc:
        digest = None
        try:
            digest = model_digest(model_dir)
        except ModelcheckFailure:
            pass
        return {"status": "failed", "model": digest, "fact": None, "certificate_sha256": None,
                "failures": [], "error": str(exc)}
    return {"status": result.status, "model": result.model_digest,
            "fact": result.fact["rows"][0] if result.fact else None,
            "certificate_sha256": result.certificate["certificate_sha256"],
            "failures": [{"id": j.id, "message": j.message} for j in result.failures],
            "error": None}


def recheck(certificate: dict[str, Any], model_dir: str | os.PathLike[str] | None = None) -> RecheckResult:
    """Recompute what needs no runtime: the certificate's own digest, the model
    digest and file hashes against ``model_dir`` when given, every unit's text
    hash, the checker sources when present, and that a well-formed verdict has
    no failing judgement.  Anything that cannot be recomputed is listed as
    unchecked, never assumed."""
    problems: list[str] = []
    unchecked: list[str] = []
    if certificate.get("kind") != CERTIFICATE_KIND:
        problems.append(f"kind is {certificate.get('kind')!r}, not {CERTIFICATE_KIND}")
    if certificate.get("certificate_sha256") != certificate_digest(certificate):
        problems.append("certificate_sha256 does not match the certificate's content")
    judgements = certificate.get("judgements") or []
    for entry in judgements:
        if _sha256_bytes(str(entry.get("text", "")).encode("utf-8")) != entry.get("unit_sha256"):
            problems.append(f"unit {entry.get('id')}: text does not hash to unit_sha256")
    verdict = certificate.get("verdict")
    if verdict == "well-formed":
        if not judgements:
            problems.append("well-formed verdict with no judgements")
        for entry in judgements:
            if entry.get("verdict") != "pass":
                problems.append(f"well-formed verdict but judgement {entry.get('id')} is {entry.get('verdict')}")
    elif verdict != "ill-formed":
        problems.append(f"verdict is {verdict!r}")
    if model_dir is not None:
        try:
            root = Path(model_dir)
            if model_digest(root) != certificate.get("model"):
                problems.append("model digest differs from the model directory")
            recorded = {e["path"]: e["sha256"] for e in certificate.get("model_files", [])}
            current = {relative: _sha256_file(root / relative) for relative in model_files(root)}
            if recorded != current:
                problems.append("model file hashes differ from the model directory")
        except ModelcheckFailure as exc:
            problems.append(f"model directory: {exc}")
    else:
        unchecked.append("model digest (no model directory given)")
    directory = modelcheck_dir()
    if directory.is_dir():
        for entry in certificate.get("checker_sources", []):
            path = directory / entry["path"]
            if not path.is_file():
                problems.append(f"checker source {entry['path']} is missing")
            elif _sha256_file(path) != entry["sha256"]:
                problems.append(f"checker source {entry['path']} differs from the certificate")
    else:
        unchecked.append("checker sources (modelcheck directory not found)")
    unchecked.append("the type judgements themselves (rerun `check` to reproduce them)")
    return RecheckResult(not problems, tuple(problems), tuple(unchecked))


__all__ = ["CHECKER", "CHECKER_VERSION", "CERTIFICATE_KIND", "PRODUCER_CLASS", "DATATYPES", "JUDGES",
           "ModelcheckUnavailable", "ModelcheckFailure", "Runtime", "Judgement", "CheckResult", "RecheckResult",
           "model_files", "model_digest", "modelcheck_dir", "runtime", "render_driver", "check", "preflight", "recheck",
           "certificate_digest", "well_formed_file"]
