"""v2 资源激活 snapshot/provenance 的冻结领域合同。

本模块只定义类型、字段清单和内容 hash 范围，不拥有 ResourceRegistry、
monitor、loader、activation policy，也不做任何 I/O。资源平台在 model-call
preparation 前冻结的 typed snapshot/decision 在此被固化成 ContextStore
可以持久化和恢复的领域事实。

破坏性 schema 清单（字段名即持久化列名，无旧字段 reader、无别名）：

ResourceActivationSnapshotRef
  activation_snapshot_id    snapshot_kind
  parent_turn_snapshot_id   activation_policy_revision
  activation_policy_hash    registry_generation
  owner_session_id          owner_thread_id
  turn_id                   model_call_id
  captured_at               bindings_hash
  activation_provenance_hash
  bindings[]                # ResourceProvenanceRef

ResourceProvenanceRef
  resource_id               display_uri          resource_kind
  owner_scope               facet                revision
  content_length            content_hash         redacted_stable_digest
  snapshot_ref              detail_ref           source_lineage_ref
  source_lineage_digest     availability         activation_ordinal
  effective_boundary        captured_registry_generation

SourceLineageRef
  lineage_id                derivation_version
  sources[]                 digest

两个内容 hash 的语义严格分离：

* bindings_hash 只覆盖按 activation ordinal 排列的实际语义选择；来源 raw
  revision、source lineage、policy、effective boundary 和 captured
  generation 均不进入，因此相同 wire 选择不会因无关观察或 policy 发布抖动。
* activation_provenance_hash 额外覆盖 snapshot kind、parent_kind 与
  parent_bindings_hash 关系描述、policy revision/hash、Registry generation
  以及每个 binding 的 effective boundary、captured generation 和 source
  lineage digest。

运行 identity（activation_snapshot_id、Turn/model-call identity、owner
session/thread）与 captured_at 只作 typed relation，不进入任一内容 hash；
具体 parent_turn_snapshot_id 由 typed parent relation 与 turn-bound binding
逐字节复用校验保护。
"""

from __future__ import annotations

from app.domain.itemized.resource_activation.common import (
    ABSOLUTE_PATH_FIELDS,
    BOUNDARY_SINGULARITY_FIELDS,
    CREDENTIAL_FIELDS,
    LEGACY_FIELDS,
    PROVENANCE_FIELD_ALIASES,
    PROVIDER_LOCATOR_FIELDS,
    RESOURCE_ACTIVATION_ERROR_CODES,
    RESOURCE_ACTIVATION_SNAPSHOT_FIELDS,
    RESOURCE_PROVENANCE_FIELDS,
    SNAPSHOT_FIELD_ALIASES,
    SOURCE_LINEAGE_REF_FIELDS,
    ResourceActivationBoundary,
    ResourceActivationContractError,
    SourceLineageRef,
)
from app.domain.itemized.resource_activation.provenance import (
    ResourceProvenanceRef,
    resource_bindings_hash,
)
from app.domain.itemized.resource_activation.snapshot import (
    ResourceActivationSnapshotRef,
    resource_activation_provenance_hash,
)


__all__ = [
    "ABSOLUTE_PATH_FIELDS",
    "BOUNDARY_SINGULARITY_FIELDS",
    "CREDENTIAL_FIELDS",
    "LEGACY_FIELDS",
    "PROVENANCE_FIELD_ALIASES",
    "PROVIDER_LOCATOR_FIELDS",
    "RESOURCE_ACTIVATION_ERROR_CODES",
    "RESOURCE_ACTIVATION_SNAPSHOT_FIELDS",
    "RESOURCE_PROVENANCE_FIELDS",
    "SNAPSHOT_FIELD_ALIASES",
    "SOURCE_LINEAGE_REF_FIELDS",
    "ResourceActivationBoundary",
    "ResourceActivationContractError",
    "ResourceActivationSnapshotRef",
    "ResourceProvenanceRef",
    "SourceLineageRef",
    "resource_activation_provenance_hash",
    "resource_bindings_hash",
]
