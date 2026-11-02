"""真实工作台工具循环内热更新策略，旧Turn和冷恢复保留原冻结事实。"""

from pathlib import Path

import pytest

from tests.integration.clients.web.itemized_context.test_turn_source_freezing import (
    agents_source as agents_source,  # noqa: PLC0414 — 来源文件按当前测试路径隔离。
)
from tests.integration.clients.web.itemized_context.test_turn_source_freezing import (
    prefix_runtime as prefix_runtime,  # noqa: PLC0414 — 复用唯一Gateway/Backend装具。
)
from tests.integration.clients.web.itemized_context.test_turn_source_freezing import (
    verify_agents_source_boundary,
)
from tests.integration.clients.web.itemized_context.test_turn_source_freezing import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 — 正式Workbench构建。
)
from tests.support.model_stream.config import ModelStreamReplay


async def test_hot_policy_preserves_running_turn_and_frozen_ui_after_restart(
    request: pytest.FixtureRequest,
    prefix_runtime: tuple[str, Path, ModelStreamReplay, int],
    agents_source: Path,
    integration_workspace_config_path: str,
) -> None:
    await verify_agents_source_boundary(request, prefix_runtime, agents_source,
        boundary="turn", policy_update=Path(integration_workspace_config_path))
