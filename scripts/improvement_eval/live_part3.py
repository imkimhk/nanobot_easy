"""Live E17–E24 only. Real local tools with explicit synthetic fault injection."""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

from loguru import logger

from nanobot.agent.hook import SDKCaptureHook
from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.skill_decision import SkillDecisionTool
from nanobot.agent.tools.skill_search import SkillSearchTool
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.loader import set_config_path
from nanobot.config.schema import Config, ModelPresetConfig, ToolsConfig
from nanobot.providers.openai_codex_provider import OpenAICodexProvider
from nanobot.session.manager import SessionManager
from nanobot.skill_store import SkillStore

MODEL = 'openai/gpt-6.1-sol'


class MeasuredProvider(OpenAICodexProvider):
    def __init__(self, journal):
        super().__init__(default_model=MODEL)
        self.generation = ModelPresetConfig(model=MODEL, provider='openai_codex',
                                           reasoning_effort='medium', max_tokens=4096).to_generation_settings()
        self.records = []
        self.journal = journal
        self.case = ''
        self.last_tools = []

    async def _call_codex(self, *args, **kwargs):
        if len(self.records) >= 48:
            raise RuntimeError('Part 3 call cap reached')
        row = {'index': len(self.records) + 1, 'case': self.case,
               'started_at': datetime.now(UTC).isoformat()}
        self.records.append(row)
        self.journal.write_text(json.dumps(self.records, ensure_ascii=False, indent=2))
        start = time.monotonic()
        try:
            response = await super()._call_codex(*args, **kwargs)
            self.last_tools = [t.name for t in response.tool_calls]
            row.update(usage=response.usage, finish_reason=response.finish_reason,
                       error_kind=response.error_kind,
                       proposed_tools=[{'name': t.name, 'arguments': t.arguments}
                                       for t in response.tool_calls])
            return response
        finally:
            row['wall_s'] = round(time.monotonic() - start, 3)
            self.journal.write_text(json.dumps(self.records, ensure_ascii=False, indent=2))


class AuditRegistry(ToolRegistry):
    def __init__(self, workspace):
        super().__init__()
        self.workspace = workspace
        self.events = []

    def register(self, tool):
        original = tool.execute
        async def execute(**params):
            path = self.workspace / 'a.md'
            row = {'name': tool.name, 'arguments': params,
                   'file_before': path.read_text() if path.exists() else None}
            self.events.append(row)
            try:
                result = await original(**params)
                row.update(result=str(result), is_error=bool(getattr(result, 'is_error', False)))
                return result
            except BaseException as exc:
                row['exception_type'] = type(exc).__name__
                raise
            finally:
                row['file_after'] = path.read_text() if path.exists() else None
        tool.execute = execute
        super().register(tool)


class TimeoutWrite(WriteFileTool):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.writes = 0

    async def execute(self, **kwargs):
        self.writes += 1
        await super().execute(**kwargs)
        raise TimeoutError('Synthetic response timeout AFTER actual save')


class SlowWrite(WriteFileTool):
    supports_cancellation = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = asyncio.Event()
        self.settled = False
        self.completed = False

    async def execute(self, **kwargs):
        # A visible partial effect precedes cancellation; it must not be called undone.
        path = self._resolve_write(kwargs['path'])
        path.write_text('phase=started\n')
        self.entered.set()
        try:
            await asyncio.sleep(30)
            result = await super().execute(**kwargs)
            self.completed = True
            return result
        finally:
            self.settled = True


def runner_spec(registry, workspace, prompt, metadata, **kwargs):
    return AgentRunSpec(initial_messages=[{'role': 'user', 'content': prompt}],
                        tools=registry, model=MODEL, max_iterations=8,
                        max_tool_result_chars=6000, max_tokens=4096,
                        reasoning_effort='medium', workspace=workspace,
                        llm_timeout_s=100, task_note_enabled=True,
                        task_note_metadata=metadata, **kwargs)


def skill_fixture(workspace):
    for name, description, method in [
        ('translate', 'Translate text between languages. 문장 번역 영어 한국어',
         'Translate the supplied text while preserving meaning. Do not use for table arithmetic.'),
        ('table-sum', '표 분석 숫자 합계 계산 table sum numeric total',
         'Add all supplied table values. Show the calculation and total. Do not stop at announcing skill selection.'),
    ]:
        path = workspace / 'skills' / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text(f'---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n## Method\n{method}\n')
    store = SkillStore(workspace)
    store.reindex()
    for name in ['translate', 'table-sum']:
        store.approve_draft(name)
    return store


def stats(store):
    return {name: {k: store.get_skill(name)[k] for k in
                   ['selection_count', 'usage_count', 'success_count', 'failure_count']}
            for name in ['translate', 'table-sum']}


async def main(out, selected_cases=None):
    if out.exists():
        raise ValueError('Use a fresh output directory')
    out.mkdir(parents=True)
    config_path = out / 'isolated-config.json'
    config_path.write_text('{}\n')
    set_config_path(config_path)
    # Isolate catalog discovery, within this evaluator process only.
    import nanobot.agent.tools.skill_search as search_module
    import nanobot.skill_store as store_module
    empty = out / 'empty-catalog'
    empty.mkdir()
    search_module.SYSTEM_SKILLS_DIR = empty
    store_module.BUILTIN_SKILLS_DIR = empty
    provider = MeasuredProvider(out / 'call-journal.json')
    cases = json.loads(Path(__file__).with_name('cases.json').read_text())[16:24]
    if selected_cases:
        cases = [case for case in cases if case['id'] in selected_cases]
    report = {'part': 3, 'model': MODEL, 'reasoning_effort': 'medium',
              'transport': 'existing Codex OAuth / Responses', 'call_cap': 48,
              'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
              'cases': []}
    output = out / 'results.json'
    for case in cases:
        key = case['id']
        provider.case = key
        workspace = out / key
        workspace.mkdir()
        registry = AuditRegistry(workspace)
        registry.register(ReadFileTool(workspace=workspace, restrict_to_workspace=True))
        metadata = {}
        row = {**case, 'grading': 'pending_review'}
        first = len(provider.records)
        start = time.monotonic()
        try:
            if key in {'E17', 'E18', 'E19', 'E20'}:
                writer = (TimeoutWrite if key == 'E19' else SlowWrite if key == 'E18' else WriteFileTool)(
                    workspace=workspace, restrict_to_workspace=True)
                registry.register(writer)
                row['runtime_path'] = 'AgentRunner + optional task_note'
                if key == 'E17':
                    (workspace / 'a.md').write_text('original\n')
                    injected = False
                    async def cancel_before_write(**kwargs):
                        nonlocal injected
                        if not injected and 'write_file' in provider.last_tools:
                            injected = True
                            return [{'role': 'user', 'content': '취소해줘. a.md를 수정하거나 저장하지 마.'}]
                        return []
                    spec = runner_spec(registry, workspace, 'a.md에 changed를 저장해줘.',
                                       metadata, injection_callback=cancel_before_write)
                    result = await asyncio.wait_for(AgentRunner(provider).run(spec), 180)
                    row.update(text=result.final_content, stop_reason=result.stop_reason,
                               cancellation_injected=injected)
                elif key == 'E18':
                    spec = runner_spec(registry, workspace, 'a.md에 phase=complete를 저장해줘.', metadata)
                    task = asyncio.create_task(AgentRunner(provider).run(spec))
                    entered = asyncio.create_task(writer.entered.wait())
                    await asyncio.wait({task, entered}, timeout=150, return_when=asyncio.FIRST_COMPLETED)
                    if not writer.entered.is_set():
                        task.cancel()
                        await asyncio.gather(task, entered, return_exceptions=True)
                        raise RuntimeError('Slow tool was not started')
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    entered.cancel()
                    row.update(cancelled_during_tool=True, child_settled=writer.settled,
                               tool_completed=writer.completed, cancellation_metadata=json.loads(json.dumps(metadata)))
                    # Verify and report actual remaining effects with a second live model turn.
                    followup = runner_spec(registry, workspace,
                        '방금 저장 작업을 취소했어. a.md의 실제 남은 상태를 읽어 확인하고 알려줘. 추가 저장하지 마.', {})
                    registry.unregister('write_file')
                    result = await asyncio.wait_for(AgentRunner(provider).run(followup), 180)
                    row.update(text=result.final_content, stop_reason=result.stop_reason)
                else:
                    result = await asyncio.wait_for(AgentRunner(provider).run(runner_spec(
                        registry, workspace, 'a.md에 정확히 saved content를 저장하고 결과를 알려줘.', metadata)), 180)
                    row.update(text=result.final_content, stop_reason=result.stop_reason)
                    if key == 'E19':
                        row['actual_write_count'] = writer.writes
                    else:
                        stamp = (workspace / 'a.md').stat().st_mtime_ns
                        sessions = SessionManager(workspace)
                        session = sessions.get_or_create('telegram:synthetic-delivery')
                        session.metadata.update(metadata)
                        config = Config.model_validate({
                            'agents': {'defaults': {'stepwise': {'lightweight': True}}},
                            'channels': {'websocket': {'enabled': False}, 'sendMaxRetries': 1},
                        })
                        manager = ChannelManager(config, MessageBus(), session_manager=sessions)
                        channel = type('SyntheticChannel', (), {'send': AsyncMock(side_effect=RuntimeError('synthetic send failure'))})()
                        message = OutboundMessage(channel='telegram', chat_id='synthetic-delivery',
                            content=result.final_content or '', metadata={'_task_delivery': {
                                'session_key': session.key, 'run_id': metadata['task_note']['run_id']}})
                        await manager._send_with_retry(channel, message)
                        row['after_failed_delivery'] = json.loads(json.dumps(session.metadata))
                        channel.send = AsyncMock()
                        await manager._send_with_retry(channel, message)
                        row.update(after_redelivery=session.metadata,
                                   redelivery_count=channel.send.await_count,
                                   file_mtime_unchanged=(workspace / 'a.md').stat().st_mtime_ns == stamp)
                row['task_metadata'] = metadata
            else:
                store = skill_fixture(workspace)
                search = SkillSearchTool(str(workspace))
                decision = SkillDecisionTool(str(workspace))
                context = RequestContext(channel='cli', chat_id='direct', session_key=f'cli:eval-{key}')
                search.set_context(context)
                decision.set_context(context)
                registry.register(search)
                registry.register(decision)
                loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace,
                    model=MODEL, reasoning_effort='medium', max_iterations=7,
                    tools_config=ToolsConfig(safe_mode=True), restrict_to_workspace=True,
                    mcp_servers={}, unified_session=False)
                loop.context.skills = SkillsLoader(workspace, builtin_skills_dir=empty, include_system=False)
                session = loop.sessions.get_or_create(f'cli:eval-{key}')
                prompt = {'E21': '다음 표를 분석해서 합계를 계산해줘. 사과 12, 배 18, 귤 7.',
                          'E22': '2+2는 얼마야?',
                          'E23': '번역 스킬을 잘못 골랐어. 표 분석 스킬로 정정하고 사과 12, 배 18, 귤 7의 합계를 계산해줘.',
                          'E24': '분석 스킬을 골랐다는 상태 메시지만 받았어. 사과 12, 배 18, 귤 7의 실제 분석 결과를 보여줘.'}[key]
                if key in {'E23', 'E24'}:
                    selected = 'translate' if key == 'E23' else 'table-sum'
                    await decision.execute(decision='cold', skill_name=selected,
                                           rationale='synthetic prior selection; no deliverable yet')
                    session.add_message('user', '사과 12, 배 18, 귤 7의 합계를 계산해줘.')
                    session.add_message('assistant', f'{selected} 스킬을 선택했습니다. 아직 분석 결과는 없습니다.')
                    loop.sessions.save(session)
                    row['initial_stats'] = stats(store)
                capture = SDKCaptureHook()
                response = await asyncio.wait_for(loop.process_direct(prompt,
                    session_key=f'cli:eval-{key}', tools=registry, hooks=[capture]), 180)
                row.update(text=response.content if response else None, stop_reason=capture.stop_reason,
                           runtime_path='AgentLoop; isolated 2-skill catalog; context_selection/stepwise off',
                           evaluated_input=prompt, final_stats=stats(store))
                with store._connect() as conn:
                    row['traces'] = [dict(r) for r in conn.execute(
                        'SELECT trace_id,selected_skill,phase,application_evidence,gate_result FROM traces')]
                if key == 'E23':
                    # Separately verify the fixed same-ID storage contract; native decisions use new IDs.
                    fixture = out / 'E23-same-id-contract'
                    fixture.mkdir()
                    contract = skill_fixture(fixture)
                    contract.record_trace(trace_id='same-run', selected_skill='translate', gate_result='error')
                    contract.record_trace(trace_id='same-run', selected_skill='table-sum', gate_result='ok')
                    contract.record_trace(trace_id='same-run', selected_skill='table-sum', gate_result='ok')
                    row['same_id_storage_contract_stats'] = stats(contract)
                loop.stop()
            row['status'] = 'completed' if row.get('text') else 'empty'
        except Exception as exc:
            row.update(status='error', error_type=type(exc).__name__)
        row.update(wall_s=round(time.monotonic() - start, 3), calls=provider.records[first:],
                   executed_tools=registry.events)
        if (workspace / 'a.md').exists():
            row['final_file'] = (workspace / 'a.md').read_text()
        report['cases'].append(row)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'case': key, 'status': row['status'], 'calls': len(row['calls']),
                          'wall_s': row['wall_s']}), flush=True)
        if any(c.get('finish_reason') == 'error' for c in row['calls']):
            break
    print(f'Results: {output}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cases', nargs='+', choices=[f'E{i}' for i in range(17, 25)])
    args = parser.parse_args()
    logger.remove()
    asyncio.run(main(args.out.resolve(), args.cases))
