"""同一真实工作台请求的Turn AGENTS与model_call Skill独立更新。"""

from pathlib import Path

import commentjson
import pytest

from tests.integration.clients.web.itemized_context.test_turn_source_freezing import (
    SKILL_BODIES,
    SKILL_NAME,
    skill_file_content,
    verify_agents_source_boundary,
)
from tests.integration.clients.web.itemized_context.test_turn_source_freezing import (
    agents_source as agents_source,  # noqa: PLC0414 — 按本文件准备真实来源。
)
from tests.integration.clients.web.itemized_context.test_turn_source_freezing import (
    prefix_runtime as prefix_runtime,  # noqa: PLC0414 — 本文件独立服务组。
)
from tests.integration.clients.web.itemized_context.test_turn_source_freezing import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 — 正式构建fixture。
)
from tests.support.model_stream.config import ModelStreamReplay


@pytest.fixture(scope="module")
def integration_workspace_config_path(integration_workspace_config_path: str) -> str:
    path = Path(integration_workspace_config_path)
    config = commentjson.loads(path.read_text(encoding="utf-8"))
    config["context"] = {"resource_activation": {
        "default_boundary": "turn", "overrides": {"skill": "model_call"},
    }}
    path.write_text(commentjson.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture(scope="module", autouse=True)
def skill_source(integration_workspace_root_path: str) -> Path:
    path = Path(integration_workspace_root_path) / ".boxteam/skills" / SKILL_NAME / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skill_file_content(SKILL_BODIES[0]), encoding="utf-8")
    return path


async def test_mixed_boundaries_load_original_call_skill_and_restore_frozen_ui(
    request: pytest.FixtureRequest,
    prefix_runtime: tuple[str, Path, ModelStreamReplay, int],
    agents_source: Path,
    skill_source: Path,
) -> None:
    await verify_agents_source_boundary(request, prefix_runtime, agents_source,
        boundary="turn", skill_source=skill_source)
