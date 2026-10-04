"""会话目录异步 mutation 协议的单测（OpenSpec 8.1-G/8.1-H）。

覆盖批内/批间幂等与冲突、terminal tombstone 防迟到重放、依赖失败后继终结、
worker 崩溃接管、同 node 串行编辑不误判冲突、其它客户端修改明确拒绝、
事件 cursor 边界，以及同步 façade 走同一写路径。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_catalog_store.contracts import (
    CatalogTransactionHook,
    SubtreeDeleteRecord,
)
from app.core.session_subtree_delete import SubtreeDeleteResult
from app.core.sqlite_state import utc_now_text
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
    NavigationMutationEnqueueResultDTO,
    NavigationMutationIntentDTO,
)
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_navigation.executor import NavigationMutationExecutor
from app.services.business.session_navigation.operations_service import (
    NavigationAuthScope,
    SessionCatalogOperationsService,
    local_navigation_scope,
)
from app.services.business.session_navigation.queue_store import (
    NavigationMutationConflictError,
    NavigationMutationQueueStore,
    NavigationQueueOwnerError,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.canonical_id_at import uuid7_hex_from_name
from tests.support.workspaces import prepare_default_test_workspace


def canonical(name: str) -> str:
    """确定性 canonical session ID（v7 位 profile，仅测试播种用）。"""
    return f"ses_{uuid7_hex_from_name(name)}"


def operation_id(seed: str) -> str:
    """确定性 ``op_`` operation ID（同款 canonical 前缀形态）。"""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"op_{digest[:32]}"


class _SessionService:
    """最小 session service 桩：只提供 resolver 与元数据读取。"""

    def __init__(self, sessions_root: Path) -> None:
        self.path_resolver = get_session_path_resolver(sessions_root)
        self.path_resolver.initialize()

    def register_change_listener(self, listener) -> None:
        del listener

    async def get(self, session_id: str) -> SessionDTO:
        import json

        session_path = self.path_resolver.resolve_session_node_for_runtime(session_id)
        payload = json.loads((session_path / "session.json").read_text(encoding="utf-8"))
        payload["title"] = self.path_resolver.get_node(session_id).name
        payload.setdefault("workspace_id", "ws-test")
        payload.setdefault("current_agent_id", "test-agent")
        return SessionDTO.model_validate(payload)


class _Stack:
    """测试装配：共享同一 store 的 operations service 与 executor。"""

    def __init__(self, sessions_root: Path) -> None:
        self.session_service = _SessionService(sessions_root)
        self.resolver: SessionCatalogPathResolver = self.session_service.path_resolver
        self.store = self.resolver.catalog_store
        self.workspace_id = self.resolver.workspace_id
        self.scope = local_navigation_scope(self.workspace_id)
        self.queue = NavigationMutationQueueStore(self.store)
        self.executor = NavigationMutationExecutor(
            store=self.store,
            workspace_id=self.workspace_id,
            queue=self.queue,
            path_resolver=self.resolver,
        )
        self.service = SessionCatalogOperationsService(
            store=self.store,
            workspace_id=self.workspace_id,
            queue=self.queue,
            executor=self.executor,
        )

    def create_folder_intent(
        self,
        *,
        seed: str,
        sequence: int,
        name: str,
        parent_node_id: str | None = None,
        created_by_operation_id: str | None = None,
    ) -> NavigationMutationIntentDTO:
        return NavigationMutationIntentDTO(
            client_operation_id=operation_id(seed),
            client_sequence=sequence,
            kind="create_folder",
            base_catalog_revision=self.resolver.revision,
            name=name,
            parent_node_id=parent_node_id,
            created_by_operation_id=created_by_operation_id,
        )

    def node_id(self, op_id: str) -> str:
        record = self.queue.get_record(
            gateway_id=self.scope.gateway_id,
            workspace_id=self.workspace_id,
            actor=self.scope.actor,
            operation_id=op_id,
        )
        assert record is not None
        assert record.result_node_id is not None
        return record.result_node_id

    async def settle(self, *operation_ids: str) -> list:
        """等待给定 operation 全部到达终态（后台 worker 可能并发认领，按 ID 等）。"""
        await self.service.start()
        records = [
            await self.service.await_terminal(operation_id, self.scope)
            for operation_id in operation_ids
        ]
        return records

    async def enqueue(
        self,
        request: NavigationMutationEnqueueRequest,
        scope: NavigationAuthScope,
    ) -> NavigationMutationEnqueueResultDTO:
        """模拟由应用 lifespan 显式启动 worker 后接受导航请求。"""
        await self.service.start()
        return await self.service.enqueue(request, scope)


def _enqueue_without_worker(
    stack: _Stack,
    intents: list[NavigationMutationIntentDTO],
) -> None:
    with stack.store.write_transaction() as connection:
        stack.queue.enqueue_batch(
            connection,
            gateway_id=stack.scope.gateway_id,
            workspace_id=stack.workspace_id,
            actor=stack.scope.actor,
            intents=intents,
            now=utc_now_text(),
        )


@pytest.mark.parametrize("dependency_kind", ["depends_on", "created_by_operation_id"])
@pytest.mark.asyncio
async def test_enqueue_rejects_forward_dependency_in_same_batch(
    navigation_workspace: Path,
    dependency_kind: str,
) -> None:
    """同批依赖若排在后面，必须拒绝而不能让严格 FIFO 队首永久停滞。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    later = stack.create_folder_intent(seed="forward_dependency", sequence=2, name="后置")
    dependent_fields: dict[str, object] = {
        "client_operation_id": operation_id(f"forward_dependent_{dependency_kind}"),
        "client_sequence": 1,
        "kind": "create_folder",
        "base_catalog_revision": stack.resolver.revision,
        "name": "前置错误的依赖方",
    }
    dependent_fields[dependency_kind] = (
        [later.client_operation_id]
        if dependency_kind == "depends_on"
        else later.client_operation_id
    )
    dependent = NavigationMutationIntentDTO.model_validate(dependent_fields)
    request = NavigationMutationEnqueueRequest(intents=[dependent, later])

    with pytest.raises(ValueError, match="同批依赖必须先于依赖方入队"):
        await stack.enqueue(request, stack.scope)

    assert stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=dependent.client_operation_id,
    ) is None
    assert stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=later.client_operation_id,
    ) is None


def _independent_service(stack: _Stack):
    store = SessionCatalogStore(stack.store.database_path, stack.store.sessions_root)
    queue = NavigationMutationQueueStore(store)
    executor = NavigationMutationExecutor(
        store=store,
        workspace_id=stack.workspace_id,
        queue=queue,
        path_resolver=stack.resolver,
    )
    service = SessionCatalogOperationsService(
        store=store,
        workspace_id=stack.workspace_id,
        queue=queue,
        executor=executor,
    )
    return store, queue, executor, service


@pytest.fixture
def navigation_workspace(request: pytest.FixtureRequest) -> Path:
    """为该测试节点复制独立完整 fixture，避免跨测试复用 SQLite 连接。"""
    context = TestRunContext.from_test_file(Path(request.node.path))
    template = Path.cwd() / "tests" / "fixtures" / "workspaces" / "default_test_workspace"
    return prepare_default_test_workspace(
        workspace_root=(
            context.workspace_root
            / f"node-{hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:12]}"
        ),
        template_root=template,
    )


@pytest.mark.asyncio
async def test_running_queue_head_blocks_later_candidate(
    navigation_workspace: Path,
) -> None:
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    first = stack.create_folder_intent(seed="running_head", sequence=1, name="队首")
    second = stack.create_folder_intent(seed="running_next", sequence=2, name="后继")
    _enqueue_without_worker(stack, [first, second])
    with stack.store.write_transaction() as connection:
        connection.execute(
            "UPDATE navigation_mutation_records SET state = 'running', "
            "holder_id = 'old-owner:1' WHERE operation_id = ?",
            (first.client_operation_id,),
        )

    with stack.store.read_transaction() as connection:
        candidate = stack.queue.next_runnable(connection, stack.workspace_id)
        later = stack.queue.fetch_record_in(
            connection,
            gateway_id=stack.scope.gateway_id,
            workspace_id=stack.workspace_id,
            actor=stack.scope.actor,
            operation_id=second.client_operation_id,
        )

    assert candidate is None
    assert later is not None and later.state == "queued"


@pytest.mark.asyncio
async def test_claim_rechecks_queue_head_after_candidate_selection(
    navigation_workspace: Path,
) -> None:
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    first = stack.create_folder_intent(seed="stale_head_first", sequence=1, name="先")
    second = stack.create_folder_intent(seed="stale_head_second", sequence=2, name="后")
    _enqueue_without_worker(stack, [first, second])
    with stack.store.write_transaction() as connection:
        connection.execute(
            "UPDATE navigation_mutation_records SET state = 'committed' "
            "WHERE operation_id = ?",
            (first.client_operation_id,),
        )
    with stack.store.read_transaction() as connection:
        stale_candidate = stack.queue.next_runnable(connection, stack.workspace_id)
    assert stale_candidate is not None
    assert stale_candidate.operation_id == second.client_operation_id
    with stack.store.write_transaction() as connection:
        connection.execute(
            "UPDATE navigation_mutation_records SET state = 'queued' "
            "WHERE operation_id = ?",
            (first.client_operation_id,),
        )

    async with stack.executor.queue_owner() as owner:
        with stack.store.write_transaction() as connection:
            claimed = stack.queue.claim_running(
                connection,
                record=stale_candidate,
                holder_id=owner.holder_id,
                owner_id=owner.owner_id,
                owner_generation=owner.generation,
                now=utc_now_text(),
            )
        assert claimed is None


@pytest.mark.asyncio
async def test_second_worker_cannot_recover_or_skip_a_live_running_owner(
    navigation_workspace: Path,
) -> None:
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    created = stack.create_folder_intent(seed="owner_delete", sequence=1, name="待删")
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[created]), stack.scope
    )
    await stack.settle(created.client_operation_id)
    folder_id = stack.node_id(created.client_operation_id)

    delete_started = asyncio.Event()
    release_delete = asyncio.Event()

    async def blocked_delete(
        operation_id: str,
        root_node_id: str,
        mark_transaction_hook: CatalogTransactionHook,
        finish_transaction_hook: CatalogTransactionHook,
    ):
        delete_started.set()
        await release_delete.wait()
        return await stack.resolver.delete_subtree(
            idempotency_key=operation_id,
            root_node_id=root_node_id,
            mark_transaction_hook=mark_transaction_hook,
            finish_transaction_hook=finish_transaction_hook,
        )

    stack.executor._delete_runner = blocked_delete
    deletion = NavigationMutationIntentDTO(
        client_operation_id=operation_id("live_owner_delete"),
        client_sequence=1,
        kind="delete_folder",
        base_catalog_revision=stack.resolver.revision,
        target_node_id=folder_id,
        recursive=True,
    )
    second_store, second_queue, second_executor, second_service = _independent_service(
        stack
    )
    successor = stack.create_folder_intent(
        seed="live_owner_successor", sequence=1, name="不得越过的后继"
    )
    try:
        with pytest.raises(NavigationQueueOwnerError, match="启动恢复超时"):
            await second_service.start(owner_timeout_seconds=0.05)
        await stack.enqueue(
            NavigationMutationEnqueueRequest(intents=[deletion]), stack.scope
        )
        await asyncio.wait_for(delete_started.wait(), timeout=5)
        await stack.service.enqueue(
            NavigationMutationEnqueueRequest(intents=[successor]), stack.scope
        )
        with stack.store.read_transaction() as connection:
            owner_before = connection.execute(
                "SELECT owner_id, owner_generation FROM navigation_queue_owners "
                "WHERE workspace_id = ?",
                (stack.workspace_id,),
            ).fetchone()
        await asyncio.sleep(0.1)
        with second_store.read_transaction() as connection:
            owner_after = connection.execute(
                "SELECT owner_id, owner_generation FROM navigation_queue_owners "
                "WHERE workspace_id = ?",
                (stack.workspace_id,),
            ).fetchone()
            running = second_queue.fetch_record_in(
                connection,
                gateway_id=stack.scope.gateway_id,
                workspace_id=stack.workspace_id,
                actor=stack.scope.actor,
                operation_id=deletion.client_operation_id,
            )
            later = second_queue.fetch_record_in(
                connection,
                gateway_id=stack.scope.gateway_id,
                workspace_id=stack.workspace_id,
                actor=stack.scope.actor,
                operation_id=successor.client_operation_id,
            )
            candidate = second_queue.next_runnable(connection, stack.workspace_id)
        assert tuple(owner_before) == tuple(owner_after)
        assert running is not None and running.state == "running"
        assert later is not None and later.state == "queued"
        assert candidate is None
        assert second_executor.has_queue_owner is False

        release_delete.set()
        await stack.settle(deletion.client_operation_id, successor.client_operation_id)
        assert second_service.record(successor.client_operation_id, stack.scope).state == "committed"
    finally:
        release_delete.set()
        await stack.service.stop()
        await second_service.stop()
        second_store.close()


@pytest.mark.asyncio
async def test_enqueue_returns_durable_receipt_without_touching_catalog(
    navigation_workspace: Path,
) -> None:
    """202 只表示 durable acceptance：入队后目录事实必须保持不变。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    intent = stack.create_folder_intent(seed="accept_only", sequence=1, name="新目录")

    result = await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[intent]), stack.scope
    )

    assert result.accepted_count == 1
    receipt = result.receipts[0]
    assert receipt.operation_id == intent.client_operation_id
    assert receipt.state == "queued"
    assert receipt.queue_seq == 1
    # 预留了 canonical Folder ID，但目录里还没有这个节点。
    assert receipt.created_node_id is not None
    assert result.created_node_ids == {intent.client_operation_id: receipt.created_node_id}
    assert stack.resolver.list_nodes() == []


@pytest.mark.asyncio
async def test_same_key_same_preimage_is_idempotent_and_reuses_queue_seq(
    navigation_workspace: Path,
) -> None:
    """同 key 同 preimage 重试返回原 receipt：不重复分配 queue_seq。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    intent = stack.create_folder_intent(seed="idem", sequence=1, name="幂等目录")
    request = NavigationMutationEnqueueRequest(intents=[intent])

    first = await stack.enqueue(request, stack.scope)
    second = await stack.enqueue(request, stack.scope)

    assert first.receipts[0].queue_seq == second.receipts[0].queue_seq
    assert first.receipts[0].created_node_id == second.receipts[0].created_node_id


@pytest.mark.asyncio
async def test_same_key_different_preimage_conflicts(
    navigation_workspace: Path,
) -> None:
    """同 key 异 preimage 必须明确冲突，不得覆盖已接受命令。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    await stack.enqueue(
        NavigationMutationEnqueueRequest(
            intents=[
                stack.create_folder_intent(seed="conflict", sequence=1, name="原名")
            ]
        ),
        stack.scope,
    )

    with pytest.raises(NavigationMutationConflictError):
        await stack.enqueue(
            NavigationMutationEnqueueRequest(
                intents=[
                    stack.create_folder_intent(seed="conflict", sequence=1, name="新名")
                ]
            ),
            stack.scope,
        )


@pytest.mark.asyncio
async def test_batch_is_atomic_and_dependency_chain_resolves_parent(
    navigation_workspace: Path,
) -> None:
    """一批原子入队，跨 intent 依赖按 committed 结果解析父节点。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    parent_intent = stack.create_folder_intent(
        seed="chain_parent", sequence=1, name="父目录"
    )
    child_intent = stack.create_folder_intent(
        seed="chain_child",
        sequence=2,
        name="子目录",
        created_by_operation_id=parent_intent.client_operation_id,
    )
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[parent_intent, child_intent]),
        stack.scope,
    )

    await stack.settle(
        parent_intent.client_operation_id, child_intent.client_operation_id
    )

    parent_id = stack.node_id(parent_intent.client_operation_id)
    child_id = stack.node_id(child_intent.client_operation_id)
    assert stack.resolver.get_node(child_id).parent_node_id == parent_id


@pytest.mark.asyncio
async def test_terminal_tombstone_blocks_late_replay(
    navigation_workspace: Path,
) -> None:
    """terminal 后重放同 key 同 preimage 返回原 terminal，不重复应用。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    intent = stack.create_folder_intent(seed="tombstone", sequence=1, name="一次性")
    request = NavigationMutationEnqueueRequest(intents=[intent])
    await stack.enqueue(request, stack.scope)
    await stack.settle(intent.client_operation_id)
    committed = stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=intent.client_operation_id,
    )
    assert committed is not None and committed.state == "committed"

    replay = await stack.enqueue(request, stack.scope)
    await stack.service.drain_once()

    assert replay.receipts[0].state == "committed"
    assert len(stack.resolver.list_nodes()) == 1


@pytest.mark.asyncio
async def test_dependency_failed_successor_has_no_business_effect(
    navigation_workspace: Path,
) -> None:
    """前置失败的后继从 queued 直接进入 dependency_failed，零副作用。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    failing = NavigationMutationIntentDTO(
        client_operation_id=operation_id("fail_parent"),
        client_sequence=1,
        kind="rename_node",
        base_catalog_revision=0,
        expected_revision=1,
        target_node_id=canonical("不存在的节点"),
        name="改名",
    )
    successor = stack.create_folder_intent(
        seed="dep_child",
        sequence=2,
        name="后继目录",
        created_by_operation_id=failing.client_operation_id,
    )
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[failing, successor]), stack.scope
    )

    await stack.settle(failing.client_operation_id, successor.client_operation_id)

    parent = stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=failing.client_operation_id,
    )
    child = stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=successor.client_operation_id,
    )
    assert parent is not None and parent.state == "rejected"
    assert child is not None and child.state == "dependency_failed"
    assert stack.resolver.list_nodes() == []


@pytest.mark.asyncio
async def test_worker_restart_resumes_same_operation_without_duplication(
    navigation_workspace: Path,
) -> None:
    """取得 OS owner lock 的新 generation 才能恢复遗留 running 并继续原操作。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    intent = stack.create_folder_intent(seed="restart", sequence=1, name="恢复目录")
    with stack.store.write_transaction() as connection:
        stack.queue.enqueue_batch(
            connection,
            gateway_id=stack.scope.gateway_id,
            workspace_id=stack.workspace_id,
            actor=stack.scope.actor,
            intents=[intent],
            now=utc_now_text(),
        )
    # 模拟旧 owner 进程退出后保留的 durable running receipt。
    with stack.store.write_transaction() as connection:
        connection.execute(
            "UPDATE navigation_mutation_records SET state = 'running', "
            "holder_id = 'crashed:7', fencing_token = 7"
        )

    async with stack.executor.queue_owner():
        recovered = await stack.executor.recover_in_flight()
        assert recovered == 1
        await stack.executor.drain()
    # 再次排空必须幂等 no-op（terminal tombstone）。

    record = stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=intent.client_operation_id,
    )
    assert record is not None and record.state == "committed"
    assert len(stack.resolver.list_nodes()) == 1


@pytest.mark.asyncio
async def test_sequential_edits_on_same_node_do_not_false_conflict(
    navigation_workspace: Path,
) -> None:
    """同 node 连续编辑以前序结果 revision 为前置，不因自身推进而误判冲突。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    created = stack.create_folder_intent(seed="seq_base", sequence=1, name="初始名")
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[created]), stack.scope
    )
    await stack.settle(created.client_operation_id)
    node_id = stack.node_id(created.client_operation_id)

    first_rename = NavigationMutationIntentDTO(
        client_operation_id=operation_id("seq_rename_1"),
        client_sequence=1,
        kind="rename_node",
        base_catalog_revision=0,
        expected_revision=1,
        target_node_id=node_id,
        name="第一次改名",
    )
    second_rename = NavigationMutationIntentDTO(
        client_operation_id=operation_id("seq_rename_2"),
        client_sequence=2,
        kind="rename_node",
        # 客户端仍以为 revision 是 1（它不知道自己的第一条命令已推进），
        # 依赖链让它按前序结果 revision 执行。
        base_catalog_revision=0,
        expected_revision=1,
        target_node_id=node_id,
        name="第二次改名",
        depends_on=[first_rename.client_operation_id],
    )
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[first_rename, second_rename]),
        stack.scope,
    )

    await stack.settle(
        first_rename.client_operation_id, second_rename.client_operation_id
    )

    assert stack.resolver.get_node(node_id).name == "第二次改名"
    second_record = stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=second_rename.client_operation_id,
    )
    assert second_record is not None and second_record.state == "committed"


@pytest.mark.asyncio
async def test_other_client_modification_conflicts_explicitly(
    navigation_workspace: Path,
) -> None:
    """其它客户端已修改同 node 时，陈旧 expected_revision 的编辑明确拒绝。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    created = stack.create_folder_intent(seed="other_base", sequence=1, name="初始名")
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[created]), stack.scope
    )
    await stack.settle(created.client_operation_id)
    node_id = stack.node_id(created.client_operation_id)
    # 模拟另一个客户端在本客户端入队后提交了修改：revision 前进。
    stack.store.apply_navigation_mutation(
        node_id, expected_revision=1, new_display_name="别的客户端改名"
    )

    stale = NavigationMutationIntentDTO(
        client_operation_id=operation_id("stale_rename"),
        client_sequence=1,
        kind="rename_node",
        base_catalog_revision=0,
        expected_revision=1,
        target_node_id=node_id,
        name="我的改名",
    )
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[stale]), stack.scope
    )

    await stack.settle(stale.client_operation_id)

    record = stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=stale.client_operation_id,
    )
    assert record is not None and record.state == "rejected"
    assert record.error_code == "conflict"
    # 不覆盖其它客户端已提交的修改。
    assert stack.resolver.get_node(node_id).name == "别的客户端改名"


@pytest.mark.asyncio
async def test_events_cursor_is_incremental_and_duplicate_free(
    navigation_workspace: Path,
) -> None:
    """事件按单调 event_seq 增量推送：cursor 不丢不重。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    intents = [
        stack.create_folder_intent(seed=f"evt_{index}", sequence=index + 1, name=f"目录{index}")
        for index in range(3)
    ]
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=intents), stack.scope
    )
    # 等全部 operation 到达终态（后台 worker 可能并发认领，必须按 ID 等终态）。
    for intent in intents:
        await stack.service.await_terminal(intent.client_operation_id, stack.scope)

    first = stack.service.events(after=0, limit=2)
    assert [event.event_seq for event in first.items] == [1, 2]
    assert first.has_more is True
    assert first.next_cursor is not None
    assert first.event_seq_watermark == 3

    after, watermark = stack.service.decode_events_cursor(first.next_cursor)
    assert after == 2
    assert watermark == 3
    second = stack.service.events(after=after, limit=2)
    assert [event.event_seq for event in second.items] == [3]
    assert second.has_more is False
    assert second.next_cursor is None
    # 两页拼起来恰好覆盖全部事件，无重复、无遗漏。
    assert [event.event_seq for event in first.items + second.items] == [1, 2, 3]
    assert len({event.operation_id for event in first.items + second.items}) == 3


@pytest.mark.asyncio
async def test_snapshot_reports_same_revision_as_committed_receipt(
    navigation_workspace: Path,
) -> None:
    """snapshot 的 revision 与事件水位来自同一只读快照，且与已提交 receipt 一致。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    intent = stack.create_folder_intent(seed="snap", sequence=1, name="快照目录")
    result = await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[intent]), stack.scope
    )
    del result
    await stack.settle(intent.client_operation_id)

    record = stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=intent.client_operation_id,
    )
    snapshot = stack.service.snapshot()
    assert record is not None
    assert record.committed_catalog_revision == snapshot.catalog_revision
    assert snapshot.event_seq_watermark == 1


@pytest.mark.asyncio
async def test_status_query_reports_unknown_ids_explicitly(
    navigation_workspace: Path,
) -> None:
    """未知 operation ID 显式列出（不是失败），客户端据此保留 pending 重试。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    intent = stack.create_folder_intent(seed="status", sequence=1, name="状态目录")
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[intent]), stack.scope
    )

    page = stack.service.status(
        [intent.client_operation_id, operation_id("never_seen")], stack.scope
    )

    assert [item.operation_id for item in page.items] == [intent.client_operation_id]
    assert page.unknown_operation_ids == [operation_id("never_seen")]


@pytest.mark.asyncio
async def test_sync_facade_uses_same_single_write_path(
    navigation_workspace: Path,
) -> None:
    """同步目录 API 只是同一写路径的 façade：产生相同的 durable operation 记录。"""
    sessions_root = navigation_workspace / ".boxteam" / "sessions"
    session_service = _SessionService(sessions_root)
    catalog = SessionCatalogService(session_service=session_service)
    from app.schemas.internal_v2.session_navigation import SessionFolderCreateRequest

    await catalog.operations.start()
    try:
        breadcrumb = await catalog.create_folder(
            SessionFolderCreateRequest(name="同步目录")
        )
    finally:
        await catalog.operations.stop()

    assert [item.name for item in breadcrumb.items] == ["同步目录"]
    created_id = breadcrumb.items[-1].node_id
    with catalog.operations._store.read_transaction() as connection:
        rows = connection.execute(
            "SELECT operation_id, kind, state FROM navigation_mutation_records"
        ).fetchall()
    assert [(row["kind"], row["state"]) for row in rows] == [("create_folder", "committed")]
    assert catalog.operations.node_kind(created_id) == "folder"


@pytest.mark.asyncio
async def test_nonrecursive_folder_delete_uses_queue_and_preserves_nonempty_tree(
    navigation_workspace: Path,
) -> None:
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    catalog = SessionCatalogService(
        session_service=stack.session_service,
        operations_service=stack.service,
    )
    parent_intent = stack.create_folder_intent(
        seed="nonrecursive_parent", sequence=1, name="父目录"
    )
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[parent_intent]), stack.scope
    )
    await stack.settle(parent_intent.client_operation_id)
    parent_id = stack.node_id(parent_intent.client_operation_id)
    child_intent = stack.create_folder_intent(
        seed="nonrecursive_child",
        sequence=1,
        name="子目录",
        parent_node_id=parent_id,
    )
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[child_intent]), stack.scope
    )
    await stack.settle(child_intent.client_operation_id)
    child_id = stack.node_id(child_intent.client_operation_id)

    with pytest.raises(ValueError, match="非空 folder 的非递归删除被明确拒绝"):
        await catalog.delete_folder(parent_id, recursive=False)

    parent = stack.resolver.get_node(parent_id)
    child = stack.resolver.get_node(child_id)
    with stack.store.read_transaction() as connection:
        delete_rows = connection.execute(
            "SELECT state, error_code, params_json FROM navigation_mutation_records "
            "WHERE kind = 'delete_folder' ORDER BY queue_seq"
        ).fetchall()
    assert parent.node_id == parent_id
    assert child.parent_node_id == parent_id
    assert len(delete_rows) == 1
    assert delete_rows[0]["state"] == "rejected"
    assert delete_rows[0]["error_code"] == "invalid_operation"

    empty_intent = stack.create_folder_intent(
        seed="nonrecursive_empty", sequence=1, name="空目录"
    )
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[empty_intent]), stack.scope
    )
    await stack.settle(empty_intent.client_operation_id)
    empty_id = stack.node_id(empty_intent.client_operation_id)
    await catalog.delete_folder(empty_id, recursive=False)
    with pytest.raises(KeyError):
        stack.resolver.get_node(empty_id)
    with stack.store.read_transaction() as connection:
        terminal = connection.execute(
            "SELECT state, params_json FROM navigation_mutation_records "
            "WHERE kind = 'delete_folder' ORDER BY queue_seq DESC LIMIT 1"
        ).fetchone()
    assert terminal["state"] == "committed"
    assert '"recursive":false' in terminal["params_json"]


@pytest.mark.asyncio
async def test_stale_base_catalog_revision_does_not_global_cas(
    navigation_workspace: Path,
) -> None:
    """``base_catalog_revision`` 只供快照/事件对账，绝不充当全局 CAS。

    钉死 spec.md 的「不得对无关 node 变更做全局 CAS」：客户端带着明显陈旧的
    base revision（此处恒为 0）提交一次无关 node 上的新建，仍必须 committed。
    若有人把它实现成全局 revision CAS，本用例立刻变红。
    """
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    # 先推进 catalog revision 若干次，制造「客户端 base revision 明显陈旧」的局面。
    for index in range(3):
        seeded = stack.create_folder_intent(
            seed=f"base_seed_{index}", sequence=1, name=f"基线目录{index}"
        )
        await stack.enqueue(
            NavigationMutationEnqueueRequest(intents=[seeded]), stack.scope
        )
        await stack.settle(seeded.client_operation_id)
    current = stack.resolver.revision
    assert current > 1

    stale = NavigationMutationIntentDTO(
        client_operation_id=operation_id("stale_base"),
        client_sequence=1,
        kind="create_folder",
        base_catalog_revision=0,
        name="陈旧基线目录",
    )
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[stale]), stack.scope
    )
    await stack.settle(stale.client_operation_id)

    record = stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=stale.client_operation_id,
    )
    assert record is not None and record.state == "committed"
    assert record.error_code is None
    assert stack.service.node_kind(stack.node_id(stale.client_operation_id)) == "folder"


@pytest.mark.asyncio
async def test_intent_carries_base_catalog_revision_for_snapshot_reconciliation(
    navigation_workspace: Path,
) -> None:
    """``base_catalog_revision`` 被持久化保留（供对账），但不参与 preimage。

    两个可选收敛方向各自会失败在读哪一端：删掉该字段会让本用例读不到持久值；
    把它纳入 preimage 会让「同 key 仅 base revision 变化的重试」误判为冲突。
    """
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    intent = stack.create_folder_intent(seed="carry_base", sequence=1, name="留存目录")
    base_revision = intent.base_catalog_revision
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[intent]), stack.scope
    )

    persisted = stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=intent.client_operation_id,
    )
    assert persisted is not None
    assert persisted.params["base_catalog_revision"] == base_revision

    # 同 key、仅 base revision 变化（重试时客户端可能换用最新快照修订）：
    # 意图未变，必须幂等复用原 record，而不是报 preimage 冲突。
    retried = intent.model_copy(update={"base_catalog_revision": base_revision + 100})
    replay = await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[retried]), stack.scope
    )
    assert replay.receipts[0].operation_id == intent.client_operation_id
    assert replay.receipts[0].queue_seq == persisted.queue_seq


@pytest.mark.asyncio
async def test_recursive_delete_reports_logical_commit_with_pending_settlement(
    navigation_workspace: Path,
) -> None:
    """递归删除：导航逻辑 committed 与物理排空分开上报（8.1-G 删除链路契约）。

    删除流自身的 mark 事务就是导航逻辑的 committed 点；物理排空是另一条链路，
    因此 terminal 只能标 ``pending_settlement``，绝不假报「全部完成」。本用例钉死
    这一区分：若有人把删除接进普通 node mutation 的单事务路径，或直接丢掉
    ``pending_settlement``，这里立刻变红。
    """
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    created = stack.create_folder_intent(seed="del_root", sequence=1, name="待删目录")
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[created]), stack.scope
    )
    await stack.settle(created.client_operation_id)
    folder_id = stack.node_id(created.client_operation_id)

    deletion = NavigationMutationIntentDTO(
        client_operation_id=operation_id("del_folder"),
        client_sequence=1,
        kind="delete_folder",
        base_catalog_revision=0,
        target_node_id=folder_id,
        recursive=True,
    )
    await stack.enqueue(
        NavigationMutationEnqueueRequest(intents=[deletion]), stack.scope
    )
    record = await stack.service.await_terminal(
        deletion.client_operation_id, stack.scope
    )

    assert record.state == "committed"
    # 纯 Folder 子树没有需要物理排空的 Session，但仍必须显式携带该字段（默认
    # False），而不是靠 None 或缺省糊过去。
    assert record.pending_settlement is False
    # 删除链路复用的是共享子树删除流：其 mark 事务关闭逻辑可见性、最终 tombstone
    # 事务移除节点行。因此删除 committed 后，该节点对正常读者不再存在。
    with pytest.raises(KeyError):
        stack.resolver.get_node(folder_id)
    assert folder_id not in {node.node_id for node in stack.resolver.list_nodes()}
    # 删除的 terminal 同样进了导航事件 outbox（与普通 mutation 同一观测面）。
    events = stack.service.events(after=0, limit=50)
    assert [
        event.operation_id for event in events.items
    ] == [created.client_operation_id, deletion.client_operation_id]
    assert events.items[-1].result_state == "committed"


@pytest.mark.asyncio
async def test_delete_return_with_running_queue_fails_instead_of_committing(
    navigation_workspace: Path,
) -> None:
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    created = stack.create_folder_intent(
        seed="delete_unhooked_root",
        sequence=1,
        name="未接 hook 的目录",
    )
    _enqueue_without_worker(stack, [created])
    async with stack.executor.queue_owner():
        await stack.executor.drain()
    folder_id = stack.node_id(created.client_operation_id)

    deletion = NavigationMutationIntentDTO(
        client_operation_id=operation_id("delete_unhooked_runner"),
        client_sequence=1,
        kind="delete_folder",
        base_catalog_revision=stack.resolver.revision,
        target_node_id=folder_id,
        recursive=True,
    )
    _enqueue_without_worker(stack, [deletion])

    async def return_without_hooks(
        operation_id_value: str,
        root_node_id: str,
        mark_transaction_hook: CatalogTransactionHook,
        finish_transaction_hook: CatalogTransactionHook,
    ) -> SubtreeDeleteResult:
        del operation_id_value, mark_transaction_hook, finish_transaction_hook
        return SubtreeDeleteResult(
            root_node_id=root_node_id,
            frozen_node_ids=(root_node_id,),
            drained_session_ids=(),
            record_state="completed",
        )

    stack.executor._delete_runner = return_without_hooks
    async with stack.executor.queue_owner():
        with pytest.raises(RuntimeError, match="导航 operation 状态不一致.*running"):
            await stack.executor.drain()

    record = stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=deletion.client_operation_id,
    )
    assert record is not None and record.state == "running"


@pytest.mark.asyncio
async def test_delete_finish_hook_rejects_running_operation_with_completed_catalog_record(
    navigation_workspace: Path,
) -> None:
    """catalog 先由另一条 key 写入完成时，不把 running queue 伪装成成功。"""
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    created = stack.create_folder_intent(
        seed="delete_unhooked_journal_root",
        sequence=1,
        name="已有完成 journal 的目录",
    )
    _enqueue_without_worker(stack, [created])
    async with stack.executor.queue_owner():
        await stack.executor.drain()
    folder_id = stack.node_id(created.client_operation_id)

    deletion = NavigationMutationIntentDTO(
        client_operation_id=operation_id("delete_unhooked_journal"),
        client_sequence=1,
        kind="delete_folder",
        base_catalog_revision=stack.resolver.revision,
        target_node_id=folder_id,
        recursive=True,
    )
    await stack.resolver.delete_subtree(
        idempotency_key=deletion.client_operation_id,
        root_node_id=folder_id,
    )
    assert (
        stack.store.get_subtree_delete_record(deletion.client_operation_id).state
        == "completed"
    )
    _enqueue_without_worker(stack, [deletion])

    async with stack.executor.queue_owner():
        outcomes = await stack.executor.drain()

    assert len(outcomes) == 1
    assert outcomes[0].record.state == "rejected"
    assert "未由 mark 事务同步提交为 committed" in (
        outcomes[0].record.error_detail or ""
    )


@pytest.mark.asyncio
async def test_operations_worker_lifecycle_is_explicit_and_cancellable(
    navigation_workspace: Path,
) -> None:
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    intent = stack.create_folder_intent(seed="lifecycle", sequence=1, name="生命周期")

    with pytest.raises(NavigationQueueOwnerError, match="尚未取得 owner"):
        await stack.service.enqueue(
            NavigationMutationEnqueueRequest(intents=[intent]), stack.scope
        )
    assert stack.service._worker_task is None

    async with stack.executor.queue_owner():
        with pytest.raises(NavigationQueueOwnerError, match="启动恢复超时"):
            await stack.service.start(owner_timeout_seconds=0.05)
        assert stack.service.worker_state == "failed"
        assert stack.service._worker_task is None

        cancelled_stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
        starting = asyncio.create_task(
            cancelled_stack.service.start(owner_timeout_seconds=5)
        )
        deadline = asyncio.get_running_loop().time() + 1
        while cancelled_stack.service.worker_state != "waiting_for_owner":
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("worker 未进入 owner lock 等待状态")
            await asyncio.sleep(0.01)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert cancelled_stack.service.worker_state == "stopped"
        assert cancelled_stack.service._worker_task is None

    ready_stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    await ready_stack.service.start()
    assert ready_stack.service.worker_state == "running"
    await ready_stack.service.stop()
    assert ready_stack.service.worker_state == "stopped"
    async with stack.executor.queue_owner():
        assert stack.executor.has_queue_owner is True


@pytest.mark.asyncio
async def test_worker_failure_is_visible_to_status_snapshot_and_events(
    navigation_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    fail_worker = asyncio.Event()

    async def fail_after_ready() -> list:
        await fail_worker.wait()
        raise RuntimeError("测试注入的 worker 故障")

    monkeypatch.setattr(stack.service, "drain_once", fail_after_ready)
    await stack.service.start()
    assert stack.service.worker_state == "running"
    fail_worker.set()

    deadline = asyncio.get_running_loop().time() + 2
    while stack.service.worker_state != "failed":
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("worker 故障未在有限时间内暴露")
        await asyncio.sleep(0.01)

    with pytest.raises(NavigationQueueOwnerError, match="测试注入的 worker 故障"):
        stack.service.status([operation_id("worker_failed_status")], stack.scope)
    with pytest.raises(NavigationQueueOwnerError, match="测试注入的 worker 故障"):
        stack.service.snapshot()
    with pytest.raises(NavigationQueueOwnerError, match="测试注入的 worker 故障"):
        stack.service.events(after=0)
    await stack.service.stop()


@pytest.mark.asyncio
async def test_lifespan_owner_recovers_a_persisted_running_operation(
    navigation_workspace: Path,
) -> None:
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    intent = stack.create_folder_intent(seed="lifespan_recovery", sequence=1, name="恢复")
    _enqueue_without_worker(stack, [intent])
    with stack.store.write_transaction() as connection:
        connection.execute(
            "UPDATE navigation_mutation_records SET state = 'running', "
            "holder_id = 'crashed:9', fencing_token = 9 WHERE operation_id = ?",
            (intent.client_operation_id,),
        )

    await stack.service.start()
    try:
        record = await stack.service.await_terminal(
            intent.client_operation_id, stack.scope
        )
        assert record.state == "committed"
        assert len(stack.resolver.list_nodes()) == 1
    finally:
        await stack.service.stop()


@pytest.mark.asyncio
async def test_delete_mark_commits_before_physical_drain_and_recovers_settlement(
    navigation_workspace: Path,
) -> None:
    stack = _Stack(navigation_workspace / ".boxteam" / "sessions")
    created = stack.create_folder_intent(
        seed="delete_recovery_root", sequence=1, name="待删"
    )
    await stack.enqueue(NavigationMutationEnqueueRequest(intents=[created]), stack.scope)
    await stack.settle(created.client_operation_id)
    folder_id = stack.node_id(created.client_operation_id)
    delete = NavigationMutationIntentDTO(
        client_operation_id=operation_id("delete_mark_recovery"),
        client_sequence=1,
        kind="delete_folder",
        base_catalog_revision=stack.resolver.revision,
        target_node_id=folder_id,
        recursive=True,
    )
    delete_service = stack.resolver._delete_service
    original_drain = delete_service._drain

    async def fail_after_mark(
        record: SubtreeDeleteRecord,
        idempotency_key: str,
    ) -> None:
        raise RuntimeError(f"测试注入：mark 后 drain 失败: {idempotency_key}")

    delete_service._drain = fail_after_mark
    try:
        await stack.enqueue(NavigationMutationEnqueueRequest(intents=[delete]), stack.scope)
        receipt = await stack.service.await_terminal(
            delete.client_operation_id, stack.scope
        )
        assert receipt.state == "committed"
        assert receipt.pending_settlement is True
        assert stack.store.get_subtree_delete_record(delete.client_operation_id).state == "deleting"
        with stack.store.read_transaction() as connection:
            node = connection.execute(
                "SELECT state FROM nodes WHERE node_id = ?", (folder_id,)
            ).fetchone()
        assert node is not None and node["state"] == "deleting"
        event = stack.service.events(after=0).items[-1]
        assert event.operation_id == delete.client_operation_id
        assert event.result_state == "committed"

        delete_service._drain = original_drain
        await stack.service.stop()
        await stack.service.start()
        deadline = asyncio.get_running_loop().time() + 5
        while stack.service.record(delete.client_operation_id, stack.scope).pending_settlement:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("子树删除 settlement 未在有限时间内恢复")
            await asyncio.sleep(0.02)
        settled = stack.service.record(delete.client_operation_id, stack.scope)
        assert settled.state == "committed"
        assert settled.pending_settlement is False
        assert stack.store.get_subtree_delete_record(delete.client_operation_id).state == "completed"
    finally:
        delete_service._drain = original_drain
        await stack.service.stop()
