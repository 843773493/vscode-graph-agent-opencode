"""验证拆分前合并配置的内容迁移。"""

from __future__ import annotations

import json
from pathlib import Path

import commentjson
import pytest

from app.agents.builtin_tool_registry import builtin_tool_ids
from configs.legacy_migrations import (
    migrate_config,
    persist_config_migration,
    read_and_migrate_config,
)


def test_v1_factory_paths_migrate_to_stable_tool_ids() -> None:
    result = migrate_config(
        {
            "agents": {
                "default": {
                    "tools": {
                        "custom": [
                            {
                                "name": "read_context",
                                "factory": (
                                    "app.agents.tools.session_history:"
                                    "create_read_session_recent_text_messages_tool"
                                ),
                            },
                            {
                                "name": "search_context",
                                "factory": (
                                    "app.agents.tools.session_history:"
                                    "create_grep_session_context_jsonl_tool"
                                ),
                            },
                            {
                                "name": "acme_tool",
                                "factory": "acme.tools:create_tool",
                            },
                        ]
                    }
                }
            }
        }
    )

    assert result.source_version == 1
    assert result.target_version == 4
    assert result.config["agents"]["default"]["tools"]["custom"] == [
        {"tool_id": "read_context"},
        {"tool_id": "search_context"},
        {"name": "acme_tool", "factory": "acme.tools:create_tool"},
    ]


def test_migration_is_idempotent() -> None:
    first = migrate_config(
        {"agents": {"default": {"tools": {"custom": [{"tool_id": "read_context"}]}}}}
    )
    second = migrate_config(first.config)

    assert second.changed is False
    assert second.config == first.config


def test_file_migration_persists_atomically_and_keeps_custom_options(
    tmp_path: Path,
) -> None:
    path = tmp_path / "boxteam.jsonc"
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "default": {
                        "tools": {
                            "custom": [
                                {
                                    "name": "fetch_webpage",
                                    "factory": (
                                        "app.agents.tools.web:create_fetch_webpage_tool"
                                    ),
                                    "options": {"mode": "strict"},
                                }
                            ]
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = read_and_migrate_config(path, persist=True)

    assert result.changed is True
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["config_version"] == 4
    assert persisted["agents"]["default"]["tools"]["custom"] == [
        {"tool_id": "fetch_webpage", "options": {"mode": "strict"}}
    ]
    assert not list(tmp_path.glob("*.tmp"))


def test_newer_config_version_fails_fast() -> None:
    with pytest.raises(ValueError, match="高于当前程序支持范围"):
        migrate_config({"config_version": 999})


def test_concurrent_identical_migration_commit_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "boxteam.jsonc"
    path.write_text('{"agents": {}}', encoding="utf-8")
    first = read_and_migrate_config(path, persist=False)
    second = read_and_migrate_config(path, persist=False)

    persist_config_migration(path, first)
    persist_config_migration(path, second)

    assert json.loads(path.read_text(encoding="utf-8"))["config_version"] == 4


def test_v2_browser_tools_gain_read_only_browser_listing() -> None:
    result = migrate_config(
        {
            "config_version": 2,
            "agents": {
                "default": {
                    "tools": {
                        "custom": [
                            {"tool_id": "read_context"},
                            {"tool_id": "openBrowserPage"},
                            {"tool_id": "readPage"},
                        ]
                    }
                },
                "without_browser": {"tools": {"custom": [{"tool_id": "read_context"}]}},
            },
        }
    )

    assert result.target_version == 4
    assert result.config["agents"]["default"]["tools"]["custom"] == [
        {"tool_id": "read_context"},
        {"tool_id": "listBrowserPage"},
        {"tool_id": "openBrowserPage"},
        {"tool_id": "readPage"},
    ]
    assert result.config["agents"]["without_browser"]["tools"]["custom"] == [
        {"tool_id": "read_context"}
    ]


def test_v3_legacy_ssh_workspace_migrates_to_remote_gateway() -> None:
    result = migrate_config(
        {
            "config_version": 3,
            "gateway": {
                "workspaces": [
                    {
                        "enabled": True,
                        "kind": "ssh",
                        "name": "Gateway E2E Docker Workspace",
                        "host": "127.0.0.1",
                        "port": 22222,
                        "username": "root",
                        "private_key_path": "~/.ssh/boxteam_gateway_e2e_ed25519",
                        "remote_backend_host": "127.0.0.1",
                        "remote_backend_port": 8010,
                        "remote_workspace_path": "/root/.boxteams/boxteam_workspace",
                        "activate": False,
                    }
                ]
            },
        }
    )

    assert result.target_version == 4
    assert result.config["gateway"]["workspaces"] == [
        {
            "enabled": True,
            "kind": "remote_gateway",
            "name": "Gateway E2E Docker Workspace",
            "host": "127.0.0.1",
            "port": 22222,
            "username": "root",
            "private_key_path": "~/.ssh/boxteam_gateway_e2e_ed25519",
            "remote_gateway_port": 8014,
            "activate": False,
        }
    ]


def test_conflicting_legacy_aliases_fail_instead_of_silently_deduplicating() -> None:
    with pytest.raises(ValueError, match="迁移后与已有 'read_context' 配置冲突"):
        migrate_config(
            {
                "agents": {
                    "default": {
                        "tools": {
                            "custom": [
                                {
                                    "name": "legacy_a",
                                    "factory": (
                                        "app.agents.tools.session_history:"
                                        "create_read_session_recent_text_messages_tool"
                                    ),
                                    "options": {"limit": 1},
                                },
                                {
                                    "name": "legacy_b",
                                    "factory": (
                                        "app.agents.tools.session_history:"
                                        "create_read_session_context_jsonl_tool"
                                    ),
                                    "options": {"limit": 2},
                                },
                            ]
                        }
                    }
                }
            }
        )


def test_schema_builtin_tool_ids_match_runtime_registry() -> None:
    schema = commentjson.loads(
        Path("configs/workspace_schema.jsonc").read_text(encoding="utf-8")
    )
    tool_id_schema = schema["$defs"]["agentTools"]["properties"]["custom"]["items"][
        "oneOf"
    ][0]["properties"]["tool_id"]

    assert set(tool_id_schema["enum"]) == set(builtin_tool_ids())
