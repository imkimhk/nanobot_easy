"""Read-only POSIX process discovery for bounded gateway shutdown.

Snapshots stay in memory: command lines may contain secrets. Process groups are
not ownership boundaries; other gateways and the restart controller are excluded.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

ProcessSnapshot = dict[int, tuple[int, list[str], str]]


def process_snapshot(platform_name: str, run: Callable) -> ProcessSnapshot | None:
    """Return live PID -> (parent PID, argv, birth identity), or None on probe failure."""
    if platform_name == "Linux":
        try:
            entries = list(Path("/proc").iterdir())
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            return None
        result = {}
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] in {"Z", "X"}:
                    continue
                parent = int(fields[1])
                identity = f"linux:{boot}:{fields[19]}"
            except (OSError, ValueError, IndexError):
                continue  # Exited between probes, or belongs to another user.
            try:
                argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
            except OSError:
                argv = []  # Keep its ancestry even if argv is unavailable.
            result[int(entry.name)] = (
                parent, [arg for arg in argv if arg], identity,
            )
        return result
    try:
        response = run(["ps", "-axo", "pid=,ppid=,stat=,lstart=,command="],
                       capture_output=True, text=True, check=False, timeout=2,
                       env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    if response.returncode:
        return None
    result = {}
    for line in response.stdout.splitlines():
        fields = line.split(None, 8)
        if len(fields) != 9 or fields[2].startswith("Z"):
            continue
        try:
            pid, parent = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        try:
            argv = shlex.split(fields[8])
        except ValueError:
            # ps does not escape arbitrary argument contents. An unmatched quote
            # in a shell/Python argument must not hide that process's ancestry.
            argv = fields[8].split()
        # Preserve lstart's internal spacing to match the runtime's birth probe.
        start = line.index(fields[3], line.index(fields[2]) + len(fields[2]))
        end = line.index(fields[7], start) + len(fields[7])
        result[pid] = (parent, argv, "posix:" + line[start:end])
    return result


def is_gateway(argv: list[str]) -> bool:
    """Recognize CLI gateways, including ones with a different runtime directory."""
    for index, arg in enumerate(argv[:-1]):
        if Path(arg).name in {"nanobot", "nanobot.exe"} and argv[index + 1] == "gateway":
            return True
    return False


def owned_descendants(
    snapshot: ProcessSnapshot, root: int, caller: int, peers: set[int],
) -> list[int]:
    """Traverse owned branches, excluding peer gateways and the controller branch."""
    controller_branch = None
    current = caller
    seen = set()
    while current in snapshot and current not in seen:
        seen.add(current)
        parent = snapshot[current][0]
        if parent == root:
            controller_branch = current
            break
        current = parent
    children: dict[int, list[int]] = {}
    for pid, (parent, _, _) in snapshot.items():
        children.setdefault(parent, []).append(pid)
    result = []
    pending = list(children.get(root, []))
    seen = {root}
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid == controller_branch or pid in peers or is_gateway(snapshot[pid][1]):
            continue
        result.append(pid)
        pending.extend(children.get(pid, []))
    return result
