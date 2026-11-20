"""Observe declared output files around a completed command, without parsing its output."""

from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG

from nanobot.agent.context_evidence import fingerprint
from nanobot.agent.tools.path_utils import resolve_workspace_path


@dataclass(frozen=True)
class ArtifactSnapshot:
    original: str
    path: Path
    stamp: tuple | None


def _stamp(path: Path) -> tuple | None:
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    if not S_ISREG(value.st_mode):
        raise ValueError("Only regular output files can supply write evidence")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def snapshot_writes(targets, workspace: Path | None, allowed_root: Path | None) -> list[ArtifactSnapshot]:
    """Stat only explicit write targets; never scan directories or acquire private paths."""
    snapshots = {}
    for target in targets:
        if target.action != "write":
            continue
        try:
            path = resolve_workspace_path(
                target.path, workspace, allowed_root, include_media_dir=False,
            )
            snapshots[path] = ArtifactSnapshot(target.path, path, _stamp(path))
        except (OSError, ValueError, RuntimeError):
            continue  # Evidence collection must not change command permissions or execution.
    return list(snapshots.values())


def changed_writes(
    snapshots: list[ArtifactSnapshot], workspace: Path | None, allowed_root: Path | None,
) -> list[dict]:
    """Hash only created/changed regular files after a successful foreground command."""
    receipts = []
    for before in snapshots:
        try:
            path = resolve_workspace_path(
                before.original, workspace, allowed_root, include_media_dir=False,
            )
            if path != before.path:
                continue  # A retargeted symlink cannot borrow the earlier path's permission.
            after = _stamp(path)
            if after is None or after == before.stamp:
                continue
            version = fingerprint(path)
            if version and version.get("sha256") and _stamp(path) == after:
                receipts.append({"target": str(path), "sha256": version["sha256"]})
        except (OSError, ValueError, RuntimeError):
            continue
    return receipts
