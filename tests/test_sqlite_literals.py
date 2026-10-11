import sqlite3
import unittest

from capcov.adapters import python_fastapi_sqlalchemy as adapter
from capcov.adapters.sqlite_literals import tables
from capcov.core import fixpoint

from .support import Project


class DeclarationTest(unittest.TestCase):
    def test_inventory_matches_real_sqlite_tables(self):
        script = '''
            -- CREATE TABLE imaginary (id);
            CREATE TABLE IF NOT EXISTS widgets (
                id INTEGER PRIMARY KEY, note TEXT DEFAULT 'CREATE TABLE fake (id);');
            /* CREATE TABLE also_fake (id); */
            CREATE TABLE "order details" (id INTEGER);
            CREATE TABLE `backtick` (id INTEGER);
            CREATE TABLE [bracketed] (id INTEGER);
        '''
        with sqlite3.connect(":memory:") as db:
            db.executescript(script)
            actual = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertEqual(set(tables(script)), actual)

    def test_attached_schema_and_escaped_identifier_are_retained(self):
        self.assertEqual(
            tables('create temporary table "audit"."odd""name" (id);'),
            ['audit.odd"name'],
        )

    def test_unsupported_declarations_and_non_sql_are_not_invented(self):
        self.assertEqual(tables('''
            SELECT 'CREATE TABLE fake (id)';
            CREATE VIEW view_name AS SELECT 1;
            CREATE TABLE 'single_quoted' (id);
            CREATE TABLE derived AS SELECT 1;
            CREATE VIRTUAL TABLE search USING fts5(body);
        '''), [])


class AdapterTest(unittest.TestCase):
    SOURCE = '''
from fastapi import FastAPI
app = FastAPI()

def initialize(db):
    db.executescript("CREATE TABLE items (id INTEGER); CREATE TABLE history (id INTEGER);")
    db.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER)")

@app.get("/items")
def items(db):
    return db.execute("SELECT * FROM items")

def dynamic(db, name):
    db.execute(f"SELECT * FROM {name}")
'''

    def discover(self, enabled):
        with Project({"main.py": self.SOURCE}) as project:
            (project.root / "capcov.toml").write_text(
                f"[capcov]\nsqlite_ddl = {str(enabled).lower()}\n"
            )
            return adapter.discover(project.source, project.root)

    def test_explicit_opt_in_preserves_existing_adapter_behavior(self):
        raw = self.discover(False)
        self.assertEqual(raw["entities"], [])
        self.assertEqual(raw["blind_spots"], [])

    def test_declarations_expand_denominator_without_fabricating_route_binding(self):
        raw = self.discover(True)
        self.assertEqual([e["name"] for e in raw["entities"]], ["history", "items"])
        entity = raw["entities"][0]
        self.assertEqual(entity["file"], "app/main.py")
        self.assertEqual(entity["declaration_kind"], "sqlite_literal_ddl")
        self.assertIsNone(entity["symbol"])
        bindings, _ = fixpoint.bind(
            [s["handler"] for s in raw["surfaces"]], raw["_calls"], raw["_direct"]
        )
        self.assertEqual(bindings["app.main:items"], {})

    def test_every_sql_call_keeps_an_explicit_binding_gap(self):
        blind = self.discover(True)["blind_spots"]
        self.assertEqual(len(blind), 4)
        self.assertEqual(sum(b["kind"] == "literal_sql_unbound" for b in blind), 3)
        self.assertEqual(sum(b["kind"] == "computed_sql_or_expression" for b in blind), 1)
        self.assertTrue(all(b["blind"] and b["line"] for b in blind))

    def test_orm_declaration_takes_priority_over_duplicate_sql(self):
        source = self.SOURCE + '\nclass Item:\n    __tablename__ = "items"\n'
        with Project({"main.py": source}) as project:
            (project.root / "capcov.toml").write_text("[capcov]\nsqlite_ddl = true\n")
            raw = adapter.discover(project.source, project.root)
        items = [e for e in raw["entities"] if e["name"] == "items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["symbol"], "Item")

    def constant_discovery(self, source):
        with Project({"main.py": source}) as project:
            (project.root / "capcov.toml").write_text("[capcov]\nsqlite_ddl = true\n")
            return adapter.discover(project.source, project.root)

    def test_shared_constant_expands_inventory_without_claiming_bindings(self):
        source = """
SCHEMA: str = "CREATE TABLE reviews (id TEXT, actor TEXT, PRIMARY KEY(id, actor))"
def initialize(db):
    db.execute(SCHEMA)
def restart(db):
    db.executescript(sql_script=SCHEMA)
"""
        raw = self.constant_discovery(source)
        self.assertEqual([e["name"] for e in raw["entities"]], ["reviews"])
        self.assertEqual(raw["entities"][0]["declaration_kind"], "sqlite_constant_ddl")
        self.assertEqual([b["kind"] for b in raw["blind_spots"]],
                         ["constant_sql_unbound", "constant_sql_unbound"])
        self.assertTrue(all(not tables for tables in raw["_direct"].values()))

    def test_competing_bindings_remain_unresolved(self):
        cases = [
            'SCHEMA = "CREATE TABLE replacement (id)"',
            'SCHEMA += "; SELECT 1"',
            'del SCHEMA',
            'def initialize(db, SCHEMA):\n    db.execute(SCHEMA)',
            'def other():\n    SCHEMA = "CREATE TABLE local (id)"',
            'from missing import SCHEMA',
            'from missing import *',
            'import unknown as SCHEMA',
            'class SCHEMA: pass',
            'def SCHEMA(): pass',
            'try: pass\nexcept Exception as SCHEMA: pass',
            'match value:\n    case {"value": SCHEMA}: pass',
            '[SCHEMA for SCHEMA in values]',
            'if condition:\n    SCHEMA = "CREATE TABLE conditional (id)"',
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                raw = self.constant_discovery(
                    'SCHEMA = "CREATE TABLE reviews (id)"\n'
                    'def create(db):\n    db.execute(SCHEMA)\n' + extra + '\n'
                )
                self.assertEqual(raw["entities"], [])
                self.assertTrue(all(b["kind"] == "computed_sql_or_expression"
                                    for b in raw["blind_spots"]))

    def test_computed_and_conditional_definitions_are_not_evaluated(self):
        for declaration in (
            'SCHEMA = build_schema()',
            'SCHEMA = "CREATE TABLE " + table + " (id)"',
            'if enabled:\n    SCHEMA = "CREATE TABLE reviews (id)"',
            'ORIGINAL = "CREATE TABLE reviews (id)"\nSCHEMA = ORIGINAL',
        ):
            with self.subTest(declaration=declaration):
                raw = self.constant_discovery(declaration + '\ndef create(db):\n    db.execute(SCHEMA)')
                self.assertEqual(raw["entities"], [])
                self.assertEqual(raw["blind_spots"][-1]["kind"], "computed_sql_or_expression")

    def test_literal_mapping_loop_matches_runtime_without_claiming_queries(self):
        source = """
SCHEMAS = {
    "widgets": ("CREATE TABLE widgets (id TEXT)", ["id"]),
    "events": ("CREATE TABLE events (id TEXT)", ["id"]),
}
def initialize(db):
    for ddl, columns in SCHEMAS.values():
        db.execute(ddl)
"""
        namespace = {}
        exec(source, namespace)
        with sqlite3.connect(":memory:") as db:
            namespace["initialize"](db)
            actual = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        raw = self.constant_discovery(source)
        self.assertEqual({e["name"] for e in raw["entities"]}, actual)
        self.assertTrue(all(e["declaration_kind"] == "sqlite_constant_ddl"
                            for e in raw["entities"]))
        self.assertEqual([b["kind"] for b in raw["blind_spots"]],
                         ["constant_sql_unbound"])
        self.assertTrue(all(not bindings for bindings in raw["_direct"].values()))

    def test_mapping_mutation_alias_shadow_and_pre_execution_rewrite_are_unresolved(self):
        declaration = 'SCHEMAS = {"x": ("CREATE TABLE widgets (id)", ["id"])}\n'
        loop = 'def initialize(db):\n    for ddl, columns in SCHEMAS.values():\n        db.execute(ddl)\n'
        cases = [
            declaration + 'SCHEMAS.clear()\n' + loop,
            declaration + 'alias = SCHEMAS\n' + loop,
            declaration + 'SCHEMAS["x"] = ("SELECT 1", [])\n' + loop,
            declaration + loop.replace('initialize(db)', 'initialize(db, SCHEMAS)'),
            declaration + loop.replace('        db.execute', '        ddl = "SELECT 1"\n        db.execute'),
            declaration + loop.replace('ddl, columns', 'ddl, ddl'),
            declaration.replace('["id"]', 'build_columns()') + loop,
            declaration.replace('("CREATE TABLE widgets (id)", ["id"])',
                                '("CREATE TABLE widgets (id)",)') + loop,
            declaration + 'from missing import *\n' + loop,
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(self.constant_discovery(source)["entities"], [])

    def test_string_false_does_not_silently_enable_discovery(self):
        with Project({"main.py": self.SOURCE}) as project:
            (project.root / "capcov.toml").write_text('[capcov]\nsqlite_ddl = "false"\n')
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                adapter.discover(project.source, project.root)


if __name__ == "__main__":
    unittest.main()
