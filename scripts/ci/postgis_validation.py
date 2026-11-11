"""Ephemeral GitHub-hosted PostGIS validation. Never accepts an external database URL.

Provisioning alone uses the container administrator. Tests use the existing guarded
connector as a non-superuser. Raw output stays private in RUNNER_TEMP.
"""
from __future__ import annotations

import json
import importlib.metadata
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
IMAGES = {
    "plain": "postgres:16-bookworm@sha256:5983db9a9c6ad225a48a46cfe2e03e6680b1826662befd0465eb05b34cb1c64e",
    "public": "postgis/postgis:16-3.6@sha256:5f97c32e0f92a0d65b253cea1b14e356718220c23ee78b25425eabe5835540d4",
    "extensions": "postgis/postgis:16-3.6@sha256:5f97c32e0f92a0d65b253cea1b14e356718220c23ee78b25425eabe5835540d4",
    # D2.6: PostgreSQL 17 without PostGIS (Supabase SQL Editor scripts), and the Supabase build
    # of PostgreSQL 17 with PostGIS 3.3.7 (no official postgis/postgis 17-3.3 image exists).
    "pg17": "postgres:17.11-bookworm@sha256:665dc7b37ac52a17e298511414c21d23ffa4e6a6ef6225b3e5d59c7afb35e994",
    "supabase17": "supabase/postgres:17.11.0.018@sha256:7c31363a3f7434ff14945f1b8a4880e6eca8b362e197e88b87c476c77aa3cf8c",
}
# Checked after provisioning, before any test: server major and PostGIS (schema, version) or None.
EXPECTED = {
    "plain": (16, None),
    "public": (16, ("public", "3.6.4")),
    "extensions": (16, ("extensions", "3.6.4")),
    "pg17": (17, None),
    "supabase17": (17, ("extensions", "3.3.7")),
}
# Container administrator (POSTGRES_USER of the image); used for provisioning only.
ADMIN = {"supabase17": "supabase_admin"}
# Test logins and the variable that carries each URL. The pg17 scripts create the GEONAF roles
# themselves, so that variant has a single login, still neither superuser nor BYPASSRLS.
LOGINS = {"pg17": {"geonaf_ci_runner": "GEONAF_TEST_DATABASE_URL"}}
DEFAULT_LOGINS = {"geonaf_migrator": "GEONAF_TEST_DATABASE_URL", "geonaf_reader": "GEONAF_CI_READER_URL",
                  "geonaf_importer": "GEONAF_CI_IMPORTER_URL"}


def required(variant):
    return json.loads((ROOT / "scripts/ci/postgis-tests.json").read_text())[variant]


def runner_directory(variant):
    if (variant not in IMAGES or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or os.environ.get("RUNNER_OS") != "Linux" or platform.system() != "Linux"
            or not re.fullmatch(r"[0-9]+", os.environ.get("GITHUB_RUN_ID", ""))
            or not re.fullmatch(r"[0-9]+", os.environ.get("GITHUB_RUN_ATTEMPT", ""))):
        raise RuntimeError("GitHub-hosted runner required")
    if any(key.startswith("PG") and value for key, value in os.environ.items()):
        raise RuntimeError("Implicit libpq configuration refused")
    if any(os.environ.get(key) for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")):
        raise RuntimeError("External Docker configuration refused")
    base = Path(os.environ["RUNNER_TEMP"])
    if not base.is_absolute() or not base.is_dir():
        raise RuntimeError("Invalid runner temporary directory")
    return base / ("geonaf-postgis-" + variant)


def docker(*args):
    result = subprocess.run(["docker", "--host", "unix:///var/run/docker.sock", *args],
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError("Container operation failed (details withheld)")
    return result.stdout.strip()


def owned_container(name, variant, label):
    item = json.loads(docker("inspect", name))[0]
    if (item["Name"] != "/" + name or item["Config"]["Image"] != IMAGES[variant]
            or item["Config"].get("Labels", {}).get("geonaf.ci") != label):
        raise RuntimeError("Container ownership mismatch")
    return item


def mask(value):
    # Generated hex passwords and local URLs contain no workflow command escapes.
    print("::add-mask::" + value, flush=True)


def provision(variant, directory, name, label):
    if directory != runner_directory(variant):
        raise RuntimeError("Unexpected provisioning directory")
    import psycopg
    from psycopg import sql

    password = secrets.token_hex(32)
    mask(password)
    envfile = directory / "container.env"
    envfile.write_text("POSTGRES_PASSWORD=" + password + "\nPOSTGRES_DB=postgres\n")
    envfile.chmod(0o600)
    docker("run", "--detach", "--name", name, "--label", "geonaf.ci=" + label,
           "--publish", "127.0.0.1::5432", "--env-file", str(envfile),
           "--health-cmd", "pg_isready -h 127.0.0.1 -U postgres -d postgres", "--health-interval", "5s",
           "--health-timeout", "5s", "--health-retries", "24", IMAGES[variant])
    for _ in range(60):
        item = owned_container(name, variant, label)
        if item["State"]["Health"]["Status"] == "healthy":
            break
        time.sleep(2)
    else:
        raise RuntimeError("Database readiness timeout")
    bindings = item["NetworkSettings"]["Ports"]["5432/tcp"]
    if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
        raise RuntimeError("Non-local container binding")
    port = int(bindings[0]["HostPort"])
    kwargs = dict(host="127.0.0.1", hostaddr="127.0.0.1", port=port, user=ADMIN.get(variant, "postgres"),
                  password=password, dbname="postgres", sslmode="disable", passfile=os.devnull,
                  gssencmode="disable", connect_timeout=5, prepare_threshold=None, autocommit=True)
    database = "geonaf_ci_" + variant + "_test"
    identity = str(uuid.uuid4())
    major, extension = EXPECTED[variant]
    logins = LOGINS.get(variant, DEFAULT_LOGINS)
    owner = next(iter(logins))
    credentials = {}
    with psycopg.connect(**kwargs) as connection:
        for role in logins:
            credentials[role] = secrets.token_hex(32)
            mask(credentials[role])
            # CREATEROLE only for the pg17 runner: PostgreSQL 16+ limits it to the roles it creates.
            connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB {} NOBYPASSRLS PASSWORD {}").format(
                sql.Identifier(role), sql.SQL("CREATEROLE" if variant == "pg17" else "NOCREATEROLE"),
                sql.Literal(credentials[role])))
        if variant == "pg17":
            # Membership in the roles it creates, so that test cleanup (DROP OWNED) works.
            connection.execute(sql.SQL("ALTER ROLE {} SET createrole_self_grant = 'set, inherit'").format(sql.Identifier(owner)))
        else:
            connection.execute("GRANT geonaf_reader, geonaf_importer TO geonaf_migrator")
        connection.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0").format(
            sql.Identifier(database), sql.Identifier(owner)))
        connection.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            sql.Identifier(database), sql.Literal("geonaf-isolated-test:" + identity)))
        schema = extension[0] if extension else "public"
        path = "pg_catalog, public" if schema == "public" else "pg_catalog, extensions, public"
        connection.execute(sql.SQL("ALTER DATABASE {} SET search_path TO " + path).format(sql.Identifier(database)))
    kwargs["dbname"] = database
    with psycopg.connect(**kwargs) as connection:
        if connection.execute("SELECT 1 FROM pg_extension WHERE extname='postgis'").fetchone():
            raise RuntimeError("New database unexpectedly contains PostGIS")
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        if extension:
            if schema == "extensions":
                connection.execute("CREATE SCHEMA extensions")
            connection.execute(sql.SQL("CREATE EXTENSION postgis WITH SCHEMA {}").format(sql.Identifier(schema)))
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.SQL(", ").join(map(sql.Identifier, logins))))
        actual = connection.execute("SELECT n.nspname, e.extversion FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='postgis'").fetchone()
        version = int(connection.execute("SHOW server_version_num").fetchone()[0])
        if version // 10000 != major or (tuple(actual) if actual else None) != extension:
            raise RuntimeError("Unexpected database or extension version/schema")
    environ = dict(os.environ)
    # Reject inherited libpq configuration, rather than silently overriding policy.
    if any(key.startswith("PG") and value for key, value in environ.items()):
        raise RuntimeError("Implicit libpq configuration refused")
    for role, secret in credentials.items():
        url = f"postgresql://{role}:{secret}@127.0.0.1:{port}/{database}"
        mask(url)
        environ[logins[role]] = url
    environ.update(GEONAF_ALLOW_DESTRUCTIVE_TESTS="1", GEONAF_TEST_DATABASE_NAME=database,
                   GEONAF_TEST_INSTANCE_ID=identity, GEONAF_DB_ENV="test", GEONAF_DB_BACKEND="file",
                   GEONAF_DATABASE_URL="", GEONAF_DB_INTEGRATION="0", GEONAF_DEMO_PACK="off",
                   GEONAF_CI_VARIANT=variant, PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                   GABON_GEOAI_DATA_DIR=str(directory / "empty-data"), MPLCONFIGDIR=str(directory / "mpl"),
                   XDG_CACHE_HOME=str(directory / "cache"))
    environ.pop("PYTEST_ADDOPTS", None)
    environ.pop("GEONAF_DEMO_PACK_DIR", None)
    versions = {name: importlib.metadata.version(name) for name in
                ("pytest", "alembic", "psycopg", "SQLAlchemy", "numpy", "rasterio", "shapely", "pyproj")}
    (directory / "reports" / "versions.json").write_text(json.dumps({"postgres": version, "postgis": actual,
        "image": IMAGES[variant], "python": platform.python_version(), "packages": versions}))
    return environ


def report(raw, nodes, destination, exit_code):
    """Allowlisted JUnit reconstruction; no exception text, properties or logs escape."""
    destination.mkdir(parents=True, exist_ok=True)
    # Only the file part is converted: parameter ids may themselves contain "/" (e.g. [db/supabase/x.sql]).
    expected = {node.split("::", 1)[0][:-3].replace("/", ".") + "." + node.split("::", 1)[1] for node in nodes}
    try:
        cases = ET.parse(raw).getroot().findall(".//testcase")
    except (ET.ParseError, OSError):
        cases = []
    identities = [case.get("classname", "") + "." + case.get("name", "") for case in cases]
    valid = (exit_code == 0 and len(nodes) == len(expected) == len(cases)
             and set(identities) == expected
             and all(not any(case.find(tag) is not None for tag in ("failure", "error", "skipped")) for case in cases))
    suite = ET.Element("testsuite", name="mandatory-postgis", tests=str(len(nodes) + 1), failures="0" if valid else "1")
    for node in nodes:
        filename, name = node.split("::")
        case = ET.SubElement(suite, "testcase", classname=filename[:-3].replace("/", "."), name=name)
        identity = filename[:-3].replace("/", ".") + "." + name
        matches = [c for c, key in zip(cases, identities) if key == identity]
        if len(matches) == 1 and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", matches[0].get("time", "")):
            case.set("time", matches[0].get("time"))
        if len(matches) != 1 or any(matches[0].find(t) is not None for t in ("failure", "error", "skipped")):
            ET.SubElement(case, "failure", message="Required validation did not pass")
    contract = ET.SubElement(suite, "testcase", classname="ci", name="complete_required_selection")
    if not valid:
        ET.SubElement(contract, "failure", message="Missing, skipped, unexpected, failed validation or runner error")
    suite.set("failures", str(len(suite.findall("./testcase/failure"))))
    ET.ElementTree(suite).write(destination / "junit.xml", encoding="utf-8", xml_declaration=True)
    (destination / "summary.json").write_text(json.dumps({"valid": valid, "required": len(nodes), "observed": len(cases)}))
    return valid


def main(variant):
    directory = runner_directory(variant)  # Before directories, Docker or database operations.
    directory.mkdir(mode=0o700, exist_ok=False)
    (directory / "reports").mkdir()
    label = os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
    name = "geonaf-ci-" + label + "-" + variant
    nodes = required(variant)
    raw = directory / "raw.xml"
    code = 1
    try:
        environ = provision(variant, directory, name, label)
        with (directory / "private-pytest.log").open("w") as output:
            process = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "--strict-markers",
                "-p", "no:cacheprovider", "--tb=no", "--junitxml=" + str(raw),
                "--basetemp=" + str(directory / "pytest"), *nodes], cwd=ROOT, env=environ,
                stdout=output, stderr=subprocess.STDOUT, timeout=900)
        code = process.returncode
    except Exception:
        print("Validation infrastructure failed; sensitive diagnostics withheld.")
    finally:
        try:
            owned_container(name, variant, label)
            docker("rm", "--force", "--volumes", name)
        except Exception:
            code = 1
            print("Container cleanup not confirmed; ephemeral runner disposal remains required.")
        valid = report(raw, nodes, directory / "reports", code)
    print("Required validations: " + ("PASS" if valid else "FAIL"))
    return 0 if valid else 1


if __name__ == "__main__":
    try:
        result = main(sys.argv[1])
    except Exception:
        print("Validation refused (runner contract).")
        result = 1
    raise SystemExit(result)
