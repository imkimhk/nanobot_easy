"""Visibility helpers for persisted session history messages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nanobot.session.automation_turns import is_automation_history_message

HIDDEN_HISTORY_META = "_hidden_history"


def _has_hidden_history_marker(message: Mapping[str, Any] | None) -> bool:
    if not message:
        return False
    marker = message.get(HIDDEN_HISTORY_META)
    return marker is True or isinstance(marker, Mapping)


def is_hidden_history_message(message: Mapping[str, Any] | None) -> bool:
    """True for persisted messages that should not be shown as chat turns."""
    return _has_hidden_history_marker(message) or is_automation_history_message(message)


def is_task_control_message(message: Mapping[str, Any]) -> bool:
    """Identify obsolete per-request guidance without hiding literal user text."""
    return (
        message.get("role") == "user"
        and _has_hidden_history_marker(message)
        and str(message.get("content", "")).startswith("[Task evidence control;")
    )
