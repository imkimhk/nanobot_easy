"""Follow-up reuse of earlier reads: behaviour that must hold before AND after the opt-in change.

Offline: scripted provider, real ReadFileTool on isolated tmp files. No model calls.
"""
import json
import os
import re

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.task_note import TaskNote, TaskUpdate, active_task_note
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMResponse, ToolCallRequest

FOLLOWUP = ("방금 분석한 두 파일의 요약을 더 짧게 정리해줘. 파일이 바뀌지 않았다면 기존 읽기 증거를 "
            "활용하고, 불필요하게 전체를 다시 읽지 마.")
ANCHOR = "".join(f"anchor line {i}\n" for i in range(1, 9))
LOG = "".join(f"REF-{i:02} status=ok item=x{i}\n" for i in range(1, 81))
SAMPLE = "".join(f"sample {i}\n" for i in range(1, 81))
ANSWER = "anchor.md 8줄, reference.log 80줄 요약입니다."
BOTH = [{"path": "anchor.md", "unit": "lines", "start": 1, "end": 8},
        {"path": "reference.log", "unit": "lines", "start": 1, "end": 80}]


async def _read(note, tmp_path, name, **kwargs):
    tool = ReadFileTool(workspace=tmp_path)
    token = active_task_note.set(note)
    try:
        result = await tool.execute(path=name, force=True, **kwargs)
    finally:
        active_task_note.reset(token)
    note.observe("read_file", {"path": name}, result, tool, "ok")
    return result


async def prior_session(tmp_path):
    """Earlier turns shaped like the recorded Q5: sample.md 41-80, anchor.md full, log x80."""
    (tmp_path / "sample.md").write_text(SAMPLE)
    (tmp_path / "anchor.md").write_text(ANCHOR)
    (tmp_path / "reference.log").write_text(LOG)
    metadata = {}
    first = TaskNote([{"role": "user", "content": "sample.md 후반부 분석"}], tmp_path, metadata)
    for offset in range(41, 81):
        await _read(first, tmp_path, "sample.md", offset=offset, limit=1)
    second = TaskNote([{"role": "user", "content": "anchor.md와 reference.log 분석"}], tmp_path, metadata)
    await _read(second, tmp_path, "anchor.md")
    for offset in range(1, 81):
        await _read(second, tmp_path, "reference.log", offset=offset, limit=1)
    return metadata


def payload(note):
    return json.loads(note.guidance()["content"].split("\n")[-1])


def names(entries):
    return {e["target"].rsplit("/", 1)[-1] for e in entries}


def by_name(entries):
    return {e["target"].rsplit("/", 1)[-1]: e for e in entries}


def completed(note, targets, answer=ANSWER):
    return TaskUpdate.model_validate({
        "request_id": note.request_id, "intent": "continue", "goal": "shorter summary",
        "targets": [{"selection_reason": "previously analysed", **t} for t in targets],
        "source": "files", "analysis_complete": True, "status": "completed",
        "explanation": "reused verified reads", "final_answer": answer})


def same_size_same_mtime(fp):
    stat = fp.stat()
    fp.write_text(ANCHOR.replace("line 1\n", "LINE 1\n"))
    assert fp.stat().st_size == stat.st_size
    os.utime(fp, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def replaced_by_rename(fp):
    stat = fp.stat()
    tmp = fp.with_name("anchor.tmp")
    tmp.write_text(ANCHOR.replace("line 2\n", "LINE 2\n"))
    os.utime(tmp, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(tmp, fp)


def touched(fp):
    stat = fp.stat()
    os.utime(fp, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


MUTATIONS = {"same_size_same_mtime": same_size_same_mtime, "rename_replace": replaced_by_rename,
             "deleted": lambda fp: fp.unlink(), "metadata_only_touch": touched,
             "appended": lambda fp: fp.write_text(ANCHOR + "added\n")}


class Script:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.calls = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        step = next(self.steps)
        return step(kwargs) if callable(step) else step


def guidance_of(kwargs):
    return next(m["content"] for m in reversed(kwargs["messages"])
                if isinstance(m.get("content"), str) and "current request_id=" in m["content"])


def control(targets, *, status="completed", analysis_complete=True, answer=ANSWER):
    def response(kwargs):
        request_id = re.search(r"current request_id=([a-f0-9]+)", guidance_of(kwargs))[1]
        args = {"request_id": request_id, "intent": "continue", "goal": "shorter summary",
                "targets": [{"selection_reason": "previously analysed", **t} for t in targets],
                "source": "files", "analysis_complete": analysis_complete, "status": status,
                "explanation": "summary", "final_answer": answer if status == "completed" else "",
                "planned_steps": [] if status == "completed" else ["두 파일의 첫 줄을 확인합니다."]}
        return LLMResponse(content="", tool_calls=[ToolCallRequest(
            id="control-" + str(len(kwargs["messages"])), name="update_task", arguments=args)])
    return response


def reads(*paths):
    return LLMResponse(content="", tool_calls=[
        ToolCallRequest(id=f"read-{i}", name="read_file", arguments={"path": p, "offset": 1, "limit": 1})
        for i, p in enumerate(paths)])


def write(path="anchor.md"):
    args = {"path": path, "content": "changed", "intent_summary": "save requested summary",
            "target": path, "scope": "once", "reversible": True}
    return LLMResponse(content="", tool_calls=[ToolCallRequest(id="w", name="write_file", arguments=args)])


def write_status(result):
    return next(e["status"] for e in result.tool_events if e["name"] == "write_file")


async def run(tmp_path, metadata, steps, request=FOLLOWUP, **spec_kwargs):
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    tools.register(WriteFileTool(workspace=tmp_path))
    provider = Script(steps)
    spec = AgentRunSpec(initial_messages=[{"role": "user", "content": request}], tools=tools,
                        model="test", max_iterations=12, max_tool_result_chars=8000,
                        workspace=tmp_path, task_note_enabled=True, task_note_metadata=metadata,
                        **spec_kwargs)
    result = await AgentRunner(provider).run(spec)
    return result, provider

# --- the recorded condition (default, flag off) ---------------------------------------------

async def test_default_guidance_hides_requested_files_and_has_no_current_marker(tmp_path):
    """Q6 condition: all 121 earlier reads are valid, but the model sees only log lines 61-80."""
    metadata = await prior_session(tmp_path)
    note = TaskNote([{"role": "user", "content": FOLLOWUP}], tmp_path, metadata)
    assert len(note.evidence) == 121
    shown = payload(note)
    assert names(shown["cached_reads"]) == {"reference.log"}
    assert sorted(e["start"] for e in shown["cached_reads"]) == list(range(61, 81))
    assert "verified_reads" not in note.guidance()["content"]


# --- unchanged files: reuse is accepted without any new read -------------------------------

async def test_unchanged_files_are_accepted_from_earlier_evidence(tmp_path):
    metadata = await prior_session(tmp_path)
    note = TaskNote([{"role": "user", "content": FOLLOWUP}], tmp_path, metadata)
    note.accept(completed(note, BOTH))
    assert note.defects() == []


# --- modified / replaced / deleted: never accepted as-is -----------------------------------

@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
async def test_changed_file_evidence_is_not_accepted(tmp_path, mutation):
    metadata = await prior_session(tmp_path)
    MUTATIONS[mutation](tmp_path / "anchor.md")
    note = TaskNote([{"role": "user", "content": FOLLOWUP}], tmp_path, metadata)
    note.accept(completed(note, BOTH))
    assert "coverage_unconfirmed:anchor.md" in note.defects()
    assert "coverage_unconfirmed:reference.log" not in note.defects()


async def test_file_changed_after_request_start_is_rejected_at_acceptance(tmp_path):
    metadata = await prior_session(tmp_path)
    note = TaskNote([{"role": "user", "content": FOLLOWUP}], tmp_path, metadata)
    (tmp_path / "anchor.md").write_text(ANCHOR + "added\n")
    note.accept(completed(note, BOTH))
    assert "coverage_unconfirmed:anchor.md" in note.defects()


async def test_symlink_retarget_does_not_reuse_previous_target(tmp_path):
    (tmp_path / "one.md").write_text("one\n")
    (tmp_path / "two.md").write_text("two\n")
    (tmp_path / "link.md").symlink_to(tmp_path / "one.md")
    metadata = {}
    first = TaskNote([{"role": "user", "content": "link.md 분석"}], tmp_path, metadata)
    await _read(first, tmp_path, "link.md")
    (tmp_path / "link.md").unlink()
    (tmp_path / "link.md").symlink_to(tmp_path / "two.md")
    note = TaskNote([{"role": "user", "content": "link.md 요약 계속"}], tmp_path, metadata)
    note.accept(completed(note, [{"path": "link.md", "unit": "all"}], answer="요약입니다."))
    assert "coverage_unconfirmed:link.md" in note.defects()


# --- partial reads are never full reads ----------------------------------------------------

async def test_partial_range_cannot_prove_whole_file(tmp_path):
    metadata = await prior_session(tmp_path)
    note = TaskNote([{"role": "user", "content": "sample.md 요약 계속"}], tmp_path, metadata)
    note.accept(completed(note, [{"path": "sample.md", "unit": "all"}], answer="요약입니다."))
    assert "coverage_unconfirmed:sample.md" in note.defects()
    note.accept(completed(note, [{"path": "sample.md", "unit": "lines", "start": 41, "end": 80}],
                          answer="41-80행 요약입니다."))
    assert note.defects() == []


# --- runner replays of the recorded trajectories (flag off) --------------------------------

async def test_reuse_trajectory_completes_in_one_call_without_reads(tmp_path):
    """Recorded passing run: no code path forces update_task/activate_tools/read_file."""
    metadata = await prior_session(tmp_path)
    result, provider = await run(tmp_path, metadata, [control(BOTH)])
    assert result.stop_reason == "completed" and result.final_content == ANSWER
    assert len(provider.calls) == 1 and result.tools_used == ["update_task"]


async def test_verification_trajectory_is_allowed_and_recorded(tmp_path):
    """Recorded failing run: the extra check is never blocked; it is the model's choice."""
    metadata = await prior_session(tmp_path)
    steps = [control([{"path": "anchor.md", "unit": "lines", "start": 1, "end": 1},
                      {"path": "reference.log", "unit": "lines", "start": 1, "end": 1}],
                     status="active", analysis_complete=False),
             reads("anchor.md", "reference.log"), control(BOTH)]
    result, provider = await run(tmp_path, metadata, steps)
    assert result.stop_reason == "completed" and result.final_content == ANSWER
    assert len(provider.calls) == 3 and result.tools_used.count("read_file") == 2
    current = metadata["task_note"]["request_id"]
    fresh = [e for e in metadata["task_note"]["evidence"] if e["request_id"] == current]
    assert names(fresh) == {"anchor.md", "reference.log"}


async def test_changed_file_forces_a_real_read_before_completion(tmp_path):
    metadata = await prior_session(tmp_path)
    (tmp_path / "anchor.md").write_text(ANCHOR.replace("line 3", "LINE 3"))
    steps = [control(BOTH), LLMResponse(content="", tool_calls=[ToolCallRequest(
        id="full", name="read_file", arguments={"path": "anchor.md"})]), control(BOTH)]
    result, provider = await run(tmp_path, metadata, steps)
    assert result.stop_reason == "completed"
    assert result.tools_used.count("read_file") == 1 and len(provider.calls) == 3


async def test_user_requested_recheck_reads_again(tmp_path):
    metadata = await prior_session(tmp_path)
    steps = [reads("anchor.md"), control([{"path": "anchor.md", "unit": "lines", "start": 1, "end": 8}],
                                         answer="anchor.md를 다시 확인했습니다. 8줄입니다.")]
    result, _ = await run(tmp_path, metadata, steps,
                          request="anchor.md가 바뀌었는지 실제로 다시 읽어서 확인해줘.")
    assert result.stop_reason == "completed" and result.tools_used.count("read_file") == 1


async def test_do_not_modify_request_blocks_writes(tmp_path):
    metadata = await prior_session(tmp_path)
    before = (tmp_path / "anchor.md").read_bytes()
    steps = [control(BOTH, status="active", analysis_complete=False), write(), control(BOTH)]
    result, _ = await run(tmp_path, metadata, steps,
                          request="anchor.md와 reference.log 요약을 더 짧게. 파일은 수정하지 마.")
    assert (tmp_path / "anchor.md").read_bytes() == before
    assert write_status(result) == "blocked"


async def test_ordinary_requested_write_still_works(tmp_path):
    metadata = await prior_session(tmp_path)
    steps = [control([{"path": "out.md", "action": "write"}], status="active", analysis_complete=False),
             write("out.md"),
             control([{"path": "out.md", "action": "write"}], answer="out.md에 저장했습니다.")]
    result, _ = await run(tmp_path, metadata, steps, request="요약을 out.md에 저장해줘.")
    assert (tmp_path / "out.md").read_text() == "changed"
    assert result.stop_reason == "completed" and write_status(result) == "ok"
