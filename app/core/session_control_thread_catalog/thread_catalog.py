"""per-session session-control.sqlite 的 thread catalog 与 lifecycle fence owner。

本模块承载单一垂直链路的唯一实现：

- thread_catalog 的 main/child 权威指针（main row 唯一、child row 只由发布
  事务插入）；
- lifecycle_fence 的生命周期闸门（create-or-get、CAS 推进、读取）；
- 已发布 child thread 的冻结 locator 解析（thread_catalog 与
  thread_creation_records 交叉校验，任何不一致 fail closed）；
- ThreadCatalogMixin._upgrade_thread_catalog_kind_v1_to_v2 承载
  thread_catalog 的 v1 到 v2 schema 加法升级。

ThreadCatalogMixin 由 app.core.session_control_store.SessionControlStore 继承
装配；本模块不感知其余控制库职责（creation record / execution intent /
operation lease / owner binding / 跨 Session 通信账本）。错误分类沿用
session_control_store 约定：KeyError 目标行缺失、RuntimeError 库被外部改动或
语义冲突、ValueError 输入形态非法、TypeError 输入类型错误。

注意：会话位置与父子组织的唯一权威来源仍是工作区级
.boxteam/navigation/session-catalog-index.json（session_catalog_store），本模块
只保存与其匹配的 per-session main thread 指针，不维护第二套会话层级。
"""

from __future__ import annotations

import calendar
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.core.session_catalog_store import validate_thread_id

__all__ = [
    "FENCE_ROW_ID",
    "LIFECYCLE_FENCE_TABLE_DDL",
    "THREAD_CATALOG_TABLE_DDL",
    "ChildThreadRow",
    "ThreadCatalogMixin",
    "validate_thread_relative_locator",
]


FENCE_ROW_ID = 1


# thread_catalog（8.5-A 升级为 v2 形态）：kind CHECK 由 ``('main')`` 扩为
# ``('main', 'child')``。SQLite 不支持原地修改 CHECK 约束，v1 库由
# ``_initialize`` 在单事务内以「建临时新表→拷贝→校验→删旧→改名」原地
# 升级（见 :meth:`_upgrade_thread_catalog_kind_v1_to_v2`）。
THREAD_CATALOG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS thread_catalog (
    thread_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('main', 'child')),
    created_at TEXT NOT NULL
)
"""


# v1→v2 升级临时表（普通 CREATE，不带 IF NOT EXISTS；升级完成后 RENAME
# 回 ``thread_catalog``，任何失败随 ``_initialize`` 事务整体回滚）。
_THREAD_CATALOG_KIND_UPGRADE_TABLE_DDL = """
CREATE TABLE thread_catalog_kind_upgrade (
    thread_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('main', 'child')),
    created_at TEXT NOT NULL
)
"""


LIFECYCLE_FENCE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS lifecycle_fence (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL CHECK (state IN ('active', 'deleting')),
    generation INTEGER NOT NULL
)
"""


_FENCE_STATES = ("active", "deleting")


# thread 最终 relative locator（相对 owner session 目录）：
# ``threads/YYYY/MM/DD/{thread_id}``，日期为 child UTC 创建日。
_THREAD_RELATIVE_LOCATOR_PATTERN = re.compile(
    r"^threads/(\d{4})/(\d{2})/(\d{2})/(thr_[0-9a-f]{32})$"
)


def validate_thread_relative_locator(value: str) -> None:
    """校验 thread 最终 relative locator（相对 owner session 目录）。

    形态 ``threads/YYYY/MM/DD/{thread_id}``：与
    ``validate_storage_relative_locator`` 同口径——YYYY 4 位数字、MM
    01-12、DD 按 ``calendar.monthrange`` 对应月份合法（含闰年），叶名
    thread_id 过完整验证器；child thread 按自身不可变 UTC ``created_at``
    分桶（design.md §9），不额外加 hash shard。
    """
    if not isinstance(value, str):
        raise TypeError(f"thread relative locator 必须是字符串: {value!r}")
    match = _THREAD_RELATIVE_LOCATOR_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"thread relative locator 形态非法: {value!r}")
    year_text, month_text, day_text, thread_id = match.groups()
    validate_thread_id(thread_id)
    month = int(month_text)
    if not 1 <= month <= 12:
        raise ValueError(f"thread relative locator 月份非法: {value!r}")
    day = int(day_text)
    _, last_day = calendar.monthrange(int(year_text), month)
    if not 1 <= day <= last_day:
        raise ValueError(f"thread relative locator 日期非法: {value!r}")


@dataclass(frozen=True, slots=True)
class ChildThreadRow:
    """thread_catalog child row 的只读投影（8.5-B API 投影用）。"""

    thread_id: str
    created_at: str


class ThreadCatalogMixin:
    """SessionControlStore 的 thread catalog 与 lifecycle fence 方法族。

    依赖宿主类提供 database_path、_connection 与 _ensure_open()。
    """

    def _upgrade_thread_catalog_kind_v1_to_v2(self) -> None:
        """v1→v2 加法升级：``thread_catalog`` kind CHECK 原地重建。

        SQLite 不支持修改既有列的 CHECK 约束，按任务书 §2.1-A 定死的
        方式一在单事务内执行「``CREATE`` 临时新表 → ``INSERT…SELECT``
        拷贝全部行 → 行数与 kind 合法性校验 → ``DROP`` 旧表 →
        ``RENAME`` 回原名」；拷贝前后行数必须相等（既有 main row 数据
        零丢失），任何失败随 ``_initialize`` 的事务整体回滚（
        ``user_version`` 保持 1，库可原样重开，fail closed）。
        """
        self._connection.execute(_THREAD_CATALOG_KIND_UPGRADE_TABLE_DDL)
        before = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM thread_catalog"
            ).fetchone()[0]
        )
        self._connection.execute(
            "INSERT INTO thread_catalog_kind_upgrade "
            "(thread_id, kind, created_at) "
            "SELECT thread_id, kind, created_at FROM thread_catalog"
        )
        after = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM thread_catalog_kind_upgrade"
            ).fetchone()[0]
        )
        if before != after:
            raise RuntimeError(
                "thread_catalog v1→v2 升级拷贝行数不一致（数据零丢失保证"
                f"被破坏，事务将回滚）: path={self.database_path}, "
                f"before={before}, after={after}"
            )
        invalid = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM thread_catalog_kind_upgrade "
                "WHERE kind NOT IN ('main', 'child')"
            ).fetchone()[0]
        )
        if invalid != 0:
            raise RuntimeError(
                "thread_catalog v1→v2 升级发现非法 kind 行（库被外部改动，"
                f"fail closed）: path={self.database_path}, invalid={invalid}"
            )
        self._connection.execute("DROP TABLE thread_catalog")
        self._connection.execute(
            "ALTER TABLE thread_catalog_kind_upgrade RENAME TO thread_catalog"
        )

    def initialize_main_thread(self, thread_id: str, created_at: datetime) -> None:
        """create-or-get 唯一 main row：已存在且一致 → 幂等；不一致 → fail closed。

        ``thread_catalog`` 只允许 ``kind='main'``（CHECK 冻结）；main row
        的 thread_id 必须与 workspace catalog 冻结的 ``main_thread_id``
        一致（由 :meth:`verify_matches_catalog_main_thread` 复验），本方法
        保证同库内不会出现第二个 thread_id 的 main row。
        """
        validate_thread_id(thread_id)
        if not isinstance(created_at, datetime):
            raise TypeError(f"created_at 必须是 datetime: {created_at!r}")
        if created_at.tzinfo is None:
            raise ValueError(f"created_at 必须带时区: {created_at!r}")
        created_at_text = created_at.isoformat()
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            # R20：thread_catalog 现同时承载 child row（8.5-A），main row
            # 判定一律按 kind='main' 过滤——child row 的存在不影响 main
            # row 的 create-or-get 语义（唯一 main row 语义不变）。
            rows = self._connection.execute(
                "SELECT thread_id, kind, created_at FROM thread_catalog "
                "WHERE kind = 'main'"
            ).fetchall()
            if not rows:
                self._connection.execute(
                    "INSERT INTO thread_catalog (thread_id, kind, created_at) "
                    "VALUES (?, 'main', ?)",
                    (thread_id, created_at_text),
                )
            else:
                if len(rows) > 1:
                    raise RuntimeError(
                        "session control 出现多个 main row（库被外部改动，"
                        f"fail closed）: path={self.database_path}, "
                        f"rows={[str(row['thread_id']) for row in rows]}"
                    )
                existing = rows[0]
                if (
                    str(existing["thread_id"]) != thread_id
                    or str(existing["kind"]) != "main"
                ):
                    raise RuntimeError(
                        "main row 已存在且与初始化参数不一致（拒绝覆盖，"
                        f"fail closed）: path={self.database_path}, "
                        f"existing_thread_id={existing['thread_id']!r}, "
                        f"existing_kind={existing['kind']!r}, "
                        f"requested_thread_id={thread_id!r}"
                    )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def initialize_fence(self, state: str = "active", generation: int = 1) -> None:
        """create-or-get 单行 fence（id=1）：已存在且一致 → no-op；不一致 → fail closed。"""
        if state not in _FENCE_STATES:
            raise ValueError(f"fence 状态非法: {state!r}")
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise TypeError(f"fence generation 必须是整数: {generation!r}")
        if generation < 0:
            raise ValueError(f"fence generation 不能为负: {generation!r}")
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT state, generation FROM lifecycle_fence WHERE id = ?",
                (FENCE_ROW_ID,),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO lifecycle_fence (id, state, generation) "
                    "VALUES (?, ?, ?)",
                    (FENCE_ROW_ID, state, generation),
                )
            elif (
                str(row["state"]) != state
                or int(row["generation"]) != generation
            ):
                raise RuntimeError(
                    "fence 已存在且与初始化参数不一致（拒绝覆盖，fail closed）: "
                    f"path={self.database_path}, "
                    f"existing_state={row['state']!r}, "
                    f"existing_generation={row['generation']!r}, "
                    f"requested_state={state!r}, "
                    f"requested_generation={generation!r}"
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def cas_fence_transition(self, expected_generation: int, new_state: str) -> bool:
        """CAS 推进 fence 状态（语义对齐 R10
        ``SessionLifecycleFence.cas_transition``，8.1-B 删除流 drain 使用）。

        - ``active → deleting`` 且 ``generation`` 匹配 → 推进为
          ``deleting`` 并 generation+1，返回 ``True``；
        - ``deleting → active`` 恒拒绝（不可复活）返回 ``False``；
        - generation 不匹配返回 ``False``（fence 保持不变）；
        - 非法 ``new_state`` 抛 ``ValueError``；
        - fence row 缺失抛 ``KeyError``（fail closed，与 :meth:`get_fence`
          一致——缺失的 fence 不是可 CAS 的 active fence）。

        事务纪律（R14 审查 M1 修复）：先以只读查询判态，False / KeyError
        失败路径在开启任何写事务前返回或抛出，不遗留打开事务、不持有
        RESERVED 写锁；仅确定要推进时才 ``BEGIN IMMEDIATE``，并在写事务
        内复核判态后 UPDATE——复核不匹配时 ROLLBACK 后返回 ``False``，
        推进判定与写入始终同处一个写事务（CAS 原子性不受先读后写影响）。
        """
        if new_state not in _FENCE_STATES:
            raise ValueError(f"fence 目标状态非法: {new_state!r}")
        if not isinstance(expected_generation, int) or isinstance(
            expected_generation, bool
        ):
            raise TypeError(
                f"expected_generation 必须是整数: {expected_generation!r}"
            )
        self._ensure_open()
        # 只读判态：连接为 isolation_level=None，裸 SELECT 不开启事务，
        # 失败路径因此天然无事务可泄漏（同实例可直接重试 CAS）。
        row = self._connection.execute(
            "SELECT state, generation FROM lifecycle_fence WHERE id = ?",
            (FENCE_ROW_ID,),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"session control 缺少 lifecycle fence row: "
                f"path={self.database_path}"
            )
        state = str(row["state"])
        generation = int(row["generation"])
        # active→deleting 是唯一合法转移（deleting→active 不可复活）。
        if state != "active" or new_state != "deleting":
            return False
        if generation != expected_generation:
            return False
        # 确定要推进才开写事务；事务内复核，防止只读判态与 BEGIN
        # IMMEDIATE 之间 fence 被其他写者改变导致误推进。
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT state, generation FROM lifecycle_fence WHERE id = ?",
                (FENCE_ROW_ID,),
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"session control 缺少 lifecycle fence row: "
                    f"path={self.database_path}"
                )
            state = str(row["state"])
            generation = int(row["generation"])
            proceed = (
                state == "active"
                and new_state == "deleting"
                and generation == expected_generation
            )
            if proceed:
                self._connection.execute(
                    "UPDATE lifecycle_fence SET state = ?, generation = ? "
                    "WHERE id = ?",
                    (new_state, generation + 1, FENCE_ROW_ID),
                )
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        if not proceed:
            # 复核不匹配（并发穿插）：整体回滚，fence 保持不变。
            self._connection.execute("ROLLBACK")
            return False
        self._connection.execute("COMMIT")
        return True

    def get_main_thread(self) -> sqlite3.Row:
        """返回唯一 main row；缺失抛 KeyError，多行抛 RuntimeError（库损坏）。

        R20：按 ``kind='main'`` 过滤——thread_catalog 现同时承载 child
        row（8.5-A），child row 的存在不改变 main row 读取语义。
        """
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT thread_id, kind, created_at FROM thread_catalog "
            "WHERE kind = 'main'"
        ).fetchall()
        if not rows:
            raise KeyError(
                f"session control 缺少 main row: path={self.database_path}"
            )
        if len(rows) > 1:
            raise RuntimeError(
                "session control 出现多个 main row（库被外部改动，fail closed）: "
                f"path={self.database_path}, "
                f"rows={[str(row['thread_id']) for row in rows]}"
            )
        return rows[0]

    def get_published_child_thread_locator(self, thread_id: str) -> str:
        """返回已发布 child thread 的冻结 locator。

        ``thread_catalog`` 只提供 child 的可见性提交点，物理定位必须继续
        读取同一发布事务冻结的 ``thread_creation_records``。缺少任一行、
        record 未发布或两者的创建时间不一致，都表示控制库被外部改动，
        解析器必须 fail closed。
        """
        validate_thread_id(thread_id)
        self._ensure_open()
        row = self._connection.execute(
            "SELECT tc.kind, tc.created_at, "
            "tcr.state, tcr.final_relative_locator, tcr.child_created_at "
            "FROM thread_catalog AS tc "
            "LEFT JOIN thread_creation_records AS tcr "
            "ON tcr.child_thread_id = tc.thread_id "
            "WHERE tc.thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise KeyError(
                "thread catalog 不存在目标 child thread: "
                f"thread_id={thread_id!r}, path={self.database_path}"
            )
        if str(row["kind"]) != "child":
            raise RuntimeError(
                "目标 thread catalog 行不是 child（fail closed）: "
                f"thread_id={thread_id!r}, kind={row['kind']!r}"
            )
        if row["state"] is None:
            raise RuntimeError(
                "child thread 缺少 thread creation record（库被外部改动，"
                f"fail closed）: thread_id={thread_id!r}, "
                f"path={self.database_path}"
            )
        if str(row["state"]) != "published":
            raise RuntimeError(
                "child thread creation record 尚未 published（fail closed）: "
                f"thread_id={thread_id!r}, state={row['state']!r}"
            )
        locator = str(row["final_relative_locator"])
        validate_thread_relative_locator(locator)
        if (
            str(row["created_at"]) != str(row["child_created_at"])
            or not locator.endswith(f"/{thread_id}")
        ):
            raise RuntimeError(
                "child thread catalog 与 creation record 不一致（fail closed）: "
                f"thread_id={thread_id!r}, locator={locator!r}"
            )
        return locator

    def get_fence(self) -> tuple[str, int]:
        """返回 (state, generation)；缺失抛 KeyError。"""
        self._ensure_open()
        row = self._connection.execute(
            "SELECT state, generation FROM lifecycle_fence WHERE id = ?",
            (FENCE_ROW_ID,),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"session control 缺少 lifecycle fence row: path={self.database_path}"
            )
        return str(row["state"]), int(row["generation"])

    def verify_matches_catalog_main_thread(self, main_thread_id: str) -> None:
        """校验 main row thread_id == workspace catalog 冻结的 main_thread_id。

        design.md §9：workspace catalog 的 ``main_thread_id`` 是唯一对外
        权威指针，session-control 只保存与其匹配且 kind 唯一的 main row，
        不维护第二个可独立修改的 main pointer；不符即 fail closed。
        """
        validate_thread_id(main_thread_id)
        row = self.get_main_thread()
        actual = str(row["thread_id"])
        if actual != main_thread_id:
            raise RuntimeError(
                "session control main row 与 workspace catalog 冻结的 "
                f"main_thread_id 不一致（fail closed）: path={self.database_path}, "
                f"control_thread_id={actual!r}, catalog_main_thread_id={main_thread_id!r}"
            )

    def get_thread_catalog_revision(self) -> int:
        """返回 catalog precondition revision 的本轮度量：thread_catalog 行数。

        本轮 thread_catalog 只有插入型变更（行数单调不减），行数等价于
        单调 revision；8.5 引入删除/移动时应在同库补建真正的 revision
        计数（TODO(8.5)）。
        """
        self._ensure_open()
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM thread_catalog"
            ).fetchone()[0]
        )

    def list_child_thread_rows(self) -> tuple[ChildThreadRow, ...]:
        """只读返回 thread_catalog 全部 child row（API 投影用）。"""
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT thread_id, created_at FROM thread_catalog "
            "WHERE kind = 'child' ORDER BY created_at, thread_id"
        ).fetchall()
        return tuple(
            ChildThreadRow(
                thread_id=str(row["thread_id"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        )
