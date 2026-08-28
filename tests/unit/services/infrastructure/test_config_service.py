from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema
import pytest

from app.agents.policy import ToolMetadata
from app.schemas.internal_v2.config import ConfigUpdateRequest
from app.services.infrastructure.config import ConfigRestartRequiredError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


def _write_workspace_config(tmp_path: Path, config: dict) -> Path:
    config_path = tmp_path / "workspace.jsonc"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _base_config() -> dict:
    return {
        "config_version": 1,
        "llm": {
            "providers": [
                {
                    "id": "primary",
                    "endpoint": "https://example.com/v1",
                    "model": "model-a",
                    "api_key": "${TEST_API_KEY}",
                    "custom_llm_provider": "openai",
                    "api_mode": {
                        "protocol": "chat_completions",
                        "model_info": {
                            "supports_function_calling": True,
                            "supports_reasoning": True,
                        },
                        "supports_reasoning": {"reasoning_content": True},
                    },
                }
            ]
        },
        "logger": {"level": "info", "pretty": True},
        "default_agent": "default",
        "agents": {
            "default": {
                "name": "Default Agent",
                "instructions": {"system_prompt": "hello"},
                "model": {
                    "primary_provider": "primary",
                    "fallback_providers": [],
                },
            }
        },
    }


def test_config_accepts_chatgpt_oauth_provider_without_api_key(tmp_path: Path):
    config = _base_config()
    config["llm"]["providers"].append(
        {
            "id": "backup_4",
            "endpoint": "https://chatgpt.com/backend-api/codex",
            "model": "gpt-5.6-luna",
            "custom_llm_provider": "chatgpt",
            "api_mode": {
                "protocol": "responses",
                "model_info": {
                    "supports_function_calling": True,
                    "supports_reasoning": True,
                },
                "supports_reasoning": {
                    "reasoning_items": {
                        "summary": True,
                        "encrypted_content": True,
                    }
                },
                "replay_policy": {"encrypted_content": "same_source"},
            },
            "auth": {"type": "oauth", "method": "chatgpt"},
        }
    )
    config_path = _write_workspace_config(tmp_path, config)

    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    service.validate_workspace_config()
    assert service.get_llm_provider("backup_4")["auth"]["method"] == "chatgpt"


def test_config_rejects_provider_without_api_key_or_oauth(tmp_path: Path):
    config = _base_config()
    del config["llm"]["providers"][0]["api_key"]
    config_path = _write_workspace_config(tmp_path, config)

    with pytest.raises(jsonschema.ValidationError, match="api_key"):
        ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
        ).validate_workspace_config()


def test_tool_policy_resolver_merges_workspace_and_agent_rules(tmp_path: Path):
    config = _base_config()
    config["tooling"] = {
        "policy_defaults": {
            "execution_enabled": True,
            "model_visible": True,
        },
        "policy_rules": {
            "by_kind": {"debugging": {"model_visible": False}},
            "by_group": {},
            "by_origin": {},
            "by_tool": {},
        },
        "restrictions": {
            "execution_disabled": [],
            "model_hidden": [],
            "confirmation_required": [],
        },
    }
    config["agents"]["default"]["tools"] = {
        "policy": {
            "rules": {
                "by_tool": {"start_debugging": {"model_visible": True}}
            },
            "restrictions": {"execution_disabled": ["tool:blocked_tool"]},
        }
    }
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_workspace_config(tmp_path, config),
    )

    resolver = service.get_tool_policy_resolver()

    debugging = resolver.resolve(
        ToolMetadata(
            tool_id="start_debugging",
            origin="builtin",
            kind="debugging",
            group_id="debugging",
        )
    )
    blocked = resolver.resolve(
        ToolMetadata(
            tool_id="blocked_tool",
            origin="builtin",
            kind="default",
            group_id="default",
        ),
        execution_override=True,
    )

    assert debugging.model_visible is True
    assert blocked.execution_enabled is False


@pytest.mark.asyncio
async def test_get_public_config_resolves_default_model_from_agent_provider(
    tmp_path: Path,
):
    config_path = _write_workspace_config(tmp_path, _base_config())
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    result = await service.get()

    assert result.default_model == "model-a"
    assert result.default_orchestration == "single_agent"
    assert result.max_concurrent_agents == 4
    assert result.metadata["default_agent_id"] == "default"
    assert result.metadata["config_path"] == str(config_path)


@pytest.mark.asyncio
async def test_update_public_config_uses_runtime_overrides(tmp_path: Path):
    config = _base_config()
    config["ui"] = {
        "default_orchestration": "planner",
        "max_concurrent_agents": 2,
        "allow_shell_tools": False,
        "ignored_paths": ["node_modules"],
        "auto_summarize": False,
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    result = await service.update(
        ConfigUpdateRequest(
            default_model="runtime-model",
            allow_shell_tools=True,
            ignored_paths=["dist"],
        )
    )

    assert result.default_model == "runtime-model"
    assert result.default_orchestration == "planner"
    assert result.max_concurrent_agents == 2
    assert result.allow_shell_tools is True
    assert result.ignored_paths == ["dist"]
    assert result.auto_summarize is False
    assert result.metadata["runtime_overrides"] == [
        "allow_shell_tools",
        "default_model",
        "ignored_paths",
    ]


@pytest.mark.asyncio
async def test_update_public_config_null_clears_runtime_override(tmp_path: Path):
    config_path = _write_workspace_config(tmp_path, _base_config())
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    await service.update(ConfigUpdateRequest(default_model="runtime-model"))
    result = await service.update(ConfigUpdateRequest(default_model=None))

    assert result.default_model == "model-a"
    assert result.metadata["runtime_overrides"] == []


def test_workspace_local_config_is_merged_before_workspace_override(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["logger"]["level"] = "warning"
    config_path = _write_workspace_config(tmp_path, config)
    local_path = tmp_path / "workspace_local.jsonc"
    local_path.write_text(
        json.dumps(
            {
                "logger": {"level": "debug"},
                "agents": {"default": {"name": "Local Agent"}},
            }
        ),
        encoding="utf-8",
    )
    workspace_root = tmp_path / "workspace"
    workspace_path = workspace_root / ".boxteam" / "workspace.jsonc"
    workspace_path.parent.mkdir(parents=True)
    workspace_path.write_text(
        json.dumps(
            {
                "agents": {"default": {"name": "Workspace Agent"}},
            }
        ),
        encoding="utf-8",
    )

    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=workspace_root,
    )

    assert service.get_logger_level() == "DEBUG"
    assert service.list_agents()["default"]["name"] == "Workspace Agent"
    assert service.get_snapshot().source_paths == (
        Path("configs/workspace_inline.jsonc").resolve(),
        config_path,
        local_path,
        workspace_path,
    )
    assert [source.layer for source in service.get_source_details()] == [
        "inline",
        "user",
        "user_local",
        "workspace",
    ]

    public_config = service.get_snapshot()
    assert public_config.source_details[2].loaded is True


def test_workspace_config_migrates_mutable_json_layers_to_sqlite(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["logger"]["level"] = "warning"
    config_path = _write_workspace_config(tmp_path, config)
    local_path = tmp_path / "workspace_local.jsonc"
    local_path.write_text(json.dumps({"logger": {"level": "debug"}}), encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_path = workspace_root / ".boxteam" / "workspace.jsonc"
    workspace_path.parent.mkdir(parents=True)
    workspace_path.write_text(
        json.dumps({"agents": {"default": {"name": "Workspace Agent"}}}),
        encoding="utf-8",
    )
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )

        assert service.get_logger_level() == "DEBUG"
        assert service.list_agents()["default"]["name"] == "Workspace Agent"
        assert [source.layer for source in service.get_source_details()] == [
            "inline",
            "sqlite",
            "sqlite",
            "sqlite",
        ]
        assert config_path.with_name("workspace.jsonc.migrated.bak").is_file()
        assert local_path.with_name("workspace_local.jsonc.migrated.bak").is_file()
        assert workspace_path.with_name("workspace.jsonc.migrated.bak").is_file()

        config_path.write_text(json.dumps({"logger": {"level": "ERROR"}}), encoding="utf-8")
        assert service.get_logger_level() == "DEBUG"
    finally:
        store.close()


def test_workspace_config_restarts_from_sqlite_after_source_json_changes(
    tmp_path: Path,
) -> None:
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        first = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        assert first.get_logger_level() == "INFO"
        config_path.write_text("{ this is not valid jsonc", encoding="utf-8")
        second = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        assert second.get_logger_level() == "INFO"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_public_runtime_overrides_restart_from_workspace_sqlite(tmp_path: Path):
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        first = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        await first.update(ConfigUpdateRequest(default_model="runtime-model"))
        second = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        assert (await second.get()).default_model == "runtime-model"
        await second.update(ConfigUpdateRequest(default_model=None))
        third = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        assert (await third.get()).default_model == "model-a"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_get_public_config_fails_when_default_agent_provider_is_missing(
    tmp_path: Path,
):
    config = _base_config()
    config["agents"]["default"]["model"]["primary_provider"] = "missing"
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(
        ValueError, match="default agent 引用了不存在的 provider: missing"
    ):
        await service.get()


def test_get_agent_tool_config_reads_custom_tools(tmp_path: Path):
    config = _base_config()
    config["agents"]["default"]["tools"] = {
        "denylist": [],
        "confirmation_required": [],
        "custom": [{"tool_id": "test_tool_2"}],
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    result = service.get_agent_tool_config("default")

    assert result["custom"] == [{"tool_id": "test_tool_2"}]


def test_get_agent_tool_config_resolves_all_minus_allowlist(tmp_path: Path):
    config = _base_config()
    config["agents"]["default"]["tools"] = {
        "denylist": ["all"],
        "allowlist": ["read_file"],
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    result = service.get_agent_tool_config("default")
    policy = service.resolve_agent_tool_policy("default")

    assert result["allowlist"] == ["read_file"]
    assert result["denylist"] == ["all"]
    assert policy.enabled_names == frozenset({"read_file"})


def test_discovered_mcp_tools_participate_in_policy_and_confirmation(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["agents"]["default"]["tools"] = {
        "denylist": ["extensions"],
        "allowlist": ["mcp__mini__echo"],
        "confirmation_required": ["mcp__mini__echo"],
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    service.set_mcp_tool_names(frozenset({"mcp__mini__echo", "mcp__mini__increment"}))
    policy = service.resolve_agent_tool_policy("default")

    assert "mcp__mini__echo" in policy.enabled_names
    assert "mcp__mini__increment" in policy.disabled_names
    assert service.resolve_agent_confirmation_tool_names("default") == frozenset(
        {"mcp__mini__echo"}
    )


def test_discovered_mcp_tools_reject_unknown_configured_tool(tmp_path: Path) -> None:
    config = _base_config()
    config["agents"]["default"]["tools"] = {
        "denylist": ["mcp__missing__tool"],
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(ValueError, match="mcp__missing__tool"):
        service.set_mcp_tool_names(frozenset({"mcp__mini__echo"}))


def test_config_loading_fails_on_delegation_dependency_conflict(tmp_path: Path):
    config = _base_config()
    config["agents"]["default"]["tools"] = {
        "denylist": ["send_message_to_session"],
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(
        ValueError,
        match="create_team_member, task 依赖 send_message_to_session",
    ):
        service.list_agents()


def test_extension_selector_can_restore_one_custom_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "app.services.infrastructure.config_service.load_custom_tool_factory",
        lambda _factory_path: object(),
    )
    config = _base_config()
    config["agents"]["default"]["tools"] = {
        "denylist": ["extensions"],
        "allowlist": ["web_search"],
        "custom": [
            {"name": "web_search", "factory": "example:create_web_search"},
            {"name": "fetch_webpage", "factory": "example:create_fetch_webpage"},
        ],
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    policy = service.resolve_agent_tool_policy("default")

    assert policy.enabled_extension_names == frozenset({"web_search"})


def test_config_service_normalizes_custom_tool_specs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "app.services.infrastructure.config_service.load_custom_tool_factory",
        lambda _factory_path: object(),
    )
    config = _base_config()
    config["agents"]["default"]["tools"] = {
        "custom": [
            {
                "name": "  web_search  ",
                "factory": "  example:create_web_search  ",
                "options": {"region": "cn"},
            }
        ]
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    tool_config = service.get_agent_tool_config("default")

    assert tool_config["custom"] == [
        {
            "name": "web_search",
            "factory": "example:create_web_search",
            "options": {"region": "cn"},
        }
    ]


def test_config_loading_rejects_duplicate_normalized_custom_tool_names(
    tmp_path: Path,
):
    config = _base_config()
    config["agents"]["default"]["tools"] = {
        "custom": [
            {"name": "web_search", "factory": "example:create_web_search"},
            {"name": " web_search ", "factory": "example:create_other"},
        ]
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(ValueError, match="重复扩展工具名: web_search"):
        service.list_agents()


def test_workspace_config_overrides_user_global_config(
    tmp_path: Path,
) -> None:
    global_config = _base_config()
    global_config["logger"]["level"] = "warning"
    global_path = tmp_path / "home" / ".boxteam" / "workspace.jsonc"
    global_path.parent.mkdir(parents=True)
    global_path.write_text(json.dumps(global_config), encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    workspace_config = workspace_root / ".boxteam" / "workspace.jsonc"
    workspace_config.parent.mkdir(parents=True)
    workspace_config.write_text(
        json.dumps(
            {
                "agents": {
                    "default": {
                        "name": "Workspace Override",
                    }
                },
                "development": {"test_tools": True},
            }
        ),
        encoding="utf-8",
    )

    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=global_path,
        workspace_root=workspace_root,
    )

    assert service.list_agents()["default"]["name"] == "Workspace Override"
    assert service.list_agents()["default"]["model"]["primary_provider"] == "primary"
    assert service.development_test_tools_enabled() is True


def test_logger_level_is_normalized_and_validated(tmp_path: Path) -> None:
    config = _base_config()
    config["logger"]["level"] = " warning "
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    assert service.get_logger_level() == "WARNING"

    config["logger"]["level"] = "verbose"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    invalid_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
    )
    with pytest.raises(ValueError, match="logger.level 仅支持"):
        invalid_service.get_logger_level()


def test_logger_pretty_is_read_and_validated(tmp_path: Path) -> None:
    config = _base_config()
    config["logger"]["pretty"] = False
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    assert service.get_logger_pretty() is False

    config["logger"]["pretty"] = "yes"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    invalid_service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
    )
    with pytest.raises(ValueError, match="logger.pretty 必须是布尔值"):
        invalid_service.get_logger_pretty()


def test_custom_tool_options_schema_accepts_embedding_config(tmp_path: Path) -> None:
    config = _base_config()
    config["agents"]["default"]["tools"] = {
        "custom": [
            {
                "name": "fetch_webpage",
                "factory": "app.agents.tools.web:create_fetch_webpage_tool",
                "options": {
                    "embedding": {
                        "provider_id": "primary",
                        "model": "text-embedding-3-small",
                    }
                },
            }
        ]
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    service.validate_workspace_config()


def test_get_llm_provider_only_resolves_selected_provider_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    config["llm"]["providers"].append(
        {
            "id": "embedding",
            "endpoint": "https://embedding.example.com/v1",
            "model": "embedding-model",
            "api_key": "${EMBEDDING_API_KEY}",
            "custom_llm_provider": "openai",
        }
    )
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_API_KEY", "embedding-secret")
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    provider = service.get_llm_provider("embedding")

    assert provider["api_key"] == "embedding-secret"


def test_provider_request_override_schema_accepts_raw_completion_parameters(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["llm"]["providers"][0]["request_options"] = {
        "overrides": {
            "temperature": 1,
            "max_tokens": None,
            "extra_body": {
                "reasoning": True,
                "max_output_tokens": 1200,
            },
        }
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    service.validate_workspace_config()


def test_agent_runtime_omits_unspecified_generation_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    runtime = service.get_agent_runtime_config("default")

    assert "temperature" not in runtime
    assert "top_p" not in runtime
    assert "max_output_tokens" not in runtime
    assert runtime["require_delegated_report"] is False


def test_agent_runtime_can_require_delegated_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    config["agents"]["default"]["execution"] = {
        "require_delegated_report": True,
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    runtime = service.get_agent_runtime_config("default")

    assert runtime["require_delegated_report"] is True


def test_agent_runtime_places_session_provider_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    config["llm"]["providers"].append(
        {
            "id": "backup",
            "endpoint": "https://example.com/v1",
            "model": "model-b",
            "api_key": "${TEST_API_KEY}",
            "custom_llm_provider": "openai",
        }
    )
    config["agents"]["default"]["model"]["fallback_providers"] = ["backup"]
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    runtime = service.get_agent_runtime_config(
        "default",
        preferred_provider_id="backup",
    )

    assert [provider["id"] for provider in runtime["providers"]] == [
        "backup",
        "primary",
    ]
    assert service.resolve_agent_provider_id("default", "backup") == "backup"

    with pytest.raises(ValueError, match="不允许使用 provider"):
        service.get_agent_runtime_config(
            "default",
            preferred_provider_id="unknown",
        )


def test_workspace_session_defaults_are_persisted_without_changing_static_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    config["llm"]["providers"].append(
        {
            "id": "backup",
            "endpoint": "https://example.com/v1",
            "model": "model-b",
            "api_key": "${TEST_API_KEY}",
            "custom_llm_provider": "openai",
        }
    )
    config["agents"]["default"]["model"]["fallback_providers"] = ["backup"]
    config["agents"]["coder"] = {
        "name": "Coder",
        "instructions": {"system_prompt": "code"},
        "model": {
            "primary_provider": "primary",
            "fallback_providers": ["backup"],
        },
    }
    config_path = _write_workspace_config(tmp_path, config)
    workspace_root = tmp_path / "workspace"
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=workspace_root,
    )

    service.set_workspace_default_agent("coder")
    service.set_workspace_default_provider("coder", "backup")

    restored = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=workspace_root,
    )
    assert restored.get_workspace_default_agent_id() == "coder"
    assert restored.get_workspace_default_provider_id("coder") == "backup"
    assert restored.get_default_agent_id() == "default"
    assert restored.resolve_agent_provider_id("coder") == "primary"
    persisted = json.loads(
        (workspace_root / ".boxteam" / "settings" / "session_defaults.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted == {
        "schema_version": 1,
        "provider_by_agent": {"coder": "backup"},
        "default_agent_id": "coder",
    }


def test_provider_request_override_schema_rejects_legacy_extra_body(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["llm"]["providers"][0]["request_options"] = {
        "extra_body": {"reasoning": True}
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(jsonschema.ValidationError):
        service.validate_workspace_config()


def test_workspace_schema_rejects_gateway_section(tmp_path: Path):
    config = _base_config()
    config["gateway"] = {
        "workspaces": [
            {
                "kind": "remote_gateway",
                "name": "remote gateway",
                "host": "127.0.0.1",
                "port": 22222,
                "username": "root",
                "private_key_path": "~/.ssh/boxteam_gateway_e2e_ed25519",
                "remote_gateway_port": 8014,
                "activate": False,
            }
        ]
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(jsonschema.ValidationError) as error_info:
        service.validate_workspace_config()

    assert list(error_info.value.absolute_path) == []


@pytest.mark.asyncio
async def test_config_uses_stable_snapshot_until_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    original_snapshot = service.get_snapshot()
    original_revision = original_snapshot.revision
    config["llm"]["providers"][0]["model"] = "model-b"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    assert service.get_llm_providers()[0]["model"] == "model-a"
    assert await service.reload() is True
    assert service.get_revision() != original_revision
    assert service.get_llm_providers()[0]["model"] == "model-b"
    with service.use_snapshot(original_snapshot):
        assert (await service.get()).default_model == "model-a"


@pytest.mark.asyncio
async def test_reload_ignores_shadowed_lower_priority_change(tmp_path: Path) -> None:
    global_config = _base_config()
    global_path = _write_workspace_config(tmp_path, global_config)
    workspace_root = tmp_path / "workspace"
    workspace_config_path = workspace_root / ".boxteam" / "workspace.jsonc"
    workspace_config_path.parent.mkdir(parents=True)
    workspace_config_path.write_text(
        json.dumps({"logger": {"level": "warning"}}),
        encoding="utf-8",
    )
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=global_path,
        workspace_root=workspace_root,
    )

    original_revision = service.get_revision()
    global_config["logger"]["level"] = "debug"
    global_path.write_text(json.dumps(global_config), encoding="utf-8")

    assert await service.reload() is False
    assert service.get_revision() == original_revision


@pytest.mark.asyncio
async def test_same_revision_reload_refreshes_active_source_paths(
    tmp_path: Path,
) -> None:
    global_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    workspace_config_path = workspace_root / ".boxteam" / "workspace.jsonc"
    workspace_config_path.parent.mkdir(parents=True)
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=global_path,
        workspace_root=workspace_root,
    )
    original_revision = service.get_revision()

    workspace_config_path.write_text("{}", encoding="utf-8")

    assert await service.reload() is False
    snapshot = service.get_snapshot()
    assert snapshot.revision == original_revision
    assert snapshot.source_paths == (
        Path("configs/workspace_inline.jsonc").resolve(),
        global_path,
        workspace_config_path,
    )


@pytest.mark.asyncio
async def test_reload_workspace_config_deletion_falls_back_to_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    user_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    workspace_config_path = workspace_root / ".boxteam" / "workspace.jsonc"
    workspace_config_path.parent.mkdir(parents=True)
    workspace_config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "default": {
                        "instructions": {"system_prompt": "workspace"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=user_path,
        workspace_root=workspace_root,
    )
    workspace_revision = service.get_revision()

    workspace_config_path.unlink()

    assert await service.reload() is True
    assert service.get_revision() != workspace_revision
    assert service.get_agent_runtime_config("default")["system_prompt"] == "hello"
    assert service.get_snapshot().source_paths == (
        Path("configs/workspace_inline.jsonc").resolve(),
        user_path,
    )


@pytest.mark.asyncio
async def test_invalid_reload_retains_last_valid_snapshot_and_exposes_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)
    original_revision = service.get_revision()
    config_path.write_text("{ invalid", encoding="utf-8")

    with pytest.raises(ValueError):
        await service.reload()

    assert service.get_revision() == original_revision
    assert service.get_llm_providers()[0]["model"] == "model-a"
    status = service.get_reload_status()
    assert status.healthy is False
    assert status.revision == original_revision
    assert status.last_error
    assert status.reason == "invalid_config"
    assert status.restart_required is False
    public_config = await service.get()
    assert public_config.metadata["reload"]["healthy"] is False
    assert public_config.metadata["reload"]["last_error"] == status.last_error


@pytest.mark.asyncio
async def test_reload_rejects_missing_custom_factory_and_retains_snapshot(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)
    original_revision = service.get_revision()
    config["agents"]["default"]["tools"] = {
        "custom": [
            {
                "name": "missing_tool",
                "factory": "missing_package.tools:create_missing_tool",
            }
        ]
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="扩展工具预检失败"):
        await service.reload()

    assert service.get_revision() == original_revision
    status = service.get_reload_status()
    assert status.healthy is False
    assert "missing_package.tools:create_missing_tool" in (status.last_error or "")
    rejected = json.loads(config_path.read_text(encoding="utf-8"))
    assert rejected["config_version"] == 1
    assert rejected["agents"]["default"]["tools"]["custom"][0]["factory"] == (
        "missing_package.tools:create_missing_tool"
    )


@pytest.mark.asyncio
async def test_candidate_applier_failure_prevents_snapshot_commit(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)
    original_revision = service.get_revision()
    config["agents"]["default"]["instructions"]["system_prompt"] = "candidate"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    async def reject_candidate(*_args) -> None:
        raise RuntimeError("候选运行时应用失败")

    with pytest.raises(RuntimeError, match="候选运行时应用失败"):
        await service.reload(candidate_applier=reject_candidate)

    assert service.get_revision() == original_revision
    status = service.get_reload_status()
    assert status.healthy is False
    assert status.reason == "apply_failed"
    assert status.restart_required is False


@pytest.mark.asyncio
async def test_restart_required_failure_exposes_changed_sections(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)
    original_revision = service.get_revision()
    config["logger"]["level"] = "debug"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    async def require_restart(*_args) -> None:
        raise ConfigRestartRequiredError(
            "需要重启工作区后端",
            changed_sections=("logger",),
        )

    with pytest.raises(ConfigRestartRequiredError):
        await service.reload(candidate_applier=require_restart)

    status = service.get_reload_status()
    assert service.get_revision() == original_revision
    assert status.healthy is False
    assert status.restart_required is True
    assert status.reason == "restart_required"
    assert status.changed_sections == ("logger",)


@pytest.mark.asyncio
async def test_pinned_snapshot_survives_await_and_runtime_reload(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)
    original_snapshot = service.get_snapshot()
    config["llm"]["providers"][0]["model"] = "model-b"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with service.use_snapshot(original_snapshot):
        assert await service.reload() is True
        await asyncio.sleep(0)
        assert service.get_revision() == original_snapshot.revision
        assert (await service.get()).default_model == "model-a"

    assert service.get_revision() != original_snapshot.revision
    assert (await service.get()).default_model == "model-b"


@pytest.mark.asyncio
async def test_watcher_detects_workspace_config_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    (workspace_root / ".boxteam").mkdir(parents=True)
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=workspace_root,
    )
    original_revision = service.get_revision()

    await service.start_watching()
    try:
        workspace_config_path = workspace_root / ".boxteam" / "workspace.jsonc"
        workspace_config_path.write_text(
            json.dumps(
                {"agents": {"default": {"instructions": {"system_prompt": "hot"}}}}
            ),
            encoding="utf-8",
        )
        for _ in range(40):
            if service.get_revision() != original_revision:
                break
            await asyncio.sleep(0.05)
        assert service.get_revision() != original_revision
        assert service.get_agent_runtime_config("default")["system_prompt"] == "hot"
    finally:
        await service.stop_watching()


@pytest.mark.asyncio
async def test_watcher_does_not_create_missing_user_config_directory(
    tmp_path: Path,
) -> None:
    missing_config_path = tmp_path / "missing-user-config" / "workspace.jsonc"
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=missing_config_path,
    )

    with pytest.raises(FileNotFoundError, match="配置监听目录不存在"):
        await service.start_watching()

    assert missing_config_path.parent.exists() is False
