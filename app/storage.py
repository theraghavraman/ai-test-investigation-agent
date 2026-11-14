"""Immutable approved-file snapshots with path, type, size and digest verification."""
import hashlib
import io
import json
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from app.settings import Settings

ALLOWED_SUFFIXES = {".py", ".md", ".txt", ".yaml", ".yml", ".json", ".toml"}
RESERVED = {".git", ".venv", "__pycache__", ".pytest_cache", "site-packages"}
FORBIDDEN_NAMES = {"credentials.json", "secrets.json", "secrets.yaml", "secrets.yml", "secrets.toml", "id_rsa", "id_ed25519"}
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:bearer\s+)[A-Za-z0-9._~+/-]{12,}"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*['\"]?[^\s,'\";]{8,}"),
)


def redact_secrets(text: str | None) -> str:
    if not text:
        return ""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED_SECRET]", out)
    return out


class ArchiveError(ValueError):
    pass


def valid_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ArchiveError("invalid approved path")
    if any(ord(c) < 32 for c in value) or "\\" in value or ":" in value:
        raise ArchiveError("paths must be clean POSIX relative paths")
    path = PurePosixPath(value)
    if value.startswith("/") or any(x in {"", ".", ".."} for x in value.split("/")):
        raise ArchiveError("absolute, non-canonical, or traversal paths are forbidden")
    if any(part in RESERVED or part.startswith(".") for part in path.parts):
        raise ArchiveError("hidden or reserved directories are forbidden")
    if len(path.parts) > 12 or path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ArchiveError("unapproved file type or directory depth")
    if path.name.lower() in FORBIDDEN_NAMES:
        raise ArchiveError("secret or credential files cannot be approved")
    if path.parts[:2] == ("tests", "generated"):
        raise ArchiveError("tests/generated is agent-owned")
    return value


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_approved(archive: bytes, approved: list[str], project_dir: Path, settings: Settings) -> tuple[list, str]:
    if len(archive) > settings.max_archive_bytes:
        raise ArchiveError("archive too large")
    if not isinstance(approved, list) or not 1 <= len(approved) <= settings.max_approved_files:
        raise ArchiveError("approved_paths must be a nonempty bounded array")
    normalized = [valid_path(p) for p in approved]
    if len(set(normalized)) != len(normalized) or len({p.casefold() for p in normalized}) != len(normalized):
        raise ArchiveError("duplicate approved paths")
    if any(p == "tests/generated" for p in normalized):
        raise ArchiveError("reserved path")

    try:
        zf = ZipFile(io.BytesIO(archive))
    except BadZipFile as e:
        raise ArchiveError("invalid ZIP archive") from e

    staging = project_dir.parent / f".{project_dir.name}.staging"
    manifest = []
    total = 0
    if staging.exists():
        raise ArchiveError("staging directory collision")
    try:
        all_entries = zf.infolist()
        if len(all_entries) > settings.max_approved_files * 3:
            raise ArchiveError("too many ZIP archive members")
        entries = [i for i in all_entries if not i.is_dir()]
        names = [valid_path(i.filename) for i in entries]
        if sorted(names) != sorted(normalized):
            raise ArchiveError("archive entries must match approved paths exactly")
        if len(set(names)) != len(names):
            raise ArchiveError("duplicate archive entries")
        for info in entries:
            kind = (info.external_attr >> 16) & 0o170000
            if kind not in (0, stat.S_IFREG):
                raise ArchiveError("non-regular archive entry rejected")
            if info.flag_bits & 1:
                raise ArchiveError("encrypted ZIPs are forbidden")
            if info.file_size > settings.max_file_bytes or info.file_size < 0:
                raise ArchiveError("file size limit exceeded")
            total += info.file_size
            if total > settings.max_extracted_bytes:
                raise ArchiveError("total extracted size limit exceeded")
            if info.file_size > 1_000 and info.compress_size and info.file_size / info.compress_size > 200:
                raise ArchiveError("suspicious ZIP compression ratio")

        (staging / "files").mkdir(parents=True, mode=0o700)
        for info in entries:
            # Each individual member is bounded before decompression and after read.
            with zf.open(info) as member:
                data = member.read(settings.max_file_bytes + 1)
            if len(data) != info.file_size or len(data) > settings.max_file_bytes:
                raise ArchiveError("invalid member length")
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as e:
                raise ArchiveError("approved files must be UTF-8 text") from e
            dest = staging / "files" / info.filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("xb") as f:
                f.write(data)
            dest.chmod(0o644)
            manifest.append({"path": info.filename, "sha256": digest(data), "bytes": len(data)})
        manifest.sort(key=lambda x: x["path"])
        project_digest = digest(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
        project_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, project_dir)
        project_dir.chmod(0o700)
        return manifest, project_digest
    except (BadZipFile, EOFError, OSError) as e:
        raise ArchiveError("malformed ZIP member or filesystem write failed") from e
    finally:
        zf.close()
        if staging.exists():
            shutil.rmtree(staging)


def create_attempt_workspace(data_dir: Path, project_id: str, job_id: str, attempt: int, manifest: list) -> tuple[Path, Path, Path]:
    # IDs originate from UUIDs generated by the service, never model output.
    UUID(project_id)
    UUID(job_id)
    if attempt < 1:
        raise ValueError("invalid attempt")
    project_root = data_dir / "projects" / project_id / "files"
    attempt_root = data_dir / "investigations" / job_id / f"attempt_{attempt}"
    workspace = attempt_root / "workspace"
    artifacts = attempt_root / "artifacts"
    workspace.mkdir(parents=True, exist_ok=False)
    attempt_root.parent.chmod(0o700)
    attempt_root.chmod(0o700)
    artifacts.mkdir(mode=0o700)
    for entry in manifest:
        rel = valid_path(entry["path"])
        src = project_root / rel
        if src.is_symlink() or not src.is_file() or digest(src.read_bytes()) != entry["sha256"]:
            raise ArchiveError("approved snapshot failed integrity verification")
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        dest.chmod(0o644)
    (workspace / "tests" / "generated").mkdir(parents=True, exist_ok=False)
    # Only the isolated runner is allowed to write to this folder, not to workspace.
    artifacts.chmod(0o777)  # parent attempt_root remains private on the host
    return workspace, artifacts, attempt_root
