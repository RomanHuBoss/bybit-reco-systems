from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", "htmlcov", "build", "dist", ".idea", ".vscode",
}
EXCLUDED_FILES = {".env", ".coverage", ".DS_Store"}
EXCLUDED_SUFFIXES = {
    ".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite-wal", ".sqlite-shm",
}


def should_exclude(relative_path: Path) -> bool:
    parts = PurePosixPath(relative_path.as_posix()).parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    name = relative_path.name
    if name in EXCLUDED_FILES or name.endswith(".egg-info"):
        return True
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def build_release(project_root: Path, output_zip: Path) -> Path:
    project_root = Path(project_root).resolve()
    output_zip = Path(output_zip).resolve()
    if not project_root.is_dir():
        raise ValueError(f"project_root is not a directory: {project_root}")
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    if output_zip.exists():
        output_zip.unlink()
    root_name = project_root.name
    with ZipFile(output_zip, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(project_root.rglob("*")):
            rel = path.relative_to(project_root)
            if should_exclude(rel):
                continue
            if path.resolve() == output_zip:
                continue
            arcname = PurePosixPath(root_name, *rel.parts).as_posix()
            if path.is_dir():
                continue
            if path.is_symlink():
                raise ValueError(f"symlink is not allowed in release: {rel}")
            archive.write(path, arcname)
    return output_zip


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a clean Bybit Recommender release ZIP")
    parser.add_argument("project_root", type=Path)
    parser.add_argument("output_zip", type=Path)
    args = parser.parse_args()
    build_release(args.project_root, args.output_zip)


if __name__ == "__main__":
    main()
