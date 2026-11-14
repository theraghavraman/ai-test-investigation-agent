"""Runs model-authored tests ONLY in a hardened, network-isolated Docker container.

The Docker daemon is a privileged trust boundary: run the worker on a dedicated
runner host with rootless Docker. Never expose the Docker socket to the API.
"""
import secrets
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from app.settings import Settings
from app.storage import redact_secrets


class RunnerError(RuntimeError):
    pass


def docker_command(
    settings: Settings, workspace: Path, artifacts: Path, container_name: str
) -> list[str]:
    for path in (workspace, artifacts):
        if any(c in str(path) for c in (",", "\n", "\r")):
            raise RunnerError("Docker mount paths cannot contain commas or control characters")
        if not path.is_absolute() or not path.is_dir() or path.is_symlink():
            raise RunnerError("Docker mount source must be an existing absolute directory")
    if not container_name.startswith("agent-test-"):
        raise RunnerError("invalid container name")
    # No shell, privileged mode, host networking, arbitrary mounts, or env secrets.
    return [
        settings.docker_binary, "run", "--rm", "--pull", "never", "--name", container_name,
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "65534:65534",
        "--pids-limit", str(settings.docker_pids_limit),
        "--memory", f"{settings.docker_memory_mb}m",
        "--memory-swap", f"{settings.docker_memory_mb}m",
        "--cpus", str(settings.docker_cpus),
        "--ulimit", "nofile=128:128", "--ulimit", "fsize=10485760:10485760",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
        "--shm-size", "64m", "--log-driver", "none",
        "--workdir", "/workspace",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "--env", "HOME=/tmp",
        "--mount", f"type=bind,src={workspace},dst=/workspace,readonly",
        "--mount", f"type=bind,src={artifacts},dst=/artifacts",
        settings.runner_image,
        "python", "-I", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "-c", "/opt/pytest.ini", "--confcutdir=/workspace/tests/generated",
        "--import-mode=importlib", "--tb=short", "--disable-warnings",
        "--maxfail=8", "-o", "pythonpath=/workspace",
        "--junitxml=/artifacts/junit.xml", "/workspace/tests/generated",
    ]


def parse_junit(artifacts: Path, max_bytes: int = 512_000) -> list[dict]:
    junit = artifacts / "junit.xml"
    try:
        s = junit.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_size > max_bytes or s.st_size <= 0:
            return []
        tree = ElementTree.parse(junit)
    except (OSError, ValueError, ElementTree.ParseError, DefusedXmlException):
        return []
    cases = []
    for case in tree.iter("testcase"):
        if len(cases) >= 200:
            break
        outcome = "passed"
        details = None
        for candidate in ("error", "failure", "skipped"):
            node = case.find(candidate)
            if node is not None:
                outcome = "failed" if candidate == "failure" else candidate
                details = redact_secrets((node.get("message") or node.text or "")[:1_000])
                break
        try:
            seconds = max(0.0, float(case.get("time", "0")))
        except ValueError:
            seconds = 0.0
        cases.append({
            "name": (case.get("classname", "") + "::" + case.get("name", ""))[:300],
            "outcome": outcome,
            "seconds": seconds,
            "details": details,
        })
    return cases


def _stop_container(settings: Settings, name: str) -> None:
    try:
        subprocess.run(
            [settings.docker_binary, "rm", "-f", name],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_pytest(settings: Settings, workspace: Path, artifacts: Path, seconds: int) -> dict:
    """Stream+bound captured output. Container is forcibly removed after deadline."""
    if seconds < 1:
        raise RunnerError("test time budget exhausted")
    container_name = "agent-test-" + secrets.token_hex(10)
    argv = docker_command(settings, workspace, artifacts, container_name)
    out = bytearray()
    truncated = False

    def collect(stream) -> None:
        nonlocal truncated
        while True:
            block = stream.read(4096)
            if not block:
                return
            room = max(0, settings.docker_log_bytes - len(out))
            out.extend(block[:room])
            if len(block) > room:
                truncated = True

    start = time.monotonic()
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0,
        )
    except OSError as e:
        raise RunnerError("Docker CLI unavailable; ensure the isolated runner host is configured") from e

    assert process.stdout is not None
    reader = threading.Thread(target=collect, args=(process.stdout,), daemon=True)
    reader.start()
    timed_out = False
    try:
        try:
            return_code = process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_container(settings, container_name)
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            return_code = 124
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        reader.join(timeout=2)
        process.stdout.close()
        # Docker --rm is insufficient if the CLI was killed or daemon lost contact.
        _stop_container(settings, container_name)
    duration = round(time.monotonic() - start, 3)
    cases = parse_junit(artifacts)
    if return_code in (125, 126, 127):
        raise RunnerError("Docker failed to start or invoke the approved Pytest image")
    counts = {name: sum(c["outcome"] == name for c in cases) for name in ("passed", "failed", "error", "skipped")}
    warnings: list[str] = []
    bad = counts["failed"] + counts["error"]
    if return_code == 0 and bad > 0:
        warnings.append("pytest exit code 0 but JUnit reports failures/errors")
    elif return_code == 1 and bad == 0 and cases:
        warnings.append("pytest exit code 1 but JUnit reports no failures/errors")
    elif return_code in (0, 1) and not cases:
        warnings.append("pytest exited without producing JUnit XML test cases")
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "exit_code": return_code, "timed_out": timed_out, "duration_seconds": duration,
        "junit_available": bool(cases), "possibly_oom_killed": return_code == 137,
        "pytest_stdout": redact_secrets(out.decode("utf-8", errors="replace")),
        "stdout_truncated": truncated, "cases": cases, "counts": counts, "consistency_warnings": warnings,
        "evidence_source": "Docker pytest process + process-reported JUnit XML; not independent proof of a bug",
        "runner_image": settings.runner_image,
    }
