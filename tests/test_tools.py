import json

import pytest

from app.runner import docker_command
from app.tools import TOOLS




def test_read_file_allowlist_and_integrity(toolset):
    assert toolset.read_file("src/module.py", 0, 4)["content"] == "def "
    with pytest.raises(ValueError, match="allowlist"):
        toolset.read_file("/etc/passwd", 0, 100)
    with pytest.raises(ValueError, match="allowlist"):
        toolset.read_file("tests/generated/anything.py", 0, 100)
    (toolset.workspace / "src/module.py").write_text("touched")
    with pytest.raises(ValueError, match="changed"):
        toolset.read_file("src/module.py", 0, 100)


def test_append_only_tests_and_tool_argument_validation(toolset):
    a = toolset.write_test("def test_one():\n    assert 1 == 1\n")
    b = toolset.write_test("def test_two():\n    assert 2 == 2\n")
    assert a["path"] == "tests/generated/test_generated_001.py"
    assert b["path"] == "tests/generated/test_generated_002.py"
    with pytest.raises(ValueError):
        toolset.write_test("invalid python :")
    assert (toolset.workspace / a["path"]).read_text().startswith("def test_one")
    out = toolset.call("read_file", json.dumps({"path": "../secret.py", "offset": 0, "max_chars": 10}),
                       deadline=10**20)
    assert out["ok"] is False
    assert toolset.audit_path.read_text().count("\n") == 1
    assert "secret.py" in toolset.audit_path.read_text()


def test_run_requires_generated_tests(toolset, monkeypatch):
    with pytest.raises(ValueError, match="generate"):
        toolset.run_tests(5)
    toolset.write_test("def test_two():\n    assert 1\n")
    seen = []

    def fake_run(settings, workspace, artifacts, seconds):
        seen.append(seconds)
        return {"exit_code": 1, "timed_out": False, "cases": [], "counts": {},
                "pytest_stdout": "FAIL " * 20000, "stdout_truncated": False,
                "duration_seconds": 0.1, "runner_image": "test-image"}

    monkeypatch.setattr("app.tools.run_pytest", fake_run)
    result = toolset.call("run_tests", "{}", deadline=__import__("time").monotonic() + 8)
    assert result["ok"] is True
    assert len(result["result"]["pytest_stdout"]) == 8000
    assert len(toolset.runs[0]["pytest_stdout"]) > 8000
    assert 1 <= seen[0] <= 7


def test_docker_hardened_settings(toolset):
    cmd = docker_command(toolset.settings, toolset.workspace, toolset.artifacts, "agent-test-deadbeef")
    flags = " ".join(cmd)
    assert "--network none" in flags
    assert "--read-only" in cmd
    assert "--cap-drop ALL" in flags
    assert "no-new-privileges" in flags
    assert "65534:65534" in cmd
    assert "--pids-limit" in cmd
    assert ",readonly" in flags
    assert "--privileged" not in cmd
    assert "--user root" not in flags


def test_strict_function_schemas():
    assert {t["name"] for t in TOOLS} == {"read_file", "write_test", "run_tests"}
    for t in TOOLS:
        p = t["parameters"]
        assert t["strict"] is True
        assert p["additionalProperties"] is False
        assert sorted(p["properties"]) == sorted(p["required"])
