"""Fork 私有冻结来源与未发布目标的唯一路径作用域。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path

from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.core.session_catalog_store import (
    SessionCreationRecord,
    validate_session_id,
    validate_thread_id,
)
from app.core.session_control_store import SessionControlStore, ThreadCreationRecord


@dataclass(frozen=True, slots=True)
class PreparedForkTarget:
    session_id: str
    thread_id: str
    session_node: Path
    thread_node: Path
    creation_record: SessionCreationRecord | None


@dataclass(frozen=True, slots=True)
class ForkSourceSnapshot:
    session_id: str
    thread_id: str
    captured_root: Path


class ForkPathScope:
    def __init__(self, resolver: SessionCatalogPathResolver) -> None:
        self._path_resolver = resolver
        self._prepared_target: ContextVar[PreparedForkTarget | None] = ContextVar("prepared_fork_target", default=None)
        self._source_snapshot: ContextVar[ForkSourceSnapshot | None] = ContextVar("fork_source_snapshot", default=None)

    def thread_identity(self, session_id: str, thread_id: str | None) -> str:
        """解析 (session_id, thread_id) 的 thread 半。

        thread_id 未显式给出、或给成裸 main 别名 / 裸 session_id 时，必须经
        thread catalog 显式解析该 Session 的唯一 main thread，绝不把 session_id
        或裸 main 当作 thread identity 拼路径。
        """
        if not isinstance(session_id, str) or not session_id:
            raise TypeError("rollout 定位的 session_id 必须是非空字符串")
        try:
            validate_session_id(session_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "rollout 定位的 session_id 不是 canonical Session 身份（OpenSpec "
                f"8.3 要求 (session_id, thread_id)）：session_id={session_id!r}"
            ) from error
        if thread_id is None:
            thread_id = self.main_thread_id(session_id)
        if not isinstance(thread_id, str):
            raise TypeError("rollout 定位的 thread_id 必须是字符串")
        if thread_id in ("", "main", session_id):
            raise RuntimeError(
                "rollout 定位拒绝 thread alias；必须传入 catalog 的真实 thread_id: "
                f"session_id={session_id!r}, thread_id={thread_id!r}"
            )
        try:
            validate_thread_id(thread_id)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                f"rollout 定位的 thread_id 不是 canonical Thread 身份: {thread_id!r}"
            ) from error
        return thread_id


    def main_thread_id(self, session_id: str) -> str:
        record = self.prepared_target(session_id)
        if record is not None:
            return record.thread_id
        snapshot = self.source_snapshot(session_id)
        if snapshot is not None:
            return snapshot.thread_id
        return self._path_resolver.main_thread_id(session_id)


    def session_node(self, session_id: str) -> Path:
        """按当前 capability 解析 Session 节点，普通访问仍由 catalog 裁决。"""
        record = self.prepared_target(session_id)
        if record is not None:
            return record.session_node
        snapshot = self.source_snapshot(session_id)
        if snapshot is not None:
            return snapshot.captured_root
        return self._path_resolver.resolve_session_node(session_id)

    def thread_node(self, session_id: str, thread_id: str) -> Path:
        """冻结来源只允许原 Thread；未发布目标使用原 creation 授予的节点。"""
        snapshot = self.source_snapshot(session_id)
        if snapshot is not None and thread_id == snapshot.thread_id:
            return snapshot.captured_root
        record = self.prepared_target(session_id)
        if record is not None and thread_id == record.thread_id:
            return record.thread_node
        if snapshot is not None:
            raise RuntimeError("fork source snapshot 只授予冻结 source Thread 的路径能力")
        return self._path_resolver.resolve_thread_node(session_id, thread_id)

    @contextmanager
    def prepared_fork_target_scope(
        self,
        record: SessionCreationRecord,
    ) -> Iterator[Path]:
        """把创建记录 capability 限定为本次 fork 的 target 路径解析权限。"""
        if not isinstance(record, SessionCreationRecord):
            raise TypeError("prepared fork target 必须是 SessionCreationRecord")
        target_path = self._path_resolver.resolve_prepared_session_node(record)
        thread_path = self._path_resolver.resolve_prepared_thread_node(record, record.main_thread_id)
        target = PreparedForkTarget(record.session_id, record.main_thread_id, target_path, thread_path, record)
        token = self._prepared_target.set(target)
        try:
            yield target_path
        finally:
            self._prepared_target.reset(token)

    @contextmanager
    def prepared_child_fork_target_scope(self, session_id: str, record: ThreadCreationRecord) -> Iterator[Path]:
        """Thread creation owner 的冻结 record 只授予该未发布 child 的路径能力。"""
        if not isinstance(record, ThreadCreationRecord) or record.purpose != "fork":
            raise TypeError("prepared child fork target 必须是 fork ThreadCreationRecord")
        session_node = self._path_resolver.resolve_session_node(session_id)
        control = SessionControlStore(session_node / "session-control.sqlite", read_only=True)
        try:
            current = control.get_thread_creation_record(record.thread_creation_idempotency_key)
            if current != record or current.state not in {"preparing", "published"}:
                raise RuntimeError("prepared child fork target 的冻结 creation intent 已变化")
        finally:
            control.close()
        thread_node = session_node / record.final_relative_locator
        if thread_node.is_symlink() or not thread_node.is_dir():
            raise RuntimeError("prepared child fork target 缺少已 rename 的普通节点目录")
        target = PreparedForkTarget(session_id, record.child_thread_id, session_node, thread_node, None)
        token = self._prepared_target.set(target)
        try:
            yield thread_node
        finally:
            self._prepared_target.reset(token)

    @contextmanager
    def fork_source_snapshot_scope(
        self,
        *,
        session_id: str,
        thread_id: str,
        captured_root: Path,
    ) -> Iterator[Path]:
        """把 source Thread 的 rollout I/O 定向到私有 snapshot captured_root。

        ``session_id``/``thread_id`` 仅标识来源历史 owner，thread_id 可为 child；
        物理 I/O 只使用调用方传入的私有捕获副本 ``captured_root``，不按 main_thread_id
        或 thread_id 推导路径。
        """
        validate_session_id(session_id)
        validate_thread_id(thread_id)
        if not isinstance(captured_root, Path):
            raise TypeError("fork source snapshot captured_root 必须是 Path")
        if self._source_snapshot.get() is not None:
            raise RuntimeError("fork source snapshot scope 不能嵌套")
        snapshot_node = captured_root.resolve(strict=True)
        thread_node = snapshot_node
        if (
            snapshot_node.is_symlink()
            or not snapshot_node.is_dir()
            or thread_node.is_symlink()
            or not thread_node.is_dir()
        ):
            raise RuntimeError(
                "fork source Thread 的私有 snapshot captured_root 必须是普通目录: "
                f"captured_root={snapshot_node}, thread_id={thread_id}"
            )
        snapshot = ForkSourceSnapshot(session_id, thread_id, snapshot_node)
        token: Token[ForkSourceSnapshot | None] = self._source_snapshot.set(
            snapshot
        )
        try:
            yield thread_node
        finally:
            self._source_snapshot.reset(token)

    def source_snapshot(
        self, session_id: str,
    ) -> ForkSourceSnapshot | None:
        snapshot = self._source_snapshot.get()
        return snapshot if snapshot is not None and snapshot.session_id == session_id else None

    def prepared_target(
        self, session_id: str,
    ) -> PreparedForkTarget | None:
        record = self._prepared_target.get()
        return record if record is not None and record.session_id == session_id else None
