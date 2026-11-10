"""Offline CI: D3/D5 Python tests and the Node tests, without any database, secret or network service.

The selection is the explicit list of scripts/ci/offline-tests.json. Fail closed: a non-zero exit code,
a failure, an error, a skip, a todo or a listed file without any test invalidates the run. Tests that need
a database carry the ``postgis`` marker and are deselected here; the PostGIS workflow covers those it lists.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "scripts/ci/offline-tests.json"


def selection():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return data["pytest"], data["node"]


def pytest_valid(raw, files, exit_code):
    """Every listed file contributes at least one case; no failure, error or skip anywhere."""
    try:
        cases = ET.parse(raw).getroot().findall(".//testcase")
    except (ET.ParseError, OSError):
        return False, 0
    # JUnit classname: "tests.test_x" for a module function, "tests.test_x.Class" for a method.
    names = [case.get("classname", "") for case in cases]
    prefixes = [f[:-3].replace("/", ".") for f in files]
    covered = all(any(n == p or n.startswith(p + ".") for n in names) for p in prefixes)
    clean = all(case.find(tag) is None for case in cases for tag in ("failure", "error", "skipped"))
    return exit_code == 0 and bool(cases) and covered and clean, len(cases)


def node_valid(tap, exit_code):
    """TAP summary of node --test: at least one pass, nothing failed, cancelled, skipped or todo."""
    counts = {key: int(value) for key, value in re.findall(r"^# (tests|pass|fail|cancelled|skipped|todo) (\d+)$",
                                                          tap, re.M)}
    valid = (exit_code == 0 and set(counts) == {"tests", "pass", "fail", "cancelled", "skipped", "todo"}
             and counts["tests"] == counts["pass"] > 0
             and counts["fail"] == counts["cancelled"] == counts["skipped"] == counts["todo"] == 0)
    return valid, counts.get("pass", 0)


def main():
    if os.environ.get("GEONAF_TEST_DATABASE_URL", "").strip():
        print("Offline validation refuses a database URL.")
        return 1
    files, scripts = selection()
    environ = {key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"}
    environ.update(GEONAF_DEMO_PACK="off", GEONAF_DB_BACKEND="file", PYTHONDONTWRITEBYTECODE="1",
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    with tempfile.TemporaryDirectory(prefix="geonaf-offline-") as temp:
        raw = Path(temp) / "pytest.xml"
        code = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-rs", "--strict-markers",
                               "-p", "no:cacheprovider", "-m", "not postgis", "--junitxml=" + str(raw),
                               "--basetemp=" + str(Path(temp) / "pytest"), *files],
                              cwd=ROOT, env=environ, timeout=900).returncode
        python_ok, python_count = pytest_valid(raw, files, code)
    node = subprocess.run(["node", "--test", "--test-reporter=tap", *scripts], cwd=ROOT, env=environ,
                          capture_output=True, text=True, encoding="utf-8", timeout=300)
    print(node.stdout[-4000:] + node.stderr[-4000:])
    node_ok, node_count = node_valid(node.stdout, node.returncode)
    print(f"Python: {'PASS' if python_ok else 'FAIL'} ({python_count} cas) | "
          f"Node: {'PASS' if node_ok else 'FAIL'} ({node_count} cas)")
    return 0 if python_ok and node_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
