"""Opt-in summary of earlier reads that still match the current file (flag off by default).

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
from nanobot.config.schema import StepwiseConfig
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


def history(metadata, *, read_results=()):
    """Replayed tool traffic of the earlier requests, as the loop would pass it (same conversation).

    read_results: (path, offset, limit, text) read_file results still visible in the history.
    """
    messages = []
    for i, run in enumerate((metadata.get("task_runs") or {}).values()):
        args = json.dumps({"request_id": run["request_id"], "status": "completed"})
        messages += [{"role": "user", "content": run["request"]},
                     {"role": "assistant", "content": "", "tool_calls": [
                         {"id": f"u{i}", "type": "function", "function": {"name": "update_task", "arguments": args}}]},
                     {"role": "tool", "tool_call_id": f"u{i}", "name": "update_task", "content": args}]
    for i, (path, offset, limit, text) in enumerate(read_results):
        args = json.dumps({"path": path, "offset": offset, "limit": limit})
        messages += [{"role": "assistant", "content": "", "tool_calls": [
                         {"id": f"r{i}", "type": "function", "function": {"name": "read_file", "arguments": args}}]},
                     {"role": "tool", "tool_call_id": f"r{i}", "name": "read_file", "content": text}]
    return messages


def follow_note(tmp_path, metadata, request=FOLLOWUP, **kwargs):
    return TaskNote([*history(metadata, **kwargs), {"role": "user", "content": request}], tmp_path, metadata,
                    verified_reads=True)


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
    spec = AgentRunSpec(initial_messages=[*history(metadata), {"role": "user", "content": request}], tools=tools,
                        model="test", max_iterations=12, max_tool_result_chars=8000,
                        workspace=tmp_path, task_note_enabled=True, task_note_metadata=metadata,
                        **spec_kwargs)
    result = await AgentRunner(provider).run(spec)
    return result, provider

def test_flag_defaults_off():
    assert StepwiseConfig().verified_read_summary is False
    assert StepwiseConfig.model_validate({"verifiedReadSummary": True}).verified_read_summary
    assert AgentRunSpec(initial_messages=[], tools=ToolRegistry(), model="m", max_iterations=1,
                        max_tool_result_chars=1).task_note_verified_reads is False


async def test_flag_off_guidance_is_unchanged_by_the_option(tmp_path):
    metadata = await prior_session(tmp_path)
    off = TaskNote([{"role": "user", "content": FOLLOWUP}], tmp_path, metadata)
    explicit = TaskNote([{"role": "user", "content": FOLLOWUP}], tmp_path, metadata,
                        verified_reads=False)
    def strip(n):
        return n.guidance()["content"].replace(n.request_id, "RID")
    assert strip(off) == strip(explicit)


async def test_summary_shows_every_requested_file_with_current_match(tmp_path):
    metadata = await prior_session(tmp_path)
    note = follow_note(tmp_path, metadata)
    shown = payload(note)
    assert len(shown["cached_reads"]) == 20   # the existing bounded field is untouched
    summary = by_name(shown["verified_reads"])
    assert summary["anchor.md"]["verified_unchanged"] == [[1, 8]] and summary["anchor.md"]["whole_file_verified"]
    assert summary["reference.log"]["verified_unchanged"] == [[1, 80]]
    assert summary["reference.log"]["whole_file_verified"]
    # No read result is in this history, so no text is claimed to be available.
    assert all(e["text_in_context"] == [] for e in summary.values())
    text = note.guidance()["content"]
    assert "Do not re-read only to check whether the file changed" in text
    assert "not that their text is available to you" in text
    assert "Read again when you need text outside text_in_context" in text


async def test_summary_is_bounded_by_files_not_by_reads(tmp_path):
    metadata = {}
    note = TaskNote([{"role": "user", "content": "여러 파일 분석"}], tmp_path, metadata)
    for i in range(30):
        (tmp_path / f"f{i}.md").write_text("a\nb\n")
        for offset in (1, 2):
            await _read(note, tmp_path, f"f{i}.md", offset=offset, limit=1)
    follow = follow_note(tmp_path, metadata, "요약 계속")
    summary = payload(follow)["verified_reads"]
    assert len(summary) == 20 and all(e["verified_unchanged"] == [[1, 2]] for e in summary)


async def test_current_request_reads_are_not_relabelled_as_earlier_evidence(tmp_path):
    (tmp_path / "a.md").write_text("x\n")
    note = TaskNote([{"role": "user", "content": "a.md 분석"}], tmp_path, {}, verified_reads=True)
    await _read(note, tmp_path, "a.md")
    assert payload(note)["verified_reads"] == []


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
async def test_changed_file_is_absent_from_summary_and_still_rejected(tmp_path, mutation):
    metadata = await prior_session(tmp_path)
    MUTATIONS[mutation](tmp_path / "anchor.md")
    note = follow_note(tmp_path, metadata)
    summary = by_name(payload(note)["verified_reads"])
    assert "anchor.md" not in summary and "reference.log" in summary
    note.accept(completed(note, BOTH))
    assert "coverage_unconfirmed:anchor.md" in note.defects()


async def test_change_after_guidance_is_rejected_at_acceptance(tmp_path):
    metadata = await prior_session(tmp_path)
    note = follow_note(tmp_path, metadata)
    assert "anchor.md" in names(payload(note)["verified_reads"])
    (tmp_path / "anchor.md").write_text(ANCHOR + "added\n")
    note.accept(completed(note, BOTH))
    assert "coverage_unconfirmed:anchor.md" in note.defects()
    assert "anchor.md" not in names(payload(note)["verified_reads"])


async def test_symlink_retarget_is_reported_by_real_target_only(tmp_path):
    (tmp_path / "one.md").write_text("one\n")
    (tmp_path / "two.md").write_text("two\n")
    (tmp_path / "link.md").symlink_to(tmp_path / "one.md")
    metadata = {}
    await _read(TaskNote([{"role": "user", "content": "link.md 분석"}], tmp_path, metadata),
                tmp_path, "link.md")
    (tmp_path / "link.md").unlink()
    (tmp_path / "link.md").symlink_to(tmp_path / "two.md")
    note = follow_note(tmp_path, metadata, "link.md 요약 계속")
    assert names(payload(note)["verified_reads"]) == {"one.md"}
    note.accept(completed(note, [{"path": "link.md", "unit": "all"}], answer="요약입니다."))
    assert "coverage_unconfirmed:link.md" in note.defects()


async def test_partial_range_is_marked_incomplete(tmp_path):
    metadata = await prior_session(tmp_path)
    note = follow_note(tmp_path, metadata, "sample.md 요약 계속")
    sample = by_name(payload(note)["verified_reads"])["sample.md"]
    assert sample["verified_unchanged"] == [[41, 80]] and sample["total"] == 80
    assert not sample["whole_file_verified"]
    note.accept(completed(note, [{"path": "sample.md", "unit": "all"}], answer="요약입니다."))
    assert "coverage_unconfirmed:sample.md" in note.defects()


async def test_large_file_without_content_hash_is_never_summarised(tmp_path):
    metadata = {"task_note": {"evidence": [{
        "target": str(tmp_path / "big.log"), "unit": "lines", "start": 1, "end": 10, "total": 10,
        "extraction_ok": True, "request_id": "old", "version": {"size": 2_000_000, "mtime_ns": 1}}]}}
    note = TaskNote([{"role": "user", "content": "big.log 요약"}], tmp_path, metadata, verified_reads=True)
    assert payload(note)["verified_reads"] == []


# --- runner: the flag reaches the guidance; every normal path still works ------------------

async def test_runner_passes_flag_and_reuse_completes_in_one_call(tmp_path):
    metadata = await prior_session(tmp_path)
    result, provider = await run(tmp_path, metadata, [control(BOTH)], task_note_verified_reads=True)
    assert result.stop_reason == "completed" and result.final_content == ANSWER
    assert len(provider.calls) == 1 and result.tools_used == ["update_task"]
    assert '"verified_reads"' in guidance_of(provider.calls[0])


async def test_runner_default_sends_no_summary(tmp_path):
    metadata = await prior_session(tmp_path)
    _, provider = await run(tmp_path, metadata, [control(BOTH)])
    assert "verified_reads" not in guidance_of(provider.calls[0])


async def test_verification_reads_are_not_blocked_with_flag(tmp_path):
    metadata = await prior_session(tmp_path)
    steps = [control([{"path": "anchor.md", "unit": "lines", "start": 1, "end": 1},
                      {"path": "reference.log", "unit": "lines", "start": 1, "end": 1}],
                     status="active", analysis_complete=False),
             reads("anchor.md", "reference.log"), control(BOTH)]
    result, provider = await run(tmp_path, metadata, steps, task_note_verified_reads=True)
    assert result.stop_reason == "completed" and result.tools_used.count("read_file") == 2


async def test_user_requested_recheck_reads_again_with_flag(tmp_path):
    metadata = await prior_session(tmp_path)
    steps = [reads("anchor.md"), control([{"path": "anchor.md", "unit": "lines", "start": 1, "end": 8}],
                                         answer="anchor.md를 다시 확인했습니다. 8줄입니다.")]
    result, _ = await run(tmp_path, metadata, steps, task_note_verified_reads=True,
                          request="anchor.md가 바뀌었는지 실제로 다시 읽어서 확인해줘.")
    assert result.stop_reason == "completed" and result.tools_used.count("read_file") == 1


async def test_do_not_modify_request_blocks_writes_with_flag(tmp_path):
    metadata = await prior_session(tmp_path)
    before = (tmp_path / "anchor.md").read_bytes()
    steps = [control(BOTH, status="active", analysis_complete=False), write(), control(BOTH)]
    result, _ = await run(tmp_path, metadata, steps, task_note_verified_reads=True,
                          request="anchor.md와 reference.log 요약을 더 짧게. 파일은 수정하지 마.")
    assert (tmp_path / "anchor.md").read_bytes() == before
    assert write_status(result) == "blocked"


async def test_ordinary_requested_write_still_works_with_flag(tmp_path):
    metadata = await prior_session(tmp_path)
    steps = [control([{"path": "out.md", "action": "write"}], status="active", analysis_complete=False),
             write("out.md"), control([{"path": "out.md", "action": "write"}], answer="out.md에 저장했습니다.")]
    result, _ = await run(tmp_path, metadata, steps, task_note_verified_reads=True,
                          request="요약을 out.md에 저장해줘.")
    assert (tmp_path / "out.md").read_text() == "changed"
    assert result.stop_reason == "completed" and write_status(result) == "ok"


# --- F-C1-01: file identity is not content availability; only this conversation --------------

def numbered(text, first, last):
    lines = text.splitlines()
    return "\n".join(f"{n}| {lines[n - 1]}" for n in range(first, last + 1))


async def test_text_in_context_lists_only_lines_visible_in_replayed_results(tmp_path):
    metadata = await prior_session(tmp_path)
    visible = [("reference.log", n, 1, numbered(LOG, n, n) + f"\n\n(Showing lines {n}-{n} of 80.)")
               for n in range(41, 81)]
    summary = by_name(payload(follow_note(tmp_path, metadata, read_results=visible))["verified_reads"])
    assert summary["reference.log"]["verified_unchanged"] == [[1, 80]]
    assert summary["reference.log"]["text_in_context"] == [[41, 80]]
    assert summary["anchor.md"]["verified_unchanged"] == [[1, 8]]
    assert summary["anchor.md"]["text_in_context"] == []          # identity known, text not shown


async def test_text_that_differs_from_the_current_file_is_not_counted(tmp_path):
    metadata = await prior_session(tmp_path)
    tampered = numbered(LOG, 1, 3).replace("item=x2", "item=OTHER")
    truncated = numbered(ANCHOR, 1, 8)[:30] + "\n... (truncated)"
    note = follow_note(tmp_path, metadata, read_results=[("reference.log", 1, 3, tampered),
                                                         ("anchor.md", 1, 8, truncated)])
    summary = by_name(payload(note)["verified_reads"])
    assert summary["reference.log"]["text_in_context"] == [[1, 1], [3, 3]]
    assert summary["anchor.md"]["text_in_context"] == [[1, 1]]


async def test_new_conversation_lists_no_earlier_reads_and_acceptance_is_unchanged(tmp_path):
    metadata = await prior_session(tmp_path)
    # After /new the replay holds only the current request; the stored task note survives
    # (baseline behaviour, not changed here) but its reads are no longer listed.
    on = TaskNote([{"role": "user", "content": FOLLOWUP}], tmp_path, metadata, verified_reads=True)
    assert payload(on)["verified_reads"] == []
    off = TaskNote([{"role": "user", "content": FOLLOWUP}], tmp_path, dict(metadata))
    on.accept(completed(on, BOTH))
    off.accept(completed(off, BOTH))
    assert on.defects() == off.defects()                          # acceptance rule untouched


async def test_requests_trimmed_out_of_the_replay_are_not_listed(tmp_path):
    metadata = await prior_session(tmp_path)
    runs = list(metadata["task_runs"].values())
    kept = {"task_runs": {"r": runs[-1]}}                         # only the anchor/log request replayed
    note = TaskNote([*history(kept), {"role": "user", "content": FOLLOWUP}], tmp_path, metadata,
                    verified_reads=True)
    assert names(payload(note)["verified_reads"]) == {"anchor.md", "reference.log"}
