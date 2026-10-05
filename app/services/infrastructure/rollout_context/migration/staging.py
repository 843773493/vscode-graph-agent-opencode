"""一次性 importer 的私有 rollout 定位与现行 v2 验收。"""

from __future__ import annotations

from pathlib import Path

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage


class ImportStagingStorage(RolloutStorage):
    """只把已绑定 SessionThread 的 I/O 定向到 importer 私有 staging。"""

    def __init__(
        self,
        owner: RolloutStorage,
        *,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        root: Path,
    ) -> None:
        super().__init__(
            owner.sessions_dir, serde=owner._serde, message_codec=owner._message_codec
        )
        self._session_id = session_id
        self._thread_id = thread_id
        self._checkpoint_ns = checkpoint_ns
        self._staging_root = root

    def root(
        self,
        session_id: str,
        checkpoint_ns: str = "",
        *,
        thread_id: str | None = None,
    ) -> Path:
        owner_thread_id = self._resolve_thread_identity(session_id, thread_id)
        if (session_id, owner_thread_id, checkpoint_ns) != (
            self._session_id,
            self._thread_id,
            self._checkpoint_ns,
        ):
            raise ValueError(
                "migration staging 只能访问已绑定的 SessionThread/namespace"
            )
        return self._staging_root


def validate_staging(
    storage: RolloutStorage,
    session_id: str,
    thread_id: str,
    checkpoint_ns: str,
) -> None:
    """由真实 v2 初始化和 reader 校验提交链、正文、owner 与关系闭合。"""
    storage.initialize(session_id, checkpoint_ns, thread_id=thread_id)
    by_id = {
        item.item_id: item
        for item in storage.read_items(
            session_id, thread_id, checkpoint_ns=checkpoint_ns
        )
    }
    calls: dict[str, CanonicalItemRecord] = {}
    results: set[str] = set()
    for item in by_id.values():
        if item.semantic_kind == "tool_call":
            call_id = item.payload["tool_call_id"]
            if call_id in calls:
                raise RuntimeError("migration staging tool call identity 重复")
            calls[call_id] = item
        elif item.semantic_kind == "tool_result":
            call_id = item.payload["tool_call_id"]
            call = calls.get(call_id)
            if call is None or call_id in results or call.turn_id != item.turn_id:
                raise RuntimeError("migration staging tool result lineage 不闭合")
            for field in ("tool_invocation_id", "tool_attempt_id"):
                if call.payload[field] != item.payload[field]:
                    raise RuntimeError(f"migration staging {field} 不一致")
            results.add(call_id)
    with storage._connect(
        thread_id, checkpoint_ns, session_id=session_id
    ) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("migration staging SQLite integrity_check 失败")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("migration staging SQLite foreign_key_check 失败")
        owner = connection.execute(
            "SELECT session_id, thread_id FROM database_meta WHERE singleton_id=1"
        ).fetchone()
        if owner != (session_id, thread_id):
            raise RuntimeError("migration staging database owner 不闭合")
        missing_owner = connection.execute(
            "SELECT i.item_id FROM item_catalog i LEFT JOIN executions e "
            "ON e.execution_id=i.execution_id AND e.session_id=? AND e.thread_id=? "
            "WHERE e.execution_id IS NULL LIMIT 1",
            (session_id, thread_id),
        ).fetchone()
        if missing_owner is not None:
            raise RuntimeError("migration staging item execution owner 不闭合")
        for turn, root, final, status in connection.execute(
            "SELECT turn_id, root_input_item_id, final_item_id, status FROM turn_records"
        ):
            item = by_id.get(root)
            if (
                item is None
                or item.turn_id != turn
                or item.semantic_kind != "user_input"
            ):
                raise RuntimeError(f"migration staging Turn root 不闭合: {turn}")
            if status in {"active", "open"}:
                raise RuntimeError(f"migration staging 不得发布运行态 Turn: {turn}")
            if (status == "completed") != (final is not None):
                raise RuntimeError(f"migration staging final/status 不闭合: {turn}")
            if final is not None:
                item = by_id.get(final)
                if (
                    item is None
                    or item.turn_id != turn
                    or item.semantic_kind != "assistant_output"
                    or item.status != "completed"
                ):
                    raise RuntimeError(f"migration staging final item 不闭合: {turn}")
        for (item_id,) in connection.execute("SELECT item_id FROM context_view_items"):
            if item_id not in by_id:
                raise RuntimeError(f"migration staging view item 不存在: {item_id}")
        active = connection.execute(
            "SELECT b.head_view_id FROM branches b JOIN checkpoint_namespace_state n "
            "ON b.branch_id=n.active_branch_id WHERE n.checkpoint_ns=?",
            (checkpoint_ns,),
        ).fetchone()
        if active is None or active[0] is None:
            raise RuntimeError("migration staging 缺少 target active view")
        visible = {
            row[0]
            for row in connection.execute(
                "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1",
                (active[0],),
            )
        }
        expected = {item.item_id for item in by_id.values() if item.turn_id is not None}
        if visible != expected:
            raise RuntimeError("migration staging active view membership 不闭合")
        for turn, root in connection.execute(
            "SELECT turn_id, root_input_item_id FROM context_view_turns WHERE view_id=?",
            (active[0],),
        ):
            if (
                root not in by_id
                or by_id[root].turn_id != turn
                or by_id[root].turn_scope != "turn_root"
            ):
                raise RuntimeError("migration staging view Turn root 不闭合")
        if connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone()[0]:
            raise RuntimeError("migration 不得伪造无法恢复的 legacy assembly")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            raise RuntimeError("migration staging SQLite WAL checkpoint 未完成")
