"""Create a safe demo project archive containing only explicitly approved files."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
archive = root / "examples" / "calculator_project.zip"
with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zf:
    for name in ("calculator.py", "spec.md"):
        zf.write(root / "examples" / name, arcname=name)
print(archive)
