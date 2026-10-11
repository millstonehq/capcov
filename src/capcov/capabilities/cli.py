"""``capcov capabilities``: reconcile specified, implemented and exercised capabilities.

    capcov capabilities specified   [--target DIR] [--out-dir DIR]
    capcov capabilities implemented [--target DIR] [--out-dir DIR] [--discovered FILE]
    capcov capabilities exercised   [--target DIR] [--out-dir DIR] [--input NAME=PATH ...]
    capcov capabilities reconcile   [--target DIR] [--out-dir DIR]
    capcov capabilities check       [--target DIR] [--out-dir DIR] [--discovered FILE]
                                    [--input NAME=PATH ...] [--json]

Each step reads its adapter configuration from the `[capabilities]` tables of
`<target>/capcov.toml` and writes `specified.json`, `implemented.json`,
`exercised.json` or `reconciliation.json` under `--out-dir` (default
`capabilities-out`). `reconcile` reads the three set files; `check` runs every
step, applies `[[capabilities.exclude]]`, writes the verdict into
`reconciliation.json` under `gate`, and prints a summary (or the document with
`--json`).

Exit status: 0 when the gate passes, 1 when it has failures, 2 for a
configuration or input error.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from .. import artifacts
from . import exercised, implemented, reconcile, spec


class ConfigError(Exception):
    pass


def _config(target: Path) -> dict:
    path = target / "capcov.toml"
    if not path.exists():
        raise ConfigError(f"{path} does not exist")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")).get("capabilities") or {}
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}")
    if not data:
        raise ConfigError(f"{path} has no [capabilities] table")
    return data


def _write(out_dir: Path, name: str, doc: dict) -> dict:
    doc = {"schema_version": artifacts.SCHEMA_VERSION, **doc}
    artifacts.write_document(out_dir / name, doc)
    return doc


def _specified(target: Path, config: dict) -> dict:
    block = config.get("specified")
    if not block:
        raise ConfigError("[capabilities.specified] is required")
    if "plugin" in block:
        from ..adapters import import_plugin_callable

        doc = import_plugin_callable(block["plugin"])(target, block)
        if not isinstance(doc, dict) or "capabilities" not in doc:
            raise ConfigError("the specified plugin must return a dict with a 'capabilities' list")
        doc.setdefault("kind", "capabilities-specified")
        doc.setdefault("unresolved", [])
        return doc
    if block.get("kind", "openspec") != "openspec":
        raise ConfigError(f"specified kind {block['kind']!r} is not built in; set plugin")
    root = block.get("root", "openspec/specs")
    return spec.read_openspec(target / root, display_root=root)


def _implemented(config: dict, discovered: Path) -> dict:
    entries = config.get("implemented") or []
    surfaces: list[dict] = []
    if entries:
        if not discovered.exists():
            raise ConfigError(f"{discovered} does not exist; run `capcov discover` first or pass --discovered")
        surfaces = artifacts.read(discovered, "capabilities").get("surfaces", [])
    return implemented.read(surfaces, entries)


def _exercised(target: Path, config: dict, raw_inputs: list[str]) -> dict:
    inputs: dict[str, list[str]] = {}
    for item in raw_inputs:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise ConfigError(f"--input {item!r}: expected NAME=PATH")
        inputs.setdefault(name, []).append(path)
    entries = config.get("exercised") or []
    unknown = sorted(set(inputs) - {e.get("name") for e in entries})
    if unknown:
        raise ConfigError(f"--input names no configured source: {', '.join(unknown)}")
    return exercised.read(entries, target, inputs)


def _run(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    out_dir = Path(args.out_dir) if Path(args.out_dir).is_absolute() else target / args.out_dir
    config = _config(target)
    step = args.step
    if step == "specified":
        doc = _write(out_dir, "specified.json", _specified(target, config))
        print(f"{len(doc['capabilities'])} specified capabilities, {len(doc['unresolved'])} unresolved")
        return 0
    if step == "implemented":
        discovered = Path(args.discovered) if args.discovered else target / "capabilities.json"
        doc = _write(out_dir, "implemented.json", _implemented(config, discovered))
        print(
            f"{len(doc['capabilities'])} implemented capabilities, "
            f"{len(doc['unmapped_surfaces'])} unmapped surfaces"
        )
        return 0
    if step == "exercised":
        doc = _write(out_dir, "exercised.json", _exercised(target, config, args.input))
        print(f"{len(doc['capabilities'])} exercised capabilities, {len(doc['unresolved'])} unresolved")
        return 0

    if step == "reconcile":
        sets = [artifacts.read(out_dir / f"{n}.json") for n in ("specified", "implemented", "exercised")]
    else:
        discovered = Path(args.discovered) if args.discovered else target / "capabilities.json"
        sets = [
            _write(out_dir, "specified.json", _specified(target, config)),
            _write(out_dir, "implemented.json", _implemented(config, discovered)),
            _write(out_dir, "exercised.json", _exercised(target, config, args.input)),
        ]
    result = reconcile.reconcile(*sets)
    verdict = reconcile.gate(result, config.get("exclude") or [])
    result["gate"] = verdict
    result = _write(out_dir, "reconciliation.json", result)
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else reconcile.summary_text(result, verdict))
    return 0 if verdict["status"] == "pass" else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="capcov capabilities", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="step", required=True)
    for name, helptext in (
        ("specified", "read the specified set"),
        ("implemented", "map discovered surfaces to capability IDs"),
        ("exercised", "read the exercised set from logs"),
        ("reconcile", "compare the three set files and gate"),
        ("check", "run every step and gate"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--target", default=".", help="the project root holding capcov.toml")
        p.add_argument("--out-dir", default="capabilities-out")
        if name in ("implemented", "check"):
            p.add_argument("--discovered", default=None, help="discover output (default <target>/capabilities.json)")
        if name in ("exercised", "check"):
            p.add_argument("--input", action="append", default=[], metavar="NAME=PATH",
                           help="a log file for source NAME, replacing its glob (repeatable)")
        if name in ("reconcile", "check"):
            p.add_argument("--json", action="store_true", help="print the reconciliation document")
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (ConfigError, ValueError, OSError, SystemExit, ImportError, AttributeError) as error:
        print(f"capcov capabilities: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
