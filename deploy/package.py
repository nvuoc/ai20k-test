"""Create a source-only VPS bundle without Git state, credentials or runtime data."""

from __future__ import annotations

import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "deploy/parrotgo-vps.tar.gz"
FILES = (
    "Dockerfile", ".dockerignore", ".gitignore", ".gitattributes",
    ".github/workflows/package.yml",
    "src/backend/requirements.runtime.lock", "src/backend/pyproject.toml",
    "src/backend/uv.lock", "src/frontend/package.json", "src/frontend/package-lock.json",
    "src/frontend/index.html", "src/frontend/tsconfig.json", "src/frontend/vite.config.ts",
    "deploy/compose.yaml", "deploy/Caddyfile", "deploy/.env.example",
    "deploy/README.md", "deploy/setup.sh", "deploy/backup.sh", "deploy/package.py",
    "deploy/GITHUB_ACTIONS.md", "deploy/smoke_image.py",
)
TREES = ("src/backend/app", "src/frontend/src", "src/frontend/public")


def main() -> None:
    paths = [ROOT / name for name in FILES]
    for name in TREES:
        paths.extend(path for path in (ROOT / name).rglob("*") if path.is_file())
    paths = [
        path for path in paths
        if "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
        and (not path.name.startswith(".env") or path.name == ".env.example")
    ]
    missing = [str(path.relative_to(ROOT)) for path in paths if not path.is_file()]
    if missing:
        raise SystemExit("Missing deployment files: " + ", ".join(missing))
    with tarfile.open(OUTPUT, "w:gz") as bundle:
        for path in sorted(set(paths)):
            bundle.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
    # Verify the archive names without exposing any file contents.
    with tarfile.open(OUTPUT) as bundle:
        names = bundle.getnames()
        if any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise SystemExit("Unsafe archive member path")
        if any(Path(name).name.startswith(".env") and not name.endswith("/.env.example") for name in names):
            raise SystemExit("Unexpected environment file in archive")
    print(f"Created {OUTPUT} ({len(names)} files, {OUTPUT.stat().st_size} bytes).")


if __name__ == "__main__":
    main()
