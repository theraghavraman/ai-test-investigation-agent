from uuid import uuid4

from app.reports import build_report, save_reports


def test_report_separates_observations_from_analysis(tmp_path):
    job_id, project_id = str(uuid4()), str(uuid4())
    report = build_report(
        job_id=job_id, project_id=project_id, digest="0" * 64,
        goal="Investigate division by zero", status="completed", model="fake",
        attempt=1, tools=None, result={"analysis": {
            "summary": "Potential defect", "suspected_causes": ["Wrong zero handling"],
            "recommendations": ["Review implementation"], "limitations": ["Not tested"],
        }},
    )
    assert report["test_evidence"]["runs"] == []
    assert "Potential defect" not in str(report["test_evidence"])
    assert "Potential defect" in str(report["model_analysis"])
    save_reports(tmp_path, job_id, report)
    content = (tmp_path / "investigations" / job_id / "report.md").read_text()
    assert "Model analysis (hypotheses, not verified facts)" in content
    assert "No Pytest run was recorded." in content
    assert "Test evidence" in content
