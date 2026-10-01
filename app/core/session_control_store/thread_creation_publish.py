"""per-session ``session-control.sqlite`` 的 thread creation 发布/终结链路。

承载 creation record 的可见性提交点与终态方法：冻结 artifact manifest、CAS
发布（唯一可见性提交点）、abort 终结与 published 终态幂等校验。发布事务在同一
事务内插入 ``thread_catalog`` child row、推进 record 状态、建立 owner binding
字段槽并按需把 collaboration member 转正。

错误分类沿用宿主约定：``TypeError`` 输入类型错误、``ValueError`` 输入形态
非法、``KeyError`` 目标行不存在、``RuntimeError`` 语义冲突。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.core.session_catalog_store import validate_thread_id
from app.core.session_control_primitives import (
    SHA256_HEX_PATTERN,
    validate_thread_creation_key,
)
from app.core.session_control_store.sql import (
    SELECT_COLLABORATION_LEDGER_REVISION,
    SELECT_THREAD_CATALOG_CHILD_ROW,
    SELECT_THREAD_CATALOG_ROW_COUNT,
)
from app.core.session_control_store.thread_creation_record import (
    ThreadCreationRecord,
)
from app.core.session_control_thread_catalog.thread_catalog import (
    read_fence_row,
    validate_thread_relative_locator,
)
from app.core.session_lifecycle_gate import SessionDeletionPendingError


class ThreadCreationPublishMixin:
    """thread creation record 的发布、abort 与终态校验方法族。"""

    def freeze_thread_creation_artifact_manifest(
        self,
        idempotency_key: str,
        *,
        artifact_manifest: str,
        artifact_manifest_hash: str,
    ) -> ThreadCreationRecord:
        """冻结预期 artifact 内容清单与 hash（preparing record，幂等）。

        - ``artifact_manifest`` 必须是「相对路径 → sha256」映射的
          canonical JSON 文本（store 做形态闸门，service 保证 canonical）；
        - ``artifact_manifest_hash`` 必须是 64 位小写 hex；
        - 已冻结且一致 → 幂等返回既有 record（恢复重入路径）；
        - 已冻结且不一致 → ``RuntimeError``（外部改动或确定性漂移，
          fail closed）；
        - 仅 preparing 可冻结；published/aborted 拒绝。
        """
        validate_thread_creation_key(idempotency_key)
        self._validate_frozen_json_text(artifact_manifest, "artifact_manifest")
        parsed = json.loads(artifact_manifest)
        for key, value in parsed.items():
            if not isinstance(key, str) or not isinstance(value, str):
                # 清单映射「内容形态」校验（json.loads 键恒为 str，此处
                # 防御外部篡改），按模块错误分类保持 ValueError。
                raise ValueError(  # noqa: TRY004
                    "artifact_manifest 必须是「相对路径 → sha256 字符串」"
                    f"映射: {key!r} -> {value!r}"
                )
        if (
            not isinstance(artifact_manifest_hash, str)
            or SHA256_HEX_PATTERN.fullmatch(artifact_manifest_hash) is None
        ):
            raise ValueError(
                "artifact_manifest_hash 必须是 64 位小写 hex: "
                f"{artifact_manifest_hash!r}"
            )
        with self._write_transaction() as connection:
            row = self._fetch_thread_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"thread creation record 不存在: key={idempotency_key!r}"
                )
            if str(row["state"]) != "preparing":
                raise RuntimeError(
                    "thread creation record 非 preparing，拒绝冻结 artifact "
                    f"manifest: key={idempotency_key!r}, state={row['state']!r}"
                )
            frozen_hash = row["artifact_manifest_hash"]
            if frozen_hash is not None:
                if (
                    str(frozen_hash) == artifact_manifest_hash
                    and str(row["artifact_manifest"]) == artifact_manifest
                ):
                    return self._thread_creation_record_from_row(row)
                raise RuntimeError(
                    "thread creation record artifact manifest 已冻结且与重入"
                    "计算不一致（外部改动或确定性漂移，fail closed）: "
                    f"key={idempotency_key!r}, "
                    f"frozen_hash={frozen_hash!r}, "
                    f"requested_hash={artifact_manifest_hash!r}"
                )
            connection.execute(
                "UPDATE thread_creation_records "
                "SET artifact_manifest = ?, artifact_manifest_hash = ?, "
                "record_updated_at = ? "
                "WHERE thread_creation_idempotency_key = ?",
                (
                    artifact_manifest,
                    artifact_manifest_hash,
                    datetime.now(UTC).isoformat(),
                    idempotency_key,
                ),
            )
            updated = self._fetch_thread_creation_record(
                connection, idempotency_key
            )
            if updated is None:
                # 防御性兜底：同事务内更新后必然可见。
                raise RuntimeError(
                    "thread creation record 冻结后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._thread_creation_record_from_row(updated)

    def get_thread_creation_record(
        self,
        idempotency_key: str,
    ) -> ThreadCreationRecord:
        """按幂等键返回 ThreadCreationRecord 投影；不存在抛 KeyError。"""
        validate_thread_creation_key(idempotency_key)
        self._ensure_open()
        row = self._fetch_thread_creation_record(self._connection, idempotency_key)
        if row is None:
            raise KeyError(
                f"thread creation record 不存在: key={idempotency_key!r}"
            )
        return self._thread_creation_record_from_row(row)

    def publish_thread_creation_record(
        self,
        idempotency_key: str,
    ) -> ThreadCreationRecord:
        """CAS 发布 ThreadCreationRecord（**唯一可见性提交点**，8.5-A）。

        单事务内依次验证：record 存在且 ``state='preparing'``；artifact
        manifest 已冻结；record 内部一致性（child ID canonical、最终
        locator 形态与日期）；**CAS 1**——owner fence 仍为 record 捕获的
        ``(active, generation)``；**CAS 2**——thread catalog 行数不得小于冻结
        的 catalog precondition revision（收缩=外部改动 fail closed；并发
        sibling publish 的合法增长不拦截，2.3-A 合同）；collaboration precondition
        revision 非空时 fail closed（collaboration ledger 归 8.5）。随后
        同一事务内插入 ``thread_catalog`` child row（kind='child'）并把
        record 推进为 ``published``。任何失败回滚整个事务：child row 不
        发布、record 保持 preparing（调用方定点清理后 abort）。
        """
        validate_thread_creation_key(idempotency_key)
        with self._write_transaction() as connection:
            row = self._fetch_thread_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"thread creation record 不存在: key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "published":
                raise RuntimeError(
                    "thread creation record 已发布，拒绝重复发布: "
                    f"key={idempotency_key!r}"
                )
            if state == "aborted":
                raise RuntimeError(
                    "thread creation record 已中止，拒绝发布: "
                    f"key={idempotency_key!r}, "
                    f"abort_reason={row['abort_reason']!r}"
                )
            child_thread_id = str(row["child_thread_id"])
            final_relative_locator = str(row["final_relative_locator"])
            child_created_at_text = str(row["child_created_at"])
            # record 内部一致性复验（防绕过软件直改 record）。
            validate_thread_id(child_thread_id)
            validate_thread_relative_locator(final_relative_locator)
            try:
                child_created_at = datetime.fromisoformat(child_created_at_text)
            except ValueError as error:
                raise RuntimeError(
                    "thread creation record child_created_at 无法解析（record "
                    f"被外部改动，fail closed）: {child_created_at_text!r}: "
                    f"{error}"
                ) from error
            expected_locator = (
                "threads/"
                f"{child_created_at.astimezone(UTC).date():%Y/%m/%d}/"
                f"{child_thread_id}"
            )
            if final_relative_locator != expected_locator:
                raise RuntimeError(
                    "thread creation record 最终 locator 与 child 创建日期不"
                    f"一致（record 被外部改动，fail closed）: "
                    f"key={idempotency_key!r}, "
                    f"frozen={final_relative_locator!r}, "
                    f"expected={expected_locator!r}"
                )
            if row["artifact_manifest_hash"] is None:
                raise RuntimeError(
                    "thread creation record artifact manifest 尚未冻结，拒绝"
                    f"发布（staging 前冻结合同被违反，fail closed）: "
                    f"key={idempotency_key!r}"
                )
            # CAS 1：owner fence 仍为捕获的 active generation。
            fence_row = read_fence_row(
                connection, database_path=self.database_path
            )
            expected_generation = int(row["owner_session_lifecycle_generation"])
            if str(fence_row["state"]) == "deleting":
                # 2.3-D：local fence 已 deleting → 统一删除中错误合同。
                raise SessionDeletionPendingError(
                    "session_deletion_pending: owner fence 已 deleting，"
                    f"拒绝新可见性发布（fail closed）: key={idempotency_key!r}"
                )
            if (
                str(fence_row["state"]) != "active"
                or int(fence_row["generation"]) != expected_generation
            ):
                raise RuntimeError(
                    "thread creation publish CAS 失败：owner fence 已漂移: "
                    f"key={idempotency_key!r}, "
                    f"expected=(active, {expected_generation}), "
                    f"actual=({fence_row['state']!r}, "
                    f"{int(fence_row['generation'])})"
                )
            # CAS 2：catalog precondition revision 收缩检测（度量=行数）。
            # 2.3-A 合同：同 Session 并发 sibling child 创建（不同幂等键）
            # 是合法交错，行数会因 sibling publish 合法增长；行数收缩只可
            # 能来自外部直改（删除流会先关 fence，CAS 1 已拦截）。故
            # frozen > actual 才 fail closed；精确相等会误伤合法并发发布
            # （跨进程文件锁使该交错成为真实合同而非偶然串行化产物）。
            actual_revision = int(
                connection.execute(
                    SELECT_THREAD_CATALOG_ROW_COUNT
                ).fetchone()[0]
            )
            frozen_revision = int(row["catalog_precondition_revision"])
            if actual_revision < frozen_revision:
                raise RuntimeError(
                    "thread creation publish CAS 失败：thread catalog "
                    f"precondition revision 已回退（行数收缩=外部改动，"
                    f"fail closed）: key={idempotency_key!r}, "
                    f"frozen_revision={frozen_revision}, "
                    f"actual_revision={actual_revision}"
                )
            # CAS 3：collaboration precondition revision 未漂移（R25 起
            # ledger 在本库落地；delegated record 必须冻结非空 revision，
            # manual creation（无 delegation）保持 None 且跳过校验）。
            if row["delegation_id"] is not None and (
                row["collaboration_precondition_revision"] is None
            ):
                raise RuntimeError(
                    "delegated thread creation record 缺少 collaboration "
                    f"precondition revision（fail closed）: "
                    f"key={idempotency_key!r}"
                )
            if row["collaboration_precondition_revision"] is not None:
                actual_collaboration_revision = int(
                    connection.execute(
                        SELECT_COLLABORATION_LEDGER_REVISION
                    ).fetchone()[0]
                )
                frozen_collaboration_revision = int(
                    row["collaboration_precondition_revision"]
                )
                if (
                    actual_collaboration_revision
                    != frozen_collaboration_revision
                ):
                    raise RuntimeError(
                        "thread creation publish CAS 失败：collaboration "
                        "ledger revision 已漂移: "
                        f"key={idempotency_key!r}, "
                        f"expected_revision={frozen_collaboration_revision}, "
                        f"actual_revision={actual_collaboration_revision}"
                    )
                member_row = connection.execute(
                    "SELECT state FROM collaboration_members "
                    "WHERE delegation_id = ?",
                    (str(row["delegation_id"]),),
                ).fetchone()
                if member_row is None:
                    raise RuntimeError(
                        "delegated thread creation record 的 collaboration "
                        f"member 缺失（fail closed）: key={idempotency_key!r}, "
                        f"delegation_id={row['delegation_id']!r}"
                    )
                if str(member_row["state"]) != "registering":
                    raise RuntimeError(
                        "collaboration member 非 registering，拒绝 publish "
                        f"转正（fail closed）: key={idempotency_key!r}, "
                        f"state={member_row['state']!r}"
                    )
            # 唯一可见性提交点前的最后占用预检（UNIQUE 兜底）。
            occupied = connection.execute(
                SELECT_THREAD_CATALOG_CHILD_ROW,
                (child_thread_id,),
            ).fetchone()
            if occupied is not None:
                raise RuntimeError(
                    "thread creation publish 失败：child thread 已存在于 "
                    f"thread catalog（fail closed）: "
                    f"thread_id={child_thread_id!r}"
                )
            # 唯一可见性提交点：thread_catalog child row 插入 + record
            # → published 同一事务；record 状态变化本身不构成可见性。
            connection.execute(
                "INSERT INTO thread_catalog (thread_id, kind, created_at) "
                "VALUES (?, 'child', ?)",
                (child_thread_id, child_created_at_text),
            )
            connection.execute(
                "UPDATE thread_creation_records "
                "SET state = 'published', record_updated_at = ? "
                "WHERE thread_creation_idempotency_key = ?",
                (datetime.now(UTC).isoformat(), idempotency_key),
            )
            # owner binding 字段槽随发布原子建立（2.1）：locator 冻结、
            # 初始 prefix epoch=1（reason=initial）；后续 epoch/ToolSet/
            # activation 等 owner 事实由各 domain owner 经 typed 更新
            # 方法推进，本表不构成第二 writer。
            self._insert_thread_owner_binding_row(
                connection,
                thread_id=child_thread_id,
                final_relative_locator=final_relative_locator,
                created_at=datetime.now(UTC).isoformat(),
                updated_at=datetime.now(UTC).isoformat(),
            )
            if row["collaboration_precondition_revision"] is not None:
                # ledger 与 thread catalog 原子可见性：member 在同一事务内
                # registering → published 并回填 child_thread_id。
                connection.execute(
                    "UPDATE collaboration_members "
                    "SET state = 'published', child_thread_id = ?, "
                    "updated_at = ? WHERE delegation_id = ?",
                    (
                        child_thread_id,
                        datetime.now(UTC).isoformat(),
                        str(row["delegation_id"]),
                    ),
                )
            updated = self._fetch_thread_creation_record(
                connection, idempotency_key
            )
            if updated is None:
                # 防御性兜底：同事务内更新后必然可见。
                raise RuntimeError(
                    "thread creation record 发布后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._thread_creation_record_from_row(updated)

    def abort_thread_creation_record(
        self,
        idempotency_key: str,
        reason: str,
    ) -> ThreadCreationRecord:
        """终结 ThreadCreationRecord：preparing → aborted（记 reason）。

        ``published`` 不可撤销（RuntimeError，8.5-A：CAS 失败不发布、
        已发布不回退）；已 aborted 幂等返回既有 record（不覆盖原
        abort_reason）。
        """
        validate_thread_creation_key(idempotency_key)
        if not isinstance(reason, str):
            raise TypeError(f"abort reason 必须是字符串: {reason!r}")
        if not reason:
            raise ValueError("abort reason 不能为空")
        with self._write_transaction() as connection:
            row = self._fetch_thread_creation_record(connection, idempotency_key)
            if row is None:
                raise KeyError(
                    f"thread creation record 不存在: key={idempotency_key!r}"
                )
            state = str(row["state"])
            if state == "published":
                raise RuntimeError(
                    "thread creation record 已发布，不可撤销: "
                    f"key={idempotency_key!r}"
                )
            if state == "aborted":
                return self._thread_creation_record_from_row(row)
            connection.execute(
                "UPDATE thread_creation_records "
                "SET state = 'aborted', abort_reason = ?, record_updated_at = ? "
                "WHERE thread_creation_idempotency_key = ?",
                (reason, datetime.now(UTC).isoformat(), idempotency_key),
            )
            if (
                row["delegation_id"] is not None
                and row["collaboration_precondition_revision"] is not None
            ):
                # 同 delegation 不换绑：abort 在同一事务内定点取消 member，
                # 重试必须换新 delegation（register 对 cancelled fail closed）。
                connection.execute(
                    "UPDATE collaboration_members "
                    "SET state = 'cancelled', updated_at = ? "
                    "WHERE delegation_id = ? AND state = 'registering'",
                    (datetime.now(UTC).isoformat(), str(row["delegation_id"])),
                )
            updated = self._fetch_thread_creation_record(
                connection, idempotency_key
            )
            if updated is None:
                # 防御性兜底：同事务内更新后必然可见。
                raise RuntimeError(
                    "thread creation record abort 后不可见（事务异常）: "
                    f"key={idempotency_key!r}"
                )
            return self._thread_creation_record_from_row(updated)

    def mark_thread_creation_published(
        self,
        idempotency_key: str,
    ) -> ThreadCreationRecord:
        """published 终态的幂等校验返回（发布后/terminal 响应前崩溃恢复）。

        - record ``published`` → 复验 thread_catalog child row 仍存在
          （唯一可见性提交点的产物），随后幂等返回既有 record；
        - record ``preparing`` → ``RuntimeError``（必须经
          :meth:`publish_thread_creation_record` 推进，不得绕过 CAS）；
        - record ``aborted`` → ``RuntimeError``；
        - record 缺失 → ``KeyError``。
        """
        validate_thread_creation_key(idempotency_key)
        self._ensure_open()
        row = self._fetch_thread_creation_record(self._connection, idempotency_key)
        if row is None:
            raise KeyError(
                f"thread creation record 不存在: key={idempotency_key!r}"
            )
        state = str(row["state"])
        if state == "preparing":
            raise RuntimeError(
                "thread creation record 仍 preparing，published 终态必须经 "
                f"publish_thread_creation_record 推进: key={idempotency_key!r}"
            )
        if state == "aborted":
            raise RuntimeError(
                "thread creation record 已中止，不是 published 终态: "
                f"key={idempotency_key!r}, abort_reason={row['abort_reason']!r}"
            )
        visible = self._connection.execute(
            SELECT_THREAD_CATALOG_CHILD_ROW,
            (str(row["child_thread_id"]),),
        ).fetchone()
        if visible is None:
            raise RuntimeError(
                "thread creation record 已 published 但 thread_catalog child "
                "row 缺失（唯一可见性提交点产物被外部改动，fail closed）: "
                f"key={idempotency_key!r}, "
                f"child_thread_id={row['child_thread_id']!r}"
            )
        return self._thread_creation_record_from_row(row)
