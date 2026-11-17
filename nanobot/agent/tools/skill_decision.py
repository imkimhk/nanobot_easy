"""Trace the agent's skill routing decision for a turn."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext, current_request_context
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema
from nanobot.skill_store import SkillStore


def _digest_text(text: str, skill_name: str | None = None) -> str:
    raw = f"{skill_name or ''}\n{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


@tool_parameters(
    tool_parameters_schema(
        decision=StringSchema(
            "Routing decision. Use 'hot' for a preloaded Active Skill, 'cold' for a skill_search "
            "candidate, or 'none' when no skill card fits.",
            enum=["hot", "cold", "none"],
        ),
        skill_name=StringSchema(
            "Selected skill name. Leave empty when decision is 'none'.",
            nullable=True,
        ),
        rationale=StringSchema(
            "Brief reason based on the user's actual instruction and the selected or rejected skill cards.",
            max_length=800,
        ),
        intent_summary=StringSchema(
            "Optional one-sentence summary of the user's intended outcome, target, and key constraints "
            "at the moment of this routing decision. Keep it under 300 characters and do not include "
            "hidden reasoning.",
            max_length=300,
            nullable=True,
        ),
        wave_no=IntegerSchema(
            description=(
                "Composite-task wave number for this routing decision. Set this for subtasks "
                "inside composite-task so traces can prove wave ordering."
            ),
            minimum=1,
            nullable=True,
        ),
        phase=StringSchema(
            "selected (default) records a choice only. applied records actual Method use with "
            "application_evidence; use it alongside the actual answer or after execution, not a plan.",
            enum=["selected", "applied"], nullable=True,
        ),
        trace_id=StringSchema(
            "Returned selection trace_id to promote or replay the SAME skill/application. "
            "Omit for a different selection or independent task; do not merge different tasks.",
            nullable=True,
        ),
        application_evidence=StringSchema(
            "Required for applied: concise actual result, artifact or tool-result references "
            "showing use of the Method. A skill choice or plan alone is not application. "
            "This records use, not a verified success judgment.", max_length=1000, nullable=True,
        ),
        required=["decision", "rationale"],
    )
)
class SkillDecisionTool(Tool, ContextAware):
    """Observe skill choices and applications without declaring verified success."""

    def __init__(self, workspace: str):
        self.workspace = Path(workspace)
        self._session_key: str | None = None

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    @property
    def name(self) -> str:
        return "skill_decision"

    @property
    def description(self) -> str:
        return (
            "Record the final skill routing decision after reading Active Skill cards or "
            "skill_search candidate cards. This is an observability tool, not a router. "
            "Call it before answering when you apply an Active Skill, apply a skill_search "
            "candidate, or intentionally choose no skill after skill_search. "
            "Use phase=selected for choices; phase=applied with application_evidence for actual use. "
            "Reuse the returned trace_id to promote that selection without counting it twice."
        )

    @property
    def read_only(self) -> bool:
        return False

    @property
    def observability(self) -> bool:
        return True

    def set_context(self, ctx: RequestContext) -> None:
        self._session_key = ctx.session_key or f"{ctx.channel}:{ctx.chat_id}"

    async def execute(
        self,
        decision: str,
        rationale: str,
        skill_name: str | None = None,
        intent_summary: str | None = None,
        wave_no: int | None = None,
        phase: str | None = None,
        trace_id: str | None = None,
        application_evidence: str | None = None,
        **_kwargs: Any,
    ) -> str:
        started = time.perf_counter()
        normalized = (decision or "").strip().lower()
        if normalized not in {"hot", "cold", "none"}:
            return json.dumps({"ok": False, "error": "decision must be hot, cold, or none"}, ensure_ascii=False)
        selected = (skill_name or "").strip() or None
        if normalized == "none":
            selected = None
        elif not selected:
            return json.dumps({"ok": False, "error": "skill_name is required unless decision is none"}, ensure_ascii=False)
        try:
            normalized_wave_no = int(wave_no) if wave_no is not None else None
        except (TypeError, ValueError):
            normalized_wave_no = None
        if normalized_wave_no is not None and normalized_wave_no < 1:
            normalized_wave_no = None

        phase = phase or "selected"
        if phase not in {"selected", "applied"} or (phase == "applied" and selected is None):
            return ToolResult.error("phase must be selected, or applied with a selected skill")
        if trace_id and not trace_id.startswith("skill_decision:"):
            return ToolResult.error("trace_id must be a returned skill_decision selection ID")
        recorded_id = trace_id or f"skill_decision:{uuid4().hex}"
        ctx = current_request_context()
        session_key = (ctx.session_key or f"{ctx.channel}:{ctx.chat_id}") if ctx else self._session_key
        try:
            phase = SkillStore(self.workspace).record_trace(
                trace_id=recorded_id,
                session_key=session_key,
                query_digest=_digest_text(rationale or "", selected),
                intent_summary=(intent_summary or "").strip() or None,
                candidates=[{"name": selected, "match_grade": normalized, "score": None}] if selected else [],
                selected_skill=selected,
                selection_reason=normalized,
                executed_by="main",
                wave_no=normalized_wave_no,
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                notes=(rationale or "").strip() or None,
                phase=phase,
                application_evidence=application_evidence,
                existing_selection=bool(trace_id),
            )
        except ValueError as exc:
            return ToolResult.error(str(exc))
        return json.dumps(
            {"ok": True, "decision": normalized, "selected_skill": selected,
             "wave_no": normalized_wave_no, "trace_id": recorded_id, "phase": phase},
            ensure_ascii=False,
        )
