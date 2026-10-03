"""per-session ``session-control.sqlite`` 的 thread creation record 垂直链路。

承载 ``thread_creation_records`` 表的唯一实现：表 DDL 与 delegation 部分唯一
索引、行投影 ``ThreadCreationRecord``、冻结列（GraphBinding/capability/seed/
reference）JSON 形态闸门、create-or-get 幂等准入与插入路径。

record 不进入 thread catalog；publish 是唯一可见性提交点，由 ``thread_
creation_publish`` 族在同一事务内完成。``initial_state`` 闭集 ``running/idle``
是 creation record 与 execution intent 共用的形态常量，唯一定义在本模块。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.identifier import (
    create_prefixed_id,
    to_epoch_ms,
    uuid7_datetime_from_hex,
)
from app.core.session_catalog_store import validate_thread_id
from app.core.session_control_primitives import validate_thread_creation_key
from app.core.session_control_store.sql import (
    SELECT_THREAD_CATALOG_CHILD_ROW,
    SELECT_THREAD_CATALOG_ROW_COUNT,
)
from app.core.session_control_thread_catalog.thread_catalog import (
    fetch_fence_row,
    validate_thread_relative_locator,
)

# ThreadCreationRecord（8.5-A，R20）：child thread 创建流 operation lease
# record。与 workspace catalog 的 session_creation_records 同构：record
# 在建立任何 staging 前 create-or-get（gate 内短事务）、不进入
# thread_catalog、冻结创建身份/precondition；publish 是唯一可见性提交点
# （thread_catalog child row 与 record→published 在同一事务）；CAS 失败
# 只定点清理 record 列出的目录并 abort，不发布、不重基。
# 列序说明：任务书 §2.1-A 给出的 16 列逐字保留（名称/顺序/约束），其间的
# ``artifact_manifest`` / ``graph_binding`` / ``capability_profile`` /
# ``task_seed`` / ``task_reference`` / ``child_created_at`` 为加法列
# （tasks.md 8.5-A 冻结项 GraphBinding/capability/seed/reference、artifact
# manifest 本体与 child UTC created_at——恢复只按预存 record 校验，需要
# 清单本体而非仅 hash），字段映射见 rounds/R20-impl.md §6。
_THREAD_CREATION_RECORDS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS thread_creation_records (
    thread_creation_idempotency_key TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'published', 'aborted')),
    preimage_hash TEXT NOT NULL,
    delegation_id TEXT,
    child_thread_id TEXT NOT NULL UNIQUE,
    final_relative_locator TEXT NOT NULL UNIQUE,
    staging_locator TEXT NOT NULL,
    artifact_manifest TEXT,
    artifact_manifest_hash TEXT,
    graph_binding TEXT NOT NULL,
    capability_profile TEXT NOT NULL,
    task_seed TEXT,
    task_reference TEXT,
    owner_session_lifecycle_generation INTEGER NOT NULL,
    catalog_precondition_revision INTEGER NOT NULL,
    collaboration_precondition_revision INTEGER,
    initial_state TEXT NOT NULL CHECK (initial_state IN ('running', 'idle')),
    admission_intent TEXT NOT NULL,
    abort_reason TEXT,
    child_created_at TEXT NOT NULL,
    record_created_at TEXT NOT NULL,
    record_updated_at TEXT NOT NULL
)
"""

# delegated child 将 delegation_id 纳入唯一约束（tasks.md 8.5-A）：同一
# delegation 至多绑定一条创建 record（含 aborted——失败后须换新
# delegation 重试，不静默改绑）。
_IDX_THREAD_CREATION_DELEGATION_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_creation_delegation "
    "ON thread_creation_records(delegation_id) WHERE delegation_id IS NOT NULL"
)


_THREAD_CREATION_RECORD_COLUMNS = (
    "thread_creation_idempotency_key, state, preimage_hash, delegation_id, "
    "child_thread_id, final_relative_locator, staging_locator, "
    "artifact_manifest, artifact_manifest_hash, graph_binding, "
    "capability_profile, task_seed, task_reference, "
    "owner_session_lifecycle_generation, catalog_precondition_revision, "
    "collaboration_precondition_revision, initial_state, admission_intent, "
    "abort_reason, child_created_at, record_created_at, record_updated_at"
)


_INITIAL_STATE_VALUES = ("running", "idle")



@dataclass(frozen=True, slots=True)
class ThreadCreationRecord:
    """thread_creation_records 表行的不可变投影（8.5-A 创建 lease record）。

    ``state`` 闭集为 ``preparing/published/aborted``；record 冻结 child
    身份（``child_thread_id``/``child_created_at``）、最终与内部 staging
    locator、GraphBinding/capability/seed/reference（canonical JSON 文
    本）、artifact manifest 清单本体与 hash、owner Session lifecycle
    generation 与 catalog/collaboration precondition revision；publish
    时在同一事务 CAS 校验后插入 thread_catalog child row（唯一可见性
    提交点）。``artifact_manifest`` 是「预期内容清单」（相对路径 →
    sha256）的 canonical JSON 文本，``artifact_manifest_hash`` 是该文本
    的 sha256——清单本体随 record 冻结，恢复只按预存 record 校验。
    """

    thread_creation_idempotency_key: str
    state: str
    preimage_hash: str
    delegation_id: str | None
    child_thread_id: str
    final_relative_locator: str
    staging_locator: str
    artifact_manifest: str | None
    artifact_manifest_hash: str | None
    graph_binding: str
    capability_profile: str
    task_seed: str | None
    task_reference: str | None
    owner_session_lifecycle_generation: int
    catalog_precondition_revision: int
    collaboration_precondition_revision: int | None
    initial_state: str
    admission_intent: str
    abort_reason: str | None
    child_created_at: str
    record_created_at: str
    record_updated_at: str


class ThreadCreationRecordMixin:
    """thread creation record 的 create-or-get 与行投影方法族。"""

    # ------------------------------------------------------------------
    # ThreadCreationRecord（8.5-A，R20：child thread 创建流 operation lease）
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_frozen_json_text(value: str, label: str) -> None:
        """冻结列（GraphBinding/capability/seed/reference）形态校验。

        必须是可解析为 JSON 对象（dict）的非空文本——service 序列化，
        store 只做形态闸门（绕过软件直改库时 fail closed）。
        """
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} 必须是非空 JSON 文本: {value!r}")
        try:
            parsed = json.loads(value)
        except ValueError as error:
            raise ValueError(
                f"{label} 不是合法 JSON 文本: {value!r}: {error}"
            ) from error
        if not isinstance(parsed, dict):
            # JSON 文本「内容形态」校验（值本身已是 str，非调用方类型错），
            # 按模块错误分类保持 ValueError（R12 冻结列解析同款 noqa 先例）。
            raise ValueError(  # noqa: TRY004
                f"{label} 必须序列化为 JSON 对象: {value!r}"
            )

    @staticmethod
    def _validate_optional_frozen_json_text(
        value: str | None,
        label: str,
    ) -> None:
        if value is None:
            return
        ThreadCreationRecordMixin._validate_frozen_json_text(value, label)

    def _fetch_thread_creation_record(
        self,
        connection: sqlite3.Connection,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_THREAD_CREATION_RECORD_COLUMNS} "
            "FROM thread_creation_records "
            "WHERE thread_creation_idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()

    @staticmethod
    def _thread_creation_record_from_row(
        row: sqlite3.Row,
    ) -> ThreadCreationRecord:
        return ThreadCreationRecord(
            thread_creation_idempotency_key=str(
                row["thread_creation_idempotency_key"]
            ),
            state=str(row["state"]),
            preimage_hash=str(row["preimage_hash"]),
            delegation_id=(
                str(row["delegation_id"])
                if row["delegation_id"] is not None
                else None
            ),
            child_thread_id=str(row["child_thread_id"]),
            final_relative_locator=str(row["final_relative_locator"]),
            staging_locator=str(row["staging_locator"]),
            artifact_manifest=(
                str(row["artifact_manifest"])
                if row["artifact_manifest"] is not None
                else None
            ),
            artifact_manifest_hash=(
                str(row["artifact_manifest_hash"])
                if row["artifact_manifest_hash"] is not None
                else None
            ),
            graph_binding=str(row["graph_binding"]),
            capability_profile=str(row["capability_profile"]),
            task_seed=str(row["task_seed"]) if row["task_seed"] is not None else None,
            task_reference=(
                str(row["task_reference"])
                if row["task_reference"] is not None
                else None
            ),
            owner_session_lifecycle_generation=int(
                row["owner_session_lifecycle_generation"]
            ),
            catalog_precondition_revision=int(
                row["catalog_precondition_revision"]
            ),
            collaboration_precondition_revision=(
                int(row["collaboration_precondition_revision"])
                if row["collaboration_precondition_revision"] is not None
                else None
            ),
            initial_state=str(row["initial_state"]),
            admission_intent=str(row["admission_intent"]),
            abort_reason=(
                str(row["abort_reason"]) if row["abort_reason"] is not None else None
            ),
            child_created_at=str(row["child_created_at"]),
            record_created_at=str(row["record_created_at"]),
            record_updated_at=str(row["record_updated_at"]),
        )

    def create_or_get_thread_creation_record(
        self,
        *,
        idempotency_key: str,
        initial_state: str,
        preimage_hash: str,
        graph_binding: str,
        capability_profile: str,
        created_at: datetime | None = None,
        thread_id: str | None = None,
        delegation_id: str | None = None,
        task_seed: str | None = None,
        task_reference: str | None = None,
        collaboration_precondition_revision: int | None = None,
    ) -> ThreadCreationRecord:
        """create-or-get ThreadCreationRecord（gate 内短事务，8.5-A）。

        - 同 key 已存在：``preimage_hash`` 一致 → 返回既有 record（幂等，
          child ID/locator/created_at 等冻结值以既有 record 为准）；
          不一致 → ``RuntimeError``（同 key 不同 preimage 冲突）。传入
          ``thread_id`` 与既有 record 的 child_thread_id 不一致同样拒绝
          （不静默改绑）。
        - 不存在 → 软件分配（或采用传入的已验证 canonical）child
          thread ID（``thr_``）、按 created_at 的 UTC 日期冻结
          ``threads/YYYY/MM/DD/{thread_id}`` 最终 locator 与
          ``.staging/{key}`` 内部 staging locator，读 owner fence
          （必须 active）冻结 generation、按 thread_catalog 行数冻结
          catalog precondition revision，插入 ``state='preparing'``。
          **不进入 thread_catalog、不建立任何 staging 目录**。
        - delegated child 必须携带非空 ``delegation_id``，并受
          ``idx_thread_creation_delegation`` 部分唯一约束（同一
          delegation 至多一条 record，含 aborted）。
        - ``collaboration_precondition_revision`` 本轮恒为 None（
          collaboration ledger 归 8.5）；publish 时遇非空值 fail closed。
        """
        validate_thread_creation_key(idempotency_key)
        if initial_state not in _INITIAL_STATE_VALUES:
            raise ValueError(f"initial_state 非法: {initial_state!r}")
        if not isinstance(preimage_hash, str) or not preimage_hash:
            raise ValueError(f"preimage_hash 不能为空: {preimage_hash!r}")
        self._validate_frozen_json_text(graph_binding, "graph_binding")
        self._validate_frozen_json_text(capability_profile, "capability_profile")
        self._validate_optional_frozen_json_text(task_seed, "task_seed")
        self._validate_optional_frozen_json_text(task_reference, "task_reference")
        if created_at is not None and not isinstance(created_at, datetime):
            raise TypeError(f"created_at 必须是 datetime: {created_at!r}")
        if thread_id is not None:
            validate_thread_id(thread_id)
        if (created_at is None) != (thread_id is None):
            raise ValueError(
                "固定 Thread fixture 必须同时提供 created_at 与 thread_id"
            )
        if created_at is not None and created_at.tzinfo is None:
            raise ValueError(f"created_at 必须带时区: {created_at!r}")
        if created_at is not None and thread_id is not None:
            thread_ms = to_epoch_ms(uuid7_datetime_from_hex(thread_id[4:]))
            created_ms = to_epoch_ms(created_at)
            if thread_ms != created_ms:
                raise ValueError(
                    "固定 Thread fixture 的 created_at 必须与 thread_id "
                    "内嵌时间戳一致: "
                    f"created_at_ms={created_ms}, thread_ms={thread_ms}"
                )
        if delegation_id is not None and (
            not isinstance(delegation_id, str) or not delegation_id
        ):
            raise ValueError(
                "delegated child 必须携带非空 delegation_id: "
                f"{delegation_id!r}"
            )
        if collaboration_precondition_revision is not None and (
            not isinstance(collaboration_precondition_revision, int)
            or isinstance(collaboration_precondition_revision, bool)
        ):
            raise TypeError(
                "collaboration_precondition_revision 必须是整数或 None: "
                f"{collaboration_precondition_revision!r}"
            )
        self._begin_immediate()
        try:
            existing = self._fetch_thread_creation_record(
                self._connection, idempotency_key
            )
            if existing is not None:
                if str(existing["preimage_hash"]) != preimage_hash:
                    raise RuntimeError(
                        "thread creation record preimage 冲突（同 key 不同 "
                        "preimage，拒绝复用）: "
                        f"key={idempotency_key!r}, "
                        f"existing_preimage={existing['preimage_hash']!r}, "
                        f"requested_preimage={preimage_hash!r}"
                    )
                if (
                    thread_id is not None
                    and str(existing["child_thread_id"]) != thread_id
                ):
                    raise RuntimeError(
                        "thread creation record child ID 冲突（同 key 幂等"
                        "复用时传入 thread_id 与既有 record 不一致，拒绝改"
                        f"绑）: key={idempotency_key!r}, "
                        f"existing_child_thread_id={existing['child_thread_id']!r}, "
                        f"requested_thread_id={thread_id!r}"
                    )
                record = self._thread_creation_record_from_row(existing)
            else:
                record = self._insert_thread_creation_record(
                    idempotency_key=idempotency_key,
                    initial_state=initial_state,
                    preimage_hash=preimage_hash,
                    graph_binding=graph_binding,
                    capability_profile=capability_profile,
                    created_at=created_at,
                    thread_id=thread_id,
                    delegation_id=delegation_id,
                    task_seed=task_seed,
                    task_reference=task_reference,
                    collaboration_precondition_revision=(
                        collaboration_precondition_revision
                    ),
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")
        return record

    def _insert_thread_creation_record(
        self,
        *,
        idempotency_key: str,
        initial_state: str,
        preimage_hash: str,
        graph_binding: str,
        capability_profile: str,
        created_at: datetime | None,
        thread_id: str | None,
        delegation_id: str | None,
        task_seed: str | None,
        task_reference: str | None,
        collaboration_precondition_revision: int | None,
    ) -> ThreadCreationRecord:
        """插入路径（事务内先验证后写入；调用方已开写事务）。"""
        # owner fence 必须存在且 active：deleting 的 session 禁止新建
        # child 创建 operation（8.1-B 删除流关闭点之后的准入红线）。
        fence_row = fetch_fence_row(self._connection)
        if fence_row is None:
            raise KeyError(
                "session control 缺少 lifecycle fence row，无法冻结 owner "
                f"lifecycle generation: path={self.database_path}"
            )
        if str(fence_row["state"]) != "active":
            raise RuntimeError(
                "owner fence 非 active，拒绝建立 thread creation record"
                f"（fail closed）: path={self.database_path}, "
                f"fence_state={fence_row['state']!r}"
            )
        main_rows = self._connection.execute(
            "SELECT thread_id FROM thread_catalog WHERE kind = 'main'"
        ).fetchall()
        if len(main_rows) != 1:
            raise RuntimeError(
                "session control main row 缺失或多行，拒绝建立 thread "
                f"creation record（fail closed）: path={self.database_path}, "
                f"main_rows={[str(row['thread_id']) for row in main_rows]}"
            )
        main_thread_id = str(main_rows[0]["thread_id"])
        if thread_id is None:
            child_thread_id = create_prefixed_id("thr")
            child_created_at = uuid7_datetime_from_hex(child_thread_id[4:])
        else:
            if created_at is None:
                raise RuntimeError("Thread creation record 固定 fixture 时间缺失")
            child_thread_id = thread_id
            child_created_at = created_at
        validate_thread_id(child_thread_id)
        if child_thread_id == main_thread_id:
            raise RuntimeError(
                "child thread ID 与 main row thread_id 冲突（fail closed）: "
                f"thread_id={child_thread_id!r}"
            )
        occupied = self._connection.execute(
            SELECT_THREAD_CATALOG_CHILD_ROW,
            (child_thread_id,),
        ).fetchone()
        if occupied is not None:
            raise RuntimeError(
                "child thread ID 已被 thread catalog 占用（fail closed）: "
                f"thread_id={child_thread_id!r}"
            )
        utc_date = child_created_at.astimezone(UTC).date()
        final_relative_locator = f"threads/{utc_date:%Y/%m/%d}/{child_thread_id}"
        validate_thread_relative_locator(final_relative_locator)
        locator_taken = self._connection.execute(
            "SELECT 1 FROM thread_creation_records "
            "WHERE final_relative_locator = ?",
            (final_relative_locator,),
        ).fetchone()
        if locator_taken is not None:
            raise RuntimeError(
                "最终 locator 已被其它 thread creation record 冻结"
                f"（fail closed）: final_relative_locator="
                f"{final_relative_locator!r}"
            )
        if delegation_id is not None:
            delegation_taken = self._connection.execute(
                "SELECT 1 FROM thread_creation_records WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if delegation_taken is not None:
                raise RuntimeError(
                    "delegation_id 已绑定其它 thread creation record（部分"
                    "唯一约束，delegated child 一 delegation 一 record）: "
                    f"delegation_id={delegation_id!r}"
                )
        # catalog precondition revision（本轮度量）：thread_catalog 行数。
        # 本轮 thread_catalog 只有插入型变更（行数单调不减），行数等价于
        # 单调 revision；publish 时 CAS 校验行数未漂移。
        catalog_precondition_revision = int(
            self._connection.execute(
                SELECT_THREAD_CATALOG_ROW_COUNT
            ).fetchone()[0]
        )
        # admission identity（8.5-A 冻结项）：幂等键 + thread ref + 初始
        # state；发布后由幂等 worker create-or-get 初始 execution。
        admission_intent = json.dumps(
            {
                "admission_idempotency_key": idempotency_key,
                "thread_id": child_thread_id,
                "initial_state": initial_state,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        record_created_at = datetime.now(UTC).isoformat()
        # 命名参数逐列对应（避免位置占位符错位）：artifact_manifest/
        # artifact_manifest_hash/abort_reason 三列此处为 NULL，由
        # freeze_thread_creation_artifact_manifest 与 abort 流程分别回填。
        self._connection.execute(
            "INSERT INTO thread_creation_records ("
            f"{_THREAD_CREATION_RECORD_COLUMNS}) "
            "VALUES (:idempotency_key, 'preparing', :preimage_hash, "
            ":delegation_id, :child_thread_id, :final_relative_locator, "
            ":staging_locator, NULL, NULL, :graph_binding, "
            ":capability_profile, :task_seed, :task_reference, "
            ":owner_generation, :catalog_revision, :collaboration_revision, "
            ":initial_state, :admission_intent, NULL, :child_created_at, "
            ":record_created_at, :record_updated_at)",
            {
                "idempotency_key": idempotency_key,
                "preimage_hash": preimage_hash,
                "delegation_id": delegation_id,
                "child_thread_id": child_thread_id,
                "final_relative_locator": final_relative_locator,
                "staging_locator": f".staging/{idempotency_key}",
                "graph_binding": graph_binding,
                "capability_profile": capability_profile,
                "task_seed": task_seed,
                "task_reference": task_reference,
                "owner_generation": int(fence_row["generation"]),
                "catalog_revision": catalog_precondition_revision,
                "collaboration_revision": collaboration_precondition_revision,
                "initial_state": initial_state,
                "admission_intent": admission_intent,
                "child_created_at": child_created_at.isoformat(),
                "record_created_at": record_created_at,
                "record_updated_at": record_created_at,
            },
        )
        row = self._fetch_thread_creation_record(self._connection, idempotency_key)
        if row is None:
            # 防御性兜底：同事务内刚插入必然可见。
            raise RuntimeError(
                "thread creation record 插入后不可见（事务异常）: "
                f"key={idempotency_key!r}"
            )
        return self._thread_creation_record_from_row(row)
