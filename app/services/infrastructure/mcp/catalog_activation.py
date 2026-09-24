"""MCP catalog binding 与指引在同一激活边界的原子冻结。

同一 snapshot 同时封存 ExtensionCatalogBindingRef 与 McpToolGuidanceSnapshot，
provenance hash 覆盖两者。mcp.catalog/* typed 事件只是通知，durable truth 由
catalog owner 的 immutable revision 与唯一 Saver 保存的 snapshot 承担。
默认 turn 策略下 model call 复用 parent snapshot 不漂移；model_call 策略在
下一安全边界显式 relist 并刷新。relist 失败/输入 dirty/revision 冲突一律
fail closed，不做半发布。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from app.services.infrastructure.mcp.catalog_owner import McpCatalogOwner
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogBindingRef,
    ExtensionCatalogUnavailableError,
    ExtensionDispatchBindingRef,
    payload_digest,
)
from app.services.infrastructure.mcp.guidance_source_port import (
    MCP_GUIDANCE_SOURCE_ID,
    McpToolGuidanceSourcePort,
    McpToolGuidanceSourceRegistration,
)
from app.services.infrastructure.mcp.tool_guidance import (
    McpToolGuidanceProducer,
    McpToolGuidanceSnapshot,
)
from app.services.orchestration.resource_activation.contracts import (
    ResourceActivationPolicySnapshot,
)

_ACTIVATION_HASH_DOMAIN = "mcp-catalog-activation:v1"
_MCP_TOOL_CATALOG_RESOURCE_KIND = "mcp_tool_catalog"


class McpCatalogActivationError(RuntimeError):
    """MCP catalog activation 冻结的显式错误基类。"""


class McpCatalogActivationConflictError(McpCatalogActivationError):
    """binding 与指引 revision 冲突或冻结期间目录漂移；拒绝半发布。"""


class McpCatalogActivationSnapshotSaver(Protocol):
    """唯一持久化口：生产实现是受保护 snapshot body store 之上的唯一 writer。

    实现必须原子保存 binding/guidance/dispatch hash 三者；任何失败都显式抛出，
    不允许 binder 伪造成功或做半发布。
    """

    async def save_mcp_catalog_activation_snapshot(
        self,
        snapshot: McpCatalogActivationSnapshot,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class McpCatalogActivationSnapshot:
    """一次激活边界冻结的原子结果：binding ref + 指引 + dispatch binding。

    ``binding_ref``/``guidance``/``dispatch_binding`` 必须在同一
    ModelCallResourceSnapshot 内原子绑定，不能只更新一侧；provenance hash 覆盖
    三者，``dispatch_binding.dispatch_binding_hash`` 是独立 dispatch 校验 hash。
    """

    activation_snapshot_id: str
    snapshot_kind: Literal["turn", "model_call"]
    owner_session_id: str
    owner_thread_id: str
    turn_id: str
    effective_boundary: str
    binding_ref: ExtensionCatalogBindingRef
    guidance: McpToolGuidanceSnapshot
    dispatch_binding: ExtensionDispatchBindingRef
    model_call_id: str | None = None
    parent_activation_id: str | None = None
    activation_provenance_hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.binding_ref.catalog_revision != self.guidance.catalog_revision:
            raise McpCatalogActivationConflictError(
                "binding 与指引 catalog revision 不一致，拒绝原子冻结: "
                f"binding={self.binding_ref.catalog_revision} "
                f"guidance={self.guidance.catalog_revision}"
            )
        # dispatch binding 与同一 snapshot 的 binding/指引必须逐字段一致，
        # 否则 seal 就是只更新一侧的半发布。
        if self.dispatch_binding.binding_ref.binding_hash != self.binding_ref.binding_hash:
            raise McpCatalogActivationConflictError(
                "dispatch binding 与 sealed catalog binding 不一致，拒绝原子冻结"
            )
        if self.dispatch_binding.guidance_revision != self.guidance.guidance_revision:
            raise McpCatalogActivationConflictError(
                "dispatch binding 与指引 revision 不一致，拒绝原子冻结: "
                f"dispatch={self.dispatch_binding.guidance_revision} "
                f"guidance={self.guidance.guidance_revision}"
            )
        if (
            self.dispatch_binding.owner_session_id != self.owner_session_id
            or self.dispatch_binding.owner_thread_id != self.owner_thread_id
            or self.dispatch_binding.turn_id != self.turn_id
            or self.dispatch_binding.model_call_id != self.model_call_id
        ):
            raise McpCatalogActivationConflictError(
                "dispatch binding 与 activation snapshot 运行 identity 不一致"
            )
        if self.effective_boundary not in {"turn", "model_call"}:
            raise ValueError(
                f"effective_boundary 必须是 turn|model_call: {self.effective_boundary!r}"
            )
        if self.snapshot_kind == "turn":
            if self.model_call_id is not None or self.parent_activation_id is not None:
                raise ValueError("turn snapshot 不得携带 model_call/parent 字段")
        elif not self.model_call_id or not self.parent_activation_id:
            raise ValueError("model_call snapshot 必须携带 model_call_id 与 parent")
        object.__setattr__(
            self,
            "activation_provenance_hash",
            payload_digest(
                {
                    "schema": _ACTIVATION_HASH_DOMAIN,
                    "activation_snapshot_id": self.activation_snapshot_id,
                    "snapshot_kind": self.snapshot_kind,
                    "owner_session_id": self.owner_session_id,
                    "owner_thread_id": self.owner_thread_id,
                    "turn_id": self.turn_id,
                    "effective_boundary": self.effective_boundary,
                    "catalog_binding_hash": self.binding_ref.binding_hash,
                    "generation": self.binding_ref.generation,
                    "guidance_revision": self.guidance.guidance_revision,
                    "extension_dispatch_binding_hash": (
                        self.dispatch_binding.dispatch_binding_hash
                    ),
                    "model_call_id": self.model_call_id,
                    "parent_activation_id": self.parent_activation_id,
                },
                context="MCP catalog activation 载荷",
                error_type=McpCatalogActivationError,
            ),
        )


class McpCatalogActivationBinder:
    """activation 边界冻结入口；默认 Turn 不漂移，model_call 下一安全边界刷新。"""

    def __init__(
        self,
        *,
        catalog_owner: McpCatalogOwner,
        policy: ResourceActivationPolicySnapshot,
        saver: McpCatalogActivationSnapshotSaver,
        guidance_producer: McpToolGuidanceProducer | None = None,
        guidance_source_port: McpToolGuidanceSourcePort | None = None,
    ) -> None:
        self._catalog_owner = catalog_owner
        self._policy = policy
        self._saver = saver
        self._producer = guidance_producer or McpToolGuidanceProducer()
        self._guidance_source_port = guidance_source_port
        self._last_guidance: McpToolGuidanceSnapshot | None = None

    async def freeze_turn_activation(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        turn_id: str,
    ) -> McpCatalogActivationSnapshot:
        """active execution slot 冻结 binding + 指引；先 relist 无通知 server。"""
        await self._relist_servers_without_notifications()
        return await self._freeze(
            snapshot_kind="turn",
            owner_session_id=owner_session_id,
            owner_thread_id=owner_thread_id,
            turn_id=turn_id,
            model_call_id=None,
            parent=None,
        )

    async def prepare_model_call_activation(
        self,
        *,
        parent: McpCatalogActivationSnapshot,
        model_call_id: str,
    ) -> McpCatalogActivationSnapshot:
        """默认 Turn 复用 parent 不漂移；model_call 边界重新冻结。"""
        if parent.snapshot_kind != "turn":
            raise McpCatalogActivationError(
                "model_call preparation 的 parent 必须是 turn snapshot: "
                f"parent_kind={parent.snapshot_kind}"
            )
        boundary = self._effective_boundary()
        if boundary == "turn":
            return parent
        await self._relist_servers_without_notifications()
        return await self._freeze(
            snapshot_kind="model_call",
            owner_session_id=parent.owner_session_id,
            owner_thread_id=parent.owner_thread_id,
            turn_id=parent.turn_id,
            model_call_id=model_call_id,
            parent=parent,
        )

    async def _relist_servers_without_notifications(self) -> None:
        # 显式激活边界 relist 无通知 server；失败显式抛出（fail closed）。
        await self._catalog_owner.relist_servers_without_notifications()

    def _effective_boundary(self) -> str:
        return self._policy.effective_boundary(_MCP_TOOL_CATALOG_RESOURCE_KIND)

    async def _freeze(
        self,
        *,
        snapshot_kind: Literal["turn", "model_call"],
        owner_session_id: str,
        owner_thread_id: str,
        turn_id: str,
        model_call_id: str | None,
        parent: McpCatalogActivationSnapshot | None,
    ) -> McpCatalogActivationSnapshot:
        owner = self._catalog_owner
        # 冻结读取序列在同一事件循环线程内同步完成，无 await 撕裂窗口；
        # revision 与 binding 再核对一次，冲突显式拒绝。
        revision = owner.catalog_revision()
        binding_ref = owner.binding_snapshot()
        if binding_ref.catalog_revision != revision:
            raise McpCatalogActivationConflictError(
                "catalog revision 在冻结期间发生变化，拒绝半发布: "
                f"read={revision} binding={binding_ref.catalog_revision}"
            )
        guidance = self._producer.produce(
            catalog_revision=revision,
            descriptors=owner.tool_descriptors(),
            tools_by_id={tool.name: tool for tool in owner.get_tools()},
            previous=self._last_guidance,
        )
        if snapshot_kind == "turn":
            snapshot_id = (
                f"mcp-activation:{owner_session_id}:{owner_thread_id}:{turn_id}"
            )
        else:
            snapshot_id = (
                f"mcp-activation:{owner_session_id}:{owner_thread_id}:{turn_id}:"
                f"{model_call_id}"
            )
        dispatch_binding = ExtensionDispatchBindingRef(
            binding_ref=binding_ref,
            guidance_revision=guidance.guidance_revision,
            activation_policy_revision=self._policy.revision,
            owner_session_id=owner_session_id,
            owner_thread_id=owner_thread_id,
            turn_id=turn_id,
            model_call_id=model_call_id,
        )
        snapshot = McpCatalogActivationSnapshot(
            activation_snapshot_id=snapshot_id,
            snapshot_kind=snapshot_kind,
            owner_session_id=owner_session_id,
            owner_thread_id=owner_thread_id,
            turn_id=turn_id,
            effective_boundary=self._effective_boundary(),
            binding_ref=binding_ref,
            guidance=guidance,
            dispatch_binding=dispatch_binding,
            model_call_id=model_call_id,
            parent_activation_id=(
                parent.activation_snapshot_id if parent is not None else None
            ),
        )
        if self._guidance_source_port is not None:
            self._guidance_source_port.register_tail_only_guidance(
                McpToolGuidanceSourceRegistration(
                    source_id=MCP_GUIDANCE_SOURCE_ID,
                    activation_snapshot_id=snapshot.activation_snapshot_id,
                    catalog_revision=snapshot.binding_ref.catalog_revision,
                    guidance_revision=snapshot.guidance.guidance_revision,
                    provenance_hash=snapshot.activation_provenance_hash,
                )
            )
        await self._saver.save_mcp_catalog_activation_snapshot(snapshot)
        self._last_guidance = guidance
        return snapshot


class McpCatalogActivationBodyStore(Protocol):
    """受保护 snapshot body store 的窄端口；正文不进 mcp 包或 SQLite 列。

    生产实现由 rollout owner 提供（复用既有 ``ContextPlanDetailStore`` 受保护
    detail 文件路径），mcp 包只按 typed identity 提交/读取，不 import CSM、
    不触 SQLite、不建第二 writer。
    """

    def write_activation_snapshot(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str,
        body: Mapping[str, object],
    ) -> None: ...

    def read_activation_snapshot(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str,
    ) -> Mapping[str, object] | None: ...


def mcp_catalog_activation_payload(
    snapshot: McpCatalogActivationSnapshot,
) -> dict[str, object]:
    """把 sealed activation snapshot 序列化为受保护 body store 的确定性正文。"""
    return {
        "schema": _ACTIVATION_HASH_DOMAIN,
        "activation_snapshot_id": snapshot.activation_snapshot_id,
        "snapshot_kind": snapshot.snapshot_kind,
        "owner_session_id": snapshot.owner_session_id,
        "owner_thread_id": snapshot.owner_thread_id,
        "turn_id": snapshot.turn_id,
        "effective_boundary": snapshot.effective_boundary,
        "model_call_id": snapshot.model_call_id,
        "parent_activation_id": snapshot.parent_activation_id,
        "activation_provenance_hash": snapshot.activation_provenance_hash,
        "catalog_binding": snapshot.binding_ref.binding_preimage(),
        "catalog_binding_hash": snapshot.binding_ref.binding_hash,
        "guidance": {
            "catalog_revision": snapshot.guidance.catalog_revision,
            "guidance_revision": snapshot.guidance.guidance_revision,
            "entries": [
                {
                    "tool_id": entry.tool_id,
                    "server_id": entry.server_id,
                    "remote_name": entry.remote_name,
                    "description": entry.description,
                    "args_summary": entry.args_summary,
                }
                for entry in snapshot.guidance.entries
            ],
            "added_tool_ids": list(snapshot.guidance.added_tool_ids),
            "modified_tool_ids": list(snapshot.guidance.modified_tool_ids),
            "tombstones": list(snapshot.guidance.tombstones),
        },
        "extension_dispatch_binding": snapshot.dispatch_binding.to_dict(),
    }


class DurableMcpCatalogActivationSaver:
    """生产 saver：把 sealed activation snapshot 提交到唯一受保护 body store。

    与测试替身相比，本实现是真实生产路径：正文只经 typed body store 落盘，
    同一 snapshot 的 binding/指引/dispatch hash 一次提交；提交失败显式抛出，
    binder 不会得到半发布结果。读取时缺失或损坏一律显式
    ``extension-catalog-unavailable``，绝不回退当前 MCP 目录或同名 target。
    """

    def __init__(
        self,
        *,
        body_store: McpCatalogActivationBodyStore,
        checkpoint_ns: str = "",
    ) -> None:
        self._body_store = body_store
        self._checkpoint_ns = checkpoint_ns

    async def save_mcp_catalog_activation_snapshot(
        self, snapshot: McpCatalogActivationSnapshot
    ) -> None:
        if not isinstance(snapshot, McpCatalogActivationSnapshot):
            raise TypeError(
                "save_mcp_catalog_activation_snapshot 需要 "
                "McpCatalogActivationSnapshot"
            )
        # 提交前重算 dispatch hash，防止被篡改的 snapshot 进入 durable store。
        snapshot.dispatch_binding.verify()
        self._body_store.write_activation_snapshot(
            owner_session_id=snapshot.owner_session_id,
            owner_thread_id=snapshot.owner_thread_id,
            activation_snapshot_id=snapshot.activation_snapshot_id,
            checkpoint_ns=self._checkpoint_ns,
            body=mcp_catalog_activation_payload(snapshot),
        )

    def load_mcp_catalog_activation_snapshot(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str | None = None,
    ) -> ExtensionDispatchBindingRef:
        """恢复 dispatch binding；历史 binding 丢失显式 ``extension-catalog-unavailable``。"""
        body = self._body_store.read_activation_snapshot(
            owner_session_id=owner_session_id,
            owner_thread_id=owner_thread_id,
            activation_snapshot_id=activation_snapshot_id,
            checkpoint_ns=(
                self._checkpoint_ns if checkpoint_ns is None else checkpoint_ns
            ),
        )
        if body is None:
            raise ExtensionCatalogUnavailableError(
                "历史 extension activation snapshot 丢失，拒绝 dispatch: "
                f"activation_snapshot_id={activation_snapshot_id}"
            )
        dispatch_body = body.get("extension_dispatch_binding")
        return ExtensionDispatchBindingRef.from_sealed_snapshot(dispatch_body)
