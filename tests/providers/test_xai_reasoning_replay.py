"""Opaque Grok reasoning must survive tool rounds and durable session replay."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.providers.openai_responses.converters import convert_messages
from nanobot.providers.openai_responses.parsing import _consume_response_events
from nanobot.providers.xai_grok_provider import XAIGrokProvider
from nanobot.session.manager import SessionManager

REASONING = {
    "type": "reasoning", "id": "rs_1", "status": "completed",
    "summary": [], "encrypted_content": "opaque-ciphertext-not-display-text",
}
STATE = {"provider": "xai_grok", "model": "grok-4.7", "reasoning_items": [REASONING]}


def _install_transport(monkeypatch, outputs):
    original_client = httpx.AsyncClient
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        output = outputs[len(bodies) - 1]
        event = {"type": "response.completed", "response": {
            "status": "completed", "output": output,
        }}
        return httpx.Response(200, content=f"data: {json.dumps(event)}\n\n", request=request)

    monkeypatch.setattr(
        "nanobot.providers.xai_grok_provider.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        "nanobot.providers.xai_grok_provider.get_xai_oauth_token",
        lambda **kwargs: SimpleNamespace(access="test-token"),
    )
    return bodies


@pytest.mark.asyncio
async def test_native_state_survives_provider_runner_checkpoint_and_disk(monkeypatch, tmp_path):
    final = {"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": "done"},
    ]}
    bodies = _install_transport(monkeypatch, [
        [REASONING, {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                     "name": "read_file", "arguments": '{"path":"example.py"}'}],
        [dict(REASONING, id="rs_2"), final],
        [final],
    ])
    provider = XAIGrokProvider(default_model="xai-grok/grok-4.7", hosted_search=False)
    tools = MagicMock()
    tools.get_definitions.return_value = [{"type": "function", "function": {
        "name": "read_file", "parameters": {"type": "object"},
    }}]
    tools.execute = AsyncMock(return_value="synthetic file")
    checkpoints = []

    async def checkpoint(payload):
        checkpoints.append(deepcopy(payload))

    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "inspect example.py"}],
        tools=tools, model="xai-grok/grok-4.7", max_iterations=3,
        max_tool_result_chars=10000, checkpoint_callback=checkpoint,
    ))
    assert result.final_content == "done"
    assert bodies[1]["input"][1] == REASONING
    assert [x.get("type") for x in bodies[1]["input"]][1:] == [
        "reasoning", "function_call", "function_call_output",
    ]
    assert any(cp.get("assistant_message", {}).get("provider_state") == STATE for cp in checkpoints)
    assert "opaque-ciphertext" not in result.final_content

    manager = SessionManager(tmp_path)
    session = manager.get_or_create("test:reasoning")
    loop = object.__new__(AgentLoop)
    loop.max_tool_result_chars = 10000
    loop._save_turn(session, result.messages, 0)
    manager.save(session)
    restored = SessionManager(tmp_path).get_or_create("test:reasoning").get_history()
    assert next(m for m in restored if m.get("tool_calls"))["provider_state"] == STATE
    await provider.chat(restored + [{"role": "user", "content": "confirm"}])
    assert [x for x in bodies[2]["input"] if x.get("type") == "reasoning"] == [
        REASONING, dict(REASONING, id="rs_2"),
    ]
    assert all(b["include"] == ["reasoning.encrypted_content"] for b in bodies)
    assert all("reasoning" not in b for b in bodies)  # No explicit effort is necessary.


@pytest.mark.parametrize("scope", [None, ("other", "grok-4.7"), ("xai_grok", "grok-4.5")])
def test_state_never_replayed_to_different_provider_or_model(scope):
    _, items = convert_messages([
        {"role": "assistant", "content": "visible", "provider_state": STATE},
    ], reasoning_scope=scope)
    assert all(x.get("type") != "reasoning" for x in items)
    assert "opaque-ciphertext" not in json.dumps(items)


def test_native_item_identity_is_preserved_without_duplicates_or_aliasing():
    message = {"role": "assistant", "content": "", "provider_state": deepcopy(STATE)}
    _, items = convert_messages([message, message], reasoning_scope=("xai_grok", "grok-4.7"))
    assert items == [REASONING]
    items[0]["summary"].append({"type": "summary_text", "text": "modified"})
    assert message["provider_state"]["reasoning_items"][0] == REASONING


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["incomplete", "cancelled", "failed", "missing"])
async def test_only_completed_responses_supply_reusable_state(status):
    captured = []

    async def events():
        yield {"type": "response.output_item.done", "item": REASONING}
        if status != "missing":
            yield {"type": f"response.{status}", "response": {
                "status": status, "output": [REASONING],
            }}

    if status == "incomplete":
        await _consume_response_events(events(), on_response_completed=captured.append)
    else:
        with pytest.raises(RuntimeError):
            await _consume_response_events(events(), on_response_completed=captured.append)
    assert captured == []


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "none", "xhigh"])
async def test_encrypted_state_requested_independently_of_effort(monkeypatch, effort):
    bodies = _install_transport(monkeypatch, [[REASONING]])
    provider = XAIGrokProvider(hosted_search=False, extra_body={"include": ["other.field"]})
    result = await provider.chat([{"role": "user", "content": "hello"}], reasoning_effort=effort)
    assert bodies[0]["include"] == ["other.field", "reasoning.encrypted_content"]
    assert result.provider_state["reasoning_items"] == [REASONING]
    assert result.reasoning_content is None
