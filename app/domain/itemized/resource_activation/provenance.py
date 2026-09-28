"""ResourceProvenanceRef：单条已激活语义资源的 provenance 事实。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.refs import require_manifest_token

from app.domain.itemized.resource_activation.common import (
    PROVENANCE_FIELD_ALIASES,
    RESOURCE_PROVENANCE_FIELDS,
    ResourceActivationBoundary,
    ResourceActivationContractError,
    SourceLineageRef,
    _AVAILABILITIES,
    _BOUNDARIES,
    _display_uri,
    _non_empty_string,
    _non_negative_int,
    _reject_forbidden_fields,
    _safe_identity,
    _schema_error,
    _token,
)


@dataclass(frozen=True, slots=True)
class ResourceProvenanceRef:
    """一条已激活语义资源的 provenance；不含 payload、locator 或凭据。"""

    resource_id: str
    display_uri: str
    resource_kind: str
    owner_scope: str
    facet: str
    revision: str
    availability: str
    content_length: int
    content_hash: str | None
    redacted_stable_digest: str | None
    source_lineage_ref: SourceLineageRef
    source_lineage_digest: str
    activation_ordinal: int
    effective_boundary: ResourceActivationBoundary
    captured_registry_generation: int
    snapshot_ref: DetailRef | None = None
    detail_ref: DetailRef | None = None

    def __post_init__(self) -> None:
        _safe_identity(self.resource_id, "ResourceProvenanceRef.resource_id")
        _display_uri(self.display_uri, "ResourceProvenanceRef.display_uri")
        _token(self.resource_kind, "ResourceProvenanceRef.resource_kind")
        _token(self.owner_scope, "ResourceProvenanceRef.owner_scope")
        _token(self.facet, "ResourceProvenanceRef.facet")
        _non_empty_string(self.revision, "ResourceProvenanceRef.revision")
        if self.availability not in _AVAILABILITIES:
            raise _schema_error(
                f"未知 ResourceProvenanceRef.availability: {self.availability!r}"
            )
        _non_negative_int(
            self.content_length, "ResourceProvenanceRef.content_length"
        )
        if not isinstance(self.source_lineage_ref, SourceLineageRef):
            raise _schema_error(
                "ResourceProvenanceRef.source_lineage_ref 必须是 SourceLineageRef"
            )
        if self.source_lineage_digest != self.source_lineage_ref.digest:
            raise ResourceActivationContractError(
                "resource-activation-hash-mismatch",
                "source_lineage_digest 与 source_lineage_ref 不一致",
            )
        _non_negative_int(
            self.activation_ordinal, "ResourceProvenanceRef.activation_ordinal"
        )
        if self.effective_boundary not in _BOUNDARIES:
            raise _schema_error(
                "ResourceProvenanceRef.effective_boundary 必须是 turn|model_call"
            )
        _non_negative_int(
            self.captured_registry_generation,
            "ResourceProvenanceRef.captured_registry_generation",
        )
        for name in ("snapshot_ref", "detail_ref"):
            ref = getattr(self, name)
            if ref is not None and not isinstance(ref, DetailRef):
                raise _schema_error(
                    f"ResourceProvenanceRef.{name} 必须是 typed DetailRef"
                )
        if (self.snapshot_ref is None) == (self.detail_ref is None):
            raise _schema_error(
                "ResourceProvenanceRef 必须恰好携带 snapshot_ref 或 detail_ref"
            )
        require_manifest_token(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_id": self.resource_id,
            "display_uri": self.display_uri,
            "resource_kind": self.resource_kind,
            "owner_scope": self.owner_scope,
            "facet": self.facet,
            "revision": self.revision,
            "content_length": self.content_length,
            "content_hash": self.content_hash,
            "redacted_stable_digest": self.redacted_stable_digest,
            "snapshot_ref": (
                self.snapshot_ref.to_dict() if self.snapshot_ref is not None else None
            ),
            "detail_ref": (
                self.detail_ref.to_dict() if self.detail_ref is not None else None
            ),
            "source_lineage_ref": self.source_lineage_ref.to_dict(),
            "source_lineage_digest": self.source_lineage_digest,
            "availability": self.availability,
            "activation_ordinal": self.activation_ordinal,
            "effective_boundary": self.effective_boundary,
            "captured_registry_generation": self.captured_registry_generation,
        }

    @classmethod
    def from_dict(cls, value: object) -> ResourceProvenanceRef:
        if not isinstance(value, Mapping):
            raise _schema_error("resource provenance 必须是 typed object")
        _reject_forbidden_fields(
            value,
            allowed=RESOURCE_PROVENANCE_FIELDS,
            aliases=PROVENANCE_FIELD_ALIASES,
            where="ResourceProvenanceRef",
        )
        missing = RESOURCE_PROVENANCE_FIELDS - set(value)
        if missing:
            raise _schema_error(
                f"ResourceProvenanceRef 缺少必填字段: {sorted(missing)}"
            )
        lineage_raw = value["source_lineage_ref"]
        lineage = (
            lineage_raw
            if isinstance(lineage_raw, SourceLineageRef)
            else SourceLineageRef.from_dict(lineage_raw)
        )
        refs: dict[str, DetailRef | None] = {}
        for name in ("snapshot_ref", "detail_ref"):
            raw_ref = value[name]
            if raw_ref is None or isinstance(raw_ref, DetailRef):
                refs[name] = raw_ref
            else:
                refs[name] = DetailRef.from_dict(raw_ref)
        return cls(
            resource_id=value["resource_id"],
            display_uri=value["display_uri"],
            resource_kind=value["resource_kind"],
            owner_scope=value["owner_scope"],
            facet=value["facet"],
            revision=value["revision"],
            availability=value["availability"],
            content_length=value["content_length"],
            content_hash=value["content_hash"],
            redacted_stable_digest=value["redacted_stable_digest"],
            source_lineage_ref=lineage,
            source_lineage_digest=value["source_lineage_digest"],
            activation_ordinal=value["activation_ordinal"],
            effective_boundary=value["effective_boundary"],
            captured_registry_generation=value["captured_registry_generation"],
            snapshot_ref=refs["snapshot_ref"],
            detail_ref=refs["detail_ref"],
        )


def _ordered_bindings(
    bindings: Sequence[ResourceProvenanceRef],
) -> tuple[ResourceProvenanceRef, ...]:
    result = tuple(bindings)
    for binding in result:
        if not isinstance(binding, ResourceProvenanceRef):
            raise _schema_error("bindings 必须是 ResourceProvenanceRef 元组")
    ordinals = [binding.activation_ordinal for binding in result]
    if len(set(ordinals)) != len(ordinals):
        raise ResourceActivationContractError(
            "resource-activation-ordinal-conflict",
            "activation_ordinal 在同一 snapshot 内必须唯一",
        )
    return tuple(sorted(result, key=lambda binding: binding.activation_ordinal))


def resource_bindings_hash(bindings: Sequence[ResourceProvenanceRef]) -> str:
    """只覆盖按 activation ordinal 排列的实际有序语义选择。"""

    return sha256_jcs(
        {
            "schema": "resource-activation-bindings:v1",
            "bindings": [
                {
                    "resource_id": binding.resource_id,
                    "resource_kind": binding.resource_kind,
                    "owner_scope": binding.owner_scope,
                    "facet": binding.facet,
                    "revision": binding.revision,
                    "content_length": binding.content_length,
                    "content_hash": binding.content_hash,
                    "redacted_stable_digest": binding.redacted_stable_digest,
                    "availability": binding.availability,
                    "display_uri": binding.display_uri,
                }
                for binding in _ordered_bindings(bindings)
            ],
        }
    )


