from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema
import pytest
from watchfiles import Change

import app.services.infrastructure.config_service as config_service_module
from app.agents.policy import ToolMetadata
from app.schemas.internal_v2.config import ConfigUpdateRequest
from app.services.infrastructure.config import ConfigRestartRequiredError
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    SecretReferenceRequiredError,
)
from app.services.infrastructure.config.watcher import ConfigFileWatcher
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


def test_literal_provider_key_is_rejected_before_workspace_sqlite_write(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["llm"]["providers"][0]["api_key"] = "literal-provider-key"
    config_path = _write_workspace_config(tmp_path, config)
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        with pytest.raises(SecretReferenceRequiredError, match="无法安全持久化"):
            service.validate_workspace_config()
        assert store.get_active_config_snapshot("workspace") is None
        assert "literal-provider-key" in config_path.read_text(encoding="utf-8")
    finally:
        store.close()


def test_provider_secret_resolution_observes_environment_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_workspace_config(tmp_path, _base_config())
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    monkeypatch.setenv("TEST_API_KEY", "first-secret")
    first = service.get_llm_provider("primary")
    monkeypatch.setenv("TEST_API_KEY", "second-secret")
    second = service.get_llm_provider("primary")

    assert first["api_key"] == "first-secret"
    assert second["api_key"] == "second-secret"


def test_source_diagnostics_is_read_only_before_config_initialization(
    tmp_path: Path,
) -> None:
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )

        revision, schema_path, sources = service.get_source_diagnostics()

        assert revision == "uninitialized"
        assert schema_path.is_file()
        assert sources[1].presence == "present"
        assert store.get_source_layer("workspace_mutable_override") is None
        assert config_path.with_name("workspace.jsonc.migrated.bak").exists() is False
        assert store.get_active_config_snapshot("workspace") is None
    finally:
        store.close()


def test_config_reads_agent_run_timeout(tmp_path: Path) -> None:
    config = _base_config()
    config["runtime"] = {"agent": {"run": {"timeout_seconds": 12.5}}}
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_workspace_config(tmp_path, config),
    )

    assert service.get_agent_run_timeout_seconds() == 12.5


def test_config_reads_agent_run_mode(tmp_path: Path) -> None:
    config = _base_config()
    config["runtime"] = {"agent": {"run": {"mode": "team"}}}
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_workspace_config(tmp_path, config),
    )

    assert service.get_agent_run_mode() == "team"


def test_config_rejects_unknown_agent_run_mode(tmp_path: Path) -> None:
    config = _base_config()
    config["runtime"] = {"agent": {"run": {"mode": "unexpected"}}}
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_workspace_config(tmp_path, config),
    )

    with pytest.raises(ValueError, match="single_agent 或 team"):
        service.get_agent_run_mode()


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
            config_layer="runtime_override",
            scope="workspace",
            base_layer_revision=None,
            base_layer_digest=None,
            expected_active_revision=None,
            expected_active_digest=None,
            idempotency_key="runtime-update-uses-overrides",
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

    await service.update(
        ConfigUpdateRequest(
            config_layer="runtime_override",
            scope="workspace",
            base_layer_revision=None,
            base_layer_digest=None,
            expected_active_revision=None,
            expected_active_digest=None,
            idempotency_key="runtime-update-set",
            default_model="runtime-model",
        )
    )
    result = await service.update(
        ConfigUpdateRequest(
            config_layer="runtime_override",
            scope="workspace",
            base_layer_revision=None,
            base_layer_digest=None,
            expected_active_revision=None,
            expected_active_digest=None,
            idempotency_key="runtime-update-clear",
            default_model=None,
        )
    )

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
        first.validate_workspace_config()
        active = store.get_active_config_snapshot("workspace")
        assert active is not None
        source = store.get_source_layer("workspace_runtime_override")
        assert source is None
        await first.update(
            ConfigUpdateRequest(
                config_layer="runtime_override",
                scope="workspace",
                default_model="runtime-model",
                base_layer_revision=None,
                base_layer_digest=None,
                expected_active_revision=active.active_revision,
                expected_active_digest=active.effective_digest,
                idempotency_key="runtime-sqlite-set",
            )
        )
        second = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        assert (await second.get()).default_model == "runtime-model"
        active = store.get_active_config_snapshot("workspace")
        source = store.get_source_layer("workspace_runtime_override")
        assert active is not None
        assert source is not None
        await second.update(
            ConfigUpdateRequest(
                config_layer="runtime_override",
                scope="workspace",
                default_model=None,
                base_layer_revision=source.layer_revision,
                base_layer_digest=source.layer_digest,
                expected_active_revision=active.active_revision,
                expected_active_digest=active.effective_digest,
                idempotency_key="runtime-sqlite-clear",
            )
        )
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
async def test_public_runtime_override_rejects_stale_layer_and_active_cas(
    tmp_path: Path,
):
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        active = store.get_active_config_snapshot("workspace")
        assert active is not None
        stale_request = ConfigUpdateRequest(
            config_layer="runtime_override",
            scope="workspace",
            base_layer_revision=None,
            base_layer_digest=None,
            expected_active_revision=active.active_revision,
            expected_active_digest=active.effective_digest,
            idempotency_key="runtime-stale-cas",
            default_model="first-writer",
        )
        await service.update(stale_request)

        with pytest.raises(ConfigConflictError, match="CAS 冲突"):
            await service.update(
                stale_request.model_copy(update={"default_model": "stale-writer"})
            )

        assert (await service.get()).default_model == "first-writer"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_public_runtime_override_replay_is_idempotent(tmp_path: Path):
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        active_before = store.get_active_config_snapshot("workspace")
        assert active_before is not None
        request = ConfigUpdateRequest(
            config_layer="runtime_override",
            scope="workspace",
            base_layer_revision=None,
            base_layer_digest=None,
            expected_active_revision=active_before.active_revision,
            expected_active_digest=active_before.effective_digest,
            idempotency_key="runtime-replay-once",
            default_model="replayed-model",
        )

        await service.update(request)
        active_after = store.get_active_config_snapshot("workspace")
        events_after = store.list_config_events(config_domain="workspace")
        assert active_after is not None
        assert (await service.get()).default_model == "replayed-model"

        await service.update(request)
        active_replayed = store.get_active_config_snapshot("workspace")
        events_replayed = store.list_config_events(config_domain="workspace")

        assert active_replayed is not None
        assert active_replayed.active_revision == active_after.active_revision
        assert active_replayed.effective_digest == active_after.effective_digest
        assert len(events_replayed) == len(events_after)
        assert (await service.get()).default_model == "replayed-model"
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
async def test_source_change_during_apply_fails_final_cas_without_active_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        active_before = store.get_active_config_snapshot("workspace")
        assert active_before is not None
        candidate_config = _base_config()
        candidate_config["ui"] = {"default_orchestration": "multi_agent"}
        config_path.write_text(json.dumps(candidate_config), encoding="utf-8")

        async def mutate_source_during_apply(*_args: object) -> None:
            source = store.get_source_layer("workspace_mutable_override")
            assert source is not None
            store.sync_config_source(
                config_key="workspace_mutable_override",
                source_path=config_path,
                config_version=1,
                presence="present",
                payload={"ui": {"default_orchestration": "single_agent"}},
                layer_digest="concurrent-source-digest",
                expected_layer_revision=source.layer_revision,
                expected_layer_digest=source.layer_digest,
            )

        with pytest.raises(ConfigConflictError, match="source .*CAS"):
            await service.reload(candidate_applier=mutate_source_during_apply)

        active_after = store.get_active_config_snapshot("workspace")
        assert active_after is not None
        assert active_after.active_revision == active_before.active_revision
        pending = store.get_pending_config_candidate(config_domain="workspace")
        assert pending is not None
        assert pending.state == "recovery_required"
        journal = store.get_config_apply_journal(apply_id=pending.last_apply_id or "")
        assert journal is not None
        assert journal.state == "recovery_required"
        assert journal.side_effects
        events = store.list_config_events(config_domain="workspace")
        assert events[-1].result == "recovery_required"
        assert events[-1].applied_paths == ()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_file_change_after_parse_is_rejected_before_source_layer_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        source_before = store.get_source_layer("workspace_mutable_override")
        assert source_before is not None

        original_parse = config_service_module.parse_stable_config_file
        mutated = False

        def parse_then_mutate(snapshot):
            nonlocal mutated
            payload = original_parse(snapshot)
            if snapshot.path == config_path and not mutated:
                mutated = True
                changed = _base_config()
                changed["logger"]["level"] = "debug"
                config_path.write_text(json.dumps(changed), encoding="utf-8")
            return payload

        monkeypatch.setattr(
            config_service_module,
            "parse_stable_config_file",
            parse_then_mutate,
        )

        with pytest.raises(RuntimeError, match="解析后发生变化"):
            await service.reload()

        source_after = store.get_source_layer("workspace_mutable_override")
        assert source_after is not None
        assert source_after.layer_revision == source_before.layer_revision
        assert source_after.layer_digest == source_before.layer_digest
    finally:
        store.close()


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


@pytest.mark.asyncio
async def test_config_watcher_does_not_recurse_into_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / ".boxteam"
    directory.mkdir()
    observed: dict[str, object] = {}

    class WatchLoopStopped(Exception):
        pass

    async def fake_awatch(*paths: Path, **kwargs: object):
        observed["paths"] = paths
        observed.update(kwargs)
        raise WatchLoopStopped
        yield set()  # pragma: no cover

    monkeypatch.setattr(
        "app.services.infrastructure.config.watcher.awatch",
        fake_awatch,
    )
    watcher = ConfigFileWatcher(
        directories=[directory],
        candidate_paths=[directory / "workspace.jsonc"],
        on_change=lambda: asyncio.sleep(0),
    )

    with pytest.raises(WatchLoopStopped):
        await watcher._watch_loop()

    assert observed["paths"] == (directory.resolve(),)
    assert observed["recursive"] is False


def test_config_watcher_only_accepts_exact_candidate_paths(tmp_path: Path) -> None:
    directory = tmp_path / ".boxteam"
    directory.mkdir()
    candidate = directory / "workspace.jsonc"
    watcher = ConfigFileWatcher(
        directories=[directory],
        candidate_paths=[candidate],
        on_change=lambda: asyncio.sleep(0),
    )

    assert watcher._contains_candidate_change(
        {
            (Change.modified, str(candidate)),
            (Change.added, str(directory / ".workspace.jsonc.swp")),
        }
    )
    assert not watcher._contains_candidate_change(
        {
            (Change.added, str(directory / "workspace.jsonc.migrated.bak")),
            (Change.deleted, str(directory / "workspace.jsonc.tmp")),
        }
    )


@pytest.mark.asyncio
async def test_sqlite_migrated_source_change_is_imported_on_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        assert service.get_logger_level() == "INFO"
        config = _base_config()
        config["logger"]["level"] = "error"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        assert service.get_logger_level() == "INFO"
        assert await service.reload() is True
        assert service.get_logger_level() == "ERROR"
        source = store.get_source_layer("workspace_mutable_override")
        assert source is not None
        assert source.presence == "present"
        assert source.layer_revision == 2
        assert source.source_generation == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_sqlite_migrated_source_deletion_writes_absent_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        original_revision = service.get_revision()
        config_path.unlink()

        assert await service.reload() is True
        source = store.get_source_layer("workspace_mutable_override")
        assert source is not None
        assert source.presence == "absent"
        assert source.payload is None
        assert source.layer_revision == 2
        assert service.get_revision() != original_revision
        assert service.get_source_details()[1].presence == "absent"
        deleted_backup = config_path.with_name("workspace.jsonc.deleted.bak")
        assert deleted_backup.is_file()
        assert "test-key" not in deleted_backup.read_text(encoding="utf-8")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_jsonc_rename_to_temp_creates_tombstone_and_restore_reimports_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    user_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    workspace_path = workspace_root / ".boxteam" / "workspace.jsonc"
    workspace_path.parent.mkdir(parents=True)
    workspace_path.write_text(
        json.dumps(
            {"agents": {"default": {"instructions": {"system_prompt": "before"}}}}
        ),
        encoding="utf-8",
    )
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=user_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        original_revision = service.get_revision()
        assert service.get_agent_runtime_config("default")["system_prompt"] == "before"
        temporary_path = workspace_path.with_name("workspace.jsonc.tmp")
        workspace_path.rename(temporary_path)

        assert await service.reload() is True
        assert service.get_revision() != original_revision
        absent = store.get_source_layer("workspace_root_mutable_override")
        assert absent is not None
        assert absent.presence == "absent"
        assert absent.payload is None
        assert (workspace_path.with_name("workspace.jsonc.deleted.bak")).is_file()

        temporary_path.write_text(
            json.dumps(
                {"agents": {"default": {"instructions": {"system_prompt": "after"}}}}
            ),
            encoding="utf-8",
        )
        temporary_path.rename(workspace_path)
        assert await service.reload() is True
        restored = store.get_source_layer("workspace_root_mutable_override")
        assert restored is not None
        assert restored.presence == "present"
        assert service.get_agent_runtime_config("default")["system_prompt"] == "after"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_source_change_during_apply_without_side_effect_fails_before_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        active_before = store.get_active_config_snapshot("workspace")
        assert active_before is not None
        candidate_config = _base_config()
        candidate_config["ui"] = {"default_orchestration": "multi_agent"}
        config_path.write_text(json.dumps(candidate_config), encoding="utf-8")

        original_persist = service._persist_active_snapshot

        def mutate_source_before_promotion(
            snapshot: object,
            **kwargs: object,
        ) -> None:
            source = store.get_source_layer("workspace_mutable_override")
            assert source is not None
            store.sync_config_source(
                config_key="workspace_mutable_override",
                source_path=config_path,
                config_version=1,
                presence="present",
                payload={"ui": {"default_orchestration": "single_agent"}},
                layer_digest="concurrent-source-digest-no-side-effect",
                expected_layer_revision=source.layer_revision,
                expected_layer_digest=source.layer_digest,
            )
            original_persist(snapshot, **kwargs)

        monkeypatch.setattr(
            service,
            "_persist_active_snapshot",
            mutate_source_before_promotion,
        )
        with pytest.raises(ConfigConflictError, match="source .*CAS"):
            await service.reload()

        active_after = store.get_active_config_snapshot("workspace")
        assert active_after is not None
        assert active_after.active_revision == active_before.active_revision
        pending = store.get_pending_config_candidate(config_domain="workspace")
        assert pending is not None
        assert pending.state == "conflict"
        journal = store.get_config_apply_journal(apply_id=pending.last_apply_id or "")
        assert journal is not None
        assert journal.state == "failed"
        assert journal.side_effects == ()
        events = store.list_config_events(config_domain="workspace")
        assert events[-1].result == "conflict"
        assert events[-1].applied_paths == ()
    finally:
        store.close()


def test_config_active_snapshot_is_persisted_redacted_and_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        snapshot = store.get_active_config_snapshot("workspace")
        assert snapshot is not None
        assert snapshot.payload["llm"]["providers"][0]["api_key"].startswith(
            "env:"
        )
        assert "test-key" not in json.dumps(snapshot.payload)
        assert snapshot.effective_digest == service.get_snapshot().revision
        assert snapshot.active_revision == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_restart_required_reload_persists_pending_candidate_without_changing_active(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        active_before = store.get_active_config_snapshot("workspace")
        assert active_before is not None
        config["logger"]["level"] = "debug"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        async def require_restart(*_args) -> None:
            raise ConfigRestartRequiredError(
                "需要重启工作区后端",
                changed_sections=("logger",),
            )

        with pytest.raises(ConfigRestartRequiredError):
            await service.reload(candidate_applier=require_restart)

        active_after = store.get_active_config_snapshot("workspace")
        assert active_after is not None
        assert active_after.active_revision == active_before.active_revision
        pending = store.get_pending_config_candidate(
            config_domain="workspace",
        )
        assert pending is not None
        assert pending.state == "pending_restart"
        assert pending.payload["logger"]["level"] == "debug"
        events = store.list_config_events(config_domain="workspace")
        assert len(events) == 1
        assert events[0].result == "restart_required"
        assert events[0].activation_scope == "restart_workspace"
        assert events[0].applied_paths == ()
        assert events[0].deferred_paths == events[0].changed_paths
    finally:
        store.close()


@pytest.mark.asyncio
async def test_workspace_recovery_pending_requires_matching_proof_before_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        config["logger"]["level"] = "debug"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        async def require_restart(*_args) -> None:
            raise ConfigRestartRequiredError(
                "需要重启工作区后端",
                changed_sections=("logger",),
            )

        with pytest.raises(ConfigRestartRequiredError):
            await service.reload(candidate_applier=require_restart)
        pending = store.get_pending_config_candidate(config_domain="workspace")
        assert pending is not None
        assert pending.candidate_ref is not None
        service.record_pending_restart_failure(
            candidate_ref=pending.candidate_ref,
            error="新 generation 启动失败",
            old_runtime_recovered=False,
        )
        failed = store.get_pending_config_candidate(config_domain="workspace")
        assert failed is not None
        assert failed.state == "recovery_required"

        proof = service._pending_restart_health_proof(
            candidate_ref=pending.candidate_ref,
            pending=failed,
        )
        wrong_proof = {**proof, "candidate_digest": "wrong"}
        with pytest.raises(ConfigConflictError, match="health proof"):
            service.resolve_pending_restart(
                candidate_ref=pending.candidate_ref,
                health_proof=wrong_proof,
            )
        assert store.get_pending_config_candidate(
            config_domain="workspace"
        ).state == "recovery_required"

        service.resolve_pending_restart(
            candidate_ref=pending.candidate_ref,
            health_proof=proof,
        )
        active = store.get_active_config_snapshot("workspace")
        resolved = store.get_pending_config_candidate(config_domain="workspace")
        assert active is not None
        assert resolved is not None
        assert active.effective_digest == resolved.effective_digest
        assert resolved.state == "active"
        assert [event.result for event in store.list_config_events(
            config_domain="workspace"
        )] == ["restart_required", "recovery_required", "applied"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_workspace_pending_discard_requires_active_and_source_baseline(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        config["logger"]["level"] = "debug"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        async def require_restart(*_args) -> None:
            raise ConfigRestartRequiredError(
                "需要重启工作区后端",
                changed_sections=("logger",),
            )

        with pytest.raises(ConfigRestartRequiredError):
            await service.reload(candidate_applier=require_restart)
        pending = store.get_pending_config_candidate(config_domain="workspace")
        active = store.get_active_config_snapshot("workspace")
        assert pending is not None
        assert active is not None
        assert pending.candidate_ref is not None

        with pytest.raises(ConfigConflictError, match="安全 active"):
            service.discard_pending_restart(
                candidate_ref=pending.candidate_ref,
                expected_active_revision=active.active_revision,
                expected_active_digest="stale",
            )
        assert store.get_pending_config_candidate(
            config_domain="workspace"
        ).state == "pending_restart"

        status = service.discard_pending_restart(
            candidate_ref=pending.candidate_ref,
            expected_active_revision=active.active_revision,
            expected_active_digest=active.effective_digest,
        )
        assert status.state == "discarded"
        assert status.reason is None
        discarded = store.get_pending_config_candidate(config_domain="workspace")
        assert discarded is not None
        assert discarded.state == "discarded"
        assert store.list_config_events(config_domain="workspace")[-1].result == (
            "discarded"
        )
        service.discard_pending_restart(
            candidate_ref=pending.candidate_ref,
            expected_active_revision=active.active_revision,
            expected_active_digest=active.effective_digest,
        )
        assert len(store.list_config_events(config_domain="workspace")) == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_workspace_pending_candidate_loader_requires_exact_startup_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        config["logger"]["level"] = "debug"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        async def require_restart(*_args) -> None:
            raise ConfigRestartRequiredError(
                "需要重启工作区后端",
                changed_sections=("logger",),
            )

        with pytest.raises(ConfigRestartRequiredError):
            await service.reload(candidate_applier=require_restart)

        pending = store.get_pending_config_candidate(config_domain="workspace")
        assert pending is not None
        assert pending.candidate_ref is not None
        assert pending.target_generation is not None
        assert pending.fencing_token is not None
        candidate_ref = pending.candidate_ref
        generation = pending.target_generation
        fencing_token = pending.fencing_token
    finally:
        store.close()

    monkeypatch.setenv("BOXTEAM_CONFIG_CANDIDATE_REF", candidate_ref)
    monkeypatch.setenv("BOXTEAM_CONFIG_GENERATION", generation)
    monkeypatch.setenv("BOXTEAM_CONFIG_FENCING_TOKEN", fencing_token)
    restored_store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        restored = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=restored_store,
        )
        assert restored.get_snapshot().to_dict()["logger"]["level"] == "debug"
        proof = restored.get_loaded_config_proof()
        assert proof["loaded_source"] == "pending"
        assert proof["candidate_id"] == pending.candidate_id
        assert proof["effective_digest"] == pending.effective_digest
        assert proof["generation_id"] == generation
        assert proof["fencing_token_digest"]
        assert "test-key" not in json.dumps(proof)
    finally:
        restored_store.close()


def test_workspace_pending_candidate_loader_rejects_wrong_generation_or_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    workspace_root = tmp_path / "workspace"
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        candidate_ref = "candidate-ref"
        store.create_pending_config_candidate(
            config_domain="workspace",
            candidate_id="candidate-1",
            idempotency_key="reload-1",
            payload={"config_version": 1},
            source_baseline={},
            candidate_digest="digest",
            effective_digest="digest",
            target_generation="generation-1",
            fencing_token="fence-1",
            state="candidate_validated",
        )
        store.update_pending_config_candidate_state(
            config_domain="workspace",
            candidate_id="candidate-1",
            expected_state="candidate_validated",
            state="pending_restart",
            candidate_ref=candidate_ref,
        )
    finally:
        store.close()

    monkeypatch.setenv("BOXTEAM_CONFIG_CANDIDATE_REF", candidate_ref)
    monkeypatch.setenv("BOXTEAM_CONFIG_GENERATION", "generation-2")
    monkeypatch.setenv("BOXTEAM_CONFIG_FENCING_TOKEN", "fence-1")
    restored_store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        with pytest.raises(ConfigConflictError, match="target generation"):
            ConfigService(
                config_dir=Path.cwd() / "configs",
                config_path=config_path,
                workspace_root=workspace_root,
                workspace_state_store=restored_store,
            ).get_snapshot()
    finally:
        restored_store.close()
