"""Dream model overrides select their own provider without changing chat routing."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.config.schema import Config
from nanobot.providers.base import LLMResponse
from nanobot.providers.factory import make_model_override_provider, make_provider


def dream_config(tmp_path, model="anthropic/claude-sonnet-4-6"):
    return Config.model_validate({
        "agents": {"defaults": {
            "workspace": str(tmp_path), "provider": "openai", "model": "gpt-4.1",
            "dream": {"model": model},
        }},
        "providers": {
            "openai": {"apiKey": "chat-test-key"},
            "anthropic": {"apiKey": "dream-test-key"},
        },
    })


async def test_dream_cross_provider_override_and_concurrent_chat(tmp_path, monkeypatch):
    config = dream_config(tmp_path)
    chat = make_provider(config)
    chat.chat_with_retry = AsyncMock(return_value=LLMResponse(content="chat answer"))
    started, release = asyncio.Event(), asyncio.Event()

    async def dream_reply(**kwargs):
        started.set()
        await release.wait()
        return LLMResponse(content="dream answer")

    dream_call = AsyncMock(side_effect=dream_reply)

    async def call_dream(provider, **kwargs):
        assert provider.api_key == "dream-test-key"
        return await dream_call(**kwargs)

    monkeypatch.setattr(
        "nanobot.providers.anthropic_provider.AnthropicProvider.chat_with_retry", call_dream,
    )
    loop = AgentLoop.from_config(config, provider=chat)
    task = asyncio.create_task(loop.process_direct(
        "Consolidate memory.", session_key="dream:test", ephemeral=True, model=loop.dream_model,
    ))
    try:
        await asyncio.wait_for(started.wait(), 5)
        reply = await loop.process_direct("Hello", session_key="chat:test")
        assert reply.content == "chat answer"
        assert loop.provider is chat and loop.runner.provider is chat
        assert loop.model == "gpt-4.1"
        assert chat.chat_with_retry.call_args.kwargs["model"] == "gpt-4.1"
        assert dream_call.call_args.kwargs["model"] == loop.dream_model
        release.set()
        assert (await task).content == "dream answer"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        loop.stop()


async def test_unconfigured_dream_provider_fails_without_using_chat(tmp_path):
    config = dream_config(tmp_path, "volcengine-plan/doubao")
    config.providers.volcengine.api_key = "regular-test-key"
    chat = make_provider(config)
    chat.chat_with_retry = AsyncMock(return_value=LLMResponse(content="chat answer"))
    # An invalid Dream override must not prevent ordinary chat from starting.
    loop = AgentLoop.from_config(config, provider=chat)
    try:
        with pytest.raises(ValueError, match="volcengine_coding_plan"):
            await loop.process_direct(
                "Consolidate memory.", session_key="dream:test", ephemeral=True,
                model=loop.dream_model,
            )
        chat.chat_with_retry.assert_not_awaited()
        reply = await loop.process_direct("Hello", session_key="chat:test")
        assert reply.content == "chat answer"
    finally:
        loop.stop()


@pytest.mark.parametrize("model", ["gpt-4.1-mini", "meta-llama/llama-3.3"])
def test_unqualified_override_preserves_forced_custom_endpoint(tmp_path, model):
    # A configured OpenAI key must not divert this override from the proxy.
    config = Config.model_validate({
        "agents": {"defaults": {"provider": "companyProxy", "workspace": str(tmp_path)}},
        "providers": {
            "openai": {"apiKey": "chat-test-key"},
            "companyProxy": {"apiBase": "https://proxy.example.test/v1", "apiKey": "proxy-test-key"},
        },
    })
    original = config.resolve_preset()
    provider = make_model_override_provider(config, model)

    assert provider.api_key == "proxy-test-key"
    assert provider.api_base == "https://proxy.example.test/v1"
    assert provider.get_default_model() == model
    assert config.resolve_preset() == original


@pytest.mark.parametrize("override", [None, "gpt-4.1"])
async def test_no_model_change_reuses_the_existing_chat_provider(tmp_path, override):
    config = dream_config(tmp_path, override)
    chat = make_provider(config)
    chat.chat_with_retry = AsyncMock(return_value=LLMResponse(content="dream answer"))
    loop = AgentLoop.from_config(config, provider=chat)
    try:
        reply = await loop.process_direct(
            "Consolidate memory.", session_key="dream:test", ephemeral=True, model=loop.dream_model,
        )
        assert reply.content == "dream answer"
        chat.chat_with_retry.assert_awaited_once()
        assert chat.chat_with_retry.call_args.kwargs["model"] == "gpt-4.1"
    finally:
        loop.stop()


async def test_override_uses_the_current_runtime_preset(tmp_path, monkeypatch):
    config = dream_config(tmp_path, "gpt-4.1-mini")
    config.model_presets["proxy"] = config.resolve_preset().model_copy(update={
        "model": "proxy-chat-model", "provider": "companyProxy",
    })
    config = Config.model_validate({**config.model_dump(), "providers": {
        **config.providers.model_dump(),
        "companyProxy": {"apiBase": "https://proxy.example.test/v1", "apiKey": "proxy-test-key"},
    }})

    async def reply(provider, **kwargs):
        assert provider.api_key == "proxy-test-key"
        assert kwargs["model"] == "gpt-4.1-mini"
        return LLMResponse(content="dream answer")

    monkeypatch.setattr(
        "nanobot.providers.openai_compat_provider.OpenAICompatProvider.chat_with_retry", reply,
    )
    loop = AgentLoop.from_config(config)
    try:
        loop.set_model_preset("proxy")
        current = loop.provider
        result = await loop.process_direct(
            "Consolidate memory.", session_key="dream:test", ephemeral=True, model=loop.dream_model,
        )
        assert result.content == "dream answer"
        assert loop.provider is current and loop.model_preset == "proxy"
        assert loop.model == "proxy-chat-model"
    finally:
        loop.stop()
