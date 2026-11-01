"""显式model_call的AGENTS更新与同assembly默认Turn来源隔离。"""

from pathlib import Path

import commentjson
import pytest

from tests.integration.clients.web.itemized_context.test_turn_source_freezing import (
    agents_source as agents_source,  # noqa: PLC0414 — 原来源fixture按当前文件隔离。
)
from tests.integration.clients.web.itemized_context.test_turn_source_freezing import (
    prefix_runtime as prefix_runtime,  # noqa: PLC0414 — 原Gateway/回放按当前文件隔离。
)
from tests.integration.clients.web.itemized_context.test_turn_source_freezing import (
    verify_agents_source_boundary,
)
from tests.integration.clients.web.itemized_context.test_turn_source_freezing import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 — 原正式构建fixture。
)
from tests.support.model_stream.config import ModelStreamReplay


@pytest.fixture(scope="module")
def integration_workspace_config_path(integration_workspace_config_path: str) -> str:
    """只改本文件工作区已有配置；其他来源继续使用原Turn默认值。"""
    path = Path(integration_workspace_config_path)
    config = commentjson.loads(path.read_text(encoding="utf-8"))
    config["context"] = {"resource_activation": {
        "default_boundary": "turn", "overrides": {"workspace_agents": "model_call"},
    }}
    path.write_text(commentjson.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


async def test_tool_loop_updates_agents_at_model_call_and_preserves_other_turn_sources(
    request: pytest.FixtureRequest,
    prefix_runtime: tuple[str, Path, ModelStreamReplay, int],
    agents_source: Path,
) -> None:
    await verify_agents_source_boundary(request, prefix_runtime, agents_source, boundary="model_call")
