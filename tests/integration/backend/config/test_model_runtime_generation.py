"""正式 Thread 准入消费冻结配置与密钥修订，连接池随原 generation 排空。"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from boxteam_plugin_sdk.lifecycle import LifetimeScope

from app.agents.graph_binding import DEEP_AGENT_GRAPH_BINDING
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.runtime.agent_runtime import prepare_agent_model
from app.schemas.gateway.secrets import ResolvedSecret
from app.services.infrastructure.config.source_owner import WorkspaceSourceOwner
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.gateway_secrets.client import GatewaySecretsClient
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore
from app.services.orchestration.agent_execution_service import AgentExecutionService
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from configs.installer import install_user_configuration
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace

API_SECRET = "sec_" + "1" * 32
FALLBACK_SECRET = "sec_" + "2" * 32
PROXY_SECRET = "sec_" + "3" * 32


@dataclass
class RuntimeCase:
    config: ConfigService
    executor: AgentExecutionService
    document: dict
    source: Path
    secrets: dict[str, ResolvedSecret]
    built_models: list


@pytest.fixture
async def runtime_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    config_root = tmp_path / "home/config"
    install_user_configuration(config_root=config_root, profile="default", workspace_root=workspace)
    source = config_root / "workspace.jsonc"
    document = {"config_version": 4, "extends": "builtin:workspace", "settings": {
        "network": {"proxy": {"mode": "explicit", "url": "http://127.0.0.1:1", "auth": {
            "username": "owner", "password": "secret:" + PROXY_SECRET,
        }}},
        "workspace": {"agents": {"default": {"model": {"fallbacks": [
            {"provider_id": "backup_1", "model_id": "default"},
        ]}}}, "llm": {"providers": {
            "primary": {"config_ref": "#/profiles/primary"},
            "backup_1": {"config_ref": "#/profiles/fallback"},
        }}},
    }, "profiles": {name: {"extends": "context:workspace", "settings": {"model_options": {
        "endpoint": "http://model.invalid/v1", "custom_llm_provider": "openai",
        "api_key": "secret:" + secret, "api_mode": {"protocol": "chat_completions", "streaming": False,
            "model_info": {"supports_vision": False, "supports_reasoning": False, "supports_function_calling": True}},
    }}} for name, secret in (("primary", API_SECRET), ("fallback", FALLBACK_SECRET))}}
    source.write_text(json.dumps(document), encoding="utf-8")
    secrets = {key: ResolvedSecret(revision=1, value="initial-" + str(index))
               for index, key in enumerate((API_SECRET, FALLBACK_SECRET, PROXY_SECRET))}

    async def resolve(_self, ids):
        selected = list(ids)
        missing = set(selected) - secrets.keys()
        if missing:
            raise RuntimeError("命名密钥不存在: " + ",".join(sorted(missing)))
        return {key: secrets[key] for key in selected}

    monkeypatch.setattr(GatewaySecretsClient, "resolve", resolve)
    source_owner = WorkspaceSourceOwner(path=config_root / "workspace-source.sqlite")
    with WorkspaceStateStore(workspace_root=workspace) as store:
        config = ConfigService(config_dir=config_root, config_path=source, workspace_root=workspace,
            workspace_state_store=store, source_owner=source_owner,
            source_owner_workspace_id=load_or_create_workspace_id(workspace))
        config.validate_workspace_config()
        mcp_owner = MagicMock()
        mcp_owner.publication_lock = threading.RLock()
        mcp_owner.publication_snapshot.return_value = SimpleNamespace(tools={})
        dependencies = MagicMock()
        dependencies.get_mcp_catalog_owner.return_value = mcp_owner
        bindings = MagicMock()
        bindings.load_graph_binding.return_value = DEEP_AGENT_GRAPH_BINDING
        executor = AgentExecutionService(
            config_service=config, background_task_registry=MagicMock(), background_mailbox=MagicMock(),
            job_event_bus=MagicMock(), dependency_provider=dependencies, session_changes_service=MagicMock(),
            tool_selection_store=MagicMock(), message_stream_store=MagicMock(),
            workspace_id=load_or_create_workspace_id(workspace), workspace_root=workspace,
            session_catalog=MagicMock(), graph_binding_store=bindings, residency_tracker=ThreadResidencyTracker(),
            workspace_file_resource_registry=MagicMock(), resource_activation_factory=MagicMock(),
            team_resource_subscriber=MagicMock(),
        )
        built_models = []

        def assemble(**kwargs):
            setup = prepare_agent_model(config_service=config, agent_id="default",
                preferred_model=kwargs["preferred_model"], lifetime_scope=kwargs["lifetime_scope"])
            model = setup.model
            model._provider_http_client(is_async=True)
            built_models.append(model)
            return model

        monkeypatch.setattr(executor, "_assemble_agent", assemble)
        monkeypatch.setattr("app.services.orchestration.agent_execution_service.build_workspace_skill_catalog", MagicMock())
        monkeypatch.setattr("app.services.orchestration.agent_execution_service.start_workspace_skill_catalog_publication", MagicMock())
        try:
            yield RuntimeCase(config, executor, document, source, secrets, built_models)
        finally:
            await executor.shutdown()
            await config.close()


@pytest.fixture
def acquire_runtime(runtime_case: RuntimeCase):
    sequence = 0
    leases = []

    async def acquire():
        nonlocal sequence
        sequence += 1
        lease = await runtime_case.executor._acquire_step_agent(
            admission=ExecutionAdmission(session_id="session-network", thread_id="thread-network",
                execution_id=f"execution-{sequence}", job_id=f"job-{sequence}",
                admission_idempotency_key=f"admission-{sequence}", execution_origin="user_turn", turn_id=f"turn-{sequence}"),
            agent_id="default", execution_overrides={}, model_visibility_overrides={}, preferred_model=None,
            include_team_tools=False,
        )
        leases.append(lease)
        return lease
    yield acquire
    for lease in leases:
        lease.release()


async def test_secret_revision_rebuilds_after_old_lease_and_deletion_cannot_reuse_pool(
    runtime_case: RuntimeCase, acquire_runtime,
) -> None:
    case = runtime_case
    lease = await acquire_runtime()
    first = lease.runtime.agent
    same = await acquire_runtime()
    assert same.runtime.agent is first and same.generation == lease.generation
    same.release()
    snapshot = case.config.get_snapshot()
    for secret in (API_SECRET, FALLBACK_SECRET, PROXY_SECRET):
        old_model = lease.runtime.agent
        old_client = old_model._owned_async_client
        case.secrets[secret] = ResolvedSecret(revision=2, value="rotated-" + secret[-1])
        pending = asyncio.create_task(acquire_runtime())
        try:
            for _ in range(100):
                if case.executor._residency_tracker.snapshot("session-network", "thread-network").residency == "unloading":
                    break
                await asyncio.sleep(0.01)
            assert not pending.done()
            assert not old_client.is_closed()
            old_generation = lease.generation
            lease.release()
            lease = await asyncio.wait_for(pending, timeout=10)
        finally:
            if not pending.done():
                lease.release()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert old_client.is_closed()
        assert lease.generation > old_generation and lease.runtime.agent is not old_model
        assert case.config.get_snapshot() is snapshot
    active_model = lease.runtime.agent
    active_client = active_model._owned_async_client
    for secret in (API_SECRET, FALLBACK_SECRET, PROXY_SECRET):
        removed = case.secrets.pop(secret)
        try:
            with pytest.raises(RuntimeError, match="命名密钥不存在"):
                await acquire_runtime()
            assert lease.runtime.agent is active_model and not active_client.is_closed()
        finally:
            case.secrets[secret] = removed
    lease.release()


async def test_captured_environment_route_changes_generation_without_option_revision(
    runtime_case: RuntimeCase, acquire_runtime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = runtime_case
    case.document["settings"]["network"] = {"proxy": {"mode": "environment"}}
    case.source.write_text(json.dumps(case.document), encoding="utf-8")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:21001")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:21001")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:21001")
    monkeypatch.setenv("NO_PROXY", "")
    await case.config.reload()
    first_snapshot = case.config.get_snapshot()
    first = await acquire_runtime()
    model, generation = first.runtime.agent, first.generation
    client = model._owned_async_client
    first.release()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:21002")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:21002")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:21002")
    await case.config.reload()
    second_snapshot = case.config.get_snapshot()
    assert second_snapshot.revision == first_snapshot.revision
    assert second_snapshot.network_revision != first_snapshot.network_revision
    second = await acquire_runtime()
    try:
        assert second.generation > generation and second.runtime.agent is not model
        assert client.is_closed()
        assert second.runtime.agent.proxy_routes.revision != model.proxy_routes.revision
        same = await acquire_runtime()
        assert same.runtime.agent is second.runtime.agent
        same.release()
        # 只改来源引用而有效选项和路由相同，不应丢弃已经驻留的模型池。
        case.document["profiles"]["same-primary"] = case.document["profiles"]["primary"]
        case.document["settings"]["workspace"]["llm"]["providers"]["primary"]["config_ref"] = "#/profiles/same-primary"
        case.source.write_text(json.dumps(case.document), encoding="utf-8")
        assert await case.config.reload() is False
        same = await acquire_runtime()
        assert same.generation == second.generation and same.runtime.agent is second.runtime.agent
        same.release()
    finally:
        second.release()


async def test_model_and_background_recipe_remain_on_one_snapshot_during_reload(runtime_case, monkeypatch):
    """构造第一候选后发布新配置，剩余候选、容量及秘密引用仍属于原快照。"""
    from app.runtime import provider_model

    case = runtime_case
    old = case.config.get_snapshot()
    frozen_before = case.config.freeze_model_recipe(case.config.resolve_agent_model("default", None))
    runtime = case.config.get_agent_runtime_config("default")
    credentials = await case.config.prepare_llm_credentials(runtime["models"])
    built, resume = threading.Event(), threading.Event()
    original = provider_model.build_model_from_provider
    model_inputs = []

    def paused_build(provider, runtime_config, **kwargs):
        model = original(provider, runtime_config, **kwargs)
        model_inputs.append(dict(provider))
        if len(model_inputs) == 1:
            built.set()
            if not resume.wait(timeout=15):
                raise TimeoutError("配置竞争测试没有释放候选构造")
        return model

    monkeypatch.setattr(provider_model, "build_model_from_provider", paused_build)
    scope = LifetimeScope("model-recipe-snapshot-race")
    task = None
    try:
        with case.config.use_llm_credentials(credentials):
            task = asyncio.create_task(asyncio.to_thread(
                prepare_agent_model, "default", case.config, lifetime_scope=scope,
            ))
        assert await asyncio.to_thread(built.wait, 10)
        case.document["profiles"]["primary"]["settings"]["model_options"]["endpoint"] = "http://updated-model.invalid/v1"
        case.source.write_text(json.dumps(case.document), encoding="utf-8")
        assert await case.config.reload()
        assert case.config.get_snapshot() is not old
        resume.set()
        setup = await asyncio.wait_for(task, timeout=10)
        assert setup.recipe_for(setup.model) == frozen_before
        assert all(provider["endpoint"] == "http://model.invalid/v1" for provider in model_inputs)
        assert case.config.freeze_model_recipe(case.config.resolve_agent_model("default", None))["endpoint"] == "http://updated-model.invalid/v1"
        for model, recipe in setup.model_recipes:
            assert "initial-" not in str(recipe)
            assert recipe["endpoint"] == "http://model.invalid/v1"
            assert setup.recipe_for(model) is recipe
    finally:
        resume.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await scope.close()
