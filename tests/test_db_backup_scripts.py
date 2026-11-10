"""Protections of the PowerShell backup scripts of the legacy geonaf schema (D2.9c, scripts/db_backup).

Three levels, none of which contacts Supabase:
- static checks of the script text (always run);
- behaviour of the refusals, by running the scripts with pwsh in dry-run or refusal paths (skipped without
  pwsh); no connection is opened: every case stops before any PostgreSQL tool is called;
- end to end on a LOCAL throwaway server (marker ``postgis``): backup, offline check, restore into a new
  database, structure comparison. Needs ``GEONAF_TEST_DATABASE_URL`` (database name ending with ``_test``)
  and ``GEONAF_BACKUP_PGBIN`` (directory of pg_dump, pg_restore and psql of the same major as that server).
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

import pytest

# Same helpers as tests/test_supabase_sql.py, copied so that this module needs no project import (it also runs
# in a minimal environment holding only pytest and psycopg, next to a local server).
READ_ONLY_GUARD = "SELECT set_config('transaction_read_only', 'on', true);"


def code_only(sql: str) -> str:
    """SQL without comments nor quoted literals."""
    return re.sub(r"'(?:[^']|'')*'", "''", "\n".join(line.split("--")[0] for line in sql.splitlines()))


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" / "db_backup"
COMMON = SCRIPTS / "GeonafBackup.Common.ps1"
BACKUP = SCRIPTS / "Invoke-GeonafSchemaBackup.ps1"
CHECK = SCRIPTS / "Test-GeonafBackupArchive.ps1"
RESTORE = SCRIPTS / "Restore-GeonafBackupIsolated.ps1"
PREREQ = SCRIPTS / "Test-GeonafBackupPrerequisites.ps1"
STRUCTURE = SCRIPTS / "legacy_structure.sql"
POWERSHELL = (BACKUP, CHECK, RESTORE, PREREQ, COMMON)
PWSH = shutil.which("pwsh")
POOLER = "aws-0-eu-west-3.pooler.supabase.com"     # Shape of a pooler host; never contacted (dry run or refusal).


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- Static checks -----------------------------------------------------------------------------------

def test_backup_files_are_the_expected_ones():
    assert sorted(p.name for p in SCRIPTS.iterdir() if p.is_file()) == sorted(
        [p.name for p in POWERSHELL] + [STRUCTURE.name])


@pytest.mark.parametrize("path", POWERSHELL + (STRUCTURE,), ids=lambda p: p.name)
def test_no_secret_connection_string_or_project_reference_in_the_scripts(path):
    content = text(path)
    assert not re.search(r"postgres(ql)?(\+\w+)?://", content, re.I)
    assert not re.search(r"(password|pwd)\s*=\s*['\"][^'\"]+", content, re.I)
    assert not re.search(r"PGPASSWORD\s*=\s*['\"]", content)
    assert not re.search(r"\beyJ[A-Za-z0-9_-]{10,}", content)                 # JWT (anon / service_role key).
    assert not re.search(r"\b(sb_secret|sb_publishable|sbp)_[A-Za-z0-9]", content)
    assert not re.search(r"(?<![a-z\\.])[a-z]{20}\.supabase\.co", content)   # A literal project reference.
    assert "ext-anyandwi" not in content and "Eramet" not in content and "—" not in content


@pytest.mark.parametrize("path", POWERSHELL, ids=lambda p: p.name)
def test_scripts_never_drop_clean_or_overwrite(path):
    content = text(path)
    for forbidden in (r"--clean\b", r"--if-exists\b", r"--create\b", r"\bDROP\s+(DATABASE|SCHEMA|TABLE|ROLE)\b",
                      r"\bdropdb\b", r"\bTRUNCATE\b", r"Remove-Item\s+-Recurse", r"-Force\b"):
        assert not re.search(forbidden, content, re.I), (path.name, forbidden)


def test_connection_settings_never_travel_on_a_command_line():
    """Host, user, port and password go through PG* variables of the process; the arguments never hold them."""
    for path in (BACKUP, RESTORE):
        content = text(path)
        for flag in ("'-h'", "'--host", "'-U'", "'--username", "'-p'", "'--port", "'-W'", "password="):
            assert flag not in content, (path.name, flag)
        assert "Set-GeonafConnection" in content and "Assert-GeonafCleanPgEnvironment" in content
        tail = content[content.rindex("} finally {"):]
        assert "Clear-GeonafConnection" in tail, path.name                    # Always cleared, even on error.
    common = text(COMMON)
    assert "Read-Host -AsSecureString" in common and "ZeroFreeBSTR" in common
    assert "'PGPASSWORD'" in common.split("$script:GeonafConnectionVariables")[1].split(")")[0]


@pytest.mark.parametrize("path", (BACKUP, RESTORE), ids=lambda p: p.name)
def test_writing_scripts_are_dry_runs_unless_execute(path):
    content = text(path)
    assert "[switch] $Execute" in content
    dry = content.index("if (-not $Execute) {")
    assert content.index("SIMULATION : aucune connexion") > dry
    for first_contact in ("Read-GeonafPassword", "New-GeonafPrivateDirectory", "Invoke-GeonafNative"):
        assert content.index(first_contact) > dry, first_contact


def test_backup_reads_only_and_uses_the_reviewed_scripts():
    content = text(BACKUP)
    assert "'--format=custom', '--schema=geonaf', '--lock-wait-timeout=10s'" in content
    assert "db\\supabase\\09_legacy_schema_fingerprint.sql" in content and "legacy_structure.sql" in content
    assert "'-1'" in content            # psql in one transaction: the read-only guard covers every statement.
    assert content.count("set_config('transaction_read_only', 'on', true)") == 2      # server.sql, rowcounts.sql.
    assert "Test-GeonafBackupArchive.ps1" in content


def test_restore_targets_a_new_local_database_only():
    content = text(RESTORE)
    assert "'^geonaf_restore_[a-z0-9_]{1,40}$'" in content
    assert content.index("Test-GeonafLoopbackHost $TargetHost") < content.index("Invoke-GeonafNative")
    assert content.index("existe deja") < content.index("CREATE DATABASE")
    assert "'--no-owner', '--no-acl', '--exit-on-error', '--single-transaction'" in content


def test_legacy_structure_is_read_only_and_independent_of_oids_and_collation():
    sql = text(STRUCTURE)
    statements = [s.strip() for s in re.sub(r"--[^\n]*", "", sql).split(";") if s.strip()]
    assert statements[0] + ";" == READ_ONLY_GUARD
    assert statements[1] == "SELECT set_config('search_path', '', true)" and len(statements) == 3
    code = code_only(sql)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|GRANT|REVOKE|TRUNCATE|COMMENT ON|LOCK)\b", code, re.I)
    items = sql[sql.index("items AS ("):sql.index("), info AS (")]
    hashed = sql[sql.index("SELECT 'empreinte'"):]
    assert "FROM items" in hashed and "FROM info" not in hashed               # Only the structure rows are hashed.
    for hashed_out in ("relacl", "relowner", "nspacl", "nspowner"):
        assert hashed_out not in items, hashed_out                            # Owners and grants are information only.
    assert 'ORDER BY kind COLLATE "C", name COLLATE "C"' in sql


def test_safe_paths_reject_the_repository_and_onedrive_in_the_code():
    common = text(COMMON)
    for rule in ("Test-GeonafPathUnder $full $script:GeonafRepoRoot", "'OneDrive', 'OneDriveCommercial', 'OneDriveConsumer'",
                 "/inheritance:r", "le dossier existe deja"):
        assert rule in common, rule


def test_scripts_resolve_paths_and_tools_on_windows_and_linux():
    """Linux CI (demo-pack-linux): no USERPROFILE, executables without .exe, / as separator."""
    for path in (BACKUP, RESTORE, PREREQ):
        content = text(path)
        assert "$env:USERPROFILE" not in content, path.name
        assert "[Environment]::GetFolderPath('UserProfile')" in content, path.name
    common = text(COMMON)
    finder = common[common.index("function Find-GeonafPgTool"):common.index("function Get-GeonafPgToolMajor")]
    assert 'if ($script:GeonafOnWindows) { "$Name.exe" } else { $Name }' in finder
    under = common[common.index("function Test-GeonafPathUnder"):common.index("function Assert-GeonafSafeOutputPath")]
    assert "DirectorySeparatorChar" in under and "TrimEnd('\\') + '\\'" not in under
    assert "'[\\\\/]OneDrive( - [^\\\\/]+)?([\\\\/]|$)'" in common       # Folder name refused with \ and with /.


def test_private_directory_is_refused_outside_windows_before_creation():
    """icacls is the only way rights are restricted: elsewhere nothing is created (refusal by default)."""
    common = text(COMMON)
    body = common[common.index("function New-GeonafPrivateDirectory"):common.index("function Find-GeonafPgTool")]
    refusal = body.index("if (-not $script:GeonafOnWindows) { Stop-Geonaf")
    assert body.index("existe deja") < refusal < body.index("New-Item -ItemType Directory") < body.index("icacls.exe")


# --- Behaviour of the refusals (pwsh, no connection) ---------------------------------------------------

needs_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh absent : comportement des scripts non teste.")


def run_pwsh(script: Path, *args: str, env: dict | None = None, drop: tuple = ()) -> subprocess.CompletedProcess:
    # Windows variable names ignore case (os.environ upper-cases them): replace, never duplicate, a variable.
    overrides = {k.upper(): (k, v) for k, v in (env or {}).items()}
    dropped = {name.upper() for name in drop}
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")
                   and k.upper() not in overrides and k.upper() not in dropped}
    environment.update(dict(overrides.values()))
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(script), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=environment, timeout=120)


def pg_bin_or_skip() -> str:
    """The dry run checks the tools before printing its plan: it needs pg_dump, pg_restore and psql somewhere."""
    found = os.environ.get("GEONAF_BACKUP_PGBIN") or (shutil.which("pg_dump") and str(Path(shutil.which("pg_dump")).parent))
    if not found:
        pytest.skip("outils PostgreSQL absents (PATH ou GEONAF_BACKUP_PGBIN) : chemin a sec non teste.")
    return found


@needs_pwsh
def test_dry_run_backup_masks_the_host_and_writes_nothing(tmp_path):
    bin_dir = pg_bin_or_skip()
    out = tmp_path / "backups"
    result = run_pwsh(BACKUP, "-ProjectLabel", "geonaf-test", "-PgHost", POOLER, "-PgUser", "postgres.abcdefghijklmnopqrst",
                      "-PgBin", str(bin_dir), "-OutputRoot", str(out))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SIMULATION : aucune connexion" in result.stdout and not out.exists()
    assert "eu-west-3" not in result.stdout and "abcdefghijklmnopqrst" not in result.stdout


@needs_pwsh
@pytest.mark.parametrize("args, message", [
    (("-ProjectLabel", "geonaf-test", "-PgHost", "db.example.com", "-PgUser", "postgres"), "hote non reconnu"),
    (("-ProjectLabel", "geonaf-test", "-PgHost", POOLER, "-PgUser", "postgres.x", "-PgPort", "6543"), "port 5432 seulement"),
    (("-ProjectLabel", "geonaf-test", "-PgHost", POOLER, "-PgUser", "postgres.x", "-NoPasswordPrompt"), "reserve a un serveur local"),
    (("-ProjectLabel", "local-test", "-PgHost", POOLER, "-PgUser", "postgres"), "exige un hote local"),
    (("-ProjectLabel", "geonaf-test", "-PgHost", POOLER, "-PgUser", "postgres.x", "-PgDatabase", "x;y"), "nom de base invalide"),
])
def test_backup_refuses_unsafe_parameters_before_anything_else(args, message):
    result = run_pwsh(BACKUP, *args, "-Execute")
    assert result.returncode == 2 and message in result.stdout, result.stdout + result.stderr


@needs_pwsh
@pytest.mark.parametrize("where", ["repository", "onedrive", "onedrive-folder-name"])
def test_backup_refuses_an_output_directory_in_the_repository_or_onedrive(tmp_path, where):
    bin_dir = pg_bin_or_skip()
    env, output = {}, str(ROOT / "backups_interdits")
    if where == "onedrive":
        env, output = {"OneDriveCommercial": str(tmp_path)}, str(tmp_path / "sauvegardes")
    elif where == "onedrive-folder-name":                     # Even when no OneDrive variable points to it.
        output = str(tmp_path / "OneDrive - Exemple" / "sauvegardes")
    result = run_pwsh(BACKUP, "-ProjectLabel", "geonaf-test", "-PgHost", POOLER, "-PgUser", "postgres.x",
                      "-PgBin", str(bin_dir), "-OutputRoot", output, env=env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert ("dans le depot Git" if where == "repository" else "OneDrive") in result.stdout
    assert not (ROOT / "backups_interdits").exists()


@needs_pwsh
@pytest.mark.parametrize("args, message", [
    (("-TargetHost", POOLER, "-TargetDatabase", "geonaf_restore_x"), "hote local seulement"),
    (("-TargetHost", "db.abcdefghijklmnopqrst.supabase.co", "-TargetDatabase", "geonaf_restore_x"), "hote local seulement"),
    (("-TargetDatabase", "postgres"), "geonaf_restore_<suffixe>"),
    (("-TargetDatabase", "geonaf_restore_x; DROP"), "geonaf_restore_<suffixe>"),
])
def test_restore_refuses_any_target_other_than_a_new_local_database(tmp_path, args, message):
    result = run_pwsh(RESTORE, "-BackupDir", str(tmp_path), *args, "-Execute")
    assert result.returncode == 2 and message in result.stdout, result.stdout + result.stderr


@needs_pwsh
@pytest.mark.parametrize("script, args, message", [
    (RESTORE, ("-TargetHost", POOLER, "-TargetDatabase", "geonaf_restore_x"), "hote local seulement"),
    (BACKUP, ("-ProjectLabel", "geonaf-test", "-PgHost", "db.example.com", "-PgUser", "postgres"), "hote non reconnu"),
])
def test_refusals_do_not_depend_on_userprofile(tmp_path, script, args, message):
    """Linux has no USERPROFILE: the default output directory must not fail before the refusal is decided."""
    extra = ("-BackupDir", str(tmp_path)) if script == RESTORE else ()
    result = run_pwsh(script, *extra, *args, "-Execute", drop=("USERPROFILE",))
    assert result.returncode == 2 and message in result.stdout, result.stdout + result.stderr
    assert "GEONAF REFUS" in result.stdout


@needs_pwsh
def test_archive_check_detects_a_modified_file(tmp_path):
    backup = tmp_path / "b"
    backup.mkdir()
    (backup / "geonaf.dump").write_bytes(b"pas une archive")
    good = "0" * 64
    (backup / "SHA256SUMS").write_text(f"{good}  geonaf.dump\n", encoding="ascii")
    (backup / "manifest.json").write_text(json.dumps({
        "format": "geonaf-schema-backup/1", "fichiers_sha256": {"geonaf.dump": good}, "archive": {"sha256": good},
        "sommaire": {}, "structure": {"sha256": ""}, "empreinte_09": {"sha256": ""}}), encoding="utf-8")
    result = run_pwsh(CHECK, "-BackupDir", str(backup))
    assert result.returncode == 1
    assert "[ECHEC] SHA-256 de geonaf.dump" in result.stdout


@needs_pwsh
def test_prerequisites_open_no_connection_by_default(tmp_path):
    result = run_pwsh(PREREQ, "-OutputRoot", str(tmp_path / "out"))
    assert "reseau non teste (-TestNetwork absent, aucune connexion)" in result.stdout
    assert result.returncode in (0, 1)


# --- End to end on a local throwaway server --------------------------------------------------------------

TEST_URL = os.environ.get("GEONAF_TEST_DATABASE_URL", "").strip()
PGBIN = os.environ.get("GEONAF_BACKUP_PGBIN", "").strip()
LEGACY = ("acquisitions", "analysis_acquisitions", "analysis_runs", "indicator_definitions", "indicator_results",
          "sources", "territories", "territory_versions")


@pytest.fixture
def local_source():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    url = "postgresql://" + TEST_URL.split("://", 1)[1] if TEST_URL.startswith("postgresql+psycopg://") else TEST_URL
    info = conninfo_to_dict(url)
    if not str(info.get("dbname", "")).endswith("_test") or info.get("host") not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("Base locale dont le nom se termine par « _test » exigée.")
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")
        c.execute("DROP SCHEMA IF EXISTS geonaf_legacy_draft CASCADE")
        c.execute("CREATE SCHEMA geonaf")
        for table in LEGACY:
            c.execute(f"CREATE TABLE geonaf.{table} (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                      f"label text NOT NULL DEFAULT 'x' CHECK (label <> ''))")
            c.execute(f"ALTER TABLE geonaf.{table} ENABLE ROW LEVEL SECURITY")
        c.execute("CREATE INDEX sources_label ON geonaf.sources (label)")
        c.execute("COMMENT ON TABLE geonaf.sources IS 'table « héritée »'")
        c.execute("CREATE POLICY lecture ON geonaf.sources FOR SELECT USING (label IS NOT NULL)")
        c.execute("INSERT INTO geonaf.sources (label) VALUES ('une ligne')")
    yield url, info
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")


@pytest.mark.postgis
@needs_pwsh
@pytest.mark.skipif(not (TEST_URL and PGBIN), reason="GEONAF_TEST_DATABASE_URL et GEONAF_BACKUP_PGBIN requis.")
def test_backup_check_and_isolated_restore_end_to_end(local_source, tmp_path):
    import psycopg

    url, info = local_source
    out = tmp_path / "backups"
    common = ["-PgBin", PGBIN, "-OutputRoot", str(out), "-NoPasswordPrompt", "-Execute"]
    backup = run_pwsh(BACKUP, "-ProjectLabel", "local-test", "-PgHost", "127.0.0.1", "-PgPort", str(info.get("port", 5432)),
                      "-PgUser", info["user"], "-PgDatabase", info["dbname"], *common)
    assert backup.returncode == 0, backup.stdout + backup.stderr
    assert "VERIFICATION CONFORME" in backup.stdout and "SAUVEGARDE TERMINEE ET VERIFIEE" in backup.stdout
    (directory,) = (out / "local-test").iterdir()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["tables"] == sorted(LEGACY) and manifest["lignes"]["sources"] == 1
    assert manifest["sommaire"]["TABLE"] == 8 and manifest["sommaire"]["POLICY"] == 1
    assert manifest["empreinte_09"]["oid_geonaf"] != "absent"

    target = f"geonaf_restore_{uuid.uuid4().hex[:10]}"
    restore_args = ["-BackupDir", str(directory), "-TargetDatabase", target, "-TargetPort", str(info.get("port", 5432)),
                    "-TargetUser", info["user"], *common]
    try:
        restored = run_pwsh(RESTORE, *restore_args)
        assert restored.returncode == 0, restored.stdout + restored.stderr
        assert "structure : identique" in restored.stdout and "lignes : identiques" in restored.stdout
        again = run_pwsh(RESTORE, *restore_args)                     # Never into an existing database.
        assert again.returncode == 2 and "existe deja" in again.stdout
    finally:
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{target}"')        # Test cleanup of the local server only.

    archive = directory / "geonaf.dump"
    archive.write_bytes(archive.read_bytes() + b"x")                  # Tampering is detected offline.
    check = run_pwsh(CHECK, "-BackupDir", str(directory), "-PgBin", PGBIN)
    assert check.returncode == 1 and "[ECHEC] SHA-256 de geonaf.dump" in check.stdout
