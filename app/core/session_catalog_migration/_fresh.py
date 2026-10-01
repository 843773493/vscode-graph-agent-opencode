"""首次迁移入口:预检 → 旧权威 → 备份 → 冻结 → preparing。"""

from __future__ import annotations

import uuid

from ._contracts import SessionCatalogMigrationResult, _MigrationContext


class SessionCatalogMigratorFreshMixin:
    """首次迁移入口:预检 → 旧权威 → 备份 → 冻结 → preparing。"""

    # ------------------------------------------------------------------
    # 首次迁移:预检 → 旧权威 → 备份 → 冻结 → preparing
    # ------------------------------------------------------------------

    async def _fresh_migrate(self) -> SessionCatalogMigrationResult:
        self._preflight_index(stage="预检")
        nodes = self._read_old_authority()
        try:
            backup = self._compute_backup(nodes, stage="备份清单")
        except (OSError, ValueError) as error:
            raise self._fail("备份清单", f"旧树备份清单计算失败: {error}") from error
        frozen, quarantined = self._freeze_and_quarantine(nodes)
        migration_id = uuid.uuid4().hex
        physical = self._build_initial_physical(nodes, frozen, quarantined)
        context = _MigrationContext(
            backup=backup,
            frozen=frozen,
            quarantined=quarantined,
            migration_id=migration_id,
            physical=physical,
        )
        self._write_journal_context(context, state="preparing", result=None)
        return await self._run_pipeline(context, entry_state="preparing")
