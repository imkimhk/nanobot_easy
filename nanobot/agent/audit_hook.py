"""Observe existing runner events; never change calls, permissions, or prompts."""

import asyncio
import uuid

from loguru import logger

from nanobot.agent.hook import AgentHook


class ActionAuditHook(AgentHook):
    def __init__(self, store, registry, session_key=None, source='agent'):
        super().__init__()
        self.store = store
        self.registry = registry
        self.session_key = session_key
        self.source = source
        self.run_id = uuid.uuid4().hex
        self.pending = {}
        self._wait_warned = False

    async def _record(self, call, status):
        registered = self.registry.get(call.name)
        tool = registered.name if registered is not None else 'unregistered_tool'
        try:
            await asyncio.wait_for(asyncio.to_thread(
                self.store.record, kind='tool', status=status, tool=tool, source=self.source,
                session_key=self.session_key, run_id=self.run_id, call_id=call.id,
            ), timeout=0.2)
        except TimeoutError:
            if not self._wait_warned:
                logger.warning('Action audit wait exceeded 200ms; tool execution remains unchanged.')
                self._wait_warned = True

    async def before_execute_tools(self, context):
        for call in context.tool_calls:
            self.pending[call.id] = call
            await self._record(call, 'attempted')

    async def after_iteration(self, context):
        for call, event in zip(context.tool_calls, context.tool_events):
            if call.id not in self.pending:
                continue
            status = event.get('status')
            if event.get('execution_unknown'):
                status = 'unknown'
            elif event.get('executed') is False and status == 'error':
                status = 'blocked'
            elif status == 'ok':
                status = 'tool_ok'
            if status not in {'tool_ok', 'error', 'blocked', 'superseded', 'unknown'}:
                status = 'unknown'
            await self._record(call, status)
            self.pending.pop(call.id, None)

    async def on_finally(self, context):
        # Interrupted attempts may already have changed state. Never label them
        # as successfully cancelled, rolled back, or safely not executed.
        for call in list(self.pending.values()):
            await self._record(call, 'unknown')
        self.pending.clear()
