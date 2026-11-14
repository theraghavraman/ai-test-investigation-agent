"""Atomic, separately labeled test evidence and non-authoritative model conclusions."""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.tools import Toolset


def build_report(
    *, job_id: str, project_id: str, digest: str, goal: str, status: str,
    model: str, attempt: int, tools: Toolset | None,
    result: dict | None, error_code: str | None = None,
) -> dict:
    runs = [dict(run) for run in tools.runs] if tools else []
    return {
        "schema_version": "1.0",
        "investigation_id": job_id,
        "project_id": project_id,
        "project_manifest_sha256": digest,
        "goal": goal,
        "status": status,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "attempt": attempt,
        "generated_tests": [dict(item) for item in tools.generated] if tools else [],
        "test_evidence": {
            "runs": runs,
            "interpretation": (
                "Test outcomes are observations reported by the isolated Pytest process. "
                "A failing generated test is not independent proof of a source-code defect; "
                "a passing test cannot establish absence of defects."
            ),
        },
        "tool_audit": [dict(event) for event in tools.events] if tools else [],
        "model_analysis": (result or {}).get("analysis"),
        "budget_use": {"rounds": (result or {}).get("rounds", 0),
                       "tool_calls": (result or {}).get("tool_calls", 0)},
        "error_code": error_code,
    }


def _cell(value) -> str:
    """Escape a value for use inside a Markdown table cell."""
    return str(value or "").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_markdown(report: dict) -> str:
    lines = [
        "# AI Code Testing & Bug Investigation Report", "",
        f"**Investigation:** `{report['investigation_id']}`  ",
        f"**Project:** `{report['project_id']}`  ",
        f"**Snapshot SHA-256:** `{report['project_manifest_sha256']}`  ",
        f"**Status:** {report['status']}  ",
        f"**Model:** `{report['model']}`  ",
        f"**Recorded:** {report['recorded_at']}", "",
        "## Objective", "", report["goal"], "",
        "## Test evidence (observations; not model conclusions)", "",
        report["test_evidence"]["interpretation"], "",
    ]
    if not report["test_evidence"]["runs"]:
        lines.extend(["No Pytest run was recorded.", ""])
    for run in report["test_evidence"]["runs"]:
        lines.extend([
            f"### Run {run['run_number']}", "",
            f"- Exit code: `{run['exit_code']}`; timed out: `{run['timed_out']}`",
            f"- Duration: `{run['duration_seconds']}s`; image: `{run['runner_image']}`",
            f"- Pytest-reported counts: `{json.dumps(run['counts'])}`", "",
            "| Test | Outcome | Seconds | Reported detail |", "|---|---|---:|---|",
        ])
        for case in run["cases"]:
            lines.append(f"| `{_cell(case['name'])}` | {_cell(case['outcome'])} | {case['seconds']} | {_cell(case['details'])[:300]} |")
        lines.extend(["", "```text", run["pytest_stdout"].replace("```", "` ` `")[:30_000], "```", ""])
    lines.extend(["## Generated test manifest", ""])
    if not report["generated_tests"]:
        lines.append("No tests generated.")
    for item in report["generated_tests"]:
        lines.append(f"- `{item['path']}` — SHA-256 `{item['sha256']}`, {item['bytes']} bytes")
    lines.extend(["", "## Model analysis (hypotheses, not verified facts)", ""])
    analysis = report.get("model_analysis")
    if analysis:
        lines.append(analysis["summary"])
        for title, key in (("Suspected causes", "suspected_causes"),
                           ("Recommendations", "recommendations"), ("Limitations", "limitations")):
            lines.extend(["", f"### {title}", ""])
            items = analysis.get(key) or ["None recorded."]
            lines.extend(f"- {item}" for item in items)
    else:
        lines.append("No final model analysis available.")
    if report["error_code"]:
        lines.extend(["", f"**Operational error:** `{report['error_code']}`"])
    lines.append("")
    return "\n".join(lines)


def atomic_save(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".report_", delete=False, mode="w", encoding="utf-8") as stream:
            tmp_name = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def save_reports(root: Path, job_id: str, report: dict) -> None:
    path = root / "investigations" / job_id
    atomic_save(path / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
    atomic_save(path / "report.md", render_markdown(report))
