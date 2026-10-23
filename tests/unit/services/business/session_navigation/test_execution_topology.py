"""导航入队不占 topology；唯一 worker 在实际写入时竞争真实 OS 锁。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.session_lifecycle_gate import _CrossProcessFileLock
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
    NavigationMutationIntentDTO,
)
from tests.unit.services.business.session_navigation.test_operations_queue import (
    _Stack,
    operation_id,
)
from tests.unit.services.business.session_navigation.test_operations_queue import (
    navigation_workspace as navigation_workspace,  # noqa: PLC0414
)


@pytest.mark.parametrize("action", ["create", "rename", "move", "delete_empty", "delete_recursive"])
async def test_enqueue_remains_available_while_worker_waits_for_topology(
    navigation_workspace: Path, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    initial = [stack.create_folder_intent(seed=f"{action}-{name}", sequence=index, name=name)
               for index, name in enumerate(["待修改", "目标父目录"], start=1)]
    try:
        await stack.enqueue(NavigationMutationEnqueueRequest(intents=initial), stack.scope)
        await stack.settle(*(intent.client_operation_id for intent in initial))
        target, parent = (stack.node_id(intent.client_operation_id) for intent in initial)
        baseline = stack.resolver.list_nodes()
        fields: dict[str, object] = {
            "client_operation_id": operation_id(f"gate-{action}"),
            "client_sequence": 1,
            "base_catalog_revision": stack.resolver.revision,
        }
        if action == "create":
            fields.update(kind="create_folder", name="新目录")
        elif action == "rename":
            fields.update(kind="rename_node", target_node_id=target, name="新显示名",
                          expected_revision=stack.service.node_revision(target))
        elif action == "move":
            fields.update(kind="move_node", target_node_id=target, parent_node_id=parent,
                          expected_revision=stack.service.node_revision(target))
        else:
            fields.update(kind="delete_folder", target_node_id=target,
                          recursive=action == "delete_recursive")
        intent = NavigationMutationIntentDTO.model_validate(fields)
        attempted, acquired = asyncio.Event(), asyncio.Event()
        other_attempts: list[asyncio.Task[object] | None] = []
        real_enter = _CrossProcessFileLock.__aenter__
        topology = stack.executor._gate

        async def observe_lock(lock: _CrossProcessFileLock):
            # 只观测并继续真实 portalocker 获取；不替换锁或执行结果。
            watched = lock.path == topology.lock_path
            worker = asyncio.current_task() is stack.service._worker_task
            if watched:
                if worker:
                    assert lock._exclusive
                    attempted.set()
                else:
                    other_attempts.append(asyncio.current_task())
            result = await real_enter(lock)
            if watched and worker:
                acquired.set()
            return result

        async with topology.exclusive():
            monkeypatch.setattr(_CrossProcessFileLock, "__aenter__", observe_lock)
            accepted = await asyncio.wait_for(stack.service.enqueue(
                NavigationMutationEnqueueRequest(intents=[intent]), stack.scope,
            ), timeout=3)
            assert accepted.accepted_count == 1
            assert accepted.receipts[0].state == "queued"
            await asyncio.wait_for(attempted.wait(), timeout=3)
            assert not other_attempts, "接受队列命令不应竞争 topology 锁"
            assert not acquired.is_set(), "worker 必须等待现有 topology owner 释放"
            assert stack.resolver.list_nodes() == baseline
            pending = stack.service.record(intent.client_operation_id, stack.scope)
            assert pending.state in {"queued", "running"}
            assert not pending.is_terminal

        terminal = await asyncio.wait_for(stack.service.await_terminal(
            intent.client_operation_id, stack.scope,
        ), timeout=5)
        assert acquired.is_set()
        assert terminal.state == "committed", terminal.error_detail
        assert stack.resolver.list_nodes() != baseline
        if action.startswith("delete"):
            with pytest.raises(KeyError):
                stack.resolver.get_node(target)
        elif action == "move":
            assert stack.resolver.get_node(target).parent_node_id == parent
        elif action == "rename":
            assert stack.resolver.get_node(target).name == "新显示名"
        else:
            assert stack.resolver.get_node(terminal.result_node_id).name == "新目录"
    finally:
        await stack.service.stop()
