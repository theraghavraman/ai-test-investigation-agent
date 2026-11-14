"""Only three model-facing tools. All paths, quotas and execution are host-enforced."""
import ast
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.runner import run_pytest
from app.settings import Settings

TOOLS = [
    {
        "type": "function", "name": "read_file", "strict": True,
        "description": "Read a UTF-8 chunk of an approved source or specification file. The path must exactly match the allowlist.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "Zero-based character offset"},
            "max_chars": {"type": "integer", "description": "1 to 12000"},
        }, "required": ["path", "offset", "max_chars"], "additionalProperties": False},
    },
    {
        "type": "function", "name": "write_test", "strict": True,
        "description": "Append a NEW pytest file. Existing generated tests cannot be replaced or removed. Supply full Python pytest code.",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"},
        }, "required": ["code"], "additionalProperties": False},
    },
    {
        "type": "function", "name": "run_tests", "strict": True,
        "description": "Run all generated tests in Docker with no network, bounded CPU/memory/PIDs and wall time. Requires at least one test.",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
]


class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadArgs(StrictArgs):
    path: str
    offset: int = Field(ge=0, le=1_000_000)
    max_chars: int = Field(ge=1, le=12_000)


class WriteArgs(StrictArgs):
    code: str = Field(min_length=1)


class RunArgs(StrictArgs):
    pass


class Toolset:
    def __init__(self, workspace: Path, artifacts: Path, audit_path: Path, manifest: list, settings: Settings):
        self.workspace = workspace
        self.artifacts = artifacts
        self.audit_path = audit_path
        self.approved = {item["path"]: item["sha256"] for item in manifest}
        self.settings = settings
        self.generated: list[dict] = []
        self.runs: list[dict] = []
        self.events: list[dict] = []

    def _audit(self, action: str, ok: bool, metadata: dict) -> None:
        event = {"at": datetime.now(timezone.utc).isoformat(), "action": action, "ok": ok, **metadata}
        line = (json.dumps(event, ensure_ascii=False) + "\n").encode()
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("audit log append failed")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        self.events.append(event)

    def read_file(self, path: str, offset: int, max_chars: int) -> dict:
        if path not in self.approved:
            raise ValueError("path is not in the explicitly approved allowlist")
        file = self.workspace / path
        if file.is_symlink() or not file.is_file():
            raise ValueError("approved file is not a regular file")
        content = file.read_bytes()
        if hashlib.sha256(content).hexdigest() != self.approved[path]:
            raise ValueError("approved file changed after project registration")
        decoded = content.decode("utf-8")
        return {
            "path": path, "sha256": self.approved[path],
            "offset": offset, "content": decoded[offset:offset + max_chars],
            "has_more": offset + max_chars < len(decoded), "total_characters": len(decoded),
        }

    def write_test(self, code: str) -> dict:
        encoded = code.encode("utf-8")
        if len(self.generated) >= self.settings.max_generated_tests:
            raise ValueError("generated test quota exhausted")
        if len(encoded) > self.settings.max_test_code_bytes:
            raise ValueError("test code size limit exceeded")
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            raise ValueError(f"test is not valid Python: line {e.lineno}") from e
        if not any(
            (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_"))
            or (isinstance(n, ast.ClassDef) and n.name.startswith("Test"))
            for n in tree.body
        ):
            raise ValueError("test file must define at least one test_ function or Test class")
        filename = f"test_generated_{len(self.generated) + 1:03d}.py"
        # Atomic exclusive creation; no model-controlled filename or overwrite possible.
        file = self.workspace / "tests" / "generated" / filename
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            file.unlink(missing_ok=True)
            raise
        item = {"path": f"tests/generated/{filename}", "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}
        self.generated.append(item)
        return item

    def run_tests(self, seconds: int) -> dict:
        if not self.generated:
            raise ValueError("generate at least one test before running")
        if len(self.runs) >= self.settings.max_test_runs:
            raise ValueError("test execution quota exhausted")
        # Artifacts are intentionally untrusted: clear prior JUnit output for every run.
        (self.artifacts / "junit.xml").unlink(missing_ok=True)
        result = run_pytest(self.settings, self.workspace, self.artifacts, seconds)
        result["run_number"] = len(self.runs) + 1
        result["test_manifest"] = [dict(t) for t in self.generated]
        self.runs.append(result)
        return result

    def call(self, name: str, arguments: str, deadline: float) -> dict:
        start = time.monotonic()
        info: dict[str, Any] = {"tool_name": name}
        try:
            if time.monotonic() >= deadline:
                raise ValueError("investigation deadline exhausted")
            try:
                payload = json.loads(arguments)
            except json.JSONDecodeError as e:
                raise ValueError("invalid tool arguments JSON") from e
            if name == "read_file":
                args = ReadArgs.model_validate(payload)
                info["path"] = args.path[:240]
                result = self.read_file(args.path, args.offset, args.max_chars)
            elif name == "write_test":
                args = WriteArgs.model_validate(payload)
                result = self.write_test(args.code)
                info["test_sha256"] = result["sha256"]
            elif name == "run_tests":
                RunArgs.model_validate(payload)
                available = deadline - time.monotonic() - 1
                timeout = int(min(self.settings.test_timeout_seconds, available))
                if timeout < 1:
                    raise ValueError("insufficient time remaining for Docker execution")
                evidence = self.run_tests(timeout)
                info["exit_code"] = evidence["exit_code"]
                info["timed_out"] = evidence["timed_out"]
                # Keep complete bounded logs in evidence reports, but return a
                # compact summary to the model to limit cost/context growth.
                result = {**evidence, "pytest_stdout": evidence["pytest_stdout"][:8000],
                          "cases": evidence["cases"][:50]}
            else:
                raise ValueError("unknown function")
            ok = True
            output = {"ok": True, "result": result}
        except (ValueError, ValidationError, OSError) as e:
            ok = False
            # Deliberately avoid echoing raw test code or tool arguments in errors/audit.
            output = {"ok": False, "error": str(e)[:400]}
        info["duration_seconds"] = round(time.monotonic() - start, 3)
        self._audit(name, ok, info)
        return output
