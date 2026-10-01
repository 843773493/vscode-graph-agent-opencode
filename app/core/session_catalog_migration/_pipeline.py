"""迁移主管线:gate 内重建 → 完整复验 → 物理迁移 → 终验 → completed。"""

from __future__ import annotations

from ._contracts import (
    SessionCatalogMigrationResult,
    _MigrationContext,
    _result_to_dict,
)


class SessionCatalogMigratorPipelineMixin:
    """迁移主管线:gate 内重建 → 完整复验 → 物理迁移 → 终验 → completed。"""

    # ------------------------------------------------------------------
    # 迁移主管线:gate 内重建 → 完整复验 → 物理迁移 → 终验 → completed
    # ------------------------------------------------------------------

    async def _run_pipeline(
        self, context: _MigrationContext, *, entry_state: str
    ) -> SessionCatalogMigrationResult:
        """preparing→catalog_rebuilt→physical_migrated→completed 的公共管线。

        ``entry_state`` 是本次调用进入时的 journal 状态;各阶段 checkpoint
        只在状态发生转移时写入,重入按 physical 节定点幂等继续。
        """
        # 阶段1:catalog 幂等重建(gate 内 create-or-verify + 全量对账)。
        await self._rebuild_catalog_in_gate(context)
        if entry_state == "preparing":
            self._write_journal_context(
                context, state="catalog_rebuilt", result=None
            )
        # 阶段2:物理迁移前完整复验(R11 语义;物理已开始则只复验 index)。
        if self._physical_started(context.physical):
            self._verify_index_only(
                context.backup,
                stage="备份复验(物理迁移已开始,分层 index-only)",
            )
        else:
            self._verify_backup_full(context.backup, stage="备份复验(物理迁移前)")
        # 阶段3:物理树迁移 + session-control 初始化(按 physical 节定点继续)。
        self._run_physical_stage(context)
        if entry_state in ("preparing", "catalog_rebuilt"):
            self._write_journal_context(
                context, state="physical_migrated", result=None
            )
        # 阶段4:分层终验(index + 新位置 sha/清单 + 隔离/删除布局)。
        self._verify_post_physical(context, stage="终验(物理迁移后)")
        # 阶段5:completed。
        result = SessionCatalogMigrationResult(
            migrated_session_nodes=sum(
                1 for item in context.frozen if item.kind == "session"
            ),
            migrated_folder_nodes=sum(
                1 for item in context.frozen if item.kind == "folder"
            ),
            quarantined_nodes=tuple(context.quarantined),
            journal_path=self._journal_path,
        )
        self._write_journal_context(
            context, state="completed", result=_result_to_dict(result)
        )
        return result
