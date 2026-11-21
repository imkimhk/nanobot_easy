"""Opt-in, bounded background refresh of the prompt copy, never the memory source."""

import asyncio
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from contextvars import Context
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock
from loguru import logger

from nanobot.agent.memory_prompt import _MAX_SNAPSHOT_BYTES, SNAPSHOT_NAME, make_snapshot
from nanobot.providers.base import LLMProvider, hosted_tools_disabled
from nanobot.utils.prompt_templates import render_template

_MAX_SOURCE_BYTES = 128_000
_REFRESH_TIMEOUT_S = 120
_RETRY_DELAY_S = 300
_DEBOUNCE_S = 1


class MemoryPromptRefresher:
    """One job per workspace; duplicate calls never wait for it or start more work.

    The caller keeps using current raw memory until a current, in-budget snapshot
    passes a separate review. No user/session context, tools, or memory mutations
    are available to either model call. Failed attempts have a five-minute cooldown.
    """

    def __init__(self, source: Path, budget: int,
                 selection: Callable[[], tuple[LLMProvider, str]]):
        self.source = source
        self.budget = budget
        self.selection = selection
        self.workspace = source.parent.parent.resolve()
        self.snapshot = source.with_name(SNAPSHOT_NAME)
        self.lock_path = source.with_name(".prompt_snapshot.lock")
        self._task: asyncio.Task | None = None
        self._closed = False
        self._last_digest: str | None = None
        self._retry_at = 0.0

    def request(self, text: str) -> None:
        if self._closed or self.budget <= 0 or not text.strip():
            return
        if self._task is not None and not self._task.done():
            return
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest == self._last_digest and time.monotonic() < self._retry_at:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Offline prompt inspection has no background lifecycle.
        self._last_digest = digest
        self._retry_at = time.monotonic() + _RETRY_DELAY_S
        # Do not inherit a chat's cancellation, permission, provider budget or
        # file-state ContextVars into a separate maintenance operation.
        self._task = loop.create_task(self._refresh(digest), context=Context())

    def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()

    async def close(self) -> None:
        self.stop()
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task

    def _check_paths(self) -> None:
        for path in (self.source, self.snapshot, self.lock_path):
            if path.is_symlink() or not path.resolve().is_relative_to(self.workspace):
                raise ValueError("Memory projection paths must stay inside the workspace")
        if not self.source.is_file():
            raise ValueError("Memory source is not a regular file")

    def _read_source(self) -> str:
        self._check_paths()
        with self.source.open(encoding="utf-8") as stream:
            text = stream.read(_MAX_SOURCE_BYTES + 1)
        if not text.strip() or len(text.encode("utf-8")) > _MAX_SOURCE_BYTES:
            raise ValueError("Memory source is empty or too large for automatic refresh")
        return text

    def _read_snapshot(self) -> bytes | None:
        try:
            if not self.snapshot.is_file():
                if self.snapshot.exists():
                    raise ValueError("Snapshot is not a regular file")
                return None
            with self.snapshot.open("rb") as stream:
                raw = stream.read(_MAX_SNAPSHOT_BYTES + 1)
            if len(raw) > _MAX_SNAPSHOT_BYTES:
                raise ValueError("Snapshot is too large")
            return raw
        except FileNotFoundError:
            return None

    async def _ask(self, provider: LLMProvider, model: str, template: str,
                   payload: dict, max_tokens: int) -> str:
        with hosted_tools_disabled():
            response = await provider.chat_with_retry(
                model=model, messages=[
                    {"role": "system", "content": render_template(template, strip=True)},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ], tools=None, tool_choice=None, temperature=0, max_tokens=max_tokens,
                max_retries=0,
            )
        if response.finish_reason != "stop" or response.tool_calls or not response.content:
            raise ValueError("Refresh requires complete text with no tool calls")
        return response.content.strip()

    async def _refresh(self, digest: str) -> None:
        phase = "path checks"
        try:
            await asyncio.sleep(_DEBOUNCE_S)
            self._check_paths()
            # Nonblocking, cross-process locking also prevents duplicate model
            # spend when two gateways intentionally share a workspace.
            with FileLock(self.lock_path, timeout=0, mode=0o600, thread_local=False):
                async with asyncio.timeout(_REFRESH_TIMEOUT_S):
                    text = self._read_source()
                    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
                        return  # A newer source will be detected on the next build.
                    before = self._read_snapshot()
                    if before is not None:
                        try:
                            data = json.loads(before)
                            if isinstance(data, dict) and isinstance(data.get("summary"), str):
                                current = make_snapshot(text, data["summary"], self.source, self.budget)
                                if data.get("version") == 1 and data.get("source_sha256") == current["source_sha256"]:
                                    return  # Another process already refreshed it.
                        except (ValueError, TypeError, KeyError, RecursionError):
                            pass
                    method, model = "lossless", None
                    try:
                        candidate = make_snapshot(text, text, self.source, self.budget)
                    except ValueError:
                        provider, model = self.selection()
                        phase = "generation"
                        summary = await self._ask(provider, model, "agent/memory_prompt_refresh.md",
                            {"source": text, "summary_token_target": max(1, int(self.budget * 0.8))},
                            min(16_000, max(2048, self.budget * 2)))
                        candidate = make_snapshot(text, summary, self.source, self.budget)
                        phase = "review"
                        review = json.loads(await self._ask(provider, model,
                            "agent/memory_prompt_review.md", {"source": text, "summary": summary}, 2048))
                        if (not isinstance(review, dict)
                                or any(review.get(key) is not True for key in
                                       ("constraints_preserved", "faithful", "no_new_authority"))
                                or review.get("issues") != []):
                            raise ValueError("Candidate did not pass review")
                        method = "model-reviewed"
                    phase = "publication"
                    candidate["refresh"] = {"method": method, "model": model,
                        "created_at": datetime.now(timezone.utc).isoformat()}
                    raw = (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                    if len(raw) > _MAX_SNAPSHOT_BYTES:
                        raise ValueError("Candidate file is too large")
                    self._publish(text, before, raw)
                    logger.info("Memory prompt refreshed: method={} source_sha256={}", method, digest)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Provider/parser errors may contain memory text or credentials.
            logger.warning("Memory prompt refresh deferred: phase={} error={}; "
                           "keeping current raw memory; retry cooldown={}s",
                           phase, type(exc).__name__, _RETRY_DELAY_S)

    def _publish(self, text: str, before: bytes | None, raw: bytes) -> None:
        fd, name = tempfile.mkstemp(prefix=".memory-prompt-", dir=self.source.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # A manual replacement wins; a source edit invalidates this whole job.
            if self._read_source() != text or self._read_snapshot() != before:
                raise ValueError("Memory or snapshot changed while refreshing")
            os.replace(temporary, self.snapshot)
            if os.name != "nt":
                directory = os.open(self.source.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
