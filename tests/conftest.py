"""Fully isolated unit-test fixtures."""
import os
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import pytest

from app.settings import Settings
from app.storage import create_attempt_workspace, extract_approved
from app.tools import Toolset

os.environ.setdefault("API_KEY", "local_unit_test_token_which_is_longer_than_32")


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,  # never let a developer's local .env change unit-test behaviour
        api_key="unit_test_api_key_value_at_least_32_chars",
        openai_api_key="unit_test_fake_openai_key",
        data_dir=tmp_path,
        database_url="sqlite:///:memory:",
    )


def archive_for(files):
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return data.getvalue()


@pytest.fixture
def toolset(settings):
    project_id = str(uuid4())
    manifest, _ = extract_approved(
        archive_for({"src/module.py": "def answer(): return 42\n"}),
        ["src/module.py"], settings.data_dir / "projects" / project_id, settings,
    )
    workspace, artifacts, root = create_attempt_workspace(
        settings.data_dir, project_id, str(uuid4()), 1, manifest
    )
    return Toolset(workspace, artifacts, root / "audit.jsonl", manifest, settings)
