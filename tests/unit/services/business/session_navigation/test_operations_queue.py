"""会话目录异步 mutation 协议的单测（OpenSpec 8.1-G/8.1-H）。

覆盖批内/批间幂等与冲突、terminal tombstone 防迟到重放、依赖失败后继终结、
worker 崩溃接管、同 node 串行编辑不误判冲突、其它客户端修改明确拒绝、
事件 cursor 边界，以及同步 façade 走同一写路径。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
    NavigationMutationIntentDTO,
)
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_navigation.executor import NavigationMutationExecutor
from app.services.business.session_navigation.operations_service import (
    SessionCatalogOperationsService,
    local_navigation_scope,
)
from app.services.business.session_navigation.queue_store import (
    NavigationMutationConflictError,
    NavigationMutationQueueStore,
)


def canonical(name: str) -> str:
    """确定性 canonical session ID（仅测试播种用）。"""
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    return f"ses_{digest[:12]}4{digest[13:16]}8{digest[17:]}"


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
            base_catalog_revision=self.service.snapshot().catalog_revision,
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
        records = [
            await self.service.await_terminal(operation_id, self.scope)
            for operation_id in operation_ids
        ]
        return records


@pytest.mark.asyncio
async def test_enqueue_returns_durable_receipt_without_touching_catalog(
    tmp_path: Path,
) -> None:
    """202 只表示 durable acceptance：入队后目录事实必须保持不变。"""
    stack = _Stack(tmp_path / "sessions")
    intent = stack.create_folder_intent(seed="accept_only", sequence=1, name="新目录")

    result = await stack.service.enqueue(
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
    tmp_path: Path,
) -> None:
    """同 key 同 preimage 重试返回原 receipt：不重复分配 queue_seq。"""
    stack = _Stack(tmp_path / "sessions")
    intent = stack.create_folder_intent(seed="idem", sequence=1, name="幂等目录")
    request = NavigationMutationEnqueueRequest(intents=[intent])

    first = await stack.service.enqueue(request, stack.scope)
    second = await stack.service.enqueue(request, stack.scope)

    assert first.receipts[0].queue_seq == second.receipts[0].queue_seq
    assert first.receipts[0].created_node_id == second.receipts[0].created_node_id


@pytest.mark.asyncio
async def test_same_key_different_preimage_conflicts(tmp_path: Path) -> None:
    """同 key 异 preimage 必须明确冲突，不得覆盖已接受命令。"""
    stack = _Stack(tmp_path / "sessions")
    await stack.service.enqueue(
        NavigationMutationEnqueueRequest(
            intents=[
                stack.create_folder_intent(seed="conflict", sequence=1, name="原名")
            ]
        ),
        stack.scope,
    )

    with pytest.raises(NavigationMutationConflictError):
        await stack.service.enqueue(
            NavigationMutationEnqueueRequest(
                intents=[
                    stack.create_folder_intent(seed="conflict", sequence=1, name="新名")
                ]
            ),
            stack.scope,
        )


@pytest.mark.asyncio
async def test_batch_is_atomic_and_dependency_chain_resolves_parent(
    tmp_path: Path,
) -> None:
    """一批原子入队，跨 intent 依赖按 committed 结果解析父节点。"""
    stack = _Stack(tmp_path / "sessions")
    parent_intent = stack.create_folder_intent(
        seed="chain_parent", sequence=1, name="父目录"
    )
    child_intent = stack.create_folder_intent(
        seed="chain_child",
        sequence=2,
        name="子目录",
        created_by_operation_id=parent_intent.client_operation_id,
    )
    await stack.service.enqueue(
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
async def test_terminal_tombstone_blocks_late_replay(tmp_path: Path) -> None:
    """terminal 后重放同 key 同 preimage 返回原 terminal，不重复应用。"""
    stack = _Stack(tmp_path / "sessions")
    intent = stack.create_folder_intent(seed="tombstone", sequence=1, name="一次性")
    request = NavigationMutationEnqueueRequest(intents=[intent])
    await stack.service.enqueue(request, stack.scope)
    await stack.settle(intent.client_operation_id)
    committed = stack.queue.get_record(
        gateway_id=stack.scope.gateway_id,
        workspace_id=stack.workspace_id,
        actor=stack.scope.actor,
        operation_id=intent.client_operation_id,
    )
    assert committed is not None and committed.state == "committed"

    replay = await stack.service.enqueue(request, stack.scope)
    await stack.service.drain_once()

    assert replay.receipts[0].state == "committed"
    assert len(stack.resolver.list_nodes()) == 1


@pytest.mark.asyncio
async def test_dependency_failed_successor_has_no_business_effect(
    tmp_path: Path,
) -> None:
    """前置失败的后继从 queued 直接进入 dependency_failed，零副作用。"""
    stack = _Stack(tmp_path / "sessions")
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
    await stack.service.enqueue(
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
    tmp_path: Path,
) -> None:
    """worker 崩溃（遗留 running）后新 owner 继续原 operation，不重复应用。"""
    stack = _Stack(tmp_path / "sessions")
    intent = stack.create_folder_intent(seed="restart", sequence=1, name="恢复目录")
    await stack.service.enqueue(
        NavigationMutationEnqueueRequest(intents=[intent]), stack.scope
    )
    # 模拟「202 已返回但 worker 在执行前退出」：直接标记为 running。
    with stack.store.write_transaction() as connection:
        connection.execute(
            "UPDATE navigation_mutation_records SET state = 'running', "
            "holder_id = 'crashed', fencing_token = 7"
        )

    recovered = await stack.service.recover_after_restart()
    assert recovered == 1
    await stack.settle(intent.client_operation_id)
    # 再次排空必须幂等 no-op（terminal tombstone）。
    await stack.service.drain_once()

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
    tmp_path: Path,
) -> None:
    """同 node 连续编辑以前序结果 revision 为前置，不因自身推进而误判冲突。"""
    stack = _Stack(tmp_path / "sessions")
    created = stack.create_folder_intent(seed="seq_base", sequence=1, name="初始名")
    await stack.service.enqueue(
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
    await stack.service.enqueue(
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
    tmp_path: Path,
) -> None:
    """其它客户端已修改同 node 时，陈旧 expected_revision 的编辑明确拒绝。"""
    stack = _Stack(tmp_path / "sessions")
    created = stack.create_folder_intent(seed="other_base", sequence=1, name="初始名")
    await stack.service.enqueue(
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
    await stack.service.enqueue(
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
async def test_events_cursor_is_incremental_and_duplicate_free(tmp_path: Path) -> None:
    """事件按单调 event_seq 增量推送：cursor 不丢不重。"""
    stack = _Stack(tmp_path / "sessions")
    intents = [
        stack.create_folder_intent(seed=f"evt_{index}", sequence=index + 1, name=f"目录{index}")
        for index in range(3)
    ]
    await stack.service.enqueue(
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
    tmp_path: Path,
) -> None:
    """snapshot 的 revision 与事件水位来自同一只读快照，且与已提交 receipt 一致。"""
    stack = _Stack(tmp_path / "sessions")
    intent = stack.create_folder_intent(seed="snap", sequence=1, name="快照目录")
    result = await stack.service.enqueue(
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
async def test_status_query_reports_unknown_ids_explicitly(tmp_path: Path) -> None:
    """未知 operation ID 显式列出（不是失败），客户端据此保留 pending 重试。"""
    stack = _Stack(tmp_path / "sessions")
    intent = stack.create_folder_intent(seed="status", sequence=1, name="状态目录")
    await stack.service.enqueue(
        NavigationMutationEnqueueRequest(intents=[intent]), stack.scope
    )

    page = stack.service.status(
        [intent.client_operation_id, operation_id("never_seen")], stack.scope
    )

    assert [item.operation_id for item in page.items] == [intent.client_operation_id]
    assert page.unknown_operation_ids == [operation_id("never_seen")]


@pytest.mark.asyncio
async def test_sync_facade_uses_same_single_write_path(tmp_path: Path) -> None:
    """同步目录 API 只是同一写路径的 façade：产生相同的 durable operation 记录。"""
    sessions_root = tmp_path / "sessions"
    session_service = _SessionService(sessions_root)
    catalog = SessionCatalogService(session_service=session_service)
    from app.schemas.internal_v2.session_navigation import SessionFolderCreateRequest

    breadcrumb = await catalog.create_folder(SessionFolderCreateRequest(name="同步目录"))

    assert [item.name for item in breadcrumb.items] == ["同步目录"]
    created_id = breadcrumb.items[-1].node_id
    with catalog.operations._store.read_transaction() as connection:
        rows = connection.execute(
            "SELECT operation_id, kind, state FROM navigation_mutation_records"
        ).fetchall()
    assert [(row["kind"], row["state"]) for row in rows] == [("create_folder", "committed")]
    assert catalog.operations.node_kind(created_id) == "folder"


@pytest.mark.asyncio
async def test_stale_base_catalog_revision_does_not_global_cas(tmp_path: Path) -> None:
    """``base_catalog_revision`` 只供快照/事件对账，绝不充当全局 CAS。

    钉死 spec.md 的「不得对无关 node 变更做全局 CAS」：客户端带着明显陈旧的
    base revision（此处恒为 0）提交一次无关 node 上的新建，仍必须 committed。
    若有人把它实现成全局 revision CAS，本用例立刻变红。
    """
    stack = _Stack(tmp_path / "sessions")
    # 先推进 catalog revision 若干次，制造「客户端 base revision 明显陈旧」的局面。
    for index in range(3):
        seeded = stack.create_folder_intent(
            seed=f"base_seed_{index}", sequence=1, name=f"基线目录{index}"
        )
        await stack.service.enqueue(
            NavigationMutationEnqueueRequest(intents=[seeded]), stack.scope
        )
        await stack.settle(seeded.client_operation_id)
    current = stack.service.snapshot().catalog_revision
    assert current > 1

    stale = NavigationMutationIntentDTO(
        client_operation_id=operation_id("stale_base"),
        client_sequence=1,
        kind="create_folder",
        base_catalog_revision=0,
        name="陈旧基线目录",
    )
    await stack.service.enqueue(
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
    tmp_path: Path,
) -> None:
    """``base_catalog_revision`` 被持久化保留（供对账），但不参与 preimage。

    两个可选收敛方向各自会失败在读哪一端：删掉该字段会让本用例读不到持久值；
    把它纳入 preimage 会让「同 key 仅 base revision 变化的重试」误判为冲突。
    """
    stack = _Stack(tmp_path / "sessions")
    intent = stack.create_folder_intent(seed="carry_base", sequence=1, name="留存目录")
    base_revision = intent.base_catalog_revision
    await stack.service.enqueue(
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
    replay = await stack.service.enqueue(
        NavigationMutationEnqueueRequest(intents=[retried]), stack.scope
    )
    assert replay.receipts[0].operation_id == intent.client_operation_id
    assert replay.receipts[0].queue_seq == persisted.queue_seq


@pytest.mark.asyncio
async def test_recursive_delete_reports_logical_commit_with_pending_settlement(
    tmp_path: Path,
) -> None:
    """递归删除：导航逻辑 committed 与物理排空分开上报（8.1-G 删除链路契约）。

    删除流自身的 mark 事务就是导航逻辑的 committed 点；物理排空是另一条链路，
    因此 terminal 只能标 ``pending_settlement``，绝不假报「全部完成」。本用例钉死
    这一区分：若有人把删除接进普通 node mutation 的单事务路径，或直接丢掉
    ``pending_settlement``，这里立刻变红。
    """
    stack = _Stack(tmp_path / "sessions")
    created = stack.create_folder_intent(seed="del_root", sequence=1, name="待删目录")
    await stack.service.enqueue(
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
    await stack.service.enqueue(
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
