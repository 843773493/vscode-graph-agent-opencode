import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from time import perf_counter

import pytest
from pydantic import ValidationError

from app.core.exceptions import NotFoundError
from app.core.identifier import create_prefixed_id
from app.core.path_utils import (
    get_session_creation_service,
    get_session_file,
    get_session_path,
    get_session_path_resolver,
    get_sessions_dir,
    initialize_directories,
    workspace_context,
)
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.session import (
    SessionCreateRequest,
    SessionUpdateRequest,
)
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_service import SessionService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.harness.python.run_context import TestRunContext
from tests.support.session_navigation_queue import (
    create_catalog_intent,
    submit_catalog_intent,
)
from tests.unit.services.business.session_service_test_support import (
    SessionServiceConfigStub,
    prepare_session_test_workspace,
)


@pytest.fixture()
def session_config_service() -> SessionServiceConfigStub:
    return SessionServiceConfigStub()


def test_config_stub_preserves_explicit_empty_configuration():
    config_service = SessionServiceConfigStub({})

    with pytest.raises(ValueError, match="agent default 不存在"):
        config_service.resolve_agent_provider_id(None)


class TestSessionService:
    """测试会话服务功能"""

    # R17：不再硬编码假 workspace_id——catalog 模式下 resolver 的
    # workspace_id 来自 identity API（load_or_create_workspace_id），
    # register 会校验 manifest workspace_id 与分配一致；service 必须与
    # resolver 同源。真实值在 setup_method 按同一来源计算（实例属性遮蔽）。
    workspace_id = "00000000-0000-4000-8000-000000000001"

    @pytest.fixture(autouse=True)
    def setup_workspace(
        self,
        request: pytest.FixtureRequest,
        session_config_service: SessionServiceConfigStub,
    ):
        """从完整只读模板准备正式测试工作区，并注入离线配置替身。"""
        context, _, workspace_root = prepare_session_test_workspace(
            test_file=Path(request.node.path),
            node_id=request.node.nodeid,
        )
        self.temp_dir = workspace_root
        self.workspace_id = load_or_create_workspace_id(workspace_root)
        initialize_directories(self.workspace_id, workspace_root)
        self.config_service = session_config_service
        try:
            with workspace_context(self.workspace_id, workspace_root):
                self.path_resolver = get_session_path_resolver()
                self.creation_service = get_session_creation_service()
                self.trace_event_store = TraceEventStore(sessions_dir=get_sessions_dir())
                self.saver = RolloutCheckpointSaver(get_sessions_dir())
                self.service = SessionService(
                    config_service=self.config_service,
                    trace_event_store=self.trace_event_store,
                    workspace_id=self.workspace_id,
                    path_resolver=self.path_resolver,
                    creation_service=self.creation_service,

                )
                # 目录父关系变更的唯一路径：导航 SessionCatalogService（executor）。
                self.catalog = SessionCatalogService(session_service=self.service)
                yield
                self.path_resolver.catalog_store.close()
        finally:
            context.close()

    async def _move_catalog_node(
        self,
        node_id: str,
        parent_node_id: str | None,
    ) -> None:
        intent = create_catalog_intent(
            self.catalog,
            "move_node",
            target_node_id=node_id,
            parent_node_id=parent_node_id,
        )
        receipt = await submit_catalog_intent(self.catalog, intent)
        if receipt.state != "committed":
            raise RuntimeError(receipt.error_detail or receipt.error_code)

    async def _rename_catalog_node(self, node_id: str, name: str):
        intent = create_catalog_intent(
            self.catalog,
            "rename_node",
            target_node_id=node_id,
            name=name,
        )
        receipt = await submit_catalog_intent(self.catalog, intent)
        assert receipt.state == "committed", receipt.error_detail
        return await self.service.get(node_id)

    async def _delete_catalog_session(self, session_id: str):
        receipt = await submit_catalog_intent(self.catalog, create_catalog_intent(
            self.catalog, "delete_session", target_node_id=session_id,
        ))
        assert receipt.state == "committed", receipt.error_detail
        return receipt

    @pytest.mark.asyncio
    async def test_create_session(self):
        request = SessionCreateRequest(title="Test Session")
        session = await self.service.create(request)

        assert session.session_id.startswith("ses_")
        assert len(session.session_id) == 36
        assert session.workspace_id == self.workspace_id
        assert session.title == "Test Session"
        assert session.title_source == "user"
        assert isinstance(session.current_agent_id, str)
        assert session.current_agent_id
        assert session.current_provider_id == "primary"
        assert session.created_at is not None
        assert session.updated_at == session.created_at

        session_file = get_session_file(session.session_id)
        assert session_file.exists()

        data = json.loads(session_file.read_text(encoding="utf-8"))
        assert data["session_id"] == session.session_id
        assert data["workspace_id"] == self.workspace_id
        assert "title" not in data
        assert "title_source" not in data
        assert (
            self.service.path_resolver.get_node(session.session_id).name
            == "Test Session"
        )
        assert set(data) == {"session_id", "workspace_id", "created_at"}
        node = self.path_resolver.catalog_store.get_node(session.session_id)
        assert node.current_agent_id == session.current_agent_id
        assert node.current_provider_id == "primary"
        assert not self.saver._storage.storage.root(
            session.session_id, thread_id=node.main_thread_id,
        ).exists(), "创建Session不得在execution admission之前物化rollout"

    @pytest.mark.asyncio
    async def test_list_uses_index_projection_when_physical_tree_has_orphan(self):
        session = await self.service.create(SessionCreateRequest(title="目录读模型"))
        folder = self.service.path_resolver.get_node(self.service.path_resolver.catalog_store.create_folder(create_prefixed_id("ses"), self.service.path_resolver.workspace_id, None, '归档').node_id)
        await self._move_catalog_node(session.session_id, folder.node_id)

        # 只模拟遗留的空物理目录，不删除或修正它；列表必须保留索引中的会话。
        (get_sessions_dir() / session.session_id).mkdir()

        result = await self.service.list()

        assert [item.session_id for item in result.items] == [session.session_id]
        resolved = self.service.path_resolver.resolve_session_node_for_runtime(
            session.session_id
        )
        # folder 无物理目录，session 物理目录是日期桶；孤立物理目录不进入
        # catalog 读模型（仍按权威 ID 解析）。
        assert resolved.name == session.session_id
        assert resolved.is_relative_to(get_sessions_dir())

    @pytest.mark.asyncio
    async def test_update_session_provider_is_persisted(self):
        config = {
            "llm": {
                "providers": [
                    {
                        "id": "primary",
                        "endpoint": "https://example.com/v1",
                        "model": "model-primary",
                        "api_key": "test-key",
                        "custom_llm_provider": "openai",
                    },
                    {
                        "id": "backup",
                        "endpoint": "https://example.com/v1",
                        "model": "model-backup",
                        "api_key": "test-key",
                        "custom_llm_provider": "openai",
                    },
                ]
            },
            "default_agent": "default",
            "agents": {
                "default": {
                    "name": "Default Agent",
                    "instructions": {"system_prompt": "hello"},
                    "model": {
                        "primary_provider": "primary",
                        "fallback_providers": ["backup"],
                    },
                }
            },
        }
        config_service = SessionServiceConfigStub(config)
        service = SessionService(
            config_service=config_service,
            trace_event_store=self.trace_event_store,
            workspace_id=self.workspace_id,
            path_resolver=self.path_resolver,
            creation_service=self.creation_service,

        )
        created = await service.create(SessionCreateRequest(title="模型持久化"))

        updated = await service.update(
            created.session_id,
            SessionUpdateRequest(provider_id="backup"),
        )

        assert updated.current_provider_id == "backup"
        restarted_service = SessionService(
            config_service=config_service,
            trace_event_store=self.trace_event_store,
            workspace_id=self.workspace_id,
            path_resolver=self.path_resolver,
            creation_service=self.creation_service,

        )
        restored = await restarted_service.get(created.session_id)
        assert restored.current_provider_id == "backup"
        persisted = self.path_resolver.catalog_store.get_node(created.session_id)
        assert persisted.current_provider_id == "backup"

        with pytest.raises(ValueError, match="不允许使用 provider"):
            await service.update(
                created.session_id,
                SessionUpdateRequest(provider_id="unknown"),
            )

    @pytest.mark.asyncio
    async def test_workspace_defaults_only_affect_new_sessions(self):
        config = {
            "llm": {
                "providers": [
                    {
                        "id": "primary",
                        "endpoint": "https://example.com/v1",
                        "model": "model-primary",
                        "api_key": "test-key",
                        "custom_llm_provider": "openai",
                    },
                    {
                        "id": "backup",
                        "endpoint": "https://example.com/v1",
                        "model": "model-backup",
                        "api_key": "test-key",
                        "custom_llm_provider": "openai",
                    },
                ]
            },
            "default_agent": "default",
            "agents": {
                "default": {
                    "name": "Default Agent",
                    "instructions": {"system_prompt": "hello"},
                    "model": {
                        "primary_provider": "primary",
                        "fallback_providers": ["backup"],
                    },
                },
                "coder": {
                    "name": "Coder",
                    "instructions": {"system_prompt": "code"},
                    "model": {
                        "primary_provider": "primary",
                        "fallback_providers": ["backup"],
                    },
                },
            },
        }
        config_service = SessionServiceConfigStub(config)
        service = SessionService(
            config_service=config_service,
            trace_event_store=self.trace_event_store,
            workspace_id=self.workspace_id,
            path_resolver=self.path_resolver,
            creation_service=self.creation_service,

        )
        existing = await service.create(SessionCreateRequest(title="Existing"))

        config_service.set_workspace_default_agent("coder")
        config_service.set_workspace_default_provider("coder", "backup")
        created_after_change = await service.create(
            SessionCreateRequest(title="Created later")
        )

        assert existing.current_agent_id == "default"
        assert existing.current_provider_id == "primary"
        assert (await service.get(existing.session_id)).current_provider_id == "primary"
        assert created_after_change.current_agent_id == "coder"
        assert created_after_change.current_provider_id == "backup"

    @pytest.mark.asyncio
    async def test_create_default_session_title_source(self):
        session = await self.service.create(SessionCreateRequest(title="新会话"))

        assert session.title == "新会话"
        assert session.title_source == "default"

    @pytest.mark.asyncio
    async def test_create_session_with_specified_agent(self):
        request = SessionCreateRequest(title="Agent Session", agent_id="deep_agent")
        session = await self.service.create(request)

        assert session.current_agent_id == "default"

    @pytest.mark.asyncio
    async def test_public_create_rejects_internal_session_kind(self):
        with pytest.raises(ValidationError, match="kind"):
            SessionCreateRequest.model_validate(
                {"title": "Broken", "kind": "context_fork"}
            )

    def test_generic_update_rejects_parent_change(self):
        with pytest.raises(ValidationError, match="parent_session_id"):
            SessionUpdateRequest.model_validate({"parent_session_id": None})

    @pytest.mark.asyncio
    async def test_update_session_can_switch_agent(self):
        created = await self.service.create(SessionCreateRequest(title="Switch Agent Session"))

        updated = await self.service.update(created.session_id, SessionUpdateRequest(agent_id="deep_agent"))

        assert updated.current_agent_id == "deep_agent"

    @pytest.mark.asyncio
    async def test_get_session(self):
        created = await self.service.create(SessionCreateRequest(title="Get Test"))
        retrieved = await self.service.get(created.session_id)

        assert retrieved.session_id == created.session_id
        assert retrieved.title == created.title
        assert retrieved.created_at == created.created_at

    @pytest.mark.asyncio
    async def test_legacy_workspace_id_is_rejected_without_startup_rewrite(self):
        session = await self.service.create(SessionCreateRequest(title="旧 ID 不迁移"))
        session_file = get_session_file(session.session_id)
        data = json.loads(session_file.read_text(encoding="utf-8"))
        data["workspace_id"] = "ws_local"
        session_file.write_text(json.dumps(data), encoding="utf-8")
        persisted_bytes = session_file.read_bytes()

        with pytest.raises(RuntimeError, match="会话与当前工作区后端标识不一致"):
            SessionService(
                config_service=self.config_service,
                trace_event_store=self.trace_event_store,
                workspace_id=self.workspace_id,
                path_resolver=self.path_resolver,
                creation_service=self.creation_service,

            )

        assert session_file.read_bytes() == persisted_bytes

    @pytest.mark.asyncio
    async def test_get_nonexistent_session(self):
        with pytest.raises(NotFoundError, match="Session .* not found"):
            await self.service.get("nonexistent-session-id")

    @pytest.mark.asyncio
    async def test_session_update_does_not_accept_title_changes(self):
        with pytest.raises(ValidationError):
            SessionUpdateRequest.model_validate({"title": "Updated Title"})

    @pytest.mark.asyncio
    async def test_session_parent_relationship_is_persisted_and_can_be_removed(self):
        parent = await self.service.create(SessionCreateRequest(title="Parent"))
        child = await self.service.create(SessionCreateRequest(title="Child"))
        grandchild = await self.service.create(SessionCreateRequest(title="Grandchild"))

        await self._move_catalog_node(
            child.session_id,
            parent.session_id,
        )
        await self._move_catalog_node(grandchild.session_id, child.session_id)

        assert (await self.service.get(child.session_id)).parent_session_id == (
            parent.session_id
        )
        resolver = self.service.path_resolver
        parent_path = resolver.resolve_session_node(parent.session_id)
        # 父子是 catalog 逻辑关系，物理目录都在日期桶下。
        child_path = resolver.resolve_session_node(child.session_id)
        assert child_path.parent == parent_path.parent
        assert resolver.get_node(child.session_id).parent_node_id == (
            parent.session_id
        )

        await self._move_catalog_node(child.session_id, None)

        assert (await self.service.get(child.session_id)).parent_session_id is None
        assert (await self.service.get(grandchild.session_id)).parent_session_id == child.session_id
        assert (
            self.service.path_resolver.resolve_session_node(child.session_id).parent
            == parent_path.parent
        )
        # grandchild 仍是 child 的逻辑后代。
        assert grandchild.session_id in resolver.descendant_session_ids(
            child.session_id, include_self=True
        )

    @pytest.mark.asyncio
    async def test_explicit_move_preserves_physical_name_after_title_update(self):
        session = await self.service.create(SessionCreateRequest(title="Original Title"))
        original_path = self.service.path_resolver.resolve_session_node(
            session.session_id
        )
        target_folder = self.service.path_resolver.get_node(self.service.path_resolver.catalog_store.create_folder(create_prefixed_id("ses"), self.service.path_resolver.workspace_id, None, '目标文件夹').node_id)

        await self._rename_catalog_node(session.session_id, "Updated Title")
        await self._move_catalog_node(session.session_id, target_folder.node_id)

        moved_path = self.service.path_resolver.resolve_session_node(
            session.session_id
        )
        # 物理叶名稳定（日期桶内目录名 == session_id），folder 挂载关系由
        # catalog 承载。
        assert moved_path.name == session.session_id
        assert self.service.path_resolver.get_node(
            session.session_id
        ).parent_node_id == target_folder.node_id
        assert moved_path.name == original_path.name
        assert (await self.service.get(session.session_id)).title == "Updated Title"

    @pytest.mark.asyncio
    async def test_session_parent_relationship_rejects_self_and_cycles(self):
        parent = await self.service.create(SessionCreateRequest(title="Parent"))
        child = await self.service.create(SessionCreateRequest(title="Child"))
        await self._move_catalog_node(child.session_id, parent.session_id)

        # 唯一导航路径下，自环/成环由 store 的 _validate_move_target 以
        # RuntimeError 拒绝（经 executor 归类为 conflict）。
        with pytest.raises(RuntimeError, match="自身"):
            await self._move_catalog_node(parent.session_id, parent.session_id)

        with pytest.raises(RuntimeError, match="循环"):
            await self._move_catalog_node(parent.session_id, child.session_id)

    @pytest.mark.asyncio
    async def test_delete_operation_cascades_catalog_children(self):
        parent = await self.service.create(SessionCreateRequest(title="Parent"))
        child = await self.service.create(SessionCreateRequest(title="Child"))
        grandchild = await self.service.create(SessionCreateRequest(title="Grandchild"))
        await self._move_catalog_node(child.session_id, parent.session_id)
        await self._move_catalog_node(grandchild.session_id, child.session_id)

        parent_path = self.service.path_resolver.resolve_session_node(parent.session_id)
        child_path = self.service.path_resolver.resolve_session_node(child.session_id)
        grandchild_path = self.service.path_resolver.resolve_session_node(
            grandchild.session_id
        )
        # 物理目录均在同一日期桶，级联父子关系由 catalog 承载。
        assert child_path.parent == parent_path.parent
        assert grandchild_path.parent == child_path.parent

        await self._delete_catalog_session(parent.session_id)

        assert not parent_path.exists()
        with pytest.raises(NotFoundError):
            await self.service.get(child.session_id)
        with pytest.raises(NotFoundError):
            await self.service.get(grandchild.session_id)

    @pytest.mark.asyncio
    async def test_delete_session(self):
        created = await self.service.create(SessionCreateRequest(title="Delete Test"))

        session_dir = get_session_path(created.session_id)
        assert session_dir.exists()
        for relative_path in (
            "rollout/rollout.jsonl",
            "rollout/index.sqlite",
            "logs/traces/events.jsonl",
            "logs/llm_requests/1.json",
            "resources/background_tasks.json",
            "context/history.md",
        ):
            artifact = session_dir / relative_path
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("{}\n", encoding="utf-8")

        result = await self._delete_catalog_session(created.session_id)

        assert result.result_node_id == created.session_id
        assert result.state == "committed"
        assert not session_dir.exists()

        with pytest.raises(NotFoundError):
            await self.service.get(created.session_id)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_session(self):
        receipt = await submit_catalog_intent(self.catalog, create_catalog_intent(
            self.catalog, "delete_session", target_node_id=create_prefixed_id("ses"),
        ))
        assert receipt.state == "rejected"
        assert receipt.error_code == "node_not_found"

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        import asyncio

        for i in range(5):
            await self.service.create(SessionCreateRequest(title=f"Session {i}"))
            await asyncio.sleep(0.001)

        result = await self.service.list()

        assert result.total == 5
        assert len(result.items) == 5
        assert [s.title for s in result.items] == ["Session 4", "Session 3", "Session 2", "Session 1", "Session 0"]

    @pytest.mark.asyncio
    async def test_list_sessions_pagination(self):
        import asyncio

        for i in range(15):
            await self.service.create(SessionCreateRequest(title=f"Session {i}"))
            await asyncio.sleep(0.001)

        page1 = await self.service.list(skip=0, limit=5)
        assert len(page1.items) == 5
        assert page1.total == 15
        assert page1.items[0].title == "Session 14"
        assert page1.items[4].title == "Session 10"

        page2 = await self.service.list(skip=5, limit=5)
        assert len(page2.items) == 5
        assert page2.items[0].title == "Session 9"
        assert page2.items[4].title == "Session 5"

    @pytest.mark.asyncio
    async def test_session_persistence(self):
        session = await self.service.create(SessionCreateRequest(title="Persistence Test"))
        retrieved = await self.service.get(session.session_id)

        assert retrieved.session_id == session.session_id
        assert retrieved.title == "Persistence Test"

    @pytest.mark.asyncio
    async def test_session_path_isolation(self):
        session1 = await self.service.create(SessionCreateRequest(title="Session 1"))
        session2 = await self.service.create(SessionCreateRequest(title="Session 2"))

        path1 = get_session_path(session1.session_id)
        path2 = get_session_path(session2.session_id)

        assert path1 != path2
        assert path1.exists()
        assert path2.exists()
        assert path1.is_dir()
        assert path2.is_dir()
        assert get_session_file(session1.session_id).parent == path1
        assert get_session_file(session2.session_id).parent == path2

    @pytest.mark.asyncio
    async def test_list_trace_events_returns_event_union(self):
        created = await self.service.create(SessionCreateRequest(title="Trace Session"))

        trace_dir = get_session_path(created.session_id) / "logs" / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_file = trace_dir / "events.jsonl"

        trace_event = {
            "event_id": "evt_1",
            "session_id": created.session_id,
            "job_id": "job_1",
            "step_id": None,
            "agent_id": "deep_agent",
            "timestamp": "2024-03-09T12:00:00+00:00",
            "type": "agent_start",
            "payload": {
                "message": "hello",
                "agent_id": "deep_agent",
            },
        }

        trace_file.write_text(
            json.dumps(trace_event, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        page = await self.service.list_trace_events(created.session_id)

        assert len(page.items) == 1
        event = page.items[0]
        assert event.type == "agent_start"
        assert event.event_id == "evt_1"
        assert event.job_id == "job_1"
        assert event.phase == "agent"
        assert event.title == "开始执行"
        assert event.content == "hello"
        assert event.raw["payload"]["agent_id"] == "deep_agent"

    @pytest.mark.asyncio
    async def test_list_trace_events_rejects_legacy_format(self):
        created = await self.service.create(SessionCreateRequest(title="Legacy Trace Session"))

        trace_dir = get_session_path(created.session_id) / "logs" / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_file = trace_dir / "events.jsonl"

        legacy_trace_event = {
            "timestamp": 1710000000000,
            "event_type": "agent_start",
            "data": {"message": "hello", "agent_id": "deep_agent"},
        }

        trace_file.write_text(
            json.dumps(legacy_trace_event, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="Trace 事件协议无效"):
            await self.service.list_trace_events(created.session_id)

    @pytest.mark.asyncio
    async def test_stream_trace_events_emits_existing_and_new_events(self):
        created = await self.service.create(SessionCreateRequest(title="Stream Trace Session"))

        trace_dir = get_session_path(created.session_id) / "logs" / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_file = trace_dir / "events.jsonl"

        first_event = {
            "event_id": "evt_1",
            "job_id": "job_1",
            "step_id": None,
            "agent_id": "deep_agent",
            "timestamp": "2024-03-09T12:00:00+00:00",
            "type": "agent_start",
            "payload": {"message": "hello", "agent_id": "deep_agent"},
        }
        trace_file.write_text(
            json.dumps(first_event, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        stream = self.service.stream_trace_events(created.session_id)

        first_trace, first_cursor = await asyncio.wait_for(
            stream.__anext__(),
            timeout=1.0,
        )
        assert first_trace.event_id == "evt_1"
        assert first_trace.type == "agent_start"
        assert first_cursor.startswith("tc1.")

        second_event = {
            "event_id": "evt_2",
            "part_id": "run_search_files",
            "job_id": "job_1",
            "step_id": None,
            "agent_id": "deep_agent",
            "timestamp": "2024-03-09T12:00:01+00:00",
            "type": "tool_call_start",
            "payload": {
                "execution_id": "run_search_files",
                "tool_name": "search_files",
                "args": {},
            },
        }
        trace_file.write_text(
            trace_file.read_text(encoding="utf-8")
            + json.dumps(second_event, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )

        second, second_cursor = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert second.event_id == "evt_2"
        assert second.type == "tool_call_start"
        assert second.phase == "tool"
        assert second.title == "调用工具"
        assert second.tool_name == "search_files"
        assert second.content == "正在调用 search_files"
        assert second.skill_names == []
        assert second_cursor.startswith("tc1.")

    @pytest.mark.asyncio
    async def test_trace_event_mapper_maps_tool_events(self):
        from app.services.mapping.trace_event_mapper import TraceEventMapper

        mapper = TraceEventMapper()

        event = mapper.map_one(
            {
                "event_id": "evt_tool",
                "job_id": "job_1",
                "step_id": "step_1",
                "timestamp": "2024-03-09T12:00:00+00:00",
                "type": "tool_call_end",
                "payload": {"tool_name": "search_files"},
            },
            session_id="ses_1",
        )

        assert event is not None
        assert event.type == "tool_call_end"
        assert event.phase == "tool"
        assert event.title == "工具返回"
        assert event.content == "工具 search_files 已返回结果"
        assert event.tool_name == "search_files"
        assert event.status == "completed"

    @pytest.mark.asyncio
    async def test_trace_event_mapper_maps_tool_skill_names(self):
        from app.services.mapping.trace_event_mapper import TraceEventMapper

        mapper = TraceEventMapper()

        event = mapper.map_one(
            {
                "event_id": "evt_tool_skill",
                "job_id": "job_1",
                "step_id": "step_1",
                "timestamp": "2024-03-09T12:00:00+00:00",
                "type": "tool_call_start",
                "payload": {
                    "tool_name": "test_tool_2",
                    "args": {},
                    "skill_names": ["test-tool-2"],
                },
            },
            session_id="ses_1",
        )

        assert event is not None
        assert event.type == "tool_call_start"
        assert event.tool_name == "test_tool_2"
        assert event.skill_names == ["test-tool-2"]

    @pytest.mark.asyncio
    async def test_delete_cleans_all_files(self):
        session = await self.service.create(SessionCreateRequest(title="Cleanup Test"))
        session_dir = get_session_path(session.session_id)

        extra_file = session_dir / "extra.txt"
        extra_file.write_text("test content")
        sub_dir = session_dir / "subdir"
        sub_dir.mkdir()
        (sub_dir / "nested.txt").write_text("nested content")

        await self._delete_catalog_session(session.session_id)

        assert not session_dir.exists()

    @pytest.mark.asyncio
    async def test_control_session(self):
        session = await self.service.create(SessionCreateRequest(title="Control Test"))

        result = await self.service.control(session.session_id, "pause", {"reason": "test"})

        assert result.session_id == session.session_id
        assert result.action == "pause"
        assert result.status == "executed"

    @pytest.mark.asyncio
    async def test_control_nonexistent_session(self):
        with pytest.raises(NotFoundError):
            await self.service.control("nonexistent", "pause")


class TestSessionListReadAmplification:
    """§10.2：会话列表读取工作量必须与页大小相关，不随会话总数增长。"""

    workspace_id = "00000000-0000-4000-8000-000000000001"

    @pytest.fixture()
    def workspace_base(self, request: pytest.FixtureRequest) -> Iterator[Path]:
        context, workspace_base, _ = prepare_session_test_workspace(
            test_file=Path(request.node.path),
            node_id=request.node.nodeid,
        )
        try:
            yield workspace_base
        finally:
            context.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("population", [30, 200])
    async def test_list_projects_only_current_page_without_manifests(
        self,
        workspace_base: Path,
        session_config_service: SessionServiceConfigStub,
        monkeypatch: pytest.MonkeyPatch,
        request: pytest.FixtureRequest,
        population: int,
    ):
        from tests.unit.core.catalog_workspace_helper import build_catalog_workspace

        workspace = build_catalog_workspace(
            workspace_base,
            workspace_id=self.workspace_id,
        )
        try:
            sessions_dir = workspace.sessions_root
            service = SessionService(
                config_service=session_config_service,
                trace_event_store=TraceEventStore(sessions_dir=sessions_dir),
                workspace_id=self.workspace_id,
                path_resolver=workspace.resolver,
                creation_service=workspace.creation_service,

            )
            # 显式冻结 provider，使读取计数只反映 manifest 读取，不触发
            # provider 解析（与外部环境变量无关）。
            session_ids = [
                await workspace.create_session(
                    f"读放大-{index:02d}", current_provider_id="primary"
                )
                for index in range(population)
            ]

            # 统计 session.json 读次数（生产读路径唯一入口是 Path.read_text）。
            original_read_text = Path.read_text
            reads: list[str] = []

            def counting_read_text(self, *args, **kwargs):
                reads.append(str(self))
                return original_read_text(self, *args, **kwargs)

            monkeypatch.setattr(Path, "read_text", counting_read_text)
            original_project = service.project_catalog_session
            projections: list[str] = []
            original_node_from_row = workspace.store._node_from_row
            full_rows: list[str] = []

            def counted_projection(node):
                projections.append(node.node_id)
                return original_project(node)

            def counted_full_row(row):
                full_rows.append(str(row["node_id"]))
                return original_node_from_row(row)

            monkeypatch.setattr(service, "project_catalog_session", counted_projection)
            monkeypatch.setattr(workspace.store, "_node_from_row", counted_full_row)
            returned_rows: list[tuple[str, ...]] = []
            original_factory = workspace.store.connection.row_factory

            def counted_query_row(cursor, row):
                returned_rows.append(tuple(column[0] for column in cursor.description))
                return original_factory(cursor, row)

            workspace.store.connection.row_factory = counted_query_row
            udf_calls: list[str] = []

            def rejected_query_udf(*_args):
                udf_calls.append("逐成员 Python 回调")
                raise AssertionError("分页查询必须消费写入时已派生的索引值")

            workspace.store.connection.create_function("session_list_created_us", 1, rejected_query_udf, deterministic=True)
            workspace.store.connection.create_function("session_list_member_part", 3, rejected_query_udf, deterministic=True)

            measured = []
            pages = []
            for limit, offset in ((5, 0), (20, 0), (50, 0), (5, 10)):
                reads.clear()
                projections.clear()
                full_rows.clear()
                returned_rows.clear()
                udf_calls.clear()
                started = perf_counter()
                page = await service.list(limit=limit, skip=offset)
                elapsed_ms = (perf_counter() - started) * 1000
                manifest_reads = sum(path.endswith("session.json") for path in reads)
                assert manifest_reads == 0
                assert len(projections) == len(full_rows) == min(limit, population - offset)
                assert page.total == population
                # 包含此前漏掉的轻量结果；根级页只返回1条COUNT/指纹摘要
                # 与本页完整行，不允许全表成员 fetchall 或 Python UDF 全扫。
                assert len(returned_rows) == len(page.items) + 1
                assert udf_calls == []
                measured.append({
                    "population": population, "limit": limit, "offset": offset,
                    "manifest_read_api_count": manifest_reads, "elapsed_ms": elapsed_ms,
                    "dto_projection_count": len(projections), "complete_node_count": len(full_rows),
                    "all_query_result_rows": len(returned_rows), "scalar_summary_rows": 1,
                    "query_udf_calls": len(udf_calls),
                })
                pages.append((offset, page))

            workspace.store.connection.row_factory = sqlite3.Row
            # 外部契约逐字段不变：list 页与逐会话 get() 完全一致。
            got_by_id = {
                session_id: await service.get(session_id)
                for session_id in session_ids
            }
            expected_order = sorted(
                got_by_id.values(),
                key=lambda item: item.created_at,
                reverse=True,
            )
            for offset, page in pages:
                expected_page = expected_order[offset:offset + len(page.items)]
                assert [item.session_id for item in page.items] == [item.session_id for item in expected_page]
                for listed, expected in zip(page.items, expected_page, strict=True):
                    assert listed.model_dump() == expected.model_dump()
            artifacts = TestRunContext.from_test_file(Path(request.node.path)).artifacts_dir
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / f"session-list-read-work-{population}.json").write_text(
                json.dumps(measured, ensure_ascii=False, indent=2) + "\n",
            )
        finally:
            workspace.close()
