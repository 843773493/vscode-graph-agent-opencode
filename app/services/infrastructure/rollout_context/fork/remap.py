"""full_rollout_copy 的分阶段 target-local identity remap coordinator。"""

from __future__ import annotations

import sqlite3

from app.services.infrastructure.rollout_context.fork.assembly_copy import (
    refresh_copied_assemblies,
    validate_source_assemblies,
)
from app.services.infrastructure.rollout_context.fork.full_copy.plans import (
    detach_copied_plan_registry,
    write_copied_plan_registry,
)
from app.services.infrastructure.rollout_context.fork.remap_files import (
    materialize_full_copy_files,
    restore_full_copy_files,
)
from app.services.infrastructure.rollout_context.fork.remap_finalize import (
    record_full_copy_lineage,
    refresh_full_copy_projections,
)
from app.services.infrastructure.rollout_context.fork.remap_prepare import (
    prepare_full_copy_remap,
)
from app.services.infrastructure.rollout_context.fork.sql.activation import (
    rewrite_full_copy_activation_catalog,
)
from app.services.infrastructure.rollout_context.fork.sql.catalog import (
    rewrite_full_copy_fast_columns,
)
from app.services.infrastructure.rollout_context.fork.sql.commits import (
    rewrite_full_copy_control_and_commits,
)
from app.services.infrastructure.rollout_context.fork.sql.references import (
    rewrite_full_copy_reference_columns,
)


class ForkRemapMixin:
    def _remap_full_copy_v2_entities(
        self,
        connection: sqlite3.Connection,
        *,
        source_session_id: str,
        target_session_id: str,
        fork_id: str,
        checkpoint_ns: str,
        timestamp: str,
        detail_capability=None,
        target_session_key: bytes | None = None,
    ) -> None:
        state = prepare_full_copy_remap(
            self,
            connection,
            source_session_id=source_session_id,
            target_session_id=target_session_id,
            fork_id=fork_id,
            checkpoint_ns=checkpoint_ns,
            timestamp=timestamp,
        )
        if state is None:
            return
        state.detail_capability = detail_capability
        state.target_session_key = target_session_key
        state.source_assembly_fingerprints = {
            assembly_id: (plan_id, plan_hash, request_hash)
            for assembly_id, plan_id, plan_hash, request_hash in connection.execute(
                "SELECT assembly_id, plan_id, plan_hash, request_hash FROM context_assemblies"
            ).fetchall()
        }
        try:
            detach_copied_plan_registry(state)
            materialize_full_copy_files(state)
            rewrite_full_copy_fast_columns(state)
            rewrite_full_copy_reference_columns(state)
            rewrite_full_copy_control_and_commits(state)
            refresh_full_copy_projections(state)
            if detail_capability is not None:
                from app.services.infrastructure.rollout_context.fork.full_copy.manifests import (
                    refresh_detail_manifests,
                )

                refresh_detail_manifests(state)
            refresh_copied_assemblies(state)
            # activation 行的 lineage_detail_ref/assembly binding 依赖已本地化的
            # target detail ref 与 assembly plan/request hash，必须在它们之后重造。
            rewrite_full_copy_activation_catalog(state)
            record_full_copy_lineage(state)
            write_copied_plan_registry(state)
            if detail_capability is not None:
                from app.services.infrastructure.rollout_context.fork.full_copy.manifests import (
                    record_source_fingerprints,
                )

                record_source_fingerprints(state)
            validate_source_assemblies(self, connection, checkpoint_ns)
        except BaseException:
            restore_full_copy_files(state)
            raise


__all__ = ["ForkRemapMixin"]
