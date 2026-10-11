"""Compiled Souffle checker: a third claim kernel with recorded provenance.

The validated pack (replay or static) translates to a Souffle program whose
text is fact-independent (``program_for_pack``).  ``compile_program``
generates C++ with ``souffle --no-preprocessor -j1 -g``, canonicalizes the
generated relation-I/O block order, and invokes the pinned sibling
``souffle-compile.py`` into a
native binary cached by ``compile_key`` = sha256 of (schema, program digest,
souffle executable sha256, compile flags, ``souffle-compile.py`` sha256, and
canonicalizer schema), and
records ``provenance.json`` next to it.  ``run_compiled`` then executes the binary on a bundle's TSV facts
through the same temp-root / parse / claim-fold path the interpreter uses
(``souffle._execute``), so the two Souffle kernels differ only in the process
that computes the closure.

One deliberate difference the provenance records in ``compile_flags``: the
compiled program is built with ``--no-preprocessor`` while the interpreter runs
with mcpp on.  The two kernels therefore run the same program *text*; a
relation alias colliding with a predefined mcpp macro would diverge, and would
do so as an interpreter failure, never as a silent disagreement.

The value of the compiled kernel is a third independent evaluator with
recorded provenance (program digest, souffle sha256, compiler configuration,
binary sha256), not throughput: at fixture scale the binary is slower than the
interpreter because process start dominates.  Nothing here gates on speed.

Failure is never a fallback: a compiler that is absent or fails is a named
operational failure (``SouffleUnavailable`` / ``CompileError``), and a binary
built from another program is refused (``CompiledProgramMismatch``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Iterable, Mapping

from ..ir import Bundle, canonical_json
from . import (MAX_OUTPUT_BYTES, MAX_PROCESSES, MAX_ROWS, MAX_SECONDS, PROGRAM_SCHEMA,
               SouffleProgram, SouffleResult, SouffleUnavailable, _ExecutionBudget,
               _executable_identity, _execute, _Limits, program_for_pack, translate_bundle)

GENERATE_FLAGS = ("--no-preprocessor", "-j1", "-g")
GENERATED_SOURCE = "checker.cpp"
NATIVE_FLAGS = ("-o",)
NATIVE_OUTPUT = "checker"
COMPILER_CONFIG_NAME = "souffle-compile.py"
COMPILE_FLAGS: tuple[str, ...] = (*GENERATE_FLAGS, GENERATED_SOURCE,
                                 "canonicalizer-source-sha256", COMPILER_CONFIG_NAME,
                                 *NATIVE_FLAGS, NATIVE_OUTPUT)
COMPILE_TIMEOUT = 900.0
VERSION_TIMEOUT = 30.0
# Exactly the keys of provenance.json; ``compiled_at`` is deliberately absent so
# the file is reproducible for one (program, souffle) pair.
PROVENANCE_KEYS = frozenset({
    "schema", "compile_key", "program_digest", "binary_sha256", "souffle_path",
    "souffle_sha256", "souffle_version", "compiler_config_sha256", "compile_flags",
    "compile_seconds",
})
# Fields a cached entry must match to be reused; the rest are recorded values.
_IDENTITY_KEYS = ("schema", "compile_key", "program_digest", "souffle_sha256",
                  "souffle_version", "compiler_config_sha256", "compile_flags")


class CompileError(RuntimeError):
    """``souffle -o`` returned a non-zero status, timed out or produced no binary."""


class CompiledProgramMismatch(ValueError):
    """The bundle's program digest is not the one the checker was compiled from."""


@dataclass(frozen=True)
class CompiledChecker:
    program_digest: str
    binary_path: str
    binary_sha256: str
    souffle_path: str
    souffle_sha256: str
    souffle_version: str
    compile_flags: tuple[str, ...]
    compile_key: str
    compiler_config_sha256: str
    compile_seconds: float = 0.0
    cache_hit: bool = field(default=False, compare=False)
    """True when this checker came from the cache rather than a fresh compile.
    How the checker was obtained, not what it is: it is neither part of
    ``provenance()`` nor of equality, so a cached checker equals the freshly
    compiled one it reproduces."""

    @property
    def runtime(self) -> str:
        return "souffle-compiled:" + self.binary_sha256[:16]

    def provenance(self) -> dict[str, Any]:
        """The ``provenance.json`` document (exactly ``PROVENANCE_KEYS``)."""
        return {
            "schema": PROGRAM_SCHEMA,
            "compile_key": self.compile_key,
            "program_digest": self.program_digest,
            "binary_sha256": self.binary_sha256,
            "souffle_path": self.souffle_path,
            "souffle_sha256": self.souffle_sha256,
            "souffle_version": self.souffle_version,
            "compiler_config_sha256": self.compiler_config_sha256,
            "compile_flags": list(self.compile_flags),
            "compile_seconds": self.compile_seconds,
        }


def compile_key(program_digest: str, souffle_sha256: str,
                flags: Iterable[str] = COMPILE_FLAGS, *,
                compiler_config_sha256: str = "") -> str:
    """64-hex identity of a compiled checker: program, compiler and flags.

    ``compiler_config_sha256`` binds ``souffle-compile.py`` (which embeds the
    C++ toolchain) and the generated-C++ canonicalizer schema. Two toolchains
    or canonicalizer revisions are therefore separate cache entries.
    """
    basis = {"schema": PROGRAM_SCHEMA, "program_digest": program_digest,
             "souffle_sha256": souffle_sha256, "compile_flags": list(flags),
             "compiler_config_sha256": compiler_config_sha256}
    return hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    content = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            content.update(chunk)
    return content.hexdigest()


def _souffle_version(resolved: str) -> str:
    """The ``Version:`` line of ``souffle --version`` (``""`` for the nix build)."""
    try:
        completed = subprocess.run([resolved, "--version"], capture_output=True, text=True,
                                   timeout=VERSION_TIMEOUT, check=False)
    except OSError as exc:
        raise SouffleUnavailable(f"Souffle executable could not be started: {resolved}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SouffleUnavailable(f"souffle --version timed out: {resolved}") from exc
    for line in (completed.stdout or "").splitlines():
        if line.strip().startswith("Version:"):
            return line.strip()[len("Version:"):].strip()
    return ""


def _compiler_config_sha256(resolved: str) -> str:
    """sha256 of the sibling ``souffle-compile.py`` (it embeds the C++ toolchain).

    ``souffle -o`` cannot build a binary without it, so an absent or unreadable
    sibling is ``SouffleUnavailable`` (the judge's exit 4) and never a compile
    failure: a toolchain gap must not surface as a kernel disagreement.
    """
    sibling = Path(resolved).resolve().parent / COMPILER_CONFIG_NAME
    try:
        identity = {
            "canonicalizer": {
                "source_sha256": hashlib.sha256(
                    inspect.getsource(_canonicalize_output_blocks).encode("utf-8")).hexdigest(),
                "patterns": {
                    name: {"pattern": pattern.pattern, "flags": pattern.flags}
                    for name, pattern in (
                        ("file_output_name", _FILE_OUTPUT_NAME),
                        ("stream_output_name", _STREAM_OUTPUT_NAME),
                        ("io_block", _IO_BLOCK),
                    )
                },
            },
            "compile_recipe": list(COMPILE_FLAGS),
            "souffle_compile_sha256": _sha256_file(sibling),
        }
        return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    except OSError as exc:
        raise SouffleUnavailable(
            f"{COMPILER_CONFIG_NAME} is not readable beside the souffle executable: "
            f"{sibling}: {exc}") from exc


_FILE_OUTPUT_NAME = re.compile(r'R"_\(name\)_",R"_\(([^)]*)\)_"')
_STREAM_OUTPUT_NAME = re.compile(r'rwOperation\["name"\] = "([^"]+)";')
_IO_BLOCK = re.compile(
    r"^try \{.*\} catch \(std::exception& e\) \{.*\}$", re.DOTALL)


def _canonicalize_output_blocks(source: str) -> str:
    """Sort independent generated relation-I/O blocks by relation name.

    Souffle 2.5 walks an address-sensitive container when emitting ``printAll``
    and its corresponding input methods. The generated programs are equivalent,
    but their C++ and native bytes differ between cold builds. Only relation-I/O
    methods are normalized; evaluator code remains byte-for-byte as emitted.
    """
    for method, name_pattern in (("loadAll", _FILE_OUTPUT_NAME),
                                 ("printAll", _FILE_OUTPUT_NAME),
                                 ("dumpInputs", _STREAM_OUTPUT_NAME),
                                 ("dumpOutputs", _STREAM_OUTPUT_NAME)):
        signature = re.compile(
            rf"(\nvoid Sf_checker::{method}\([^\n]*\)\{{\n)(.*?)(\n\}}\n)"
            rf"(?=\n[^\n]*Sf_checker::)", re.DOTALL)
        match = signature.search(source)
        if match is None:
            raise CompileError(f"generated C++ has no {method} method")
        body = match.group(2)
        raw_blocks = [block for block in re.split(r"(?=try \{)", body) if block]
        blocks = [block.strip() for block in raw_blocks]
        if not blocks or any(_IO_BLOCK.fullmatch(block) is None for block in blocks):
            raise CompileError(f"generated C++ {method} body is not a sequence of output blocks")
        named: list[tuple[str, str]] = []
        for block in blocks:
            relations = name_pattern.findall(block)
            if len(relations) != 1:
                raise CompileError(
                    f"generated C++ {method} block must have exactly one relation name")
            named.append((relations[0], block))
        names = [name for name, _ in named]
        if len(names) != len(set(names)):
            raise CompileError(f"generated C++ {method} contains duplicate relation outputs")
        replacement = (match.group(1) + "\n".join(block for _, block in sorted(named))
                       + match.group(3))
        source = source[:match.start()] + replacement + source[match.end():]
    return source


def _run_compile_step(argv: list[str], *, work: Path, timeout: float) -> tuple[str, str, float]:
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=work, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise SouffleUnavailable(f"compiler step could not be started: {argv[0]}: {exc}") from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill(); proc.communicate()
        raise CompileError(f"compiled-checker build exceeded {timeout:.0f}s")
    elapsed = time.monotonic() - started
    if proc.returncode:
        raise CompileError((stderr or stdout)[-4000:])
    return stdout, stderr, elapsed


def _entry_dir(cache_dir: str | os.PathLike[str], key: str) -> Path:
    return Path(cache_dir) / f"compiled-{key}"


def _read_provenance(entry: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((entry / "provenance.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != PROVENANCE_KEYS:
        return None
    return payload


def _cached_checker(entry: Path, expected: Mapping[str, Any]) -> CompiledChecker | None:
    """Reuse ``entry`` iff its provenance matches and the binary hashes as recorded."""
    payload = _read_provenance(entry)
    if payload is None:
        return None
    if any(payload.get(key) != expected[key] for key in _IDENTITY_KEYS):
        return None
    if (not isinstance(payload["binary_sha256"], str) or not isinstance(payload["souffle_path"], str)
            or not isinstance(payload["compile_seconds"], (int, float))
            or isinstance(payload["compile_seconds"], bool)):
        return None
    checker = entry / "checker"
    try:
        if not checker.is_file() or not os.access(checker, os.X_OK):
            return None
        actual = _sha256_file(checker)
    except OSError:
        return None
    if actual != payload["binary_sha256"]:
        return None
    return CompiledChecker(
        program_digest=payload["program_digest"], binary_path=os.fspath(checker),
        binary_sha256=actual, souffle_path=payload["souffle_path"],
        souffle_sha256=payload["souffle_sha256"], souffle_version=payload["souffle_version"],
        compile_flags=tuple(payload["compile_flags"]), compile_key=payload["compile_key"],
        compiler_config_sha256=payload["compiler_config_sha256"],
        compile_seconds=float(payload["compile_seconds"]), cache_hit=True)


def prune_cache(cache_dir: str | os.PathLike[str], souffle_sha256: str) -> list[str]:
    """Remove ``compiled-*`` entries not built by the souffle executable in use.

    Bounds the cache to one souffle build: an entry whose provenance is
    unreadable or names another ``souffle_sha256`` is deleted.  A ``.compiled-*``
    staging directory older than one compile timeout is removed too --
    ``compile_program`` cleans up its own, so one that old is the residue of a
    killed process, and the age bound keeps a live compile's staging directory.
    Returns the removed entry names.
    """
    removed = []
    directory = Path(cache_dir)
    if not directory.is_dir():
        return removed
    for entry in sorted(directory.glob("compiled-*")):
        if not entry.is_dir():
            continue
        payload = _read_provenance(entry)
        if payload is None or payload.get("souffle_sha256") != souffle_sha256:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    for staging in sorted(directory.glob(".compiled-*")):
        try:
            stale = staging.is_dir() and time.time() - staging.stat().st_mtime > COMPILE_TIMEOUT
        except OSError:
            continue
        if stale:
            shutil.rmtree(staging, ignore_errors=True)
            removed.append(staging.name)
    return removed


def compile_program(program: SouffleProgram, *, executable: str = "souffle",
                    cache_dir: str | os.PathLike[str], timeout: float = COMPILE_TIMEOUT,
                    prune: bool = True) -> CompiledChecker:
    """Compile ``program`` once into ``<cache_dir>/compiled-<key>/checker``.

    A cached entry is reused only when its ``provenance.json`` parses, every
    identity field matches and the binary's sha256 equals the recorded one;
    otherwise the program is compiled in a temp dir, moved atomically into the
    cache and its provenance written.  rc != 0 is ``CompileError`` (Souffle
    writes dozens of ``variable only occurs once`` warnings to stderr with rc
    0; only the return code decides).  ``cache_dir`` may be relative: it is
    made absolute first, so ``binary_path`` runs from the temp root the shared
    execution path spawns it in.  A cache directory that cannot be written is
    ``SouffleUnavailable`` -- an environment failure, never a claim about the
    program.
    """
    # The binary is spawned with ``cwd`` = a temp root, where a relative
    # argv[0] would not resolve: the cache directory is made absolute here so
    # every ``binary_path`` (cached or fresh) is runnable from any directory.
    cache_dir = Path(cache_dir).absolute()
    resolved = shutil.which(executable)
    if resolved is None:
        raise SouffleUnavailable(f"Souffle executable not found: {executable}")
    identity = _executable_identity(resolved)
    if identity is None:
        raise SouffleUnavailable(f"Souffle executable could not be read: {resolved}")
    souffle_sha256 = identity["sha256"]
    souffle_path = identity["path"]
    compiler_config = _compiler_config_sha256(resolved)
    key = compile_key(program.program_digest, souffle_sha256, COMPILE_FLAGS,
                      compiler_config_sha256=compiler_config)
    expected = {
        "schema": PROGRAM_SCHEMA, "compile_key": key, "program_digest": program.program_digest,
        "souffle_sha256": souffle_sha256, "souffle_version": _souffle_version(resolved),
        "compiler_config_sha256": compiler_config,
        "compile_flags": list(COMPILE_FLAGS),
    }
    entry = _entry_dir(cache_dir, key)
    cached = _cached_checker(entry, expected)
    if cached is not None:
        return cached
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SouffleUnavailable(
            f"compiled-checker cache directory is not usable: {cache_dir}: {exc}") from exc
    work = Path(tempfile.mkdtemp(prefix="capcov-souffle-compile-"))
    try:
        (work / "program.dl").write_text(program.program, encoding="utf-8")
        compiler = Path(resolved).resolve().parent / COMPILER_CONFIG_NAME
        _, generate_stderr, generate_seconds = _run_compile_step(
            [resolved, *GENERATE_FLAGS, GENERATED_SOURCE, "program.dl"],
            work=work, timeout=timeout)
        generated = work / GENERATED_SOURCE
        if not generated.is_file():
            raise CompileError("souffle -g returned 0 but wrote no checker.cpp: "
                               + generate_stderr[-4000:])
        try:
            canonical = _canonicalize_output_blocks(generated.read_text(encoding="utf-8"))
            generated.write_text(canonical, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CompileError(f"generated C++ could not be canonicalized: {exc}") from exc
        _, compile_stderr, native_seconds = _run_compile_step(
            [os.fspath(compiler), GENERATED_SOURCE, *NATIVE_FLAGS, NATIVE_OUTPUT], work=work,
            timeout=max(0.001, timeout - generate_seconds))
        compile_seconds = generate_seconds + native_seconds
        binary = work / NATIVE_OUTPUT
        if not binary.is_file():
            raise CompileError("souffle -o returned 0 but wrote no checker binary: "
                               + compile_stderr[-4000:])
        binary.chmod(0o755)
        checker = CompiledChecker(
            program_digest=program.program_digest, binary_path=os.fspath(entry / "checker"),
            binary_sha256=_sha256_file(binary), souffle_path=souffle_path,
            souffle_sha256=souffle_sha256, souffle_version=expected["souffle_version"],
            compile_flags=COMPILE_FLAGS, compile_key=key,
            compiler_config_sha256=expected["compiler_config_sha256"],
            compile_seconds=round(compile_seconds, 3))
        # The entry appears under its final name only once it is complete, and
        # a staging directory never outlives the attempt that created it.
        try:
            staging = Path(tempfile.mkdtemp(prefix=".compiled-", dir=cache_dir))
        except OSError as exc:
            raise SouffleUnavailable(
                f"compiled-checker cache directory is not usable: {cache_dir}: {exc}") from exc
        try:
            shutil.move(os.fspath(binary), staging / "checker")
            (staging / "provenance.json").write_text(
                json.dumps(checker.provenance(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
            if entry.exists():
                shutil.rmtree(entry, ignore_errors=True)
            os.replace(staging, entry)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            # A concurrent compile of the same key may have taken the entry
            # between the rmtree and the replace; its bytes are this binary's.
            concurrent = _cached_checker(entry, expected)
            if concurrent is not None:
                return concurrent
            raise SouffleUnavailable(
                f"compiled-checker cache entry could not be written: {entry}: {exc}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if prune:
        prune_cache(cache_dir, souffle_sha256)
    return checker


def run_compiled(bundle: Bundle, checker: CompiledChecker, *, timeout: float = MAX_SECONDS,
                 max_rows: int = MAX_ROWS, max_output_bytes: int = MAX_OUTPUT_BYTES,
                 max_processes: int = MAX_PROCESSES,
                 outputs: Iterable[str] | None = None,
                 _budget: _ExecutionBudget | None = None) -> SouffleResult:
    """Run ``checker`` on ``bundle``'s facts through the shared execution path.

    The bundle is validated and translated in full first (an invalid bundle is
    refused exactly as the interpreter refuses it), then its fact-independent
    program digest must equal the checker's or ``CompiledProgramMismatch`` is
    raised.  The compiled binary always emits every declared relation, so
    ``outputs`` may only name the full relation set.  The eligible-bundle
    re-run goes through the compiled binary again.  ``runtime`` is
    ``souffle-compiled:<binary sha256 prefix>``.
    """
    started = time.monotonic()
    all_names = tuple(decl.name for decl in bundle.relations)
    if outputs is not None and set(outputs) != set(all_names):
        raise ValueError("a compiled checker emits every declared relation; outputs must name them all")
    translated = translate_bundle(bundle, outputs=all_names)
    pack = program_for_pack(bundle)
    if pack.program_digest != checker.program_digest or translated.program_digest != checker.program_digest:
        raise CompiledProgramMismatch(
            f"bundle program {pack.program_digest[:16]} is not the checker's program "
            f"{checker.program_digest[:16]}")
    if _budget is None:
        if max_processes < 1:
            raise OverflowError("Souffle process limit must be positive")
        _budget = _ExecutionBudget(started + timeout, max_processes)

    def rerun(eligible: Bundle, budget: _ExecutionBudget) -> SouffleResult:
        return run_compiled(eligible, checker, timeout=timeout, max_rows=max_rows,
                            max_output_bytes=max_output_bytes, max_processes=max_processes,
                            _budget=budget)

    return _execute(bundle, translated, [checker.binary_path, "-j1"], budget=_budget,
                    limits=_Limits(timeout, max_rows, max_output_bytes, max_processes),
                    rerun=rerun, runtime=checker.runtime, started=started)


__all__ = ["COMPILE_FLAGS", "PROVENANCE_KEYS", "CompileError", "CompiledProgramMismatch",
           "CompiledChecker", "compile_key", "compile_program", "prune_cache", "run_compiled"]
