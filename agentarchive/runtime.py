"""One runtime file inventory for installation, portable export, and SSD seeding."""

from __future__ import annotations

import pathlib
import shutil


def viewer_root(source_root: pathlib.Path) -> pathlib.Path:
    """Find packaged assets or an older installed runtime's viewer."""
    packaged = source_root / "agentarchive" / "web"
    return packaged if (packaged / "index.html").is_file() else source_root / "viewer" / "dist"

def copy_runtime(source_root: pathlib.Path, destination: pathlib.Path) -> None:
    """Copy the complete query runtime without caches or machine config."""
    entrypoint = source_root / "agent_archive.py"
    if not entrypoint.is_file():
        entrypoint = source_root / "agent-archive"
    files = [(entrypoint, pathlib.Path("agent-archive"))]
    files.extend(
        (source_root / name, pathlib.Path(name))
        for name in (
            "reasoning_index.py", "privacy_filter.py", "transcript_model.py",
            "transcript_viewer.py", "transcript_media.py", "transcript_usage.py",
        )
    )
    package = source_root / "agentarchive"
    if not (package / "__init__.py").is_file():
        raise FileNotFoundError(f"AgentArchive runtime package is missing: {package}")
    files.extend((path, path.relative_to(source_root)) for path in sorted(package.glob("*.py")))
    viewer = viewer_root(source_root)
    if not (viewer / "index.html").is_file():
        raise FileNotFoundError("Build the viewer before packaging: cd viewer && npm ci && npm run build")
    files.extend((path, pathlib.Path("agentarchive/web") / path.relative_to(viewer)) for path in sorted(viewer.rglob("*"))
                 if path.is_file() and not path.is_symlink())
    for source, relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        target.chmod(0o700 if relative.name == "agent-archive" else 0o600)
