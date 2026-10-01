"""journal/staging/quarantine 相关模块级常量(逐字搬迁)。"""

from __future__ import annotations

import re

from app.core.session_catalog_legacy_layout import SESSION_MANIFEST_NAME

# journal 落盘位置:maintenance_root / "session-catalog-migration" / "journal.json"。
_JOURNAL_DIRECTORY_NAME = "session-catalog-migration"
_JOURNAL_FILE_NAME = "journal.json"

# 物理迁移 staging 区:sessions_root / ".staging" / <migration_id> / {session_id}。
_STAGING_DIR_NAME = ".staging"

# quarantine 物理隔离目标:boxteam_root / "orphaned" / "session-catalog-migration" /
# {session_id}(boxteam_root = sessions_root.parent;对齐 app/core/AGENTS.md
# 「无法可靠归属的旧数据移入 .boxteam/orphaned/ 并保留可诊断信息」)。
_ORPHANED_DIR_NAME = "orphaned"

# quarantine 原因闭集与节点种类闭集(journal 恢复时逐一校验)。
_QUARANTINE_REASONS = frozenset({"illegal_id", "illegal_date", "parent_quarantined"})
_NODE_KINDS = frozenset({"folder", "session"})

# session.json 剥离键:可变导航父节点/显示名已进 SQLite catalog,物理
# manifest 不再承载(其余字段原样保留)。
_STRIP_MANIFEST_KEYS = ("title", "title_source", "parent_session_id")

# journal physical 节的 per-session 分类与状态闭集。
_CLASSIFICATIONS = frozenset({"migrate", "quarantine"})
_SESSION_PHYSICAL_STATES = frozenset(
    {"pending", "staged", "placed", "quarantine_isolated"}
)
_FOLDER_PHYSICAL_STATES = frozenset({"pending", "deleted", "quarantine_isolated"})
_CONTROL_STATES = frozenset({"pending", "initialized"})

# migration_id 形态:uuid4().hex,32 位小写 hex(staging 目录名,安全单段)。
_MIGRATION_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

# 内容清单排除项:session.json 单独记录 sha256;session-control.sqlite(+WAL
# 边车)是本机器 placed 后新建的控制库,不属于「迁移的 canonical bytes」,
# 由 _verify_session_control_rows 单独校验。
_CONTENT_MANIFEST_EXCLUDED_NAMES = frozenset(
    {
        SESSION_MANIFEST_NAME,
        "session-control.sqlite",
        "session-control.sqlite-wal",
        "session-control.sqlite-shm",
    }
)

# 全量对账时 list_children 的分页大小。
_RECONCILE_PAGE_LIMIT = 500
