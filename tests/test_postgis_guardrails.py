"""Security guardrails of the PostGIS layer, checked on behaviour (D2-D4 audit corrections); offline.

* R5 bis by behaviour: a recording driver plays every public read and every import; any statement naming
  a PostGIS function must come after the transaction-local search path, in the same transaction.
* Writes need an identity: each import checks the environment mark under its lock, before the first write,
  and refuses an absent, other or invalid environment without writing anything.
* Atomic multi-scope import: an error in the second scope rolls back the first; nothing is reported.
* Import graph (AST, imports inside functions included): only the listed modules reach drivers, sessions or
  the PostGIS modules; the interface reaches storage through two entry points only.
Each guardrail has a counter-proof: a faulty module or call makes it fail.
The real database is exercised by tests/test_supabase_corrections_integration.py and the postgis-marked tests.
"""

import ast
from contextlib import contextmanager
from pathlib import Path
import re

import pytest

from src.data import postgis_observations as pgo
from src.data import postgis_territory_registry as pg
from src.data.database import DatabaseConfigError, EnvironmentIdentityError
from src.data.postgis_session import MARK_QUERY, POSTGIS_SCHEMA_QUERY, SET_SEARCH_PATH

ROOT = Path(__file__).resolve().parents[1]
POSTGIS_CALL = re.compile(r"\bST_\w+\s*\(", re.I)
WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.I)


# --- Recording driver ---------------------------------------------------------------------------------


class Recorder:
    """Records (unique transaction/savepoint ID, event or SQL), including implicit BEGIN."""

    def __init__(self, mark="geonaf-environment:test"):
        self.mark, self.log, self.read_only = mark, [], None
        self._scopes, self._next_id = [], 0

    def _begin(self):
        self._next_id += 1
        event = "SAVEPOINT" if self._scopes else "BEGIN"
        self._scopes.append(self._next_id)
        self.log.append((self._next_id, event))

    def _end(self, success):
        nested = len(self._scopes) > 1
        event = ("RELEASE" if success else "ROLLBACK TO SAVEPOINT") if nested else ("COMMIT" if success else "ROLLBACK")
        self.log.append((self._scopes.pop(), event))

    @contextmanager
    def transaction(self):
        import psycopg

        self._begin()
        try:
            yield
        except psycopg.Rollback:
            self._end(False)
        except BaseException:
            self._end(False)
            raise
        else:
            self._end(True)

    def cursor(self, **_):
        return RecorderCursor(self)

    def rollback(self):
        self.log.append((self._scopes[0] if self._scopes else 0, "ROLLBACK"))
        self._scopes.clear()

    def close(self):
        self.log.append((0, "CLOSE"))
        self._scopes.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecorderCursor:
    def __init__(self, connection):
        self.connection, self.last = connection, ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if not self.connection._scopes:                 # psycopg starts a transaction on the first query.
            self.connection._begin()
        self.connection.log.append((self.connection._scopes[-1], str(sql)))
        self.last = str(sql)

    def fetchone(self):
        if self.last == POSTGIS_SCHEMA_QUERY:
            return ("extensions",)
        if self.last == MARK_QUERY:
            return (self.connection.mark,)
        if "RETURNING" in self.last:
            return (1,)
        return None

    def fetchall(self):
        return []


def statements(recorder):
    return [sql for _, sql in recorder.log]


def assert_path_before_postgis(recorder):
    """SET LOCAL belongs to the active transaction; rollback of a savepoint restores its parent state."""
    scopes = []                                       # (unique ID, secure path established)
    for identity, sql in recorder.log:
        if sql == "BEGIN":
            assert not scopes
            scopes.append((identity, False))
        elif sql == "SAVEPOINT":
            assert scopes
            scopes.append((identity, scopes[-1][1]))
        elif sql in ("RELEASE", "ROLLBACK TO SAVEPOINT"):
            assert scopes and scopes[-1][0] == identity
            _, secure = scopes.pop()
            assert scopes
            if sql == "RELEASE":
                scopes[-1] = (scopes[-1][0], secure)
        elif sql in ("COMMIT", "ROLLBACK", "CLOSE"):
            scopes.clear()
        elif sql == SET_SEARCH_PATH:
            assert scopes and scopes[-1][0] == identity
            scopes[-1] = (identity, True)
        elif POSTGIS_CALL.search(sql):
            assert scopes and scopes[-1] == (identity, True), "appel PostGIS sans chemin R5 bis dans la transaction active"


def one_connection(recorder):
    return lambda: recorder


READS = {
    "territories": lambda r: pg.PostgisTerritoryRegistry(one_connection(r)).territories(),
    "versions": lambda r: pg.PostgisTerritoryRegistry(one_connection(r)).versions("gab-test"),
    "find_geometry": lambda r: pg.PostgisTerritoryRegistry(one_connection(r)).find_geometry("0" * 64),
    "intersecting": lambda r: pg.PostgisTerritoryRegistry(one_connection(r)).intersecting((9, 0, 10, 1)),
    "acquisitions.scan": lambda r: pgo.PostgisAcquisitionCatalog(one_connection(r)).scan(),
    "acquisitions.intersecting": lambda r: pgo.PostgisAcquisitionCatalog(one_connection(r)).intersecting((9, 0, 10, 1)),
    "indicators.results": lambda r: pgo.PostgisIndicatorStore(one_connection(r)).results(),
}


@pytest.mark.parametrize("name", sorted(READS))
def test_every_read_sets_the_r5_bis_path_before_any_postgis_call(name):
    pytest.importorskip("psycopg")
    recorder = Recorder()
    READS[name](recorder)
    assert SET_SEARCH_PATH in statements(recorder)
    assert_path_before_postgis(recorder)
    assert recorder.read_only is True and statements(recorder)[-2:] == ["ROLLBACK", "CLOSE"]


def test_geometry_read_sets_the_path_before_st_asbinary():
    pytest.importorskip("psycopg")
    recorder = Recorder()
    record = type("R", (), {"geometry_sha256": "0" * 64})()
    with pytest.raises(KeyError):                 # The recorder returns no row: the query still ran.
        pg.PostgisTerritoryRegistry(one_connection(recorder)).geometry(record)
    assert any("ST_AsBinary" in sql for sql in statements(recorder))
    assert_path_before_postgis(recorder)


def test_counter_proof_a_postgis_call_without_the_path_is_detected():
    recorder = Recorder()
    recorder.log.append((1, "SELECT ST_Intersects(a, b)"))
    with pytest.raises(AssertionError, match="sans chemin R5 bis"):
        assert_path_before_postgis(recorder)


# --- Writes need a verified identity ------------------------------------------------------------------


EMPTY_PLAN = pg.ImportPlan((), {})
IMPORTS = {
    "territories": lambda c, env: pg.apply_import(c, EMPTY_PLAN, source_label="lot", code_version="t",
                                                  expected_environment=env, apply=True),
    "acquisitions": lambda c, env: pgo.apply_acquisition_import(c, [], source_label="lot", code_version="t",
                                                                expected_environment=env, apply=True),
    "indicators": lambda c, env: pgo.apply_indicator_import(c, [], source_label="lot", code_version="t",
                                                            expected_environment=env, apply=True),
}


@pytest.mark.parametrize("name", sorted(IMPORTS))
def test_each_import_checks_the_mark_under_its_lock_before_the_first_write(name):
    pytest.importorskip("psycopg")
    recorder = Recorder("geonaf-environment:test")
    IMPORTS[name](recorder, "test")
    log = statements(recorder)
    lock = next(i for i, sql in enumerate(log) if "advisory_xact_lock" in sql)
    mark = log.index(MARK_QUERY)
    path = log.index(SET_SEARCH_PATH)
    first_write = next(i for i, sql in enumerate(log) if WRITE.match(sql))
    assert lock < mark < path < first_write
    assert all(depth >= 1 for depth, _ in recorder.log)          # Everything inside the import transaction.
    assert_path_before_postgis(recorder)


@pytest.mark.parametrize("name", sorted(IMPORTS))
@pytest.mark.parametrize("mark, expected, error", [
    (None, "test", EnvironmentIdentityError),                         # Schema not marked.
    ("geonaf-environment:dev", "test", EnvironmentIdentityError),     # Another environment.
    ("geonaf-legacy: schema herite", "test", EnvironmentIdentityError),
    ("geonaf-environment:test", "prod", DatabaseConfigError),          # Invalid expectation.
    ("geonaf-environment:test", "", DatabaseConfigError),
])
def test_imports_refuse_an_absent_other_or_invalid_environment_without_writing(name, mark, expected, error):
    pytest.importorskip("psycopg")
    recorder = Recorder(mark)
    with pytest.raises(error):
        IMPORTS[name](recorder, expected)
    assert not any(WRITE.match(sql) for sql in statements(recorder))


def test_imports_cannot_be_called_without_an_expected_environment():
    for function in (pg.apply_import, pgo.apply_acquisition_import, pgo.apply_indicator_import):
        with pytest.raises(TypeError, match="expected_environment"):
            function(Recorder(), [] if function is not pg.apply_import else EMPTY_PLAN, source_label="l",
                     code_version="t")


# --- Atomic multi-scope import (scripts/import_observations_postgis.py) --------------------------------


def test_scopes_are_imported_in_one_transaction_and_a_failure_rolls_back_all_of_them():
    import sys

    sys.path.insert(0, str(ROOT))
    from scripts.import_observations_postgis import run_imports

    events = []

    class Connection:
        @contextmanager
        def transaction(self):
            events.append("BEGIN")
            try:
                yield
            except Exception:
                events.append("ROLLBACK")
                raise
            events.append("COMMIT")

    def acquisitions(connection, items, **kwargs):
        assert kwargs["expected_environment"] == "test" and kwargs["apply"] is True
        events.append("acquisitions inserted")
        return "rapport acquisitions"

    def indicators(connection, items, **kwargs):
        raise pgo.ObservationImportError("géométries absentes")

    with pytest.raises(pgo.ObservationImportError):
        run_imports(Connection(), {"acquisitions": [1], "indicators": [2]},
                    {"acquisitions": acquisitions, "indicators": indicators}, source_label="l", code_version="t",
                    environment="test", apply=True)
    assert events == ["BEGIN", "acquisitions inserted", "ROLLBACK"]          # Never COMMIT, nothing reported.
    events.clear()
    reports = run_imports(Connection(), {"acquisitions": [1]}, {"acquisitions": acquisitions}, source_label="l",
                          code_version="t", environment="test", apply=True)
    assert events == ["BEGIN", "acquisitions inserted", "COMMIT"] and reports == [("acquisitions",
                                                                                   "rapport acquisitions")]


def test_import_tools_print_results_only_after_the_global_commit():
    source = (ROOT / "scripts" / "import_observations_postgis.py").read_text(encoding="utf-8")
    main = source[source.index("def main("):]
    assert main.index("reports = run_imports(") < main.index("for scope, report in reports:")
    assert "with connect_target(target)" in main and "connect_from_env" not in source


# --- Import graph ---------------------------------------------------------------------------------


DATABASE_MODULES = ("psycopg", "sqlalchemy", "alembic", "src.data.database", "src.data.postgis_session",
                    "src.data.postgis_territory_registry", "src.data.postgis_observations", "src.data.storage")
# Exact allow list (module -> database modules it may import). Any new edge fails until reviewed here.
ALLOWED = {
    "scripts/ci/postgis_validation.py": {"psycopg"},
    "db/migrations/env.py": {"alembic", "src.data.database", "src.data.postgis_session"},
    "scripts/benchmark_storage_queries.py": {"src.data.database", "src.data.postgis_session", "src.data.storage",
                                               "src.data.postgis_observations", "src.data.postgis_territory_registry"},
    "scripts/build_supabase_sql.py": {"src.data.postgis_territory_registry"},
    "scripts/check_database_live.py": {"src.data.database"},
    "scripts/import_observations_postgis.py": {"src.data.database", "src.data.postgis_observations",
                                               "src.data.postgis_session"},
    "scripts/import_territories_postgis.py": {"src.data.database", "src.data.postgis_session",
                                              "src.data.postgis_territory_registry"},
    "scripts/verify_supabase_export.py": {"src.data.postgis_territory_registry"},
    "src/components/data_source.py": {"src.data.storage"},
    "src/components/errors.py": {"src.data.database", "src.data.postgis_session"},
    "src/data/database.py": {"psycopg", "sqlalchemy", "src.data.postgis_session"},
    "src/data/database_diagnostic.py": {"sqlalchemy", "src.data.database"},
    "src/data/postgis_observations.py": {"psycopg", "src.data.postgis_session"},
    "src/data/postgis_session.py": {"psycopg", "src.data.database"},
    "src/data/postgis_territory_registry.py": {"psycopg", "src.data.postgis_session"},
    "src/data/storage.py": {"psycopg", "src.data.database", "src.data.postgis_observations",
                            "src.data.postgis_session", "src.data.postgis_territory_registry"},
    "src/data/supabase_sql.py": {"src.data.postgis_territory_registry"},
    "src/services/territory_catalog.py": {"src.data.storage"},
}
# errors.py may take exception types only from the database layer.
ERROR_TYPES = {"DatabaseConfigError", "DatabaseConnectionError", "EnvironmentIdentityError", "PostgisUnavailableError"}


def database_imports(source: str) -> tuple[set, set]:
    """(database modules imported anywhere in the source, names imported from them)."""
    modules, names = set(), set()
    for node in ast.walk(ast.parse(source)):
        imported = []
        if isinstance(node, ast.Import):
            imported = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported = [node.module, *(node.module + "." + alias.name for alias in node.names)]
            # A package import can name a protected module; aliases never change its identity.
            names |= {alias.name for alias in node.names if node.module + "." + alias.name in DATABASE_MODULES}
            if any(alias.name == "*" for alias in node.names):
                imported += [root for root in DATABASE_MODULES if root.startswith(node.module + ".")]
                names.add("*")
            if any(node.module == m or node.module.startswith(m + ".") for m in DATABASE_MODULES[3:]):
                names |= {alias.name for alias in node.names}
        for module in imported:
            for root in DATABASE_MODULES:
                if module == root or module.startswith(root + "."):
                    modules.add(root)
    return modules, names


def project_sources():
    files = [*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py"), ROOT / "app.py",
             *(ROOT / "views").rglob("*.py"), ROOT / "db/migrations/env.py"]
    return {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in files}


def test_only_the_reviewed_modules_reach_drivers_sessions_or_postgis():
    found = {}
    for name, source in project_sources().items():
        modules, _ = database_imports(source)
        if modules:
            found[name] = modules
    assert found == ALLOWED


def test_the_interface_reaches_storage_through_data_source_only_and_errors_takes_types_only():
    interface = {name: source for name, source in project_sources().items()
                 if name == "app.py" or name.startswith(("views/", "src/components/"))}
    for name, source in interface.items():
        modules, names = database_imports(source)
        if name == "src/components/errors.py":
            assert names <= ERROR_TYPES, names
        elif name == "src/components/data_source.py":
            assert modules == {"src.data.storage"}
        else:
            assert modules == set(), name


def test_counter_proof_a_lazy_driver_import_or_a_session_import_is_detected():
    faulty = "def f():\n    import psycopg\n    from src.data.postgis_session import PostgisSessions\n"
    modules, names = database_imports(faulty)
    assert modules == {"psycopg", "src.data.postgis_session"} and names == {"PostgisSessions"}
    assert not names <= ERROR_TYPES


def test_postgis_function_inventory_is_frozen():
    """New PostGIS calls in Python must be reviewed (R5 bis tests above play every public method)."""
    calls = set()
    for name in ("postgis_territory_registry.py", "postgis_observations.py"):
        calls |= set(re.findall(r"\b(ST_\w+)\(", (ROOT / "src" / "data" / name).read_text(encoding="utf-8")))
    assert calls == {"ST_Intersects", "ST_MakeEnvelope", "ST_AsBinary", "ST_GeomFromWKB"}
    others = [name for name, source in project_sources().items()
              if ".execute(" in source and POSTGIS_CALL.search(source)
              and name not in ("src/data/postgis_territory_registry.py", "src/data/postgis_observations.py")]
    assert others == []


@pytest.mark.parametrize("source", [
    "import src.data.postgis_session", "import src.data.postgis_session as session",
    "from src.data import postgis_session", "from src.data import postgis_session as session",
    "from src.data.postgis_session import PostgisSessions",
    "from src.data.postgis_session import PostgisSessions as Sessions",
    "def f():\n    from src.data import postgis_session as session",
])
def test_counter_proof_unapproved_module_import_syntax_is_detected(source):
    modules, _ = database_imports(source)
    assert modules == {"src.data.postgis_session"}
    assert modules - ALLOWED.get("src/services/unapproved_module.py", set())


def test_counter_proof_path_from_a_completed_transaction_is_not_reused():
    pytest.importorskip("psycopg")
    recorder = Recorder()
    with recorder.transaction(), recorder.cursor() as cursor:
        cursor.execute(SET_SEARCH_PATH, ("extensions",))
        cursor.execute("SELECT ST_Intersects(a, b)")
    with recorder.transaction(), recorder.cursor() as cursor:
        cursor.execute("SELECT ST_Intersects(a, b)")
    with pytest.raises(AssertionError, match="sans chemin R5 bis"):
        assert_path_before_postgis(recorder)


@pytest.mark.parametrize("end", ["commit", "rollback"])
def test_each_new_transaction_can_establish_its_own_path(end):
    psycopg = pytest.importorskip("psycopg")
    recorder = Recorder()
    for _ in range(2):
        with recorder.transaction(), recorder.cursor() as cursor:
            cursor.execute(SET_SEARCH_PATH, ("extensions",))
            cursor.execute("SELECT ST_Intersects(a, b)")
            if end == "rollback":
                raise psycopg.Rollback()
    assert [identity for identity, event in recorder.log if event == "BEGIN"] == [1, 2]
    assert_path_before_postgis(recorder)


def test_counter_proof_path_from_a_rolled_back_savepoint_is_not_reused():
    psycopg = pytest.importorskip("psycopg")
    recorder = Recorder()
    with recorder.transaction(), recorder.cursor() as cursor:
        with recorder.transaction():
            cursor.execute(SET_SEARCH_PATH, ("extensions",))
            raise psycopg.Rollback()
        cursor.execute("SELECT ST_Intersects(a, b)")
    with pytest.raises(AssertionError, match="sans chemin R5 bis"):
        assert_path_before_postgis(recorder)


def test_counter_proof_wildcard_package_import_is_detected():
    modules, names = database_imports("from src.data import *")
    assert "src.data.postgis_session" in modules and not names <= ERROR_TYPES
