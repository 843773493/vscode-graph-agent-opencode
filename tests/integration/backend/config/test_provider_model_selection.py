"""从真实配置、Agent 目录到 Session catalog 验证完整模型身份。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import pytest

from app.core.path_utils import (
    get_session_creation_service,
    get_session_path_resolver,
    get_sessions_dir,
    initialize_directories,
    workspace_context,
)
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.execution_admission import UserExecutionIdentity
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.schemas.internal_v2.session import SessionCreateRequest, SessionUpdateRequest
from app.services.business.agent_service import AgentService
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.session_trace.store import TraceEventStore


@pytest.fixture
def model_definitions() -> dict:
    return {
        "config_version": 3,
        "default_agent": "default",
        "llm": {"providers": {"team": {
            "name": "团队模型服务",
            "defaults": {
                "endpoint": "https://models.example/v1",
                "custom_llm_provider": "openai", "api_key": "",
                "api_mode": {"protocol": "chat_completions", "model_info": {
                    "supports_function_calling": True, "supports_reasoning": False,
                }, "supports_reasoning": {}, "streaming": False},
                "request_options": {"parameters": {"temperature": 0.8}},
            },
            "models": {
                "main": {"model": "upstream-main", "name": "日常助手"},
                "alternate": {"model": "upstream-alternate", "name": "备用助手", "overrides": {
                    "request_options": {"parameters": {"temperature": 0}},
                }},
                "catalog-only": {"model": "unselected"},
            },
        }}},
        "agents": {agent: {"model": {
            "primary": {"provider_id": "team", "model_id": "main"},
            "fallbacks": [{"provider_id": "team", "model_id": "alternate"}],
        }} for agent in ("default", "coder", "reviewer", "researcher")},
    }


@pytest.fixture
def services(tmp_path: Path, model_definitions: dict) -> Iterator[tuple[ConfigService, SessionService, Path]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_id = load_or_create_workspace_id(workspace)
    initialize_directories(workspace_id, workspace)
    with workspace_context(workspace_id, workspace):
        path = workspace / ".boxteam/workspace.jsonc"
        path.write_text(json.dumps(model_definitions), encoding="utf-8")
        config = ConfigService(config_dir=Path.cwd() / "configs", workspace_root=workspace)
        config.validate_workspace_config()
        resolver = get_session_path_resolver()
        sessions = SessionService(
            config_service=config, workspace_id=workspace_id, path_resolver=resolver,
            creation_service=get_session_creation_service(),
            trace_event_store=TraceEventStore(sessions_dir=get_sessions_dir()),
        )
        try:
            yield config, sessions, path
        finally:
            resolver.catalog_store.close()


async def test_agent_candidates_and_session_defaults_keep_model_identity(services) -> None:
    config, sessions, _path = services
    alternate = ModelReferenceDTO(provider_id="team", model_id="alternate")
    catalog_only = ModelReferenceDTO(provider_id="team", model_id="catalog-only")
    assert {model["model_id"] for model in config.list_llm_models()
            if model["provider_id"] == "team"} == {"main", "alternate", "catalog-only"}
    runtime = config.get_agent_runtime_config("default", preferred_model=alternate)
    assert [model["model_id"] for model in runtime["models"]] == ["alternate", "main"]
    assert runtime["models"][0]["request_options"]["parameters"]["temperature"] == 0
    assert runtime["models"][0]["api_mode"]["streaming"] is False
    with pytest.raises(ValueError, match="不允许使用模型"):
        config.get_agent_runtime_config("default", preferred_model=catalog_only)
    listed = {agent.agent_id: agent for agent in await AgentService(config_service=config).list()}
    assert [model.model_ref.model_id for model in listed["default"].models] == ["main", "alternate"]
    assert [(model.name, model.model, model.provider_name) for model in listed["default"].models] == [
        ("日常助手", "upstream-main", "团队模型服务"),
        ("备用助手", "upstream-alternate", "团队模型服务"),
    ]
    assert "name" not in runtime["models"][0], "展示名不得进入实际模型请求选项"
    config.set_workspace_default_model("default", alternate)
    restored = ConfigService(config_dir=Path.cwd() / "configs", workspace_root=_path.parent.parent)
    assert restored.get_workspace_default_model("default") == alternate
    created = await sessions.create(SessionCreateRequest(title="模型归属"))
    assert created.current_model == alternate
    assert ModelReferenceDTO.model_validate_json(
        sessions.path_resolver.get_node(created.session_id).current_model_json,
    ) == alternate
    assert (await sessions.get(created.session_id)).current_model == alternate
    with pytest.raises(ValueError, match="不允许使用模型"):
        await sessions.update(created.session_id, SessionUpdateRequest(model=catalog_only))
    assert (await sessions.get(created.session_id)).current_model == alternate
    switched = await sessions.update(created.session_id, SessionUpdateRequest(agent_id="coder"))
    assert switched.current_model == ModelReferenceDTO(provider_id="team", model_id="main")


async def test_removed_model_keeps_session_binding_and_requires_explicit_choice(services, model_definitions) -> None:
    _config, sessions, path = services
    selected = ModelReferenceDTO(provider_id="team", model_id="alternate")
    created = await sessions.create(SessionCreateRequest())
    await sessions.update(created.session_id, SessionUpdateRequest(model=selected))
    for agent in model_definitions["agents"].values():
        agent["model"]["fallbacks"] = []
    del model_definitions["llm"]["providers"]["team"]["models"]["alternate"]
    path.write_text(json.dumps(model_definitions), encoding="utf-8")
    restored_config = ConfigService(config_dir=Path.cwd() / "configs", workspace_root=path.parent.parent)
    restored_config.validate_workspace_config()
    restored = SessionService(
        config_service=restored_config, workspace_id=sessions.workspace_id,
        path_resolver=sessions.path_resolver, creation_service=get_session_creation_service(),
        trace_event_store=TraceEventStore(sessions_dir=get_sessions_dir()),
    )
    assert (await restored.get(created.session_id)).current_model == selected
    with pytest.raises(ValueError, match="不允许使用模型"):
        restored_config.resolve_agent_model("default", selected)
    reselected = await restored.update(created.session_id, SessionUpdateRequest(
        model=ModelReferenceDTO(provider_id="team", model_id="main"),
    ))
    assert reselected.current_model.model_id == "main"


async def test_queue_freezes_selected_model_as_plain_json(services) -> None:
    _config, sessions, _path = services
    session = await sessions.create(SessionCreateRequest())
    frozen = ExecutionQueueInput(
        execution_binding=UserExecutionIdentity(turn_id="turn-one", job_id="job-one", admission_idempotency_key="admission-one"),
        message_id="message-one", agent_id=session.current_agent_id,
        model=session.current_model.model_dump(mode="json"), delivery_policy="after_turn",
        gateway_id=None, request_id=None, created_at=session.created_at.isoformat(),
    )
    serialized = json.loads(json.dumps(asdict(frozen)))
    assert serialized["model"] == {"provider_id": "team", "model_id": "main"}
    assert ExecutionQueueInput.from_dict(serialized) == frozen


@pytest.mark.parametrize("invalid", ["flat", "identity", "literal", "missing-key", "missing-model", "protocol", "extra-body", "version"])
def test_invalid_model_candidate_is_rejected_without_rewriting_source(services, model_definitions, invalid: str) -> None:
    _config, _sessions, path = services
    options = model_definitions["llm"]["providers"]["team"]["defaults"]
    if invalid == "flat":
        model_definitions["llm"]["providers"] = [{"id": "team", **options, "model": "upstream-main"}]
    elif invalid == "identity":
        options["id"] = "another-identity"
    elif invalid == "literal":
        options["api_key"] = "plaintext-model-key"
    elif invalid == "missing-key":
        del options["api_key"]
    elif invalid == "missing-model":
        model_definitions["agents"]["default"]["model"]["primary"]["model_id"] = "missing"
    elif invalid == "protocol":
        options["api_mode"]["supports_reasoning"] = {"reasoning_content": False}
        options["api_mode"]["protocol"] = "responses"
    elif invalid == "extra-body":
        options["request_options"]["parameters"]["extra_body"] = {"temperature": 1}
    else:
        model_definitions["config_version"] = 2
    source = json.dumps(model_definitions)
    path.write_text(source, encoding="utf-8")
    restored = ConfigService(config_dir=Path.cwd() / "configs", workspace_root=path.parent.parent)
    with pytest.raises(ValueError):
        restored.validate_workspace_config()
    assert path.read_text(encoding="utf-8") == source
