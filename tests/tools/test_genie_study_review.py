"""Registration checks: never execute against Genie's real database."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.genie_study_review import GenieStudyReviewAdapter
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[2]
GENIE = ROOT / ".local" / "workspace-genie"


def context(workspace):
    return ToolContext(config=SimpleNamespace(safe_mode=False), workspace=str(workspace))


def test_discovered_and_registered_without_opening_database(monkeypatch):
    assert GenieStudyReviewAdapter in ToolLoader().discover()
    loader = ToolLoader(test_classes=[GenieStudyReviewAdapter])
    monkeypatch.setattr(loader, "_discover_plugins", lambda: {})
    registry = ToolRegistry()
    assert loader.load(context(GENIE), registry) == ["genie_study_review"]
    tool = registry.get("genie_study_review")
    assert tool.db_path == GENIE / "memory" / "genie_study_review.sqlite"
    assert tool.parameters["properties"]["action"]["enum"] == ["register", "due", "result"]
    assert tool.user_id == "8580974491"


@pytest.mark.parametrize("name", ["workspace-testy", "workspace-heart", "workspace", "workspace-genie"])
def test_not_registered_in_other_workspaces(tmp_path, monkeypatch, name):
    loader = ToolLoader(test_classes=[GenieStudyReviewAdapter])
    monkeypatch.setattr(loader, "_discover_plugins", lambda: {})
    registry = ToolRegistry()
    assert loader.load(context(tmp_path / name), registry) == []
    assert not registry.has("genie_study_review")
    with pytest.raises(ValueError, match="only in the Genie"):
        GenieStudyReviewAdapter.create(context(tmp_path / name))


@pytest.mark.asyncio
async def test_adapter_delegates_to_existing_implementation_with_temporary_db(tmp_path):
    tool = GenieStudyReviewAdapter.create(context(GENIE))
    tool.db_path = tmp_path / "review.sqlite"
    # Unsupported action still exercises service construction/closing, safely on temp DB.
    assert '"status": "rejected"' in await tool.execute(action="unsupported")
    assert tool.db_path.is_file()
