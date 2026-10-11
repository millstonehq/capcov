"""Stage D: typed well-formedness of a Shen domain model (EXPERIMENT-PLAN section 18).

The replay judge signs every qualified verdict against a *model digest* and
requires, as a positive premise, that the model at that digest is well
formed: ``model_well_formed(model, checker, checker_version, binary, certificate)``
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

``model_well_formed`` is a structural result: it covers the model load closure
and global registry identity. Operation judgements are separate. An operation
gets a positive checked row only when its complete local judgement set passes;
an unchecked or failing operation cannot inherit another operation's result.

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
import secrets
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
CHECKER_VERSION = "1.2.0"
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
_FORBIDDEN_RUNTIME_SYMBOL = re.compile(
    r'(?<![A-Za-z0-9_.-])(load|eval|read-file)(?![A-Za-z0-9_.-])')


def _shen_executable_surface(text: str) -> str:
    """Blank strings and block comments, preserving executable token boundaries."""
    out: list[str] = []
    index = 0
    comment = False
    string = False
    while index < len(text):
        if comment:
            if text.startswith("*\\", index):
                out.extend("  ")
                index += 2
                comment = False
            else:
                out.append("\n" if text[index] == "\n" else " ")
                index += 1
            continue
        if string:
            out.append("\n" if text[index] == "\n" else " ")
            if text[index] == '"':
                string = False
            index += 1
            continue
        if text.startswith("\\*", index):
            out.extend("  ")
            index += 2
            comment = True
        elif text[index] == '"':
            out.append(" ")
            index += 1
            string = True
        else:
            out.append(text[index])
            index += 1
    if comment or string:
        raise ModelcheckFailure("model source contains an unterminated comment or string")
    return "".join(out)


class ModelcheckUnavailable(RuntimeError):
    """The pinned runtime or the checker sources cannot be used."""


class ModelcheckFailure(RuntimeError):
    """The checker could not produce a verdict; this is never a verdict."""


@dataclass(frozen=True)
class Runtime:
    bifrost: str
    bifrost_sha256: str
    shen_go: str
    shen_go_sha256: str
    modelcheck_dir: str
    sources: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        # Host paths and executable names are not evidence and may leak a
        # machine's layout; the executable digests identify the runtime.
        return {"impl": IMPL,
                "bifrost_sha256": self.bifrost_sha256,
                "shen_go_sha256": self.shen_go_sha256,
                "checker_binary": self.checker_binary,
                "invocation": ["bifrost", "run", "--impl", IMPL, "--raw", "<driver.shen>"]}

    @property
    def checker_binary(self) -> str:
        """Digest of the resolved native Shen checker executable itself."""
        return self.shen_go_sha256


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
    root = Path(model_dir).resolve()
    loader = root / "shen" / "load.shen"
    if not loader.is_file():
        raise ModelcheckFailure(f"{loader} is not a file; a model directory holds shen/load.shen")
    files = ["shen/load.shen"]
    in_comment = False
    for line in loader.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if in_comment:
            if "*\\" in stripped:
                _, _, trailing = stripped.partition("*\\")
                if trailing.strip():
                    raise ModelcheckFailure(
                        "load.shen block comments may not conceal trailing forms")
                in_comment = False
            continue
        if stripped.startswith(r"\*"):
            comment = stripped[2:]
            if "*\\" in comment:
                _, _, trailing = comment.partition("*\\")
                if trailing.strip():
                    raise ModelcheckFailure(
                        "load.shen block comments may not conceal trailing forms")
            else:
                in_comment = True
            continue
        match = _LOAD_LINE.match(stripped)
        if match:
            relative = Path(match.group(1))
            if relative.is_absolute() or ".." in relative.parts:
                raise ModelcheckFailure(
                    "load.shen entries must be relative paths contained by the model directory")
            files.append(relative.as_posix())
        elif stripped and stripped != "(tc -)":
            raise ModelcheckFailure(
                "load.shen may contain only (tc -), literal contained load forms, and block comments")
    if in_comment:
        raise ModelcheckFailure("load.shen contains an unclosed block comment")
    for relative in files:
        candidate = root / relative
        if not candidate.is_file():
            raise ModelcheckFailure(f"load.shen names {relative}, which is not a file under {root}")
        try:
            candidate.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise ModelcheckFailure("model source resolves outside the model directory") from exc
        executable = _shen_executable_surface(candidate.read_text(encoding="utf-8"))
        forbidden = _FORBIDDEN_RUNTIME_SYMBOL.search(executable)
        if relative != "shen/load.shen" and forbidden:
            raise ModelcheckFailure(
                f"model source {relative} uses forbidden runtime symbol {forbidden.group(1)!r}; "
                "all executable sources must be named by shen/load.shen")
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


_NATIVE_EXECUTABLE_MAGICS = {
    b"\x7fELF",                    # ELF
    b"\xfe\xed\xfa\xce",          # Mach-O 32-bit, big endian
    b"\xce\xfa\xed\xfe",          # Mach-O 32-bit, little endian
    b"\xfe\xed\xfa\xcf",          # Mach-O 64-bit, big endian
    b"\xcf\xfa\xed\xfe",          # Mach-O 64-bit, little endian
    b"\xca\xfe\xba\xbe",          # Mach-O universal 32-bit
    b"\xbe\xba\xfe\xca",          # Mach-O universal 32-bit, swapped
    b"\xca\xfe\xba\xbf",          # Mach-O universal 64-bit
    b"\xbf\xba\xfe\xca",          # Mach-O universal 64-bit, swapped
    b"MZ\x90\x00",                  # PE/COFF
}


def _resolve_native_executable(path: str, *, setting: str) -> tuple[str, str]:
    """Hash the resolved native executable, never an executable shell wrapper."""
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise ModelcheckUnavailable(f"{setting}={path!r} cannot be resolved to an executable") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ModelcheckUnavailable(f"{setting}={path!r} does not resolve to an executable file")
    try:
        with resolved.open("rb") as handle:
            magic = handle.read(4)
    except OSError as exc:
        raise ModelcheckUnavailable(f"{setting}={path!r} cannot be read") from exc
    if magic not in _NATIVE_EXECUTABLE_MAGICS:
        raise ModelcheckUnavailable(
            f"{setting}={path!r} is not a recognized native executable; name the actual pinned "
            "shen-go binary, not a shell or script launcher")
    return str(resolved), _sha256_file(resolved)


def runtime() -> Runtime:
    """Resolve the pinned runtime and the checker sources, or raise; never fall back."""
    bifrost = shutil.which("bifrost")
    if not bifrost:
        raise ModelcheckUnavailable("bifrost launcher is not on PATH")
    shen_go = os.environ.get("BIFROST_SHEN_GO", "")
    if not shen_go:
        raise ModelcheckUnavailable("BIFROST_SHEN_GO is not set; the pinned shen-go binary must be named explicitly")
    shen_go, shen_go_sha256 = _resolve_native_executable(shen_go, setting="BIFROST_SHEN_GO")
    directory = modelcheck_dir()
    sources = []
    for relative in _source_files():
        path = directory / relative
        if not path.is_file():
            raise ModelcheckUnavailable(f"checker source {path} is missing")
        sources.append((relative, _sha256_file(path)))
    return Runtime(bifrost, _sha256_file(bifrost), shen_go, shen_go_sha256, str(directory), tuple(sources))


# ---------------------------------------------------------------------------
# the driver


def _shen_path(path: Path) -> str:
    text = str(path)
    if '"' in text or "\n" in text:
        raise ModelcheckFailure(f"path cannot be written as a Shen string: {text!r}")
    return text


def render_driver(model_dir: Path, workdir: Path, checker_dir: Path, protocol_nonce: str) -> str:
    """The top-level script.  Its own forms run untyped; only the nested loads
    after ``(tc +)`` are typechecked, which is exactly the boundary wanted."""
    model = _shen_path(model_dir.resolve())
    gen = _shen_path((workdir / "gen").resolve())
    types = checker_dir / "types"
    lines = [
        "(tc -)",
        # The checker owns model loading so every transitive source is reported.
        f'(load "{_shen_path(checker_dir / PRELUDE)}")',
        f'(output "MC {protocol_nonce} BEGIN~%")',
        f'(cd "{model}/")',
        '(mc.quiet-load "shen/load.shen")',
        '(cd "")',
        # Restore checker definitions after the untrusted model so it cannot
        # replace mc.* protocol or judgement helpers.
        f'(load "{_shen_path(checker_dir / PRELUDE)}")',
        f'(set mc.units (mc.reify "{gen}/"))',
        "(tc +)",
        *(f'(load "{_shen_path(types / (name + ".shen"))}")' for name in (*DATATYPES, *JUDGES)),
        "(mc.judge-all (value mc.units))",
        "(tc -)",
        f'(output "MC {protocol_nonce} DONE ~A~%" (length (value mc.units)))',
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


def _judge_unit(rt: Runtime, checker_dir: Path, unit: Path, unit_id: str,
                nonce: str, workdir: Path, env: dict[str, str], timeout: float) -> tuple[str, str, str, float]:
    """Typecheck one generated literal in a fresh process that never loads the model."""
    expected_judge = ("mc.judge-ids" if unit_id == "registry-ids" else
                      "mc.judge-registry" if unit_id.startswith("registry:") else
                      f"mc.judge-{unit_id.split(':', 1)[0]}")
    text = unit.read_text(encoding="utf-8")
    prefix = f'(output "MC {nonce} PASS {unit_id} ~A~%" ({expected_judge} '
    stripped = text.rstrip("\n")
    if not stripped.startswith(prefix) or not stripped.endswith("))") or "\n" in stripped:
        raise ModelcheckFailure(f"generated unit {unit_id} does not have the canonical one-form shape")
    _validate_shen_literal(stripped[len(prefix):-2], unit_id)
    types = checker_dir / "types"
    driver_lines = [
        "(tc -)",
        f'(load "{_shen_path(checker_dir / PRELUDE)}")',
        "(tc +)",
        *(f'(load "{_shen_path(types / (name + ".shen"))}")' for name in (*DATATYPES, *JUDGES)),
        f'(trap-error (load "{_shen_path(unit)}") '
        f'(/. E (output "MC {nonce} FAIL {unit_id} ~A~%" (error-to-string E))))',
        "(tc -)",
        f'(output "MC {nonce} DONE 1~%")',
        "",
    ]
    driver = workdir / f"judge-{hashlib.sha256(unit_id.encode()).hexdigest()[:16]}.shen"
    driver.write_text("\n".join(driver_lines), encoding="utf-8")
    code, stdout, stderr, elapsed = _run(
        [rt.bifrost, "run", "--impl", IMPL, "--raw", str(driver)],
        cwd=workdir, env=env, timeout=timeout)
    if code != 0:
        raise ModelcheckFailure(f"isolated judgement process failed for {unit_id}")
    frame = re.compile(rf"^MC {re.escape(nonce)} (PASS|FAIL) {re.escape(unit_id)}(?: (.*))?$")
    matches = [frame.match(line.rstrip()) for line in stdout.splitlines()]
    matches = [match for match in matches if match]
    if len(matches) != 1:
        raise ModelcheckFailure(f"isolated judgement for {unit_id} did not emit exactly one verdict")
    verdict = "pass" if matches[0].group(1) == "PASS" else "fail"
    message = (matches[0].group(2) or "").strip()
    transcript = f"MC {nonce} {matches[0].group(1)} {unit_id} {message}\nMC {nonce} DONE 1\n"
    return verdict, message, transcript, elapsed


_LITERAL_LEXEME = re.compile(
    r'\s*(?:(?P<open>\[)|(?P<close>\])|(?P<string>"[^"\n]*")|'
    r'(?P<number>-?[0-9]+)|(?P<symbol>[A-Za-z_][A-Za-z0-9_.:/?+\-]*))')


def _validate_shen_literal(text: str, unit_id: str) -> None:
    """Accept exactly one recursively literal-only Shen value, never code."""
    position = 0
    depth = 0
    saw_value = False
    roots = 0
    while position < len(text):
        match = _LITERAL_LEXEME.match(text, position)
        if match is None:
            raise ModelcheckFailure(f"generated unit {unit_id} contains non-literal Shen syntax")
        position = match.end()
        if match.lastgroup == "open":
            depth += 1
            saw_value = True
        elif match.lastgroup == "close":
            depth -= 1
            if depth < 0:
                raise ModelcheckFailure(f"generated unit {unit_id} has an unmatched list close")
            if depth == 0:
                roots += 1
        else:
            saw_value = True
            if depth == 0:
                roots += 1
    if not saw_value or depth != 0 or roots != 1:
        raise ModelcheckFailure(f"generated unit {unit_id} does not contain one complete literal")


# ---------------------------------------------------------------------------
# the transcript


def _parse_transcript(stdout: str, protocol_nonce: str) -> dict[str, Any]:
    loaded: list[str] = []
    operations: list[str] = []
    skipped: list[tuple[str, str]] = []
    units: dict[str, str] = {}
    judges: list[str] = []
    verdicts: dict[str, tuple[str, str]] = {}
    done: int | None = None
    stray_errors: list[str] = []
    active = False
    prefix = f"MC {protocol_nonce} "
    begin = prefix + "BEGIN"
    protocol_line = re.compile(
        rf"^{re.escape(prefix)}(LOADED|OP|SKIP|UNIT|JUDGE|PASS|FAIL|DONE) ?(.*)$")
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if not active:
            if line == begin:
                active = True
            continue
        match = protocol_line.match(line)
        if not match:
            if line.startswith("ERROR:") or line.startswith("!!! FATAL"):
                stray_errors.append(line)
            continue
        kind, rest = match.group(1), match.group(2)
        if kind == "LOADED":
            loaded.append(rest)
        elif kind == "OP":
            if not _ATOM.fullmatch(rest) or rest in operations:
                raise ModelcheckFailure("operation inventory is empty or contains duplicates")
            operations.append(rest)
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
    return {"loaded": loaded, "operations": operations, "skipped": skipped,
            "units": units, "verdicts": verdicts}


# ---------------------------------------------------------------------------
# certificate


def _strip_digest(certificate: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in certificate.items() if key != "certificate_sha256"}


def certificate_digest(certificate: dict[str, Any]) -> str:
    return _sha256_bytes(canonical_json(_strip_digest(certificate)).encode("utf-8"))


_REPRODUCTION_FIELDS = (
    "kind", "checker", "checker_version", "verdict", "model",
    "model_digest_recipe", "model_files", "runtime", "checker_sources",
    "judgements", "skipped", "operations",
)


def semantic_certificate_digest(payload: dict[str, Any]) -> str:
    """Hash deterministic meaning, never run-envelope or elapsed-time fields."""
    return _sha256_bytes(canonical_json(payload).encode("utf-8"))


def _reproduction_projection(certificate: dict[str, Any]) -> dict[str, Any]:
    """Deterministic checker result, excluding nonce/time/transcript run data."""
    return {name: certificate.get(name) for name in _REPRODUCTION_FIELDS}


def _fact_from_checked_certificate(certificate: dict[str, Any]) -> dict[str, Any]:
    """The ``model_well_formed.json`` receipt file the replay exporter reads
    (replay_facts, MODEL WELL-FORMEDNESS).  Only a well-formed verdict has one."""
    if certificate.get("verdict") != "well-formed":
        raise ModelcheckFailure("only a structurally well-formed model yields a model_well_formed fact")
    if (certificate.get("checker") != CHECKER
            or certificate.get("checker_version") != CHECKER_VERSION):
        raise ModelcheckFailure("certificate does not name this checker and version")
    model = certificate["model"]
    if not isinstance(model, str) or len(model) != 64:
        raise ModelcheckFailure("certificate model is not a sha256 digest")
    try:
        int(model, 16)
    except ValueError as exc:
        raise ModelcheckFailure("certificate model is not a sha256 digest") from exc
    binary = certificate["runtime"]["checker_binary"]
    structural = [entry for entry in certificate["judgements"] if entry["id"] == "registry-ids"]
    semantic = semantic_certificate_digest({
        "model": model, "checker": CHECKER, "checker_version": CHECKER_VERSION,
        "checker_binary": binary,
        "checker_launcher_sha256": certificate["runtime"]["bifrost_sha256"],
        "checker_sources": certificate["checker_sources"],
        "judgements": [{k: j[k] for k in ("id", "verdict", "unit_sha256", "text")}
                       for j in structural],
    })
    return {
        "producer": f"{PRODUCER_CLASS} {CHECKER} {CHECKER_VERSION} model:{model[:12]}",
        "rows": [{"model": model, "checker": CHECKER, "checker_version": CHECKER_VERSION,
                  "checker_binary": binary, "certificate": semantic}],
    }


_ATOM = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:/?+\-]*$")


def _registry_operation(text: str) -> str | None:
    """Read the operation atom from a checker-emitted registry literal."""
    match = re.search(r"mc\.judge-registry\s+\[\s*([^\s\[\]]+)\s+([^\s\[\]]+)", text)
    if not match or not _ATOM.fullmatch(match.group(2)):
        return None
    return match.group(2)


def _operation_facts(certificate: dict[str, Any]) -> dict[str, Any]:
    """Return exact successful operation check rows from this checker result.

    A model whose global structure is ill formed certifies no operation: the
    operation-local judgements read a registry that did not type-check.
    """
    judgements = certificate["judgements"]
    by_id = {entry["id"]: entry for entry in judgements}
    rows = []
    identity = {
        "checker": certificate["checker"],
        "checker_version": certificate["checker_version"],
        "checker_binary": certificate["runtime"]["checker_binary"],
    }
    operations = certificate["operations"] if certificate["verdict"] == "well-formed" else []
    for operation in operations:
        required = [f"writes:{operation}", f"matrix:{operation}", f"atlas:{operation}"]
        local_ids = [j["id"] for j in judgements
                     if j["id"].startswith("registry:") and _registry_operation(j["text"]) == operation]
        required.extend(local_ids)
        if any(name not in by_id or by_id[name]["verdict"] != "pass" for name in required):
            continue
        semantic = semantic_certificate_digest({
            "model": certificate["model"], "operation": operation, **identity,
            "checker_launcher_sha256": certificate["runtime"]["bifrost_sha256"],
            "checker_sources": certificate["checker_sources"],
            "judgements": [{k: by_id[name][k] for k in ("id", "verdict", "unit_sha256", "text")}
                           for name in sorted(required)],
        })
        rows.append({"model": certificate["model"], "operation": operation,
                     **identity, "certificate": semantic})
    return {"producer": f"{PRODUCER_CLASS} {CHECKER} {CHECKER_VERSION} model:{certificate['model'][:12]}",
            "rows": rows}


def well_formed_file(certificate: dict[str, Any], model_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Mint a fact only after reproducing the certificate with the runtime.

    Structural ``recheck`` is useful diagnostics but is intentionally not an
    authority boundary.  A caller-provided certificate cannot produce
    ``model_well_formed`` unless a fresh pinned-checker run over the named
    model emits the exact same content-addressed certificate.
    """
    if certificate.get("verdict") != "well-formed":
        raise ModelcheckFailure("only a well-formed verdict yields a model_well_formed fact")
    checked = recheck(certificate, model_dir)
    if not checked.ok:
        raise ModelcheckFailure(
            "a model_well_formed fact needs a structurally valid checker certificate: "
            + "; ".join(checked.problems))
    reproduced = check(model_dir)
    if reproduced.status != "well-formed" or reproduced.fact is None:
        raise ModelcheckFailure("the named model did not reproduce a well-formed checker result")
    if _reproduction_projection(certificate) != _reproduction_projection(reproduced.certificate):
        raise ModelcheckFailure("certificate does not match a fresh checker run over the named model")
    return reproduced.fact


def _snapshot_model(source: Path, destination: Path) -> tuple[list[str], tuple[tuple[str, str], ...], str]:
    """Copy one exact model snapshot and return its load recipe and identity."""
    files = model_files(source)
    destination.mkdir()
    h = hashlib.sha256()
    hashes: list[tuple[str, str]] = []
    for relative in files:
        data = (source / relative).read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o444)
        h.update(data)
        hashes.append((relative, _sha256_bytes(data)))
    return files, tuple(hashes), h.hexdigest()


def _protocol_checker(source: Path, destination: Path, nonce: str) -> Path:
    """Copy checker sources with an unguessable per-run protocol prefix."""
    destination.mkdir()
    for relative in _source_files():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (source / relative).read_text(encoding="utf-8")
        target.write_text(text.replace("MC ", f"MC {nonce} "), encoding="utf-8")
        target.chmod(0o444)
    return destination


def _canonical_transcript(stdout: str, *, nonce: str, workdir: Path, model: Path) -> str:
    """Persist reproducible evidence without machine-local absolute paths."""
    return (stdout.replace(str(workdir), "<workdir>")
                  .replace(str(model), "<model>")
                  .replace(nonce, "<nonce>"))


def check(model_dir: str | os.PathLike[str], *, out_dir: str | os.PathLike[str] | None = None,
          timeout: float | None = None, keep: bool = False) -> CheckResult:
    """Run the checker once; write structural and per-operation facts under ``out_dir``."""
    # A failed or unavailable new check must never leave a prior positive fact
    # looking current.  Withdraw it before runtime/model resolution can fail.
    out = Path(out_dir) if out_dir is not None else None
    target = out / "model_well_formed.json" if out is not None else None
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        (out / "model_operation_checked.json").unlink(missing_ok=True)
    rt = runtime()
    source_root = Path(model_dir).resolve()
    workdir = Path(tempfile.mkdtemp(prefix="capcov-modelcheck-"))
    root = workdir / "model"
    files, file_hashes, digest = _snapshot_model(source_root, root)
    (workdir / "gen").mkdir()
    protocol_nonce = f"{secrets.randbits(192):048x}"
    checker_root = _protocol_checker(Path(rt.modelcheck_dir), workdir / "checker", protocol_nonce)
    driver_text = render_driver(root, workdir, checker_root, protocol_nonce)
    driver = workdir / "driver.shen"
    driver.write_text(driver_text, encoding="utf-8")
    limit = timeout if timeout is not None else float(os.environ.get("CAPCOV_MODELCHECK_TIMEOUT") or DEFAULT_TIMEOUT)
    env = {**os.environ, "BIFROST_SHEN_GO": rt.shen_go}
    try:
        code, stdout, stderr, elapsed = _run([rt.bifrost, "run", "--impl", IMPL, "--raw", str(driver)],
                                             cwd=workdir, env=env, timeout=limit)
        if code != 0:
            raise ModelcheckFailure(f"runtime exited {code}; stdout tail {stdout[-400:]!r}; stderr {stderr[-300:]!r}")
        after_hashes = tuple((relative, _sha256_file(root / relative)) for relative in files)
        if after_hashes != file_hashes or model_digest(root) != digest:
            raise ModelcheckFailure("the immutable model snapshot changed during checking")
        parsed = _parse_transcript(stdout, protocol_nonce)
        if parsed["loaded"] != list(files):
            raise ModelcheckFailure(
                "runtime-loaded model files differ from the hashed model closure")
        judgements = []
        trusted_transcripts: list[str] = []
        judgement_elapsed = 0.0
        for unit_id, path in parsed["units"].items():
            unit_path = Path(path).resolve()
            if unit_path.parent != (workdir / "gen").resolve():
                raise ModelcheckFailure(f"generated unit for {unit_id} is outside the checker work directory")
            if not unit_path.is_file():
                raise ModelcheckFailure(f"generated unit {path} for {unit_id} is missing")
            verdict, message, trusted_stdout, unit_elapsed = _judge_unit(
                rt, checker_root, unit_path, unit_id, protocol_nonce, workdir, env, limit)
            trusted_transcripts.append(trusted_stdout)
            judgement_elapsed += unit_elapsed
            text = unit_path.read_text(encoding="utf-8").replace(protocol_nonce, "<nonce>")
            judgements.append(Judgement(unit_id, verdict, message, unit_path.name, _sha256_bytes(text.encode("utf-8")), text))
        judgements.sort(key=lambda j: j.id)
        by_id = {j.id: j for j in judgements}
        status = ("well-formed" if "registry-ids" in by_id
                  and by_id["registry-ids"].verdict == "pass" else "ill-formed")
        canonical_transcript = _canonical_transcript(
            "".join(trusted_transcripts), nonce=protocol_nonce, workdir=workdir, model=root)
        transcript_sha256 = _sha256_bytes(canonical_transcript.encode("utf-8"))
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
            "operations": parsed["operations"],
            "transcript_sha256": transcript_sha256,
            "elapsed_seconds": round(elapsed + judgement_elapsed, 3),
        }
        certificate["certificate_sha256"] = certificate_digest(certificate)
        fact = _fact_from_checked_certificate(certificate) if status == "well-formed" else None
        if out is not None:
            (out / "modelcheck-certificate.json").write_text(json.dumps(certificate, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            (out / "modelcheck-transcript.txt").write_text(canonical_transcript, encoding="utf-8")
            if fact is not None:
                target.write_text(json.dumps(fact, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            operations = _operation_facts(certificate)
            (out / "model_operation_checked.json").write_text(
                json.dumps(operations, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return CheckResult(status, digest, file_hashes, tuple(judgements), tuple(parsed["skipped"]), certificate, fact,
                           transcript_sha256, elapsed + judgement_elapsed, str(workdir) if keep else None)
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
    required = set(_REPRODUCTION_FIELDS) | {
        "driver_sha256", "transcript_sha256", "elapsed_seconds", "certificate_sha256"
    }
    if set(certificate) != required:
        problems.append(
            f"certificate fields differ (missing {sorted(required - set(certificate))}, "
            f"unknown {sorted(set(certificate) - required)})")
    if certificate.get("kind") != CERTIFICATE_KIND:
        problems.append(f"kind is {certificate.get('kind')!r}, not {CERTIFICATE_KIND}")
    if certificate.get("checker") != CHECKER or certificate.get("checker_version") != CHECKER_VERSION:
        problems.append("certificate does not name the current checker and version")
    if certificate.get("certificate_sha256") != certificate_digest(certificate):
        problems.append("certificate_sha256 does not match the certificate's content")
    judgements = certificate.get("judgements") or []
    if not isinstance(judgements, list):
        problems.append("judgements is not an array")
        judgements = []
    ids = [entry.get("id") for entry in judgements if isinstance(entry, dict)]
    if len(ids) != len(set(ids)):
        problems.append("judgement ids are not unique")
    for entry in judgements:
        if _sha256_bytes(str(entry.get("text", "")).encode("utf-8")) != entry.get("unit_sha256"):
            problems.append(f"unit {entry.get('id')}: text does not hash to unit_sha256")
    verdict = certificate.get("verdict")
    if verdict == "well-formed":
        structural = [entry for entry in judgements if entry.get("id") == "registry-ids"]
        if len(structural) != 1 or structural[0].get("verdict") != "pass":
            problems.append("well-formed verdict without a passing global registry structure judgement")
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
    runtime_doc = certificate.get("runtime")
    runtime_keys = {"impl", "bifrost_sha256", "shen_go_sha256", "checker_binary", "invocation"}
    if not isinstance(runtime_doc, dict) or set(runtime_doc) != runtime_keys:
        problems.append("runtime identity is missing or has unknown fields")
    elif runtime_doc.get("impl") != IMPL:
        problems.append(f"runtime impl is not {IMPL}")
    directory = modelcheck_dir()
    if directory.is_dir():
        expected_sources = {relative: _sha256_file(directory / relative) for relative in _source_files()}
        try:
            recorded_sources = {entry["path"]: entry["sha256"] for entry in certificate.get("checker_sources", [])}
        except (KeyError, TypeError):
            recorded_sources = {}
        if recorded_sources != expected_sources:
            problems.append("checker source set or digests differ from the current checker")
    else:
        unchecked.append("checker sources (modelcheck directory not found)")
    unchecked.append("the type judgements themselves (rerun `check` to reproduce them)")
    return RecheckResult(not problems, tuple(problems), tuple(unchecked))


__all__ = ["CHECKER", "CHECKER_VERSION", "CERTIFICATE_KIND", "PRODUCER_CLASS", "DATATYPES", "JUDGES",
           "ModelcheckUnavailable", "ModelcheckFailure", "Runtime", "Judgement", "CheckResult", "RecheckResult",
           "model_files", "model_digest", "modelcheck_dir", "runtime", "render_driver", "check", "preflight", "recheck",
           "certificate_digest", "well_formed_file"]
