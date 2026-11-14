import io
import stat
from uuid import uuid4
from zipfile import ZipFile, ZipInfo

import pytest

from app.storage import ArchiveError, create_attempt_workspace, extract_approved, valid_path


def archive_for(files: dict[str, str]) -> bytes:
    stream = io.BytesIO()
    with ZipFile(stream, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return stream.getvalue()


def test_snapshot_only_explicit_allowlist(settings):
    p = str(uuid4())
    source = {"lib/math.py": "def f(x): return x+1\n", "requirements.md": "# Spec"}
    manifest, project_sha = extract_approved(
        archive_for(source), list(source), settings.data_dir / "projects" / p, settings
    )
    assert len(manifest) == 2 and len(project_sha) == 64
    assert set(item["path"] for item in manifest) == set(source)
    assert (settings.data_dir / "projects" / p / "files/lib/math.py").read_text() == source["lib/math.py"]
    workspace, artifacts, attempt = create_attempt_workspace(settings.data_dir, p, str(uuid4()), 1, manifest)
    assert (workspace / "lib/math.py").read_text() == source["lib/math.py"]
    assert (workspace / "tests/generated").is_dir()
    assert artifacts.is_dir() and attempt.is_dir()


@pytest.mark.parametrize("path", [
    "../secret.py", "src/../../secret.py", "/etc/passwd", "c:\\secrets.py",
    "./safe.py", "a//b.py", "a/./b.py", "a/.git/secret.py", "setup.sh", "tests/generated/evil.py",
])
def test_traversal_hidden_and_reserved_paths_rejected(path):
    with pytest.raises(ArchiveError):
        valid_path(path)


def test_unapproved_file_rejected(settings):
    archive = archive_for({"file.py": "x=1\n", "other.py": "y=2\n"})
    with pytest.raises(ArchiveError, match="match approved paths"):
        extract_approved(archive, ["file.py"], settings.data_dir / "projects" / str(uuid4()), settings)


def test_credential_file_in_archive_rejected(settings):
    archive = archive_for({"file.py": "x=1\n", "credentials.json": "{}"})
    with pytest.raises(ArchiveError, match="secret or credential files"):
        extract_approved(archive, ["file.py"], settings.data_dir / "projects" / str(uuid4()), settings)


def test_archive_symlink_rejected(settings):
    stream = io.BytesIO()
    with ZipFile(stream, "w") as zf:
        info = ZipInfo("f.py")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ArchiveError, match="non-regular"):
        extract_approved(stream.getvalue(), ["f.py"], settings.data_dir / "projects" / str(uuid4()), settings)


def test_hash_detects_tampered_snapshot(settings):
    p = str(uuid4())
    manifest, _ = extract_approved(archive_for({"x.py": "x=1\n"}), ["x.py"],
                                   settings.data_dir / "projects" / p, settings)
    (settings.data_dir / "projects" / p / "files/x.py").write_text("x=2\n")
    with pytest.raises(ArchiveError, match="integrity"):
        create_attempt_workspace(settings.data_dir, p, str(uuid4()), 1, manifest)


def test_limits_and_duplicates(settings):
    with pytest.raises(ArchiveError, match="duplicate"):
        extract_approved(archive_for({"a.py": "x"}), ["a.py", "a.py"],
                         settings.data_dir / "projects" / str(uuid4()), settings)
    with pytest.raises(ArchiveError, match="file size"):
        extract_approved(archive_for({"a.py": "x" * (settings.max_file_bytes + 1)}), ["a.py"],
                         settings.data_dir / "projects" / str(uuid4()), settings)


def test_zip_member_count_bounded(settings):
    stream = io.BytesIO()
    with ZipFile(stream, "w") as zf:
        zf.writestr("a.py", "x=1")
        for i in range(settings.max_approved_files * 3):
            zf.writestr(f"folders{i}/", "")
    with pytest.raises(ArchiveError, match="too many ZIP"):
        extract_approved(stream.getvalue(), ["a.py"],
                         settings.data_dir / "projects" / str(uuid4()), settings)


def test_secret_filenames_and_redaction():
    from app.storage import redact_secrets
    for name in ("secrets.json", "credentials.json", "sub/secrets.yaml"):
        with pytest.raises(ArchiveError, match="secret"):
            valid_path(name)
    scrubbed = redact_secrets("Bearer sk-1234567890abcdefghij and api_key=mysecretvalue123")
    assert "sk-1234567890" not in scrubbed
    assert "[REDACTED_SECRET]" in scrubbed
