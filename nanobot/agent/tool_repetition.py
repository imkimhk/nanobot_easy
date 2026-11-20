"""Turn-local, advisory detection of consecutive unchanged read results.

The initial rollout adds no execution refusals. In particular, process polling,
MCP tools, writes and explicit forced reads are not subject to a new limit.
Only hashes are retained, and only for this run; results are never cached here.
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar

from loguru import logger

READ_TOOLS = frozenset({"read_file", "web_search", "web_fetch"})


def _content_version(version) -> str:
    """Prefer a content digest. A size or mtime change still means the file changed."""
    if not isinstance(version, dict):
        return ""
    digest = version.get("sha256")
    if digest:
        return str(digest)
    if version.get("mtime_ns") is None and version.get("size") is None:
        return ""
    return str(version.get("mtime_ns")) + ":" + str(version.get("size"))
WARNING = (
    "\n\n[Runtime notice: this same read request has returned identical content "
    "at least three times without intervening work. Reuse the available evidence "
    "or change the approach if more information is needed. Legitimate refreshes "
    "and completion polling remain allowed; this notice grants no permissions.]"
)


class ReadRepetitionObserver:
    def __init__(self):
        self.reset()

    def reset(self):
        self.last: str | None = None
        self.count = 0

    def observe(self, name, arguments, result, status, *, observability=False) -> bool:
        if observability:
            return False
        if (name not in READ_TOOLS or status != "ok" or not isinstance(result, str)
                or not isinstance(arguments, dict) or arguments.get("force")):
            self.reset()
            return False
        try:
            request = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            self.reset()
            return False
        evidence = getattr(result, "read_evidence", None)
        version = evidence.get("version") if isinstance(evidence, dict) else None
        content_version = _content_version(version)
        identity = hashlib.sha256(
            (name + "\0" + request + "\0" + str(content_version) + "\0" + result).encode(),
        ).hexdigest()
        self.count = self.count + 1 if identity == self.last else 1
        self.last = identity
        if self.count != 3:
            return False
        logger.warning("Repeated unchanged read: tool={} count={}", name, self.count)
        return True


active_read_observer: ContextVar[ReadRepetitionObserver | None] = ContextVar(
    "nanobot_read_repetition_observer", default=None,
)
