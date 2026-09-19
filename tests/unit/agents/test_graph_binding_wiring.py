"""OpenSpec 8.4 GraphBinding 接线（R9）集成测试。

覆盖 R6a 审查 N6 要求的「真实 builder load→resolve→重建」链路：
- runtime 层透传（build_session_agent_runtime → create_runtime_deep_agent_for_session）
- 执行服务透传（AgentExecutionService._build_agent → build_session_agent_runtime）
- 真实 store 的 save→load→resolve 重建链路（真实注册表 + 真实 deep-agent binding）
- 篡改任一字段 → GraphBindingUnavailableError（不回退）
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.agents.agent_factory import MAIN_THREAD_ID
from app.agents.graph_binding import (
    DEEP_AGENT_GRAPH_BINDING,
    GRAPH_FACTORY_REGISTRY,
    GraphBinding,
    GraphBindingOwnerKey,
    GraphBindingStorePort,
    GraphBindingUnavailableError,
    JsonFileGraphBindingStore,
)
from app.runtime.agent_runtime import build_session_agent_runtime


def _make_store(tmp_path: Path) -> JsonFileGraphBindingStore:
    return JsonFileGraphBindingStore(directory=tmp_path / "graph-bindings")


def test_runtime_layer_passes_graph_binding_store_through() -> None:
    """runtime 层透传：build_session_agent_runtime 把 store 原样传给工厂层。"""

    config_service = MagicMock()
    config_service.resolve_agent_id.return_value = "default"
    dependency_provider = MagicMock()
    dependency_provider.get_checkpointer.return_value = MagicMock()
    store = MagicMock(spec=GraphBindingStorePort)

    with patch(
        "app.runtime.agent_runtime.create_runtime_deep_agent_for_session"
    ) as create_runtime:
        build_session_agent_runtime(
            session_id="session_test",
            agent_id="default",
            workspace_root=Path("/workspace"),
            config_service=config_service,
            background_task_registry=MagicMock(),
            background_message_bus=MagicMock(),
            job_event_bus=MagicMock(),
            dependency_provider=dependency_provider,
            graph_binding_store=store,
        )

    assert create_runtime.call_args.kwargs["graph_binding_store"] is store


def test_runtime_layer_defaults_to_none_store() -> None:
    """未传 store 时保持 None（不伪造持久化成功）。"""

    config_service = MagicMock()
    config_service.resolve_agent_id.return_value = "default"
    dependency_provider = MagicMock()
    dependency_provider.get_checkpointer.return_value = MagicMock()

    with patch(
        "app.runtime.agent_runtime.create_runtime_deep_agent_for_session"
    ) as create_runtime:
        build_session_agent_runtime(
            session_id="session_test",
            agent_id="default",
            workspace_root=Path("/workspace"),
            config_service=config_service,
            background_task_registry=MagicMock(),
            background_message_bus=MagicMock(),
            job_event_bus=MagicMock(),
            dependency_provider=dependency_provider,
        )

    assert create_runtime.call_args.kwargs["graph_binding_store"] is None


def test_execution_service_passes_store_to_runtime() -> None:
    """执行服务透传：AgentExecutionService 把 store 传给 build_session_agent_runtime。"""

    from app.services.orchestration.agent_execution_service import AgentExecutionService

    service = AgentExecutionService(
        config_service=MagicMock(),
        background_task_registry=MagicMock(),
        background_message_bus=MagicMock(),
        job_event_bus=MagicMock(),
        dependency_provider=MagicMock(),
        session_changes_service=MagicMock(),
        tool_selection_store=MagicMock(),
        message_stream_store=MagicMock(),
        workspace_root=Path("/workspace"),
        graph_binding_store=MagicMock(spec=GraphBindingStorePort),
    )

    with patch(
        "app.services.orchestration.agent_execution_service.build_session_agent_runtime"
    ) as build_runtime:
        service._build_agent(
            session_id="session_test",
            agent_id="default",
            execution_overrides={},
            model_visibility_overrides={},
            preferred_provider_id=None,
            include_team_tools=False,
        )

    assert build_runtime.call_args.kwargs["graph_binding_store"] is (
        service._graph_binding_store
    )


def test_real_builder_save_load_resolve_rebuild(tmp_path: Path) -> None:
    """真实 builder 链路：save（如 create_my_deep_agent 所为）→ load → resolve 重建。

    使用真实 JsonFileGraphBindingStore、真实注册表与真实 deep-agent binding，
    不 mock 任何 GraphBinding 组件。
    """

    store = _make_store(tmp_path)
    owner = GraphBindingOwnerKey(
        session_id="ses_00000000000000000000000000000000",
        thread_id=MAIN_THREAD_ID,
    )

    # 构建路径（create_my_deep_agent 的持久化行为）：save 当前 deep-agent binding。
    store.save_graph_binding(owner, DEEP_AGENT_GRAPH_BINDING)

    # 重启重建：load → resolve 必须命中同一 factory（不回退）。
    loaded = store.load_graph_binding(owner)
    assert loaded == DEEP_AGENT_GRAPH_BINDING
    builder = GRAPH_FACTORY_REGISTRY.resolve(loaded)
    assert callable(builder)

    # 幂等：同一 owner 同一 selector 重复 save 无副作用。
    store.save_graph_binding(owner, DEEP_AGENT_GRAPH_BINDING)
    assert store.load_graph_binding(owner) == DEEP_AGENT_GRAPH_BINDING


@pytest.mark.parametrize(
    "field_name",
    ["graph_id", "graph_revision", "graph_schema_hash", "capability_profile_hash"],
)
def test_tampered_binding_fails_resolve_without_fallback(
    tmp_path: Path, field_name: str
) -> None:
    """篡改任一字段 → GraphBindingUnavailableError，绝不回退到注册表当前值。"""

    store = _make_store(tmp_path)
    owner = GraphBindingOwnerKey(
        session_id="ses_00000000000000000000000000000000",
        thread_id=MAIN_THREAD_ID,
    )
    store.save_graph_binding(owner, DEEP_AGENT_GRAPH_BINDING)

    tampered_values = {
        "graph_id": "deep-agent-legacy",
        "graph_revision": DEEP_AGENT_GRAPH_BINDING.graph_revision + 1,
        "graph_schema_hash": "sha256:" + "0" * 64,
        "capability_profile_hash": "sha256:" + "0" * 64,
    }
    tampered = GraphBinding(
        graph_id=(
            tampered_values["graph_id"]
            if field_name == "graph_id"
            else DEEP_AGENT_GRAPH_BINDING.graph_id
        ),
        graph_revision=(
            tampered_values["graph_revision"]
            if field_name == "graph_revision"
            else DEEP_AGENT_GRAPH_BINDING.graph_revision
        ),
        graph_schema_hash=(
            tampered_values["graph_schema_hash"]
            if field_name == "graph_schema_hash"
            else DEEP_AGENT_GRAPH_BINDING.graph_schema_hash
        ),
        capability_profile_hash=(
            tampered_values["capability_profile_hash"]
            if field_name == "capability_profile_hash"
            else DEEP_AGENT_GRAPH_BINDING.capability_profile_hash
        ),
    )

    with pytest.raises(GraphBindingUnavailableError) as error:
        GRAPH_FACTORY_REGISTRY.resolve(tampered)
    # 错误消息必须含期望与实际，便于定位漂移字段。
    message = str(error.value)
    assert "graph_id" in message or field_name in message or "revision" in message


def test_store_directory_is_created_on_first_save(tmp_path: Path) -> None:
    """container 装配的目录约定：首次 save 自动创建 .boxteam 附属目录。"""

    store = JsonFileGraphBindingStore(
        directory=tmp_path / ".boxteam" / "graph-bindings"
    )
    owner = GraphBindingOwnerKey(
        session_id="ses_00000000000000000000000000000000",
        thread_id=MAIN_THREAD_ID,
    )
    store.save_graph_binding(owner, DEEP_AGENT_GRAPH_BINDING)
    assert (tmp_path / ".boxteam" / "graph-bindings").is_dir()
    assert store.load_graph_binding(owner) == DEEP_AGENT_GRAPH_BINDING
