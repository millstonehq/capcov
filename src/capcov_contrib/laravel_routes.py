"""Laravel route reader, brought in through the plugin seam.

A tree-sitter query sees one call at a time. Laravel's route surface is the
COMPOSITION of nested `Route::group(['prefix' => ...], fn)` and fluent
`Route::prefix(...)->group(fn)` scopes around each `Route::get(...)`, so a query
that captures the literal reads '/' for every index route in the codebase and
the inventory collapses. This reader walks the syntax tree with a prefix stack
and emits the path the router serves.

The outermost prefix is usually declared OUTSIDE the route file: a service
provider mounts `routes/api.php` under `api` (Laravel 11: `apiPrefix` in
`bootstrap/app.php`) and a versioned API mounts each directory under
`api/<version>`. The reader cannot see that, so `mounts` in the adapter config
names it per file glob; a file matching no mount seeds with the empty prefix and
-- when mounts ARE configured -- is named as a limit rather than left silent.

What it refuses to do, in the package's usual style:

* a non-literal path (`Route::get($x, ...)`) is a `dynamic-route` unresolved entry;
* a non-literal prefix (`['prefix' => $tenant]`, `Route::group($attrs, ...)`,
  `Route::prefix($p)`) makes every route beneath it a `dynamic-prefix`
  unresolved entry -- the subtree is named, not emitted with a guessed path;
* a group whose body is a file include (`Route::group([...], base_path(...))`)
  is a `dynamic-prefix` entry: the included file, if globbed, is read without
  this group's prefix;
* a verb or group call on a receiver that is not the Route facade
  (`$router->get(...)`, `$router->group(...)`) is a `dynamic-route` entry naming
  the receiver, and its body is not walked -- but ONLY when the call also has a
  route declaration's argument shape, so ordinary PHP in a route file
  (`$config->get('a')`, `config()->get('x')`) is not turned into a fabricated
  limit. A two-argument call ending in a closure, or one whose second argument
  is handler-shaped, IS that shape however its first argument reads, so
  `$router->group($attrs, fn)` and `$router->get($path, 'C@i')` stay named
  rather than slipping through the gate;
* a nested resource name (`photos.comments`) is a `dynamic-route` entry;
* a second declaration of the same METHOD+path is reported under
  `excluded_surfaces` with reason "duplicate ..." -- visible, not a silent dedupe.
  Laravel's route collection overwrites on method+URI, so the RETAINED surface
  generally carries the shadowed handler and line; the exclusion says so;
* a `mounts` glob that resolves to no file is a ValueError, not a silent no-op
  that leaves a whole directory unprefixed while the run still reads green;
* a routed file matching none of the configured mounts is an `unmounted-file`
  unresolved entry (with no mounts configured the empty prefix IS the contract,
  and stays silent);
* no route file at all is a `no-surfaces` unresolved entry, never an empty green.

Every per-site unresolved entry carries
`id = "laravel-routes:<file>:<line>:<column>:<kind>"` (the file-level
`unmounted-file` entry carries `"laravel-routes:<file>:unmounted-file"`) so the
gate keys each limit separately and can exempt ONE of them at a time. The column
is part of it for the same reason `flows.discovery` keys by column: two
declarations on ONE line are two obligations, and an id per node+kind is only
addressable if it is also an id per node.

Handler styles read: `[Controller::class, 'method']`, `'Controller@method'`,
`['uses' => 'Controller@method']`, an invokable `Controller::class`
("Controller@__invoke"), closures ("closure"). Anything else is carried in
full as "unresolved:<source text>". `resource` / `apiResource` expand to
Laravel's canonical action set, honouring `->only(...)` and `->except(...)` in
array or variadic form. The route parameter follows
`ResourceRegistrar::getResourceWildcard`: an explicit `->parameters([...])` map
wins, otherwise a singularisation HEURISTIC, and either way the registrar's
unconditional `str_replace('-', '_')` applies (`work-orders` ->
`{work_order}`). A `->parameters(...)` argument that is not a literal map is a
`dynamic-parameters` entry naming the fallback. `match([...], path, h)` emits
one surface per verb; `any(path, h)` emits the five recognised verbs and NOT
`OPTIONS` or `HEAD`, which Laravel also registers -- Laravel registers `HEAD`
alongside every `GET` too, so emitting the pair for `any` alone would be
arbitrary, and the probe exercises the five. `Route::view`, `Route::redirect`
and `Route::fallback` are not read.

Plugin contract (CORE path, see capcov/PLUGINS.md):
    discover(source_root, target, config) -> core dict
"""

from __future__ import annotations

from pathlib import Path

from capcov.adapters import build_core_dict

NAME = "laravel-routes"
LANGUAGE = "php"
DEFAULT_GLOBS = ["routes/**/*.php", "app/**/Routes/**/*.php"]
VERBS = ("get", "post", "put", "patch", "delete")
ROUTE_FACADES = {"Route", "\\Route", "Illuminate\\Support\\Facades\\Route"}
# Registrar methods that terminate a fluent chain in a route declaration.
TERMINALS = set(VERBS) | {"any", "match", "group", "resource", "apiResource"}

# Node-type names the installed tree-sitter PHP grammar uses; the ONE place to
# adjust if a grammar upgrade renames them.
CLOSURE_TYPES = ("anonymous_function", "anonymous_function_creation_expression", "arrow_function")
STRING_TYPES = ("string", "encapsed_string")
ARRAY_TYPE = "array_creation_expression"
ARRAY_ELEMENT_TYPE = "array_element_initializer"
CLASS_CONSTANT_TYPE = "class_constant_access_expression"
SCOPED_CALL = "scoped_call_expression"
MEMBER_CALL = "member_call_expression"

# Laravel's resource controller actions: name -> (verb, path suffix).
RESOURCE_ACTIONS = [
    ("index", "GET", ""),
    ("create", "GET", "/create"),
    ("store", "POST", ""),
    ("show", "GET", "/{param}"),
    ("edit", "GET", "/{param}/edit"),
    ("update", "PUT", "/{param}"),
    ("update", "PATCH", "/{param}"),
    ("destroy", "DELETE", "/{param}"),
]
API_RESOURCE_OMITS = {"create", "edit"}

# A chained option whose argument the reader cannot read as a literal.
UNREADABLE = object()


def _join(prefix: str, path: str) -> str:
    parts = [p for p in (prefix.strip("/") + "/" + path.strip("/")).split("/") if p]
    return "/" + "/".join(parts)


def _singular(name: str) -> str:
    """A HEURISTIC for Laravel's Str::singular of the last URI segment.

    `ies` -> `y` (companies -> company; movies -> movy is the accepted miss);
    `es` is stripped only after a sibilant (boxes, statuses, classes, batches,
    dishes); otherwise a single trailing `s` goes (cases -> case, licenses ->
    license). The `uses` rule that fixes `statuses` breaks `houses`,
    `warehouses` and `causes` (each loses its trailing `e`), and English gives
    no structural way to tell those apart -- so the miss stays DOCUMENTED rather
    than traded for another, and Laravel's own escape hatch,
    `->parameters([...])`, overrides it (`_wildcard`).
    """
    last = name.strip("/").split("/")[-1]
    if last.endswith("ies"):
        return last[:-3] + "y"
    if last.endswith(("sses", "uses", "xes", "ches", "shes")):
        return last[:-2]
    if last.endswith("s") and not last.endswith("ss"):
        return last[:-1]
    return last


def _wildcard(name: str, parameters: dict | None = None) -> str:
    """Laravel's `ResourceRegistrar::getResourceWildcard` for a resource name.

    The registrar consults its `->parameters([...])` map FIRST, falls back to
    `Str::singular`, and ends in an UNCONDITIONAL `str_replace('-', '_')` -- so
    `Route::apiResource('work-orders', ...)` serves `/work-orders/{work_order}`
    and `{work-order}` would be an id no runtime probe can match.
    """
    last = name.strip("/").split("/")[-1]
    value = (parameters or {}).get(last) or _singular(last)
    return value.replace("-", "_")


def _mount_table(root: Path, mounts: list[dict]) -> list[tuple[set[Path], str]]:
    """Resolve each mount's glob to a file set, in declaration order."""
    table: list[tuple[set[Path], str]] = []
    for index, mount in enumerate(mounts):
        if not isinstance(mount, dict) or "glob" not in mount or "prefix" not in mount:
            raise ValueError(f"mounts[{index}] must be a table with 'glob' and 'prefix' keys")
        prefix = mount["prefix"]
        if not isinstance(prefix, str) or not prefix.startswith("/"):
            raise ValueError(f"mounts[{index}].prefix {prefix!r} must start with '/'")
        # Case-sensitive on every host. On a case-insensitive filesystem pathlib
        # (3.13 and later) otherwise resolves a mis-cased literal segment, and
        # the mount would match here and match nothing where the target runs.
        files = {p for p in root.glob(mount["glob"], case_sensitive=True) if p.is_file()}
        if not files:
            raise ValueError(
                f"mounts[{index}].glob {mount['glob']!r} matched no file under {root}; "
                "a mount resolving to nothing leaves its routes silently unprefixed "
                "while the run still reports zero unresolved"
            )
        table.append((files, prefix))
    return table


class _Reader:
    def __init__(self, relative: str, source: bytes, seen: dict[str, tuple[str, int]]) -> None:
        self.relative = relative
        self.source = source
        self.seen = seen  # duplicates are global across route files
        self.records: list[dict] = []
        self.excluded: list[dict] = []
        self.unresolved: list[dict] = []

    # -- tree helpers -------------------------------------------------------
    def text(self, node) -> str:
        return self.source[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def line(self, node) -> int:
        return node.start_point[0] + 1

    @staticmethod
    def children(node, *types: str):
        return [c for c in node.named_children if c.type in types]

    def string_literal(self, node) -> str | None:
        """The content of a single- or double-quoted literal, else None."""
        if node.type in STRING_TYPES:
            inner = [c for c in node.named_children if c.type == "string_content"]
            if node.type == "encapsed_string" and any(
                c.type != "string_content" for c in node.named_children
            ):
                return None  # interpolation: not a literal
            return self.text(inner[0]) if inner else ""
        return None

    def string_list(self, nodes) -> set[str]:
        """Literal strings from array elements or variadic arguments."""
        out: set[str] = set()
        for node in nodes:
            leaf = node.named_children[-1] if node.type == ARRAY_ELEMENT_TYPE else node
            literal = self.string_literal(leaf)
            if literal is not None:
                out.add(literal)
        return out

    def string_map(self, node) -> dict[str, str] | None:
        """`['a' => 'b', ...]` as a dict of literals; None when not a literal map."""
        if node is None or node.type != ARRAY_TYPE:
            return None
        out: dict[str, str] = {}
        for element in self.children(node, ARRAY_ELEMENT_TYPE):
            kids = element.named_children
            if len(kids) != 2:
                return None
            key, value = self.string_literal(kids[0]), self.string_literal(kids[1])
            if key is None or value is None:
                return None
            out[key] = value
        return out

    def call_parts(self, node):
        """(facade_or_None, method_name, arguments_node) for a scoped call."""
        if node is None or node.type != SCOPED_CALL:
            return None
        scope = node.child_by_field_name("scope")
        name = node.child_by_field_name("name")
        args = node.child_by_field_name("arguments")
        if scope is None or name is None or args is None:
            return None
        return self.text(scope).lstrip("\\"), self.text(name), args

    def is_facade_call(self, node) -> bool:
        parts = self.call_parts(node)
        return bool(parts and parts[0] in ROUTE_FACADES)

    def arguments(self, args_node) -> list:
        return [a.named_children[0] for a in args_node.named_children
                if a.type == "argument" and a.named_children]

    @staticmethod
    def chain_root(node):
        while node is not None and node.type == MEMBER_CALL:
            node = node.child_by_field_name("object")
        return node

    # -- handlers -----------------------------------------------------------
    def class_short_name(self, node) -> str | None:
        """`\\App\\Http\\Foo::class` -> "Foo"; None for anything else."""
        if node.type == CLASS_CONSTANT_TYPE and self.text(node).endswith("::class"):
            return self.text(node).rsplit("::", 1)[0].lstrip("\\").split("\\")[-1]
        return None

    def handler(self, node) -> str:
        if node is None:
            return "closure"
        if node.type in CLOSURE_TYPES:
            return "closure"
        literal = self.string_literal(node)
        if literal is not None:
            return literal
        invokable = self.class_short_name(node)
        if invokable is not None:
            return f"{invokable}@__invoke"
        if node.type == ARRAY_TYPE:
            elements = self.children(node, ARRAY_ELEMENT_TYPE)
            # ['uses' => 'C@m']
            for element in elements:
                kids = element.named_children
                if len(kids) == 2 and self.string_literal(kids[0]) == "uses":
                    value = self.string_literal(kids[1])
                    if value is not None:
                        return value
            # [C::class, 'm']
            if len(elements) == 2:
                cls = self.class_short_name(elements[0].named_children[-1])
                method = self.string_literal(elements[1].named_children[-1])
                if cls is not None and method is not None:
                    return f"{cls}@{method}"
        return "unresolved:" + self.text(node)

    def handler_shaped(self, node) -> bool:
        """Does this argument look like a route HANDLER rather than a value?

        The second argument is what separates `$router->get($path, 'C@i')` --
        a route declaration on a router the reader cannot bind, which must be
        named -- from `$cache->get($key, $fallback)`, which is ordinary PHP.
        """
        if node is None:
            return False
        if node.type in CLOSURE_TYPES:
            return True
        literal = self.string_literal(node)
        if literal is not None:
            return "@" in literal
        if self.class_short_name(node) is not None:
            return True
        if node.type == ARRAY_TYPE:
            elements = self.children(node, ARRAY_ELEMENT_TYPE)
            for element in elements:
                kids = element.named_children
                if len(kids) == 2 and self.string_literal(kids[0]) == "uses":
                    return True
            if len(elements) == 2:
                return (self.class_short_name(elements[0].named_children[-1]) is not None
                        and self.string_literal(elements[1].named_children[-1]) is not None)
        return False

    # -- emission -----------------------------------------------------------
    def emit(self, verb: str, path: str, handler: str, node) -> None:
        sid = f"http:{verb} {path}"
        line = self.line(node)
        if sid in self.seen:
            first_file, first_line = self.seen[sid]
            self.excluded.append({
                "file": self.relative, "line": line, "method": verb, "path": path,
                "handler": handler,
                "reason": (
                    f"duplicate declaration of {sid}; first at {first_file}:{first_line} "
                    "is the surface kept, but Laravel's route collection overwrites on "
                    "method+URI, so the runtime serves this later declaration -- the "
                    "retained surface generally carries the shadowed handler and line"
                ),
            })
            return
        self.seen[sid] = (self.relative, line)
        self.records.append({
            "id": sid, "method": verb, "path": path, "handler": handler,
            "file": self.relative, "line": line, "module": self.relative,
        })

    def unresolved_entry(self, kind: str, reason: str, node) -> None:
        line, column = self.line(node), node.start_point[1]
        self.unresolved.append({
            "adapter": NAME, "kind": kind, "reason": reason,
            "id": f"{NAME}:{self.relative}:{line}:{column}:{kind}",
            "file": self.relative, "line": line, "column": column,
        })

    # -- walking ------------------------------------------------------------
    def walk(self, node, prefix: str, dynamic: bool) -> None:
        if self.route_call(node, prefix, dynamic):
            return
        for child in node.children:
            self.walk(child, prefix, dynamic)

    def prefix_from_chain(self, node):
        """(prefix, is_dynamic) from the `prefix(...)` calls of a fluent chain.

        `node` is the chain below a terminal call; its root must be a facade
        call. The registrar OVERWRITES `prefix`, so `Route::prefix('a')
        ->prefix('b')->group()` serves `/b/...`: the LAST prefix in source
        order wins, which is the FIRST one met walking outward-in.
        """
        prefixes: list[str | None] = []  # None marks a non-literal
        while node is not None:
            if node.type == MEMBER_CALL:
                name = node.child_by_field_name("name")
                args_node = node.child_by_field_name("arguments")
                if name is not None and self.text(name) == "prefix" and args_node is not None:
                    args = self.arguments(args_node)
                    prefixes.append(self.string_literal(args[0]) if args else None)
                node = node.child_by_field_name("object")
                continue
            parts = self.call_parts(node)
            if parts and parts[1] == "prefix":
                args = self.arguments(parts[2])
                prefixes.append(self.string_literal(args[0]) if args else None)
            break
        if not prefixes:
            return None, False
        last = prefixes[0]
        return (None, True) if last is None else (last, False)

    def prefix_from_array(self, node):
        """(prefix, is_dynamic) from a group attribute array; non-array is dynamic."""
        if node.type != ARRAY_TYPE:
            return None, True
        for element in self.children(node, ARRAY_ELEMENT_TYPE):
            kids = element.named_children
            if len(kids) == 2 and self.string_literal(kids[0]) == "prefix":
                literal = self.string_literal(kids[1])
                return (literal, False) if literal is not None else (None, True)
        return None, False

    def route_call(self, node, prefix: str, dynamic: bool) -> bool:
        """Dispatch a route declaration at `node`; True when it was one."""
        if node.type == SCOPED_CALL:
            parts = self.call_parts(node)
            if not parts or parts[0] not in ROUTE_FACADES or parts[1] not in TERMINALS:
                return False
            return self.declaration(parts[1], self.arguments(parts[2]), node, prefix, dynamic)
        if node.type == MEMBER_CALL:
            name = node.child_by_field_name("name")
            args_node = node.child_by_field_name("arguments")
            if name is None or args_node is None or self.text(name) not in TERMINALS:
                return False
            method = self.text(name)
            receiver = node.child_by_field_name("object")
            root = self.chain_root(receiver)
            if not self.is_facade_call(root):
                if not self.route_shaped(method, self.arguments(args_node)):
                    return False  # ordinary PHP, not a route declaration: keep walking
                # `$router->get(...)`, `$this->router->group(...)`: a router the
                # reader cannot bind to the facade; named, and its body not walked.
                shown = self.text(root) if root is not None else "?"
                self.unresolved_entry(
                    "dynamic-route",
                    f"Route::{method} form on non-facade receiver {shown!r}; not read",
                    node,
                )
                return True
            chain_prefix, chain_dynamic = self.prefix_from_chain(receiver)
            if chain_dynamic and method == "group":
                # ONLY the group form names the limit here: a verb or resource
                # call names its own non-literal prefix at this same node (and
                # the id is per node+kind, so emitting both would give one gate
                # id to two obligations and the gate could exempt neither alone).
                self.unresolved_entry(
                    "dynamic-prefix", "fluent prefix(...) is not a literal; routes beneath it are not emitted", node,
                )
            new_prefix = _join(prefix, chain_prefix) if chain_prefix else prefix
            return self.declaration(method, self.arguments(args_node), node, new_prefix, dynamic or chain_dynamic)
        return False

    def route_shaped(self, method: str, args: list) -> bool:
        """Does a non-facade call carry a route declaration's ARGUMENT shape?

        Keyed on the method NAME alone, ordinary PHP in a route file --
        `$config->get('a')`, `$request->get('k')`, `config()->get('x')` --
        becomes a manufactured gate blocker, and a fabricated limit is as
        dishonest as a silent drop. A declaration is `group(array|closure, ...)`,
        `group(anything, closure)`, `match(array, path, ...)`, or
        `verb(path, handler)` where the path is a string literal OR the handler
        is handler-shaped; anything else is not reported and the walk continues
        into the node.

        The first argument alone decides neither way, so the LAST/second one
        settles the hard cases. A two-argument call ending in a closure is a
        group declaration whatever its attributes read as, which keeps
        `$router->group($attrs, fn)` named instead of letting the walk descend
        into a body whose prefix the reader never saw. A handler-shaped second
        argument keeps `$router->get($path, 'C@i')` named, exactly as the facade
        twin `Route::get($path, ...)` is, while `$cache->get($key, $fallback)`
        stays out.

        One residual over-report remains, and it is VISIBLE (the entry names the
        receiver): a two-argument `$request->get('key', 'default')` reads as
        route-shaped. A test loose enough to exclude it would start dropping
        real `$router->get(...)` declarations instead.
        """
        if not args:
            return False
        if method == "group":
            return (args[0].type == ARRAY_TYPE or args[0].type in CLOSURE_TYPES
                    or (len(args) >= 2 and args[-1].type in CLOSURE_TYPES))
        if method == "match":
            return len(args) >= 2 and args[0].type == ARRAY_TYPE
        if len(args) < 2:
            return False
        return self.string_literal(args[0]) is not None or self.handler_shaped(args[1])

    def declaration(self, method: str, args: list, node, prefix: str, dynamic: bool) -> bool:
        if method == "group":
            self.group(args, node, prefix, dynamic)
            return True
        if method in VERBS or method in ("any", "match"):
            self.verb(method, args, node, prefix, dynamic)
            return True
        if method in ("resource", "apiResource"):
            self.resource(method, args, node, prefix, dynamic)
            return True
        return False

    def group(self, args: list, node, prefix: str, dynamic: bool) -> None:
        group_prefix, group_dynamic = None, False
        if len(args) >= 2:
            group_prefix, group_dynamic = self.prefix_from_array(args[0])
        body = args[-1] if args else None
        if group_dynamic:
            self.unresolved_entry(
                "dynamic-prefix",
                "Route group prefix is not a literal; routes beneath it are not emitted",
                node,
            )
        if body is None or body.type not in CLOSURE_TYPES:
            self.unresolved_entry(
                "dynamic-prefix",
                "Route group body is not a closure (a file include?); the included file "
                "is read without this group's prefix only if it is globbed",
                node,
            )
            return
        new_prefix = _join(prefix, group_prefix) if group_prefix else prefix
        self.walk(body, new_prefix, dynamic or group_dynamic)

    def verb(self, method: str, args: list, node, prefix: str, dynamic: bool) -> None:
        if method == "match":
            if len(args) < 2 or args[0].type != ARRAY_TYPE:
                self.unresolved_entry("dynamic-route", "Route::match verbs are not a literal array", node)
                return
            verbs = [v.upper() for v in self.string_list(self.children(args[0], ARRAY_ELEMENT_TYPE))]
            path_node, handler_node = args[1], (args[2] if len(args) > 2 else None)
        else:
            verbs = [v.upper() for v in VERBS] if method == "any" else [method.upper()]
            path_node, handler_node = (args[0] if args else None), (args[1] if len(args) > 1 else None)
        literal = self.string_literal(path_node) if path_node is not None else None
        if literal is None:
            self.unresolved_entry("dynamic-route", "route path is not a string literal", node)
            return
        if dynamic:
            self.unresolved_entry("dynamic-prefix", f"route {literal!r} sits under a non-literal prefix", node)
            return
        path = _join(prefix, literal)
        handler = self.handler(handler_node)
        for verb in verbs:
            self.emit(verb, path, handler, node)

    def resource(self, method: str, args: list, node, prefix: str, dynamic: bool) -> None:
        literal = self.string_literal(args[0]) if args else None
        if literal is None:
            self.unresolved_entry("dynamic-route", f"Route::{method} name is not a literal", node)
            return
        if "." in literal:
            self.unresolved_entry(
                "dynamic-route", f"Route::{method} nested name {literal!r} is not read", node,
            )
            return
        if dynamic:
            self.unresolved_entry("dynamic-prefix", f"resource {literal!r} sits under a non-literal prefix", node)
            return
        controller = "closure"
        if len(args) > 1:
            controller = self.class_short_name(args[1]) or self.string_literal(args[1]) \
                or "unresolved:" + self.text(args[1])
        only, except_, parameters = self.resource_options(node)
        if parameters is UNREADABLE:
            self.unresolved_entry(
                "dynamic-parameters",
                f"Route::{method} ->parameters(...) is not a literal map; the wildcard "
                f"for {literal!r} falls back to the singularisation heuristic",
                node,
            )
            parameters = None
        base = _join(prefix, literal)
        param = _wildcard(literal, parameters)
        for action, verb, suffix in RESOURCE_ACTIONS:
            if method == "apiResource" and action in API_RESOURCE_OMITS:
                continue
            if only is not None and action not in only:
                continue
            if except_ is not None and action in except_:
                continue
            self.emit(verb, base + suffix.replace("{param}", "{" + param + "}"),
                      f"{controller}@{action}", node)

    def resource_options(self, node):
        """->only(...) / ->except(...) / ->parameters([...]) chained onto a resource.

        `only`/`except` are read in array or variadic form. `parameters` is
        Laravel's OWN override for the route wildcard
        (`->parameters(['work-orders' => 'order'])`), so it outranks this
        reader's singularisation heuristic; an argument that is not a literal
        map comes back as UNREADABLE for the caller to name.
        """
        only = except_ = None
        parameters = None  # a literal map, None, or the UNREADABLE sentinel
        parent = node.parent
        while parent is not None and parent.type == MEMBER_CALL:
            name = parent.child_by_field_name("name")
            args_node = parent.child_by_field_name("arguments")
            called = self.text(name) if name is not None else None
            if args_node is not None and called in ("only", "except"):
                args = self.arguments(args_node)
                if len(args) == 1 and args[0].type == ARRAY_TYPE:
                    names = self.string_list(self.children(args[0], ARRAY_ELEMENT_TYPE))
                else:
                    names = self.string_list(args)
                if called == "only":
                    only = names
                else:
                    except_ = names
            elif args_node is not None and called == "parameters":
                args = self.arguments(args_node)
                read = self.string_map(args[0]) if args else None
                parameters = read if read is not None else UNREADABLE
            parent = parent.parent
        return only, except_, parameters


def discover(source_root, target, config: dict | None = None) -> dict:
    """The CORE-path plugin entry point (see capcov/PLUGINS.md)."""
    try:
        import tree_sitter as ts
        from tree_sitter_language_pack import get_language
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise SystemExit(
            "laravel-routes needs the treesitter extra: uv tool install 'capcov[treesitter]'"
        ) from exc
    root = Path(source_root)
    config = config or {}
    globs = list(config.get("globs") or DEFAULT_GLOBS)
    declared_mounts = list(config.get("mounts") or [])
    mounts = _mount_table(root, declared_mounts)
    parser = ts.Parser(get_language(LANGUAGE))
    records: list[dict] = []
    excluded: list[dict] = []
    unresolved: list[dict] = []
    seen: dict[str, tuple[str, int]] = {}
    files = sorted({p for g in globs for p in root.glob(g, case_sensitive=True) if p.is_file()})
    for path in files:
        source = path.read_bytes()
        reader = _Reader(str(path.relative_to(root)), source, seen)
        mount = next((prefix for members, prefix in mounts if path in members), "")
        tree = parser.parse(source)
        reader.walk(tree.root_node, mount, False)
        records.extend(reader.records)
        excluded.extend(reader.excluded)
        unresolved.extend(reader.unresolved)
        if declared_mounts and not mount and reader.records:
            # With mounts configured, an unmounted routed file is a LIMIT of this
            # config -- the provider prefix it may carry is missing from every
            # path in it. With no mounts at all, the empty prefix IS the
            # contract, so that case stays silent.
            unresolved.append({
                "adapter": NAME, "kind": "unmounted-file",
                "reason": (
                    f"{reader.relative} declares routes but matches no entry in `mounts`; "
                    "its paths are composed from the empty prefix, so any prefix a service "
                    "provider adds outside the file is missing from them"
                ),
                "id": f"{NAME}:{reader.relative}:unmounted-file",
                "file": reader.relative,
            })
    if not records:
        unresolved.append({
            "adapter": NAME, "kind": "no-surfaces",
            "reason": f"no Laravel route declarations found under {globs}",
        })
    return build_core_dict(
        records,
        {"count": len(excluded), "surfaces": excluded},
        unresolved,
    )
