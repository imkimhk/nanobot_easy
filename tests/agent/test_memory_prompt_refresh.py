"""Automatic maintenance cannot block chat, lose source edits or grant permissions."""

import asyncio
import json
from contextvars import ContextVar

import pytest
from filelock import FileLock

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory_prompt import BoundedMemoryPrompt, make_snapshot
from nanobot.agent.memory_prompt_refresh import MemoryPromptRefresher
from nanobot.config.schema import Config
from nanobot.providers.base import LLMResponse, hosted_tools_enabled
from tests.agent.test_compact_context import Script, call

RULE = 'Reply in Korean. Never restart other gateways or send messages without approval.'
REVISED = RULE + '\nNew correction: never overwrite an existing report.'
APPROVED = json.dumps({'constraints_preserved': True, 'faithful': True,
                       'no_new_authority': True, 'issues': []})


@pytest.fixture(autouse=True)
def no_debounce(monkeypatch):
    monkeypatch.setattr('nanobot.agent.memory_prompt_refresh._DEBOUNCE_S', 0)


def setup(root, provider=None):
    directory = root / 'memory'
    directory.mkdir()
    source = directory / 'MEMORY.md'
    text = RULE + '\n' + 'Old implementation detail.\n' * 400
    source.write_text(text)
    loader = BoundedMemoryPrompt(source, 240)
    snapshot = source.with_name('prompt_snapshot.json')
    snapshot.write_text(json.dumps(make_snapshot(text, RULE, source, 240)))
    provider = provider or Script([LLMResponse(content=REVISED), LLMResponse(content=APPROVED)])
    refresh = MemoryPromptRefresher(source, 240, lambda: (provider, 'test'))
    loader.refresher = refresh
    return source, snapshot, loader, refresh, provider


async def test_stale_falls_back_immediately_then_refreshes_once_without_source_mutation(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    original = snapshot.read_bytes()
    revised = source.read_text() + '\nNew correction: never overwrite an existing report.'
    source.write_text(revised)
    builder = ContextBuilder(tmp_path, memory_prompt_tokens=240)
    builder.memory_prompt = loader
    assert revised in builder.build_system_prompt()  # No wait for the model.
    job = refresh._task
    for _ in range(5):
        assert revised in builder.build_compact_system_prompt()
        assert refresh._task is job
    await job
    assert source.read_text() == revised
    assert snapshot.read_bytes() != original
    assert REVISED in builder.build_system_prompt()
    assert 'Old implementation detail.' not in builder.build_compact_system_prompt()
    assert len(provider.calls) == 2
    assert all(c['tools'] is None and c['max_retries'] == 0 for c in provider.calls)
    assert len(provider.calls[1]['messages']) == 2  # Separate review, no draft conversation.
    assert json.loads(snapshot.read_text())['refresh']['method'] == 'model-reviewed'
    await refresh.close()


async def test_current_snapshot_never_spends_and_small_source_refresh_is_lossless(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    assert loader.build(source.read_text())
    assert refresh._task is None
    source.write_text(REVISED)
    assert loader.build(REVISED) is None
    await refresh._task
    data = json.loads(snapshot.read_text())
    assert data['summary'] == REVISED
    assert data['refresh']['method'] == 'lossless'
    assert provider.calls == []
    assert loader.build(REVISED)
    await refresh.close()


@pytest.mark.parametrize('bad', [
    LLMResponse(content='provider error', finish_reason='error'),
    LLMResponse(content=REVISED, finish_reason='length'),
    call('write_file', path='MEMORY.md', content='unauthorized'),
    LLMResponse(content=''),
    LLMResponse(content='too large ' * 2000),
])
async def test_bad_generation_keeps_raw_memory_and_previous_snapshot(tmp_path, bad):
    source, snapshot, loader, refresh, provider = setup(tmp_path, Script([bad]))
    before = snapshot.read_bytes()
    revised = source.read_text() + '\nNew correction: never overwrite an existing report.'
    source.write_text(revised)
    assert loader.build(revised) is None
    await refresh._task
    assert snapshot.read_bytes() == before and source.read_text() == revised
    assert loader.build(revised) is None
    assert len(provider.calls) == 1  # Cooldown prevents per-question regeneration.
    await refresh.close()


@pytest.mark.parametrize('review', [
    'not JSON', '[]', '{}',
    json.dumps({'constraints_preserved': 1, 'faithful': True,
                'no_new_authority': True, 'issues': []}),
    json.dumps({'constraints_preserved': False, 'faithful': True,
                'no_new_authority': True, 'issues': ['New restriction missing']}),
    json.dumps({'constraints_preserved': True, 'faithful': True,
                'no_new_authority': False, 'issues': ['Added permission']}),
    APPROVED.replace('[]', '["unresolved omission"]'),
])
async def test_rejected_or_malformed_review_never_activates_candidate(tmp_path, review):
    source, snapshot, loader, refresh, provider = setup(tmp_path, Script([
        LLMResponse(content=RULE), LLMResponse(content=review)]))
    before = snapshot.read_bytes()
    revised = source.read_text() + '\nNew correction: never overwrite an existing report.'
    source.write_text(revised)
    loader.build(revised)
    await refresh._task
    assert loader.build(revised) is None and snapshot.read_bytes() == before
    assert source.read_text() == revised and len(provider.calls) == 2
    await refresh.close()


@pytest.mark.parametrize('edit', ['source', 'snapshot'])
async def test_edit_during_generation_wins_over_background_job(tmp_path, edit):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(source.read_text() + '\nNew correction: never overwrite an existing report.')
    original = provider.chat_with_retry

    async def change(**kwargs):
        if not provider.calls:
            target = source if edit == 'source' else snapshot
            target.write_text(target.read_text() + '\nmanual change')
        return await original(**kwargs)

    provider.chat_with_retry = change
    before = snapshot.read_bytes()
    loader.build(source.read_text())
    await refresh._task
    assert snapshot.read_bytes() == before + (b'\nmanual change' if edit == 'snapshot' else b'')
    assert 'manual change' in (source if edit == 'source' else snapshot).read_text()
    await refresh.close()


async def test_refresh_isolated_from_turn_context_and_shutdown_cancels_promptly(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(source.read_text() + '\nNew correction: never overwrite an existing report.')
    turn = ContextVar('synthetic_turn_permission', default=False)
    entered = asyncio.Event()
    original = snapshot.read_bytes()

    async def wait(**kwargs):
        assert not turn.get() and not hosted_tools_enabled()
        entered.set()
        await asyncio.Event().wait()

    provider.chat_with_retry = wait
    token = turn.set(True)
    try:
        assert loader.build(source.read_text()) is None
    finally:
        turn.reset(token)
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(refresh.close(), 1)
    assert refresh._task.cancelled() and snapshot.read_bytes() == original
    old_job = refresh._task
    loader.build(source.read_text())
    assert refresh._task is old_job  # Shutdown cannot schedule new maintenance.


@pytest.mark.parametrize('path', ['MEMORY.md', 'prompt_snapshot.json', '.prompt_snapshot.lock'])
async def test_symlink_escape_cannot_be_read_or_overwritten(tmp_path, path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(REVISED)
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.write_text('OUTSIDE MUST NOT CHANGE')
    link = source.with_name(path)
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip('Symlinks unavailable')
    refresh.request(REVISED)
    await refresh._task
    assert outside.read_text() == 'OUTSIDE MUST NOT CHANGE'
    assert link.is_symlink() and provider.calls == []
    await refresh.close()


async def test_cross_process_lock_avoids_duplicate_spend_and_releases_after_failure(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(source.read_text() + '\nNew correction: never overwrite an existing report.')
    before = snapshot.read_bytes()
    with FileLock(refresh.lock_path, timeout=0):
        loader.build(source.read_text())
        await refresh._task
    assert snapshot.read_bytes() == before and provider.calls == []
    refresh._retry_at = 0
    loader.build(source.read_text())
    await refresh._task
    assert loader.build(source.read_text()) and len(provider.calls) == 2
    await refresh.close()


async def test_failed_atomic_publish_preserves_existing_snapshot(tmp_path, monkeypatch):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(REVISED)
    before = snapshot.read_bytes()

    def disk_full(*args):
        raise OSError('synthetic disk full')

    monkeypatch.setattr('nanobot.agent.memory_prompt_refresh.os.replace', disk_full)
    loader.build(REVISED)
    await refresh._task
    assert snapshot.read_bytes() == before and source.read_text() == REVISED
    assert not list(source.parent.glob('.memory-prompt-*'))
    assert provider.calls == []
    await refresh.close()


async def test_source_too_large_is_not_sent_or_truncated(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    revised = REVISED + '\n' + '한글' * 30_000
    source.write_text(revised)
    before = snapshot.read_bytes()
    loader.build(revised)
    await refresh._task
    assert snapshot.read_bytes() == before and source.read_text() == revised
    assert provider.calls == [] and loader.build(revised) is None
    await refresh.close()


async def test_timeout_preserves_fallback_and_new_hash_can_retry(tmp_path, monkeypatch):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(source.read_text() + '\nNew correction: never overwrite an existing report.')
    original = provider.chat_with_retry

    async def wait(**kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr('nanobot.agent.memory_prompt_refresh._REFRESH_TIMEOUT_S', 0.01)
    provider.chat_with_retry = wait
    before = snapshot.read_bytes()
    loader.build(source.read_text())
    await asyncio.wait_for(refresh._task, 1)
    assert snapshot.read_bytes() == before
    source.write_text(source.read_text() + '\nAnother edit; the correction still applies.')
    provider.chat_with_retry = original
    monkeypatch.setattr('nanobot.agent.memory_prompt_refresh._REFRESH_TIMEOUT_S', 1)
    loader.build(source.read_text())
    await refresh._task
    assert loader.build(source.read_text()) and len(provider.calls) == 2
    await refresh.close()


@pytest.mark.parametrize('file_access,budget,enabled', [
    (True, 240, True), (False, 240, True), (True, 0, True), (True, 240, False),
])
def test_opt_in_wiring_respects_file_access_and_zero_budget(tmp_path, file_access, budget, enabled):
    config = Config.model_validate({'agents': {'defaults': {
        'workspace': str(tmp_path), 'memoryPromptTokens': budget,
        'memoryPromptAutoRefresh': enabled}}, 'tools': {'file': {'enable': file_access},
                                                      'safeMode': True}})
    loop = AgentLoop.from_config(config, provider=Script([]))
    try:
        assert (loop.context.memory_prompt.refresher is not None) == (file_access and budget > 0 and enabled)
        assert Config().agents.defaults.memory_prompt_auto_refresh is False
    finally:
        loop.stop()
