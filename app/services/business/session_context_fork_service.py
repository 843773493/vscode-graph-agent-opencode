from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace

from pydantic import TypeAdapter

from app.abstractions.session_context import (
    SessionForkTransportProtocol,
    WorkspaceSessionContextAccessError,
)
from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    GraphBinding,
)
from app.core.identifier import create_prefixed_id
from app.core.key_lock_pool import KeyLockPool
from app.core.phase_observer import observe_phase
from app.core.resource_uri import (
    parse_resource_uri,
    parse_session_thread_uri,
    resource_display_uri,
    thread_resource_uri,
)
from app.core.session_catalog_store import SessionCreationRecord
from app.core.session_catalog_store.contracts import SessionGenerationOriginDTO
from app.core.session_catalog_store.creation_envelope import (
    validate_session_creation_envelope,
)
from app.core.session_control_store import ThreadCreationRecord
from app.core.thread_creation import ThreadCreationAbortedError
from app.core.trace_middleware import (
    get_current_gateway_id,
    require_current_gateway_id,
    require_current_request_id,
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.gateway.federation.grants import ForkGrantTarget
from app.schemas.internal_v2.goal import SessionGoalSetRequest
from app.schemas.internal_v2.session import (
    SessionDTO,
    SessionForkExportReleaseDTO,
    SessionForkMode,
    SessionForkOwnerReference,
    SessionForkRequest,
    SessionForkResourceChoice,
    SessionForkResourceResult,
    SessionForkResultDTO,
    SessionForkSourcePointDTO,
)
from app.schemas.internal_v2.session_fork_transfer import (
    ForkSourceCaptureDTO,
    SessionForkTransferRequest,
    SessionForkTransferResultDTO,
)
from app.schemas.internal_v2.workspace_resource import (
    ResourceThreadRef,
    WorkspaceResourceDTO,
)
from app.services.business.session_context_resource import (
    parse_session_context_ref,
    session_resource_identity,
    workspace_session_inventory_uri,
)
from app.services.business.session_goal_service import SessionGoalService
from app.services.business.session_service import SessionService
from app.services.business.workspace_resources.facts import (
    public_error,
    resource_target_facts,
)
from app.services.business.workspace_resources.owner import WorkspaceResourceOwner
from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    check_resource_target,
)
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceTargetContext,
)
from app.services.infrastructure.rollout_context.checkpoint.fork_source import (
    RolloutForkResult,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.fork.source_capture import (
    IncompleteForkSourceCaptureError,
    capture_directory,
)
from app.services.infrastructure.rollout_context.fork.transfer import (
    export_file_batch,
    export_file_chunk,
    source_transfer_request,
    verify_capture_identity,
    verify_capture_receipt,
)
from app.services.orchestration.owner_thread_creation_factory import (
    OwnerThreadCreationFactory,
)

logger = logging.getLogger(__name__)


class SessionContextForkService:
    """冻结公开请求；Session/child 的可见性与资源副作用仍交给原 owner。"""

    def __init__(
        self, *, session_service: SessionService, checkpointer: RolloutCheckpointSaver,
        thread_creation_factory: OwnerThreadCreationFactory, goal_service: SessionGoalService,
        terminal_resource_owner: WorkspaceResourceOwner, browser_resource_owner: WorkspaceResourceOwner,
        transport: SessionForkTransportProtocol | None = None,
    ) -> None:
        self._session_service = session_service
        self._checkpointer = checkpointer
        self._threads = thread_creation_factory
        self._goals = goal_service
        self._transport = transport
        self._terminal_owner = terminal_resource_owner
        self._browser_owner = browser_resource_owner
        self._key_locks = KeyLockPool()

    def _live_owner(self, reference: SessionForkOwnerReference) -> WorkspaceResourceOwner:
        owner = self._terminal_owner if reference.kind == "terminal" else self._browser_owner
        if reference.workspace_id != owner.workspace_id:
            raise ValueError("fork_live_cross_workspace_unsupported: live 资源必须属于原 owner 的同一工作区")
        return owner

    async def _preflight_live_choices(self, request: SessionForkRequest) -> None:
        for choice in request.resources:
            reference = choice.owner_reference
            if reference is None or choice.action == "omit":
                continue
            owner = self._live_owner(reference)
            snapshot = await owner.get_resource(reference.resource_id)
            if snapshot["owner_revision"] != reference.expected_revision:
                raise ValueError("fork_live_revision_conflict: 用户选择的 owner_revision 已变化，请重新读取资源")
            decision = check_resource_target(
                resource_target_facts(snapshot),
                ResourceTargetContext(storage_domain_id=self._session_service.workspace_id,
                                      workspace_id=self._session_service.workspace_id),
                choice=choice.action,
            )
            if not decision.available:
                raise ValueError(f"fork_live_resource_rejected: {decision.reason}")

    async def _settle_live_resources(
        self, request: SessionForkRequest, *, key: str, session_id: str, thread_id: str,
    ) -> list[SessionForkResourceResult]:
        results: list[SessionForkResourceResult] = []
        for choice in request.resources:
            reference = choice.owner_reference
            if reference is None:
                continue
            result = SessionForkResourceResult(
                owner_reference=reference, action=choice.action, availability="unavailable",
                operation_state="not_dispatched", reason="用户未继承该运行资源",
            )
            if choice.action == "omit":
                results.append(result)
                continue
            owner = self._live_owner(reference)
            target = ThreadRef(session_id, thread_id)
            await observe_phase(key, f"fork.live.before_attach.{reference.kind}", details={
                "resource_id": reference.resource_id, "session_id": session_id, "thread_id": thread_id,
            })
            try:
                _, receipt = await owner.attach_for_fork(
                    reference.resource_id, creation_key=key, expected_revision=reference.expected_revision, thread=target,
                )
                result.operation_state = receipt.state
                result.committed_revision = receipt.committed_revision
                # 历史回执只证明提交；当前完整投影必须重新由原 owner 核实。
                current = WorkspaceResourceDTO.from_owner(await owner.get_resource(reference.resource_id))
                result.owner_resource = current
                associated = ResourceThreadRef(session_id=session_id, thread_id=thread_id) in current.thread_associations
                if receipt.state == "committed" and current.reconciliation_status == "verified" and associated:
                    result.availability = "available"
                    result.reason = None
                elif receipt.state == "committed" and not associated:
                    result.reason = "原 attach 已提交；当前 owner 已解除目标关联，不自动重新关联"
                else:
                    result.reason = current.error or receipt.error or "原 owner 尚未核实本次资源关联"
            except Exception as error:
                # 目标已经发布；逐项暴露失败，不把现存目标伪报为回滚或重建。
                logger.exception("fork 已发布目标的 live 资源结算失败: key=%s kind=%s", key, reference.kind)
                receipt = owner.fork_attach_receipt(
                    reference.resource_id, creation_key=key, expected_revision=reference.expected_revision, thread=target,
                )
                result.operation_state = receipt.state if receipt is not None else "not_dispatched"
                result.committed_revision = receipt.committed_revision if receipt is not None else None
                result.reason = public_error(error)
            results.append(result)
            await observe_phase(key, f"fork.live.after_attach.{reference.kind}")
        return results

    @staticmethod
    def _creation_key(idempotency_key: str) -> str:
        return "fork-" + hashlib.sha256(idempotency_key.encode()).hexdigest()

    async def source_point(
        self, *, session_id: str, thread_id: str | None = None,
        turn_id: str | None = None, item_id: str | None = None,
        source_view_id: str | None = None, source_view_revision: int | None = None,
        mode: SessionForkMode = "view_prefix_fork", anchor_mode: str = "inclusive",
    ) -> SessionForkSourcePointDTO:
        session = await self._session_service.get(session_id)
        if thread_id is None:
            thread_id = await self._session_service.resolve_main_thread(session_id)
        owner = self._checkpointer.forks.source.resolve_thread_owner(session_id, thread_id)
        if (source_view_id is None) != (source_view_revision is None):
            raise ValueError("来源必须同时携带 source view/revision")
        if source_view_id is not None and turn_id is None and item_id is None:
            point = await self._checkpointer.forks.source.preflight_fork(
                source_session_id=session_id, source_owner=owner,
                mode="full_rollout_copy" if mode == "view_prefix_fork" and item_id is None else mode,
                source_view_id=source_view_id, source_view_revision=source_view_revision,
                anchor_item_id=item_id if mode == "view_prefix_fork" else None,
                anchor_mode="inclusive" if mode == "view_prefix_fork" and item_id is None else anchor_mode,
            )
        else:
            point = await self._checkpointer.forks.source.freeze_display_point(
                source_session_id=session_id, source_owner=owner, turn_id=turn_id, item_id=item_id,
                source_view_id=source_view_id, source_view_revision=source_view_revision,
                mode=mode, anchor_mode=anchor_mode,
            )
        if point.source_view_id is None or point.source_view_revision is None:
            raise RuntimeError("fork source point 缺少真实 view/revision")
        resources = await self._checkpointer.forks.source.project_display_resources(
            source_session_id=session_id, source_owner=owner, mode=mode, anchor_mode=anchor_mode,
            source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
            anchor_item_id=point.anchor_item_id,
            source_uri=thread_resource_uri(session.workspace_id, session_id, thread_id),
        ) if mode != "view_prefix_fork" or point.anchor_item_id is not None else ()
        return SessionForkSourcePointDTO(
            source_uri=thread_resource_uri(session.workspace_id, session_id, thread_id),
            source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
            anchor_item_id=point.anchor_item_id, child_container_uri=session.uri,
            main_container_uri=workspace_session_inventory_uri(session.workspace_id),
            title=f"{session.title}（上下文分支）", goal_available=await self._goals.get(session_id) is not None,
            resources=list(resources), resource_mode=mode, resource_anchor_mode=anchor_mode,
            live_resources=[WorkspaceResourceDTO.from_owner(snapshot)
                            for owner in (self._terminal_owner, self._browser_owner)
                            for snapshot in await owner.list_resources()],
        )

    async def prepare_generation_request(
        self, *, source_session_id: str, source_thread_id: str | None,
        origin: SessionGenerationOriginDTO, title: str, parent_node_id: str | None,
    ) -> SessionForkRequest:
        """生成策略只冻结同一公开请求；重试先取原 point，不追随 source latest。"""
        idempotency_key = "context-fork-generation:" + hashlib.sha256(canonical_json_bytes(origin.model_dump(mode="json"))).hexdigest()
        gateway_id = get_current_gateway_id()
        target_uri = (
            resource_display_uri(
                scope="workspace", scope_id=self._session_service.workspace_id,
                kind="folder", tail_segments=(parent_node_id,), gateway_authority=gateway_id,
            )
            if parent_node_id is not None
            else workspace_session_inventory_uri(self._session_service.workspace_id, gateway_authority=gateway_id)
        )
        try:
            existing = self._session_service.path_resolver.catalog_store.get_creation_record(self._creation_key(idempotency_key))
        except KeyError:
            existing = None
        if existing is not None:
            if existing.purpose != "context_fork" or existing.request_intent_json is None:
                raise RuntimeError("generation fork key 与创建 owner 冲突")
            intent = json.loads(existing.request_intent_json)
            frozen = SessionForkRequest.model_validate(intent["request"])
            if (intent["source_session_id"] != source_session_id
                or source_thread_id is not None and intent["source_thread_id"] != source_thread_id
                or intent["generation_origin"] != origin.model_dump(mode="json")
                or frozen.title != title or frozen.target_container_uri != target_uri
                or frozen.creation_kind != "new_session_main" or frozen.mode != "context_fork"):
                raise RuntimeError("generation fork 同稳定 key 的请求意图冲突")
            return frozen
        if source_thread_id is None:
            source_thread_id = await self._session_service.resolve_main_thread(source_session_id)
        owner = self._checkpointer.forks.source.resolve_thread_owner(source_session_id, source_thread_id)
        point = await self._checkpointer.forks.source.preflight_fork(source_session_id=source_session_id, source_owner=owner)
        if point.source_view_id is None or point.source_view_revision is None:
            raise RuntimeError("generation fork 缺少已完成 source point")
        return SessionForkRequest(
            idempotency_key=idempotency_key,
            source_uri=thread_resource_uri(self._session_service.workspace_id, source_session_id, source_thread_id,
                                           gateway_authority=gateway_id),
            source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
            anchor_item_id=point.anchor_item_id, mode="context_fork", target_container_uri=target_uri,
            creation_kind="new_session_main", title=title,
        )

    async def fork(
        self, request: SessionForkRequest, *, generation_origin: SessionGenerationOriginDTO | None = None,
    ) -> SessionForkResultDTO:
        gateway_id = get_current_gateway_id()
        source, source_session_id, source_thread_id = parse_session_thread_uri(
            request.source_uri, local_gateway_id=gateway_id,
        )
        target = parse_resource_uri(request.target_container_uri, local_gateway_id=gateway_id)
        if target.scope != "workspace":
            raise ValueError("fork target 必须是明确的 Workspace/Session 容器")
        if any(choice.owner_reference is not None and choice.action != "omit" for choice in request.resources) and (
            source.scope_id != target.scope_id
            or (source.gateway_authority or gateway_id) != (target.gateway_authority or gateway_id)
        ):
            raise ValueError("fork_live_cross_workspace_unsupported: live Terminal/Browser 身份与实例不可跨工作区复制")
        if generation_origin is not None and request.creation_kind != "new_session_main":
            raise ValueError("生成策略必须显式创建新 Session main")
        if target.gateway_authority is not None or target.scope_id != self._session_service.workspace_id:
            if self._transport is None:
                raise RuntimeError("fork_transport_unavailable: 指定目标未装配资源导入 owner")
            origin = require_current_gateway_id("fork target transport")
            source_gateway_id = source.gateway_authority or origin
            if (
                source_thread_id is None
                and source.gateway_authority is None
                and source.scope_id == self._session_service.workspace_id
            ):
                source_thread_id = await self._session_service.resolve_main_thread(source_session_id)
            source_uri = (
                thread_resource_uri(
                    source.scope_id, source_session_id, source_thread_id,
                    gateway_authority=source_gateway_id,
                )
                if source_thread_id is not None else resource_display_uri(
                    scope=source.scope, scope_id=source.scope_id, kind=source.kind,
                    tail_segments=source.tail_segments, gateway_authority=source_gateway_id,
                )
            )
            target_uri = resource_display_uri(
                scope=target.scope, scope_id=target.scope_id, kind=target.kind,
                tail_segments=target.tail_segments, gateway_authority=target.gateway_authority or origin,
            )
            qualified = request.model_copy(update={
                "source_uri": source_uri,
                "target_container_uri": target_uri,
            })
            target_session_id = (
                parse_session_thread_uri(target.display_uri)[1]
                if request.creation_kind == "new_child_thread" else None
            )
            audience = target.gateway_authority or origin
            reply = await self._transport.fork_transfer(SessionForkTransferRequest(
                source_gateway_id=origin,
                fork_target=ForkGrantTarget(gateway_id=audience, uri=qualified.target_container_uri, fork_role="target"),
                action="create", request=qualified, generation_origin=generation_origin,
            ))
            try:
                if reply.result is None or reply.result.session.workspace_id != target.scope_id:
                    raise RuntimeError("fork target owner 未返回固定目标 Workspace 的真实发布结果")
                if reply.result.target_gateway_id != audience:
                    raise RuntimeError("fork target owner 返回的 Gateway 身份与固定目标不一致")
                if reply.result.creation_kind != request.creation_kind:
                    raise RuntimeError("fork target owner 返回的创建意图与固定请求不一致")
                if reply.result.mode != qualified.mode:
                    raise RuntimeError("fork target owner 返回的 mode 与固定请求不一致")
                self._validate_source_response(qualified, reply.result.source_uri, reply.result)
                if target_session_id is not None and reply.result.session.session_id != target_session_id:
                    raise RuntimeError("fork target owner 返回的 child 不属于固定目标 Session")
                session_ref = parse_session_context_ref(
                    resource_identity=reply.result.session.resource_identity,
                    uri=reply.result.session.uri, scope=reply.result.session.scope,
                    local_gateway_id=audience,
                )
                if (
                    session_ref.gateway_authority is not None or session_ref.scope != "workspace"
                    or session_ref.scope_id != target.scope_id or session_ref.kind != "session"
                    or session_ref.session_id != reply.result.session.session_id
                ):
                    raise RuntimeError("fork target owner 返回的 Session URI 与固定目标身份不一致")
                if session_ref.resource_identity != session_resource_identity(
                    target.scope_id, reply.result.session.session_id,
                ):
                    raise RuntimeError("fork target owner 返回的 Session resource_identity 与固定目标身份不一致")
                expected_thread_uri = thread_resource_uri(
                    target.scope_id, reply.result.session.session_id, reply.result.thread_id, gateway_authority=audience,
                )
                if reply.result.thread_uri != expected_thread_uri:
                    raise RuntimeError("fork target owner 返回的 Thread URI 与固定目标身份不一致")
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(f"fork_response_protocol_invalid: 远端 target 发布回执协议无效: {error}") from error
            return reply.result
        # 在权威创建 owner 冻结前统一补齐 Gateway；本机 authority 的两种写法共用同一请求。
        request = request.model_copy(update={
            "source_uri": resource_display_uri(
                scope=source.scope, scope_id=source.scope_id, kind=source.kind,
                tail_segments=source.tail_segments, gateway_authority=source.gateway_authority or gateway_id,
            ),
            "target_container_uri": resource_display_uri(
                scope=target.scope, scope_id=target.scope_id, kind=target.kind,
                tail_segments=target.tail_segments, gateway_authority=target.gateway_authority or gateway_id,
            ),
        })
        parent_node_id, target_session_id = self._target_container(request, target)
        key = self._creation_key(request.idempotency_key)
        async with self._key_locks.lock_for(key):
            if request.creation_kind == "new_child_thread":
                if target_session_id is None:
                    raise ValueError("new_child_thread 必须指定已有 Session/Thread 容器")
                return await self._fork_child(request, key, target_session_id, source_session_id, source_thread_id)
            return await self._fork_main(request, key, parent_node_id, source_session_id, source_thread_id, generation_origin)

    async def transfer(self, payload: SessionForkTransferRequest) -> SessionForkTransferResultDTO:
        """只消费 federation 已校验的固定 role；任何不匹配都先于业务访问。"""
        gateway_id = require_current_gateway_id("fork transfer workspace owner")
        target = parse_resource_uri(payload.fork_target.uri, local_gateway_id=gateway_id)
        if payload.fork_target.gateway_id != gateway_id or target.gateway_authority is not None or target.scope_id != self._session_service.workspace_id:
            raise ValueError("fork transfer 固定 audience/workspace 与实际 owner 不一致")
        source = parse_resource_uri(payload.request.source_uri)
        destination = parse_resource_uri(payload.request.target_container_uri)
        if source.gateway_authority is None or destination.gateway_authority is None:
            raise ValueError("fork transfer 必须显式绑定来源和目标 Gateway/Workspace，禁止激活工作区回退")
        if payload.action == "create":
            result = await self.fork(payload.request, generation_origin=payload.generation_origin)
            return SessionForkTransferResultDTO(result=result)
        if destination.gateway_authority != payload.source_gateway_id:
            raise ValueError("fork source export 的目标 Gateway 与授权 origin 不一致")
        _, source_session_id, source_thread_id = parse_session_thread_uri(payload.request.source_uri)
        if payload.action == "intent":
            intent = await self._new_intent(payload.request, source_session_id, source_thread_id, None)
            target_creation = None
            if destination.scope_id == self._session_service.workspace_id:
                key = self._creation_key(payload.request.idempotency_key)
                try:
                    if payload.request.creation_kind == "new_session_main":
                        target_creation = self._session_service.path_resolver.catalog_store.get_creation_record(key)
                    else:
                        _, target_session_id, _ = parse_session_thread_uri(payload.request.target_container_uri)
                        target_creation = self._threads.for_owner_session(target_session_id).control_store.get_thread_creation_record(key)
                except KeyError:
                    # 没有本地 target 是普通独立来源；不扫描磁盘或注册第二份身份。
                    pass
            return SessionForkTransferResultDTO(intent={
                **intent, "source_owner_instance_id": self._checkpointer.forks.instance_id,
                "source_storage_domain_id": self._session_service.path_resolver.catalog_store.get_node(source_session_id).workspace_id,
                "source_target_creation_preimage_hash": target_creation.preimage_hash if target_creation is not None else None,
            })
        expected = payload.capture.to_record()
        frozen = json.loads(expected.request_json)
        owner = self._checkpointer.forks
        catalog = self._session_service.path_resolver.catalog_store
        local_capture = catalog.get_fork_source_capture(copy_operation_id=expected.copy_operation_id)
        if local_capture is not None and owner._capture_target_creation(local_capture) is not None:
            raise ValueError("fork_owner_overlap: 本地 creation 的 capture 只由当前 owner 执行，拒绝外域导出或释放")
        source_domain = self._session_service.workspace_id
        target_domain = frozen["target_storage_domain_id"]
        if source_domain != source.scope_id or target_domain != destination.scope_id or source_domain == target_domain:
            raise ValueError("fork_owner_overlap: source export 的稳定资源域与实际 owner 不一致")
        if (expected.copy_operation_id != self._creation_key(payload.request.idempotency_key)
            or expected.creation_key != expected.copy_operation_id
            or (expected.source_session_id, expected.source_thread_id) != (source_session_id, source_thread_id)
            or frozen != {
                "source_uri": payload.request.source_uri,
                "source_view_id": payload.request.source_view_id,
                "source_view_revision": payload.request.source_view_revision,
                "anchor_item_id": payload.request.anchor_item_id,
                "mode": payload.request.mode, "anchor_mode": payload.request.anchor_mode,
                "relationship": payload.request.relationship, "checkpoint_ns": "",
                "creation_kind": payload.request.creation_kind,
                "resources": [choice.model_dump(mode="json") for choice in payload.request.resources if choice.owner_reference is None],
                "source_storage_domain_id": source_domain,
                "target_storage_domain_id": target_domain,
                "target_gateway_id": destination.gateway_authority,
                "target_main_thread_id": frozen.get("target_main_thread_id"),
                "creation_preimage_hash": frozen.get("creation_preimage_hash"),
            }
            or not frozen.get("target_main_thread_id") or not frozen.get("creation_preimage_hash")):
            raise ValueError("fork source export 的冻结请求/owner/domain 与授权请求不一致")
        if payload.request.creation_kind == "new_child_thread":
            _, target_session_id, _ = parse_session_thread_uri(payload.request.target_container_uri)
            if target_session_id != expected.target_session_id:
                raise ValueError("fork source export target Session 与冻结 capture 不一致")
        async with self._key_locks.lock_for("export:" + expected.copy_operation_id):
            if payload.action == "capture":
                await observe_phase(expected.copy_operation_id, "copy.source.export_authorized", details={
                    "gateway_id": gateway_id, "same_storage_owner": False,
                })
                capture = await owner.export_source_capture(expected)
                verify_capture_receipt(expected, ForkSourceCaptureDTO.from_record(capture))
            else:
                capture = self._session_service.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=expected.copy_operation_id)
                if capture is None:
                    raise KeyError("fork source export 缺少本 operation 的冻结 capture")
                verify_capture_identity(expected, ForkSourceCaptureDTO.from_record(capture))
                if payload.action == "release":
                    capture = await owner.release_source_export(capture, committed=payload.release_state == "committed")
                    await observe_phase(expected.copy_operation_id, "copy.source.released")
                else:
                    verify_capture_receipt(expected, ForkSourceCaptureDTO.from_record(capture))
                    sessions_root = self._checkpointer._storage.storage.sessions_dir
                    if payload.action == "files":
                        return export_file_batch(sessions_root, capture, payload.cursor)
                    if payload.action == "chunk":
                        return export_file_chunk(sessions_root, capture, payload.file_name, payload.offset)
                    raise ValueError("fork transfer action 未派发")
            return SessionForkTransferResultDTO(capture=ForkSourceCaptureDTO.from_record(capture))

    def _target_container(self, request: SessionForkRequest, target) -> tuple[str | None, str | None]:
        if request.creation_kind == "new_child_thread":
            _, session_id, thread_id = parse_session_thread_uri(
                target.display_uri, local_gateway_id=get_current_gateway_id(),
            )
            if thread_id is not None:
                self._session_service.path_resolver.resolve_thread_node(session_id, thread_id)
            else:
                self._session_service.path_resolver.resolve_session_node(session_id)
            return None, session_id
        if target.kind == "session" and not target.tail_segments:
            return None, None
        if target.kind == "folder":
            node = self._session_service.path_resolver.catalog_store.get_node(target.tail_segments[0])
            if node.kind != "folder" or node.state != "active":
                raise ValueError("fork target Folder 不存在或不可创建")
            return node.node_id, None
        _, session_id, thread_id = parse_session_thread_uri(
            target.display_uri, local_gateway_id=get_current_gateway_id(),
        )
        if thread_id is not None:
            raise ValueError("新 Session 的导航容器不能是 Thread")
        self._session_service.path_resolver.resolve_session_node(session_id)
        return session_id, None

    @staticmethod
    def _validate_frozen_request(
        intent: dict[str, object], request: SessionForkRequest, generation_origin: SessionGenerationOriginDTO | None,
    ) -> None:
        origin = generation_origin.model_dump(mode="json") if generation_origin is not None else None
        if intent["request"] != request.model_dump(mode="json") or intent["generation_origin"] != origin:
            raise RuntimeError("fork request conflict: 同 idempotency_key 不同冻结请求")

    @staticmethod
    def _validate_source_response(
        request: SessionForkRequest, source_uri: str, point: RolloutForkResult | SessionForkResultDTO,
    ) -> tuple[str, str]:
        expected, expected_session, expected_thread = parse_session_thread_uri(request.source_uri)
        actual, actual_session, actual_thread = parse_session_thread_uri(source_uri)
        if (
            (actual.gateway_authority, actual.scope_id, actual_session)
            != (expected.gateway_authority, expected.scope_id, expected_session)
            or actual_thread is None
            or (expected_thread is not None and actual_thread != expected_thread)
        ):
            raise RuntimeError("fork owner 返回的来源 URI 与固定请求不一致")
        if (point.source_view_id, point.source_view_revision) != (request.source_view_id, request.source_view_revision):
            raise RuntimeError("fork owner 返回的来源 view/revision 与固定请求不一致")
        # context_fork 未指定 anchor 时仍由来源 owner 解析安全截止点。
        if (request.mode != "context_fork" or request.anchor_item_id is not None) and point.anchor_item_id != request.anchor_item_id:
            raise RuntimeError("fork owner 返回的来源 anchor 与固定请求不一致")
        return actual_session, actual_thread

    async def _request_source_intent(self, request: SessionForkRequest) -> tuple[dict[str, object], str, str, str | None]:
        """临时 instance 回执只用于本次活 owner 核实，不进入 creation/capture。"""
        if self._transport is None:
            raise RuntimeError("fork_transport_unavailable: 指定来源未装配资源导出 owner")
        source = parse_resource_uri(request.source_uri)
        reply = await self._transport.fork_transfer(source_transfer_request(request, None, action="intent"))
        if reply.intent is None or reply.intent.get("request") != request.model_dump(mode="json"):
            raise RuntimeError("fork source owner 未返回同一冻结请求的真实来源意图")
        try:
            intent = dict(reply.intent)
            owner_instance_id = TypeAdapter(str).validate_python(intent.pop("source_owner_instance_id"), strict=True)
            storage_domain_id = TypeAdapter(str).validate_python(intent.pop("source_storage_domain_id"), strict=True)
            target_preimage = TypeAdapter(str | None).validate_python(intent.pop("source_target_creation_preimage_hash"), strict=True)
            if not owner_instance_id:
                raise ValueError("fork source intent 缺少实际 owner instance 握手")
            if storage_domain_id != source.scope_id:
                raise ValueError("fork source intent 的实际存储域与寻址 Workspace 不一致")
            for field in (
                "source_gateway_id", "source_uri", "source_point", "source_session_id", "source_thread_id",
                "source_graph_binding", "display_title", "source_agent_id", "goal_snapshot",
            ):
                if field not in intent:
                    raise KeyError(f"fork source intent 缺少字段: {field}")
            if intent["source_gateway_id"] != source.gateway_authority:
                raise RuntimeError("fork source owner 返回了其它 Gateway，拒绝改绑")
            point = RolloutForkResult(**intent["source_point"])
            point = TypeAdapter(RolloutForkResult).validate_json(json.dumps(asdict(point)), strict=True)
            frozen_owner = self._validate_source_response(
                request, intent["source_uri"], point,
            )
            if (intent["source_session_id"], intent["source_thread_id"]) != frozen_owner:
                raise RuntimeError("fork source owner 返回的 Session/Thread 与来源 URI 不一致")
            GraphBinding(**intent["source_graph_binding"])
            for field in ("display_title", "source_agent_id"):
                TypeAdapter(str).validate_python(intent[field], strict=True)
            goal_snapshot = intent["goal_snapshot"]
            if goal_snapshot is not None:
                TypeAdapter(str).validate_python(goal_snapshot["goal_id"], strict=True)
                goal_settings = SessionGoalSetRequest.model_validate({
                    "objective": goal_snapshot["objective"], "token_budget": goal_snapshot["token_budget"],
                }, strict=True)
                if goal_settings.objective is None or not goal_settings.objective.strip():
                    raise ValueError("fork source intent 的 Goal objective 不能为空")
        except (KeyError, TypeError, ValueError) as error:
            detail = (
                f"fork_response_protocol_invalid: 远端 source intent 回执协议无效: "
                f"operation_id={self._creation_key(request.idempotency_key)}, "
                f"gateway_id={source.gateway_authority}, workspace_id={source.scope_id}, "
                f"stage=source_intent_validation, error={type(error).__name__}"
            )
            logger.error("%s；完整诊断见异常链", detail, exc_info=error)
            raise RuntimeError(detail) from error
        return intent, owner_instance_id, storage_domain_id, target_preimage

    async def _new_intent(
        self, request: SessionForkRequest, source_session_id: str, source_thread_id: str | None,
        generation_origin: SessionGenerationOriginDTO | None,
    ) -> dict[str, object]:
        await self._preflight_live_choices(request)
        source = parse_resource_uri(request.source_uri, local_gateway_id=get_current_gateway_id())
        if source.gateway_authority is not None or source.scope_id != self._session_service.workspace_id:
            if self._transport is None:
                raise RuntimeError("fork_transport_unavailable: 指定来源未装配资源导出 owner")
            if request.relationship == "pinned":
                raise ValueError("fork_remote_retention_unsupported: 来源端未提供独立 pinned retention 授权")
            qualified = request.model_copy(update={
                "source_uri": resource_display_uri(scope=source.scope, scope_id=source.scope_id, kind=source.kind,
                    tail_segments=source.tail_segments, gateway_authority=source.gateway_authority or require_current_gateway_id("fork source identity")),
                "target_container_uri": self._qualified_target_uri(request),
            })
            intent, source_instance_id, source_domain, _ = await self._request_source_intent(qualified)
            same_owner = self._checkpointer.forks.verify_source_domain(
                source_domain, self._session_service.workspace_id, source_instance_id,
            )
            intent["request"] = request.model_dump(mode="json")
            intent["source_remote"] = True
            # 活 owner 握手只发生在冻结 creation 前；持久决定随原 creation preimage 固定。
            intent["capture_decision"] = {
                "source_storage_domain_id": source_domain,
                "target_storage_domain_id": self._session_service.workspace_id,
                "capture_owner": "local" if same_owner else "source_export",
            }
            intent["target_gateway_id"] = get_current_gateway_id()
            intent["generation_origin"] = generation_origin.model_dump(mode="json") if generation_origin is not None else None
            return intent
        session = await self._session_service.get(source_session_id)
        if source_thread_id is None:
            source_thread_id = await self._session_service.resolve_main_thread(source_session_id)
        source_owner = self._checkpointer.forks.source.resolve_thread_owner(source_session_id, source_thread_id)
        point = await self._checkpointer.forks.source.preflight_fork(
            source_session_id=source_session_id, source_owner=source_owner, mode=request.mode,
            source_view_id=request.source_view_id, source_view_revision=request.source_view_revision,
            anchor_item_id=request.anchor_item_id, anchor_mode=request.anchor_mode,
        )
        binding = self._threads.for_owner_session(source_session_id).control_store.get_thread_graph_binding(source_thread_id)
        if binding is None or binding.as_tuple() != DEEP_AGENT_GRAPH_BINDING.selector_fields():
            raise RuntimeError("fork_source_graph_binding_unavailable: 来源持久 GraphBinding 不能由当前代码重建")
        goal_snapshot = None
        if request.goal is not None:
            goal = await self._goals.get(source_session_id)
            if goal is None:
                raise ValueError("fork source Session 没有可继承的 Goal")
            goal_snapshot = {"goal_id": create_prefixed_id("goal"), "objective": goal.objective,
                             "token_budget": request.goal.token_budget}
        return {
            "request": request.model_dump(mode="json"),
            "generation_origin": generation_origin.model_dump(mode="json") if generation_origin is not None else None,
            "source_uri": thread_resource_uri(session.workspace_id, source_session_id, source_thread_id,
                                               gateway_authority=parse_resource_uri(request.source_uri).gateway_authority),
            "source_session_id": source_session_id, "source_thread_id": source_thread_id,
            "source_gateway_id": get_current_gateway_id(), "target_gateway_id": get_current_gateway_id(),
            "source_remote": False,
            "capture_decision": {
                "source_storage_domain_id": session.workspace_id,
                "target_storage_domain_id": self._session_service.workspace_id,
                "capture_owner": "local",
            },
            "source_point": asdict(point), "source_graph_binding": asdict(binding),
            "display_title": request.title or f"{session.title}（上下文分支）",
            "source_agent_id": session.current_agent_id, "goal_snapshot": goal_snapshot,
        }

    async def _materialize(
        self, request: SessionForkRequest, intent: dict[str, object], target_session_id: str,
        *, creation: SessionCreationRecord | None = None, child: ThreadCreationRecord | None = None,
    ) -> RolloutForkResult:
        source_owner = MutationIntentOwner(session_id=intent["source_session_id"], thread_id=intent["source_thread_id"])
        point = RolloutForkResult(**intent["source_point"])
        if creation is not None:
            key = creation.session_creation_idempotency_key
        elif child is not None:
            key = child.thread_creation_idempotency_key
        else:
            raise ValueError("fork materialization 必须由已冻结的目标 creation record 发起")
        owner = self._checkpointer.forks
        source_domain, target_domain, same_owner = owner.require_creation_capture_decision(
            creation or child, target_session_id=target_session_id, intent=intent,
        )
        source_external = not same_owner
        if source_external and self._transport is None:
            raise RuntimeError("fork_transport_unavailable: 冻结 export 来源缺少明确 transport，禁止本地回退")
        resource_choices = owner.materialized_resource_choices(request, intent)
        transfer_request = request.model_copy(update={
            "source_uri": intent["source_uri"], "target_container_uri": self._qualified_target_uri(request),
            "source_view_id": point.source_view_id, "source_view_revision": point.source_view_revision,
            "anchor_item_id": point.anchor_item_id, "resources": resource_choices,
        }) if source_external else None
        catalog = self._session_service.path_resolver.catalog_store
        capture = catalog.get_fork_source_capture(copy_operation_id=key)
        source_owner_instance_id = None
        if source_external and capture is None:
            fresh, source_owner_instance_id, actual_domain, source_target_preimage = await self._request_source_intent(transfer_request)
            if source_owner_instance_id != owner.instance_id and source_target_preimage == (creation or child).preimage_hash:
                raise ValueError("fork_owner_overlap: 不同活 owner 命中同一冻结 target creation，拒绝建立 capture")
            if actual_domain != source_domain or owner.verify_source_domain(actual_domain, target_domain, source_owner_instance_id):
                raise ValueError("fork_owner_overlap: 本次 source owner 与原 creation 的 export 决定不一致")
            if any(fresh[field] != intent[field] for field in (
                "source_uri", "source_session_id", "source_thread_id", "source_gateway_id",
                "source_point", "source_graph_binding",
            )):
                raise RuntimeError("fork source owner handshake 与冻结来源 point/owner 不一致")
        if capture is not None:
            frozen = json.loads(capture.request_json)
            if (frozen["source_storage_domain_id"], frozen["target_storage_domain_id"]) != (source_domain, target_domain):
                raise ValueError("fork_owner_overlap: capture 与原 creation 的实际存储归属不一致")
        await observe_phase(key, "copy.target.prepared", details={
            "gateway_id": get_current_gateway_id(), "same_storage_owner": same_owner,
        })
        return await self._checkpointer.forks.afork(
            source_session_id=source_owner.session_id, target_session_id=target_session_id,
            source_uri=intent["source_uri"], source_owner=source_owner,
            creation_record=creation, thread_creation_record=child,
            mode=request.mode, anchor_mode=request.anchor_mode, relationship=request.relationship,
            source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
            anchor_item_id=point.anchor_item_id, source_preflight=point,
            resource_choices=[choice.model_dump(mode="json") for choice in resource_choices],
            source_storage_domain_id=source_domain, target_storage_domain_id=target_domain,
            source_transport=self._transport if source_external else None,
            transfer_request=transfer_request, source_owner_instance_id=source_owner_instance_id,
        )

    def _qualified_target_uri(self, request: SessionForkRequest) -> str:
        target = parse_resource_uri(request.target_container_uri)
        return resource_display_uri(scope=target.scope, scope_id=target.scope_id, kind=target.kind,
                                    tail_segments=target.tail_segments,
                                    gateway_authority=target.gateway_authority or require_current_gateway_id("fork target identity"))

    async def _finish_remote_export(self, intent: dict[str, object], key: str) -> None:
        if not intent["source_remote"]:
            return
        catalog = self._session_service.path_resolver.catalog_store
        capture = catalog.get_fork_source_capture(copy_operation_id=key)
        if capture is None:
            return
        self._checkpointer.forks.require_target_capture(capture)
        state, _ = catalog.get_fork_export_release(key)
        if state is None:
            return
        if state == "confirmed":
            return
        if state != "pending":
            raise RuntimeError("fork 远端 capture 缺少目标释放 journal")
        frozen = json.loads(capture.request_json)
        if frozen["creation_kind"] == "new_session_main":
            creation = catalog.get_creation_record(capture.creation_key)
        else:
            creation = self._threads.for_owner_session(capture.target_session_id).control_store.get_thread_creation_record(capture.creation_key)
        # 服务/task 中断保留 preparing；只有 target owner 的持久终态能释放来源。
        if creation.state == "preparing":
            return
        if creation.state not in {"published", "aborted"}:
            raise RuntimeError(f"fork export release 的 target creation 状态非法: {creation.state}")
        committed = creation.state == "published"
        if capture.state != ("materialized" if committed else "aborted"):
            raise RuntimeError("fork export release 的 target capture 与 creation 终态不一致")
        frozen_request = SessionForkRequest.model_validate(intent["request"])
        request = frozen_request.model_copy(update={
            "source_uri": frozen["source_uri"], "target_container_uri": self._qualified_target_uri(frozen_request),
            "source_view_id": frozen["source_view_id"], "source_view_revision": frozen["source_view_revision"],
            "anchor_item_id": frozen["anchor_item_id"],
            "resources": [SessionForkResourceChoice.model_validate(choice) for choice in frozen["resources"]],
        })
        if self._transport is None:
            raise RuntimeError("fork_transport_unavailable: 无法释放冻结远端 export")
        payload = source_transfer_request(
            request, capture, action="release", release_state="committed" if committed else "aborted",
        )

        def record_pending(error: BaseException) -> None:
            reason = (
                f"fork 来源 export 释放待确认: operation_id={key}, "
                f"session_id={capture.target_session_id}, thread_id={capture.target_thread_id}, "
                f"stage=source_export_release, error={type(error).__name__}"
            )
            for name in ("errno", "winerror", "sqlite_errorcode"):
                if isinstance(code := getattr(error, name, None), int):
                    reason += f", {name}={code}"
            catalog.record_fork_export_release(capture, error=reason)
            logger.error("%s；完整诊断见异常链", reason, exc_info=error)

        try:
            reply = await self._transport.fork_transfer(payload)
        except asyncio.CancelledError as error:
            record_pending(error)
            raise
        except (WorkspaceSessionContextAccessError, ConnectionError, TimeoutError) as error:
            # Gateway 的 relay 可能在来源 owner 已提交释放后丢失响应；保留传输错误为 pending。
            record_pending(error)
            return
        released = verify_capture_identity(capture, reply.capture)
        if released.state != ("materialized" if committed else "aborted"):
            raise RuntimeError("fork source export 未返回同 operation 的终结回执")
        catalog.record_fork_export_release(capture, receipt=released)

    @contextmanager
    def _delivery_context(
        self, intent: dict[str, object], request_id: str | None,
    ) -> Iterator[None]:
        """复用目标 creation record 冻结的 Trace，不改变任何业务 identity。"""
        gateway_id = intent["target_gateway_id"]
        if gateway_id is not None and (not isinstance(gateway_id, str) or not gateway_id.strip()):
            raise RuntimeError("fork creation record 的冻结 Gateway 身份非法")
        if intent["source_remote"] and (
            gateway_id is None or not isinstance(request_id, str) or not request_id.strip()
        ):
            raise RuntimeError("fork creation record 缺少冻结 Gateway/delivery request_id，拒绝补造")
        gateway_token = set_current_gateway_id(gateway_id)
        request_token = set_current_request_id(request_id) if intent["source_remote"] else None
        try:
            yield
        finally:
            if request_token is not None:
                reset_current_request_id(request_token)
            reset_current_gateway_id(gateway_token)

    async def retry_pending_remote_exports(self) -> None:
        """独立清理 owner 重试 delivery；已发布目标启动不等待来源。"""
        catalog = self._session_service.path_resolver.catalog_store
        for capture in catalog.list_pending_fork_export_captures():
            frozen = json.loads(capture.request_json)
            if frozen["creation_kind"] == "new_session_main":
                record = catalog.get_creation_record(capture.creation_key)
            else:
                record = self._threads.for_owner_session(capture.target_session_id).control_store.get_thread_creation_record(capture.creation_key)
            if record.request_intent_json is None:
                raise RuntimeError("fork export cleanup 缺少冻结目标 creation intent")
            intent = json.loads(record.request_intent_json)
            with self._delivery_context(intent, record.fork_delivery_request_id):
                await self._finish_remote_export(intent, capture.copy_operation_id)

    async def _fork_main(
        self, request: SessionForkRequest, key: str, parent_node_id: str | None,
        source_session_id: str, source_thread_id: str | None,
        generation_origin: SessionGenerationOriginDTO | None,
    ) -> SessionForkResultDTO:
        catalog = self._session_service.path_resolver.catalog_store
        try:
            record = catalog.get_creation_record(key)
        except KeyError:
            record = None
        if record is None:
            intent = await self._new_intent(request, source_session_id, source_thread_id, generation_origin)
            _, record = await self._session_service.prepare_context_fork(
                idempotency_key=key, title=intent["display_title"], agent_id=intent["source_agent_id"],
                parent_session_id=self._session_service.path_resolver.nearest_session_ancestor(parent_node_id),
                context_source_session_id=source_session_id, generation_origin=generation_origin,
                parent_node_id=parent_node_id, request_intent=intent,
                fork_delivery_request_id=require_current_request_id("fork creation delivery") if intent["source_remote"] else None,
            )
        validate_session_creation_envelope(record)
        if record.purpose != "context_fork" or record.request_intent_json is None:
            raise RuntimeError("fork key 不属于完整 context fork 创建意图")
        intent = json.loads(record.request_intent_json)
        with self._delivery_context(intent, record.fork_delivery_request_id):
            self._validate_frozen_request(intent, request, generation_origin)
            if record.state == "aborted":
                await self._finish_remote_export(intent, key)
                raise RuntimeError(f"fork operation 已中止: {record.abort_reason}")
            if record.state == "published":
                session = await self._session_service.get(record.session_id)
                return await self._result(intent, session, record.main_thread_id, key, creation=record)
            try:
                # capture 接管物理树后，其 rollout 发布目录由原物化 owner 恢复。
                if catalog.get_fork_source_capture(copy_operation_id=key) is None:
                    record = await self._session_service.resume_prepared_context_fork(record)
                result = await self._materialize(request, intent, record.session_id, creation=record)
                proof = self._checkpointer.forks.recovery.read_committed_fork_completion_proof(
                    record=record, source_session_id=intent["source_session_id"],
                    source_owner=MutationIntentOwner(session_id=intent["source_session_id"], thread_id=intent["source_thread_id"]),
                    result=result, mode=request.mode, relationship=request.relationship, checkpoint_ns="",
                )
                session = await self._publish_prepared_fork(record, proof)
                return await self._result(intent, session, record.main_thread_id, key, creation=record)
            except Exception:
                await self._abort_if_unpublished(record, "fork capture/materialization/publication failed")
                await self._finish_remote_export(intent, key)
                raise

    async def _fork_child(
        self, request: SessionForkRequest, key: str, target_session_id: str,
        source_session_id: str, source_thread_id: str | None,
    ) -> SessionForkResultDTO:
        service = self._threads.for_owner_session(target_session_id)
        control = service.control_store
        try:
            record = control.get_thread_creation_record(key)
        except KeyError:
            record = None
        if record is None:
            intent = await self._new_intent(request, source_session_id, source_thread_id, None)
        else:
            if record.purpose != "fork" or record.request_intent_json is None:
                raise RuntimeError("fork key 不属于 child fork 创建意图")
            intent = json.loads(record.request_intent_json)
            self._validate_frozen_request(intent, request, None)
        delivery_request_id = (
            record.fork_delivery_request_id if record is not None else
            require_current_request_id("fork child creation delivery") if intent["source_remote"] else None
        )

        async def materialize(record: ThreadCreationRecord) -> None:
            await self._materialize(request, intent, target_session_id, child=record)
            proof = self._checkpointer.forks.read_child_fork_completion(target_session_id, record)
            control.freeze_thread_fork_completion(key, **proof)

        async def abort(record: ThreadCreationRecord) -> bool:
            return await self._checkpointer.forks.abort_prepared_child_fork(target_session_id, record)

        with self._delivery_context(intent, delivery_request_id):
            try:
                result = await service.create(
                    idempotency_key=key, session_id=target_session_id, thread_id=None, delegation_id=None,
                    initial_state="idle", purpose="fork", request_intent=intent,
                    fork_delivery_request_id=delivery_request_id,
                    session_metadata={"graph_binding": intent["source_graph_binding"],
                                      "capability_profile": dict(DEEP_AGENT_CAPABILITY_PROFILE),
                                      "task_seed": None, "task_reference": None},
                    artifact_manifest={}, before_publish=materialize, before_abort=abort,
                    on_published=lambda current: self._checkpointer.forks.mark_published_child_fork(target_session_id, current),
                )
            except Exception:
                await self._finish_remote_export(intent, key)
                raise
            published = control.get_thread_creation_record(key)
            await observe_phase(key, "copy.target.published")
            session = await self._session_service.get(target_session_id)
            return await self._result(intent, session, result.child_thread_id, key, creation=published)

    async def _result(
        self, intent: dict[str, object], session: SessionDTO, thread_id: str, key: str,
        *, creation: SessionCreationRecord | ThreadCreationRecord,
    ) -> SessionForkResultDTO:
        session_id = session.session_id
        capture = self._session_service.path_resolver.catalog_store.get_fork_source_capture(creation_key=key)
        if capture is None or capture.state != "materialized" or (capture.target_session_id, capture.target_thread_id) != (session_id, thread_id):
            raise RuntimeError("fork result 缺少真实目标 committed operation")
        point = json.loads(capture.request_json)
        request = SessionForkRequest.model_validate(intent["request"])
        target = parse_resource_uri(request.target_container_uri)
        if target.scope_id != session.workspace_id:
            raise RuntimeError("fork result 的冻结目标 Workspace 与已发布 Session 不一致")
        target_thread_uri = thread_resource_uri(
            target.scope_id, session_id, thread_id, gateway_authority=intent["target_gateway_id"],
        )
        resources = [SessionForkResourceResult.model_validate(resource) for resource in
                     self._checkpointer.forks.copy_resource_results(capture, target_thread_uri=target_thread_uri)]
        # 先读取原 publication 的完整响应事实，再完成原 owner 的附件提交。
        # 提交成功后的删除可以关闭实时读取，但不能把原成功回执改成准入失败。
        if isinstance(creation, SessionCreationRecord):
            await self._checkpointer.forks.finalize_published_fork(creation)
        else:
            await self._checkpointer.forks.finalize_published_child_fork(session_id, creation)
        resources.extend(await self._settle_live_resources(request, key=key, session_id=session_id, thread_id=thread_id))
        await self._finish_remote_export(intent, key)
        goal = intent["goal_snapshot"]
        release_state, release_error = self._session_service.path_resolver.catalog_store.get_fork_export_release(key)
        return SessionForkResultDTO(
            session=session, thread_id=thread_id,
            target_gateway_id=intent["target_gateway_id"],
            thread_uri=target_thread_uri,
            creation_kind=request.creation_kind, source_uri=intent["source_uri"],
            source_view_id=point["source_view_id"], source_view_revision=point["source_view_revision"],
            anchor_item_id=point["anchor_item_id"], mode=request.mode,
            resources=resources,
            goal_id=goal["goal_id"] if goal is not None else None,
            export_release=SessionForkExportReleaseDTO(state=release_state, error=release_error) if release_state is not None else None,
        )

    async def _abort_if_unpublished(self, record: SessionCreationRecord, reason: str) -> None:
        current = self._session_service.path_resolver.catalog_store.get_creation_record(record.session_creation_idempotency_key)
        if replace(current, state=record.state, abort_reason=record.abort_reason, record_updated_at=record.record_updated_at) != record:
            raise ValueError("fork_owner_overlap: main 清理的 creation capability 与当前原件不一致")
        if current.state != "preparing":
            return
        await self._checkpointer.forks.abort_prepared_capture(current, reason)
        await self._checkpointer.forks.recovery.release_prepared_fork_retentions(current)
        await self._session_service.abort_prepared_context_fork(current, reason)

    async def _recover_prepared_fork(
        self, record: SessionCreationRecord | ThreadCreationRecord, *, target_session_id: str,
    ) -> bool:
        if record.request_intent_json is None:
            raise RuntimeError("fork startup 缺少冻结请求")
        main = isinstance(record, SessionCreationRecord)
        if isinstance(record, SessionCreationRecord):
            key, thread_id = record.session_creation_idempotency_key, record.main_thread_id
        else:
            key, thread_id = record.thread_creation_idempotency_key, record.child_thread_id
        intent = json.loads(record.request_intent_json)
        origin = intent["generation_origin"] if main else None
        try:
            with self._delivery_context(intent, record.fork_delivery_request_id):
                if isinstance(record, SessionCreationRecord):
                    capture = self._session_service.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
                    if capture is not None and capture.state == "aborted":
                        # capture 已终结时只继续原创建 owner 的清理，不能重新物化。
                        await self._abort_if_unpublished(record, "fork startup resumed aborted capture cleanup")
                        await self._finish_remote_export(intent, key)
                        return False
                await self.fork(SessionForkRequest.model_validate(intent["request"]),
                                generation_origin=SessionGenerationOriginDTO.model_validate(origin) if origin is not None else None)
        except (IncompleteForkSourceCaptureError, ThreadCreationAbortedError) as error:
            # fork 原 owner 完成中止与清理后，startup 才能继续处理其他 operation。
            catalog = self._session_service.path_resolver.catalog_store
            current = (catalog.get_creation_record(key) if main else
                       self._threads.for_owner_session(target_session_id).control_store.get_thread_creation_record(key))
            capture = catalog.get_fork_source_capture(copy_operation_id=key)
            if isinstance(error, ThreadCreationAbortedError):
                if main or error.record != current or capture is None:
                    raise RuntimeError("fork child 清理未由原 creation owner 完成中止") from error
                self._checkpointer.forks.require_target_capture(capture, creation=current)
                expected_capture = capture
            else:
                expected_capture = error.capture
            if (expected_capture.copy_operation_id, expected_capture.creation_key) != (key, key) or current.state != "aborted" or (
                current.preimage_hash, current.request_intent_json,
            ) != (record.preimage_hash, record.request_intent_json) or capture is None or (
                capture.state, capture.creation_key, capture.target_session_id, capture.target_thread_id,
                capture.preimage_hash, capture.snapshot_id, capture.snapshot_relative_locator,
            ) != (
                "aborted", key, target_session_id, thread_id,
                expected_capture.preimage_hash, expected_capture.snapshot_id, expected_capture.snapshot_relative_locator,
            ):
                raise RuntimeError("fork incomplete capture 未由原 creation owner 完成精确中止") from error
            root = capture_directory(self._session_service.path_resolver.sessions_root, capture)
            if root.exists() or root.is_symlink():
                raise RuntimeError("fork incomplete capture 中止后仍残留私有 snapshot 目录") from error
            logger.warning("fork startup 已中止未完成 capture: %s", error)
            return False
        return True

    async def recover_prepared_forks(self) -> int:
        recovered = 0
        for record in self._session_service.list_preparing_creation_records(purpose="context_fork"):
            recovered += await self._recover_prepared_fork(record, target_session_id=record.session_id)
        for record in self._session_service.path_resolver.catalog_store.list_published_fork_creation_records():
            await self._checkpointer.forks.finalize_published_fork(record)
            await self._settle_live_resources(
                SessionForkRequest.model_validate(json.loads(record.request_intent_json)["request"]),
                key=record.session_creation_idempotency_key, session_id=record.session_id, thread_id=record.main_thread_id,
            )
        _, nodes = self._session_service.path_resolver.catalog_store.read_nodes_snapshot()
        for node in nodes:
            if node.kind != "session" or node.state != "active":
                continue
            control = self._threads.for_owner_session(node.node_id).control_store
            for record in control.list_fork_thread_creations():
                if record.state == "preparing":
                    recovered += await self._recover_prepared_fork(record, target_session_id=node.node_id)
                elif record.state == "published":
                    await self._checkpointer.forks.finalize_published_child_fork(node.node_id, record)
                    await self._settle_live_resources(
                        SessionForkRequest.model_validate(json.loads(record.request_intent_json)["request"]),
                        key=record.thread_creation_idempotency_key, session_id=node.node_id, thread_id=record.child_thread_id,
                    )
        return recovered

    async def _publish_prepared_fork(self, record: SessionCreationRecord, proof) -> SessionDTO:
        await self._goals.install_fork_goal(record)
        session = await self._session_service.publish_prepared_context_fork(
            record, fork_completion=proof, on_published=self._checkpointer.forks.mark_published_fork,
        )
        await observe_phase(record.session_creation_idempotency_key, "copy.target.published")
        return session


__all__ = ["SessionContextForkService"]
