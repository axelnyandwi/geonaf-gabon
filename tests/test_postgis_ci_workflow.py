"""Offline contract tests for the isolated GitHub validation runner."""
import ast
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from scripts.ci import postgis_validation as ci

ROOT = Path(__file__).resolve().parents[1]


def test_local_machine_is_refused_before_any_docker_call(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(RuntimeError):
        ci.runner_directory("public")


@pytest.mark.parametrize("bad", ["missing", "skip", "failure", "duplicate", "extra"])
def test_required_results_fail_closed(tmp_path, bad):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    if bad != "missing":
        case = ET.SubElement(suite, "testcase", classname="tests.test_example", name="test_one")
        if bad in ("skip", "failure"):
            ET.SubElement(case, "skipped" if bad == "skip" else "failure").text = "PRIVATE"
        if bad in ("duplicate", "extra"):
            ET.SubElement(suite, "testcase", classname="tests.test_example",
                          name="test_one" if bad == "duplicate" else "test_other")
    path = tmp_path / "raw.xml"
    ET.ElementTree(root).write(path)
    assert not ci.report(path, ["tests/test_example.py::test_one"], tmp_path / "reports", 0)
    assert "PRIVATE" not in (tmp_path / "reports" / "junit.xml").read_text()


def test_complete_report_passes_but_process_failure_does_not(tmp_path):
    path = tmp_path / "raw.xml"
    path.write_text('<testsuites><testsuite><testcase classname="tests.test_example" name="test_one"/></testsuite></testsuites>')
    nodes = ["tests/test_example.py::test_one"]
    assert ci.report(path, nodes, tmp_path / "reports", 0)
    assert not ci.report(path, nodes, tmp_path / "reports", 1)


@pytest.mark.parametrize("variable", ["PGHOST", "PGSERVICE", "DOCKER_HOST", "DOCKER_CONTEXT"])
def test_implicit_destinations_are_refused(monkeypatch, tmp_path, variable):
    for key, value in {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
                       "RUNNER_OS": "Linux", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
                       "RUNNER_TEMP": str(tmp_path), variable: "unapproved"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ci.platform, "system", lambda: "Linux")
    with pytest.raises(RuntimeError):
        ci.runner_directory("public")


@pytest.mark.parametrize("field", ["name", "image", "label"])
def test_container_ownership_must_match_before_cleanup(monkeypatch, field):
    item = {"Name": "/owned", "Config": {"Image": ci.IMAGES["public"], "Labels": {"geonaf.ci": "123-1"}}}
    if field == "name":
        item["Name"] = "/other"
    elif field == "image":
        item["Config"]["Image"] = "unapproved"
    else:
        item["Config"]["Labels"]["geonaf.ci"] = "other"
    monkeypatch.setattr(ci, "docker", lambda *args: json.dumps([item]))
    with pytest.raises(RuntimeError):
        ci.owned_container("owned", "public", "123-1")


def test_malformed_report_is_rejected_without_exposing_its_content(tmp_path):
    raw = tmp_path / "raw.xml"
    raw.write_text("PRIVATE malformed document")
    assert not ci.report(raw, ["tests/test_example.py::test_one"], tmp_path / "reports", 0)
    assert "PRIVATE" not in (tmp_path / "reports/junit.xml").read_text()


def test_workflow_and_manifest_contract():
    # JSON notation is a strict YAML 1.2 subset: no optional local YAML dependency.
    workflow = json.loads((ROOT / ".github/workflows/postgis-validation.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["validate"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] <= 30
    assert job["strategy"]["matrix"]["variant"] == ["plain", "public", "extensions"]
    for step in job["steps"]:
        if "uses" in step:
            assert len(step["uses"].split("@")[1]) == 40
    assert job["steps"][0]["with"]["persist-credentials"] is False
    assert "secrets." not in json.dumps(workflow)
    for variant in ("plain", "public", "extensions"):
        assert len(ci.IMAGES[variant].split("@sha256:")[1]) == 64
        nodes = ci.required(variant)
        assert nodes and len(nodes) == len(set(nodes))
        for node in nodes:
            filename, name = node.split("::")
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
            assert name in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "test_real_registry_import_and_parity" not in str(ci.required("public"))



def test_sql_editor_mark_scenarios_are_mandatory_in_existing_workflow():
    filename = "tests/test_supabase_mark_postgis_integration.py"
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    nodes = {filename + "::" + node.name for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")}
    assert len(nodes) >= 15
    assert nodes <= set(ci.required("extensions"))
    for variant in ("plain", "public"):
        assert filename + "::test_s0_cannot_become_test_even_with_operator_confirmations" in ci.required(variant)
        assert filename + "::test_independent_local_database_identity_is_required" in ci.required(variant)
