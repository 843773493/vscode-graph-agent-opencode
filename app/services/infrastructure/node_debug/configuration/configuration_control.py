"""Node Debug 调试方案配置控制链路。

承载方案能力投影、profile 名称解析，以及方案列表/读取/创建/更新/激活/删除/
导入/复制这一条完整的产品入口链路，连同它独占的运行中阻断断言与动作记录收口。
由 NodeDebugService 继承（宿主必须提供 _runtimes、_session_state、
_configuration_registry、_owner_key、_resolve_owner、_admit_mutation、
_append_action、_assert_no_unsettled_claim、_get_typed_debug_runtime_config
与公开入口 get_state），不反向依赖顶层 service.py。
"""

from __future__ import annotations

from typing import Literal

from app.schemas.internal_v2.node_debug import (
    NodeDebugCapabilitiesDTO,
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationDTO,
    NodeDebugConfigurationUpdateRequest,
    NodeDebugLaunchProfileDTO,
    NodeDebugStateDTO,
)
from app.services.infrastructure.node_debug.runtime_state import (
    ACTIVE_NODE_DEBUG_RUNTIME_STATUSES,
    LIVE_NODE_DEBUG_RUNTIME_STATUSES,
)
from app.services.infrastructure.node_debug.session.snapshot import (
    MAX_NODE_DEBUG_ACTIONS,
)
from app.services.infrastructure.node_debug.session.thread_owner import (
    NodeDebugOwner,
)


class NodeDebugConfigurationControlMixin:
    """调试方案配置控制链路的方法族（由 NodeDebugService 继承）。"""

    def get_capabilities(self) -> NodeDebugCapabilitiesDTO:
        """返回供客户端选择启动配置的脱敏调试能力。"""
        debug_config = self._get_typed_debug_runtime_config()
        profiles: list[NodeDebugLaunchProfileDTO] = []
        for name, profile in debug_config.launch_profiles.items():
            profiles.append(
                NodeDebugLaunchProfileDTO(
                    name=name,
                    adapter=profile.adapter,
                    runtime=profile.runtime,
                    supported=(
                        profile.adapter == "node_inspector"
                        and profile.runtime == "node"
                    ),
                    program=profile.program,
                    working_directory=profile.working_directory,
                    args=list(profile.args),
                )
            )
        return NodeDebugCapabilitiesDTO(
            enabled=debug_config.enabled,
            default_adapter=debug_config.default_adapter,
            supported_adapters=["node_inspector"],
            launch_profiles=profiles,
        )

    def resolve_launch_profile_name(self, launch_profile_name: str | None) -> str:
        """把方案/请求里的 profile 名称解析为实际生效的 profile 名称。

        Agent 工具面需要在启动前核对“显式 profile 与方案解析结果一致”，
        因此复用唯一的 typed 启动配置解析规则，避免在工具层
        复制默认 profile 名称形成第二套语义。本方法只读配置，不触碰运行时。
        """
        resolved_name, _ = self._get_typed_debug_runtime_config().resolve_profile(
            launch_profile_name
        )
        return resolved_name

    def list_configurations(
        self,
        session_id: str,
        thread_id: str,
    ) -> list[NodeDebugConfigurationDTO]:
        session_id, thread_id = self._resolve_owner(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        self._configuration_registry.refresh_new_files(session_id, thread_id)
        return self._configuration_registry.list(session_id, thread_id)

    def get_configuration(
        self,
        session_id: str,
        configuration_id: str,
        thread_id: str,
    ) -> NodeDebugConfigurationDTO:
        session_id, thread_id = self._resolve_owner(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        self._configuration_registry.refresh_new_files(session_id, thread_id)
        return self._configuration_registry.get(
            session_id, thread_id, configuration_id
        ).model_copy(deep=True)

    async def create_configuration(
        self,
        request: NodeDebugConfigurationCreateRequest,
        *,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(
            request.session_id, request.thread_id
        )
        self._session_state.ensure_loaded(session_id, thread_id)
        if request.activate:
            self._assert_no_running_target(session_id, thread_id)
        configuration = self._configuration_registry.create(
            request.model_copy(
                update={"session_id": session_id, "thread_id": thread_id}
            ),
        )
        if request.activate:
            self._session_state.sync_active_configuration(session_id, thread_id)
            self._drop_inactive_runtime((session_id, thread_id))
        self._record_session_action(
            session_id,
            "create_configuration",
            f"已创建调试方案 {configuration.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
        )
        self._session_state.write_session_manifest(session_id, thread_id)
        return await self.get_state(session_id, thread_id)

    async def update_configuration(
        self,
        configuration_id: str,
        request: NodeDebugConfigurationUpdateRequest,
        *,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(
            request.session_id, request.thread_id
        )
        self._session_state.ensure_loaded(session_id, thread_id)
        self._configuration_registry.get(session_id, thread_id, configuration_id)
        self._assert_configuration_not_running(
            session_id, thread_id, configuration_id
        )
        replacement = self._configuration_registry.update(
            configuration_id,
            request.model_copy(
                update={"session_id": session_id, "thread_id": thread_id}
            ),
        )
        self._session_state.sync_active_configuration(session_id, thread_id)
        self._record_session_action(
            session_id,
            "update_configuration",
            f"已更新调试方案 {replacement.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
        )
        self._session_state.write_session_manifest(session_id, thread_id)
        return await self.get_state(session_id, thread_id)

    async def activate_configuration(
        self,
        session_id: str,
        configuration_id: str,
        *,
        thread_id: str,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        configuration = self._configuration_registry.get(
            session_id, thread_id, configuration_id
        )
        self._assert_no_running_target(session_id, thread_id)
        self._configuration_registry.activate(
            session_id,
            configuration_id,
            thread_id=thread_id,
        )
        self._session_state.sync_active_configuration(session_id, thread_id)
        self._drop_inactive_runtime((session_id, thread_id))
        self._record_session_action(
            session_id,
            "activate_configuration",
            f"已激活调试方案 {configuration.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
        )
        self._session_state.write_session_manifest(session_id, thread_id)
        return await self.get_state(session_id, thread_id)

    async def delete_configuration(
        self,
        session_id: str,
        configuration_id: str,
        *,
        thread_id: str,
        actor: Literal["human", "ai", "system"] = "human",
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        configuration = self._configuration_registry.get(
            session_id, thread_id, configuration_id
        )
        self._assert_configuration_not_running(
            session_id, thread_id, configuration_id
        )
        was_active = (
            self._configuration_registry.active_id(session_id, thread_id)
            == configuration_id
        )
        self._configuration_registry.remove(
            session_id,
            configuration_id,
            thread_id,
        )
        if was_active:
            self._session_state.clear_pending_breakpoints((session_id, thread_id))
            self._drop_inactive_runtime((session_id, thread_id))
        self._record_session_action(
            session_id,
            "delete_configuration",
            f"已删除调试方案 {configuration.name}",
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
        )
        self._session_state.write_session_manifest(session_id, thread_id)
        return await self.get_state(session_id, thread_id)

    async def import_configuration(
        self,
        session_id: str,
        configuration: NodeDebugConfigurationDTO,
        *,
        thread_id: str,
        activate: bool = False,
        actor: Literal["human", "ai", "system"] = "human",
    ) -> NodeDebugStateDTO:
        session_id, thread_id = await self._admit_mutation(session_id, thread_id)
        self._session_state.ensure_loaded(session_id, thread_id)
        if activate:
            self._assert_no_running_target(session_id, thread_id)
        imported = self._configuration_registry.import_configuration(
            session_id,
            configuration,
            thread_id=thread_id,
            activate=activate,
        )
        if activate:
            self._session_state.sync_active_configuration(session_id, thread_id)
            self._drop_inactive_runtime((session_id, thread_id))
        self._record_session_action(
            session_id,
            "import_configuration",
            f"已导入调试方案 {imported.name}",
            actor=actor,
            thread_id=thread_id,
        )
        self._session_state.write_session_manifest(session_id, thread_id)
        return await self.get_state(session_id, thread_id)

    async def copy_configuration(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        configuration_id: str,
        source_thread_id: str,
        target_thread_id: str,
        name: str | None = None,
        activate: bool = False,
    ) -> NodeDebugConfigurationDTO:
        source_session_id, source_thread_id = await self._admit_mutation(
            source_session_id, source_thread_id
        )
        target_session_id, target_thread_id = await self._admit_mutation(
            target_session_id, target_thread_id
        )
        self._session_state.ensure_loaded(target_session_id, target_thread_id)
        self.get_configuration(
            source_session_id, configuration_id, source_thread_id
        )
        if activate:
            self._assert_no_running_target(target_session_id, target_thread_id)
        copied = self._configuration_registry.copy_configuration(
            source_session_id=source_session_id,
            target_session_id=target_session_id,
            configuration_id=configuration_id,
            source_thread_id=source_thread_id,
            target_thread_id=target_thread_id,
            name=name,
            activate=activate,
        )
        if activate:
            self._session_state.sync_active_configuration(
                target_session_id, target_thread_id
            )
            self._drop_inactive_runtime((target_session_id, target_thread_id))
        self._record_session_action(
            target_session_id,
            "copy_configuration",
            f"已从会话 {source_session_id} 复制调试方案 {copied.name}",
            actor="human",
            thread_id=target_thread_id,
        )
        self._session_state.write_session_manifest(
            target_session_id, target_thread_id
        )
        return copied.model_copy(deep=True)

    def _record_session_action(
        self,
        session_id: str,
        action: str,
        message: str,
        *,
        thread_id: str,
        actor: Literal["human", "ai", "system"],
        tool_name: str | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        owner = self._owner_key(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        if runtime is not None:
            self._append_action(
                runtime,
                action,
                message,
                actor=actor,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
            self._session_state.set_pending_actions(
                owner,
                runtime.actions[-MAX_NODE_DEBUG_ACTIONS:],
            )
            return
        self._session_state.append_pending_action(
            session_id,
            thread_id,
            action,
            message,
            actor=actor,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )

    def _drop_inactive_runtime(self, owner: NodeDebugOwner) -> None:
        runtime = self._runtimes.get(owner)
        if (
            runtime is not None
            and runtime.status not in LIVE_NODE_DEBUG_RUNTIME_STATUSES
        ):
            self._runtimes.pop(owner, None)

    def _assert_no_running_target(self, session_id: str, thread_id: str) -> None:
        owner = self._owner_key(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        if (
            runtime is not None
            and runtime.status in ACTIVE_NODE_DEBUG_RUNTIME_STATUSES
        ):
            raise RuntimeError("目标程序运行中，停止后才能切换调试方案")
        self._assert_no_unsettled_claim(owner, operation="切换调试方案")

    def _assert_configuration_not_running(
        self,
        session_id: str,
        thread_id: str,
        configuration_id: str,
    ) -> None:
        owner = self._owner_key(session_id, thread_id)
        runtime = self._runtimes.get(owner)
        if (
            runtime is not None
            and runtime.configuration_id == configuration_id
            and runtime.status in ACTIVE_NODE_DEBUG_RUNTIME_STATUSES
        ):
            raise RuntimeError("目标程序运行中，不能修改或删除当前调试方案")
        # 与 _assert_no_running_target 的阻面对齐（R3b 建议 3）：冷场景（backend
        # 重启后无在册 runtime）下，未结清的 durable claim（含 reconcile_required）
        # 同样必须阻断方案修改/删除。
        self._assert_no_unsettled_claim(owner, operation="修改或删除当前调试方案")
