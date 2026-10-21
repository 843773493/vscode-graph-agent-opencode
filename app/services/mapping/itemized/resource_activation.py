"""把 Saver 已验证的封存资源映射为普通检查面安全字段；不执行 I/O。"""

from __future__ import annotations

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.schemas.internal_v2.session_context import (
    SessionContextProvenanceRefDTO,
    SessionContextResourceDTO,
)


def project_sealed_resource_refs(
    snapshot: ResourceActivationSnapshotRef,
) -> tuple[SessionContextResourceDTO, ...]:
    """保留封存 binding 顺序；URI不作为身份，也不补读当前资源。"""
    return tuple(
        SessionContextResourceDTO(
            display_uri=binding.display_uri,
            resource_kind=binding.resource_kind,
            owner_scope=binding.owner_scope,
            facet=binding.facet,
            revision=sha256_jcs({
                "schema": "resource-display-revision:v1",
                "revision": binding.revision,
            }),
            availability=binding.availability,
            provenance_ref=SessionContextProvenanceRefDTO(token=sha256_jcs({
                "schema": "resource-provenance-ref:v1",
                "owner_session_id": snapshot.owner_session_id,
                "owner_thread_id": snapshot.owner_thread_id,
                "activation_snapshot_id": snapshot.activation_snapshot_id,
                "resource_id": binding.resource_id,
                "revision": binding.revision,
            })),
        )
        for binding in snapshot.bindings
    )
