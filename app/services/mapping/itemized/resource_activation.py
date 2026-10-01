"""sealed resource activation refs 的安全历史投影（9.4）。

三种运行面（LangChain/native Provider request、history API、Web response
model）统一消费同一份 sealed refs，不得按正文、路径、URI 或 DOM 推断/去重。

安全边界：

- 策略允许的 history detail 只携带安全 ``boxteam://`` display URI、语义
  revision、availability、resource kind/facet 与 opaque provenance ref。
- locator、credential、绝对路径与语义 payload 正文永不进入投影；payload 只
  存在于受保护 detail/snapshot body store。
- request-only resource binding 不是 canonical Turn member，不得计入 Turn
  ``item_count``/``elapsed_ms``/可展开 item。

本模块不执行 I/O，也不重新读取当前资源或 Registry。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
)

#: 正向投影允许出现的字段闭集；任何额外字段（locator/body/credential）必须在
#: 构造期被拒绝，而不是依赖调用方自觉。
SAFE_RESOURCE_PROJECTION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "activation_snapshot_id",
        "snapshot_kind",
        "effective_boundary",
        "activation_ordinal",
        "display_uri",
        "resource_kind",
        "facet",
        "revision",
        "availability",
        "provenance_ref",
    }
)


class ResourceActivationProjectionError(RuntimeError):
    """sealed refs 的历史投影违反安全边界；调用方必须显式失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class SafeResourceProjection:
    """一条 sealed resource binding 的模型安全历史投影；无 locator/正文。"""

    activation_snapshot_id: str
    snapshot_kind: str
    effective_boundary: str
    activation_ordinal: int
    display_uri: str
    resource_kind: str
    facet: str
    revision: str
    availability: str
    provenance_ref: str

    def to_dict(self) -> dict[str, object]:
        return {
            "activation_snapshot_id": self.activation_snapshot_id,
            "snapshot_kind": self.snapshot_kind,
            "effective_boundary": self.effective_boundary,
            "activation_ordinal": self.activation_ordinal,
            "display_uri": self.display_uri,
            "resource_kind": self.resource_kind,
            "facet": self.facet,
            "revision": self.revision,
            "availability": self.availability,
            "provenance_ref": self.provenance_ref,
        }


def _opaque_provenance_ref(
    snapshot: ResourceActivationSnapshotRef, binding: ResourceProvenanceRef
) -> str:
    """由 owner/snapshot/binding 关系确定性导出的 opaque ref。

    它不暴露 raw ``resource_id``、locator 或凭据，也不能用于解析当前资源；
    同一 sealed 事实在任何运行面得到同一个 ref。
    """

    return sha256_jcs(
        {
            "schema": "resource-provenance-ref:v1",
            "owner_session_id": snapshot.owner_session_id,
            "owner_thread_id": snapshot.owner_thread_id,
            "activation_snapshot_id": snapshot.activation_snapshot_id,
            "resource_id": binding.resource_id,
            "revision": binding.revision,
        }
    )


def project_sealed_resource_refs(
    snapshot: ResourceActivationSnapshotRef,
) -> tuple[SafeResourceProjection, ...]:
    """把 sealed activation snapshot 投影成有序、模型安全的历史 refs。

    顺序严格等于 domain 归一后的 ``activation_ordinal``，因此 live、刷新、
    重启与跨端读取得到同一 identity/order；不排序、不去重、不读正文。
    """

    if not isinstance(snapshot, ResourceActivationSnapshotRef):
        raise TypeError(
            "project_sealed_resource_refs 需要 domain ResourceActivationSnapshotRef"
        )
    return tuple(
        SafeResourceProjection(
            activation_snapshot_id=snapshot.activation_snapshot_id,
            snapshot_kind=snapshot.snapshot_kind,
            effective_boundary=binding.effective_boundary,
            activation_ordinal=binding.activation_ordinal,
            display_uri=binding.display_uri,
            resource_kind=binding.resource_kind,
            facet=binding.facet,
            revision=binding.revision,
            availability=binding.availability,
            provenance_ref=_opaque_provenance_ref(snapshot, binding),
        )
        for binding in snapshot.bindings
    )


def resource_projection_order_key(
    projections: Sequence[SafeResourceProjection],
) -> tuple[tuple[int, str], ...]:
    """跨端比较用的稳定 identity/order 键：ordinal + opaque provenance ref。"""

    return tuple(
        (projection.activation_ordinal, projection.provenance_ref)
        for projection in projections
    )


def assert_resource_bindings_not_counted_as_items(
    projections: Sequence[SafeResourceProjection],
    activity_stats: Mapping[str, object],
) -> None:
    """校验 request-only resource binding 未混入 Turn item 统计。

    resource binding 不是 canonical item：它既不贡献可展开 item，也不参与
    ``item_count`` 或 item sequence 范围。任何把 binding 计成 item 的投影都是
    显式错误，而不是显示层可以自行容忍的差异。
    """

    item_count = activity_stats.get("item_count")
    if not isinstance(item_count, int) or isinstance(item_count, bool) or item_count < 0:
        raise ResourceActivationProjectionError(
            "resource-activation-projection-invalid",
            f"activity_stats.item_count 非法: {item_count!r}",
        )
    # resource binding 不携带 canonical item sequence；beyond 的字段只允许
    # item_count 与首尾 canonical sequence，不得出现 per-binding 计数键。
    for forbidden in (
        "resource_count",
        "resource_item_count",
        "binding_count",
        "resource_elapsed_ms",
    ):
        if forbidden in activity_stats:
            raise ResourceActivationProjectionError(
                "resource-activation-projection-invalid",
                f"resource binding 不得进入 Turn item 统计: {forbidden}",
            )
    first = activity_stats.get("first_item_sequence")
    last = activity_stats.get("last_item_sequence")
    if item_count == 0 and (first is not None or last is not None):
        raise ResourceActivationProjectionError(
            "resource-activation-projection-invalid",
            "零 Item Turn 不得因 resource binding 携带 item sequence 范围",
        )
    if projections and item_count == 0 and first is None and last is None:
        # 显式正向断言：存在 resource binding 时 Turn 仍可以是零 Item。
        return


__all__ = [
    "SAFE_RESOURCE_PROJECTION_FIELDS",
    "ResourceActivationProjectionError",
    "SafeResourceProjection",
    "assert_resource_bindings_not_counted_as_items",
    "project_sealed_resource_refs",
    "resource_projection_order_key",
]
