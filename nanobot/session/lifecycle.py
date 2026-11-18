"""Prevent work started before a session deletion from writing it back."""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


class SessionDeletedError(asyncio.CancelledError):
    """The session generation that owns this work has been deleted."""


_WRITE_SCOPE: ContextVar[tuple[Path, Callable[[], bool]] | None] = ContextVar(
    "session_write_scope", default=None
)


@contextmanager
def bind_session_writes(workspace: Path, is_current: Callable[[], bool]) -> Iterator[None]:
    token = _WRITE_SCOPE.set((workspace.resolve(), is_current))
    try:
        yield
    finally:
        _WRITE_SCOPE.reset(token)


def ensure_session_writable(workspace: Path) -> None:
    """Also fences child tasks, which inherit the originating turn's scope."""
    scope = _WRITE_SCOPE.get()
    if scope is not None and scope[0] == workspace.resolve() and not scope[1]():
        raise SessionDeletedError("Session was deleted")
