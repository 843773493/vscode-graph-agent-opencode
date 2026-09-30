"""config_source_journal 事件账本与 config_source_owner generation 水位方法族。

``WorkspaceConfigSourceJournalMixin`` 只承载本族方法与行投影常量，由
``WorkspaceConfigSourceMixin`` 组合装配；依赖宿主提供的 ``_database``。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import cast

from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigSourceJournalRecord,
)

__all__ = ["WorkspaceConfigSourceJournalMixin"]


# config_source_journal 的完整行投影：单事件回读（按 event_id / 按 generation）
# 与按域分页读取三处共用同一列清单，新增列时只需改这里。
_JOURNAL_SELECT = """
SELECT source_key, source_generation, source_event_id, vrn,
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
    source_key, source_generation, source_event_id, vrn,
    presence, layer_revision, layer_digest, previous_digest,
    origin, fanout_id, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class WorkspaceConfigSourceJournalMixin:
    @staticmethod
    def _journal_from_row(
        row: sqlite3.Row | tuple[object, ...],
    ) -> ConfigSourceJournalRecord:
        """config_source_journal 行投影的唯一实现（三处读取共用）。"""

        return ConfigSourceJournalRecord(
            source_key=str(row[0]),
            source_generation=int(row[1]),
            source_event_id=str(row[2]),
            vrn=str(row[3]) if row[3] is not None else None,
            presence=cast(str, row[4]),
            layer_revision=int(row[5]),
            layer_digest=str(row[6]) if row[6] is not None else None,
            previous_digest=str(row[7]) if row[7] is not None else None,
            origin=str(row[8]),
            fanout_id=str(row[9]),
            created_at=datetime.fromisoformat(str(row[10])),
        )

    @staticmethod
    def _append_config_source_journal_in_connection(
        connection: sqlite3.Connection,
        *,
        source_key: str,
        source_event_id: str,
        vrn: str | None,
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
                vrn,
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
        vrn: str | None,
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
                    vrn,
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
