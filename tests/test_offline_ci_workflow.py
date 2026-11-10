"""Offline contract tests for the offline-tests workflow (D3/D5 Python tests and Node tests)."""
import json
from pathlib import Path

import pytest

from scripts.ci import offline_validation as offline

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/offline-tests.yml"


def test_workflow_is_read_only_pinned_and_secret_free():
    # JSON notation is a strict YAML 1.2 subset: no optional local YAML dependency.
    workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == ["db/d2a-territory-registry", "main"]
    assert workflow["on"]["pull_request"] == {"branches": ["main"]}
    job = workflow["jobs"]["offline"]
    assert job["runs-on"] == "ubuntu-24.04" and job["timeout-minutes"] <= 30
    steps = job["steps"]
    for step in steps:
        if "uses" in step:
            assert len(step["uses"].split("@")[1]) == 40
    assert steps[0]["with"]["persist-credentials"] is False
    versions = {step["uses"].split("@")[0]: step.get("with", {}) for step in steps if "uses" in step}
    assert versions["actions/setup-python"]["python-version"] == "3.13.18"
    assert versions["actions/setup-node"]["node-version"] == "22.14.0"
    text = json.dumps(workflow)
    assert "secrets." not in text and "SUPABASE" not in text.upper() and "DATABASE_URL" not in text
    assert steps[-1]["run"] == "python -B scripts/ci/offline_validation.py"


def test_every_workflow_checks_pull_requests_and_pushes_to_main_read_only():
    """Integration into main is checked before (pull request) and after (push), without any write right."""
    for name in ("postgis-validation.yml", "offline-tests.yml"):
        workflow = json.loads((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))
        assert "main" in workflow["on"]["push"]["branches"], name
        assert workflow["on"]["pull_request"] == {"branches": ["main"]}, name
        assert workflow["permissions"] == {"contents": "read"}, name
    # demo-pack-linux.yml is plain YAML: its trigger and permission blocks are checked as text.
    demo = (ROOT / ".github/workflows/demo-pack-linux.yml").read_text(encoding="utf-8")
    assert "on:\n  push:\n    branches: [deploy/cloud-demo-akanda, main]\n" in demo
    assert "\n  pull_request:\n    branches: [main]\n" in demo
    assert "\npermissions:\n  contents: read\n\n" in demo and demo.count("permissions:") == 1
    # The full suite holds the D2-D4 database unit tests: the existing DB pins must be installed.
    assert "run: python -m pip install -r requirements-ci-postgis.txt\n" in demo
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "pull_request_target" not in text and "secrets." not in text, path.name


def test_manifest_lists_the_d3_d5_files_and_every_node_test():
    files, scripts = offline.selection()
    assert len(files) == len(set(files)) and len(scripts) == len(set(scripts))
    for name in ("test_edge_acquisitions_d5.py", "test_supabase_acquisitions_streamlit.py",
                 "test_generate_observations_sql.py"):
        assert "tests/" + name in files
    assert all((ROOT / path).is_file() for path in files + scripts)
    found = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests/frontend").glob("test_*.mjs"))
    assert sorted(scripts) == found, "every Node test file must be listed"


def junit(tmp_path, cases):
    root = "<testsuites><testsuite>" + "".join(cases) + "</testsuite></testsuites>"
    path = tmp_path / "raw.xml"
    path.write_text(root)
    return path


FILES = ["tests/test_a.py", "tests/test_b.py"]
GOOD = ['<testcase classname="tests.test_a" name="test_x"/>', '<testcase classname="tests.test_b.Group" name="test_y"/>']


def test_pytest_report_passes_only_when_complete_and_clean(tmp_path):
    assert offline.pytest_valid(junit(tmp_path, GOOD), FILES, 0) == (True, 2)
    assert not offline.pytest_valid(junit(tmp_path, GOOD), FILES, 1)[0]
    assert not offline.pytest_valid(junit(tmp_path, GOOD[:1]), FILES, 0)[0], "a listed file without any case"
    assert not offline.pytest_valid(junit(tmp_path, ['<testcase classname="tests.test_ab" name="t"/>', GOOD[1]]),
                                    FILES, 0)[0], "prefix of another module"
    for tag in ("skipped", "failure", "error"):
        bad = GOOD + [f'<testcase classname="tests.test_a" name="test_z"><{tag}/></testcase>']
        assert not offline.pytest_valid(junit(tmp_path, bad), FILES, 0)[0], tag
    broken = tmp_path / "broken.xml"
    broken.write_text("not xml")
    assert not offline.pytest_valid(broken, FILES, 0)[0]
    assert not offline.pytest_valid(tmp_path / "absent.xml", FILES, 0)[0]


SUMMARY = "# tests {t}\n# suites 0\n# pass {p}\n# fail {f}\n# cancelled {c}\n# skipped {s}\n# todo {d}\n"


@pytest.mark.parametrize("values, code, expected", [
    (dict(t=28, p=28, f=0, c=0, s=0, d=0), 0, True),
    (dict(t=28, p=28, f=0, c=0, s=0, d=0), 1, False),
    (dict(t=28, p=27, f=1, c=0, s=0, d=0), 0, False),
    (dict(t=28, p=27, f=0, c=0, s=1, d=0), 0, False),
    (dict(t=28, p=27, f=0, c=0, s=0, d=1), 0, False),
    (dict(t=28, p=27, f=0, c=1, s=0, d=0), 0, False),
    (dict(t=0, p=0, f=0, c=0, s=0, d=0), 0, False),
])
def test_node_summary_fails_closed(values, code, expected):
    assert offline.node_valid(SUMMARY.format(**values), code)[0] is expected


def test_node_summary_missing_is_refused():
    assert not offline.node_valid("ok 1 - something\n", 0)[0]


def test_a_database_url_is_refused_before_running_anything(monkeypatch):
    monkeypatch.setenv("GEONAF_TEST_DATABASE_URL", "postgresql://u:p@127.0.0.1:5432/x_test")
    monkeypatch.setattr(offline.subprocess, "run", lambda *a, **k: pytest.fail("no process may start"))
    assert offline.main() == 1
