"""Optional local adapter discovery; use a synthetic package and temporary DB only."""
from types import SimpleNamespace

import pytest

from nanobot.agent.tools import genie_study_review as adapter
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.genie_study_review import GenieStudyReviewAdapter
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry


def context(workspace):
    return ToolContext(config=SimpleNamespace(safe_mode=False), workspace=str(workspace))


@pytest.fixture
def local_implementation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace-genie"
    package = workspace / "study_review"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "tool.py").write_text('''import sqlite3
from nanobot.agent.tools.base import Tool
class GenieStudyReviewTool(Tool):
    name = "genie_study_review"
    description = "Synthetic local review tool"
    parameters = {"type": "object", "properties": {"action": {"enum": ["register", "due", "result"]}}}
    def __init__(self, db_path):
        self.db_path = db_path
    async def execute(self, **kwargs):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS review (id INTEGER)")
        return '{"status": "rejected"}'
''')
    monkeypatch.setattr(adapter, "_GENIE_WORKSPACE", workspace)
    monkeypatch.setattr(adapter, "_PACKAGE_NAME", "_genie_review_test_" + tmp_path.name.replace("-", "_"))
    return workspace


def test_discovered_and_registered_without_opening_database(local_implementation, monkeypatch):
    assert GenieStudyReviewAdapter in ToolLoader().discover()
    loader = ToolLoader(test_classes=[GenieStudyReviewAdapter])
    monkeypatch.setattr(loader, "_discover_plugins", lambda: {})
    registry = ToolRegistry()
    assert loader.load(context(local_implementation), registry) == ["genie_study_review"]
    tool = registry.get("genie_study_review")
    assert tool.db_path == local_implementation / "memory/genie_study_review.sqlite"
    assert tool.parameters["properties"]["action"]["enum"] == ["register", "due", "result"]
    assert not tool.db_path.exists()


@pytest.mark.parametrize("name", ["workspace-testy", "workspace-heart", "workspace", "workspace-genie"])
def test_not_registered_in_other_workspaces(tmp_path, monkeypatch, name):
    loader = ToolLoader(test_classes=[GenieStudyReviewAdapter])
    monkeypatch.setattr(loader, "_discover_plugins", lambda: {})
    registry = ToolRegistry()
    assert loader.load(context(tmp_path / name), registry) == []
    assert not registry.has("genie_study_review")
    with pytest.raises(ValueError, match="only in the Genie"):
        GenieStudyReviewAdapter.create(context(tmp_path / name))


def test_missing_local_package_is_optional_in_fresh_checkout(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace-genie"
    monkeypatch.setattr(adapter, "_GENIE_WORKSPACE", workspace)
    loader = ToolLoader(test_classes=[GenieStudyReviewAdapter])
    monkeypatch.setattr(loader, "_discover_plugins", lambda: {})
    assert loader.load(context(workspace), ToolRegistry()) == []


async def test_adapter_delegates_to_local_implementation_with_temporary_db(local_implementation):
    tool = GenieStudyReviewAdapter.create(context(local_implementation))
    assert '"status": "rejected"' in await tool.execute(action="unsupported")
    assert tool.db_path.is_file()
