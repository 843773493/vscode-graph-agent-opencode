from __future__ import annotations

import json
from pathlib import Path

import commentjson


def write_mcp_config(
    *,
    workspace_root: Path,
    servers: dict[str, dict[str, object]],
    allowed_mcp_tools: list[str],
) -> str:
    project_root = Path.cwd().resolve()
    default_config_path = (
        project_root
        / "configs"
        / "tests"
        / "workspace"
        / "default.jsonc"
    )
    with default_config_path.open("r", encoding="utf-8") as stream:
        workspace_config = commentjson.load(stream)
    workspace_config["mcp"] = {"servers": servers}
    workspace_config["agents"]["default"]["tools"]["denylist"] = ["all"]
    workspace_config["agents"]["default"]["tools"]["allowlist"] = list(allowed_mcp_tools)

    artifacts_dir = workspace_root.parent / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    output_config_path = artifacts_dir / "workspace.jsonc"
    output_config_path.write_text(
        json.dumps(workspace_config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(output_config_path)
