from __future__ import annotations

import json
from pathlib import Path

from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.tool_catalog_service import ToolCatalogService


class _RuntimeCatalog:
    def get_available_tools(self, agent_id: str = "default") -> list[dict]:
        return [
            {
                "id": "read_file",
                "name": "read_file",
                "description": "读取文件",
                "parameters": {"type": "object"},
            },
            {
                "id": "invoke_custom_tool",
                "name": "invoke_custom_tool",
                "description": "调用扩展工具",
                "parameters": {"type": "object"},
            },
            {
                "id": "send_message_to_session",
                "name": "send_message_to_session",
                "description": "跨会话发送消息",
                "parameters": {"type": "object"},
                "group_id": "agent-collaboration",
                "group_name": "默认工具 · Agent Collaboration",
                "kind": "collaboration",
            },
        ]


def test_catalog_only_exposes_extensions_enabled_by_resolved_policy(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "boxteam.jsonc"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "default": {
                        "tools": {
                            "denylist": ["extensions"],
                            "allowlist": ["web_search"],
                            "custom": [
                                {
                                    "name": "read_session_recent_text_messages",
                                    "factory": (
                                        "app.agents.tools.session_history:"
                                        "create_read_session_recent_text_messages_tool"
                                    ),
                                },
                                {
                                    "name": "web_search",
                                    "factory": "example:create_web_search",
                                    "description": "网络搜索",
                                },
                                {
                                    "name": "fetch_webpage",
                                    "factory": "example:create_fetch_webpage",
                                },
                            ],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
    )
    service = ToolCatalogService(
        runtime_catalog=_RuntimeCatalog(),
        config_service=config_service,
    )

    definitions = service.get_available_tools()

    assert [item["id"] for item in definitions] == [
        "read_file",
        "send_message_to_session",
        "web_search",
    ]
    assert definitions[1]["group_id"] == "agent-collaboration"
    assert definitions[1]["group_name"] == "默认工具 · Agent Collaboration"
    assert definitions[1]["kind"] == "collaboration"
    assert definitions[2]["description"] == "网络搜索"


def test_catalog_groups_default_session_context_extensions_as_collaboration(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "boxteam.jsonc"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "default": {
                        "tools": {
                            "custom": [
                                {
                                    "name": "read_session_recent_text_messages",
                                    "factory": (
                                        "app.agents.tools.session_history:"
                                        "create_read_session_recent_text_messages_tool"
                                    ),
                                },
                            ],
                        },
                    },
                },
            },
        ),
        encoding="utf-8",
    )
    service = ToolCatalogService(
        runtime_catalog=_RuntimeCatalog(),
        config_service=ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
        ),
    )

    by_id = {
        item["id"]: item
        for item in service.get_available_tools()
    }
    session_reader = by_id["read_session_recent_text_messages"]
    assert session_reader["group_id"] == "agent-collaboration"
    assert session_reader["group_name"] == "默认工具 · Agent Collaboration"
    assert session_reader["kind"] == "collaboration"
