"""Souffle 2.5 backend for the restricted claims IR.

The backend is intentionally a small boundary: validation and semantics live in
the IR, while this module only translates a validated bundle, executes the
generated program, and returns normalized relation/claim observations.  It
does not read corpus expectations or call another evaluator.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping

from ..ir import (Atom, Bundle, Comparison, Constant, OutputKind, Rule, Term,
                 TypeName, Variable, canonical_dict, canonical_json, digest)
from ..output import output_triggered, relevant_evidence_ids
from ..validation import assert_valid
from ..verdicts import EvaluationBasis, EvaluationResult, OperationalStatus, SemanticVerdict, verdict


MAX_SECONDS = 30.0
MAX_ROWS = 100_000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_PROCESSES = 64
MAX_CACHE_ARTIFACT_BYTES = 32 * 1024 * 1024

# Bump when translation semantics or the serialized payload schema changes.
_TRANSLATION_CACHE_SCHEMA = "capcov-souffle-translation-v1"


class SouffleUnavailable(RuntimeError):
    """The requested Souffle executable could not be resolved or started."""


@dataclass
class _ExecutionBudget:
    deadline: float
    remaining_processes: int


@dataclass(frozen=True)
class SouffleProgram:
    bundle_digest: str
    program: str
    facts: Mapping[str, str]
    program_digest: str
    outputs: tuple[str, ...]
    runtime: str = "souffle-2.5"


@dataclass(frozen=True)
class SouffleResult:
    bundle_digest: str
    program_digest: str
    runtime: str
    relations: Mapping[str, tuple[tuple[Any, ...], ...]]
    claims: tuple[EvaluationResult, ...]
    output_digest: str
    evidence_digest: str
    elapsed_seconds: float


def _identifier(value: str) -> str:
    out = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value)
    return out if out and not out[0].isdigit() else "r_" + out


def _aliases(names):
    """Assign collision-free stable identifiers after Souffle sanitization."""
    result = {}
    used = set()
    for name in names:
        base = _identifier(name)
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        result[name] = candidate
    return result


def _stype(type_name: TypeName) -> str:
    if type_name in {TypeName.SYMBOL, TypeName.DIGEST, TypeName.JSON_METADATA_ONLY}:
        return "symbol"
    if type_name in {TypeName.INTEGER, TypeName.UNSIGNED, TypeName.TIMESTAMP}:
        return "number"
    if type_name is TypeName.BOOLEAN:
        return "number"
    raise ValueError(f"Souffle cannot represent {type_name.value}")


def _symbol_text(value: Any, type_name: TypeName | None) -> str:
    return canonical_json(value) if type_name is TypeName.JSON_METADATA_ONLY else str(value)


def _encode_symbol(value: Any, type_name: TypeName | None) -> str:
    """Use one reversible, TSV-safe representation in facts and rules.

    Hexadecimal UTF-8 preserves equality and Unicode scalar ordering while
    avoiding Souffle's file delimiters, quotes, and control-character grammar.
    """
    return "x" + _symbol_text(value, type_name).encode("utf-8").hex()


def _decode_symbol(value: str) -> str:
    if not value.startswith("x"):
        raise ValueError("malformed encoded Souffle symbol")
    try:
        return bytes.fromhex(value[1:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("malformed encoded Souffle symbol") from exc


def _quote(value: Any, type_name: TypeName | None = None) -> str:
    if type_name is TypeName.BOOLEAN or (type_name is None and isinstance(value, bool)):
        return "1" if value else "0"
    if (type_name in {TypeName.INTEGER, TypeName.UNSIGNED, TypeName.TIMESTAMP}
            or (type_name is None and isinstance(value, int))):
        return str(int(value))
    import json
    return json.dumps(_encode_symbol(value, type_name), ensure_ascii=False)


def _fact_field(value: Any, type_name: TypeName) -> str:
    """Serialize one TSV fact field using the rule literal encoding."""
    if type_name is TypeName.BOOLEAN:
        return "1" if value else "0"
    if type_name in {TypeName.INTEGER, TypeName.UNSIGNED, TypeName.TIMESTAMP}:
        return str(int(value))
    return _encode_symbol(value, type_name)


def _term(term: Term, columns, index: int, variable_aliases=None) -> str:
    column_type = columns[index].type
    if isinstance(term, Variable):
        return (variable_aliases or {}).get(term.name, _identifier(term.name))
    if isinstance(term, Constant):
        return _quote(term.value, column_type)
    raise TypeError(f"unsupported term {type(term).__name__}")


def _atom(atom: Atom, relation_map, relation_aliases=None, column_aliases=None, variable_aliases=None, term_overrides=None) -> str:
    relation = relation_map[atom.relation]
    relation_name = (relation_aliases or {}).get(atom.relation, _identifier(atom.relation))
    args = ", ".join((term_overrides or {}).get(i, _term(t, relation.columns, i, variable_aliases)) for i, t in enumerate(atom.terms))
    return ("!" if atom.negated else "") + relation_name + "(" + args + ")"


def _comparison(c: Comparison, variable_aliases=None) -> str:
    def simple(term):
        if isinstance(term, Variable): return (variable_aliases or {}).get(term.name, _identifier(term.name))
        if isinstance(term, Constant): return _quote(term.value, term.type)
        raise TypeError(type(term).__name__)
    return f"{simple(c.left)} {c.operator} {simple(c.right)}"


def _translated_output_names(bundle: Bundle, outputs: Iterable[str] | None) -> tuple[str, ...]:
    relation_map = {r.name: r for r in bundle.relations}
    requested_outputs = set(relation_map if outputs is None else outputs)
    unknown_outputs = requested_outputs - set(relation_map)
    if unknown_outputs:
        raise ValueError(f"unknown Souffle output relations: {sorted(unknown_outputs)}")
    # Claim folding may need a derived claim/domain/diagnostic/mapping relation
    # even when the caller omits it from the payload subset.  Primitive rows
    # are reconstructed from facts; only derived semantic inputs must be
    # emitted internally.
    semantic_names = {claim.relation for claim in bundle.claims}
    domains = {claim.domain for claim in bundle.claims if claim.domain}
    semantic_names.update(domains)
    semantic_names.update(decl.name for decl in bundle.relations
                          if decl.modality.value == "completeness"
                          and decl.completes in domains)
    semantic_names.update(mapping.evidence_relation for mapping in bundle.mappings)
    semantic_names.update(diagnostic.trigger_relation for diagnostic in bundle.diagnostics)
    requested_outputs.update(name for name in semantic_names
                             if name is not None and not relation_map[name].primitive)
    return tuple(sorted(requested_outputs))


def translate_bundle(bundle: Bundle, *, outputs: Iterable[str] | None = None) -> SouffleProgram:
    """Translate a validated bundle into deterministic Souffle source/facts."""
    assert_valid(bundle)
    relation_map = {r.name: r for r in bundle.relations}
    output_names = _translated_output_names(bundle, outputs)
    relation_aliases = _aliases([r.name for r in bundle.relations])
    column_aliases = {r.name: _aliases([c.name for c in r.columns]) for r in bundle.relations}
    lines = ["// Generated by capcov.claims.souffle; do not edit."]
    extra_decls = []
    extra_rules = []
    facts: dict[str, str] = {}
    for relation in bundle.relations:
        fields = ", ".join(f"{column_aliases[relation.name][c.name]}:{_stype(c.type)}" for c in relation.columns)
        lines.append(f".decl {relation_aliases[relation.name]}({fields})")
    for relation in bundle.relations:
        if relation.primitive:
            lines.append(f'.input {relation_aliases[relation.name]}(IO=file, filename="facts/{relation_aliases[relation.name]}.facts")')
        if relation.name in output_names:
            lines.append(f'.output {relation_aliases[relation.name]}(IO=file, filename="outputs/{relation_aliases[relation.name]}.csv")')
    for rule in bundle.rules:
        var_names = []
        for term in [*rule.head.terms, *(t for a in rule.body if isinstance(a, Atom) for t in a.terms)]:
            if isinstance(term, Variable) and term.name not in var_names: var_names.append(term.name)
        variable_aliases = _aliases(var_names)
        head = _atom(rule.head, relation_map, relation_aliases, column_aliases, variable_aliases)
        body = [_atom(a, relation_map, relation_aliases, column_aliases, variable_aliases) if isinstance(a, Atom) else _comparison(a, variable_aliases) for a in rule.body]
        if rule.aggregation is not None:
            agg = rule.aggregation
            source = relation_map[agg.relation]
            source_atom = next((a for a in rule.body if isinstance(a, Atom) and a.relation == agg.relation and not a.negated), None)
            if source_atom is None:
                raise ValueError(f"aggregation {agg.name} has no positive source atom")
            value_index = [c.name for c in source.columns].index(agg.value_variable)
            original_source_text = _atom(source_atom, relation_map, relation_aliases, column_aliases, variable_aliases)
            grouped = set(agg.group_by) | set(source.context_indices)
            aggregate_overrides = {i: _identifier(f"__capcov_{agg.name}_{column.name}")
                                   for i, column in enumerate(source.columns) if column.name not in grouped}
            source_text = _atom(source_atom, relation_map, relation_aliases, column_aliases,
                                variable_aliases, aggregate_overrides)
            value = aggregate_overrides.get(value_index, variable_aliases.get(agg.value_variable,
                                                                              _identifier(agg.value_variable)))
            if agg.operator == "count": expression = f"count : {source_text}"
            elif agg.operator in {"sum", "min", "max"}:
                op = agg.operator
                expression = f"{op} {value} : {source_text}"
            else:
                if agg.operator not in {"any", "all"}:
                    raise NotImplementedError(f"unsupported Souffle aggregation {agg.operator!r}")
                if source.columns[value_index].type is not TypeName.BOOLEAN:
                    raise ValueError("any/all aggregation requires a boolean value_variable")
                # Boolean aggregation is represented by a generated count
                # helper.  A true row is emitted only when the finite source
                # has at least one member (any), or exactly the finite domain
                # cardinality (all).  This avoids pretending Souffle's count
                # result is a Boolean value.
                helper = _identifier("__capcov_agg_" + agg.name)
                helper = _aliases([helper])[helper]
                source_columns = {column.name: column.type for column in source.columns}
                source_names = [column.name for column in source.columns]
                group_names = list(dict.fromkeys((*agg.group_by, *source.context_indices)))
                group_terms = [_term(source_atom.terms[source_names.index(name)], source.columns,
                                     source_names.index(name), variable_aliases) for name in group_names]
                group_types = [_stype(source_columns[name]) for name in group_names]
                helper_fields = [f"g{i}:{kind}" for i, kind in enumerate(group_types)] + ["n_true:number", "n_total:number"]
                helper_body = [x for x in body if x != original_source_text]
                true_overrides = dict(aggregate_overrides); true_overrides[value_index] = "1"
                true_source = _atom(source_atom, relation_map, relation_aliases, column_aliases,
                                    variable_aliases, true_overrides)
                helper_args = [*group_terms, "n_true", "n_total"]
                if agg.operator == "any":
                    helper_body.extend([f"n_total = count : {source_text}",
                                        f"n_true = count : {true_source}"])
                else:
                    domain_atom = next((a for a in rule.body if isinstance(a, Atom)
                                        and a.relation == agg.domain and not a.negated), None)
                    if domain_atom is None:
                        raise ValueError("all aggregation requires a domain atom")
                    domain_decl = relation_map[agg.domain]
                    domain_names = [column.name for column in domain_decl.columns]
                    # Cardinality alone is unsound: source keys {a,c} and
                    # domain keys {a,b} have equal sizes.  Project both sets to
                    # their shared identity columns, count their intersection,
                    # and separately reject any false source row.  Projection
                    # relations also collapse source-only dimensions exactly
                    # like the Python evaluator's set projection.
                    shared_names = [name for name in domain_names
                                    if name in source_names and name != agg.value_variable]
                    member_names = [name for name in shared_names if name not in group_names]
                    source_keys = _identifier(helper + "_source_keys")
                    domain_keys = _identifier(helper + "_domain_keys")
                    shared_keys = _identifier(helper + "_shared_keys")
                    key_names = [*group_names, *member_names]
                    key_types = [_stype(source_columns[name]) for name in key_names]
                    key_fields = ", ".join(f"k{i}:{kind}" for i, kind in enumerate(key_types))
                    extra_decls.extend((f".decl {source_keys}({key_fields})",
                                        f".decl {domain_keys}({key_fields})",
                                        f".decl {shared_keys}({key_fields})"))
                    source_key_terms = [_term(source_atom.terms[source_names.index(name)], source.columns,
                                              source_names.index(name), variable_aliases)
                                        for name in key_names]
                    domain_key_terms = [_term(domain_atom.terms[domain_names.index(name)], domain_decl.columns,
                                              domain_names.index(name), variable_aliases)
                                        for name in key_names]
                    source_key_call = ", ".join(source_key_terms)
                    domain_key_call = ", ".join(domain_key_terms)
                    key_variables = [_identifier(f"__capcov_{agg.name}_key_{i}")
                                     for i in range(len(key_names))]
                    key_variable_call = ", ".join(key_variables)
                    extra_rules.extend((
                        f"{source_keys}({source_key_call}) :- {original_source_text}.",
                        f"{domain_keys}({domain_key_call}) :- "
                        f"{_atom(domain_atom, relation_map, relation_aliases, column_aliases, variable_aliases)}.",
                        f"{shared_keys}({key_variable_call}) :- {source_keys}({key_variable_call}), "
                        f"{domain_keys}({key_variable_call}).",
                    ))
                    count_tail = ["_" for _ in member_names]
                    source_count_call = ", ".join([*group_terms, *count_tail])
                    false_overrides = dict(aggregate_overrides); false_overrides[value_index] = "0"
                    false_source = _atom(source_atom, relation_map, relation_aliases, column_aliases,
                                         variable_aliases, false_overrides)
                    helper_fields.extend(("n_domain:number", "n_shared:number", "n_false:number"))
                    helper_body.extend((f"n_total = count : {source_keys}({source_count_call})",
                                        f"n_true = count : {true_source}",
                                        f"n_domain = count : {domain_keys}({source_count_call})",
                                        f"n_shared = count : {shared_keys}({source_count_call})",
                                        f"n_false = count : {false_source}"))
                    helper_args.extend(("n_domain", "n_shared", "n_false"))
                extra_decls.append(f".decl {helper}({', '.join(helper_fields)})")
                helper_call = ", ".join(helper_args)
                extra_rules.append(f"{helper}({helper_call}) :- {', '.join(helper_body)}.")
                body = [x for x in body if x != original_source_text]
                body.append(f"{helper}({helper_call})")
                false_bodies = []
                if agg.operator == "any":
                    false_bodies.append([*body, "n_true = 0"])
                    body.append("n_true > 0")
                else:
                    false_bodies.extend(([*body, "n_total != n_domain"],
                                         [*body, "n_shared != n_domain"],
                                         [*body, "n_false > 0"]))
                    body.extend(["n_domain > 0", "n_total = n_domain",
                                 "n_shared = n_domain", "n_false = 0"])
                head_names = [c.name for c in relation_map[rule.head.relation].columns]
                if agg.value_variable in head_names:
                    target_i = head_names.index(agg.value_variable)
                    false_head = _atom(rule.head, relation_map, relation_aliases, column_aliases,
                                       variable_aliases, {target_i: "0"})
                    for false_body in false_bodies:
                        lines.append(f"{false_head} :- {', '.join(false_body)}.")
                    head = _atom(rule.head, relation_map, relation_aliases, column_aliases,
                                 variable_aliases, {target_i: "1"})
                lines.append(f"{head} :- {', '.join(body)}.")
                continue
            # Souffle's aggregate is a body constraint whose result binds the
            # head value variable.  Remove the source atom from the ordinary
            # body; the aggregate expression supplies it.
            body = [x for x in body if x != original_source_text]
            head_term = rule.head.terms[[c.name for c in relation_map[rule.head.relation].columns].index(agg.value_variable)]
            head_value = variable_aliases.get(head_term.name, _identifier(head_term.name)) if isinstance(head_term, Variable) else value
            body.append(f"{head_value} = {expression}")
        lines.append(f"{head} :- {', '.join(body)}.")
    lines.extend(extra_decls)
    lines.extend(extra_rules)
    program = "\n".join(lines) + "\n"
    for relation in bundle.relations:
        if not relation.primitive and any(f.relation == relation.name for f in bundle.facts):
            raise ValueError(f"facts cannot target non-primitive relation {relation.name!r}")
        rows = []
        for fact in bundle.facts:
            if fact.relation != relation.name:
                continue
            rows.append("\t".join(_fact_field(t.value, relation.columns[i].type) if isinstance(t, Constant) else "" for i, t in enumerate(fact.terms)))
        facts[relation_aliases[relation.name]] = "\n".join(sorted(rows)) + ("\n" if rows else "")
    bundle_digest = digest(bundle)
    program_digest = hashlib.sha256(program.encode()).hexdigest()
    return SouffleProgram(bundle_digest, program, facts, program_digest, output_names)


def _parse_value(raw: str, type_name: TypeName) -> Any:
    if type_name is TypeName.BOOLEAN: return bool(int(raw))
    if type_name in {TypeName.INTEGER, TypeName.UNSIGNED, TypeName.TIMESTAMP}: return int(raw)
    decoded = _decode_symbol(raw)
    if type_name is TypeName.JSON_METADATA_ONLY:
        import json
        return json.loads(decoded)
    return decoded


def _executable_identity(resolved: str) -> Mapping[str, Any] | None:
    """Return a content identity, or disable caching if it cannot be read safely."""
    try:
        path = Path(resolved).resolve(strict=True)
        stat = path.stat()
        content_digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                content_digest.update(chunk)
    except OSError:
        return None
    return {"path": os.fspath(path), "size": stat.st_size,
            "mode": stat.st_mode, "sha256": content_digest.hexdigest()}


def _translation_cache_basis(bundle_digest: str, outputs: tuple[str, ...],
                             executable_identity: Mapping[str, Any], *,
                             timeout: float, max_rows: int, max_output_bytes: int,
                             max_processes: int) -> Mapping[str, Any]:
    return {
        "schema": _TRANSLATION_CACHE_SCHEMA,
        "bundle_digest": bundle_digest,
        "outputs": list(outputs),
        "executable": dict(executable_identity),
        "limits": {"timeout": timeout, "max_rows": max_rows,
                   "max_output_bytes": max_output_bytes,
                   "max_processes": max_processes},
    }


def _translation_cache_key(basis: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()


def _cached_translation(cache_dir: str | os.PathLike[str], key: str,
                        basis: Mapping[str, Any], expected_outputs: tuple[str, ...],
                        expected_fact_names: set[str]) -> SouffleProgram | None:
    path = Path(cache_dir) / f"translation-{key}.json"
    try:
        with path.open("rb") as stream:
            encoded = stream.read(MAX_CACHE_ARTIFACT_BYTES + 1)
        if len(encoded) > MAX_CACHE_ARTIFACT_BYTES:
            return None
        payload = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None
    if (not isinstance(payload, dict)
            or set(payload) != {"schema", "key", "basis", "program", "artifact_digest"}):
        return None
    if (payload["schema"] != _TRANSLATION_CACHE_SCHEMA or payload["key"] != key
            or payload["basis"] != basis or not isinstance(payload["program"], dict)):
        return None
    program = payload["program"]
    if set(program) != {"bundle_digest", "program", "facts", "program_digest", "outputs", "runtime"}:
        return None
    facts = program["facts"]
    outputs = program["outputs"]
    if (program["bundle_digest"] != basis["bundle_digest"]
            or not isinstance(program["program"], str)
            or not isinstance(program["program_digest"], str)
            or program["runtime"] != "souffle-2.5"
            or not isinstance(facts, dict) or set(facts) != expected_fact_names
            or any(not isinstance(name, str) or not isinstance(value, str)
                   for name, value in facts.items())
            or not isinstance(outputs, list) or tuple(outputs) != expected_outputs
            or any(not isinstance(name, str) for name in outputs)):
        return None
    try:
        artifact_digest = hashlib.sha256(
            canonical_json(program).encode("utf-8")).hexdigest()
        actual_digest = hashlib.sha256(program["program"].encode("utf-8")).hexdigest()
    except (TypeError, UnicodeError, RecursionError):
        return None
    if (not isinstance(payload["artifact_digest"], str)
            or artifact_digest != payload["artifact_digest"]):
        return None
    if actual_digest != program["program_digest"]:
        return None
    return SouffleProgram(program["bundle_digest"], program["program"], facts,
                          program["program_digest"], tuple(outputs), program["runtime"])


def _store_translation(cache_dir: str | os.PathLike[str], key: str,
                       basis: Mapping[str, Any], translated: SouffleProgram) -> None:
    program = {
        "bundle_digest": translated.bundle_digest,
        "program": translated.program,
        "facts": dict(translated.facts),
        "program_digest": translated.program_digest,
        "outputs": list(translated.outputs),
        "runtime": translated.runtime,
    }
    payload = {
        "schema": _TRANSLATION_CACHE_SCHEMA,
        "key": key,
        "basis": basis,
        "program": program,
        "artifact_digest": hashlib.sha256(
            canonical_json(program).encode("utf-8")).hexdigest(),
    }
    encoded = canonical_json(payload).encode("utf-8")
    if len(encoded) > MAX_CACHE_ARTIFACT_BYTES:
        return
    directory = Path(cache_dir)
    temporary: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".translation-", suffix=".tmp",
                                                       dir=directory)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / f"translation-{key}.json")
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


@dataclass(frozen=True)
class _Limits:
    timeout: float
    max_rows: int
    max_output_bytes: int
    max_processes: int


def _execute(bundle: Bundle, translated: SouffleProgram, argv: list[str], *,
             budget: _ExecutionBudget, limits: _Limits,
             rerun: Callable[[Bundle, _ExecutionBudget], SouffleResult],
             runtime: str | None = None, started: float | None = None) -> SouffleResult:
    """Execute ``argv`` in a temp root holding ``translated`` and fold the outputs.

    This is the one execution path every Souffle kernel shares: the temp root
    with ``program.dl`` and ``facts/*.facts``, the Popen/poll/budget loop, the
    ``outputs/*.csv`` parse, normalization and claim folding.  ``argv`` is the
    interpreter (``souffle --jobs 1 program.dl``) or a compiled checker binary
    (``checker -j1``); the process is started with ``cwd`` = the temp root so
    the program's relative IO paths resolve identically for both.  ``rerun`` is
    called for the eligible-bundle recursion so a compiled kernel re-runs the
    compiled binary, never the interpreter.  Only the return code decides
    failure: both runtimes write warnings to stderr with rc 0.
    """
    if started is None:
        started = time.monotonic()
    relation_map = {r.name: r for r in bundle.relations}
    relation_aliases = _aliases([r.name for r in bundle.relations])
    root = Path(tempfile.mkdtemp(prefix="capcov-souffle-"))
    try:
        (root / "facts").mkdir(); (root / "outputs").mkdir()
        (root / "program.dl").write_text(translated.program, encoding="utf-8")
        for relation in bundle.relations:
            alias = relation_aliases[relation.name]
            (root / "facts" / f"{alias}.facts").write_text(translated.facts[alias], encoding="utf-8")
        input_rows = len(bundle.facts)
        if input_rows > limits.max_rows:
            raise OverflowError(f"Souffle input rows exceed {limits.max_rows}")
        if budget.remaining_processes <= 0:
            raise OverflowError(f"Souffle evaluation exceeded {limits.max_processes} processes")
        if time.monotonic() > budget.deadline:
            raise TimeoutError(f"Souffle evaluation exceeded {limits.timeout:.1f}s total")
        budget.remaining_processes -= 1
        try:
            proc = subprocess.Popen(argv, cwd=root,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            raise SouffleUnavailable(f"Souffle executable could not be started: {argv[0]}: {exc}") from exc
        while proc.poll() is None:
            output_bytes = sum(p.stat().st_size for p in (root / "outputs").glob("*") if p.is_file())
            if output_bytes > limits.max_output_bytes:
                proc.kill(); proc.communicate()
                raise OverflowError(f"Souffle outputs exceed {limits.max_output_bytes} bytes during execution")
            if time.monotonic() > budget.deadline:
                proc.kill(); proc.communicate()
                raise TimeoutError(f"Souffle evaluation exceeded {limits.timeout:.1f}s total")
            time.sleep(0.005)
        stdout, stderr = proc.communicate()
        if proc.returncode:
            raise RuntimeError((stderr or stdout)[-4000:])
        output_bytes = sum(p.stat().st_size for p in (root / "outputs").glob("*") if p.is_file())
        if output_bytes > limits.max_output_bytes:
            raise OverflowError(f"Souffle outputs exceed {limits.max_output_bytes} bytes")
        relations: dict[str, tuple[tuple[Any, ...], ...]] = {}
        total_rows = input_rows
        fact_rows: dict[str, list[tuple[Any, ...]]] = {name: [] for name in relation_map}
        for fact in bundle.facts:
            fact_rows[fact.relation].append(tuple(term.value for term in fact.terms if isinstance(term, Constant)))
        for name, relation in relation_map.items():
            path = root / "outputs" / f"{relation_aliases[name]}.csv"
            rows = []
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if relation.arity == 0:
                        rows.append(())
                        continue
                    if not line: continue
                    fields = line.split("\t")
                    if len(fields) != relation.arity: raise ValueError(f"malformed output row for {name}")
                    rows.append(tuple(_parse_value(v, c.type) for v, c in zip(fields, relation.columns)))
            # Primitive inputs omitted from the requested output subset remain
            # part of the normalized relation model and resource accounting.
            if name not in translated.outputs and relation.primitive:
                rows = fact_rows[name]
            if name in translated.outputs and relation.primitive:
                # Output rows include inputs; they were already counted above.
                total_rows += max(0, len(rows) - len(fact_rows[name]))
            else:
                total_rows += len(rows) if not relation.primitive else 0
            if total_rows > limits.max_rows: raise OverflowError(f"Souffle rows exceed {limits.max_rows}")
            # JSON metadata values decode to mappings and are intentionally not
            # hashable.  Canonical keys preserve set semantics without mutating
            # or stringifying the public normalized values.
            unique = {canonical_json(row): row for row in rows}
            relations[name] = tuple(unique[key] for key in sorted(unique))
        output_digest = hashlib.sha256(b"".join((k + "=" + repr(v)).encode() for k, v in sorted(relations.items()))).hexdigest()
        claim_results = []
        eligible_cache: dict[str, Mapping[str, tuple[tuple[Any, ...], ...]]] = {}
        for claim in bundle.claims:
            eligible = _eligible_bundle(claim, bundle, relations, relation_map)
            semantic_relations = relations
            if eligible is not None:
                # Souffle independently recomputes closure after removing only
                # fact propositions whose every producer path depends on the
                # claim's forbidden assumption.  This preserves an independent
                # producer for the same proposition and avoids claim-global
                # revocation based on unrelated evidence.
                eligible_digest = digest(eligible)
                semantic_relations = eligible_cache.get(eligible_digest)
                if semantic_relations is None:
                    semantic_relations = rerun(eligible, budget).relations
                    eligible_cache[eligible_digest] = semantic_relations
            claim_results.append(_claim_result(claim, relation_map[claim.relation],
                                               semantic_relations, relations,
                                               relation_map, bundle))
        claim_results = tuple(claim_results)
        if runtime is None:
            runtime = next((line.strip() for line in stdout.splitlines() if line.strip().startswith("Version:")), translated.runtime)
        evidence_digest = hashlib.sha256((translated.bundle_digest + translated.program_digest + runtime + output_digest).encode()).hexdigest()
        return SouffleResult(translated.bundle_digest, translated.program_digest, runtime, relations, claim_results, output_digest, evidence_digest, time.monotonic() - started)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def run_bundle(bundle: Bundle, *, executable: str = "souffle", timeout: float = MAX_SECONDS,
               max_rows: int = MAX_ROWS, max_output_bytes: int = MAX_OUTPUT_BYTES,
               max_processes: int = MAX_PROCESSES,
               outputs: Iterable[str] | None = None,
               cache_dir: str | os.PathLike[str] | None = None,
               _budget: _ExecutionBudget | None = None) -> SouffleResult:
    """Run Souffle with one cumulative deadline/process budget per evaluation.

    ``cache_dir`` opts into a bounded, validated, atomic cache of translated
    source and fact artifacts.  Normalized run results are deliberately not
    cached: executable bytes do not identify dynamic libraries or hidden
    environment inputs strongly enough to make result reuse correctness-safe.
    """
    resolved = shutil.which(executable)
    if resolved is None:
        raise SouffleUnavailable(f"Souffle executable not found: {executable}")
    started = time.monotonic()
    if _budget is None:
        if max_processes < 1:
            raise OverflowError("Souffle process limit must be positive")
        _budget = _ExecutionBudget(started + timeout, max_processes)
    if cache_dir is None:
        translated = translate_bundle(bundle, outputs=outputs)
    else:
        assert_valid(bundle)
        output_names = _translated_output_names(bundle, outputs)
        bundle_digest = digest(bundle)
        executable_identity = _executable_identity(resolved)
        translated = None
        if executable_identity is not None:
            basis = _translation_cache_basis(
                bundle_digest, output_names, executable_identity,
                timeout=timeout, max_rows=max_rows,
                max_output_bytes=max_output_bytes, max_processes=max_processes)
            cache_key = _translation_cache_key(basis)
            fact_names = set(_aliases([r.name for r in bundle.relations]).values())
            translated = _cached_translation(cache_dir, cache_key, basis,
                                               output_names, fact_names)
        if translated is None:
            translated = translate_bundle(bundle, outputs=output_names)
            if executable_identity is not None:
                _store_translation(cache_dir, cache_key, basis, translated)

    def rerun(eligible: Bundle, budget: _ExecutionBudget) -> SouffleResult:
        return run_bundle(
            eligible, executable=resolved, timeout=timeout,
            max_rows=max_rows, max_output_bytes=max_output_bytes,
            max_processes=max_processes,
            outputs=(decl.name for decl in eligible.relations),
            cache_dir=cache_dir,
            _budget=budget,
        )

    return _execute(bundle, translated, [resolved, "--jobs", "1", "program.dl"],
                    budget=_budget,
                    limits=_Limits(timeout, max_rows, max_output_bytes, max_processes),
                    rerun=rerun, started=started)


def program_for_pack(bundle: Bundle) -> SouffleProgram:
    """The fact-independent Souffle program of ``bundle``'s declarations and rules.

    Facts, evidence, claims, mappings, diagnostics and output templates are
    stripped and every declared relation is an output, so the program text --
    and therefore ``program_digest`` -- is a function of the relation
    declarations (in bundle order) and the rules alone.  Two bundles built
    from the same schema and rule pack through the same combiner share one
    program; that program is what a compiled checker is built from.
    """
    stripped = replace(bundle, facts=(), evidence=(), claims=(), mappings=(),
                       diagnostics=(), outputs=())
    return translate_bundle(stripped, outputs=tuple(decl.name for decl in bundle.relations))


# Identity schema of a compiled checker (``capcov.claims.souffle.compile``).
PROGRAM_SCHEMA = "capcov-souffle-compiled-v1"


def _claim_matches(row, terms, relation, context):
    bindings = {}
    columns = {column.name: i for i, column in enumerate(relation.columns)}
    for i, term in enumerate(terms):
        if isinstance(term, Constant):
            if row[i] != term.value:
                return False
        elif isinstance(term, Variable):
            if term.name in bindings and bindings[term.name] != row[i]:
                return False
            bindings[term.name] = row[i]
    for name, value in context.items():
        if name in columns and row[columns[name]] != value:
            return False
    return True


def _claim_values(claim, relation):
    values = dict(claim.context.as_dict())
    for column, term in zip(relation.columns, claim.terms):
        if isinstance(term, Constant): values[column.name] = term.value
    return values


def _mapping_matches(claim, mapping, relations, relation_map,
                     variable_values=None):
    """Return source rows and the claim-variable environment they extend."""
    claim_relation = relation_map[claim.relation]
    claim_names = [column.name for column in claim_relation.columns]
    claim_values = _claim_values(claim, claim_relation)
    source = relation_map[mapping.evidence_relation]
    names = [column.name for column in source.columns]
    matched = []
    for row in relations.get(mapping.evidence_relation, ()):
        values = dict(zip(names, row)); next_variables = dict(variable_values or {}); ok = True
        for left, right in mapping.bindings:
            if left not in claim_names or right not in values:
                ok = False; break
            value = values[right]; term = claim.terms[claim_names.index(left)]
            if isinstance(term, Constant) and value != term.value:
                ok = False; break
            if isinstance(term, Variable):
                previous = next_variables.setdefault(term.name, value)
                if previous != value:
                    ok = False; break
            if left in claim_values and value != claim_values[left]:
                ok = False; break
        if not ok:
            continue
        if any(values[name] != value for name, value in claim.context.as_dict().items() if name in values):
            continue
        matched.append((row, next_variables))
    return matched


def _mapping_conjunction_matches(claim, mappings, relations, relation_map,
                                 position=0, variable_values=None):
    """Relationally join every support mapping through claim variables."""
    if position == len(mappings):
        return True
    mapping = mappings[position]
    return any(_mapping_conjunction_matches(
                   claim, mappings, relations, relation_map, position + 1,
                   next_variables)
               for _, next_variables in _mapping_matches(
                   claim, mapping, relations, relation_map, variable_values))


def _declared_missing_premises(bundle, claim, relation, relation_map, fallback,
                               claim_state=SemanticVerdict.UNRESOLVED):
    values = _claim_values(claim, relation)
    active_evidence = relevant_evidence_ids(
        bundle, claim.id, {record.id for record in bundle.evidence},
        scoped_claim=claim)
    evidence_values = {}
    for record in bundle.evidence:
        declaration = relation_map.get(record.atom.relation)
        row_values = {"id": record.id}
        if declaration is not None:
            row_values.update(
                (column.name, term.value)
                for column, term in zip(declaration.columns, record.atom.terms)
                if isinstance(term, Constant))
        evidence_values[record.id] = row_values
    declared = []
    for output in bundle.outputs:
        if (output.claim_id != claim.id
                or output.kind != OutputKind.MISSING_PREMISE
                or not output_triggered(
                    output, active_evidence,
                    (claim_state.value, "underived"), bundle=bundle,
                    scoped_claim=claim)):
            continue
        item = {"relation": output.relation}; complete = True
        for name, template in output.fields:
            if template.source == "constant":
                item[name] = canonical_dict(template.value)
            elif template.source == "claim" and template.column in values:
                item[name] = canonical_dict(values[template.column])
            elif template.source == "evidence":
                evidence_id = template.evidence_id or output.evidence_id or ""
                source_values = evidence_values.get(evidence_id, {})
                if evidence_id not in active_evidence or template.column not in source_values:
                    complete = False
                    break
                item[name] = canonical_dict(source_values[template.column])
            else:
                complete = False
                break
        if complete:
            declared.append(item)
    if not declared:
        return fallback
    unique = {canonical_json(item): item for item in declared}
    return tuple(unique[key] for key in sorted(unique))


def _predicate_matches(row, relation, predicate):
    if not predicate: return True
    values = dict(zip((column.name for column in relation.columns), row))
    actual = values[predicate["column"]]; expected = predicate.get("value")
    return {"=": actual == expected, "!=": actual != expected,
            "in": actual in expected if isinstance(expected, (list, tuple)) else False,
            "not-in": actual not in expected if isinstance(expected, (list, tuple)) else False,
            "exists": actual is not None}.get(predicate["operator"], False)


def _diagnostic_rows(claim, diagnostic, relations, relation_map):
    source = relation_map[diagnostic.trigger_relation]
    values = _claim_values(claim, relation_map[claim.relation])
    names = [column.name for column in source.columns]
    scoped_names = set(diagnostic.context_indices) | (set(names) & set(values))
    return [row for row in relations.get(source.name, ())
            if all(name in values and dict(zip(names, row))[name] == values[name]
                   for name in scoped_names)
            and _predicate_matches(row, source, dict(diagnostic.predicate))]


def _diagnostic_active(claim, diagnostic, relations, relation_map):
    rows = _diagnostic_rows(claim, diagnostic, relations, relation_map)
    return not rows if diagnostic.when_missing else bool(rows)


def _atom_key(atom):
    return atom.relation, canonical_json(tuple(term.value if isinstance(term, Constant) else None
                                                for term in atom.terms))


def _eligible_bundle(claim, bundle, diagnostic_relations, relation_map):
    blocked_rows = set()
    for diagnostic in bundle.diagnostics:
        if (diagnostic.claim_id != claim.id
                or diagnostic.effect.value not in {"forbidden", "refutation"}
                or relation_map[diagnostic.trigger_relation].modality.value != "assumption"
                or diagnostic.when_missing):
            continue
        blocked_rows.update((diagnostic.trigger_relation, canonical_json(row))
                            for row in _diagnostic_rows(claim, diagnostic, diagnostic_relations,
                                                        relation_map))
    blocked = {record.id for record in bundle.evidence
               if _atom_key(record.atom) in blocked_rows}
    if not blocked:
        return None
    by_id = {record.id: record for record in bundle.evidence}

    def allowed(record):
        pending = [record.id]; seen = set(pending)
        while pending:
            dependency = pending.pop()
            parent = by_id.get(dependency)
            if parent:
                for item in parent.depends_on:
                    if item not in seen:
                        seen.add(item); pending.append(item)
        return not bool(seen & blocked)

    eligible_keys = {_atom_key(record) if isinstance(record, Atom) else _atom_key(record.atom)
                     for record in bundle.evidence if allowed(record)}
    facts = tuple(fact for fact in bundle.facts if _atom_key(fact) in eligible_keys)
    # Evidence controls fact eligibility above.  The filtered closure bundle
    # keeps exactly the allowed records: attribution is unconditional
    # (``fact-without-evidence``), and an allowed record depends only on other
    # allowed records or external ids, so no forbidden dependency is retained
    # merely to describe the surviving fact set.
    evidence = tuple(record for record in bundle.evidence if allowed(record))
    return replace(bundle, facts=facts, evidence=evidence, claims=(), mappings=(),
                   diagnostics=(), outputs=())


def _rule_may_head_claim(rule, claim, relation):
    if rule.head.relation != claim.relation:
        return False
    known = _claim_values(claim, relation)
    variables = {}
    for column, term in zip(relation.columns, rule.head.terms):
        if column.name not in known:
            continue
        expected = known[column.name]
        if isinstance(term, Constant) and term.value != expected:
            return False
        if isinstance(term, Variable):
            previous = variables.setdefault(term.name, expected)
            if previous != expected:
                return False
    return True


def _exists_state(claim, relation, relations, diagnostic_relations, relation_map, bundle):
    rows = relations.get(claim.relation, ())
    direct = any(_claim_matches(row, claim.terms, relation, claim.context.as_dict()) for row in rows)
    support = direct and relation.polarity.value == "positive"
    refutation = direct and relation.polarity.value == "negative"
    support_mappings = [mapping for mapping in bundle.mappings
                        if mapping.claim_id == claim.id and mapping.effect.value == "support"]
    has_claim_rules = any(_rule_may_head_claim(rule, claim, relation)
                          for rule in bundle.rules)
    if (not support and support_mappings
            and (not has_claim_rules or direct)):
        support = _mapping_conjunction_matches(
            claim, tuple(support_mappings), relations, relation_map)
    refutation = refutation or any(bool(_mapping_matches(
                                       claim, mapping, relations, relation_map))
                                   for mapping in bundle.mappings
                                   if mapping.claim_id == claim.id and mapping.effect.value == "refutation")
    status = OperationalStatus.COMPLETE
    for diagnostic in bundle.diagnostics:
        if diagnostic.claim_id != claim.id or diagnostic.operational_status == "complete": continue
        if not _diagnostic_active(claim, diagnostic, diagnostic_relations, relation_map): continue
        trigger = relation_map[diagnostic.trigger_relation]
        if trigger.modality.value == "assumption":
            if diagnostic.effect.value == "forbidden": continue
            if support: continue
        status = OperationalStatus(diagnostic.operational_status); break
    semantic = verdict(support, refutation)
    missing = (() if support or refutation else
               _declared_missing_premises(
                   bundle, claim, relation, relation_map,
                   (f"claim:{claim.relation}:{canonical_json(claim.context.as_dict())}",),
                   semantic))
    return EvaluationResult(semantic, status, EvaluationBasis.DERIVATIONAL,
                            missing_premises=missing)


def _claim_result(claim, relation, relations, diagnostic_relations, relation_map, bundle):
    if claim.quantifier.value != "forall":
        return _exists_state(claim, relation, relations, diagnostic_relations, relation_map, bundle)
    domain_decl = relation_map[claim.domain]
    domain_names = [column.name for column in domain_decl.columns]
    values = _claim_values(claim, relation)
    shared_values = {name: value for name, value in values.items()
                     if name in domain_names}
    # Domain membership is claim input, not a conclusion that revocation may
    # silently shrink.  Quantify over the full closure and separately require
    # every member to remain claim-eligible, mirroring the provenance kernel.
    domain = [row for row in diagnostic_relations.get(claim.domain, ())
              if all(dict(zip(domain_names, row))[name] == value
                     for name, value in shared_values.items())]
    closure_decls = [decl for decl in relation_map.values()
                     if decl.modality.value == "completeness"
                     and decl.completes == domain_decl.name
                     and decl.context_indices == domain_decl.context_indices
                     and tuple(column.name for column in decl.columns)
                     == domain_decl.context_indices]
    closure_available = any(
        all(dict(zip((column.name for column in closure.columns), row))[name]
            == shared_values[name]
            for name in closure.context_indices if name in shared_values)
        for closure in closure_decls
        for row in relations.get(closure.name, ()))
    if not closure_available:
        return EvaluationResult(SemanticVerdict.UNRESOLVED, OperationalStatus.COMPLETE,
                                EvaluationBasis.BOUNDED_HISTORY_MODEL,
                                missing_premises=_declared_missing_premises(
                                    bundle, claim, relation, relation_map,
                                    (f"closure:{claim.domain}",)),
                                message="universal domain has no claim-eligible completeness witness")
    if not domain:
        return EvaluationResult(SemanticVerdict.UNRESOLVED, OperationalStatus.INCONSISTENT_PREMISES,
                                EvaluationBasis.BOUNDED_HISTORY_MODEL,
                                missing_premises=_declared_missing_premises(
                                    bundle, claim, relation, relation_map,
                                    (f"domain:{claim.domain}",)),
                                message="universal domain is empty in claim context")
    eligible_domain = {canonical_json(row)
                       for row in relations.get(claim.domain, ())}
    missing_member = next((row for row in domain
                           if canonical_json(row) not in eligible_domain), None)
    if missing_member is not None:
        return EvaluationResult(
            SemanticVerdict.UNRESOLVED, OperationalStatus.COMPLETE,
            EvaluationBasis.BOUNDED_HISTORY_MODEL,
            missing_premises=(
                f"domain-evidence:{claim.domain}:{canonical_json(missing_member)}",),
            message="universal domain member has no claim-eligible provenance")
    subresults = []
    positions = {column.name: i for i, column in enumerate(domain_decl.columns)}
    for row in domain:
        terms = tuple(Constant(row[positions[term.name]], relation.columns[i].type)
                      if isinstance(term, Variable) and term.name in positions else term
                      for i, term in enumerate(claim.terms))
        from ..ir import Claim
        subclaim = Claim(claim.relation, terms, claim.context, "exists", None, claim.id)
        subresults.append(_exists_state(subclaim, relation, relations, diagnostic_relations,
                                        relation_map, bundle))
    support = all(result.semantic in {SemanticVerdict.SUPPORTED, SemanticVerdict.CONFLICTING}
                  for result in subresults)
    refutation = any(result.semantic in {SemanticVerdict.REFUTED, SemanticVerdict.CONFLICTING}
                     for result in subresults)
    status = next((result.operational for result in subresults
                   if result.operational != OperationalStatus.COMPLETE), OperationalStatus.COMPLETE)
    subresult_missing = tuple(item for result in subresults
                              for item in result.missing_premises)
    semantic = verdict(support, refutation)
    # Each subresult has already rendered outputs against its grounded domain
    # member.  Applying templates again to the open FORALL claim would allow
    # evidence for one member to satisfy another member's trigger.
    if support or refutation:
        missing = ()
    else:
        unique_missing = {canonical_json(item): item for item in subresult_missing}
        missing = tuple(unique_missing[key] for key in sorted(unique_missing))
    return EvaluationResult(semantic, status,
                            EvaluationBasis.BOUNDED_HISTORY_MODEL,
                            missing_premises=missing)


__all__ = ["SouffleProgram", "SouffleResult", "SouffleUnavailable", "translate_bundle", "run_bundle",
           "program_for_pack", "PROGRAM_SCHEMA",
           "MAX_SECONDS", "MAX_ROWS", "MAX_OUTPUT_BYTES", "MAX_PROCESSES"]
