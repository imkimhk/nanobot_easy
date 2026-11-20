"""IV3 regressions: model-copy visibility and damaged saved read spans (offline)."""

import copy
import json

import pytest

from nanobot.agent.task_note import TaskNote
from tests.agent.test_task_note_verified_reads import ANCHOR, history, prior_session


async def test_final_guidance_clears_snipped_lines_without_new_file_reads(tmp_path, monkeypatch):
    metadata = await prior_session(tmp_path)
    read = "\n".join(f"{i}| {line}" for i, line in enumerate(ANCHOR.splitlines(), 1))
    old = history(metadata, read_results=[("anchor.md", 1, 8, read)])
    note = TaskNote(old + [{"role": "user", "content": "anchor.md 요약해줘"}], tmp_path,
                    metadata, verified_reads=True)
    guidance = note.guidance()
    payload = json.loads(guidance["content"].split("\n")[-1])
    entry = next(e for e in payload["verified_reads"] if e["target"].endswith("anchor.md"))
    assert entry["text_in_context"] == [[1, 8]]
    messages = [{"role": "user", "content": "anchor.md 요약해줘"}, guidance]
    saved = copy.deepcopy(messages)

    def forbidden(*args, **kwargs):
        pytest.fail("finalization must not reopen source files")

    monkeypatch.setattr(type(tmp_path), "read_text", forbidden)
    updated = note.refresh_visible_reads(messages)
    payload = json.loads(updated[-1]["content"].split("\n")[-1])
    assert all(e["text_in_context"] == [] for e in payload["verified_reads"])
    assert messages == saved


@pytest.mark.parametrize("field,value", [("start", None), ("start", "1"), ("end", None),
                                        ("end", -1), ("unit", []), ("request_id", []),
                                        ("total", "80")])
async def test_bad_persisted_span_cannot_abort_guidance(tmp_path, field, value):
    metadata = await prior_session(tmp_path)
    messages = history(metadata)
    for entry in metadata["task_note"]["evidence"]:
        entry[field] = value
    note = TaskNote(messages, tmp_path, metadata, verified_reads=True)
    assert json.loads(note.guidance()["content"].split("\n")[-1])["verified_reads"] == []
