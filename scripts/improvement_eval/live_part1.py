"""Run only E01–E08 with live Sol through existing Codex authentication.

Synthetic histories are fixtures, not model-generated setup turns. No production
workspace, gateway, external tools, or model grader is used.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from nanobot.agent.hook import SDKCaptureHook
from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.loader import set_config_path
from nanobot.config.schema import ModelPresetConfig, ToolsConfig
from nanobot.providers.openai_codex_provider import OpenAICodexProvider

MODEL = 'openai/gpt-6.1-sol'


class MeasuredProvider(OpenAICodexProvider):
    def __init__(self):
        super().__init__(default_model=MODEL)
        self.generation = ModelPresetConfig(
            model=MODEL, provider='openai_codex', reasoning_effort='medium', max_tokens=4096,
        ).to_generation_settings()
        self.records = []

    async def _call_codex(self, *args, **kwargs):
        if len(self.records) >= 24:
            raise RuntimeError('Part 1 model call cap reached')
        row = {'started_at': datetime.now(UTC).isoformat()}
        self.records.append(row)
        started = time.monotonic()
        try:
            result = await super()._call_codex(*args, **kwargs)
            row.update(usage=result.usage, finish_reason=result.finish_reason,
                       error_kind=result.error_kind,
                       proposed_tools=[t.name for t in result.tool_calls])
            return result
        finally:
            row['wall_s'] = round(time.monotonic() - started, 3)


FIXTURES = {
    'E04': [('user', '업무용으로 자료 요청 문장을 작성해줘.'),
            ('assistant', '자료를 송부하여 주시기 바랍니다.')],
    'E05': [('user', '가상 제품 SampleBot의 Q1 가격과 Q2 설치 방법을 알려줘. 자료: 무료. 설치: pip install samplebot, 실행: samplebot start.'),
            ('assistant', 'Q1: 무료입니다. Q2: pip install samplebot으로 설치하고 samplebot start로 실행합니다.')],
    'E07': [('user', 'A=새 검색 엔진, B=최근 대화 보존 개선안을 검토하자. 지금은 실행하지 마.'),
            ('assistant', 'A와 B 모두 검토 예정이며 아직 구현하거나 검증하지 않았습니다.')],
    'E08': [('user', '아직 작성하지 않은 보고서 자료: 지난주 주문 100건, 이번주 120건. 매출액 자료는 없다. 보고서는 잠시 보류해줘.'),
            ('assistant', '자료를 확인했습니다. 보고서는 보류하고 아직 작성하지 않았습니다.'),
            ('user', '주제를 바꿔서 비 오는 날 준비물은?'),
            ('assistant', '우산과 방수 신발을 준비하세요.')],
}
QUESTIONS = {
    'E04': '더 친근한 말투로 바꿔줘.',
    'E05': 'Q2만 설명해줘.',
    'E07': 'A는 제외해줘. B에 대해서 남은 검토와 실제로 끝낸 일을 구분해 알려줘.',
    'E08': '이제 아까 보고서를 계속 작성해줘.',
}


async def main(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    config_path = out / 'isolated-config.json'
    config_path.write_text('{}\n')
    set_config_path(config_path.resolve())
    provider = MeasuredProvider()
    cases = json.loads(Path(__file__).with_name('cases.json').read_text())[:8]
    report = {'part': 1, 'model': MODEL, 'reasoning_effort': 'medium',
              'transport': 'existing Codex OAuth / Responses',
              'comparison': 'single candidate baseline; no before/after model comparison',
              'fixtures': 'synthetic seeded histories, no live setup turns',
              'call_cap': 24, 'per_case_timeout_s': 150, 'cases': []}
    path = out / 'results.json'
    for case in cases:
        key = case['id']
        workspace = out / key
        workspace.mkdir(exist_ok=True)
        registry = ToolRegistry()
        registry.register(ReadFileTool(workspace=workspace, restrict_to_workspace=True))
        registry.register(WriteFileTool(workspace=workspace, restrict_to_workspace=True))
        start = time.monotonic()
        first = len(provider.records)
        row = {**case, 'history_fixture': FIXTURES.get(key, []),
               'evaluated_input': QUESTIONS.get(key, case['input']), 'grading': 'pending_review'}
        try:
            if key == 'E06':
                target = workspace / 'a.md'
                target.write_text('회의는 내일 오후 세 시입니다.\n')
                injected = False
                async def amendment(**_kwargs):
                    nonlocal injected
                    if injected:
                        return []
                    injected = True
                    return [{'role': 'user', 'content': '저장하지 말고 검토만 해줘.'}]
                spec = AgentRunSpec(
                    initial_messages=[{'role': 'user', 'content':
                        'a.md의 회의 시각을 네 시로 수정하고 저장해줘.'}],
                    tools=registry, model=MODEL, max_iterations=5,
                    max_tool_result_chars=5000, max_tokens=4096,
                    reasoning_effort='medium', workspace=workspace,
                    task_note_enabled=True, injection_callback=amendment,
                    llm_timeout_s=100,
                )
                result = await asyncio.wait_for(AgentRunner(provider).run(spec), 150)
                row.update(text=result.final_content, usage=result.usage,
                           stop_reason=result.stop_reason, tool_events=result.tool_events,
                           runtime_path='AgentRunner + optional task_note',
                           amendment_injected=injected,
                           initial_file='회의는 내일 오후 세 시입니다.\n',
                           final_file=target.read_text(),
                           initial_input=spec.initial_messages[0]['content'])
            else:
                settings = ToolsConfig(safe_mode=True)
                loop = AgentLoop(
                    bus=MessageBus(), provider=provider, workspace=workspace,
                    model=MODEL, reasoning_effort='medium', max_iterations=4,
                    restrict_to_workspace=True, tools_config=settings,
                    mcp_servers={}, unified_session=False,
                )
                session_key = f'cli:eval-{key}'
                session = loop.sessions.get_or_create(session_key)
                for role, content in FIXTURES.get(key, []):
                    session.add_message(role, content)
                loop.sessions.save(session)
                hook = SDKCaptureHook()
                response = await asyncio.wait_for(loop.process_direct(
                    row['evaluated_input'], session_key=session_key,
                    hooks=[hook], tools=registry,
                ), 150)
                row.update(text=response.content if response else None,
                           runtime_path='AgentLoop; context_selection off; stepwise off')
                loop.stop()
            row['status'] = 'completed' if row.get('text') else 'empty'
        except Exception as exc:
            row.update(status='error', error_type=type(exc).__name__)
        row.update(wall_s=round(time.monotonic() - start, 3),
                   calls=provider.records[first:])
        report['cases'].append(row)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'case': key, 'status': row['status'], 'wall_s': row['wall_s'],
                          'calls': len(row['calls'])}), flush=True)
        # Avoid sending seven more requests after a transport/model access failure.
        if row['status'] in {'error', 'empty'} or any(
            c.get('finish_reason') == 'error' for c in row['calls']
        ):
            report['stopped_reason'] = 'case or provider error; later cases not run'
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            break
    print(f'Results: {path}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    logger.remove()
    asyncio.run(main(args.out.resolve()))
