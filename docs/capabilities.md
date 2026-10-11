# Specified, implemented and exercised capabilities

`capcov capabilities` compares three sets of capability IDs and gates the
differences. An ID is `<area>/<requirement>/<scenario>`, each segment lowercase
with runs of other characters turned into `-`. A shorter ID applies to every ID
beneath it, so a log line naming `area/requirement` covers each scenario of that
requirement. `capcov.capabilities.ids` has the helpers (`slug`, `capability_id`).

| Set | Adapter | Reads |
| --- | --- | --- |
| specified | `openspec` | `<root>/**/spec.md`: each `### Requirement:` and each `#### Scenario:` under it. The directory holding `spec.md` is the area. A requirement with no scenario is itself an ID. |
| implemented | `[[capabilities.implemented]]` entries | the surfaces `capcov discover` wrote to `capabilities.json`, mapped to IDs by glob (`surface`), regex (`pattern`, named groups fill `{name}` in `capability`) or `plugin` |
| exercised | `[[capabilities.exercised]]` entries | log files, one record per line, each classified `pass` or `fail` |

```toml
[capabilities.specified]
kind = "openspec"                 # or plugin = "package.module:callable"
root = "openspec/specs"           # default

[[capabilities.implemented]]
surface = "POST /v1/sessions"
capability = "sessions/open-a-session"

[[capabilities.exercised]]
name = "audit"                    # label in reports, exclusions and --input
glob = "logs/*.log"               # relative to the project root
format = "logfmt"                 # logfmt | json | regex (with pattern = '(?P<x>...)')
match = { contains = "audit" }    # lines of interest; also { regex = "..." }
where = []                        # conditions every line must meet
capability = "sessions/{action}/{target}"   # fields are slugged into the ID
pass_when = [{ field = "result", op = "eq", value = "ok" }]
fail_when = [{ field = "result", op = "eq", value = "error" }]

[[capabilities.exclude]]
capability = "sessions/close-a-session/*"   # fnmatch over the finding's ID
kind = "specified-unexercised"              # optional; absent waives every kind
reason = "close ships next"                 # required
```

A condition is `{ field, op, value }` with `op` one of `eq`, `ne`, `gt`, `gte`,
`lt`, `lte`, `in`, `contains`, `exists`. A condition on a field the record lacks
is false for every operator. `fail_when` wins; with no `pass_when` every other
attributed line passes. A JSON access log works the same way, for example
`format = "json"` with `fail_when` on `status` `gte` 500 and on `response_flags`
`ne` `"-"`.

A log format capcov does not parse belongs in a plugin, not in capcov. An
`exercised` entry with `plugin = "package.module:callable"` is called per line as
`callable(line, source)` and returns `None`, one record or a list of records
`{"capability": id, "result": "pass" | "fail", "detail": optional text}`.
An `implemented` plugin is called `callable(surfaces, config)` and returns
`{"capability", "surface"}` records. A `specified` plugin is called
`callable(target, config)` and returns `{"capabilities": [{"id": ...}], "unresolved": []}`.

```console
capcov capabilities check --target . [--discovered capabilities.json] [--input audit=run.log] [--json]
```

`specified`, `implemented`, `exercised` and `reconcile` run one step each;
`check` runs all of them. Files go to `capabilities-out/`: `specified.json`,
`implemented.json`, `exercised.json` and `reconciliation.json`, which holds the
findings, a row per specified ID and the gate verdict under `gate`.

| Finding | Meaning |
| --- | --- |
| `specified-unimplemented` | a specified ID no implemented ID covers |
| `implemented-unspecified` | an implemented ID outside the specification, or a discovered surface no entry maps (`unmapped:<surface>`) |
| `specified-unexercised` | an implemented, specified ID no log record shows running |
| `exercised-failing` | an exercised ID with at least one failing record, with counts and the first failing lines |

`exercised-unspecified` is listed but does not gate. Adapter problems are named
in `unresolved` and gate under their own ID: a duplicate scenario, a spec tree
with nothing in it, a source with no log file (`no-input:<name>`), selected lines
that gave no capability or result (`unattributed:<name>`), an entry matching no
surface. An exclusion with no reason does not apply, and one that waives nothing
fails. Exit status is 0 on pass, 1 on failures, 2 on a configuration error.
A passing gate says the three sets agree. It does not say the system is correct.
