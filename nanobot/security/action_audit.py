"""Optional local action receipts, without arguments, output, or conversation text."""

import hashlib
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from nanobot.security.secrets import redact

_STATUSES = {'attempted', 'tool_ok', 'error', 'blocked', 'superseded', 'unknown',
             'approved', 'rejected', 'approval_failed'}
_SOURCES = {'agent', 'ephemeral', 'subagent', 'chat_confirmation', 'chat_command', 'webui', 'cli'}


def reference(value: str | None) -> str | None:
    return hashlib.sha256(value.encode('utf-8')).hexdigest() if value else None


class ActionAuditStore:
    def __init__(self, workspace: Path, *, enabled: bool = False, max_records: int = 10_000):
        self.workspace = workspace.resolve()
        self.path = self.workspace / '.audit' / 'actions.sqlite3'
        self.enabled = enabled
        self.max_records = max_records
        self._warned = False

    @classmethod
    def from_config(cls, workspace: Path, options=None):
        return cls(workspace, enabled=bool(getattr(options, 'enabled', False)),
                   max_records=getattr(options, 'max_records', 10_000))

    def record(self, *, kind: str, status: str, tool: str = '', source: str = 'agent',
               session_key: str | None = None, run_id: str | None = None,
               call_id: str | None = None, subject: str | None = None) -> bool:
        if not self.enabled:
            return False
        try:
            if kind not in {'tool', 'approval'} or status not in _STATUSES or source not in _SOURCES:
                raise ValueError('Invalid audit category')
            # Tool names must come from the registry, never from arbitrary model text.
            safe_tool = tool if re.fullmatch(r'[\w.:-]{1,128}', tool) and redact(tool) == tool else 'other'
            if not self.path.parent.resolve().is_relative_to(self.workspace):
                raise ValueError('Audit directory escapes workspace')
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not self.path.resolve().is_relative_to(self.path.parent.resolve()):
                raise ValueError('Audit file escapes directory')
            # A private directory also protects SQLite journal files.
            if not self.path.exists():
                try:
                    with self.path.open('xb'):
                        pass
                except FileExistsError:
                    pass  # Another session may have initialized it meanwhile.
                self.path.chmod(0o600)
            # The hook stops waiting after 200ms. This thread keeps a short lock
            # retry so a busy database still gets the receipt without holding the tool.
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    with closing(sqlite3.connect(self.path, timeout=0.2)) as conn, conn:
                        conn.execute('''CREATE TABLE IF NOT EXISTS actions (
                            seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,
                            status TEXT NOT NULL, tool TEXT NOT NULL, source TEXT NOT NULL,
                            session_ref TEXT, run_ref TEXT, call_ref TEXT, subject_ref TEXT)''')
                        conn.execute('INSERT INTO actions VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (
                            datetime.now(UTC).isoformat(), kind, status, safe_tool, source,
                            reference(session_key), reference(run_id), reference(call_id), reference(subject),
                        ))
                        conn.execute('DELETE FROM actions WHERE seq <= '
                                     '(SELECT MAX(seq) - ? FROM actions)', (self.max_records,))
                    break
                except sqlite3.OperationalError as exc:
                    locked = 'locked' in str(exc).lower() or 'busy' in str(exc).lower()
                    if not locked or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            self._warned = False
            return True
        except (OSError, sqlite3.Error, ValueError, RuntimeError, TypeError, AttributeError) as exc:
            if not self._warned:
                logger.warning('Action audit unavailable ({}); execution continues without a receipt.',
                               type(exc).__name__)
                self._warned = True
            return False

    def read(self, *, limit: int = 50, session_key: str | None = None,
             status: str | None = None) -> list[dict]:
        if not 1 <= limit <= 500:
            raise ValueError('Audit limit must be between 1 and 500')
        if status is not None and status not in _STATUSES:
            raise ValueError('Unknown audit status')
        if not self.path.exists():
            return []
        if not self.path.resolve().is_relative_to(self.workspace):
            raise ValueError('Audit file escapes workspace')
        clauses, values = [], []
        if session_key is not None:
            clauses.append('session_ref = ?')
            values.append(reference(session_key))
        if status is not None:
            clauses.append('status = ?')
            values.append(status)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        uri = self.path.resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=0.05)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT * FROM actions' + where + ' ORDER BY seq DESC LIMIT ?',
                                [*values, limit]).fetchall()
        return [dict(row) for row in reversed(rows)]


def approval_receipt(workspace: Path, options, *, status: str, source: str,
                     subject: str, session_key: str | None = None) -> None:
    ActionAuditStore.from_config(workspace, options).record(
        kind='approval', status=status, source=source, tool='skill_registration',
        subject=subject, session_key=session_key,
    )
