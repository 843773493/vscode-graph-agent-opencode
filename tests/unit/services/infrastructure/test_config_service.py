from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import NoReturn

import httpx
import jsonschema
import pytest
from boxteam_plugin_sdk.lifecycle import LifetimeScope
from watchfiles import Change

import app.services.infrastructure.config_service.config_reload as config_reload_module
import app.services.infrastructure.config_service.config_source_layers as config_source_layers_module
from app.agents.policy import ToolMetadata
from app.core.mounted_workspaces import MountedWorkspace
from app.core.path_utils import get_workspace_config_path
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.config import ConfigSourceEditRequest, ConfigUpdateRequest
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.config import ConfigRestartRequiredError
from app.services.infrastructure.config.contracts import ConfigPublication
from app.services.infrastructure.config.snapshot import ConfigSnapshot
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventCursorGoneError,
)
from app.services.infrastructure.config.watcher import ConfigFileWatcher
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from configs.installer import (
    install_user_configuration,
    install_workspace_configuration,
)

PRIMARY_MODEL = ModelReferenceDTO(provider_id="primary", model_id="default")
BACKUP_MODEL = ModelReferenceDTO(provider_id="backup", model_id="default")


def _write_workspace_config(tmp_path: Path, config: dict) -> Path:
    install_user_configuration(config_root=tmp_path, profile="default")
    config_path = tmp_path / "workspace.jsonc"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _initialize_workspace_config(workspace_root: Path) -> Path:
    workspace_root.mkdir(parents=True, exist_ok=True)
    return install_workspace_configuration(workspace_root=workspace_root)


def _base_config() -> dict:
    return {
        "config_version": 4,
        "extends": "builtin:workspace",
        "settings": {"workspace": {
            "llm": {"providers": {"primary": {
                "config_ref": "#/profiles/primary",
                "models": {"default": {"model": "model-a", "config_ref": "context:provider/primary"}},
            }}},
            "mcp": {"supervision": {"sealed_call_timeout_seconds": 1800}, "servers": {}},
            "default_agent": "default",
            "agents": {
                **{name: {"model": {"primary": PRIMARY_MODEL.model_dump(), "fallbacks": []}}
                   for name in ("coder", "reviewer", "researcher")},
                "default": {
                    "name": "Default Agent",
                    "instructions": {"system_prompt": "hello"},
                    "model": {
                        "primary": PRIMARY_MODEL.model_dump(),
                        "fallbacks": [],
                    },
                },
            },
        }},
        "profiles": {"primary": {"extends": "context:workspace", "settings": {"model_options": {
                    "endpoint": "https://example.com/v1",
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
        }}}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("launched_url", [None, "http://127.0.0.1:18030"])
async def test_plugin_refresh_uses_bound_gateway_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launched_url: str | None,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    if launched_url is None:
        monkeypatch.delenv("BOXTEAM_GATEWAY_URL", raising=False)
    else:
        monkeypatch.setenv("BOXTEAM_GATEWAY_URL", launched_url)
    config = _base_config()
    configured_url = "http://127.0.0.1:18014"
    config["settings"]["workspace"]["runtime"] = {"gateway": {"connection": {"url": configured_url}}}
    service = ConfigService(
        config_dir=Path.cwd() / "configs", config_path=_write_workspace_config(tmp_path, config),
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": True})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(config_reload_module.httpx, "AsyncClient", lambda **kwargs:
                        client_type(**kwargs, transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(config_reload_module, "get_gateway_local_token", lambda: "test-token")
    await service._notify_gateway_workspace_plugins(workspace_id="workspace-test", candidate_id="candidate-test")
    assert len(requests) == 1
    assert str(requests[0].url) == (
        f"{launched_url or configured_url}/api/internal/service-plugins/workspaces/workspace-test/refresh"
    )
    assert requests[0].headers["X-Request-ID"] == "config-candidate-test"


@pytest.mark.parametrize("legacy_logger", [{"level": "info"}, {"level": "verbose"}, {"pretty": False}])
def test_workspace_logger_is_rejected_by_workspace_schema(tmp_path: Path, legacy_logger: dict) -> None:
    config = {**_base_config(), "logger": legacy_logger}
    path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=path)
    with pytest.raises(ValueError, match="配置节点校验失败"):
        service.get_snapshot()


def test_legacy_workspace_layout_is_not_used_as_config_entry(tmp_path: Path) -> None:
    legacy_path = tmp_path / ".boxteam" / "boxteam.jsonc"
    legacy_path.parent.mkdir()
    legacy_path.write_text('{"config_version": 1}\n', encoding="utf-8")
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        workspace_root=tmp_path,
    )

    with pytest.raises(FileNotFoundError, match="声明的配置来源不存在"):
        service.get_snapshot()

    assert legacy_path.is_file()
    assert not (legacy_path.parent / "workspace.jsonc").exists()


def _append_test_event(store: WorkspaceStateStore, event_id: str) -> None:
    store.append_config_event(
        event_id=event_id,
        config_domain="workspace",
        candidate_id=None,
        attempt_id=None,
        apply_id=None,
        idempotency_key=None,
        commit_revision=None,
        active_revision=None,
        pending_revision=None,
        source="test",
        result="unchanged",
    )


def test_config_event_cursor_family_uses_store_state_gate_and_cursor(
    tmp_path: Path,
) -> None:
    """游标族必须逐字转发 store 契约：after 语义、消费者隔离与投递幂等。"""

    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=tmp_path / "workspace.jsonc",
            workspace_root=tmp_path / "workspace",
            workspace_state_store=store,
        )
        storeless = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=tmp_path / "workspace.jsonc",
            workspace_root=tmp_path / "workspace",
        )
        for index in range(4):
            _append_test_event(store, f"event-{index}")

        assert [event.event_seq for event in service.list_config_events()] == [
            1,
            2,
            3,
            4,
        ]
        # after 必须逐字生效，不能被丢弃或改成 0
        assert [
            event.event_seq for event in service.list_config_events(after=2)
        ] == [3, 4]
        assert [
            event.event_seq
            for event in service.list_config_events(after=2, limit=1)
        ] == [3]
        assert list(service.list_config_events(after=99)) == []

        # 两个消费者独立认领，互不影响
        assert [
            event.event_seq
            for event in service.claim_config_events_for_consumer(
                after=0, consumer_id="consumer-a"
            )
        ] == [1, 2, 3, 4]
        assert [
            event.event_seq
            for event in service.claim_config_events_for_consumer(
                after=0, consumer_id="consumer-b", limit=2
            )
        ] == [1, 2]
        assert list(
            service.claim_config_events_for_consumer(
                after=0, consumer_id="consumer-a"
            )
        ) == []

        # 投递确认必须幂等，重复确认不得改变游标
        first = service.mark_config_event_delivered_for_consumer(
            event_id="event-2", consumer_id="consumer-a"
        )
        repeated = service.mark_config_event_delivered_for_consumer(
            event_id="event-2", consumer_id="consumer-a"
        )
        assert first.event_id == repeated.event_id == "event-2"

        service.ensure_config_event_cursor(after=0)
        service.ensure_config_event_cursor(after=1)
        # 事件被裁剪后，越界游标必须由 store 报 CursorGone，且带 first_available
        connection = store.connection()
        try:
            connection.execute(
                "DELETE FROM config_events WHERE event_seq IN (1, 2)"
            )
        finally:
            connection.close()
        with pytest.raises(ConfigEventCursorGoneError) as gone:
            service.ensure_config_event_cursor(after=1)
        assert gone.value.first_available == 3
        with pytest.raises(ConfigEventCursorGoneError):
            service.list_config_events(after=1)
        assert [
            event.event_seq for event in service.list_config_events(after=2)
        ] == [3, 4]

        assert list(storeless.list_config_events()) == []
        assert (
            list(
                storeless.claim_config_events_for_consumer(
                    after=0, consumer_id="consumer-a"
                )
            )
            == []
        )
        storeless.ensure_config_event_cursor(after=0)
        with pytest.raises(RuntimeError, match="配置事件状态库"):
            storeless.mark_config_event_delivered_for_consumer(
                event_id="event-2", consumer_id="consumer-a"
            )
    finally:
        store.close()


def test_config_accepts_chatgpt_oauth_provider_without_api_key(tmp_path: Path):
    config = _base_config()
    config["settings"]["workspace"]["llm"]["providers"]["backup_4"] = {
        "config_ref": "#/profiles/backup_4",
        "models": {"default": {"model": "gpt-5.6-luna", "config_ref": "context:provider/backup_4"}},
    }
    config["profiles"]["backup_4"] = {"extends": "context:workspace", "settings": {"model_options": {
            "endpoint": "https://chatgpt.com/backend-api/codex",
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
    }}}
    config_path = _write_workspace_config(tmp_path, config)

    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    service.validate_workspace_config()
    assert service.get_llm_model(ModelReferenceDTO(provider_id="backup_4", model_id="default"))["auth"]["method"] == "chatgpt"


def test_literal_provider_key_is_rejected_before_publishing_workspace_snapshot(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["profiles"]["primary"]["settings"]["model_options"]["api_key"] = "literal-provider-key"
    config_path = _write_workspace_config(tmp_path, config)
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        with pytest.raises(ValueError, match="配置节点校验失败"):
            service.validate_workspace_config()
        assert store.get_active_config_snapshot("workspace") is None
        assert json.loads(config_path.read_text(encoding="utf-8")) == config
    finally:
        store.close()


def test_provider_secret_resolution_observes_environment_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_workspace_config(tmp_path, _base_config())
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    monkeypatch.setenv("TEST_API_KEY", "first-secret")
    with service.use_llm_credentials({}):
        first = service.get_llm_model(PRIMARY_MODEL)
    monkeypatch.setenv("TEST_API_KEY", "second-secret")
    with service.use_llm_credentials({}):
        second = service.get_llm_model(PRIMARY_MODEL)

    assert first["api_key"] == "first-secret"
    assert second["api_key"] == "second-secret"


def test_source_diagnostics_is_read_only_before_config_initialization(
    tmp_path: Path,
) -> None:
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
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
        assert all(source.presence == "present" for source in sources)
        assert store.get_source_layer("workspace_mutable_override") is None
        assert config_path.with_name("workspace.jsonc.migrated.bak").exists() is False
        assert store.get_active_config_snapshot("workspace") is None
    finally:
        store.close()


@pytest.mark.parametrize(("policy", "seconds"), [
    ({"mode": "event", "poll_interval": None}, None),
    ({"mode": "manual", "poll_interval": None}, None),
    ({"mode": "poll", "poll_interval": "7m"}, 420),
    ({"mode": "event_poll", "poll_interval": "2h"}, 7200),
])
def test_gateway_skill_policy_loads_through_effective_workspace_config(tmp_path: Path, policy: dict, seconds: int | None) -> None:
    config = _base_config()
    config["settings"]["workspace"]["resources"] = {"skill_sources": {"gateway_updates": policy}}
    path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=path)
    actual = service.get_gateway_skill_update_policy()
    assert actual.model_dump() == policy and actual.poll_seconds == seconds


@pytest.mark.parametrize("policy", [
    {"mode": "unknown"}, {"mode": "poll"}, {"mode": "event_poll"},
    {"mode": "event", "poll_interval": "1m"}, {"mode": "manual", "poll_interval": "1h"},
    *({"mode": "poll", "poll_interval": interval} for interval in ["0m", "-1h", "1s", "1.5m", "1", 60]),
    {"mode": "event", "protocol": "unknown"},
])
def test_gateway_skill_policy_rejects_invalid_effective_combinations(tmp_path: Path, policy: dict) -> None:
    config = _base_config()
    config["settings"]["workspace"]["resources"] = {"skill_sources": {"gateway_updates": policy}}
    path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=path)
    with pytest.raises(ValueError):
        service.get_gateway_skill_update_policy()


def test_gateway_skill_update_policy_defaults_to_events_and_reports_current_scope(tmp_path: Path) -> None:
    from app.services.infrastructure.config.policy import workspace_config_policy

    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=_write_workspace_config(tmp_path, _base_config()))
    assert service.get_gateway_skill_update_policy().model_dump() == {"mode": "event", "poll_interval": None}
    decision, = workspace_config_policy().classify(("/resources/skill_sources/gateway_updates/mode",))
    assert not decision.policy_missing and decision.policy == "immediate_read" and decision.activation_scope == "current"


def test_private_http_sources_load_through_effective_workspace_config(tmp_path: Path) -> None:
    from app.services.infrastructure.config.policy import workspace_config_policy

    sources = [{"id": "private-one", "name": "demo", "priority": 2,
                "fetch": {"protocol": "http", "url": "https://example.test/SKILL.md"},
                "updates": {"mode": "poll", "poll_interval": "7m"}}]
    config = _base_config()
    config["settings"]["workspace"]["resources"] = {"skill_sources": {"sources": sources}}
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=_write_workspace_config(tmp_path, config))
    assert service._get_effective_config()["resources"]["skill_sources"]["sources"] == sources
    for path in ("/resources/skill_sources/sources", "/resources/skill_sources/sources/0/fetch/url"):
        decision, = workspace_config_policy().classify((path,))
        assert not decision.policy_missing and decision.policy == "immediate_read" and decision.activation_scope == "current"


@pytest.mark.parametrize("change", [
    {"fetch": {"protocol": "ftp", "url": "https://example.test/SKILL.md"}},
    {"fetch": {"protocol": "http", "url": "https://example.test/#fragment"}},
    {"updates": {"mode": "event"}}, {"updates": {"mode": "poll"}},
    {"updates": {"mode": "manual", "poll_interval": "1h"}}, {"priority": True},
])
def test_private_http_sources_reject_invalid_effective_config(tmp_path: Path, change: dict) -> None:
    source = {"id": "private-one", "name": "demo", "fetch": {"protocol": "http", "url": "https://example.test/SKILL.md"}}
    sources = [{**source, **change}]
    config = _base_config()
    config["settings"]["workspace"]["resources"] = {"skill_sources": {"sources": sources}}
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=_write_workspace_config(tmp_path, config))
    with pytest.raises((ValueError, jsonschema.ValidationError)):
        service._get_effective_config()


def test_private_http_source_ids_are_unique_in_effective_workspace_config(tmp_path: Path) -> None:
    source = {"id": "private-one", "name": "demo", "fetch": {"protocol": "http", "url": "https://example.test/SKILL.md"}}
    config = _base_config()
    config["settings"]["workspace"]["resources"] = {"skill_sources": {"sources": [source, source]}}
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=_write_workspace_config(tmp_path, config))
    with pytest.raises(ValueError, match="id 重复"):
        service._get_effective_config()


def test_config_reads_agent_run_timeout(tmp_path: Path) -> None:
    config = _base_config()
    config["settings"]["workspace"]["runtime"] = {"agent": {"run": {"timeout_seconds": 12.5}}}
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_workspace_config(tmp_path, config),
    )

    assert service.get_agent_run_timeout_seconds() == 12.5


def test_config_reads_agent_run_mode(tmp_path: Path) -> None:
    config = _base_config()
    config["settings"]["workspace"]["runtime"] = {"agent": {"run": {"mode": "team"}}}
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_workspace_config(tmp_path, config),
    )

    assert service.get_agent_run_mode() == "team"


@pytest.mark.parametrize("grouping", [None, "aggregate", "separate"])
def test_source_message_grouping_uses_workspace_layers(tmp_path: Path, grouping: str | None) -> None:
    config = _base_config()
    if grouping is not None:
        config["settings"]["workspace"]["context"] = {"source_messages": {"grouping": grouping}}
    service = ConfigService(
        config_dir=Path.cwd() / "configs", config_path=_write_workspace_config(tmp_path, config),
    )
    assert service.get_source_message_grouping() == (grouping or "aggregate")
    assert service.get_snapshot().to_dict()["config_version"] == 4


def test_source_message_grouping_rejects_unknown_mode(tmp_path: Path) -> None:
    config = _base_config()
    config["settings"]["workspace"]["context"] = {"source_messages": {"grouping": "auto"}}
    service = ConfigService(
        config_dir=Path.cwd() / "configs", config_path=_write_workspace_config(tmp_path, config),
    )
    with pytest.raises(ValueError, match="配置节点校验失败"):
        service.validate_workspace_config()


def test_config_rejects_unknown_agent_run_mode(tmp_path: Path) -> None:
    config = _base_config()
    config["settings"]["workspace"]["runtime"] = {"agent": {"run": {"mode": "unexpected"}}}
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_workspace_config(tmp_path, config),
    )

    with pytest.raises(ValueError, match="配置节点校验失败"):
        service.get_agent_run_mode()


def test_config_rejects_provider_without_api_key_or_oauth(tmp_path: Path):
    config = _base_config()
    options = config["profiles"]["primary"]["settings"]["model_options"].copy()
    del options["api_key"]
    config["settings"]["workspace"]["llm"]["providers"]["missing_key"] = {
        "config_ref": "#/profiles/missing_key",
        "models": {"default": {"model": "model-a", "config_ref": "context:provider/missing_key"}},
    }
    config["profiles"]["missing_key"] = {"extends": "context:workspace", "settings": {"model_options": options}}
    config_path = _write_workspace_config(tmp_path, config)

    with pytest.raises(ValueError, match="配置节点校验失败") as failure:
        ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
        ).validate_workspace_config()
    assert isinstance(failure.value.__cause__, jsonschema.ValidationError)
    assert "api_key" in str(failure.value.__cause__)


def test_tool_policy_resolver_merges_workspace_and_agent_rules(tmp_path: Path):
    config = _base_config()
    config["settings"]["workspace"]["tooling"] = {
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
    config["settings"]["workspace"]["agents"]["default"]["tools"] = {
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
    # real path 不再对外：metadata 既无 config_path，也无 source_paths；
    # 来源位置只以 source_details[].uri 表达。
    assert "config_path" not in result.metadata
    assert "source_paths" not in result.metadata
    source_details = result.metadata["source_details"]
    assert source_details[0]["uri"].startswith("boxteam://inline/")
    assert all(detail["uri"].startswith("boxteam://") for detail in source_details[1:])
    assert str(config_path) not in result.model_dump_json()


@pytest.mark.asyncio
async def test_update_public_config_uses_runtime_overrides(tmp_path: Path):
    config = _base_config()
    config["settings"]["workspace"]["ui"] = {
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


def test_workspace_parent_chain_applies_local_then_workspace_values(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 1900
    config_path = _write_workspace_config(tmp_path, config)
    local_path = tmp_path / "workspace_local.jsonc"
    local_path.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace",
        "settings": {"workspace": {
            "mcp": {"supervision": {"sealed_call_timeout_seconds": 2000}},
            "agents": {"default": {"name": "Local Agent"}},
        }},
    }), encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_path = workspace_root / ".boxteam" / "workspace.jsonc"
    _initialize_workspace_config(workspace_root)
    workspace_path.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace_local",
        "settings": {"workspace": {"agents": {"default": {"name": "Workspace Agent"}}}},
    }), encoding="utf-8")

    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=workspace_root,
    )

    assert service.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"] == 2000
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


def test_workspace_config_syncs_reachable_sources_to_sqlite(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 1900
    config_path = _write_workspace_config(tmp_path, config)
    local_path = tmp_path / "workspace_local.jsonc"
    local_path.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace",
        "settings": {"workspace": {"mcp": {"supervision": {"sealed_call_timeout_seconds": 2000}}}},
    }), encoding="utf-8")
    workspace_root = tmp_path / "workspace"
    workspace_path = workspace_root / ".boxteam" / "workspace.jsonc"
    _initialize_workspace_config(workspace_root)
    workspace_path.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace_local",
        "settings": {"workspace": {"agents": {"default": {"name": "Workspace Agent"}}}},
    }), encoding="utf-8")
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )

        assert service.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"] == 2000
        assert service.list_agents()["default"]["name"] == "Workspace Agent"
        assert [source.layer for source in service.get_source_details()] == [
            "inline", "user", "user_local", "workspace",
        ]
        details = service.get_source_details()
        assert details[0].uri.startswith("boxteam://inline/")
        for source in details[1:]:
            assert source.uri.startswith("boxteam://")
            assert store.get_source_layer(source.source_key) is not None
        assert not list(tmp_path.rglob("*.migrated.bak"))

        service.validate_workspace_config()
        config["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 2500
        config_path.write_text(json.dumps(config), encoding="utf-8")
        assert service.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"] == 2000
    finally:
        store.close()


def test_config_source_layer_consistent_across_read_paths(
    tmp_path: Path,
) -> None:
    """恢复后的来源身份与实际父链来源一致。"""

    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        first = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        first.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"]
        first.validate_workspace_config()
        assert store.get_active_config_snapshot("workspace") is not None

        # 同一 state store、未初始化的新 service 直接取诊断：命中
        # `get_source_diagnostics` 的 `_persisted_source_details` 分支。
        restored = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        _, _, restored_sources = restored.get_source_diagnostics()

        first_sources = first.get_source_details()
        assert {
            source.source_key: (source.uri, source.layer) for source in restored_sources
        } == {
            source.source_key: (source.uri, source.layer) for source in first_sources
        }
        assert {source.layer for source in restored_sources} == {
            "inline", "user", "user_local", "workspace",
        }
    finally:
        store.close()


def test_workspace_config_restarts_from_active_snapshot_after_source_json_changes(
    tmp_path: Path,
) -> None:
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        first = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        first.validate_workspace_config()
        assert store.get_active_config_snapshot("workspace") is not None
        assert first.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"] == 1800
        config_path.write_text("{ this is not valid jsonc", encoding="utf-8")
        second = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        assert second.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"] == 1800
    finally:
        store.close()


@pytest.mark.parametrize(("invalid_source", "exception"), [
    ("{ this is not valid jsonc", ValueError),
    ("[]", TypeError),
])
def test_cold_start_rejects_invalid_source_without_committed_active_snapshot(
    tmp_path: Path, invalid_source: str, exception: type[Exception],
) -> None:
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        first = ConfigService(
            config_dir=Path.cwd() / "configs", config_path=config_path,
            workspace_root=workspace_root, workspace_state_store=store,
        )
        # 来源已导入、active 尚未发布，模拟首次启动在两者之间退出。
        assert first.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"] == 1800
        source = store.get_source_layer("workspace_mutable_override")
        assert source is not None
        assert store.get_active_config_snapshot("workspace") is None
        store.close()
        store = WorkspaceStateStore(workspace_root=workspace_root)
        config_path.write_text(invalid_source, encoding="utf-8")
        recovered = ConfigService(
            config_dir=Path.cwd() / "configs", config_path=config_path,
            workspace_root=workspace_root, workspace_state_store=store,
        )
        for _ in range(2):
            with pytest.raises(exception):
                recovered.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"]
            unchanged = store.get_source_layer("workspace_mutable_override")
            assert unchanged is not None
            # 既有秘密迁移会刷新更新时间，但不得发布损坏来源的任何业务字段。
            assert unchanged == replace(source, updated_at=unchanged.updated_at)
            assert store.get_active_config_snapshot("workspace") is None
            assert store.get_pending_config_candidate(config_domain="workspace") is None
        # 修复源文件后，原 owner 可以从真实新内容建立唯一 active。
        fixed = _base_config()
        fixed["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 2000
        config_path.write_text(json.dumps(fixed), encoding="utf-8")
        recovered.validate_workspace_config()
        assert recovered.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"] == 2000
        assert store.get_active_config_snapshot("workspace") is not None
        changed = store.get_source_layer("workspace_mutable_override")
        assert changed.layer_revision == source.layer_revision + 1
        assert changed.layer_digest != source.layer_digest
    finally:
        store.close()


@pytest.mark.asyncio
async def test_public_runtime_overrides_restart_from_workspace_sqlite(tmp_path: Path):
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
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
        runtime_source = next(
            source
            for source in second.get_source_details()
            if source.source_key == "workspace_runtime_override"
        )
        assert (runtime_source.layer, runtime_source.precedence) == (
            "runtime_override",
            4,
        )
        assert runtime_source.uri is None
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
    _initialize_workspace_config(workspace_root)
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
    _initialize_workspace_config(workspace_root)
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
    config["settings"]["workspace"]["agents"]["default"]["model"]["primary"] = {"provider_id": "missing", "model_id": "default"}
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(
        ValueError, match="引用了不存在的 Provider: missing"
    ):
        await service.get()


def test_get_agent_tool_config_reads_custom_tools(tmp_path: Path):
    config = _base_config()
    config["settings"]["workspace"]["agents"]["default"]["tools"] = {
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
    config["settings"]["workspace"]["agents"]["default"]["tools"] = {
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
    config["settings"]["workspace"]["agents"]["default"]["tools"] = {
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
    config["settings"]["workspace"]["agents"]["default"]["tools"] = {
        "denylist": ["mcp__missing__tool"],
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(ValueError, match="mcp__missing__tool"):
        service.set_mcp_tool_names(frozenset({"mcp__mini__echo"}))


def test_config_loading_fails_on_delegation_dependency_conflict(tmp_path: Path):
    config = _base_config()
    config["settings"]["workspace"]["agents"]["default"]["tools"] = {
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
        "app.services.infrastructure.config_service.config_agent_tools.load_custom_tool_factory",
        lambda _factory_path: object(),
    )
    config = _base_config()
    config["settings"]["workspace"]["agents"]["default"]["tools"] = {
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
        "app.services.infrastructure.config_service.config_agent_tools.load_custom_tool_factory",
        lambda _factory_path: object(),
    )
    config = _base_config()
    config["settings"]["workspace"]["agents"]["default"]["tools"] = {
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
    config["settings"]["workspace"]["agents"]["default"]["tools"] = {
        "custom": [
            {"name": "web_search", "factory": "example:create_web_search"},
            {"name": " web_search ", "factory": "example:create_other"},
        ]
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(ValueError, match="配置源扩展工具声明无效") as failure:
        service.list_agents()
    assert "重复扩展工具名: web_search" in str(failure.value.__cause__)
    assert str(config_path) not in str(failure.value)


def test_workspace_config_overrides_user_global_config(
    tmp_path: Path,
) -> None:
    global_config = _base_config()
    global_config["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 1900
    global_path = tmp_path / "home" / "config" / "workspace.jsonc"
    global_path.parent.mkdir(parents=True)
    install_user_configuration(config_root=global_path.parent, profile="default")
    global_path.write_text(json.dumps(global_config), encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
    workspace_config = workspace_root / ".boxteam" / "workspace.jsonc"
    workspace_config.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace_local",
        "settings": {"workspace": {
            "agents": {"default": {"name": "Workspace Override"}},
            "development": {"test_tools": True},
        }},
    }), encoding="utf-8")

    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=global_path,
        workspace_root=workspace_root,
    )

    assert service.list_agents()["default"]["name"] == "Workspace Override"
    assert service.list_agents()["default"]["model"]["primary"] == PRIMARY_MODEL.model_dump()
    assert service.development_test_tools_enabled() is True






def test_web_fetch_config_needs_no_embedding_options(tmp_path: Path) -> None:
    config = _base_config()
    config["settings"]["workspace"]["agents"]["default"]["tools"] = {
        "custom": [
            {
                "tool_id": "fetch_webpage",
            }
        ]
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    service.validate_workspace_config()


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["environment", "gateway"])
async def test_resolve_llm_model_only_prepares_selected_model_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential: str,
) -> None:
    from app.schemas.gateway.secrets import ResolvedSecret
    from app.services.infrastructure.gateway_secrets.client import GatewaySecretsClient

    config = _base_config()
    secret_id = "sec_00000000000040008000000000000001"
    config["settings"]["workspace"]["llm"]["providers"]["embedding"] = {
        "config_ref": "#/profiles/embedding",
        "models": {
            "default": {"model": "embedding-model", "config_ref": "#/profiles/embedding_default"},
            "unused": {"model": "unused-model", "config_ref": "#/profiles/embedding_unused"},
        },
    }
    config["profiles"]["embedding"] = {"extends": "context:workspace", "settings": {"model_options": {
            "endpoint": "https://embedding.example.com/v1",
            "api_key": "${EMBEDDING_API_KEY}",
            "custom_llm_provider": "openai",
            "api_mode": {"protocol": "chat_completions",
                         "model_info": {"supports_function_calling": False, "supports_reasoning": False},
                         "supports_reasoning": {}},
    }}}
    config["profiles"]["embedding_default"] = {"extends": "context:provider/embedding", "settings": {"model_options": {
        "api_key": "${EMBEDDING_API_KEY}" if credential == "environment" else "secret:" + secret_id,
    }}}
    config["profiles"]["embedding_unused"] = {"extends": "context:provider/embedding", "settings": {"model_options": {
        "api_key": "${UNUSED_EMBEDDING_API_KEY}",
    }}}
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    monkeypatch.delenv("UNUSED_EMBEDDING_API_KEY", raising=False)
    monkeypatch.setenv("EMBEDDING_API_KEY", "embedding-secret")
    resolved_ids = []

    async def resolve(self, ids):
        resolved_ids.extend(ids)
        return {secret_id: ResolvedSecret(revision=7, value="gateway-embedding-secret")}

    monkeypatch.setattr(GatewaySecretsClient, "resolve", resolve)
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    reference = ModelReferenceDTO(provider_id="embedding", model_id="default")
    before = service.get_llm_model(reference)
    resolved = await service.resolve_llm_model(reference)
    assert resolved["provider_id"] == "embedding" and resolved["model_id"] == "default"
    assert resolved["api_key"] == ("embedding-secret" if credential == "environment" else "gateway-embedding-secret")
    assert resolved_ids == ([] if credential == "environment" else [secret_id])
    assert service.get_llm_model(reference) == before


def test_model_request_parameters_schema_accepts_raw_completion_parameters(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["profiles"]["primary"]["settings"]["model_options"]["request_options"] = {
        "parameters": {
            "temperature": 1,
            "max_tokens": None,
            "extra_body": {
                "reasoning": True,
                "extension_limit": 1200,
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


@pytest.fixture
def runtime_provider_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConfigService:
    monkeypatch.setenv("TEST_API_KEY", "initial-key")
    config = _base_config()
    options = config["profiles"]["primary"]["settings"]["model_options"]
    options["request_options"] = {"default_headers": {"X-Runtime": "original"}}
    config["settings"]["workspace"]["llm"]["providers"]["backup"] = {
        "config_ref": "#/profiles/backup", "models": {"default": {
            "model": "model-b", "config_ref": "context:provider/backup",
        }},
    }
    config["profiles"]["backup"] = {"extends": "context:workspace", "settings": {"model_options": options.copy()}}
    config["settings"]["workspace"]["agents"]["default"]["model"]["fallbacks"] = [BACKUP_MODEL.model_dump()]
    service = ConfigService(config_dir=Path.cwd() / "configs",
                            config_path=_write_workspace_config(tmp_path, config))
    service.get_revision()
    return service


@pytest.mark.parametrize("preferred", [None, "backup"])
@pytest.mark.parametrize("agent_id", [None, "default"])
def test_runtime_provider_resolution_reuses_snapshot_and_keeps_results_isolated(
    runtime_provider_service: ConfigService, monkeypatch: pytest.MonkeyPatch, preferred: str | None,
    agent_id: str | None,
) -> None:
    service = runtime_provider_service
    decode = ConfigSnapshot.to_dict
    reads = []

    def observe(snapshot):
        reads.append(snapshot.revision)
        return decode(snapshot)

    monkeypatch.setattr(ConfigSnapshot, "to_dict", observe)
    preferred_model = BACKUP_MODEL if preferred else None
    with service.use_llm_credentials({}):
        first = service.get_agent_runtime_config(agent_id, preferred_model=preferred_model)
    assert reads == []
    expected_order = ["backup", "primary"] if preferred else ["primary", "backup"]
    assert [model["provider_id"] for model in first["models"]] == expected_order
    assert all(model["model_id"] == "default" for model in first["models"])
    assert first["system_prompt"] == "hello"
    assert all(model["api_key"] == "initial-key" for model in first["models"])
    first["models"][0]["request_options"]["default_headers"]["x-runtime"] = "caller mutation"

    # 不缓存展开后的凭据或可变配置；同一 revision 的下一次读取仍反映环境变化。
    monkeypatch.setenv("TEST_API_KEY", "updated-key")
    with service.use_llm_credentials({}):
        second = service.get_agent_runtime_config(agent_id, preferred_model=preferred_model)
    assert reads == []
    assert [model["provider_id"] for model in second["models"]] == expected_order
    assert all(model["api_key"] == "updated-key" for model in second["models"])
    assert second["models"][0]["request_options"]["default_headers"]["x-runtime"] == "original"
    with service.use_llm_credentials({}):
        assert service.get_llm_model(PRIMARY_MODEL)["api_key"] == "updated-key"


def test_model_catalog_warm_reads_do_not_read_sources_or_copy_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    provider_ids = ("primary", *(f"provider-{index}" for index in range(19)))
    model_ids = ("default", *(f"model-{index}" for index in range(9)))
    config["settings"]["workspace"]["llm"]["providers"] = {
        provider_id: {"config_ref": f"#/profiles/{provider_id}", "models": {
            model_id: {"model": "upstream-model", "config_ref": f"context:provider/{provider_id}"}
            for model_id in model_ids
        }} for provider_id in provider_ids
    }
    config["profiles"] = {
        provider_id: {"extends": "context:workspace", "settings": {
            "model_options": config["profiles"]["primary"]["settings"]["model_options"],
        }} for provider_id in provider_ids
    }
    inline = config_source_layers_module.read_jsonc_object(Path.cwd() / "configs/workspace_inline.jsonc")
    inline["settings"]["workspace"]["llm"]["providers"] = {}
    inline_path = tmp_path / "workspace_inline.jsonc"
    inline_path.write_text(json.dumps(inline), encoding="utf-8")
    service = ConfigService(config_dir=Path.cwd() / "configs", inline_config_path=inline_path,
                            config_path=_write_workspace_config(tmp_path, config))
    initial = service.list_llm_models()
    assert len(initial) == 200
    with pytest.raises(TypeError):
        service.get_snapshot().read_only["llm"]["providers"]["primary"]["config_ref"] = "#/profiles/changed"

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("模型暖读不得读取来源或重新物化整份工作区配置")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(ConfigSnapshot, "to_dict", forbidden)
    initial[0]["api_mode"]["model_info"]["supports_reasoning"] = False
    for _ in range(20):
        models = service.list_llm_models()
        assert len(models) == 200
        assert all(model["api_mode"]["model_info"]["supports_reasoning"] for model in models)
        assert service.get_llm_model(PRIMARY_MODEL)["model"] == "upstream-model"
        assert service.get_agent_runtime_config()["models"][0]["model"] == "upstream-model"


@pytest.mark.parametrize("agent_id", ["deep_agent", "removed", ""])
def test_agent_identity_is_not_replaced_by_default_or_unrelated_credential_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_id: str,
) -> None:
    monkeypatch.delenv("UNRELATED_AGENT_PROVIDER_KEY", raising=False)
    config = _base_config()
    config["profiles"]["primary"]["settings"]["model_options"]["api_key"] = "${UNRELATED_AGENT_PROVIDER_KEY}"
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=_write_workspace_config(tmp_path, config))
    assert service.get_default_agent_id() == "default"
    assert service.validate_agent_id(None) == "default"
    for resolve in (service.validate_agent_id, service.get_agent_runtime_config):
        with pytest.raises(ValueError, match=f"agent {agent_id} 不存在"):
            resolve(agent_id)


def test_invalid_config_default_agent_is_not_replaced_by_another_definition(tmp_path: Path) -> None:
    config = _base_config()
    config["settings"]["workspace"]["default_agent"] = "removed"
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=_write_workspace_config(tmp_path, config))
    with pytest.raises(ValueError, match="default_agent 引用了不存在的 Agent: removed"):
        service.get_default_agent_id()


def test_instruction_inputs_use_merged_configuration_without_python_prompt_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    instructions = config["settings"]["workspace"]["agents"]["default"]["instructions"]
    instructions["runtime_identity"] = {"description": "我的路径说明"}
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)
    runtime = service.get_agent_runtime_config("default")
    assert runtime["system_prompt"] == "hello"
    assert runtime["runtime_identity_description"] == "我的路径说明"
    # 实际加载结果缺少指令不能退回另一份隐藏的英文正文。
    damaged = service._get_effective_config()
    damaged["agents"]["default"]["instructions"].pop("system_prompt")
    with (
        service.use_snapshot(replace(service.get_snapshot(), canonical_json=json.dumps(damaged))),
        pytest.raises(KeyError, match="system_prompt"),
    ):
        service.get_agent_runtime_config("default")


def test_agent_runtime_can_require_delegated_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    config["settings"]["workspace"]["agents"]["default"]["execution"] = {
        "require_delegated_report": True,
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    runtime = service.get_agent_runtime_config("default")

    assert runtime["require_delegated_report"] is True


def test_agent_runtime_places_session_model_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    config["settings"]["workspace"]["llm"]["providers"]["backup"] = {
        "config_ref": "#/profiles/backup", "models": {"default": {
            "model": "model-b", "config_ref": "context:provider/backup",
        }},
    }
    config["profiles"]["backup"] = {"extends": "context:workspace", "settings": {"model_options":
        config["profiles"]["primary"]["settings"]["model_options"].copy()}}
    config["settings"]["workspace"]["agents"]["default"]["model"]["fallbacks"] = [BACKUP_MODEL.model_dump()]
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    runtime = service.get_agent_runtime_config(
        "default",
        preferred_model=BACKUP_MODEL,
    )

    assert [model["provider_id"] for model in runtime["models"]] == [
        "backup",
        "primary",
    ]
    assert service.resolve_agent_model("default", BACKUP_MODEL) == BACKUP_MODEL

    with pytest.raises(ValueError, match="不允许使用模型"):
        service.get_agent_runtime_config(
            "default",
            preferred_model=ModelReferenceDTO(provider_id="backup", model_id="unknown"),
        )


def test_workspace_session_defaults_file_guards_reject_corrupt_and_stale_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """会话默认值文件必须校验可解析、是对象且 schema 版本匹配。"""

    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
    settings_dir = workspace_root / ".boxteam" / "settings"
    settings_dir.mkdir(parents=True)
    defaults_path = settings_dir / "session_defaults.json"

    def _service() -> ConfigService:
        return ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
        )

    # 文件不存在时回落静态默认值，不报错
    assert _service().get_workspace_default_agent_id() == "default"

    # schema 版本不匹配必须报错，不得静默接受旧版本
    defaults_path.write_text(
        json.dumps({"schema_version": 999}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="版本非法"):
        _service().get_workspace_default_agent_id()

    # 顶层不是对象必须报错
    defaults_path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(TypeError, match="必须是对象"):
        _service().get_workspace_default_agent_id()

    # 非法 JSON 必须原样抛出，不得被吞掉
    defaults_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        _service().get_workspace_default_agent_id()

    # 模型映射不是对象必须报错
    defaults_path.write_text(
        json.dumps({"schema_version": 2, "model_by_agent": "nope"}),
        encoding="utf-8",
    )
    with pytest.raises(TypeError, match="映射必须是对象"):
        _service().get_workspace_default_model("default")

    # 写回必须带 schema_version，且未绑定工作区时明确报错
    defaults_path.write_text(
        json.dumps({"schema_version": 2}), encoding="utf-8"
    )
    _service().set_workspace_default_agent("default")
    persisted = json.loads(defaults_path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 2
    assert persisted["default_agent_id"] == "default"

    unbound = ConfigService(
        config_dir=Path.cwd() / "configs", config_path=config_path
    )
    with pytest.raises(RuntimeError, match="未绑定工作区"):
        unbound.set_workspace_default_agent("default")


def test_workspace_session_defaults_are_persisted_without_changing_static_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    config["settings"]["workspace"]["llm"]["providers"]["backup"] = {
        "config_ref": "#/profiles/backup", "models": {"default": {
            "model": "model-b", "config_ref": "context:provider/backup",
        }},
    }
    config["profiles"]["backup"] = {"extends": "context:workspace", "settings": {"model_options":
        config["profiles"]["primary"]["settings"]["model_options"].copy()}}
    config["settings"]["workspace"]["agents"]["default"]["model"]["fallbacks"] = [BACKUP_MODEL.model_dump()]
    config["settings"]["workspace"]["agents"]["coder"] = {
        "name": "Coder",
        "instructions": {"system_prompt": "code"},
        "model": {
            "primary": PRIMARY_MODEL.model_dump(),
            "fallbacks": [BACKUP_MODEL.model_dump()],
        },
    }
    config_path = _write_workspace_config(tmp_path, config)
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=workspace_root,
    )

    service.set_workspace_default_agent("coder")
    service.set_workspace_default_model("coder", BACKUP_MODEL)

    restored = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=workspace_root,
    )
    assert restored.get_workspace_default_agent_id() == "coder"
    assert restored.get_workspace_default_model("coder") == BACKUP_MODEL
    assert restored.get_default_agent_id() == "default"
    assert restored.resolve_agent_model("coder") == PRIMARY_MODEL
    persisted = json.loads(
        (workspace_root / ".boxteam" / "settings" / "session_defaults.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted == {
        "schema_version": 2,
        "model_by_agent": {"coder": BACKUP_MODEL.model_dump()},
        "default_agent_id": "coder",
    }


def test_provider_request_override_schema_rejects_legacy_extra_body(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config["profiles"]["primary"]["settings"]["model_options"]["request_options"] = {
        "extra_body": {"reasoning": True}
    }
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    with pytest.raises(ValueError, match="配置节点校验失败") as failure:
        service.validate_workspace_config()
    assert isinstance(failure.value.__cause__, jsonschema.ValidationError)
    assert "extra_body" in str(failure.value.__cause__)


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

    with pytest.raises(ValueError, match="配置节点校验失败") as error_info:
        service.validate_workspace_config()

    cause = error_info.value.__cause__
    assert isinstance(cause, jsonschema.ValidationError)
    assert list(cause.absolute_path) == []
    assert cause.validator == "additionalProperties"


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
    config["settings"]["workspace"]["llm"]["providers"]["primary"]["models"]["default"]["model"] = "model-b"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    assert service.get_llm_model(PRIMARY_MODEL)["model"] == "model-a"
    assert await service.reload() is True
    assert service.get_revision() != original_revision
    assert service.get_llm_model(PRIMARY_MODEL)["model"] == "model-b"
    with service.use_snapshot(original_snapshot):
        assert (await service.get()).default_model == "model-a"


@pytest.mark.asyncio
async def test_reload_ignores_shadowed_lower_priority_change(tmp_path: Path) -> None:
    global_config = _base_config()
    global_path = _write_workspace_config(tmp_path, global_config)
    workspace_root = tmp_path / "workspace"
    workspace_config_path = _initialize_workspace_config(workspace_root)
    workspace_config_path.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace_local",
        "settings": {"workspace": {"mcp": {"supervision": {"sealed_call_timeout_seconds": 1900}}}},
    }), encoding="utf-8")
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=global_path,
        workspace_root=workspace_root,
    )

    original_revision = service.get_revision()
    global_config["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 2000
    global_path.write_text(json.dumps(global_config), encoding="utf-8")

    assert await service.reload() is False
    assert service.get_revision() == original_revision


@pytest.mark.asyncio
async def test_same_revision_reload_refreshes_active_source_paths(
    tmp_path: Path,
) -> None:
    global_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    workspace_config_path = _initialize_workspace_config(workspace_root)
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=global_path,
        workspace_root=workspace_root,
    )
    original_revision = service.get_revision()

    workspace_config_path.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace", "settings": {},
    }), encoding="utf-8")

    assert await service.reload() is False
    snapshot = service.get_snapshot()
    assert snapshot.revision == original_revision
    assert snapshot.source_paths == (
        Path("configs/workspace_inline.jsonc").resolve(),
        global_path,
        workspace_config_path,
    )


@pytest.mark.asyncio
async def test_reload_workspace_config_deletion_keeps_last_active_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    user_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    workspace_config_path = _initialize_workspace_config(workspace_root)
    workspace_config_path.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace_local",
        "settings": {"workspace": {"agents": {"default": {
            "instructions": {"system_prompt": "workspace"},
        }}}},
    }), encoding="utf-8")
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=user_path,
        workspace_root=workspace_root,
    )
    workspace_revision = service.get_revision()

    workspace_config_path.unlink()

    with pytest.raises(FileNotFoundError, match="声明的配置来源不存在"):
        await service.reload()
    assert service.get_revision() == workspace_revision
    assert service.get_agent_runtime_config("default")["system_prompt"] == "workspace"


@pytest.mark.asyncio
async def test_reload_status_keeps_unhealthy_snapshot_base_with_active_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有 active 记录时，不健康的快照基底必须继续体现在 status 上。"""

    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        healthy_status = service.get_reload_status()
        assert healthy_status.healthy is True
        assert healthy_status.reason is None
        assert healthy_status.state == "active"

        # 让快照基底失败，但状态库仍有 active 记录
        config_path.write_text("{ invalid", encoding="utf-8")
        with pytest.raises(ValueError):
            await service.reload()
        assert store.get_active_config_snapshot("workspace") is not None

        status = service.get_reload_status()
        # active 记录叠加后，基底的失败原因与 healthy=False 都不能被抹掉
        assert status.healthy is False
        assert status.last_error
        assert status.state == "active"
        assert status.active_revision == 1
    finally:
        store.close()


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
    assert service.get_llm_model(PRIMARY_MODEL)["model"] == "model-a"
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
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)
    original_revision = service.get_revision()
    config["settings"]["workspace"]["agents"]["default"]["tools"] = {
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
    assert status.last_error.startswith("Workspace 配置热重载失败: stage=invalid_config, error=ValueError")
    assert "扩展工具预检失败" in status.last_error
    assert "missing_package.tools:create_missing_tool" in caplog.text
    rejected = json.loads(config_path.read_text(encoding="utf-8"))
    assert rejected["config_version"] == 4
    assert rejected["settings"]["workspace"]["agents"]["default"]["tools"]["custom"][0]["factory"] == (
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
    config["settings"]["workspace"]["agents"]["default"]["instructions"]["system_prompt"] = "candidate"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    async def reject_candidate(*_args) -> NoReturn:
        raise RuntimeError("候选运行时应用失败")

    with pytest.raises(RuntimeError, match="候选运行时应用失败"):
        await service.reload(candidate_applier=reject_candidate)

    assert service.get_revision() == original_revision
    status = service.get_reload_status()
    assert status.healthy is False
    assert status.reason == "apply_failed"
    assert status.restart_required is False


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway_accepts", [True, False])
async def test_plugin_binding_candidate_is_validated_after_pending_and_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_accepts: bool,
) -> None:
    config = _base_config()
    user_config_path = _write_workspace_config(tmp_path, config)
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    load_or_create_workspace_id(workspace_root)
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=user_config_path,
        workspace_root=workspace_root,
        workspace_state_store=store,
    )
    service.validate_workspace_config()
    previous_active = store.get_active_config_snapshot("workspace")
    assert previous_active is not None
    workspace_config_path = get_workspace_config_path(workspace_root)
    workspace_config_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_config_path.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace_local",
        "settings": {"workspace": {"plugins": {"runtime": {"entries": [{
            "package_id": "sample", "plugin_id": "sample-tool",
            "package_version": "1.0.0", "enabled": True,
        }]}}}},
    }), encoding="utf-8")
    requests: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/validate"):
            pending = store.get_pending_config_candidate(config_domain="workspace")
            active = store.get_active_config_snapshot("workspace")
            assert pending is not None
            assert pending.state == "applying"
            assert active is not None
            assert active.active_revision == previous_active.active_revision
            parameters = {"sample": {"sample-tool": {}}}
            assert json.loads(request.content) == {
                "bindings": [
                    {
                        "package_id": "sample",
                        "plugin_id": "sample-tool",
                        "package_version": "1.0.0",
                        "workspace_id": load_or_create_workspace_id(workspace_root),
                        "enabled": True,
                    }
                ],
                "parameters": parameters,
                "delivery": {"values": parameters},
            }
            if not gateway_accepts:
                return httpx.Response(409, json={"detail": "未安装指定 Workspace 插件"})
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": True,
                    "request_id": request.headers["X-Request-ID"],
                },
            )
        assert request.url.path.endswith("/refresh")
        return httpx.Response(
            200,
            json={"data": {}, "request_id": request.headers["X-Request-ID"]},
        )

    monkeypatch.setattr(
        config_reload_module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            **kwargs,
            transport=httpx.MockTransport(respond),
        ),
    )
    monkeypatch.setattr(config_reload_module, "get_gateway_local_token", lambda: "local-token")
    try:
        active = store.get_active_config_snapshot("workspace")
        assert active is not None
        if gateway_accepts:
            assert await service.reload()
            active = store.get_active_config_snapshot("workspace")
            assert active is not None
            assert active.active_revision != previous_active.active_revision
            assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
                "validate",
                "refresh",
            ]
        else:
            with pytest.raises(ConfigConflictError, match="Gateway 拒绝"):
                await service.reload()
            pending = store.get_pending_config_candidate(config_domain="workspace")
            active = store.get_active_config_snapshot("workspace")
            assert pending is not None and pending.state == "conflict"
            assert active is not None
            assert active.active_revision == previous_active.active_revision
            assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
                "validate",
            ]
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("activation_fails", [False, True])
async def test_shadow_resources_activate_with_sqlite_publication_and_rollback_only_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, activation_fails: bool,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    path = _write_workspace_config(tmp_path, config)
    _initialize_workspace_config(tmp_path / "workspace")
    store = WorkspaceStateStore(workspace_root=tmp_path / "workspace")
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=path,
                            workspace_root=tmp_path / "workspace", workspace_state_store=store)
    scopes, activated, closed = [], [], []

    async def prepare(candidate, scope):
        scopes.append(scope)
        name = candidate["agents"]["default"]["instructions"]["system_prompt"]
        scope.register(lambda: closed.append(name), label="prepared-source")

        def activate() -> None:
            assert service.get_snapshot().to_dict() == candidate
            assert service._shadow_lifecycle.active.config == candidate
            persisted = store.get_active_config_snapshot("workspace")
            if len(scopes) == 1:
                assert persisted is None  # bootstrap 只绑定已有内存配置，没有候选发布事务。
            else:
                assert persisted is not None and persisted.effective_digest == service.get_revision()
            activated.append(name)
            if activation_fails and len(scopes) > 1:
                raise RuntimeError("已发布后的激活错误")

        return activate

    async def reject(_previous, _candidate, _scope, _record_side_effect) -> NoReturn:
        raise RuntimeError("拒绝候选")

    try:
        await service.start_watching(shadow_reconcile=prepare)
        await service.stop_watching()
        initial = activated[0]
        config["settings"]["workspace"]["agents"]["default"]["instructions"]["system_prompt"] = "新的来源策略配置"
        path.write_text(json.dumps(config), encoding="utf-8")
        with pytest.raises(RuntimeError, match="拒绝候选"):
            await service.reload(candidate_applier=reject)
        assert activated == [initial] and closed == ["新的来源策略配置"]
        assert scopes[0].state == "open" and scopes[1].state == "closed"
        if activation_fails:
            with pytest.raises(RuntimeError, match="已发布后的激活错误"):
                await service.reload()
            assert not service.get_reload_status().healthy
            assert scopes[0].state == "open" and closed == ["新的来源策略配置"]
        else:
            assert await service.reload()
            assert closed == ["新的来源策略配置", initial] and scopes[0].state == "closed"
        assert activated == [initial, "新的来源策略配置"]
        assert scopes[2].state == "open"
        persisted = store.get_active_config_snapshot("workspace")
        assert persisted is not None and service.get_revision() == persisted.effective_digest
        assert service._shadow_lifecycle.generation == 2
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_published_reload_keeps_sqlite_cache_and_shadow_consistent_when_old_scope_drain_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    path = _write_workspace_config(tmp_path, config)
    workspace = tmp_path / "workspace"
    _initialize_workspace_config(workspace)
    store = WorkspaceStateStore(workspace_root=workspace)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=path,
                            workspace_root=workspace, workspace_state_store=store)
    try:
        await service.start_watching()
        await service.stop_watching()
        previous = service.get_revision()
        old = service._shadow_lifecycle.active

        def fail_old_release() -> None:
            raise RuntimeError("测试旧来源订阅释放失败")

        old.scope.register(fail_old_release, label="source-policy-release")
        config["settings"]["workspace"]["agents"]["default"]["instructions"]["system_prompt"] = "已提交的新配置"
        path.write_text(json.dumps(config), encoding="utf-8")
        with pytest.raises(ExceptionGroup):
            await service.reload()
        persisted = store.get_active_config_snapshot("workspace")
        assert persisted is not None and persisted.effective_digest != previous
        assert service.get_revision() == persisted.effective_digest
        assert service.get_snapshot().to_dict() == service._shadow_lifecycle.active.config
        assert service._shadow_lifecycle.generation == old.generation + 1
        assert not service.get_reload_status().healthy
    finally:
        try:
            with pytest.raises(ExceptionGroup):
                await service.close()
            assert service._shadow_lifecycle.readiness == "close_failed"
        finally:
            store.close()


@pytest.mark.asyncio
async def test_sqlite_cache_and_shadow_publish_before_claim_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    path = _write_workspace_config(tmp_path, config)
    workspace = tmp_path / "workspace"
    _initialize_workspace_config(workspace)
    store = WorkspaceStateStore(workspace_root=workspace)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=path,
                            workspace_root=workspace, workspace_state_store=store)
    try:
        await service.start_watching()
        await service.stop_watching()
        previous = service.get_revision()
        release = store.release_config_apply_claim

        def fail_after_release(**kwargs) -> None:
            release(**kwargs)
            raise RuntimeError("测试 claim 释放后的错误")

        monkeypatch.setattr(store, "release_config_apply_claim", fail_after_release)
        config["settings"]["workspace"]["agents"]["default"]["instructions"]["system_prompt"] = "claim 清理前已发布"
        path.write_text(json.dumps(config), encoding="utf-8")
        with pytest.raises(RuntimeError, match="claim 释放后的错误"):
            await service.reload()
        active = store.get_active_config_snapshot("workspace")
        assert active is not None and active.effective_digest != previous
        assert service.get_revision() == active.effective_digest
        assert service.get_snapshot().to_dict() == service._shadow_lifecycle.active.config
        assert service._shadow_lifecycle.generation == 2
        assert service._shadow_lifecycle.active.scope.state == "open"
        assert not service.get_reload_status().healthy
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_reload_cancelled_during_old_scope_drain_keeps_published_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    path = _write_workspace_config(tmp_path, config)
    workspace = tmp_path / "workspace"
    _initialize_workspace_config(workspace)
    store = WorkspaceStateStore(workspace_root=workspace)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=path,
                            workspace_root=workspace, workspace_state_store=store)
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    try:
        await service.start_watching()
        await service.stop_watching()
        previous = service.get_revision()
        old = service._shadow_lifecycle.active

        async def wait_old_release() -> None:
            entered.set()
            await release.wait()

        old.scope.register(wait_old_release, label="old-source-drain-barrier")
        config["settings"]["workspace"]["agents"]["default"]["instructions"]["system_prompt"] = "取消前已发布"
        path.write_text(json.dumps(config), encoding="utf-8")
        task = asyncio.create_task(service.reload())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        active = store.get_active_config_snapshot("workspace")
        assert active is not None and active.effective_digest != previous
        assert service.get_revision() == active.effective_digest
        assert service.get_snapshot().to_dict() == service._shadow_lifecycle.active.config
        assert service._shadow_lifecycle.generation == old.generation + 1
        assert service._shadow_lifecycle.active.scope.state == "open"
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_source_change_during_apply_fails_final_cas_without_active_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
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
        candidate_config["settings"]["workspace"]["ui"] = {"default_orchestration": "multi_agent"}
        config_path.write_text(json.dumps(candidate_config), encoding="utf-8")

        async def mutate_source_during_apply(*_args: object):
            source = store.get_source_layer("workspace_mutable_override")
            assert source is not None
            store.sync_config_source(
                config_key="workspace_mutable_override",
                uri=None,
                config_version=1,
                presence="present",
                payload={"ui": {"default_orchestration": "single_agent"}},
                layer_digest="concurrent-source-digest",
                expected_layer_revision=source.layer_revision,
                expected_layer_digest=source.layer_digest,
            )
            return lambda: None

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
    _initialize_workspace_config(workspace_root)
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

        from configs.inheritance import resolver as resolver_module

        original_parse = resolver_module.parse_stable_config_file
        mutated = False

        def parse_then_mutate(snapshot):
            nonlocal mutated
            payload = original_parse(snapshot)
            if snapshot.path == config_path and not mutated:
                mutated = True
                changed = _base_config()
                changed["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 2000
                config_path.write_text(json.dumps(changed), encoding="utf-8")
            return payload

        monkeypatch.setattr(
            resolver_module,
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
    config["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 2000
    config_path.write_text(json.dumps(config), encoding="utf-8")

    async def require_restart(*_args) -> NoReturn:
        raise ConfigRestartRequiredError(
            "需要重启工作区后端",
            changed_sections=("mcp",),
        )

    with pytest.raises(ConfigRestartRequiredError):
        await service.reload(candidate_applier=require_restart)

    status = service.get_reload_status()
    assert service.get_revision() == original_revision
    assert status.healthy is False
    assert status.restart_required is True
    assert status.reason == "restart_required"
    assert status.changed_sections == ("mcp",)


@pytest.mark.asyncio
async def test_pinned_snapshot_survives_await_and_runtime_reload(
    tmp_path: Path,
) -> None:
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)
    original_snapshot = service.get_snapshot()
    config["settings"]["workspace"]["llm"]["providers"]["primary"]["models"]["default"]["model"] = "model-b"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with service.use_snapshot(original_snapshot):
        assert await service.reload() is True
        await asyncio.sleep(0)
        assert service.get_revision() == original_snapshot.revision
        assert (await service.get()).default_model == "model-a"

    assert service.get_revision() != original_snapshot.revision
    assert (await service.get()).default_model == "model-b"


@pytest.mark.asyncio
async def test_watcher_detects_workspace_config_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    workspace_config_path = _initialize_workspace_config(workspace_root)
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=config_path,
        workspace_root=workspace_root,
    )
    original_revision = service.get_revision()

    await service.start_watching()
    try:
        workspace_config_path.write_text(json.dumps({
            "config_version": 4, "extends": "user:workspace_local",
            "settings": {"workspace": {"agents": {"default": {
                "instructions": {"system_prompt": "hot"},
            }}}},
        }), encoding="utf-8")
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

    with pytest.raises(FileNotFoundError, match="声明的配置来源不存在"):
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
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        assert service.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"] == 1800
        config = _base_config()
        config["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 2500
        config_path.write_text(json.dumps(config), encoding="utf-8")

        assert service.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"] == 1800
        assert await service.reload() is True
        assert service.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"] == 2500
        source = store.get_source_layer("workspace_mutable_override")
        assert source is not None
        assert source.presence == "present"
        assert source.layer_revision == 2
        assert source.source_generation == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reachable_user_source_deletion_rejects_candidate_and_keeps_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        original_revision = service.get_revision()
        source_before = store.get_source_layer("workspace_mutable_override")
        assert source_before is not None
        config_path.unlink()

        with pytest.raises(FileNotFoundError, match="声明的配置来源不存在"):
            await service.reload()
        source = store.get_source_layer("workspace_mutable_override")
        assert source == source_before
        assert service.get_revision() == original_revision
        assert store.get_active_config_snapshot("workspace") is not None
        assert not list(tmp_path.rglob("*.deleted.bak"))
    finally:
        store.close()


@pytest.mark.asyncio
async def test_jsonc_rename_to_temp_rejects_candidate_then_restores_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    user_path = _write_workspace_config(tmp_path, _base_config())
    workspace_root = tmp_path / "workspace"
    workspace_path = _initialize_workspace_config(workspace_root)
    workspace_path.write_text(json.dumps({
        "config_version": 4, "extends": "user:workspace_local",
        "settings": {"workspace": {"agents": {"default": {
            "instructions": {"system_prompt": "before"},
        }}}},
    }), encoding="utf-8")
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

        with pytest.raises(FileNotFoundError, match="声明的配置来源不存在"):
            await service.reload()
        assert service.get_revision() == original_revision
        active_source = store.get_source_layer("workspace_root_mutable_override")
        assert active_source is not None and active_source.presence == "present"
        assert not list(tmp_path.rglob("*.deleted.bak"))

        temporary_path.write_text(json.dumps({
            "config_version": 4, "extends": "user:workspace_local",
            "settings": {"workspace": {"agents": {"default": {
                "instructions": {"system_prompt": "after"},
            }}}},
        }), encoding="utf-8")
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
    _initialize_workspace_config(workspace_root)
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
        candidate_config["settings"]["workspace"]["ui"] = {"default_orchestration": "multi_agent"}
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
                uri=None,
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
    _initialize_workspace_config(workspace_root)
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
        assert any(binding["secret_ref"] == "env:TEST_API_KEY" for binding in snapshot.secret_bindings.values())
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
    _initialize_workspace_config(workspace_root)
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
        config["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 2000
        config_path.write_text(json.dumps(config), encoding="utf-8")

        async def require_restart(*_args) -> NoReturn:
            raise ConfigRestartRequiredError(
                "需要重启工作区后端",
                changed_sections=("mcp",),
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
        assert pending.payload["mcp"]["supervision"]["sealed_call_timeout_seconds"] == 2000
        with closing(store.connection()) as connection:
            persisted = dict(connection.execute("SELECT * FROM config_pending_candidate").fetchone())
        assert "persistence_location" not in persisted
        assert str(store.path) not in json.dumps(persisted)
        events = store.list_config_events(config_domain="workspace")
        assert len(events) == 1
        assert events[0].result == "restart_required"
        assert events[0].activation_scope == "restart_workspace"
        assert events[0].applied_paths == ()
        assert events[0].deferred_paths == events[0].changed_paths
    finally:
        store.close()


@pytest.mark.asyncio
async def test_workspace_pending_restart_resolves_with_matching_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    config_path = _write_workspace_config(tmp_path, config)
    workspace_root = tmp_path / "workspace"
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        config["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 2000
        config_path.write_text(json.dumps(config), encoding="utf-8")

        async def require_restart(*_args) -> NoReturn:
            raise ConfigRestartRequiredError(
                "需要重启工作区后端",
                changed_sections=("mcp",),
            )

        with pytest.raises(ConfigRestartRequiredError):
            await service.reload(candidate_applier=require_restart)
        pending = store.get_pending_config_candidate(config_domain="workspace")
        assert pending is not None
        assert pending.candidate_ref is not None
        proof = service._pending_restart.build_pending_restart_health_proof(
            candidate_ref=pending.candidate_ref,
            pending=pending,
        )
        claims_during_cleanup = []

        def observe_claim_during_cleanup(_released: object) -> None:
            claims_during_cleanup.append(
                store.get_config_apply_claim(config_domain="workspace")
            )

        service._remove_released_plugin_private_inputs = observe_claim_during_cleanup

        service.resolve_pending_restart(
            candidate_ref=pending.candidate_ref,
            health_proof=proof,
        )

        assert len(claims_during_cleanup) == 1
        assert claims_during_cleanup[0] is not None
        assert store.get_config_apply_claim(config_domain="workspace") is None
        active = store.get_active_config_snapshot("workspace")
        resolved = store.get_pending_config_candidate(config_domain="workspace")
        assert active is not None
        assert resolved is not None
        assert active.effective_digest == resolved.effective_digest
        assert resolved.state == "active"
        assert [event.result for event in store.list_config_events(
            config_domain="workspace"
        )] == ["restart_required", "applied"]
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
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        config["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 2000
        config_path.write_text(json.dumps(config), encoding="utf-8")

        async def require_restart(*_args) -> NoReturn:
            raise ConfigRestartRequiredError(
                "需要重启工作区后端",
                changed_sections=("mcp",),
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

        proof = service._pending_restart.build_pending_restart_health_proof(
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
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        config["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 2000
        config_path.write_text(json.dumps(config), encoding="utf-8")

        async def require_restart(*_args) -> NoReturn:
            raise ConfigRestartRequiredError(
                "需要重启工作区后端",
                changed_sections=("mcp",),
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
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        service = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=store,
        )
        service.validate_workspace_config()
        config["settings"]["workspace"]["mcp"]["supervision"]["sealed_call_timeout_seconds"] = 2000
        config_path.write_text(json.dumps(config), encoding="utf-8")

        async def require_restart(*_args) -> NoReturn:
            raise ConfigRestartRequiredError(
                "需要重启工作区后端",
                changed_sections=("mcp",),
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

    startup_mount = MountedWorkspace(
        load_or_create_workspace_id(workspace_root), workspace_root.resolve(),
        config_candidate_ref=candidate_ref, config_generation=generation,
        config_fencing_token=fencing_token,
    )
    restored_store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        restored = ConfigService(
            config_dir=Path.cwd() / "configs",
            config_path=config_path,
            workspace_root=workspace_root,
            workspace_state_store=restored_store,
            startup_mount=startup_mount,
        )
        assert restored.get_snapshot().to_dict()["mcp"]["supervision"]["sealed_call_timeout_seconds"] == 2000
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
    _initialize_workspace_config(workspace_root)
    store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        candidate_ref = "candidate-ref"
        store.create_pending_config_candidate(
            config_domain="workspace",
            candidate_id="candidate-1",
            idempotency_key="reload-1",
            payload={"config_version": 4},
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

    startup_mount = MountedWorkspace(
        load_or_create_workspace_id(workspace_root), workspace_root.resolve(),
        config_candidate_ref=candidate_ref, config_generation="generation-2",
        config_fencing_token="fence-1",
    )
    restored_store = WorkspaceStateStore(workspace_root=workspace_root)
    try:
        with pytest.raises(ConfigConflictError, match="target generation"):
            ConfigService(
                config_dir=Path.cwd() / "configs",
                config_path=config_path,
                workspace_root=workspace_root,
                workspace_state_store=restored_store,
                startup_mount=startup_mount,
            ).get_snapshot()
    finally:
        restored_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "prepare", "cancel", "promotion"])
async def test_candidate_runtime_prepares_under_claim_and_activates_only_at_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    path = _write_workspace_config(tmp_path, config)
    workspace = tmp_path / "workspace"
    _initialize_workspace_config(workspace)
    store = WorkspaceStateStore(workspace_root=workspace)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=path,
                            workspace_root=workspace, workspace_state_store=store)
    scopes: list[LifetimeScope] = []
    activated: list[str] = []
    closed: list[str] = []
    service.validate_workspace_config()
    previous = service.get_revision()
    config["settings"]["workspace"]["agents"]["default"]["instructions"]["system_prompt"] = "prepared-runtime"
    path.write_text(json.dumps(config), encoding="utf-8")

    async def prepare(_previous, candidate, scope: LifetimeScope, _record_side_effect) -> ConfigPublication:
        pending = store.get_pending_config_candidate(config_domain="workspace")
        assert pending is not None and pending.state == "applying"
        journal = store.get_config_apply_journal(apply_id=pending.last_apply_id or "")
        assert journal is not None and journal.state == "applying"
        assert store.get_config_apply_claim(config_domain="workspace") is not None
        assert service.get_active_snapshot().revision == previous
        assert service.get_revision() == candidate.revision and not activated
        scopes.append(scope)
        scope.register(lambda: closed.append(candidate.revision), label="prepared-runtime")
        if failure == "prepare":
            raise RuntimeError("候选准备失败")
        if failure == "cancel":
            raise asyncio.CancelledError("候选准备取消")

        def activate() -> None:
            persisted = store.get_active_config_snapshot("workspace")
            assert persisted is not None and persisted.effective_digest == candidate.revision
            assert service.get_active_snapshot().revision == candidate.revision
            assert service._shadow_lifecycle.active.scope is scope
            activated.append(candidate.revision)

        return activate

    def reject_promotion(*_args, **_kwargs) -> NoReturn:
        raise ConfigConflictError("候选提交CAS冲突")

    if failure == "promotion":
        monkeypatch.setattr(service, "_persist_active_snapshot", reject_promotion)
    try:
        if failure is None:
            assert await service.reload(candidate_applier=prepare)
            assert activated == [service.get_revision()] and not closed
            assert scopes[0].state == "open"
        else:
            expected = {"prepare": RuntimeError, "cancel": asyncio.CancelledError,
                        "promotion": ConfigConflictError}[failure]
            with pytest.raises(expected):
                await service.reload(candidate_applier=prepare)
            assert service.get_revision() == previous and not activated
            assert len(closed) == 1 and scopes[0].state == "closed"
        assert store.get_config_apply_claim(config_domain="workspace") is None
        pending = store.get_pending_config_candidate(config_domain="workspace")
        assert pending is not None
        assert pending.state == ({None: "active", "prepare": "rejected", "cancel": "rejected",
                                  "promotion": "recovery_required"}[failure])
    finally:
        await service.close()
        store.close()
    assert len(closed) == 1


@pytest.mark.asyncio
async def test_expired_apply_claim_prevents_recording_destructive_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    path = _write_workspace_config(tmp_path, config)
    workspace = tmp_path / "workspace"
    _initialize_workspace_config(workspace)
    store = WorkspaceStateStore(workspace_root=workspace)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=path,
                            workspace_root=workspace, workspace_state_store=store)
    service.validate_workspace_config()
    previous = service.get_revision()
    config["settings"]["workspace"]["agents"]["default"]["instructions"]["system_prompt"] = "expired-claim"
    path.write_text(json.dumps(config), encoding="utf-8")
    effects: list[str] = []

    async def prepare(_previous, _candidate, _scope, record_side_effect) -> ConfigPublication:
        claim = store.get_config_apply_claim(config_domain="workspace")
        assert claim is not None
        store.renew_config_apply_claim(
            config_domain="workspace", apply_id=claim.apply_id,
            fencing_token=claim.fencing_token, lease_seconds=0.01,
        )
        await asyncio.sleep(0.03)
        record_side_effect({"resource": "mcp_connection", "action": "stop"})
        effects.append("stopped")
        return lambda: None

    try:
        with pytest.raises(ConfigConflictError, match="apply claim 已失效"):
            await service.reload(candidate_applier=prepare)
        assert not effects
        assert service.get_revision() == previous
        pending = store.get_pending_config_candidate(config_domain="workspace")
        assert pending is not None and pending.state == "conflict"
        journal = store.get_config_apply_journal(apply_id=pending.last_apply_id or "")
        assert journal is not None and journal.state == "failed" and not journal.side_effects
        assert store.get_config_apply_claim(config_domain="workspace") is None
    finally:
        await service.close()
        store.close()


@pytest.mark.asyncio
async def test_reader_cannot_observe_sqlite_promotion_before_runtime_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "test-key")
    config = _base_config()
    path = _write_workspace_config(tmp_path, config)
    workspace = tmp_path / "workspace"
    _initialize_workspace_config(workspace)
    store = WorkspaceStateStore(workspace_root=workspace)
    service = ConfigService(config_dir=Path.cwd() / "configs", config_path=path,
                            workspace_root=workspace, workspace_state_store=store)
    service.validate_workspace_config()
    runtime_revision = service.get_revision()
    readers = []
    attempted, observed = Event(), Event()
    original_persist = service._persist_active_snapshot

    def read_runtime() -> tuple[str, str]:
        attempted.set()
        revision = service.get_revision()
        with service.publication_lock:
            current_runtime = runtime_revision
        observed.set()
        return revision, current_runtime

    with ThreadPoolExecutor(max_workers=1) as pool:
        def persist_and_probe(*args, **kwargs) -> None:
            original_persist(*args, **kwargs)
            readers.append(pool.submit(read_runtime))
            assert attempted.wait(1)
            assert not observed.wait(0.05), "配置读取越过了尚未完成的共同发布屏障"

        async def prepare(_previous, candidate, _scope, _record_side_effect) -> ConfigPublication:
            def activate() -> None:
                nonlocal runtime_revision
                runtime_revision = candidate.revision
            return activate

        monkeypatch.setattr(service, "_persist_active_snapshot", persist_and_probe)
        config["settings"]["workspace"]["agents"]["default"]["instructions"]["system_prompt"] = "publication-barrier"
        path.write_text(json.dumps(config), encoding="utf-8")
        try:
            assert await service.reload(candidate_applier=prepare)
            assert readers[0].result(timeout=2) == (service.get_revision(), service.get_revision())
        finally:
            await service.close()
            store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("version_change", "with_parameters"), [(False, True), (True, True), (True, False)])
async def test_workspace_bound_parameters_are_checked_before_source_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version_change: bool, with_parameters: bool,
) -> None:
    config_path = _write_workspace_config(tmp_path, _base_config())
    root = tmp_path / "workspace"
    workspace_id = load_or_create_workspace_id(root)
    _initialize_workspace_config(root)
    with WorkspaceStateStore(workspace_root=root) as store:
        service = ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path,
            workspace_root=root, workspace_state_store=store)
        source = get_workspace_config_path(root)
        binding = {"package_id": "proof", "plugin_id": "probe", "package_version": "1.0.0", "enabled": False}
        settings = {"plugins": {"runtime": {"entries": [binding]},
            "configuration": {"values": {"proof": {"probe": {"label": "before"}}}}}}
        payload = {"config_version": 4, "extends": "user:workspace_local",
            "settings": {"workspace": settings}}
        if not with_parameters:
            del settings["plugins"]["configuration"]
        source.write_text(json.dumps(payload))
        service.validate_workspace_config()
        document = service.get_source_document(source_key="workspace_root_mutable_override", gateway_id="gateway-proof")
        if version_change:
            # 值不变也须按新版本校验，不能先把来源写完再拒绝候选。
            binding["package_version"] = "2.0.0"
            async def reject_new_schema(**kwargs) -> None:
                assert kwargs["candidate"].to_dict()["plugins"]["runtime"]["entries"][0]["package_version"] == "2.0.0"
                raise ConfigConflictError("新版本 schema 拒绝参数")
            monkeypatch.setattr(service, "_validate_gateway_workspace_plugin_bindings", reject_new_schema)
        else:
            settings["plugins"]["configuration"]["values"]["proof"]["probe"]["label"] = "after"
        request = ConfigSourceEditRequest(gateway_id="gateway-proof", workspace_id=workspace_id,
            source_key=document.source_key, jsonc=json.dumps(payload), base_file_digest=document.file_digest,
            base_layer_revision=document.layer_revision, base_layer_digest=document.layer_digest,
            expected_active_revision=document.active_revision, expected_active_digest=document.active_digest,
            idempotency_key="parameters-no-runtime-permission")
        original = source.read_bytes()
        with pytest.raises(ConfigConflictError if version_change else PermissionError,
            match="新版本 schema" if version_change else "插件运行绑定"):
            await service.save_source_edit(source_key=document.source_key, gateway_id="gateway-proof",
                request=request, service_plugin_runtime_manage=version_change)
        assert source.read_bytes() == original
        assert service.get_revision() == document.active_digest


@pytest.mark.asyncio
async def test_frozen_model_recipe_keeps_options_across_new_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "before-rotation")
    original = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_workspace_config(tmp_path / "original", _base_config()),
    )
    recipe = original.freeze_model_recipe(PRIMARY_MODEL)
    assert recipe["api_key"] == "env:TEST_API_KEY"
    assert "before-rotation" not in json.dumps(recipe)
    assert isinstance(recipe["proxy_environment"], dict)
    changed = _base_config()
    provider = changed["settings"]["workspace"]["llm"]["providers"]["primary"]
    provider["models"]["default"]["model"] = "new-model"
    changed["profiles"]["primary"]["settings"]["model_options"]["endpoint"] = (
        "https://changed.example/v1"
    )
    current = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=_write_workspace_config(tmp_path / "current", changed),
    )
    monkeypatch.setenv("TEST_API_KEY", "after-rotation")
    prepared = await current.prepare_model_recipe(recipe)
    assert prepared["model"] == "model-a"
    assert prepared["endpoint"] == "https://example.com/v1"
    assert prepared["api_key"] == "after-rotation"
    assert recipe["api_key"] == "env:TEST_API_KEY"
