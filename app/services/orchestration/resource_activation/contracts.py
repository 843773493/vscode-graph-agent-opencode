"""资源激活冻结边界的 policy 与唯一持久化 port。

冻结快照本体只有一个事实源：domain 的
:class:`app.domain.itemized.resource_activation.ResourceActivationSnapshotRef`
（9.1 冻结）。本模块不再保留第二套 Turn/ModelCall snapshot 类型，只定义
activation policy（配置派生）、受保护 body port 和唯一 Saver port。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
)

ResourceActivationBoundary = Literal["turn", "model_call"]
_BOUNDARIES: frozenset[str] = frozenset({"turn", "model_call"})
_KIND_PATTERN_ERROR = "resource kind 必须匹配 ^[a-z][a-z0-9_-]{0,63}$"

# binding 的 owner scope 由资源平台声明；derivation 契约当前未携带该字段
# （见 derivation/types.SemanticResourceDescriptor）。在资源平台补全前，
# activation 统一使用 session-scoped 语义，不得由 URI/路径推断。
# TODO(9.3→resource_platform): ResourceSnapshot 增加 owner_scope 后改为透传。
ACTIVATION_OWNER_SCOPE = "session"


@dataclass(frozen=True, slots=True)
class ResourceActivationPolicySnapshot:
    """一次 Turn 冻结的 activation policy；热更新只影响下一个 Turn。"""

    revision: str
    hash: str
    default_boundary: ResourceActivationBoundary
    boundaries: Mapping[str, ResourceActivationBoundary]

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> ResourceActivationPolicySnapshot:
        """从 Workspace 配置的 context.resource_activation 构造 policy。"""

        context = config.get("context")
        if not isinstance(context, Mapping):
            raise TypeError("context 配置必须是对象")
        section = context.get("resource_activation")
        if not isinstance(section, Mapping):
            raise TypeError("context.resource_activation 配置必须是对象")
        unknown_section_keys = set(section) - {"default_boundary", "overrides"}
        if unknown_section_keys:
            raise ValueError(
                "context.resource_activation 存在未登记字段: "
                f"{sorted(unknown_section_keys)}"
            )
        default_boundary = section.get("default_boundary")
        if default_boundary not in _BOUNDARIES:
            raise ValueError("context.resource_activation.default_boundary 必须是 turn|model_call")
        raw_overrides = section.get("overrides")
        if not isinstance(raw_overrides, Mapping):
            raise TypeError("context.resource_activation.overrides 必须是对象")
        boundaries: dict[str, ResourceActivationBoundary] = {}
        for key, value in raw_overrides.items():
            if (
                not isinstance(key, str)
                or len(key) == 0
                or len(key) > 64
                or key[0] not in "abcdefghijklmnopqrstuvwxyz"
                or any(
                    not (
                        char.isascii() and (char.islower() or char.isdigit())
                    )
                    and char not in "_-"
                    for char in key
                )
            ):
                raise ValueError(_KIND_PATTERN_ERROR)
            if value not in _BOUNDARIES:
                raise ValueError(
                    f"context.resource_activation.overrides.{key} 必须是 turn|model_call"
                )
            boundaries[key] = value  # type: ignore[assignment]
        normalized = {
            "default_boundary": default_boundary,
            "overrides": {key: boundaries[key] for key in sorted(boundaries)},
        }
        content_hash = sha256_jcs(normalized)
        return cls(
            revision=(
                "resource-activation-policy:v1:"
                + content_hash.removeprefix("sha256:jcs:v1:")
            ),
            hash=content_hash,
            default_boundary=default_boundary,  # type: ignore[arg-type]
            boundaries={key: boundaries[key] for key in sorted(boundaries)},
        )

    def effective_boundary(self, resource_kind: str) -> ResourceActivationBoundary:
        """返回某个已登记 resource kind 的 effective boundary。"""

        return self.boundaries.get(resource_kind, self.default_boundary)

    def has_model_call_boundary(self) -> bool:
        return self.default_boundary == "model_call" or "model_call" in self.boundaries.values()


class ResourceActivationBodyStore(Protocol):
    """把已冻结的语义 payload 正文写入受保护 detail/snapshot body store。

    只接受 activation coordinator 传入的内存 payload；绝不读取当前文件、
    URI、网络或 memory provider。返回 target-local typed :class:`DetailRef`。
    """

    def write_resource_body(
        self,
        *,
        owner_session_id: str,
        activation_snapshot_id: str,
        resource_id: str,
        activation_ordinal: int,
        payload: object,
        checkpoint_ns: str,
    ) -> DetailRef: ...


class ResourceActivationSnapshotSaver(Protocol):
    """itemized rollout owner 暴露给本目录的唯一持久化 port。

    只接受 domain 已冻结的内存 snapshot；失败必须抛出，不允许 Coordinator
    伪造成功。seal 路径另外通过 Saver 的 assembly 事务原子绑定。
    """

    async def save_resource_activation_snapshot(
        self, snapshot: ResourceActivationSnapshotRef
    ) -> None: ...


__all__ = [
    "ACTIVATION_OWNER_SCOPE",
    "ResourceActivationBodyStore",
    "ResourceActivationBoundary",
    "ResourceActivationPolicySnapshot",
    "ResourceActivationSnapshotSaver",
]
