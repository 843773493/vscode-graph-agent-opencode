"""创建当前集成测试文件专属的标准工作区。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_test_workspace


def prepare_rollout_workspace(request: pytest.FixtureRequest) -> Path:
    """重建当前测试文件的显式工作区，由 fixture owner 持有 catalog。"""

    context = TestRunContext.from_test_file(Path(request.node.path))
    template_root = Path.cwd() / "tests/fixtures/workspaces/default_test_workspace"
    # 下方会重新创建 .boxteam/sessions，因此不复制随后会被整体替换的 .boxteam。
    workspace_root = prepare_test_workspace(
        workspace_root=context.workspace_root,
        template_root=template_root,
        template_items=tuple(
            sorted(
                entry.name
                for entry in template_root.iterdir()
                if entry.name != ".boxteam"
            )
        ),
    )
    get_session_path_resolver(workspace_root / ".boxteam" / "sessions")
    return workspace_root
