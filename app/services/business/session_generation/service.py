"""会话动作的类型、命名与创建规则；调度和运行账本归统一任务 owner。"""

from __future__ import annotations

from jsonschema import Draft202012Validator

from app.core.session_catalog_store.contracts import SessionGenerationOriginDTO
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_navigation import (
    SessionCatalogBreadcrumbDTO,
    SessionGenerationCapabilitiesDTO,
    SessionGenerationCapabilityDTO,
    SessionGenerationExecuteRequest,
)
from app.schemas.internal_v2.session_navigation.models import (
    SessionGenerationDefinitionDTO,
    SessionGenerationPreviewDTO,
    SessionGenerationPreviewRequest,
    SessionGenerationTargetDTO,
    SessionGenerationValidateRequest,
    SessionGenerationValidationDTO,
)
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.session_generation.definition import (
    GenerationProviderUnavailable,
    resolve_generation_provider,
)
from app.services.business.session_generation.naming import render_generation_names
from app.services.business.session_generation.providers import (
    SessionGenerationProviderProtocol,
)
from app.services.business.session_navigation import SessionCatalogService
from app.services.business.session_service import SessionService


class SessionGenerationService:
    def __init__(self, *, session_service: SessionService, session_catalog_service: SessionCatalogService,
                 session_context_fork_service: SessionContextForkService,
                 providers: list[SessionGenerationProviderProtocol]) -> None:
        self._session_service = session_service
        self._session_catalog_service = session_catalog_service
        self._session_context_fork_service = session_context_fork_service
        self._providers = {provider.type_id: provider for provider in providers}
        if len(self._providers) != len(providers):
            raise ValueError("会话生成器 provider type_id 重复")
        for provider in providers:
            Draft202012Validator.check_schema(provider.config_schema)

    def list_capabilities(self) -> SessionGenerationCapabilitiesDTO:
        return SessionGenerationCapabilitiesDTO(
            items=[
                SessionGenerationCapabilityDTO(
                    type_id=provider.type_id,
                    supported_versions=sorted(provider.supported_versions),
                    config_schema=provider.config_schema,
                )
                for provider in sorted(
                    self._providers.values(),
                    key=lambda item: item.type_id,
                )
            ]
        )

    async def preview(self, payload: SessionGenerationPreviewRequest) -> SessionGenerationPreviewDTO:
        """从目标工作区 catalog 解析导航，不创建节点或分配物理 locator。"""
        chains: dict[str, list[str]] = {}
        breadcrumbs: dict[str, SessionCatalogBreadcrumbDTO] = {}
        for node_id, expected_kind in (
            (payload.placement_node_id, "folder" if payload.placement_kind == "session_folder" else "session"),
            (payload.target_session_id, "session"),
        ):
            if node_id is None:
                continue
            breadcrumb = breadcrumbs.get(node_id)
            if breadcrumb is None:
                breadcrumb = await self._session_catalog_service.breadcrumb(node_id)
                breadcrumbs[node_id] = breadcrumb
            node = breadcrumb.items[-1]
            if node.kind != expected_kind or any(item.state != "active" for item in breadcrumb.items):
                raise ValueError(
                    f"生成器节点不可用: node_id={node_id}, expected={expected_kind}, "
                    f"actual={node.kind}, state={node.state}"
                )
            chains[node_id] = [item.name for item in breadcrumb.items]
        if payload.session_strategy == "continue_existing":
            target_session_id = payload.target_session_id
            assert target_session_id is not None
            navigation = chains[target_session_id]
            return SessionGenerationPreviewDTO(
                workspace_id=self._session_catalog_service.workspace_id,
                title=navigation[-1], path_segments=[], navigation_path=navigation,
                physical_location="existing_session", target_session_id=payload.target_session_id,
            )
        title, segments = render_generation_names(payload)
        parent = chains[payload.placement_node_id] if payload.placement_node_id is not None else []
        return SessionGenerationPreviewDTO(
            workspace_id=self._session_catalog_service.workspace_id,
            title=title, path_segments=segments, navigation_path=[*parent, *segments, title],
            physical_location="unallocated", target_session_id=payload.target_session_id,
        )

    async def validate_definition(self, payload: SessionGenerationValidateRequest) -> SessionGenerationValidationDTO:
        self._validate_definition_workspaces(payload)
        workspace_id = self._session_catalog_service.workspace_id
        if payload.execution_workspace_id != workspace_id:
            raise ValueError("生成器定义必须使用目标工作区的稳定身份")
        try:
            resolve_generation_provider(self._providers, payload)
        except GenerationProviderUnavailable as error:
            return SessionGenerationValidationDTO(workspace_id=workspace_id, provider_available=False, reason=str(error))
        await self.preview(SessionGenerationPreviewRequest(
            name=payload.name, title_template=payload.title_template, path_template=payload.path_template,
            session_title=str(payload.config.get("session_title") or payload.name),
            placement_kind=payload.placement.kind,
            placement_node_id=payload.placement.session_id or payload.placement.folder_id,
            session_strategy=payload.session_strategy.mode,
            target_session_id=payload.session_strategy.target.session_id if payload.session_strategy.target is not None else None,
        ))
        threads: set[tuple[str, str]] = set()
        if payload.session_strategy.target is not None:
            target = payload.session_strategy.target
            threads.add((target.session_id, target.thread_id))
        source = payload.context_source
        if source.kind == "live_session":
            assert source.session_id is not None and source.thread_id is not None
            threads.add((source.session_id, source.thread_id))
        for session_id, thread_id in threads:
            async with self._session_service.session_read_admission(session_id):
                self._session_service.path_resolver.resolve_thread_node(session_id, thread_id)
        return SessionGenerationValidationDTO(workspace_id=workspace_id, provider_available=True)

    @staticmethod
    def _validate_definition_workspaces(payload: SessionGenerationDefinitionDTO) -> None:
        execution_workspace_id = payload.execution_workspace_id
        if payload.placement.workspace_id != execution_workspace_id:
            raise ValueError(
                "生成会话挂载工作区与执行工作区不一致: "
                f"placement={payload.placement.workspace_id}, "
                f"execution_workspace_id={execution_workspace_id}"
            )
        target = payload.session_strategy.target
        if target is not None and target.workspace_id != execution_workspace_id:
            raise ValueError(
                "生成策略目标工作区与执行工作区不一致: "
                f"target={target.workspace_id}, execution={execution_workspace_id}"
            )
        context_source = payload.context_source
        if (
            context_source.kind == "live_session"
            and context_source.workspace_id != execution_workspace_id
        ):
            raise ValueError(
                "生成上下文来源工作区与执行工作区不一致: "
                f"context={context_source.workspace_id}, "
                f"execution_workspace_id={execution_workspace_id}"
            )

    def prompt(self, definition: SessionGenerationDefinitionDTO) -> str:
        return resolve_generation_provider(self._providers, definition).build_prompt(definition.config)

    async def create_or_resume(self, payload: SessionGenerationExecuteRequest, *, session_id: str | None) -> SessionDTO:
        self._validate_definition_workspaces(payload)
        mode = payload.session_strategy.mode
        if mode == "continue_existing":
            return await self._session_service.get(self._require_target(payload).session_id)
        if session_id is not None:
            session = await self._session_service.get(session_id)
            self._validate_generation_origin(payload, session)
            return session
        if payload.context_source.kind == "snapshot":
            raise ValueError("当前工作区尚未注册 snapshot 上下文导入 provider")
        origin = SessionGenerationOriginDTO(
            generator_id=payload.generator_id, run_id=payload.run_id, idempotency_key=payload.idempotency_key,
            generator_type_id=payload.generator_type.type_id, generator_type_version=payload.generator_type.version,
        )
        parent_node_id = await self._resolve_placement_folder(payload)
        if mode == "fork_new_and_report_back" or payload.context_source.kind == "live_session":
            target = payload.session_strategy.target
            source_session_id = target.session_id if mode == "fork_new_and_report_back" and target is not None else payload.context_source.session_id
            source_thread_id = target.thread_id if mode == "fork_new_and_report_back" and target is not None else payload.context_source.thread_id
            assert source_session_id is not None
            request = await self._session_context_fork_service.prepare_generation_request(
                source_session_id=source_session_id, source_thread_id=source_thread_id,
                origin=origin, title=payload.title, parent_node_id=parent_node_id,
            )
            return (await self._session_context_fork_service.fork(request, generation_origin=origin)).session
        return await self._session_service.create_generated(
            title=payload.title, agent_id=None, parent_session_id=None,
            generation_origin=origin, parent_node_id=parent_node_id,
        )

    async def _resolve_placement_folder(
        self,
        payload: SessionGenerationExecuteRequest,
    ) -> str | None:
        if payload.placement.workspace_id != payload.execution_workspace_id:
            raise ValueError(
                "生成会话挂载工作区与执行工作区不一致: "
                f"placement={payload.placement.workspace_id}, "
                f"execution={payload.execution_workspace_id}"
            )
        if payload.placement.kind == "session":
            anchor_session_id = payload.placement.session_id
            if anchor_session_id is None:
                raise ValueError("session placement 缺少 session_id")
            anchor = self._session_catalog_service.path_resolver.get_node(
                anchor_session_id
            )
            if anchor.kind != "session":
                raise ValueError(f"生成器挂载目标不是会话: {anchor_session_id}")
            parent_node_id = anchor.node_id
        elif payload.placement.kind == "session_folder":
            parent_node_id = payload.placement.folder_id
            if parent_node_id is None:
                raise ValueError("session_folder placement 缺少 folder_id")
        else:
            parent_node_id = None
        return await self._session_catalog_service.ensure_folder_path(
            payload.navigation_path,
            parent_folder_id=parent_node_id,
        )

    @staticmethod
    def _require_target(
        payload: SessionGenerationExecuteRequest,
    ) -> SessionGenerationTargetDTO:
        target = payload.session_strategy.target
        if target is None:
            raise ValueError(f"{payload.session_strategy.mode} 缺少 target")
        return target

    @staticmethod
    def _validate_generation_origin(
        payload: SessionGenerationExecuteRequest,
        session: SessionDTO,
    ) -> None:
        origin = session.generation_origin
        if (
            origin is None
            or origin.run_id != payload.run_id
            or origin.generator_id != payload.generator_id
            or origin.idempotency_key != payload.idempotency_key
        ):
            raise RuntimeError(
                "生成账本指向的会话来源不匹配: "
                f"session_id={session.session_id}, run_id={payload.run_id}, "
                f"generator_id={payload.generator_id}"
            )
