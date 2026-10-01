"""ResourceActivationSnapshotRef：Turn / model-call 冻结的资源激活结果。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.domain.itemized.hashing import sha256_jcs, validate_hash_token

from app.domain.itemized.resource_activation.common import (
    RESOURCE_ACTIVATION_SNAPSHOT_FIELDS,
    SNAPSHOT_FIELD_ALIASES,
    ResourceActivationContractError,
    _CODE_HASH_MISMATCH,
    _CODE_PARENT_INVALID,
    _SNAPSHOT_KINDS,
    _non_empty_string,
    _non_negative_int,
    _reject_forbidden_fields,
    _schema_error,
)
from app.domain.itemized.resource_activation.provenance import (
    ResourceProvenanceRef,
    _ordered_bindings,
    resource_bindings_hash,
)


def resource_activation_provenance_hash(
    *,
    snapshot_kind: str,
    parent: ResourceActivationSnapshotRef | None,
    activation_policy_revision: str,
    activation_policy_hash: str,
    registry_generation: int,
    bindings: Sequence[ResourceProvenanceRef],
) -> str:
    """覆盖 policy/capture/parent 关系与 source lineage 的独立 provenance hash。"""

    return sha256_jcs(
        {
            "schema": "resource-activation-provenance:v1",
            "snapshot_kind": snapshot_kind,
            "parent_snapshot_kind": None if parent is None else parent.snapshot_kind,
            "parent_bindings_hash": None if parent is None else parent.bindings_hash,
            "activation_policy_revision": activation_policy_revision,
            "activation_policy_hash": activation_policy_hash,
            "registry_generation": registry_generation,
            "bindings": [
                {
                    "resource_id": binding.resource_id,
                    "effective_boundary": binding.effective_boundary,
                    "captured_registry_generation": (
                        binding.captured_registry_generation
                    ),
                    "source_lineage_digest": binding.source_lineage_digest,
                }
                for binding in _ordered_bindings(bindings)
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class ResourceActivationSnapshotRef:
    """一个 Turn 或一次 model call 冻结的资源激活结果。"""

    activation_snapshot_id: str
    snapshot_kind: str
    activation_policy_revision: str
    activation_policy_hash: str
    registry_generation: int
    owner_session_id: str
    owner_thread_id: str
    turn_id: str
    captured_at: str
    bindings: tuple[ResourceProvenanceRef, ...]
    parent: ResourceActivationSnapshotRef | None = None
    model_call_id: str | None = None
    bindings_hash: str = field(init=False, repr=False, compare=False)
    activation_provenance_hash: str = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _non_empty_string(
            self.activation_snapshot_id,
            "ResourceActivationSnapshotRef.activation_snapshot_id",
        )
        if self.snapshot_kind not in _SNAPSHOT_KINDS:
            raise _schema_error(
                f"未知 snapshot_kind: {self.snapshot_kind!r}，必须是 turn|model_call"
            )
        _non_empty_string(
            self.activation_policy_revision,
            "ResourceActivationSnapshotRef.activation_policy_revision",
        )
        validate_hash_token(
            self.activation_policy_hash,
            "ResourceActivationSnapshotRef.activation_policy_hash",
        )
        _non_negative_int(
            self.registry_generation,
            "ResourceActivationSnapshotRef.registry_generation",
        )
        for name in ("owner_session_id", "owner_thread_id", "turn_id", "captured_at"):
            _non_empty_string(
                getattr(self, name), f"ResourceActivationSnapshotRef.{name}"
            )
        if self.snapshot_kind == "turn":
            if self.parent is not None:
                raise ResourceActivationContractError(
                    _CODE_PARENT_INVALID,
                    "turn snapshot 不得携带 parent turn snapshot",
                )
            if self.model_call_id is not None:
                raise ResourceActivationContractError(
                    _CODE_PARENT_INVALID,
                    "turn snapshot 不得携带 model_call_id",
                )
        else:
            if not isinstance(self.parent, ResourceActivationSnapshotRef):
                raise ResourceActivationContractError(
                    _CODE_PARENT_INVALID,
                    "model_call snapshot 必须携带 typed parent turn snapshot",
                )
            if self.parent.snapshot_kind != "turn":
                raise ResourceActivationContractError(
                    _CODE_PARENT_INVALID,
                    "model_call snapshot 的 parent 必须是 turn snapshot",
                )
            if (
                self.parent.owner_session_id != self.owner_session_id
                or self.parent.owner_thread_id != self.owner_thread_id
                or self.parent.turn_id != self.turn_id
            ):
                raise ResourceActivationContractError(
                    _CODE_PARENT_INVALID,
                    "model_call snapshot 的 owner/turn 必须与 parent 一致",
                )
            if not isinstance(self.model_call_id, str) or not self.model_call_id:
                raise ResourceActivationContractError(
                    _CODE_PARENT_INVALID,
                    "model_call snapshot 必须携带非空 model_call_id",
                )
        ordered = _ordered_bindings(self.bindings)
        if not ordered:
            raise _schema_error(
                "ResourceActivationSnapshotRef 必须捕获至少一个 binding"
            )
        if self.snapshot_kind == "turn":
            if any(binding.effective_boundary != "turn" for binding in ordered):
                raise _schema_error("turn snapshot 只能包含 turn-bound binding")
        else:
            parent_bindings = self.parent.bindings
            if ordered[: len(parent_bindings)] != parent_bindings:
                raise ResourceActivationContractError(
                    _CODE_PARENT_INVALID,
                    "model_call snapshot 必须逐字节复用 parent turn-bound binding 前缀",
                )
            if any(
                binding.effective_boundary == "turn"
                for binding in ordered[len(parent_bindings):]
            ):
                raise _schema_error(
                    "model_call snapshot 追加的 binding 必须使用 model_call boundary"
                )
            if self.registry_generation < self.parent.registry_generation:
                raise _schema_error(
                    "model_call snapshot.registry_generation 不得小于 parent"
                )
        object.__setattr__(self, "bindings", ordered)
        object.__setattr__(self, "bindings_hash", resource_bindings_hash(ordered))
        object.__setattr__(
            self,
            "activation_provenance_hash",
            resource_activation_provenance_hash(
                snapshot_kind=self.snapshot_kind,
                parent=self.parent,
                activation_policy_revision=self.activation_policy_revision,
                activation_policy_hash=self.activation_policy_hash,
                registry_generation=self.registry_generation,
                bindings=ordered,
            ),
        )

    @property
    def parent_turn_snapshot_id(self) -> str | None:
        """typed parent relation 的 id 投影；不进入任一内容 hash。"""

        return None if self.parent is None else self.parent.activation_snapshot_id

    def to_dict(self) -> dict[str, object]:
        return {
            "activation_snapshot_id": self.activation_snapshot_id,
            "snapshot_kind": self.snapshot_kind,
            "parent_turn_snapshot_id": self.parent_turn_snapshot_id,
            "activation_policy_revision": self.activation_policy_revision,
            "activation_policy_hash": self.activation_policy_hash,
            "registry_generation": self.registry_generation,
            "owner_session_id": self.owner_session_id,
            "owner_thread_id": self.owner_thread_id,
            "turn_id": self.turn_id,
            "model_call_id": self.model_call_id,
            "captured_at": self.captured_at,
            "bindings_hash": self.bindings_hash,
            "activation_provenance_hash": self.activation_provenance_hash,
            "bindings": [binding.to_dict() for binding in self.bindings],
        }

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        parent: ResourceActivationSnapshotRef | None = None,
    ) -> ResourceActivationSnapshotRef:
        """恢复 snapshot；model_call 必须由调用方提供 typed parent relation。"""

        if not isinstance(value, Mapping):
            raise _schema_error("resource activation snapshot 必须是 typed object")
        _reject_forbidden_fields(
            value,
            allowed=RESOURCE_ACTIVATION_SNAPSHOT_FIELDS,
            aliases=SNAPSHOT_FIELD_ALIASES,
            where="ResourceActivationSnapshotRef",
        )
        missing = RESOURCE_ACTIVATION_SNAPSHOT_FIELDS - set(value)
        if missing:
            raise _schema_error(
                f"ResourceActivationSnapshotRef 缺少必填字段: {sorted(missing)}"
            )
        raw_bindings = value["bindings"]
        if not isinstance(raw_bindings, Sequence) or isinstance(
            raw_bindings, (str, bytes)
        ):
            raise _schema_error(
                "ResourceActivationSnapshotRef.bindings 必须是数组"
            )
        snapshot_kind = value["snapshot_kind"]
        declared_parent_id = value["parent_turn_snapshot_id"]
        if snapshot_kind == "model_call":
            if not isinstance(parent, ResourceActivationSnapshotRef):
                raise ResourceActivationContractError(
                    _CODE_PARENT_INVALID,
                    "恢复 model_call snapshot 必须提供 typed parent turn snapshot",
                )
            if declared_parent_id != parent.activation_snapshot_id:
                raise ResourceActivationContractError(
                    _CODE_PARENT_INVALID,
                    "parent_turn_snapshot_id 与提供的 parent relation 不一致",
                )
        elif declared_parent_id is not None:
            raise ResourceActivationContractError(
                _CODE_PARENT_INVALID,
                "turn snapshot 的 parent_turn_snapshot_id 必须是 null",
            )
        restored = cls(
            activation_snapshot_id=value["activation_snapshot_id"],
            snapshot_kind=snapshot_kind,
            activation_policy_revision=value["activation_policy_revision"],
            activation_policy_hash=value["activation_policy_hash"],
            registry_generation=value["registry_generation"],
            owner_session_id=value["owner_session_id"],
            owner_thread_id=value["owner_thread_id"],
            turn_id=value["turn_id"],
            captured_at=value["captured_at"],
            bindings=tuple(
                ResourceProvenanceRef.from_dict(item) for item in raw_bindings
            ),
            parent=parent,
            model_call_id=value["model_call_id"],
        )
        for name in ("bindings_hash", "activation_provenance_hash"):
            if value[name] != getattr(restored, name):
                raise ResourceActivationContractError(
                    _CODE_HASH_MISMATCH,
                    f"恢复的 {name} 与重算结果不一致",
                )
        return restored
