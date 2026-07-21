from __future__ import annotations

import json
from pathlib import Path

import commentjson


def write_e2e_mcp_config(
    *,
    workspace_root: Path,
    servers: dict[str, dict[str, object]],
    allowed_mcp_tools: list[str],
) -> str:
    project_root = Path.cwd().resolve()
    source_config = project_root / "configs" / "tests" / "default.jsonc"
    with source_config.open("r", encoding="utf-8") as stream:
        config = commentjson.load(stream)
    config["mcp"] = {"servers": servers}
    config["agents"]["default"]["tools"]["denylist"] = ["all"]
    config["agents"]["default"]["tools"]["allowlist"] = list(allowed_mcp_tools)

    artifacts_dir = workspace_root.parent / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    config_path = artifacts_dir / "boxteam.jsonc"
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(config_path)
