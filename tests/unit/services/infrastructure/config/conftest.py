"""模型解析测试使用真实节点文档与统一配置图。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.infrastructure.config.source_uri import config_file_uri
from configs.inheritance import ConfigGraph


@pytest.fixture
def model_graph_factory(tmp_path: Path, workspace_config_schema: dict):
    def create(document: dict, *, sources: dict[str, dict] | None = None, environment: dict[str, str] | None = None) -> ConfigGraph:
        files = {"workspace.jsonc": document, **(sources or {})}
        for name, payload in files.items():
            (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
        schema = {
            "type": "object", "$defs": workspace_config_schema["$defs"],
            "properties": {"workspace": {"type": "object"},
                           "model_options": {"$ref": "#/$defs/modelOptions"},
                           "network": {"type": "object", "properties": {"proxy": {"x-merge": "replace"}}}},
        }
        return ConfigGraph(domain="workspace", version=4, owner_id="gateway-test/workspace-test",
            entry=tmp_path / "workspace.jsonc", aliases={}, settings_schema=schema, options_schema=schema,
            proxy_environment=environment,
            source_key=lambda path: path.stem,
            source_uri=lambda path: config_file_uri(scope="user", scope_id="local", logical_name=path.stem),
            authorize_source=lambda path: path.parent == tmp_path)
    return create
