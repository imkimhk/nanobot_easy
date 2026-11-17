"""Discovery adapter for Genie's local study-review implementation only."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool

_GENIE_WORKSPACE = Path(__file__).resolve().parents[3] / ".local" / "workspace-genie"
_PACKAGE_NAME = "_nanobot_genie_study_review"


def _implementation() -> type[Tool]:
    # A private package namespace preserves relative imports without adding the
    # workspace to sys.path or colliding with another workspace's packages.
    if _PACKAGE_NAME not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _PACKAGE_NAME, _GENIE_WORKSPACE / "study_review" / "__init__.py",
            submodule_search_locations=[str(_GENIE_WORKSPACE / "study_review")],
        )
        if spec is None or spec.loader is None:
            raise ImportError("Cannot load Genie's study_review package")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PACKAGE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_PACKAGE_NAME, None)
            raise
    from importlib import import_module

    return import_module(f"{_PACKAGE_NAME}.tool").GenieStudyReviewTool


class GenieStudyReviewAdapter(Tool):
    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        workspace = _GENIE_WORKSPACE.resolve()
        package = workspace / "study_review"
        return (Path(ctx.workspace).resolve() == workspace
                and package.resolve().is_relative_to(workspace)
                and all((package / name).is_file()
                        and (package / name).resolve().is_relative_to(package.resolve())
                        for name in ("__init__.py", "tool.py")))

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        if not cls.enabled(ctx):
            raise ValueError("Study review is available only in the Genie workspace with its local implementation")
        return _implementation()(
            db_path=Path(ctx.workspace).resolve() / "memory" / "genie_study_review.sqlite"
        )

    @property
    def name(self) -> str:
        return "genie_study_review"

    @property
    def description(self) -> str:
        return "Genie workspace study-review adapter."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        raise RuntimeError("Construct this tool through create()")
