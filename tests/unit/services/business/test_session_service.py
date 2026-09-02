import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.exceptions import NotFoundError
from app.core.path_utils import get_session_file, get_session_path, get_sessions_dir
from app.schemas.internal_v2.session import (
    SessionCreateRequest,
    SessionUpdateRequest,
)
from app.services.business.session_service import SessionService
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.trace_event_store import TraceEventStore


class TestSessionService:
    """测试会话服务功能"""

    def setup_method(self):
        """每个测试前设置临时会话目录"""
        self.temp_dir = tempfile.mkdtemp()
        import os

        self.original_workspace = os.environ.get("WORKSPACE_ROOT")
        os.environ["WORKSPACE_ROOT"] = self.temp_dir

        get_sessions_dir().mkdir(exist_ok=True, parents=True)
        self.trace_event_store = TraceEventStore(sessions_dir=get_sessions_dir())
        self.service = SessionService(
            config_service=ConfigService(),
            trace_event_store=self.trace_event_store,
        )

    def teardown_method(self):
        """测试后恢复原始目录"""
        import os
        import shutil

        if self.original_workspace is not None:
            os.environ["WORKSPACE_ROOT"] = self.original_workspace
        else:
            os.environ.pop("WORKSPACE_ROOT", None)

        shutil.rmtree(self.temp_dir)

    @pytest.mark.asyncio
    async def test_create_session(self):
        request = SessionCreateRequest(title="Test Session")
        session = await self.service.create(request)

        assert session.session_id.startswith("ses_")
        assert len(session.session_id) == 36
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
        assert data["title"] == "Test Session"
        assert data["title_source"] == "user"
        assert data["current_agent_id"] == session.current_agent_id
        assert data["current_provider_id"] == "primary"

    @pytest.mark.asyncio
    async def test_list_uses_index_projection_when_physical_tree_has_orphan(self):
        session = await self.service.create(SessionCreateRequest(title="目录读模型"))
        folder = self.service.path_resolver.create_folder(
            name="归档",
            parent_node_id=None,
        )
        await self.service.move_session(session.session_id, folder.node_id)

        # 只模拟遗留的空物理目录，不删除或修正它；列表必须保留索引中的会话。
        (get_sessions_dir() / session.session_id).mkdir()

        result = await self.service.list()

        assert [item.session_id for item in result.items] == [session.session_id]
        assert self.service.path_resolver.resolve_session_node_for_runtime(
            session.session_id
        ) == get_sessions_dir() / folder.node_id / session.session_id

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
        config_path = Path(self.temp_dir) / "workspace.jsonc"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        config_service = ConfigService(config_path=config_path)
        service = SessionService(
            config_service=config_service,
            trace_event_store=self.trace_event_store,
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
        )
        restored = await restarted_service.get(created.session_id)
        assert restored.current_provider_id == "backup"
        persisted = json.loads(
            get_session_file(created.session_id).read_text(encoding="utf-8")
        )
        assert persisted["current_provider_id"] == "backup"

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
        config_path = Path(self.temp_dir) / "workspace-defaults.jsonc"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        config_service = ConfigService(
            config_path=config_path,
            workspace_root=self.temp_dir,
        )
        service = SessionService(
            config_service=config_service,
            trace_event_store=self.trace_event_store,
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
    async def test_create_delegated_session_persists_provenance(self):
        parent = await self.service.create(SessionCreateRequest(title="Parent"))

        child = await self.service.create_delegated(
            title="Delegated",
            agent_id=parent.current_agent_id,
            parent_session_id=parent.session_id,
            parent_job_id="job_parent",
            parent_tool_call_id="call_task",
            subagent_type="general-purpose",
        )

        assert child.parent_session_id == parent.session_id
        assert child.kind == "delegated"
        assert child.delegation is not None
        assert child.delegation.parent_tool_call_id == "call_task"
        persisted = await self.service.get(child.session_id)
        assert persisted == child

        failed = await self.service.set_delegation_start_result(
            child.session_id,
            status="failed",
            error="调度器不可用",
        )
        assert failed.delegation is not None
        assert failed.delegation.start_status == "failed"
        assert failed.delegation.start_error == "调度器不可用"
        assert await self.service.get(child.session_id) == failed

    @pytest.mark.asyncio
    async def test_public_create_rejects_internal_session_kind(self):
        with pytest.raises(ValidationError, match="kind"):
            SessionCreateRequest.model_validate(
                {"title": "Broken", "kind": "delegated"}
            )

    def test_generic_update_rejects_parent_change(self):
        with pytest.raises(ValidationError, match="parent_session_id"):
            SessionUpdateRequest.model_validate({"parent_session_id": None})

    @pytest.mark.asyncio
    async def test_detaching_delegated_session_keeps_immutable_provenance(self):
        parent = await self.service.create(SessionCreateRequest(title="Parent"))
        child = await self.service.create_delegated(
            title="Delegated",
            agent_id=parent.current_agent_id,
            parent_session_id=parent.session_id,
            parent_job_id="job_parent",
            parent_tool_call_id="call_task",
            subagent_type="general-purpose",
        )

        detached = await self.service.move_session(child.session_id, None)

        assert detached.parent_session_id is None
        assert detached.kind == "delegated"
        assert detached.delegation is not None
        assert detached.delegation.parent_session_id == parent.session_id

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
    async def test_get_nonexistent_session(self):
        with pytest.raises(NotFoundError, match="Session .* not found"):
            await self.service.get("nonexistent-session-id")

    @pytest.mark.asyncio
    async def test_update_session(self):
        created = await self.service.create(SessionCreateRequest(title="Original Title"))
        original_updated_at = created.updated_at
        original_path = self.service.path_resolver.resolve_session_node(
            created.session_id
        )

        updated = await self.service.update(created.session_id, SessionUpdateRequest(title="Updated Title"))

        assert updated.title == "Updated Title"
        assert updated.title_source == "user"
        assert updated.session_id == created.session_id
        assert updated.updated_at > original_updated_at
        assert self.service.path_resolver.resolve_session_node(
            created.session_id
        ) == original_path

        session_file = get_session_file(created.session_id)
        data = json.loads(session_file.read_text(encoding="utf-8"))
        assert data["title"] == "Updated Title"
        assert data["title_source"] == "user"

    @pytest.mark.asyncio
    async def test_update_session_can_mark_auto_title_source(self):
        created = await self.service.create(SessionCreateRequest(title="新会话"))

        updated = await self.service.update(
            created.session_id,
            SessionUpdateRequest(title="hello world", title_source="auto"),
        )

        assert updated.title == "hello world"
        assert updated.title_source == "auto"

    @pytest.mark.asyncio
    async def test_session_parent_relationship_is_persisted_and_can_be_removed(self):
        parent = await self.service.create(SessionCreateRequest(title="Parent"))
        child = await self.service.create(SessionCreateRequest(title="Child"))
        grandchild = await self.service.create(SessionCreateRequest(title="Grandchild"))

        bound_child = await self.service.move_session(
            child.session_id,
            parent.session_id,
        )
        await self.service.move_session(grandchild.session_id, child.session_id)

        assert bound_child.parent_session_id == parent.session_id
        assert (await self.service.get(child.session_id)).parent_session_id == parent.session_id
        parent_path = self.service.path_resolver.resolve_session_node(parent.session_id)
        assert (
            self.service.path_resolver.resolve_session_node(child.session_id).parent
            == parent_path / "children"
        )

        unbound_child = await self.service.move_session(child.session_id, None)

        assert unbound_child.parent_session_id is None
        assert (await self.service.get(grandchild.session_id)).parent_session_id == child.session_id
        assert (
            self.service.path_resolver.resolve_session_node(child.session_id).parent
            == parent_path.parent
        )
        assert self.service.path_resolver.resolve_session_node(
            grandchild.session_id
        ).is_relative_to(self.service.path_resolver.resolve_session_node(child.session_id))

    @pytest.mark.asyncio
    async def test_moving_parent_session_carries_complete_child_tree(self):
        parent = await self.service.create(SessionCreateRequest(title="Parent"))
        child = await self.service.create_context_fork(
            title="Child",
            agent_id=parent.current_agent_id,
            parent_session_id=parent.session_id,
            context_source_session_id=parent.session_id,
        )
        target_folder = self.service.path_resolver.create_folder(
            name="目标文件夹",
            parent_node_id=None,
        )
        child_path_before = self.service.path_resolver.resolve_session_node(
            child.session_id
        )

        moved = await self.service.move_to_folder(
            parent.session_id,
            target_folder.node_id,
        )

        moved_parent_path = self.service.path_resolver.resolve_session_node(
            parent.session_id
        )
        moved_child_path = self.service.path_resolver.resolve_session_node(
            child.session_id
        )
        assert moved.parent_session_id is None
        assert moved_parent_path.parent == target_folder.path
        assert moved_child_path.parent == moved_parent_path / "children"
        assert moved_child_path.name == child_path_before.name

    @pytest.mark.asyncio
    async def test_explicit_move_preserves_physical_name_after_title_update(self):
        session = await self.service.create(SessionCreateRequest(title="Original Title"))
        original_path = self.service.path_resolver.resolve_session_node(
            session.session_id
        )
        target_folder = self.service.path_resolver.create_folder(
            name="目标文件夹",
            parent_node_id=None,
        )

        await self.service.update(
            session.session_id,
            SessionUpdateRequest(title="Updated Title"),
        )
        await self.service.move_to_folder(session.session_id, target_folder.node_id)

        moved_path = self.service.path_resolver.resolve_session_node(
            session.session_id
        )
        assert moved_path.parent == target_folder.path
        assert moved_path.name == original_path.name
        assert (await self.service.get(session.session_id)).title == "Updated Title"

    @pytest.mark.asyncio
    async def test_session_parent_relationship_rejects_self_and_cycles(self):
        parent = await self.service.create(SessionCreateRequest(title="Parent"))
        child = await self.service.create(SessionCreateRequest(title="Child"))
        await self.service.move_session(child.session_id, parent.session_id)

        with pytest.raises(ValueError, match="自身"):
            await self.service.move_session(parent.session_id, parent.session_id)

        with pytest.raises(ValueError, match="循环"):
            await self.service.move_session(parent.session_id, child.session_id)

    @pytest.mark.asyncio
    async def test_delete_parent_requires_confirmation_and_cascades_children(self):
        parent = await self.service.create(SessionCreateRequest(title="Parent"))
        child = await self.service.create(SessionCreateRequest(title="Child"))
        grandchild = await self.service.create(SessionCreateRequest(title="Grandchild"))
        await self.service.move_session(child.session_id, parent.session_id)
        await self.service.move_session(grandchild.session_id, child.session_id)

        parent_path = self.service.path_resolver.resolve_session_node(parent.session_id)
        child_path = self.service.path_resolver.resolve_session_node(child.session_id)
        grandchild_path = self.service.path_resolver.resolve_session_node(
            grandchild.session_id
        )
        assert child_path.parent == parent_path / "children"
        assert grandchild_path.parent == child_path / "children"

        with pytest.raises(RuntimeError, match="显式确认级联删除"):
            await self.service.delete(parent.session_id)

        await self.service.delete(parent.session_id, cascade=True)

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

        result = await self.service.delete(created.session_id)

        assert result.session_id == created.session_id
        assert result.status == "deleted"
        assert result.cleaned_execution_runs == 0
        assert not session_dir.exists()

        with pytest.raises(NotFoundError):
            await self.service.get(created.session_id)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_session(self):
        with pytest.raises(NotFoundError, match="Session .* not found"):
            await self.service.delete("nonexistent-session-id")

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

        await self.service.delete(session.session_id)

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
