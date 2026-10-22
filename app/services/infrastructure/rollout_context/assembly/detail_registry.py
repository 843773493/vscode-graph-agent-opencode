"""受 owner 约束的详情 manifest registry；正文仅由 runtime detail store 持有。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.selection import ContextSelectionEntry
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    check_resource_target,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceTargetAction,
    ResourceTargetContext,
    ResourceTargetFacts,
)
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.assembly.detail_retention import (
    DetailRetentionMixin,
)
from app.services.infrastructure.rollout_context.runtime.detail_files import DetailFiles
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    detail_record_from_mapping,
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text

_COLUMNS = (
    "detail_ref", "session_id", "thread_id", "checkpoint_ns", "assembly_id", "detail_id",
    "detail_kind", "retention_class", "visibility", "relative_path", "content_hash",
    "source_revision", "content_length", "protection",
    "availability", "required", "sensitive", "status", "created_at", "expires_at",
)


def _detail_mapping(row: tuple[object, ...]) -> dict[str, object]:
    value = dict(zip(_COLUMNS, row, strict=True))
    ref = detail_ref_from_key(value["detail_ref"])
    value["detail_ref"] = ref
    value["length"] = value.pop("content_length")
    record = detail_record_from_mapping(value)
    if record.detail_ref != ref:
        raise ValueError("source-mismatch: detail owner 字段与 typed reference 不一致")
    if record.relative_path != detail_relative_path(ref).as_posix():
        raise ValueError("source-mismatch: detail locator 不属于 typed owner")
    return value


def validate_assembly_details(
    connection: sqlite3.Connection, snapshot: ContextAssemblySnapshot,
    checkpoint_ns: str, header_detail_key: str | None,
) -> None:
    """seal preflight 与恢复共用同一份详情完整性合同，不在提交后补查。"""
    selected: dict[str, ContextSelectionEntry | None] = {
        detail_ref_key(entry.detail_ref): entry
        for entry in snapshot.selection
        if entry.included and entry.detail_ref is not None
    }
    if header_detail_key is not None:
        detail_ref_from_key(header_detail_key).require_owner(
            snapshot.session_id, snapshot.thread_id, snapshot.assembly_id
        )
        selected.setdefault(header_detail_key, None)
    rows = connection.execute(
        f"SELECT {','.join(_COLUMNS)} FROM context_plan_details "
        "WHERE session_id = ? AND thread_id = ? AND assembly_id = ?",
        (snapshot.session_id, snapshot.thread_id, snapshot.assembly_id),
    ).fetchall()
    records = {row[0]: _detail_mapping(tuple(row)) for row in rows}
    if set(records) != set(selected):
        raise RuntimeError("detail-unavailable: sealed assembly detail manifest 集合不一致")
    for key, record in records.items():
        ref = detail_ref_from_key(key)
        ref.require_owner(snapshot.session_id, snapshot.thread_id, snapshot.assembly_id)
        entry = selected[key]
        # optional header 是审计附件，不是进入请求的正文。fork/GC 可以保留
        # 它的 unavailable manifest；included source 和 required header 仍必须可读。
        needs_body = entry is not None or bool(record["required"])
        if (record["session_id"], record["thread_id"]) != (
            snapshot.session_id,
            snapshot.thread_id,
        ):
            raise RuntimeError("source-mismatch: detail manifest 与 assembly owner 不一致")
        if record["checkpoint_ns"] != checkpoint_ns or (
            needs_body and record["availability"] != "available"
        ):
            raise RuntimeError("detail-unavailable: sealed assembly detail 状态或 namespace 不一致")
        if entry is not None and (
            entry.source_revision != record["source_revision"]
            or entry.content_length != record["length"]
            or entry.protection != record["protection"]
            or entry.visibility != record["visibility"]
            or entry.availability != record["availability"]
            or record["detail_kind"] != "request_source"
            or record["retention_class"] != "request_replay"
        ):
            raise RuntimeError("source-mismatch: sealed assembly detail source manifest 不一致")
        if entry is None and (
            record["detail_kind"] != "assembly_snapshot"
            or record["retention_class"] != "assembly_audit"
        ):
            raise RuntimeError("source-mismatch: assembly header detail 用途不一致")


class DetailRegistryMixin(DetailRetentionMixin):
    @staticmethod
    def check_fork_target_resources(
        connection: sqlite3.Connection, source: ResourceTargetFacts, target: ResourceTargetContext,
        *, choices: Mapping[str, ResourceTargetAction], selected_only: bool, captured_node: Path,
    ) -> None:
        """只处理冻结 manifest 的 backing 正文，不把历史 DetailRef 改绑到目标。"""
        selection = " JOIN fork_selected_details selected ON selected.detail_ref=detail.detail_ref" if selected_only else ""
        for key, session_id, thread_id, assembly_id, relative_path, revision, digest, length, availability, required in connection.execute(
            "SELECT detail.detail_ref,detail.session_id,detail.thread_id,detail.assembly_id,detail.relative_path,"
            "detail.source_revision,detail.content_hash,detail.content_length,detail.availability,detail.required "
            "FROM context_plan_details detail" + selection,
        ):
            ref = detail_ref_from_key(key)
            ref.require_owner(session_id, thread_id, assembly_id)
            require_historical_scope(connection, session_id, thread_id)
            if relative_path != str(detail_relative_path(ref)):
                raise ValueError("source-mismatch: 冻结 detail locator 不属于精确历史 owner")
            available = availability == "available"
            decision = check_resource_target(
                replace(source, available=available, required_for_context=available or bool(required),
                        revision=revision, content_hash=digest, content_length=length),
                target, choice=choices.get(source.resource_uri),
            )
            if decision.available and decision.action != "copy_snapshot":
                raise ValueError("detail owner 只支持冻结快照导入")
            if decision.available:
                path = captured_node / detail_relative_path(ref)
                if path.is_symlink() or not path.is_file():
                    raise FileNotFoundError(f"context_required_resource_unavailable: 冻结 detail 正文缺失: {ref}")

    def register_context_plan_detail(self, detail: DetailRecord) -> None:
        """独立 detail 写入端口；seal 使用显式的同 connection 端口。"""
        if not isinstance(detail, DetailRecord):
            raise TypeError("detail registry 只接受完整 DetailRecord")
        ref = detail.detail_ref
        with self._storage._lock(
            ref.thread_id, detail.checkpoint_ns, session_id=ref.session_id
        ):
            self._storage.initialize(
                ref.session_id, detail.checkpoint_ns, thread_id=ref.thread_id
            )
            with self._storage._connect(
                ref.thread_id, detail.checkpoint_ns, session_id=ref.session_id
            ) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._register_context_plan_detail(
                    connection, detail, session_id=ref.session_id,
                    thread_id=ref.thread_id, checkpoint_ns=detail.checkpoint_ns,
                )
                connection.commit()

    def _register_context_plan_detail(
        self, connection: sqlite3.Connection, detail: DetailRecord, *,
        session_id: str, thread_id: str, checkpoint_ns: str,
    ) -> None:
        """唯一 manifest writer；不初始化、不另开 connection、不提交事务。"""
        if not connection.in_transaction:
            raise RuntimeError("detail manifest 登记需要调用方的活动事务")
        self._storage._require_v2_runtime(connection)
        if not isinstance(detail, DetailRecord):
            raise TypeError("detail registry 只接受完整 DetailRecord")
        ref = detail.detail_ref
        ref.require_owner(session_id, thread_id)
        if detail.checkpoint_ns != checkpoint_ns:
            raise ValueError("source-mismatch: detail manifest namespace 不一致")
        key = detail_ref_key(ref)
        expected_path = detail_relative_path(ref)
        if detail.relative_path != expected_path.as_posix():
            raise ValueError("detail locator 必须严格等于 typed owner 的 assembly/detail_id 路径")
        # 使用正文唯一 path owner 校验既存路径；尚未发布的父目录可不存在。
        DetailFiles(self._storage._path_resolver).path(
            ref, creation_record=(
                target.creation_record if (target := self._storage.fork_paths.prepared_target(ref.session_id)) is not None else None
            ),
        )
        values = (
            key, ref.session_id, ref.thread_id, detail.checkpoint_ns, ref.assembly_id, ref.detail_id,
            detail.detail_kind, detail.retention_class, detail.visibility,
            detail.relative_path, detail.content_hash, detail.source_revision,
            detail.length, detail.protection,
            detail.availability, int(detail.required), int(detail.sensitive),
            detail.status, datetime.now(UTC).isoformat(), detail.expires_at,
        )
        _detail_mapping(values)
        existing = connection.execute(
            f"SELECT {','.join(_COLUMNS)} FROM context_plan_details WHERE detail_ref = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            _detail_mapping(tuple(existing))
            # created_at 属于首次 registry 提交；其余 manifest 均不可覆盖。
            if tuple(existing[:18]) != values[:18] or tuple(existing[19:]) != values[19:]:
                raise ValueError("source-mismatch: detail_ref 已存在但 manifest 冲突")
            return
        result = connection.execute(
            f"INSERT INTO context_plan_details ({','.join(_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in _COLUMNS)})", values,
        )
        if result.rowcount != 1:
            raise RuntimeError("context detail manifest 未写入")

    def get_context_plan_detail(
        self,
        session_id: str,
        thread_id: str,
        *,
        detail_ref: DetailRef,
        checkpoint_ns: str = "",
        snapshot: RolloutReadSnapshot | None = None,
    ) -> dict[str, object]:
        """读取并核验 SQLite detail manifest，不读取正文文件。

        返回映射已将 ``detail_ref`` 解码为 typed ``DetailRef``，并把数据库列
        ``content_length`` 映射为 ``length``；``required`` 与 ``sensitive``
        保持 SQLite 的 0/1 值，经过 ``detail_record_from_mapping`` 后才成为
        typed record 中的 bool。
        """
        session_id = strict_text(session_id, field="get_context_plan_detail.session_id")
        thread_id = strict_text(thread_id, field="get_context_plan_detail.thread_id")
        checkpoint_ns = strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        key = detail_ref_key(detail_ref)
        with self._storage.read_connection(
            session_id, thread_id, checkpoint_ns, snapshot=snapshot,
        ) as connection:
            self._storage._require_v2_runtime(connection)
            require_historical_scope(
                connection, detail_ref.session_id, detail_ref.thread_id,
                current_owner=(session_id, thread_id),
            )
            row = connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM context_plan_details "
                "WHERE detail_ref = ? AND session_id = ? AND thread_id = ? AND checkpoint_ns = ?",
                (key, detail_ref.session_id, detail_ref.thread_id, checkpoint_ns),
            ).fetchone()
        if row is None:
            raise KeyError(f"detail-unavailable: context detail 不存在: {detail_ref}")
        value = _detail_mapping(tuple(row))
        if value["detail_ref"] != detail_ref:
            raise ValueError("source-mismatch: registry 返回了错误的 detail identity")
        return value
