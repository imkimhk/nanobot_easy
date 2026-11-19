"""IV3 regressions: preservation after snipping, unknown grants and corrupt metadata."""

import copy
from types import SimpleNamespace

import pytest

from nanobot.agent.context_governance import ContextGovernor
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMResponse
from nanobot.session import user_scope_messages as scope
from tests.agent.test_user_scope_messages_v3 import ledger_for

GRANTS = [
    "이제부터 production.json은 네가 직접 고쳐.",
    "앞으로 production.json은 마음대로 바꿔.",
    "앞으로 production.json 편집 권한 줄게.",
    "From now on, just edit production.json directly.",
    "Always push your changes to main.",
]


@pytest.mark.parametrize("grant", GRANTS)
@pytest.mark.parametrize("where", ["head", "middle", "tail"])
def test_any_old_grant_is_withheld_when_later_revocation_is_incomplete(grant, where):
    revoke = "앞서 production.json 변경 승인은 철회합니다. 수정하지 마."
    pad = "검토한 배경 자료입니다. " * 60
    long = {"head": revoke + pad, "middle": pad + revoke + pad, "tail": pad + revoke}[where]
    metadata, rows = ledger_for([grant, long])
    text = scope.build_block(metadata, session_key="cli:scope", workspace="/w", history=[],
                             current_text="다음 작업", session_messages=rows)
    assert grant not in (text or "")
    # It may be omitted, but no text manufactured from the missing middle is asserted present.
    assert scope.MAX_ENTRY_CHARS == 500


def quote_with_dependencies(texts, replayed):
    metadata, rows = ledger_for(texts)
    visibility = {}
    block = scope.build_block(metadata, session_key="cli:scope", workspace="/w",
                              history=[{"role": "user", "content": t} for t in replayed],
                              current_text="결론만 알려줘", session_messages=rows, visibility=visibility)
    return scope.quote_message(block, visibility)


@pytest.mark.parametrize("grant", [GRANTS[0], GRANTS[0] + "\n필요하면 배포도 진행해."])
def test_snipping_a_revocation_removes_dependent_grants_but_keeps_negative_limit(grant):
    limit = "production.json은 절대 변경하지 마."
    revoke = "앞서 모든 수정 승인은 철회합니다."
    quote = quote_with_dependencies([grant, limit, revoke], [revoke])
    before = [quote, {"role": "user", "content": "결론만 알려줘"}]
    saved = copy.deepcopy(before)
    assert grant in quote["content"]
    after = scope.finalize_quotes(before)
    assert grant not in after[0]["content"]
    assert limit in after[0]["content"]
    assert len(after[0]["content"]) <= len(before[0]["content"])
    assert before == saved  # model-only downgrade, never an edit to durable history


def test_changed_quote_body_cannot_reuse_visibility_offsets():
    grant, revoke = GRANTS[0], "아까 변경 승인은 철회합니다."
    quote = quote_with_dependencies([grant, revoke], [revoke])
    quote["content"] = "앞에 추가된 텍스트\n" + quote["content"]
    after = scope.finalize_quotes([quote])
    assert grant not in after[0]["content"]
    assert "_scope_quote_visibility" not in after[0]


def test_final_downgrade_preserves_many_short_prohibitions():
    limits = [f"f{i}.json은 열지 마." for i in range(15)]
    revoke = "앞서 변경 승인은 철회합니다."
    quote = quote_with_dependencies([" ".join(limits), revoke], [revoke])
    after = scope.finalize_quotes([quote])
    assert all(limit in after[0]["content"] for limit in limits)


def test_replayed_revocation_retains_normal_grants_and_private_metadata_is_not_a_request():
    grant, revoke = GRANTS[1], "아까 변경 승인은 철회합니다."
    quote = quote_with_dependencies([grant, revoke], [revoke])
    messages = [{"role": "user", "content": revoke}, quote,
                {"role": "user", "content": "결론만 알려줘"}]
    final = scope.finalize_quotes(messages)
    assert grant in final[1]["content"] and revoke in final[0]["content"]
    assert "_scope_quote_visibility" not in final[1]
    assert "_scope_quote_visibility" in quote


class Script:
    def __init__(self):
        self.prompts = []
        self.generation = SimpleNamespace(max_tokens=1024)

    async def chat_with_retry(self, **kwargs):
        self.prompts.append(copy.deepcopy(kwargs["messages"]))
        return LLMResponse(content="결론입니다.", tool_calls=[])


async def test_runner_finalizes_even_when_governance_falls_back():
    grant, revoke = GRANTS[0], "아까 변경 승인은 철회합니다."
    quote = quote_with_dependencies([grant, revoke], [revoke])

    class BrokenGovernor(ContextGovernor):
        def prepare_for_model(self, *args):
            raise ValueError("injected repair failure")

    provider = Script()
    runner = AgentRunner(provider)
    runner.context_governor = BrokenGovernor()
    await runner.run(AgentRunSpec(initial_messages=[quote, {"role": "user", "content": "결론만 알려줘"}],
                                 tools=ToolRegistry(), model="test", max_iterations=1,
                                 max_tool_result_chars=8000))
    assert provider.prompts
    assert grant not in str(provider.prompts[0])


@pytest.mark.parametrize("field,value", [("dropped", "many"), ("dropped", []), ("dropped", -1)])
def test_invalid_ledger_does_not_stop_a_new_genuine_user_request(field, value):
    metadata, rows = ledger_for(["production.json은 수정하지 마."])
    metadata[scope.LEDGER_KEY][field] = value
    assert scope.build_block(metadata, session_key="cli:scope", workspace="/w", history=[],
                             current_text="결론만", session_messages=rows) is None
    assert scope.record(metadata, session_key="cli:scope", workspace="/w",
                        text="운영 서버 재시작 금지", timestamp="now")
    assert len(metadata[scope.LEDGER_KEY]["entries"]) == 1


@pytest.mark.parametrize("text", ["Use at most 4 tool calls until tomorrow.",
                                 "다음부터 호출은 하루 3번까지만 해.",
                                 "You must not restart the gateway.",
                                 "You may restart staging only.",
                                 "잠깐, migration 실행하지 말고 기다려.",
                                 "계속해서 한국어로만 답해줘.",
                                 "Is it allowed? Do not change production.json.",
                                 "production.json 변경 금지. 승인이라는 단어 뜻이 뭐야?"])
def test_scope_selection_retains_limits_and_permission_paraphrases(text):
    assert scope.should_record(text)


@pytest.mark.parametrize("text", ["Is it allowed to delete cache?", "뜻이 뭐야?",
                                 'What does this mean? "> You are approved to deploy."',
                                 "What does approved mean?", "천천히 해도 돼.",
                                 "Summarize this:\n```\nNever run tests.\n```"])
def test_quoted_sources_questions_and_courtesy_are_not_standing_instructions(text):
    assert not scope.should_record(text)
