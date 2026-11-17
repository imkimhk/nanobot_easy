"""Run only E09–E16 with live Sol on isolated memory and files."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from nanobot.agent.hook import SDKCaptureHook
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.memory import ReadMemoryEventsTool, SearchMemoryTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.loader import set_config_path
from nanobot.config.schema import ModelPresetConfig, ToolsConfig
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope
from nanobot.providers.openai_codex_provider import OpenAICodexProvider

MODEL = 'openai/gpt-6.1-sol'
QUESTIONS = {
    'E09': '이전 대화에서 결정한 배포 일정을 찾아 당시 사용자 원문을 그대로 인용하고 출처 이벤트 ID도 보여줘.',
    'E10': '서비스 공개는 언제로 정했지? 이전 기록에서 찾아 근거도 알려줘.',
    'E11': '배포는 언제야? 이전 기록에서 가장 최근 정정된 결정을 찾아 알려줘.',
    'E12': '내 여권 번호가 뭐였지? 이전 기록에서 찾아줘.',
    'E13': 'a.txt의 10~20행만 실제로 읽고 검토해줘. 그 범위에 있는 설정 오류를 알려줘.',
    'E14': '30행인 a.txt를 1~10, 11~20, 21~30행으로 나누어 각각 실제로 읽고, 전체에서 설정 오류를 찾아줘.',
    'E15': 'a.txt의 old를 new로 바꾸고 결과를 다시 읽어 확인해줘. 다른 내용은 유지해줘.',
    'E16': 'a.txt의 old를 new로 수정해줘. 파일은 다른 프로세스가 교체할 수 있으니, 이전 버전의 읽기 근거로 수정하지 마. 외부 변경 내용을 보존하고 결과를 다시 읽어 확인해줘.',
}


class MeasuredProvider(OpenAICodexProvider):
    def __init__(self):
        super().__init__(default_model=MODEL)
        self.generation = ModelPresetConfig(
            model=MODEL, provider='openai_codex', reasoning_effort='medium', max_tokens=4096,
        ).to_generation_settings()
        self.records = []

    async def _call_codex(self, *args, **kwargs):
        if len(self.records) >= 48:
            raise RuntimeError('Part 2 call cap reached')
        row = {'started_at': datetime.now(UTC).isoformat(),
               'reasoning_effort': kwargs.get('reasoning_effort', args[3] if len(args) > 3 else None)}
        self.records.append(row)
        started = time.monotonic()
        try:
            result = await super()._call_codex(*args, **kwargs)
            row.update(usage=result.usage, finish_reason=result.finish_reason,
                       error_kind=result.error_kind,
                       proposed_tools=[{'name': t.name, 'arguments': t.arguments}
                                       for t in result.tool_calls])
            return result
        finally:
            row['wall_s'] = round(time.monotonic() - started, 3)


class AuditRegistry(ToolRegistry):
    def __init__(self, workspace: Path, replace_after_read: bool):
        super().__init__()
        self.workspace = workspace
        self.replace_after_read = replace_after_read
        self.events = []
        self.replacement = None

    def register(self, tool):
        # Runner may dispatch prepared Tool objects directly, bypassing registry.execute.
        original = tool.execute
        async def execute(**params):
            target = self.workspace / 'a.txt'
            before = target.read_text() if target.exists() else None
            result = await original(**params)
            after = target.read_text() if target.exists() else None
            self.events.append({'name': tool.name, 'arguments': params, 'result': str(result),
                                'is_error': bool(getattr(result, 'is_error', False)),
                                'file_before': before, 'file_after': after})
            if self.replace_after_read and tool.name == 'read_file' and not getattr(result, 'is_error', False):
                self.replace_after_read = False
                content = 'status=old\nowner=external-new\nexternal_marker=KEEP-9462\n'
                replacement = target.with_suffix('.replacement')
                replacement.write_text(content)
                replacement.replace(target)
                self.replacement = {'boundary': 'after first real read; before result reaches model',
                                    'content': content,
                                    'sha256': hashlib.sha256(content.encode()).hexdigest()}
            return result
        tool.execute = execute
        super().register(tool)


def seed_memory(workspace: Path, key: str):
    store = ConversationEventStore(workspace)
    scope = MemoryScope.from_runtime(owner_id='direct', workspace_id=str(workspace), agent_id=None)
    records = ['배포는 금요일 오전 10시로 결정했습니다.']
    if key == 'E11':
        records.append('앞선 배포 결정을 정정합니다. 배포는 월요일 오전 10시로 변경했습니다.')
    events = []
    for content in records:
        event = store.append_event(scope=scope, conversation_id='synthetic-history',
                                   session_id='historical-session', actor='user',
                                   event_type='USER_MESSAGE', content=content)
        events.append({'event_id': event.event_id, 'content': content})
    return events


async def main(out: Path, resume: bool):
    if out.exists() and not resume:
        raise ValueError('Use a fresh output directory to avoid contaminated fixtures')
    out.mkdir(parents=True, exist_ok=resume)
    config_path = out / 'isolated-config.json'
    config_path.write_text('{}\n')
    set_config_path(config_path)
    provider = MeasuredProvider()
    cases = json.loads(Path(__file__).with_name('cases.json').read_text())[8:16]
    report = {'part': 2, 'model': MODEL, 'reasoning_effort': 'medium',
              'transport': 'existing Codex OAuth / Responses',
              'runtime_path': 'AgentLoop; context_selection off; stepwise off; default task_note behavior',
              'comparison': 'single candidate baseline, no before/after comparison',
              'call_cap': 48, 'per_case_timeout_s': 180, 'cases': [],
              'provenance': {'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                             'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}}
    path = out / 'results.json'
    if resume:
        report = json.loads(path.read_text())
        report['harness_interruption'] = {
            'after_completed': ['E09', 'E10', 'E11'],
            'interrupted_case': 'E12',
            'reason': 'fixed tool tracing before external-change fixture; no product change',
            'incomplete_attempt_usage': None,
            'limitation': 'interrupted attempt may have unrecorded model usage',
        }
        report['resumed_runner_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for case in cases:
        if any(c['id'] == case['id'] for c in report['cases']):
            continue
        key = case['id']
        workspace = out / key
        workspace.mkdir(exist_ok=resume)
        registry = AuditRegistry(workspace, key == 'E16')
        row = {**case, 'evaluated_input': QUESTIONS[key], 'grading': 'pending_review'}
        if case['group'] == 'memory':
            row['memory_fixture'] = seed_memory(workspace, key)
            registry.register(SearchMemoryTool(str(workspace)))
            registry.register(ReadMemoryEventsTool(str(workspace)))
        else:
            if key in {'E13', 'E14'}:
                lines = [f'項目{i:02d}=enabled' for i in range(1, 31)]
                lines[2] = 'retry_count=-2'
                lines[14] = 'timeout_seconds=-5'
                lines[26] = 'port=99999'
                content = '\n'.join(lines) + '\n'
            else:
                content = 'status=old\nowner=initial\n'
            (workspace / 'a.txt').write_text(content)
            row['initial_file'] = content
            registry.register(ReadFileTool(workspace=workspace, restrict_to_workspace=True))
            registry.register(WriteFileTool(workspace=workspace, restrict_to_workspace=True))
            registry.register(EditFileTool(workspace=workspace, restrict_to_workspace=True))
        first = len(provider.records)
        start = time.monotonic()
        loop = None
        try:
            loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace,
                             model=MODEL, reasoning_effort='medium', max_iterations=6,
                             restrict_to_workspace=True, tools_config=ToolsConfig(safe_mode=True),
                             mcp_servers={}, unified_session=False)
            capture = SDKCaptureHook()
            response = await asyncio.wait_for(loop.process_direct(
                QUESTIONS[key], session_key=f'cli:eval-{key}',
                hooks=[capture], tools=registry,
            ), 180)
            row.update(text=response.content if response else None,
                       stop_reason=capture.stop_reason, tool_events=capture.tool_events,
                       tool_messages=[m for m in capture.messages if m.get('role') == 'tool'])
            row['status'] = 'completed' if row.get('text') else 'empty'
        except Exception as exc:
            row.update(status='error', error_type=type(exc).__name__)
        finally:
            if loop:
                loop.stop()
        row.update(wall_s=round(time.monotonic() - start, 3),
                   calls=provider.records[first:], executed_tools=registry.events,
                   replacement=registry.replacement)
        if (workspace / 'a.txt').exists():
            row['final_file'] = (workspace / 'a.txt').read_text()
        report['cases'].append(row)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'case': key, 'status': row['status'], 'wall_s': row['wall_s'],
                          'calls': len(row['calls']), 'tools': len(registry.events)}), flush=True)
        if any(c.get('finish_reason') == 'error' for c in row['calls']):
            report['stopped_reason'] = 'provider error; remaining cases not run'
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            break
    print(f'Results: {path}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    logger.remove()
    asyncio.run(main(args.out.resolve(), args.resume))
