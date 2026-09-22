"""Workspace 状态库 config source layer、source journal 与 fan-out 账本的唯一实现。

本模块承载单一垂直链路的唯一实现：

- ``config_source_layers`` 权威 layer 行（去重同步、CAS、上一版快照与备份路径）；
- ``config_source_journal`` 事件账本与 ``config_source_owner`` generation 水位；
- ``config_source_fanout`` 逐 generation/逐 workspace 的导入结果账本；
- ``source_generation_high_water_mark`` 只读水位查询。

``WorkspaceConfigSourceMixin`` 由
``app.services.infrastructure.workspace_state_store.WorkspaceStateStore`` 继承
装配；宿主负责 ``_WORKSPACE_MIGRATIONS`` 中本族三张表的 DDL 与迁移序号，本模块
只承载读写方法族，宿主提供 ``_database``。宿主 ``__init__`` 之外的其它方法族不调用
本模块私有辅助。

错误分类沿用 workspace_state_store 约定：``ValueError`` 输入形态非法、
``ConfigConflictError`` revision/digest/generation CAS 冲突、``RuntimeError``
事务后读取失败。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import cast

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigSourceJournalRecord,
    ConfigSourceLayerRecord,
    dump_json,
    load_json_object,
    prepare_config_for_persistence,
)

__all__ = [
    "WorkspaceConfigSourceMixin",
]


# config_source_journal 的完整行投影：单事件回读（按 event_id / 按 generation）
# 与按域分页读取三处共用同一列清单，新增列时只需改这里。
_JOURNAL_SELECT = """
SELECT source_key, source_generation, source_event_id, source_path,
       presence, layer_revision, layer_digest, previous_digest,
       origin, fanout_id, created_at
FROM config_source_journal
"""

# 同一上行追加路径（事务内辅助与公开入口）共用的两条语句：读最新 generation
# 做去重/CAS 判定，以及插入新 generation。
_LATEST_JOURNAL_SELECT = """
SELECT source_generation, presence, layer_digest
FROM config_source_journal
WHERE source_key = ?
ORDER BY source_generation DESC
LIMIT 1
"""

_JOURNAL_INSERT = """
INSERT INTO config_source_journal(
    source_key, source_generation, source_event_id, source_path,
    presence, layer_revision, layer_digest, previous_digest,
    origin, fanout_id, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

class WorkspaceConfigSourceMixin:
    @staticmethod
    def _journal_from_row(
        row: sqlite3.Row | tuple[object, ...],
    ) -> ConfigSourceJournalRecord:
        """config_source_journal 行投影的唯一实现（三处读取共用）。"""

        return ConfigSourceJournalRecord(
            source_key=str(row[0]),
            source_generation=int(row[1]),
            source_event_id=str(row[2]),
            source_path=str(row[3]),
            presence=cast(str, row[4]),
            layer_revision=int(row[5]),
            layer_digest=str(row[6]) if row[6] is not None else None,
            previous_digest=str(row[7]) if row[7] is not None else None,
            origin=str(row[8]),
            fanout_id=str(row[9]),
            created_at=datetime.fromisoformat(str(row[10])),
        )

    def get_source_layer(self, config_key: str) -> ConfigSourceLayerRecord | None:
        connection = self._database.connection()
        try:
            row = connection.execute(
                """
                SELECT config_key, source_path, presence, config_version,
                       payload_json, layer_revision, layer_digest,
                       source_generation, previous_digest, updated_at,
                       previous_payload_json, backup_path
                FROM config_source_layers
                WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if row is None:
                return None
            payload = (
                load_json_object(str(row[4]), field="source layer payload")
                if row[4] is not None
                else None
            )
            return ConfigSourceLayerRecord(
                config_key=str(row[0]),
                source_path=str(row[1]),
                presence=str(row[2]),  # type: ignore[arg-type]
                config_version=int(row[3]),
                payload=payload,
                layer_revision=int(row[5]),
                layer_digest=str(row[6]) if row[6] is not None else None,
                source_generation=int(row[7]),
                previous_digest=str(row[8]) if row[8] is not None else None,
                updated_at=datetime.fromisoformat(str(row[9])),
                previous_payload=(
                    load_json_object(
                        str(row[10]), field="source layer previous payload"
                    )
                    if row[10] is not None
                    else None
                ),
                backup_path=str(row[11]) if row[11] is not None else None,
            )
        finally:
            connection.close()

    def sync_config_source(
        self,
        *,
        config_key: str,
        source_path: Path,
        config_version: int,
        presence: str,
        payload: dict[str, object] | None,
        layer_digest: str | None,
        expected_layer_revision: int | None = None,
        expected_layer_digest: str | None = None,
        backup_path: Path | None = None,
        journal_origin: str | None = None,
        source_event_id: str | None = None,
        fanout_id: str | None = None,
        config_domain: str | None = None,
        expected_active_revision: int | None = None,
        expected_active_digest: str | None = None,
        enforce_layer_cas: bool = False,
        enforce_active_cas: bool = False,
    ) -> ConfigSourceLayerRecord:
        if presence not in {"present", "absent"}:
            raise ValueError(f"未知 source layer presence: {presence}")
        if presence == "present" and payload is None:
            raise ValueError("present source layer 必须有 payload")
        if presence == "absent" and payload is not None:
            raise ValueError("absent source layer 的 payload 必须为空")
        if (expected_active_revision is None) != (expected_active_digest is None):
            raise ValueError("active CAS 必须同时提供 revision 和 digest")
        if (enforce_active_cas or expected_active_revision is not None) and not config_domain:
            raise ValueError("active CAS 必须声明 config_domain")

        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if enforce_active_cas or expected_active_revision is not None:
                active = connection.execute(
                    """
                    SELECT active_revision, effective_digest
                    FROM config_active_snapshot
                    WHERE config_domain = ?
                    """,
                    (config_domain,),
                ).fetchone()
                current_active_revision = int(active[0]) if active is not None else None
                current_active_digest = str(active[1]) if active is not None else None
                if (
                    current_active_revision != expected_active_revision
                    or current_active_digest != expected_active_digest
                ):
                    raise ConfigConflictError(
                        "active snapshot CAS 冲突: "
                        f"domain={config_domain}, "
                        f"current_revision={current_active_revision}, "
                        f"current_digest={current_active_digest}"
                    )
            row = connection.execute(
                """
                SELECT source_path, presence, layer_revision, layer_digest,
                       source_generation, payload_json
                FROM config_source_layers
                WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if row is None:
                if expected_layer_revision is not None or expected_layer_digest is not None:
                    raise ConfigConflictError(
                        f"source layer 不存在但调用方声明了旧基线: key={config_key}"
                    )
                layer_revision = 1
                source_generation = 1
                previous_digest = None
                previous_payload_json = None
            else:
                current_path = str(row[0])
                current_presence = str(row[1])
                current_revision = int(row[2])
                current_digest = str(row[3]) if row[3] is not None else None
                if (
                    (enforce_layer_cas or expected_layer_revision is not None)
                    and current_revision != expected_layer_revision
                ) or (
                    (enforce_layer_cas or expected_layer_digest is not None)
                    and current_digest != expected_layer_digest
                ):
                    raise ConfigConflictError(
                        "source layer CAS 冲突: "
                        f"key={config_key}, current_revision={current_revision}, "
                        f"current_digest={current_digest}"
                    )
                if (
                    current_path == str(source_path.expanduser().resolve())
                    and current_presence == presence
                    and current_digest == layer_digest
                ):
                    sanitized_json = (
                        dump_json(prepare_config_for_persistence(payload))
                        if payload is not None
                        else None
                    )
                    now = utc_now_text()
                    connection.execute(
                        """
                        UPDATE config_source_layers
                        SET config_version = ?, payload_json = ?, updated_at = ?
                        WHERE config_key = ?
                        """,
                        (config_version, sanitized_json, now, config_key),
                    )
                    if presence == "present":
                        connection.execute(
                            """
                            INSERT INTO workspace_config(
                                config_key, config_version, payload_json, updated_at
                            ) VALUES (?, ?, ?, ?)
                            ON CONFLICT(config_key) DO UPDATE SET
                                config_version=excluded.config_version,
                                payload_json=excluded.payload_json,
                                updated_at=excluded.updated_at
                            """,
                            (config_key, config_version, sanitized_json, now),
                        )
                    else:
                        connection.execute(
                            "DELETE FROM workspace_config WHERE config_key = ?",
                            (config_key,),
                        )
                    if journal_origin is not None:
                        self._append_config_source_journal_in_connection(
                            connection,
                            source_key=config_key,
                            source_event_id=(
                                source_event_id
                                or f"{config_key}:layer:{current_revision}"
                            ),
                            source_path=source_path,
                            presence=presence,
                            layer_revision=current_revision,
                            layer_digest=layer_digest,
                            previous_digest=current_digest,
                            origin=journal_origin,
                            fanout_id=(
                                fanout_id
                                or f"fanout:{config_key}:event:{config_key}:layer:{current_revision}"
                            ),
                        )
                    connection.execute("COMMIT")
                    record = self.get_source_layer(config_key)
                    if record is None:
                        raise RuntimeError(f"source layer 提交后无法读取: {config_key}")
                    return record
                layer_revision = current_revision + 1
                source_generation = int(row[4]) + 1
                previous_digest = current_digest
                previous_payload_json = row[5]

            payload_json = (
                dump_json(prepare_config_for_persistence(payload))
                if payload is not None
                else None
            )
            now = utc_now_text()
            connection.execute(
                """
                INSERT INTO config_source_layers(
                    config_key, source_path, presence, config_version, payload_json,
                    layer_revision, layer_digest, source_generation, previous_digest,
                    updated_at, previous_payload_json, backup_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(config_key) DO UPDATE SET
                    source_path=excluded.source_path,
                    presence=excluded.presence,
                    config_version=excluded.config_version,
                    payload_json=excluded.payload_json,
                    layer_revision=excluded.layer_revision,
                    layer_digest=excluded.layer_digest,
                    source_generation=excluded.source_generation,
                    previous_digest=excluded.previous_digest,
                    updated_at=excluded.updated_at,
                    previous_payload_json=excluded.previous_payload_json,
                    backup_path=excluded.backup_path
                """,
                (
                    config_key,
                    str(source_path.expanduser().resolve()),
                    presence,
                    config_version,
                    payload_json,
                    layer_revision,
                    layer_digest,
                    source_generation,
                    previous_digest,
                    now,
                    previous_payload_json,
                    str(backup_path.expanduser().resolve())
                    if backup_path is not None
                    else None,
                ),
            )
            if journal_origin is not None:
                self._append_config_source_journal_in_connection(
                    connection,
                    source_key=config_key,
                    source_event_id=(
                        source_event_id or f"{config_key}:layer:{layer_revision}"
                    ),
                    source_path=source_path,
                    presence=presence,
                    layer_revision=layer_revision,
                    layer_digest=layer_digest,
                    previous_digest=previous_digest,
                    origin=journal_origin,
                    fanout_id=(
                        fanout_id
                        or f"fanout:{config_key}:event:{config_key}:layer:{layer_revision}"
                    ),
                )
            if presence == "present":
                if payload is None:
                    raise RuntimeError("present source layer payload 在事务中丢失")
                connection.execute(
                    """
                    INSERT INTO workspace_config(config_key, config_version, payload_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(config_key) DO UPDATE SET
                        config_version=excluded.config_version,
                        payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at
                    """,
                    (config_key, config_version, payload_json, now),
                )
            else:
                connection.execute(
                    "DELETE FROM workspace_config WHERE config_key = ?",
                    (config_key,),
                )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        record = self.get_source_layer(config_key)
        if record is None:
            raise RuntimeError(f"source layer 提交后无法读取: {config_key}")
        return record

    def update_source_generation(
        self,
        *,
        config_key: str,
        source_generation: int,
        expected_layer_revision: int,
        expected_layer_digest: str | None,
    ) -> ConfigSourceLayerRecord:
        """把本地 materialized layer 绑定到共享 source owner 的 generation。"""

        if source_generation < 1 or expected_layer_revision < 1:
            raise ValueError("source generation 或 layer revision 必须为正数")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT source_path, presence, config_version, payload_json,
                       layer_revision, layer_digest, source_generation,
                       previous_digest, updated_at, previous_payload_json, backup_path
                FROM config_source_layers
                WHERE config_key = ?
                """,
                (config_key,),
            ).fetchone()
            if row is None:
                raise ConfigConflictError(
                    f"source layer 不存在，无法绑定共享 generation: {config_key}"
                )
            current_revision = int(row[4])
            current_digest = str(row[5]) if row[5] is not None else None
            if (
                current_revision != expected_layer_revision
                or current_digest != expected_layer_digest
            ):
                raise ConfigConflictError(
                    "source layer generation 绑定 CAS 冲突: "
                    f"key={config_key}, revision={current_revision}, digest={current_digest}"
                )
            if int(row[6]) > source_generation:
                raise ConfigConflictError(
                    "source layer generation 不能回退: "
                    f"key={config_key}, current={row[6]}, requested={source_generation}"
                )
            connection.execute(
                """
                UPDATE config_source_layers
                SET source_generation = ?, updated_at = ?
                WHERE config_key = ?
                """,
                (source_generation, utc_now_text(), config_key),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        record = self.get_source_layer(config_key)
        if record is None:
            raise RuntimeError(f"source layer generation 绑定后无法读取: {config_key}")
        return record

    @staticmethod
    def _append_config_source_journal_in_connection(
        connection: sqlite3.Connection,
        *,
        source_key: str,
        source_event_id: str,
        source_path: Path,
        presence: str,
        layer_revision: int,
        layer_digest: str | None,
        previous_digest: str | None,
        origin: str,
        fanout_id: str,
        expected_source_generation: int | None = None,
    ) -> int:
        """在调用方事务内追加 source journal，避免 source/journal 裂脑。"""
        if presence not in {"present", "absent"}:
            raise ValueError("source journal presence 无效")
        existing = connection.execute(
            """
            SELECT source_key, source_generation, layer_revision, layer_digest,
                   presence
            FROM config_source_journal
            WHERE source_event_id = ?
            """,
            (source_event_id,),
        ).fetchone()
        if existing is not None:
            existing_digest = str(existing[3]) if existing[3] is not None else None
            if (
                str(existing[0]) != source_key
                or int(existing[2]) != layer_revision
                or existing_digest != layer_digest
                or str(existing[4]) != presence
            ):
                raise ConfigConflictError(
                    "source journal event_id 已绑定不同 source 记录"
                )
            return int(existing[1])
        latest = connection.execute(_LATEST_JOURNAL_SELECT, (source_key,)).fetchone()
        current_generation = int(latest[0]) if latest is not None else 0
        if (
            expected_source_generation is not None
            and current_generation != expected_source_generation
        ):
            raise ConfigConflictError("source journal generation CAS 冲突")
        if (
            latest is not None
            and str(latest[1]) == presence
            and (str(latest[2]) if latest[2] is not None else None) == layer_digest
        ):
            return current_generation
        generation = current_generation + 1
        owner = connection.execute(
            "SELECT next_generation FROM config_source_owner WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        if owner is not None and int(owner[0]) != generation:
            raise ConfigConflictError("source owner generation CAS 冲突")
        if owner is None:
            connection.execute(
                "INSERT INTO config_source_owner(source_key, next_generation) VALUES (?, ?)",
                (source_key, generation + 1),
            )
        else:
            connection.execute(
                "UPDATE config_source_owner SET next_generation = ? WHERE source_key = ?",
                (generation + 1, source_key),
            )
        connection.execute(
            _JOURNAL_INSERT,
            (
                source_key,
                generation,
                source_event_id,
                str(source_path.expanduser().resolve()),
                presence,
                layer_revision,
                layer_digest,
                previous_digest,
                origin,
                fanout_id,
                utc_now_text(),
            ),
        )
        return generation

    def append_config_source_journal(
        self,
        *,
        source_key: str,
        source_event_id: str,
        source_path: Path,
        presence: str,
        layer_revision: int,
        layer_digest: str | None,
        previous_digest: str | None,
        origin: str,
        fanout_id: str,
        expected_source_generation: int | None = None,
    ) -> ConfigSourceJournalRecord:
        if presence not in {"present", "absent"}:
            raise ValueError(f"未知 source journal presence: {presence}")
        if not source_key or not source_event_id or not fanout_id:
            raise ValueError("source journal 身份不能为空")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                _JOURNAL_SELECT
                + """
                WHERE source_event_id = ?
                """,
                (source_event_id,),
            ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                return self._journal_from_row(existing)
            latest = connection.execute(
                _LATEST_JOURNAL_SELECT, (source_key,)
            ).fetchone()
            current_generation = int(latest[0]) if latest is not None else 0
            if (
                expected_source_generation is not None
                and current_generation != expected_source_generation
            ):
                raise ConfigConflictError(
                    "source journal generation CAS 冲突: "
                    f"source_key={source_key}, current={current_generation}, "
                    f"expected={expected_source_generation}"
                )
            if (
                latest is not None
                and str(latest[1]) == presence
                and (
                    str(latest[2]) if latest[2] is not None else None
                )
                == layer_digest
            ):
                existing = connection.execute(
                    _JOURNAL_SELECT
                    + """
                    WHERE source_key = ? AND source_generation = ?
                    """,
                    (source_key, current_generation),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("source journal 去重后无法读取最新记录")
                connection.execute("COMMIT")
                return self._journal_from_row(existing)
            generation = current_generation + 1
            owner = connection.execute(
                "SELECT next_generation FROM config_source_owner WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            if owner is not None and int(owner[0]) != generation:
                raise ConfigConflictError(
                    "source owner generation 已被其他提交推进: "
                    f"source_key={source_key}, next={owner[0]}, expected={generation}"
                )
            if owner is None:
                connection.execute(
                    "INSERT INTO config_source_owner(source_key, next_generation) VALUES (?, ?)",
                    (source_key, generation + 1),
                )
            else:
                connection.execute(
                    "UPDATE config_source_owner SET next_generation = ? WHERE source_key = ?",
                    (generation + 1, source_key),
                )
            now = utc_now_text()
            connection.execute(
                _JOURNAL_INSERT,
                (
                    source_key,
                    generation,
                    source_event_id,
                    str(source_path.expanduser().resolve()),
                    presence,
                    layer_revision,
                    layer_digest,
                    previous_digest,
                    origin,
                    fanout_id,
                    now,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        records = self.list_config_source_journal(
            source_key=source_key,
            after_generation=generation - 1,
            limit=1,
        )
        if not records:
            raise RuntimeError("source journal 提交后无法读取")
        return records[0]

    def list_config_source_journal(
        self,
        *,
        source_key: str,
        after_generation: int = 0,
        limit: int = 100,
    ) -> tuple[ConfigSourceJournalRecord, ...]:
        if after_generation < 0 or limit < 1 or limit > 2000:
            raise ValueError("source journal 分页参数无效")
        connection = self._database.connection()
        try:
            rows = connection.execute(
                _JOURNAL_SELECT
                + """
                WHERE source_key = ? AND source_generation > ?
                ORDER BY source_generation ASC
                LIMIT ?
                """,
                (source_key, after_generation, limit),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._journal_from_row(row) for row in rows)

    def source_generation_high_water_mark(self, *, source_key: str) -> int:
        """返回本 source 已提交的最大 journal generation；从未写入时为 0。

        ``config_source_owner.next_generation`` 与 journal 在同一事务推进，合法写入
        至少为 ``generation + 1``（首个事件后为 2，因此高水位至少为 1）。这里对两种
        不可能由软件产生的形态响亮报错，绝不返回虚假默认值：

        - owner 行缺失但 journal 已有 generation：有人绕过软件清空了水位行；
        - owner 行存在但 ``next_generation < 1``：水位本身已损坏（会算出负值高水位，
          让 CAS 永远失配）。
        """

        connection = self._database.connection()
        try:
            row = connection.execute(
                "SELECT next_generation FROM config_source_owner WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            if row is None:
                journal_row = connection.execute(
                    "SELECT MAX(source_generation) FROM config_source_journal "
                    "WHERE source_key = ?",
                    (source_key,),
                ).fetchone()
                if journal_row is not None and journal_row[0] is not None:
                    raise RuntimeError(
                        "Workspace source owner 水位缺失但 journal 已有 generation；"
                        "检测到绕过软件直接修改 Workspace 配置来源状态: "
                        f"source_key={source_key}"
                    )
                return 0
            next_generation = int(row[0])
            if next_generation < 1:
                raise RuntimeError(
                    "Workspace source owner next_generation 非法: "
                    f"source_key={source_key}, next_generation={next_generation}"
                )
        finally:
            connection.close()
        return next_generation - 1

    def record_config_source_fanout(
        self,
        *,
        source_key: str,
        source_generation: int,
        workspace_id: str,
        status: str,
        layer_revision: int | None = None,
        layer_digest: str | None = None,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        if not workspace_id or not status:
            raise ValueError("fan-out 工作区和状态不能为空")
        if source_generation < 1:
            raise ValueError("fan-out source_generation 必须为正数")
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                """
                SELECT 1 FROM config_source_journal
                WHERE source_key = ? AND source_generation = ?
                """,
                (source_key, source_generation),
            ).fetchone() is None:
                raise ConfigConflictError("fan-out 关联的 source journal 不存在")
            connection.execute(
                """
                INSERT INTO config_source_fanout(
                    source_key, source_generation, workspace_id, status,
                    layer_revision, layer_digest, result, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key, source_generation, workspace_id) DO UPDATE SET
                    status=excluded.status,
                    layer_revision=excluded.layer_revision,
                    layer_digest=excluded.layer_digest,
                    result=excluded.result,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    source_key,
                    source_generation,
                    workspace_id,
                    status,
                    layer_revision,
                    layer_digest,
                    result,
                    error,
                    utc_now_text(),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def prepare_config_source_fanout(
        self,
        *,
        source_key: str,
        workspace_id: str,
        after_generation: int = 0,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        """为停止后重新上线的 Workspace 建立逐 generation 的待导入记录。"""
        if not workspace_id.strip() or after_generation < 0:
            raise ValueError("fan-out workspace 或 high-water 无效")
        records = self.list_config_source_journal(
            source_key=source_key,
            after_generation=after_generation,
            limit=limit,
        )
        connection = self._database.connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now_text()
            for record in records:
                connection.execute(
                    """
                    INSERT INTO config_source_fanout(
                        source_key, source_generation, workspace_id, status,
                        layer_revision, layer_digest, result, error, updated_at
                    ) VALUES (?, ?, ?, 'pending', NULL, NULL, NULL, NULL, ?)
                    ON CONFLICT(source_key, source_generation, workspace_id)
                    DO NOTHING
                    """,
                    (
                        source_key,
                        record.source_generation,
                        workspace_id,
                        now,
                    ),
                )
            # 返回必须反映库中真实状态：ON CONFLICT DO NOTHING 不会把既有
            # applied/conflict 行改回 pending，此处按 workspace 回读权威状态。
            statuses = dict(
                connection.execute(
                    """
                    SELECT source_generation, status FROM config_source_fanout
                    WHERE source_key = ? AND workspace_id = ?
                    """,
                    (source_key, workspace_id),
                ).fetchall()
            )
            connection.execute("COMMIT")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return tuple(
            {
                "source_generation": record.source_generation,
                "source_event_id": record.source_event_id,
                "fanout_id": record.fanout_id,
                "status": str(statuses[record.source_generation]),
            }
            for record in records
        )

    def config_source_fanout_summary(
        self,
        *,
        source_key: str,
        source_generation: int,
        workspace_ids: tuple[str, ...],
    ) -> dict[str, object]:
        """汇总 fan-out，明确区分全部完成、进行中和 fanout_partial。"""
        if not workspace_ids or len(workspace_ids) != len(set(workspace_ids)):
            raise ValueError("fan-out workspace_ids 不能为空且不能重复")
        statuses = {
            str(item["workspace_id"]): str(item["status"])
            for item in self.list_config_source_fanout(
                source_key=source_key,
                source_generation=source_generation,
            )
        }
        missing = tuple(
            workspace_id for workspace_id in workspace_ids if workspace_id not in statuses
        )
        failed = tuple(
            workspace_id
            for workspace_id in workspace_ids
            if statuses.get(workspace_id) in {"conflict", "failed"}
        )
        pending = tuple(
            workspace_id
            for workspace_id in workspace_ids
            if workspace_id in statuses
            and statuses[workspace_id] not in {"applied", "conflict", "failed"}
        )
        if failed:
            result = "fanout_partial"
        elif missing or any(
            statuses.get(workspace_id) != "applied" for workspace_id in workspace_ids
        ):
            result = "pending"
        else:
            result = "applied"
        return {
            "source_key": source_key,
            "source_generation": source_generation,
            "result": result,
            "missing_workspace_ids": missing,
            "pending_workspace_ids": pending,
            "failed_workspace_ids": failed,
        }

    def list_config_source_fanout(
        self,
        *,
        source_key: str,
        source_generation: int,
    ) -> tuple[dict[str, object], ...]:
        connection = self._database.connection()
        try:
            rows = connection.execute(
                """
                SELECT workspace_id, status, layer_revision, layer_digest, result,
                       error, updated_at
                FROM config_source_fanout
                WHERE source_key = ? AND source_generation = ?
                ORDER BY workspace_id ASC
                """,
                (source_key, source_generation),
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            {
                "workspace_id": str(row[0]),
                "status": str(row[1]),
                "layer_revision": int(row[2]) if row[2] is not None else None,
                "layer_digest": str(row[3]) if row[3] is not None else None,
                "result": str(row[4]) if row[4] is not None else None,
                "error": str(row[5]) if row[5] is not None else None,
                "updated_at": str(row[6]),
            }
            for row in rows
        )
