"""Offline migration policy tests: no Alembic installation and no server required."""
from contextlib import contextmanager
from pathlib import Path
import runpy
import sys
import types

import pytest

from src.data import database as db

ROOT = Path(__file__).resolve().parents[1]
LOCAL = "postgresql+psycopg://audit:synthetic@127.0.0.1/audit_test"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    import os
    for key in list(os.environ):
        if key.startswith(("PG", "GEONAF_DB", "GEONAF_TEST", "GEONAF_ALLOW", "GEONAF_MIGRATION")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GEONAF_DATABASE_URL", LOCAL)
    monkeypatch.setenv("GEONAF_DB_ENV", "test")


def run_env(monkeypatch, engine=None, context=None):
    module = types.ModuleType("alembic")
    module.context = context or types.SimpleNamespace(is_offline_mode=lambda: False)
    monkeypatch.setitem(sys.modules, "alembic", module)
    if engine is not None:
        monkeypatch.setattr(db, "create_engine", lambda *a, **k: engine)
        monkeypatch.setattr("sqlalchemy.create_engine", lambda *a, **k: engine)
    return runpy.run_path(str(ROOT / "db/migrations/env.py"))


@pytest.mark.parametrize("settings", [
    {"GEONAF_DATABASE_URL": "postgresql+psycopg://audit@remote.invalid/audit_test?sslmode=disable"},
    {"GEONAF_DATABASE_URL": "postgresql+psycopg://audit@localhost/audit_test?host=remote.invalid"},
    {"GEONAF_DB_ENV": "prod"},
    {"PGHOSTADDR": "192.0.2.1"},
])
def test_invalid_configuration_refused_before_connection(monkeypatch, settings):
    calls = []
    engine = types.SimpleNamespace(connect=lambda: calls.append("connect"), dispose=lambda: None)
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(db.DatabaseConfigError):
        run_env(monkeypatch, engine)
    assert calls == []

REVISIONS = ('0001_territorial_registry', '0002_observations')
TABLES_0001 = {'licences', 'data_sources', 'source_releases', 'geometries', 'territory_kinds',
               'legal_statuses', 'territories', 'registry_imports', 'territory_versions'}
TABLES_0002 = {'data_imports', 'acquisitions', 'acquisition_manifests', 'indicator_result_kinds',
               'indicator_definitions', 'indicator_results'}
TOKEN = '308ebc6a-b882-4132-9eba-f1f60193ca41'


def authorize(monkeypatch, *, bootstrap=True):
    monkeypatch.setenv('GEONAF_ALLOW_DESTRUCTIVE_TESTS', '1')
    monkeypatch.setenv('GEONAF_TEST_DATABASE_NAME', 'audit_test')
    monkeypatch.setenv('GEONAF_TEST_INSTANCE_ID', TOKEN)
    if bootstrap:
        monkeypatch.setenv('GEONAF_MIGRATION_BOOTSTRAP', '1')


def digest(revision):
    import hashlib
    return hashlib.sha256((ROOT / 'db/sql' / (revision + '.up.sql')).read_text(encoding='utf-8').encode()).hexdigest()


class Catalog:
    """Small catalog double; records every query and models rollback, not PostgreSQL semantics."""
    def __init__(self, revision=None, *, journal=False):
        self.schema = revision is not None
        self.mark = 'geonaf-environment:test' if self.schema else None
        self.heads = [revision] if revision else []
        self.tables = TABLES_0001 | (TABLES_0002 if revision == REVISIONS[1] else set()) if self.schema else set()
        self.history = [(r, digest(r)) for r in REVISIONS[:REVISIONS.index(revision) + 1]] if journal else []
        if journal:
            self.tables |= {'schema_migrations'}
        self.identity = ('audit_test', 'geonaf-isolated-test:' + TOKEN)
        self.events = []
        self.rows = []
        self.closed = False
        self.read_only = False
        self.autocommit = False

    def execute(self, sql, params=None):
        self.events.append((sql, params))
        if sql.startswith("SELECT pg_catalog.to_regnamespace"):
            self.rows = [(123 if self.schema else None, self.mark, 'public.alembic_version' if self.heads else None)]
        elif sql == 'SELECT version_num FROM public.alembic_version':
            self.rows = [(h,) for h in self.heads]
        elif 'pg_catalog.pg_tables' in sql:
            self.rows = [(t,) for t in self.tables]
        elif sql.startswith('SELECT version, body_sha256'):
            self.rows = list(self.history)
        elif 'shobj_description' in sql:
            self.rows = [self.identity]
        elif 'extnamespace' in sql:
            self.rows = [('extensions',)]
        elif sql.startswith('COMMENT ON SCHEMA'):
            assert self.schema
            self.mark = 'geonaf-environment:test'
        elif sql.startswith('INSERT INTO geonaf.schema_migrations'):
            self.history.append(tuple(params[:2]))
        elif sql.startswith('SELECT pg_catalog.'):
            self.rows = [(None,)]
        else:
            raise AssertionError('Unexpected SQL in double')
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    @contextmanager
    def cursor(self):
        yield self

    def rollback(self):
        self.events.append(('ROLLBACK', None))

    def close(self):
        self.closed = True


class Engine:
    def __init__(self, catalog):
        self.catalog = catalog
        self.connection = types.SimpleNamespace(driver_connection=catalog)
        self.disposed = False

    @contextmanager
    def connect(self):
        self.catalog.events.append(('CONNECT', None))
        try:
            yield self
        finally:
            self.catalog.close()

    @contextmanager
    def begin(self):
        from copy import deepcopy
        names = ('schema', 'mark', 'heads', 'tables', 'history')
        state = {n: deepcopy(getattr(self.catalog, n)) for n in names}
        self.catalog.events.append(('BEGIN', None))
        try:
            yield
        except BaseException:
            for name, value in state.items():
                setattr(self.catalog, name, value)
            self.catalog.events.append(('ROLLBACK', None))
            raise
        else:
            self.catalog.events.append(('COMMIT', None))

    def dispose(self):
        self.disposed = True


def step(revision, *, upgrade=True, stamp=False):
    return types.SimpleNamespace(info=types.SimpleNamespace(up_revision_id=revision, is_upgrade=upgrade, is_stamp=stamp))


class Context:
    def __init__(self, catalog, steps=(), *, fail=False, purge=False):
        self.catalog, self.steps, self.fail = catalog, steps, fail
        self.opts = {'fn': lambda heads, ctx: iter(steps), 'purge': purge}
        self.configurations = []
        self.ran = False

    def is_offline_mode(self):
        return False

    def configure(self, **options):
        self.configurations.append(options)
        self.opts.update(options)

    def get_context(self):
        return self

    def get_current_heads(self):
        return tuple(self.catalog.heads)

    def run_migrations(self):
        self.ran = True
        for item in self.opts['fn'](self.get_current_heads(), self):
            self.catalog.events.append(('DDL', None))
            info = item.info
            index = REVISIONS.index(info.up_revision_id)
            if info.is_upgrade:
                self.catalog.schema = True
                self.catalog.tables |= TABLES_0001 if index == 0 else TABLES_0002
                self.catalog.heads = [info.up_revision_id]
            elif index == 1:
                self.catalog.tables -= TABLES_0002
                self.catalog.heads = [REVISIONS[0]]
            else:
                self.catalog.schema = False
                self.catalog.tables = set()
                self.catalog.heads = []
            self.opts['on_version_apply'](ctx=self, step=info, heads=self.get_current_heads(), run_args={})
            if self.fail:
                raise RuntimeError('SENSITIVE_DRIVER_HOST_PASSWORD_PATH')


@pytest.mark.parametrize('mark', [None, 'geonaf-environment:dev', 'geonaf-environment:prod', 'SENSITIVE'])
def test_existing_wrong_identity_refused_before_any_write(monkeypatch, mark):
    catalog = Catalog(REVISIONS[0]); catalog.mark = mark
    engine, context = Engine(catalog), Context(catalog, [step(REVISIONS[1])])
    with pytest.raises(db.DatabaseConfigError):
        run_env(monkeypatch, engine, context)
    assert not context.ran and 'DDL' not in [q for q, _ in catalog.events]
    assert catalog.closed and engine.disposed


@pytest.mark.parametrize('damage', ['unknown_head', 'two_heads', 'missing_head', 'missing_table',
                                     'wrong_hash', 'extra_history', 'missing_history'])
def test_inconsistent_history_refused_before_any_write(monkeypatch, damage):
    catalog = Catalog(REVISIONS[0], journal=True)
    if damage == 'unknown_head': catalog.heads = ['unexpected']
    if damage == 'two_heads': catalog.heads = list(REVISIONS)
    if damage == 'missing_head': catalog.heads = []
    if damage == 'missing_table': catalog.tables.remove('geometries')
    if damage == 'wrong_hash': catalog.history = [(REVISIONS[0], 'x' * 64)]
    if damage == 'extra_history': catalog.history.append((REVISIONS[1], digest(REVISIONS[1])))
    if damage == 'missing_history': catalog.history = []
    context = Context(catalog, [step(REVISIONS[1])])
    with pytest.raises(db.DatabaseConfigError):
        run_env(monkeypatch, Engine(catalog), context)
    assert not context.ran


@pytest.mark.parametrize('missing', ['GEONAF_MIGRATION_BOOTSTRAP', 'GEONAF_ALLOW_DESTRUCTIVE_TESTS',
                                      'GEONAF_TEST_DATABASE_NAME', 'GEONAF_TEST_INSTANCE_ID', 'database_mark'])
def test_bootstrap_needs_every_authorization_and_database_identity(monkeypatch, missing):
    authorize(monkeypatch)
    catalog = Catalog()
    if missing == 'database_mark': catalog.identity = ('audit_test', None)
    else: monkeypatch.delenv(missing)
    context = Context(catalog, [step(r) for r in REVISIONS])
    with pytest.raises(db.DatabaseConfigError):
        run_env(monkeypatch, Engine(catalog), context)
    assert not context.ran and not catalog.schema


def test_bootstrap_marks_only_after_0001_and_commits_all_revisions_together(monkeypatch):
    authorize(monkeypatch)
    catalog = Catalog(); engine = Engine(catalog)
    context = Context(catalog, [step(r) for r in REVISIONS])
    run_env(monkeypatch, engine, context)
    assert catalog.heads == [REVISIONS[1]] and catalog.mark == 'geonaf-environment:test'
    events = [q for q, _ in catalog.events]
    assert events.count('BEGIN') == events.count('COMMIT') == 1
    assert events.index('DDL') < next(i for i, q in enumerate(events) if q.startswith('COMMENT'))
    path = next(i for i, q in enumerate(events) if "set_config('search_path'" in q)
    assert path < events.index('DDL')
    assert all(c['version_table_schema'] == 'public' and not c['transaction_per_migration'] for c in context.configurations)
    assert catalog.closed and engine.disposed


def test_existing_journal_appended_atomically_without_stamp(monkeypatch):
    catalog = Catalog(REVISIONS[0], journal=True)
    run_env(monkeypatch, Engine(catalog), Context(catalog, [step(REVISIONS[1])]))
    assert catalog.history == [(r, digest(r)) for r in REVISIONS]


@pytest.mark.parametrize('operation', ['stamp', 'purge', 'downgrade_journal', 'wrong_order', 'downgrade_no_permission'])
def test_disallowed_plan_refused_before_version_or_schema_writes(monkeypatch, operation):
    catalog = Catalog(REVISIONS[1], journal=operation == 'downgrade_journal')
    steps = [step(REVISIONS[1], upgrade=False, stamp=operation == 'stamp')]
    if operation == 'wrong_order': steps = [step(REVISIONS[0])]
    context = Context(catalog, steps, purge=operation == 'purge')
    with pytest.raises(db.DatabaseConfigError):
        run_env(monkeypatch, Engine(catalog), context)
    assert not context.ran


def test_explicit_isolated_downgrade_to_base(monkeypatch):
    authorize(monkeypatch, bootstrap=False)
    catalog = Catalog(REVISIONS[1])
    run_env(monkeypatch, Engine(catalog), Context(catalog, [step(r, upgrade=False) for r in reversed(REVISIONS)]))
    assert not catalog.schema and catalog.heads == []


def test_failure_rolls_back_and_redacts_driver_message(monkeypatch):
    authorize(monkeypatch)
    catalog = Catalog(); engine = Engine(catalog)
    with pytest.raises(db.DatabaseConfigError) as caught:
        run_env(monkeypatch, engine, Context(catalog, [step(r) for r in REVISIONS], fail=True))
    assert 'SENSITIVE' not in str(caught.value) and caught.value.__suppress_context__
    assert not catalog.schema and not catalog.heads
    assert ('ROLLBACK', None) in catalog.events and ('COMMIT', None) not in catalog.events
    assert engine.disposed and catalog.closed


@pytest.mark.parametrize('settings', [
    {'GEONAF_ALLOW_DESTRUCTIVE_TESTS': '0'}, {'GEONAF_TEST_DATABASE_NAME': 'different_test'},
    {'GEONAF_TEST_INSTANCE_ID': 'invalid'}, {'PGHOSTADDR': '192.0.2.1'},
])
def test_fixture_refuses_connection_without_valid_capability(monkeypatch, settings):
    from tests import postgis_support as support
    authorize(monkeypatch)
    calls = []
    monkeypatch.setattr(support, 'connect_target', lambda t: calls.append(t))
    for key, value in settings.items(): monkeypatch.setenv(key, value)
    with pytest.raises(db.DatabaseConfigError): support.isolated_connection(LOCAL)
    assert calls == []


@pytest.mark.parametrize('identity', [('other_test', 'geonaf-isolated-test:' + TOKEN), ('audit_test', None),
                                      ('audit_test', 'geonaf-environment:test')])
def test_fixture_refuses_destruction_and_closes_on_wrong_database_identity(monkeypatch, identity):
    from tests import postgis_support as support
    authorize(monkeypatch)
    catalog = Catalog(); catalog.identity = identity
    monkeypatch.setattr(support, 'connect_target', lambda target: catalog)
    with pytest.raises(db.EnvironmentIdentityError):
        with support.isolated_connection(LOCAL) as connection:
            connection.execute('DROP SCHEMA geonaf CASCADE')
    assert catalog.closed and all(q.startswith('SELECT') for q, _ in catalog.events)


def test_fixture_checks_identity_read_only_before_enabling_writes(monkeypatch):
    from tests import postgis_support as support
    authorize(monkeypatch)
    catalog = Catalog()
    execute = catalog.execute
    def checked(sql, params=None):
        assert catalog.read_only
        return execute(sql, params)
    catalog.execute = checked
    monkeypatch.setattr(support, 'connect_target', lambda target: catalog)
    assert support.isolated_connection(LOCAL, autocommit=True) is catalog
    assert ('ROLLBACK', None) in catalog.events and not catalog.read_only and catalog.autocommit

@pytest.fixture
def ca(monkeypatch, tmp_path):
    trustme = pytest.importorskip('trustme')
    path = tmp_path / 'audit-ca.pem'
    trustme.CA().cert_pem.write_to_path(path)
    monkeypatch.setenv('GEONAF_DB_SSLROOTCERT', str(path))
    return str(path)


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost', '[::1]', 'remote.invalid'])
def test_alembic_uses_real_sqlalchemy_creator_and_verified_driver_parameters(monkeypatch, ca, host):
    import psycopg
    monkeypatch.setenv('GEONAF_DATABASE_URL', f'postgresql+psycopg://audit:synthetic@{host}/audit_test')
    calls = []
    def stop(*args, **kwargs):
        calls.append((args, kwargs))
        raise psycopg.OperationalError('SENSITIVE_DRIVER_DETAILS')
    monkeypatch.setattr(psycopg, 'connect', stop)
    with pytest.raises(db.DatabaseConfigError) as caught:
        run_env(monkeypatch)
    assert 'SENSITIVE' not in str(caught.value)
    assert len(calls) == 1 and calls[0][0] == ()
    params = calls[0][1]
    assert params['host'] == host.strip('[]') and params['dbname'] == 'audit_test'
    assert params['gssencmode'] == 'disable' and params['prepare_threshold'] is None
    import os
    assert params['passfile'] == os.devnull
    if host == 'remote.invalid':
        assert params['sslmode'] == 'verify-full' and params['sslrootcert'] == ca
        assert 'hostaddr' not in params
    else:
        assert params['hostaddr'] == ('::1' if host == '[::1]' else '127.0.0.1')


@pytest.mark.parametrize('mode', ['disable', 'allow', 'prefer', 'require', 'verify-ca'])
def test_alembic_refuses_weak_remote_tls_before_sqlalchemy(monkeypatch, ca, mode):
    monkeypatch.setenv('GEONAF_DATABASE_URL', f'postgresql://audit@remote.invalid/audit_test?sslmode={mode}')
    calls = []
    monkeypatch.setattr(db, 'create_engine', lambda t: calls.append(t))
    with pytest.raises(db.DatabaseConfigError): run_env(monkeypatch)
    assert calls == []


@pytest.mark.parametrize('bad_ca', ['', 'missing-audit-ca.pem'])
def test_alembic_refuses_missing_or_invalid_ca(monkeypatch, bad_ca):
    monkeypatch.setenv('GEONAF_DATABASE_URL', 'postgresql://audit@remote.invalid/audit_test')
    monkeypatch.setenv('GEONAF_DB_SSLROOTCERT', bad_ca)
    calls = []
    monkeypatch.setattr(db, 'create_engine', lambda t: calls.append(t))
    with pytest.raises(db.DatabaseConfigError): run_env(monkeypatch)
    assert calls == []


@pytest.mark.parametrize('consumer', ['alembic', 'fixture'])
def test_remote_bootstrap_or_destructive_fixture_never_connects(monkeypatch, ca, consumer):
    from tests import postgis_support as support
    authorize(monkeypatch)
    url = 'postgresql://audit@remote.invalid/audit_test'
    monkeypatch.setenv('GEONAF_DATABASE_URL', url)
    calls = []
    monkeypatch.setattr(db, 'create_engine', lambda t: calls.append(t))
    monkeypatch.setattr(support, 'connect_target', lambda t: calls.append(t))
    # Fixture URL carries CA because it deliberately does not inherit GEONAF_DATABASE_URL options.
    from urllib.parse import quote
    url += '?sslmode=verify-full&sslrootcert=' + quote(ca, safe='')
    with pytest.raises(db.DatabaseConfigError):
        run_env(monkeypatch) if consumer == 'alembic' else support.isolated_connection(url)
    assert calls == []


@pytest.mark.parametrize('module_name, fixture_name', [
    ('test_postgis_territory_integration', 'database'),
    ('test_postgis_storage_integration', 'local'),
    ('test_supabase_corrections_integration', 'db'),
    ('test_supabase_legacy_rename_integration', 'connect'),
    ('test_supabase_legacy_verify_integration', 'db'),
    ('test_supabase_state_diagnostic_integration', 'db'),
])
def test_actual_destructive_fixtures_stop_before_drop_on_wrong_identity(monkeypatch, module_name, fixture_name):
    import importlib
    from tests import postgis_support as support
    authorize(monkeypatch)
    catalog = Catalog(); catalog.identity = ('audit_test', None)
    module = importlib.import_module('tests.' + module_name)
    monkeypatch.setattr(module, 'TEST_URL', LOCAL)
    monkeypatch.setattr(support, 'connect_target', lambda target: catalog)
    with pytest.raises(db.EnvironmentIdentityError):
        next(getattr(module, fixture_name).__wrapped__())
    assert catalog.closed and all(q.startswith('SELECT') for q, _ in catalog.events)


def test_unmarked_local_base_not_implicitly_initialized_even_with_test_capability(monkeypatch):
    authorize(monkeypatch, bootstrap=False)
    catalog = Catalog()
    context = Context(catalog, [step(r) for r in REVISIONS])
    with pytest.raises(db.DatabaseConfigError): run_env(monkeypatch, Engine(catalog), context)
    assert not context.ran


def test_noop_existing_migration_preserves_history(monkeypatch):
    catalog = Catalog(REVISIONS[1], journal=True)
    initial = list(catalog.history)
    run_env(monkeypatch, Engine(catalog), Context(catalog))
    assert catalog.history == initial and not any(q == 'DDL' or q.startswith('INSERT') for q, _ in catalog.events)


def test_journal_append_is_rolled_back_when_revision_fails(monkeypatch):
    catalog = Catalog(REVISIONS[0], journal=True)
    initial = list(catalog.history)
    with pytest.raises(db.DatabaseConfigError):
        run_env(monkeypatch, Engine(catalog), Context(catalog, [step(REVISIONS[1])], fail=True))
    assert catalog.heads == [REVISIONS[0]] and catalog.history == initial


def test_stamp_on_empty_database_refused_before_version_table_creation(monkeypatch):
    authorize(monkeypatch)
    catalog = Catalog(); context = Context(catalog, [step(REVISIONS[0], stamp=True)])
    with pytest.raises(db.DatabaseConfigError): run_env(monkeypatch, Engine(catalog), context)
    assert not context.ran and not catalog.heads
