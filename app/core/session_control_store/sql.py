"""per-session ``session-control.sqlite`` 各垂直链路共用的 SQL 常量。

只收敛逐字重复的同语义查询，供 creation record / publish / execution intent
各族共用；调用点仍各自决定缺失时的错误文案与分支。
"""

from __future__ import annotations

__all__ = [
    "SELECT_COLLABORATION_LEDGER_REVISION",
    "SELECT_THREAD_CATALOG_CHILD_ROW",
    "SELECT_THREAD_CATALOG_ROW_COUNT",
]

# collaboration revision 读取（get_collaboration_ledger_revision 与 publish
# 的 CAS 校验共用同一语义查询）。
SELECT_COLLABORATION_LEDGER_REVISION = (
    "SELECT revision FROM collaboration_ledger WHERE id = 1"
)

# child thread 占用预检：create record 插入、publish 可见性提交点与 initial
# execution intent 三道闸门共用同一存在性查询。
SELECT_THREAD_CATALOG_CHILD_ROW = "SELECT 1 FROM thread_catalog WHERE thread_id = ?"

# thread_catalog 行数度量（catalog precondition revision）：创建时冻结与
# publish 时 CAS 校验共用。
SELECT_THREAD_CATALOG_ROW_COUNT = "SELECT COUNT(*) FROM thread_catalog"
