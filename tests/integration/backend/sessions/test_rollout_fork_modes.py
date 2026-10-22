from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock
from uuid import uuid4
from xml.etree.ElementTree import fromstring

import httpx
import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)

from app import main
from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.abstractions.workspace_resources import ResourceKind
from app.abstractions.workspace_resources import (
    ResourceSnapshot as RuntimeResourceSnapshot,
)
from app.agents.summarization.planning import build_cache_preserving_event
from app.agents.summarization.state import CachePreservingPartition
from app.container import AppContainer, WorkspaceContainerRegistry
from app.core import session_subtree_delete
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.resource_uri import (
    parse_resource_uri,
    parse_session_thread_uri,
    resource_display_uri,
    thread_resource_uri,
)
from app.core.session_catalog_store import SessionCatalogStore
from app.core.session_catalog_store.contracts import (
    ForkSourceCaptureRecord,
    SessionGenerationOriginDTO,
)
from app.core.session_control_store import SessionControlStore
from app.core.session_control_store.copy_attachment_settlement import (
    read_copy_attachment_settlements,
)
from app.core.session_control_store.fork_retention_release import (
    read_fork_retention_releases,
)
from app.core.session_lifecycle_gate import (
    SessionDeletionPendingError,
    SessionLifecycleGate,
)
from app.core.session_thread_context import session_thread_owner_scope
from app.core.thread_creation import ThreadCreationService
from app.core.trace_middleware import (
    get_current_gateway_id,
    get_current_request_id,
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.core.workspace_identity import bind_workspace_id, load_or_create_workspace_id
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    PayloadKind,
    SemanticKind,
    TurnScope,
)
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.execution_admission import (
    InternalExecutionIntent,
    user_turn_admission_payload_hash,
)
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    sha256_jcs,
)
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.records import CanonicalItemRecord, user_input_payload
from app.domain.itemized.request_plan import ContextContribution
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.gateway.federation.grants import ForkGrantTarget
from app.prompting.factory import PromptSection, internal_message_factory
from app.schemas.internal_v2.message import AttachmentRef, MessageCreateRequest
from app.schemas.internal_v2.node_debug import (
    NodeDebugActionRecordDTO,
    NodeDebugConfigurationBreakpointDTO,
    NodeDebugConfigurationDTO,
    NodeDebugLaunchClaimDTO,
    NodeDebugSessionManifestDTO,
)
from app.schemas.internal_v2.session import (
    SessionCreateRequest,
    SessionDTO,
    SessionForkCreationKind,
    SessionForkMode,
    SessionForkOwnerReference,
    SessionForkRequest,
    SessionForkResourceChoice,
    SessionForkResultDTO,
)
from app.schemas.internal_v2.session_fork_transfer import (
    SessionForkTransferRequest,
    SessionForkTransferResultDTO,
)
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest, TurnHistoryPageDTO
from app.services.business.job.service import JobService
from app.services.business.session_context_fork_service import SessionContextForkService
from app.services.business.session_context_resource import (
    workspace_session_inventory_uri,
)
from app.services.business.session_service import SessionService
from app.services.business.user_content_builder import UserContentBuilder
from app.services.business.workspace_resources.owner import WorkspaceResourceOwner
from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.attachment_content_service import (
    AttachmentContentService,
)
from app.services.infrastructure.node_debug.session.fork import (
    NodeDebugTargetValidationError,
    debug_configuration_resource_uri,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.activation.common import (
    HASH_MISMATCH_CODE,
    ResourceActivationStoreError,
)
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
)
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.checkpoint import (
    fork_owner,
    fork_source,
)
from app.services.infrastructure.rollout_context.checkpoint.boundary.tool_protocol_boundary import (
    ToolProtocolBoundaryConflict,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.fork import (
    node_debug_materialization,
    retention_release,
    snapshot_copy,
    source_snapshot,
)
from app.services.infrastructure.rollout_context.fork import transfer as fork_transfer
from app.services.infrastructure.rollout_context.fork.source_capture import (
    IncompleteForkSourceCaptureError,
    capture_directory,
    read_captured_snapshot,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.source_observation import (
    SourceObservation,
    build_source_lifecycle_decision,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
    detail_record_from_mapping,
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    parse_detail_payload,
)
from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from app.services.mapping.itemized.resource_activation import (
    project_sealed_resource_refs,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_test_workspace import (
    prepare_rollout_workspace,
)
from tests.integration.backend.sessions.test_navigation_worker_lifespan import (
    _build_owned_container,
    _isolate_non_navigation_services,
)
from tests.support import fork_deadline as fork_deadline_module
from tests.support.fork_deadline import (
    ForkDeadlineExceeded,
    ForkDeadlineRegistry,
    ForkOperationDeadline,
)
from tests.support.rollout_output import commit_fixture_output
from tests.support.rollout_view_mutations import mutate_view_members
from tests.support.session_navigation_queue import (
    create_catalog_intent,
    submit_catalog_intent,
)
from tests.support.workspaces import (
    prepare_default_test_workspace,
    render_test_workspace_config,
)


@dataclass(frozen=True, slots=True)
class ForkIntegrationContext:
    workspace: Path
    sessions: SessionService
    saver: RolloutCheckpointSaver
    forks: SessionContextForkService


@pytest.fixture
def fork_context(
    production_fork_container: AppContainer,
    fork_gateway_request_context: None,
) -> ForkIntegrationContext:
    return _production_fork_context(production_fork_container)


@pytest.fixture
def fork_gateway_request_context() -> Iterator[None]:
    gateway_token = set_current_gateway_id("gateway_6c8b2f9317a5a2c6c4569958b1e23d84")
    request_token = set_current_request_id("compaction-fork-request")
    try:
        yield
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)


@pytest.fixture
def tool_set() -> ToolSetSnapshot:
    return ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())


def _fork_deadline_target(workspace_id: str, target_uri: str) -> str:
    """首次调用与冻结 intent 恢复共用规范目标，不重开操作期限。"""
    gateway_id = get_current_gateway_id()
    target = parse_resource_uri(target_uri, local_gateway_id=gateway_id)
    canonical_uri = resource_display_uri(
        scope=target.scope, scope_id=target.scope_id, kind=target.kind,
        tail_segments=target.tail_segments,
        gateway_authority=target.gateway_authority or gateway_id,
    )
    return f"{workspace_id}/{canonical_uri}"


@pytest.fixture
def fork_provider_prepare(
    tool_set: ToolSetSnapshot,
    frozen_provider_wire, frozen_provider_activation,
):
    """fork 前后使用真实 Saver/CSM/activation 编译并封存实际 Provider 请求。"""
    def prepare(context: ForkIntegrationContext, session_id: str, execution_id: str, manager: ContextSourceManager, key: str, *, attempt: int, outcome: str = "completed", selected_tools: ToolSetSnapshot | None = None, thread_id: str | None = None, registry: ResourceRegistry | None = None, activation_policy: ResourceActivationPolicySnapshot | None = None):
        thread = thread_id if thread_id is not None else _main_thread_id(context, session_id)
        admission = context.saver.execution_admission(
            session_id, thread_id=thread, execution_id=execution_id,
        )
        with (
            session_thread_owner_scope(MutationIntentOwner(session_id, thread), langgraph_thread_id=thread),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            prepared = context.saver.prepare_context_for_provider(
                session_id, thread, turn_id=admission.turn_id,
                execution_id=execution_id,
                plan_creation_idempotency_key="fork-create:" + key,
                seal_idempotency_key="fork-seal:" + key,
                context_source_manager=manager,
                tool_set_supplier=lambda: selected_tools if selected_tools is not None else tool_set,
                **frozen_provider_wire("fork-provider", "chat_completions"),
                **frozen_provider_activation(context.saver, plan_creation_idempotency_key="fork-create:" + key,
                                             registry=registry, policy=activation_policy),
            )
            context.saver.register_model_call(
                session_id, thread_id=thread, execution_id=admission.execution_id,
                model_call_id=prepared["snapshot"].model_call_id, attempt=attempt,
                provider="fork-provider", assembly_id=prepared["assembly_id"],
            )
            context.saver.update_model_call_outcome(
                session_id, thread_id=thread, model_call_id=prepared["snapshot"].model_call_id,
                outcome=outcome, dispatch_state="completed" if outcome in {"completed", "completed_empty"} else "failed",
            )
            return prepared
    return prepare


@pytest.fixture
def production_fork_container(request: pytest.FixtureRequest,
                              monkeypatch: pytest.MonkeyPatch,
                              fork_gateway_request_context: None) -> AppContainer:
    """fork/creation/admission 使用真实生产装配，只隔离无关 lifespan 外部服务。"""
    workspace = prepare_rollout_workspace(request)
    # 本文件通过冻结请求验证存储/fork，不调用外部模型；干净 Git 快照也不能依赖个人密钥。
    config = json.loads(render_test_workspace_config(
        (Path.cwd() / "configs/tests/workspace/default.jsonc").read_text(),
    ))
    for provider in config["llm"]["providers"]:
        provider["api_key"] = "fork-contract-unused-key"
        provider["endpoint"] = "http://127.0.0.1:1/v1"
        provider.pop("auth", None)
    (workspace / ".boxteam/workspace.jsonc").write_text(json.dumps(config, ensure_ascii=False) + "\n")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("BOXTEAM_HOME", str(context.boxteam_home))
    container = _build_owned_container(workspace, monkeypatch, request)
    fork_deadlines = ForkDeadlineRegistry(context.artifacts_dir)
    original_fork = SessionContextForkService.fork

    def terminal_fork_failure(
        service: SessionContextForkService, fork_request: SessionForkRequest, error: Exception,
    ) -> bool:
        """只认可同工作区无副作用预检拒绝或 catalog 的持久终态。"""
        try:
            target = parse_resource_uri(
                fork_request.target_container_uri, local_gateway_id=get_current_gateway_id(),
            )
        except ValueError:
            return False
        # live 跨端拒绝发生在路由/transport 前；只接受无本地创建事实的已知预检码。
        live_cross_target_rejected = str(error).startswith("fork_live_cross_workspace_unsupported:")
        if not live_cross_target_rejected and (
            target.scope != "workspace" or target.scope_id != service._session_service.workspace_id
            or target.gateway_authority not in {None, get_current_gateway_id()}
        ):
            return False
        key = service._creation_key(fork_request.idempotency_key)
        catalog = service._session_service.path_resolver.catalog_store
        try:
            record = catalog.get_creation_record(key)
        except KeyError:
            record = None
        if record is None and fork_request.creation_kind == "new_child_thread":
            _, target_session_id, _ = parse_session_thread_uri(fork_request.target_container_uri)
            control = service._threads.for_owner_session(target_session_id).control_store
            try:
                record = control.get_thread_creation_record(key)
            except KeyError:
                record = None
        capture = (catalog.get_fork_source_capture(creation_key=key)
                   or catalog.get_fork_source_capture(copy_operation_id=key))
        # 发布/中止并不代表远端导出已释放；同键重试失败仍必须保留原期限。
        if capture is not None and catalog.get_fork_export_release(capture.copy_operation_id)[0] == "pending":
            return False
        if record is not None:
            if fork_request.creation_kind == "new_child_thread":
                if record.purpose != "fork" or record.request_intent_json is None:
                    return False
                frozen = json.loads(record.request_intent_json)
                normalized = fork_request.model_dump(mode="json")
                for field in ("source_uri", "target_container_uri"):
                    address = parse_resource_uri(normalized[field], local_gateway_id=get_current_gateway_id())
                    normalized[field] = resource_display_uri(
                        scope=address.scope, scope_id=address.scope_id, kind=address.kind,
                        tail_segments=address.tail_segments,
                        gateway_authority=address.gateway_authority or get_current_gateway_id(),
                    )
                if frozen["request"] != normalized or frozen["generation_origin"] is not None:
                    return False
                if record.state == "published" and capture is None:
                    return False
                if record.state == "aborted":
                    session_node = service._session_service.path_resolver.resolve_session_node(target_session_id)
                    if any((session_node / locator).exists() or (session_node / locator).is_symlink()
                           for locator in (record.staging_locator, record.final_relative_locator)):
                        return False
                if capture is not None and (
                    (capture.target_session_id, capture.target_thread_id) != (target_session_id, record.child_thread_id)
                    or capture.state != {"aborted": "aborted", "published": "materialized"}.get(record.state)
                ):
                    return False
                if capture is not None:
                    service._checkpointer.forks.require_target_capture(capture, creation=record)
            return record.state in {"aborted", "published"}
        if capture is not None:
            return False
        if isinstance(error, ToolProtocolBoundaryConflict):
            # 本地预检已明确拒绝且没有创建/捕获事实，观察已结束。
            return True
        # Pending、传输或运行时失败仍属未知状态；只有稳定的来源校验码
        # 才表示目标创建前发生了无写入拒绝。
        return error is not None and str(error).startswith((
            "source-mismatch:", "fork_source_not_completed:", "unknown_fork_resource:",
            "fork_owner_overlap:", "不支持的 fork mode:", "fork anchor_mode 必须",
            "fork_live_cross_workspace_unsupported:", "fork_live_revision_conflict:",
            "fork_live_resource_rejected:",
        ))

    async def fork_with_deadline(
        service: SessionContextForkService,
        fork_request: SessionForkRequest,
        *,
        generation_origin: SessionGenerationOriginDTO | None = None,
    ) -> SessionForkResultDTO:
        deadline = fork_deadlines.for_operation(
            target=_fork_deadline_target(container.workspace_id, fork_request.target_container_uri),
            operation_key=fork_request.idempotency_key,
        )
        try:
            result = await deadline.wait(
                original_fork(service, fork_request, generation_origin=generation_origin),
                "SessionContextForkService.fork",
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if terminal_fork_failure(service, fork_request, error) and not deadline._timed_out:
                fork_deadlines.complete(deadline)
            raise
        if result.export_release is None or result.export_release.state == "confirmed":
            deadline.complete()
        return result

    monkeypatch.setattr(SessionContextForkService, "fork", fork_with_deadline)

    original_http_post = httpx.AsyncClient.post

    async def post_with_fork_deadline(client, url, *args, **kwargs):
        if httpx.URL(url).path.endswith("/fork-context"):
            body = kwargs.get("json")
            if isinstance(body, dict):
                operation_key = body.get("idempotency_key")
                target_uri = body.get("target_container_uri")
                if isinstance(operation_key, str) and isinstance(target_uri, str):
                    deadline = fork_deadlines.for_operation(
                        target=_fork_deadline_target(container.workspace_id, target_uri),
                        operation_key=operation_key,
                    )
                    response_task = asyncio.create_task(
                        original_http_post(client, url, *args, **kwargs),
                    )
                    try:
                        response = await deadline.wait(
                            response_task,
                            "ASGI fork-context HTTP request",
                        )
                    except ForkDeadlineExceeded:
                        if not deadline._completed:
                            raise
                        response = await response_task
                    terminal = 400 <= response.status_code < 500
                    if 200 <= response.status_code < 300:
                        try:
                            payload = response.json()
                        except ValueError:
                            payload = None
                        data = payload.get("data") if isinstance(payload, dict) else None
                        release = data.get("export_release") if isinstance(data, dict) else object()
                        terminal = isinstance(data, dict) and (release is None or (
                            isinstance(release, dict) and release.get("state") == "confirmed"
                        ))
                    if terminal:
                        fork_deadlines.complete(deadline)
                    return response
        return await original_http_post(client, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", post_with_fork_deadline)
    request.addfinalizer(fork_deadlines.close)
    _isolate_non_navigation_services(container, monkeypatch)
    # 默认场景无外设；live 用例另行提供真实 owner 所消费的完整传输事实。
    for kind in ("terminal", "browser"):
        provider = container.session_resource_service._provider_registry.get(kind)
        monkeypatch.setattr(provider._owner._transport, "list_resources", AsyncMock(return_value=[]))
    monkeypatch.setattr(main, "configure_application_logging", lambda **_: None)
    monkeypatch.setattr(main, "_model_stream_controller", None)
    return container


def _run_protected_fork_deadline_probe(mode: str, artifacts_dir: Path) -> subprocess.CompletedProcess[bytes]:
    probe = r'''
import asyncio
import json
import sys
from pathlib import Path

from tests.support.fork_deadline import ForkDeadlineExceeded, ForkDeadlineRegistry, ForkOperationDeadline


class RecordingWatchdog:
    def __init__(self) -> None:
        self.completions = 0
        self.termination_waited = False

    def start(self, *_args) -> None:
        pass

    def stage(self, *_args) -> None:
        pass

    def complete(self, *_args) -> None:
        self.completions += 1

    def wait_for_termination(self) -> None:
        self.termination_waited = True


async def main(mode: str, artifacts: Path) -> None:
    del artifacts
    watchdog = RecordingWatchdog()
    deadline = ForkOperationDeadline.for_test(
        "fork-probe-target", f"fork-probe-{mode}", watchdog, 0.01,
    )
    if mode == "durable-response":
        async def delayed_response() -> dict[str, int]:
            await asyncio.sleep(0.005)
            deadline.complete()
            await asyncio.sleep(0.02)
            return {"status": 200}

        response = await deadline.wait(delayed_response(), "HTTP response after durable completion")
        assert response == {"status": 200}
        assert deadline.elapsed > 0.01 and deadline._completed
        assert watchdog.completions == 1
        print(json.dumps({"elapsed": deadline.elapsed, "completed": deadline._completed}))
        return
    if mode == "watchdog-timeout":
        operation_started = asyncio.Event()

        async def operation() -> None:
            operation_started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(operation())
        try:
            await deadline.wait(task, "blocked protected operation")
        except ForkDeadlineExceeded as error:
            assert operation_started.is_set() and not task.cancelled()
            assert not deadline._completed
            assert "backend_state=unknown" in str(error)
            assert watchdog.completions == 0
            watchdog.wait_for_termination()
            assert watchdog.termination_waited
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            print(json.dumps({"timed_out": True, "operation_cancelled_by_deadline": False}))
            return
    if mode in {"registry-active-close", "registry-completed-close"}:
        class RegistryWatchdog:
            def __init__(self) -> None:
                self.waited = 0
                self.closed = 0

            def start(self, *_args: object) -> None:
                pass

            def complete(self, *_args: object) -> None:
                pass

            def wait_for_termination(self) -> None:
                self.waited += 1

            def close(self) -> None:
                self.closed += 1

        registry = ForkDeadlineRegistry.__new__(ForkDeadlineRegistry)
        registry.watchdog = RegistryWatchdog()
        registry._active = {}
        active = ForkOperationDeadline.for_test(
            "fork-probe-target", f"fork-probe-{mode}", registry.watchdog, 0.01,
        )
        registry._active[(active.target, active.operation_key)] = active
        if mode == "registry-active-close":
            registry.close()
            assert registry.watchdog.waited == 1 and registry.watchdog.closed == 0
        else:
            registry.complete(active)
            registry.close()
            assert registry.watchdog.waited == 0 and registry.watchdog.closed == 1
        print(json.dumps({"mode": mode, "waited": registry.watchdog.waited,
                          "closed": registry.watchdog.closed}))
        return
    raise ValueError(f"unknown fork probe mode: {mode}")


asyncio.run(main(sys.argv[1], Path(sys.argv[2])))
'''
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "timeout", "--signal=TERM", "--kill-after=5s", "15s", "bash", "-c",
        'ulimit -d 4194304 || exit 97; exec "$@"', "bash",
        sys.executable, "-c", probe, mode, str(artifacts_dir),
    ]
    result = subprocess.run(
        command, cwd=Path.cwd(), capture_output=True, timeout=20, start_new_session=True, check=False,
    )
    (artifacts_dir / f"{mode}.stdout.raw").write_bytes(result.stdout)
    (artifacts_dir / f"{mode}.stderr.raw").write_bytes(result.stderr)
    (artifacts_dir / f"{mode}.exit-code.txt").write_text(f"{result.returncode}\n", encoding="utf-8")
    return result


@pytest.mark.asyncio
async def test_fork_deadline_starts_before_asgi_client_post(
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_identity: list[tuple[str, str]] = []
    stages: list[str] = []
    entered_http_request = asyncio.Event()

    class RecordingDeadline:
        completed = False
        _completed = False

        async def wait(self, awaitable, stage: str):
            stages.append(stage)
            task = asyncio.ensure_future(awaitable)
            await asyncio.sleep(0)
            assert entered_http_request.is_set(), "deadline 必须覆盖底层 HTTP request"
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise ForkDeadlineExceeded("probe deadline")

        def complete(self) -> None:
            self.completed = True
            self._completed = True

    deadline = RecordingDeadline()

    def for_operation(_registry, *, target: str, operation_key: str):
        requested_identity.append((target, operation_key))
        return deadline

    monkeypatch.setattr(ForkDeadlineRegistry, "for_operation", for_operation)

    async def block_http_request(_client, *_args, **_kwargs):
        entered_http_request.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(httpx.AsyncClient, "request", block_http_request)
    target_uri = workspace_session_inventory_uri(production_fork_container.workspace_id)
    body = {"idempotency_key": "fork-http-deadline-probe", "target_container_uri": target_uri}
    async with httpx.AsyncClient(base_url="http://workspace") as client:
        with pytest.raises(ForkDeadlineExceeded, match="probe deadline"):
            await client.post(
                "/api/v1/sessions/ses_deadline_probe/fork-context", json=body,
            )

    assert requested_identity == [
        (_fork_deadline_target(production_fork_container.workspace_id, target_uri), body["idempotency_key"]),
    ]
    assert stages == ["ASGI fork-context HTTP request"]
    assert not deadline.completed


@pytest.mark.asyncio
async def test_fork_http_response_after_deadline_error_uses_durable_completion(
    production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_uri = workspace_session_inventory_uri(production_fork_container.workspace_id)
    operation_key = "fork-http-durable-response"
    expected_identity = (
        _fork_deadline_target(production_fork_container.workspace_id, target_uri), operation_key,
    )
    original_for_operation = ForkDeadlineRegistry.for_operation
    requested_identity: list[tuple[str, str]] = []
    deadlines: dict[tuple[str, str], ForkOperationDeadline] = {}
    durable_completion = asyncio.Event()

    def record_deadline(
        registry: ForkDeadlineRegistry, *, target: str, operation_key: str,
    ) -> ForkOperationDeadline:
        deadline = original_for_operation(
            registry, target=target, operation_key=operation_key,
        )
        identity = (target, operation_key)
        requested_identity.append(identity)
        deadlines[identity] = deadline
        return deadline

    monkeypatch.setattr(ForkDeadlineRegistry, "for_operation", record_deadline)

    # 保持正式 60 秒期限对象，只压缩超时竞态，不等待真实 60 秒。
    async def wait_for_durable_completion(
        deadline: ForkOperationDeadline,
        awaitable: asyncio.Future[httpx.Response],
        stage: str,
    ) -> httpx.Response:
        deadline.set_stage(stage)
        response_task = asyncio.ensure_future(awaitable)
        await durable_completion.wait()
        assert not response_task.done()
        raise deadline._timeout()

    monkeypatch.setattr(ForkOperationDeadline, "wait", wait_for_durable_completion)

    class DelayedResponse(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            identity = (
                _fork_deadline_target(production_fork_container.workspace_id, payload["target_container_uri"]),
                payload["idempotency_key"],
            )
            assert identity == expected_identity
            deadline = deadlines[identity]
            await asyncio.sleep(0.005)
            deadline.complete()
            durable_completion.set()
            await asyncio.sleep(0.02)
            return httpx.Response(
                200, json={"data": {"export_release": None}}, request=request,
            )

    body = {
        "idempotency_key": operation_key,
        "target_container_uri": target_uri,
    }
    async with httpx.AsyncClient(
        base_url="http://workspace", transport=DelayedResponse(),
    ) as client:
        response = await client.post(
            "/api/v1/sessions/ses_fork_deadline_probe/fork-context", json=body,
        )

    assert requested_identity == [expected_identity]
    deadline = deadlines[expected_identity]
    assert deadline.deadline - deadline.started_at == 60.0
    assert deadline._completed and deadline.elapsed > 0.02
    assert response.status_code == 200
    assert response.json() == {"data": {"export_release": None}}


@pytest.mark.asyncio
async def test_fork_deadline_helper_returns_response_after_test_budget_if_completed(
    request: pytest.FixtureRequest,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path))
    artifacts = context.runtime_root_for_node(request.node.nodeid) / "fork-deadline-probes"
    result = _run_protected_fork_deadline_probe("durable-response", artifacts)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    output = json.loads(result.stdout)
    assert output["elapsed"] > 0.01 and output["completed"] is True


@pytest.mark.asyncio
async def test_direct_service_fork_completes_deadline_after_durable_result(
    fork_provider_prepare, production_fork_container: AppContainer,
    fork_gateway_request_context: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context, fork_provider_prepare)
    request = await _fork_request(
        context, source.session_id, creation_kind="new_session_main", mode="context_fork",
    )

    class RecordingDeadline:
        _completed = False

        async def wait(self, awaitable, _stage: str):
            return await awaitable

        def complete(self) -> None:
            self._completed = True

    deadline = RecordingDeadline()
    monkeypatch.setattr(
        ForkDeadlineRegistry, "for_operation",
        lambda _registry, **_kwargs: deadline,
    )

    result = await production_fork_container.session_context_fork_service.fork(request)

    assert result.export_release is None or result.export_release.state == "confirmed"
    assert deadline._completed


def test_fork_watchdog_terminates_exact_group_when_all_diagnostics_fail(
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []
    identity = {"pid": 501, "pgid": 501}
    now = time.monotonic()
    messages = [
        {"action": "sshd_identity", "identity": identity},
        {
            "action": "start", "token": "expired-token", "target": "target-uri",
            "operation_key": "fork-diagnostic-failure", "started_at": now - 2,
            "deadline": now - 1, "last_stage": "http-request",
        },
    ]
    start_message = b"".join(json.dumps(message).encode() + b"\n" for message in messages)

    class ClosedStderr:
        def write(self, _message: str) -> None:
            raise ValueError("stderr is closed")

        def flush(self) -> None:
            raise ValueError("stderr is closed")

    monkeypatch.setattr(fork_deadline_module.sys, "stderr", ClosedStderr())
    class ReadableStdin:
        def fileno(self) -> int:
            return 29

    monkeypatch.setattr(fork_deadline_module.sys, "stdin", ReadableStdin())
    monkeypatch.setattr(fork_deadline_module.os, "write", lambda *_args: (_ for _ in ()).throw(OSError("fd closed")))
    monkeypatch.setattr(
        fork_deadline_module, "_write_exclusive_json",
        lambda *_args: (_ for _ in ()).throw(OSError("sidecar is unavailable")),
    )
    monkeypatch.setattr(
        fork_deadline_module.select, "select", lambda *_args: ([29], [], []),
    )
    monkeypatch.setattr(fork_deadline_module.os, "read", lambda *_args: start_message)
    monkeypatch.setattr(
        fork_deadline_module, "_kill_owned_c3_sshd",
        lambda frozen: events.append(("c3-identity-checked-and-killed", frozen)),
    )
    monkeypatch.setattr(
        fork_deadline_module, "_terminate_process_group",
        lambda pgid, parent_pid: events.append(("terminate", pgid, parent_pid)),
    )

    exit_code = fork_deadline_module._watchdog_main(
        Path.cwd() / "out/tests/temp/2026/10/18/fork60-deadline/fork60_closeout/artifacts/python",
        7123,
        7123,
    )

    assert exit_code == 137
    assert events == [
        ("c3-identity-checked-and-killed", identity),
        ("terminate", 7123, 7123),
    ]


@pytest.mark.asyncio
async def test_fork_deadline_timeout_keeps_operation_armed_until_watchdog_termination(
    request: pytest.FixtureRequest,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path))
    artifacts = context.runtime_root_for_node(request.node.nodeid) / "fork-deadline-probes"
    result = _run_protected_fork_deadline_probe("watchdog-timeout", artifacts)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    output = json.loads(result.stdout)
    assert output == {"timed_out": True, "operation_cancelled_by_deadline": False}


@pytest.mark.asyncio
async def test_fork_deadline_registry_close_keeps_active_watchdog_and_closes_completed(
    request: pytest.FixtureRequest,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.runtime_root_for_node(request.node.nodeid) / "fork-deadline-close-probes"
    for mode, expected in (
        ("registry-active-close", {"mode": "registry-active-close", "waited": 1, "closed": 0}),
        ("registry-completed-close", {"mode": "registry-completed-close", "waited": 0, "closed": 1}),
    ):
        result = _run_protected_fork_deadline_probe(mode, artifacts / mode)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert json.loads(result.stdout) == expected


@pytest.mark.asyncio
async def test_fork_deadline_caller_cancellation_waits_for_operation_cleanup(
    fork_deadlines: ForkDeadlineRegistry,
) -> None:
    deadline = fork_deadlines.for_operation("target-uri", "fork-caller-cancellation-probe")
    operation_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    operation_cleaned = asyncio.Event()
    operation_task: asyncio.Task[None] | None = None

    async def operation() -> None:
        nonlocal operation_task
        operation_task = asyncio.current_task()
        operation_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await release_cleanup.wait()
            operation_cleaned.set()
            raise

    waiting = asyncio.create_task(deadline.wait(operation(), "blocked-operation"))
    await asyncio.wait_for(operation_started.wait(), timeout=1)
    waiting.cancel("caller cancelled fork deadline wait")
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert not waiting.done(), "deadline wait must remain pending through operation cleanup"
        assert not operation_cleaned.is_set()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError, match="caller cancelled fork deadline wait"):
            await waiting
        assert operation_cleaned.is_set()
    finally:
        release_cleanup.set()
        if operation_task is not None and not operation_task.done():
            operation_task.cancel()
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, *([operation_task] if operation_task is not None else []), return_exceptions=True)
    # 已确认取消后的实际操作完成清理，结束观察，不能留下虚假的在途期限。
    fork_deadlines.complete(deadline)


@dataclass
class ForkLiveTransport:
    """只替换外设传输；创建、版本、关联与恢复仍经过生产 owner。"""

    kind: ResourceKind
    workspace_id: str
    records: dict[str, RuntimeResourceSnapshot]
    creates: int = 0
    revisions: list[tuple[str, int]] = field(default_factory=list)

    async def create(self, **options: object) -> RuntimeResourceSnapshot:
        rid = str(options[f"{self.kind}_id"])
        self.creates += 1
        self.records[rid] = {
            f"{self.kind}_id": rid, "workspace_id": self.workspace_id,
            "status": "running", "reconciliation_status": "verified",
            "transport_owner_revision": options["owner_revision"],
            "title": options["title"], "created_at": "2026-10-15T00:00:00Z",
            "updated_at": "2026-10-15T00:00:00Z",
            **({"cwd_relative": ".", "cols": 100, "rows": 30} if self.kind == "terminal" else {
                "active_page_id": "fork-page", "pages": [{"page_id": "fork-page", "title": "Fork", "url": "about:blank", "active": True}],
            }),
        }
        return dict(self.records[rid])

    async def list_resources(self) -> list[RuntimeResourceSnapshot]:
        return [dict(record) for record in self.records.values()]

    async def get_resource(self, resource_id: str) -> RuntimeResourceSnapshot:
        return dict(self.records[resource_id])

    async def sync_owner_revision(self, resource_id: str, revision: int) -> None:
        self.revisions.append((resource_id, revision))
        self.records[resource_id]["transport_owner_revision"] = revision

    async def delete_resource(self, resource_id: str) -> RuntimeResourceSnapshot:
        self.records[resource_id]["status"] = "deleted"
        return {"deleted": True, self.kind: dict(self.records[resource_id])}


@pytest.fixture
def live_resource_transport_factory(monkeypatch: pytest.MonkeyPatch) -> Callable[..., ForkLiveTransport]:
    def bind(container: AppContainer, kind: ResourceKind, existing: ForkLiveTransport | None = None) -> ForkLiveTransport:
        external = existing or ForkLiveTransport(kind, container.workspace_id, {})
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        for method in ("list_resources", "get_resource", "sync_owner_revision", "delete_resource"):
            monkeypatch.setattr(owner._transport, method, getattr(external, method))
        monkeypatch.setattr(owner._transport, f"create_{kind}", external.create)
        return external
    return bind


async def _create_fork_live_resource(
    container: AppContainer, kind: ResourceKind, session_id: str, *, scope: Literal["thread", "workspace"] = "workspace",
) -> tuple[WorkspaceResourceOwner, RuntimeResourceSnapshot]:
    if kind == "terminal":
        return container.terminal_resource_owner, await container.terminal_resource_owner.create_terminal(
            session_id=session_id, retention_scope=scope, holder_id="fork-live-setup", operation_id="fork-live-create")
    return container.browser_resource_owner, await container.browser_resource_owner.create_browser(
        session_id=session_id, retention_scope=scope, holder_id="fork-live-setup", operation_id="fork-live-create")


def _fork_live_choice(snapshot: RuntimeResourceSnapshot) -> SessionForkResourceChoice:
    return SessionForkResourceChoice(action="reuse_reference", owner_reference=SessionForkOwnerReference(
        kind=snapshot["kind"], resource_id=snapshot["resource_id"], workspace_id=snapshot["workspace_id"],
        expected_revision=snapshot["owner_revision"],
    ))


@dataclass
class SourceForkOwnerTransport:
    source: SessionContextForkService
    calls: list[SessionForkTransferRequest]
    replies: list[SessionForkTransferResultDTO] = field(default_factory=list)

    async def fork_transfer(self, payload: SessionForkTransferRequest) -> SessionForkTransferResultDTO:
        # 网络替身只负责传输；来源 transfer/捕获/释放均走真实生产 owner。
        transferred = SessionForkTransferRequest.model_validate(payload.model_dump(mode="json"))
        self.calls.append(transferred)
        token = set_current_gateway_id("gateway_4aae160f813ed87bcf65a8bb8bfd3729")
        try:
            result = await self.source.transfer(transferred)
            reply = SessionForkTransferResultDTO.model_validate(result.model_dump(mode="json"))
            self.replies.append(reply)
            return reply
        finally:
            reset_current_gateway_id(token)


@pytest.fixture
async def remote_fork_target(
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[AppContainer, SourceForkOwnerTransport]]:
    workspace = prepare_default_test_workspace(
        workspace_root=production_fork_container.workspace_root / "fork-recovery-target",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    shutil.rmtree(workspace / ".boxteam")
    workspace_id = (bind_workspace_id(workspace, production_fork_container.workspace_id)
                    if getattr(request, "param", False) else load_or_create_workspace_id(workspace))
    registry = WorkspaceContainerRegistry(
        mounted_workspaces=MountedWorkspaceRegistry([MountedWorkspace(workspace_id, workspace)]),
        project_root=Path.cwd(),
    )
    target = registry.get(workspace_id)
    transport = SourceForkOwnerTransport(production_fork_container.session_context_fork_service, [])
    monkeypatch.setattr(target.session_context_fork_service, "_transport", transport)
    try:
        yield target, transport
    finally:
        await target.workspace_session_context_transport.aclose()
        await production_fork_container.workspace_session_context_transport.aclose()
        registry.close()


def _production_fork_context(container: AppContainer) -> ForkIntegrationContext:
    return ForkIntegrationContext(
        workspace=container.workspace_root,
        sessions=container.session_service,
        saver=container.checkpointer,
        forks=container.session_context_fork_service,
    )


async def _fork_request(
    context: ForkIntegrationContext,
    session_id: str,
    *,
    creation_kind: SessionForkCreationKind,
    mode: SessionForkMode = "view_prefix_fork",
    title: str | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
    item_id: str | None = None,
    relationship: Literal["detached", "pinned"] = "detached",
    target_container_uri: str | None = None,
) -> SessionForkRequest:
    display = context.saver.load_history(
        session_id, thread_id or _main_thread_id(context, session_id),
        TurnHistoryLoadRequest(direction="tail"),
    ) if mode == "view_prefix_fork" and (turn_id is not None or item_id is not None) else None
    point = await context.forks.source_point(
        session_id=session_id, thread_id=thread_id, turn_id=turn_id, item_id=item_id, mode=mode,
        source_view_id=display.source_view_id if display is not None else None,
        source_view_revision=display.source_view_revision if display is not None else None,
    )
    return SessionForkRequest(
        idempotency_key=f"fork-{uuid4().hex}", source_uri=point.source_uri,
        source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
        anchor_item_id=point.anchor_item_id if mode != "full_rollout_copy" else None,
        mode=mode, title=title, creation_kind=creation_kind,
        relationship=relationship,
        target_container_uri=target_container_uri or (point.main_container_uri if creation_kind == "new_session_main" else point.child_container_uri),
    )


def _checkpoint(
    checkpoint_id: str,
    messages: list[object],
    *,
    pending_sends: list[object] | None = None,
) -> dict[str, object]:
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-08-15T00:00:00+00:00",
        "channel_values": {"messages": messages, "counter": len(messages)},
        "channel_versions": {
            "messages": str(len(messages)),
            "counter": str(len(messages)),
        },
        "versions_seen": {},
        "pending_sends": pending_sends or [],
        "updated_channels": ["messages", "counter"],
    }


def _assert_fork_provider_history(
    prepared, source_prepared, source_items, selected_item_ids: list[str], new_root_item_id: str,
) -> None:
    """以源端真实封存的 typed selection 和 wire carrier 核目标首次请求。"""
    selected_ids = set(selected_item_ids)
    by_id = {item.item_id: item for item in source_items}
    source_refs = [entry.ref for entry in source_prepared["snapshot"].selection
                   if entry.included and entry.ref.ref_type == "canonical_item" and entry.ref.ref_id in selected_ids]
    assert len(source_refs) == len(selected_ids)
    assert {ref.ref_id for ref in source_refs} == selected_ids
    # 新 prefix epoch 延用业务成员在前、runtime notice 在后的既有顺序。
    business_refs = [ref for ref in source_refs if by_id[ref.ref_id].semantic_kind != "runtime_notice"]
    notice_refs = [ref for ref in source_refs if by_id[ref.ref_id].semantic_kind == "runtime_notice"]
    target_refs = [entry.ref for entry in prepared["snapshot"].selection
                   if entry.included and entry.ref.ref_type == "canonical_item"]
    # 新请求定位目标本地 canonical 正文；只替换新请求的全局归属，原 local ID/manifest 全部保持。
    target_owner = (prepared["snapshot"].session_id, prepared["snapshot"].thread_id)
    expected_refs = [replace(ref, session_id=target_owner[0], thread_id=target_owner[1])
                     for ref in business_refs + notice_refs]
    assert [ref.to_dict() for ref in target_refs if ref.ref_id != new_root_item_id] == [ref.to_dict() for ref in expected_refs]
    roots = [ref for ref in target_refs if ref.ref_id == new_root_item_id]
    assert len(roots) == 1
    assert (roots[0].session_id, roots[0].thread_id, roots[0].semantic_kind) == (
        prepared["snapshot"].session_id, prepared["snapshot"].thread_id, "user_input",
    )
    source_frames = [frame.content for frame in source_prepared["snapshot"].seal_binding.prefix.state.frames
                     if frame.item_ref.ref_type == "canonical_item"
                     and set(frame.content["item_ids"]) <= selected_ids]
    assert {item_id for frame in source_frames for item_id in frame["item_ids"]} == selected_ids
    business_frames = [frame for frame in source_frames if by_id[frame["item_ids"][0]].semantic_kind != "runtime_notice"]
    notice_frames = [frame for frame in source_frames if by_id[frame["item_ids"][0]].semantic_kind == "runtime_notice"]
    expected_frames = business_frames + notice_frames
    target_frames = [frame.content for frame in prepared["snapshot"].seal_binding.prefix.state.frames
                     if frame.item_ref.ref_type == "canonical_item" and frame.content["item_ids"] != [new_root_item_id]]
    assert target_frames == expected_frames
    # 不按写前消息 ID 过滤；每个真实 canonical carrier 都必须完整进入实际 Provider 返回值。
    for request in (source_prepared, prepared):
        canonical_messages = [frame.content["message"] for frame in request["snapshot"].seal_binding.prefix.state.frames
                              if frame.item_ref.ref_type == "canonical_item"]
        actual_messages = [message_to_dict(message) for message in request["messages"]]
        assert [message for message in actual_messages if message in canonical_messages] == canonical_messages
    expected_calls = [call["id"] for frame in expected_frames for call in frame["message"]["data"].get("tool_calls", [])]
    expected_results = [frame["message"]["data"]["tool_call_id"] for frame in expected_frames if frame["message"]["type"] == "tool"]
    calls = [call["id"] for message in prepared["messages"] if isinstance(message, AIMessage) for call in message.tool_calls]
    results = [message.tool_call_id for message in prepared["messages"] if isinstance(message, ToolMessage)]
    assert calls == expected_calls
    assert results == expected_results
    assert calls == results


async def _seed_source(context: ForkIntegrationContext, fork_provider_prepare, *, tail_outcome: str = "completed"):
    source = await context.sessions.create(
        SessionCreateRequest(title="Fork 集成源会话")
    )
    thread_id = context.saver._storage.storage.main_thread_id(source.session_id)
    owner = MutationIntentOwner(
        session_id=source.session_id,
        thread_id=thread_id,
    )
    first_accepted = context.saver.accept_turn(
        source.session_id,
        thread_id=thread_id,
        accepted_ingress_id="u1",
        acceptance_idempotency_key="acceptance-turn-1",
        job_id="job-turn-1",
        admission_idempotency_key="admission-turn-1",
        payload="问题一",
        turn_id="turn-1",
        root_item_id="item-u1",
        initial_execution_id="execution-turn-1",
    )
    first_admission = context.saver.execution_admission(
        source.session_id,
        thread_id=thread_id,
        execution_id=str(first_accepted["initial_execution_id"]),
    )
    first_messages = [
        HumanMessage(
            content="问题一",
            id="u1",
            response_metadata={"turn_id": "turn-1"},
        )
    ]
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(
            first_admission,
            langgraph_thread_id=thread_id,
        ),
    ):
        first_config = await context.saver.aput(
            build_checkpoint_config(thread_id),
            _checkpoint("cp1", first_messages),
            {"source": "stub", "step": 1, "parents": {}},
            {"messages": "1", "counter": "1"},
        )
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(source.session_id, thread_id),
        control_state_port=context.saver,
    )
    fork_provider_prepare(context, source.session_id, first_admission.execution_id,
                          manager, "seed-turn-1", attempt=1)
    second_messages = [*first_messages, AIMessage(content="回答一", id="a1")]
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(
            first_admission,
            langgraph_thread_id=thread_id,
        ),
    ):
        commit_fixture_output(context.saver, messages=second_messages)
        second_config = await context.saver.aput(
            first_config,
            _checkpoint(
                "cp2",
                second_messages,
                pending_sends=[{"node": "pending-node", "attempt": 2}],
            ),
            {"source": "stub", "step": 2, "parents": {}},
            {"messages": "2", "counter": "2"},
        )
    second_accepted = context.saver.accept_turn(
        source.session_id,
        thread_id=thread_id,
        accepted_ingress_id="u2",
        acceptance_idempotency_key="acceptance-turn-2",
        job_id="job-turn-2",
        admission_idempotency_key="admission-turn-2",
        payload="问题二",
        turn_id="turn-2",
        root_item_id="item-u2",
        initial_execution_id="execution-turn-2",
    )
    # cp3 尚未写入时检查 source writer，避免 fork 端裁剪掩盖 checkpoint 污染。
    frozen_cp2 = await _get_tuple(context, source.session_id, checkpoint_id="cp2")
    active = await _get_tuple(context, source.session_id)
    assert frozen_cp2 is not None and active is not None
    assert [message.content for message in frozen_cp2.checkpoint["channel_values"]["messages"]] == [
        "问题一", "回答一",
    ]
    assert [message.content for message in active.checkpoint["channel_values"]["messages"]] == [
        "问题一", "回答一", "问题二",
    ]
    second_admission = context.saver.execution_admission(
        source.session_id,
        thread_id=thread_id,
        execution_id=str(second_accepted["initial_execution_id"]),
    )
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(
            second_admission,
            langgraph_thread_id=thread_id,
        ),
    ):
        await context.saver.aput_writes(
            second_config,
            [("scratchpad", {"pending": True})],
            "task-pending",
            "parent/child",
        )
    with (
        session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
        execution_admission_scope(
            second_admission,
            langgraph_thread_id=thread_id,
        ),
    ):
        await context.saver.aput(
            second_config,
            _checkpoint(
                "cp3",
                [
                    *second_messages,
                    HumanMessage(
                        content="问题二",
                        id="u2",
                        response_metadata={"turn_id": "turn-2"},
                    ),
                ],
            ),
            {"source": "stub", "step": 3, "parents": {}},
            {"messages": "3", "counter": "3"},
        )
    fork_provider_prepare(context, source.session_id, second_admission.execution_id,
                          manager, "seed-turn-2", attempt=1, outcome=tail_outcome)
    return source


def _seed_debug_configurations(
    context: ForkIntegrationContext, source_session_id: str
) -> tuple[str, str]:
    script = context.workspace / "app.mjs"
    script.write_text("const value = 1;\nconsole.log(value);\n", encoding="utf-8")
    created_at = datetime(2026, 8, 15, tzinfo=UTC)
    source_thread_id = context.sessions.path_resolver.main_thread_id(source_session_id)
    active_id = "dbgcfg_11111111111111111111111111111111"
    saved_id = "dbgcfg_22222222222222222222222222222222"
    for configuration_id, name in (
        (active_id, "活动方案"),
        (saved_id, "保存方案"),
    ):
        context.saver._node_debug_store.write_configuration(
            source_session_id,
            NodeDebugConfigurationDTO(
                configuration_id=configuration_id,
                name=name,
                script_path="app.mjs",
                working_directory="",
                launch_profile_name="node-default",
                breakpoints=[
                    NodeDebugConfigurationBreakpointDTO(
                        breakpoint_id=f"bp_{configuration_id[-4:]}",
                        path="app.mjs",
                        line=1,
                        created_at=created_at,
                    )
                ],
                created_at=created_at,
                updated_at=created_at,
            ),
            source_thread_id,
        )
    context.saver._node_debug_store.write_manifest(
        NodeDebugSessionManifestDTO(
            session_id=source_session_id,
            thread_id=source_thread_id,
            active_configuration_id=active_id,
            actions=[
                NodeDebugActionRecordDTO(
                    action_id="act_source_only",
                    session_id=source_session_id,
                    thread_id=source_thread_id,
                    action="continue",
                    message="source runtime only",
                    result="success",
                    created_at=created_at,
                )
            ],
            updated_at=created_at,
        )
    )
    context.saver._node_debug_store.write_launch_claim(
        NodeDebugLaunchClaimDTO(
            session_id=source_session_id,
            thread_id=source_thread_id,
            process_instance_id="node-debug-proc_source",
            nonce="source-nonce",
            phase="running",
            configuration_id=active_id,
            pid=4242,
            process_identity_source="test",
            process_start_marker="source-marker",
            inspector_host="127.0.0.1",
            inspector_port=9311,
            created_at=created_at,
            updated_at=created_at,
        )
    )
    return active_id, saved_id


def _main_thread_id(context: ForkIntegrationContext, session_id: str) -> str:
    return context.sessions.path_resolver.main_thread_id(session_id)


async def _get_tuple(
    context: ForkIntegrationContext,
    session_id: str,
    *,
    saver: RolloutCheckpointSaver | None = None,
    checkpoint_id: str | None = None,
):
    checkpoint_saver = saver or context.saver
    owner = checkpoint_saver.forks.source.resolve_main_thread_owner(session_id)
    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
        return await checkpoint_saver.aget_tuple(
            build_checkpoint_config(
                owner.thread_id,
                checkpoint_id=checkpoint_id,
            )
        )


def _fork_origins(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = _main_thread_node(context, session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT child_session_id, source_session_id, source_checkpoint_id, "
            "source_view_id, fork_mode, relationship FROM fork_origins"
        ).fetchall()


def _retention_refs(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = _main_thread_node(context, session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT reference_kind, reference_id, target_view_id, "
            "owner_session_id, status FROM retention_refs"
        ).fetchall()


def _pending_write_rows(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = _main_thread_node(context, session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT task_id, task_path, write_index, channel, status "
            "FROM pending_writes ORDER BY task_path, write_index"
        ).fetchall()


def _fork_materialization_rows(
    context: ForkIntegrationContext, session_id: str
) -> list[tuple[object, ...]]:
    rollout_root = _main_thread_node(context, session_id)
    with sqlite3.connect(rollout_root / "rollout" / "index.sqlite") as connection:
        return connection.execute(
            "SELECT materialization_id, fork_id, status, copied_message_count "
            "FROM fork_materializations"
        ).fetchall()




def _main_thread_node(context: ForkIntegrationContext, session_id: str) -> Path:
    thread_id = context.saver._storage.storage.main_thread_id(session_id)
    return context.sessions.path_resolver.resolve_thread_node(session_id, thread_id)


def _turn_history_clocks(
    context: ForkIntegrationContext, session_id: str,
) -> dict[str, tuple[str, ...]]:
    with sqlite3.connect(_main_thread_node(context, session_id) / "rollout" / "index.sqlite") as connection:
        return {
            row[0]: tuple(row[1:]) for row in connection.execute(
                "SELECT tr.turn_id, tr.created_at, tr.updated_at, a.created_at, "
                "t.created_at, t.updated_at, e.created_at, e.updated_at "
                "FROM turn_records tr JOIN turn_acceptances a ON a.turn_id = tr.turn_id "
                "JOIN turns t ON t.turn_id = tr.turn_id "
                "JOIN executions e ON e.execution_id = tr.last_execution_id"
            )
        }


def _message_history_clocks(context: ForkIntegrationContext, session_id: str) -> dict[str, str]:
    with sqlite3.connect(_main_thread_node(context, session_id) / "rollout" / "index.sqlite") as connection:
        return dict(connection.execute("SELECT message_id, created_at FROM messages"))


@pytest.mark.asyncio
async def test_child_message_ingest_keeps_exact_attachment_owner_through_source_point(
    production_fork_container: AppContainer, fork_provider_prepare,
) -> None:
    """真实消息入口写入 child 附件，候选读取与同字节 main 附件仍分别归属。"""
    container = production_fork_container
    context = _production_fork_context(container)
    source = await _seed_source(context, fork_provider_prepare)
    child = await context.forks.fork(await _fork_request(
        context, source.session_id, creation_kind="new_child_thread", mode="full_rollout_copy",
    ))
    body = b"child-inline-attachment" * 3200
    request = MessageCreateRequest(content="child 独有附件", attachments=[AttachmentRef(
        file_id="inline:child.txt", name="child.txt", content_type="text/plain",
        data_url="data:text/plain;base64," + base64.b64encode(body).decode("ascii"),
    )])
    message = await container.message_service.create(source.session_id, request, thread_id=child.thread_id)
    ref = message.attachments[0]
    store = container.attachment_blob_store
    attachment_id = ref.file_id.rsplit("/", 1)[1]
    owner = store._catalog.get_owner_ref(attachment_id)
    assert owner is not None and (owner.owner_session_id, owner.owner_thread_id) == (source.session_id, child.thread_id)
    assert store.read(source.session_id, ref.file_id, owner_thread_id=child.thread_id).data == body
    with pytest.raises(PermissionError, match="当前 SessionThread"):
        store.read(source.session_id, ref.file_id, owner_thread_id=source.thread_id)
    repeated = await container.message_service.create(source.session_id, request, thread_id=child.thread_id)
    assert repeated.attachments[0].file_id == ref.file_id
    main_message = await container.message_service.create(source.session_id, request, thread_id=source.thread_id)
    assert main_message.attachments[0].file_id != ref.file_id
    main_owner = store._catalog.get_owner_ref(main_message.attachments[0].file_id.rsplit("/", 1)[1])
    assert main_owner is not None and (main_owner.owner_thread_id, main_owner.blob_id) == (source.thread_id, owner.blob_id)

    identity = container.job_service.reserve_user_execution_identity()
    container.message_service.accept_user_message(message, execution_identity=identity)
    admission = container.message_service.execution_admission_for_job(
        source.session_id, thread_id=child.thread_id, job_id=identity.job_id,
    )
    manager = ContextSourceManager(owner=ContextSourceOwnerKey(source.session_id, child.thread_id), control_state_port=context.saver)
    fork_provider_prepare(context, source.session_id, admission.execution_id, manager,
                          "child-inline-ingress", thread_id=child.thread_id, attempt=1, outcome="completed_empty")
    context.saver.converge_execution(
        source.session_id, thread_id=child.thread_id, turn_id=identity.turn_id,
        execution_id=admission.execution_id, outcome="completed_empty", turn_status="completed_empty",
    )
    point = await context.forks.source_point(session_id=source.session_id, thread_id=child.thread_id, mode="full_rollout_copy")
    assert point.source_uri.endswith(f"/{source.session_id}/threads/{child.thread_id}")
    assert [(resource.resource_uri, resource.availability, resource.required_for_context) for resource in point.resources] == [
        (ref.file_id, "available", True),
    ]
    assert not await container.job_service.list()


@pytest.mark.asyncio
@pytest.mark.parametrize("input_kind", ["user", "internal"])
@pytest.mark.parametrize("creation_kind", ["new_child_thread", "new_session_main"])
@pytest.mark.parametrize("mode", ["view_prefix_fork", "full_rollout_copy"])
async def test_fork_attachment_retention_uses_real_owner_and_cold_target_tool(
    production_fork_container: AppContainer, fork_provider_prepare, creation_kind: SessionForkCreationKind,
    mode: SessionForkMode, input_kind: str,
) -> None:
    """真实 fork 证明 main 复用、独立保活、删除源后冷读和目标新工具 capability。"""
    context = _production_fork_context(production_fork_container)
    store = production_fork_container.attachment_blob_store
    source = await context.sessions.create(SessionCreateRequest(title="附件 fork 来源"))
    source_thread = _main_thread_id(context, source.session_id)
    source_node = _main_thread_node(context, source.session_id)
    body = b"fork attachment original bytes\n"
    messages = production_fork_container.message_service
    prepared = internal_message_factory.build(
        kind="generated_session_result", control="处理附带的结果文件。",
        sections=(PromptSection("control_context", {"source_session_id": source.session_id}),
                  PromptSection("generated_session_result", "附件内部正文")),
        display_content="结果文件已就绪。",
    ) if input_kind == "internal" else None
    message = await messages.create(source.session_id, MessageCreateRequest(
        content=prepared.content if prepared is not None else "附件源问题",
        metadata=prepared.metadata if prepared is not None else {},
        attachments=[AttachmentRef(
            file_id="inline:fork-source.txt", name="fork-source.txt", content_type="text/plain",
            data_url="data:text/plain;base64," + base64.b64encode(body).decode("ascii"),
        )],
    ), thread_id=source_thread)
    ref = message.attachments[0]
    if prepared is None:
        identity = production_fork_container.job_service.reserve_user_execution_identity()
        messages.accept_user_message(message, execution_identity=identity)
        job_id = identity.job_id
        frozen = context.saver.accepted_user_message(
            source.session_id, thread_id=source_thread, job_id=job_id, message_id=message.message_id,
        )
    else:
        intent = InternalExecutionIntent(
            message_id=message.message_id, execution_id=create_uuid_hex(namespace="execution"),
            admission_idempotency_key=create_uuid_hex(namespace="admission"),
        )
        job_id = create_uuid_hex(namespace="job")
        messages.accept_job_input(SimpleNamespace(
            session_id=source.session_id, thread_id=source_thread, job_id=job_id,
            execution_binding=intent, message_id=message.message_id, message=message.content,
            attachments=message.attachments, message_metadata=message.metadata,
            message_created_at=message.created_at.isoformat(),
        ))
        frozen = context.saver.accepted_internal_message(
            source.session_id, thread_id=source_thread, job_id=job_id, message_id=message.message_id, intent=intent,
        )
    source_admission = context.saver.execution_admission_for_job(source.session_id, thread_id=source_thread, job_id=job_id)
    with (session_thread_owner_scope(MutationIntentOwner(source.session_id, source_thread), langgraph_thread_id=source_thread),
          execution_admission_scope(source_admission, langgraph_thread_id=source_thread)):
        await context.saver.aput(
            build_checkpoint_config(source_thread), _checkpoint("resource-source-cp", [frozen]),
            {"source": "input", "step": 1, "parents": {}}, {"messages": "1", "counter": "1"},
        )
    fork_provider_prepare(
        context, source.session_id, source_admission.execution_id,
        ContextSourceManager(owner=ContextSourceOwnerKey(source.session_id, source_thread), control_state_port=context.saver),
        "resource-source", attempt=1, outcome="completed_empty",
    )
    context.saver.converge_execution(
        source.session_id, thread_id=source_thread, turn_id=source_admission.turn_id,
        execution_id=source_admission.execution_id, outcome="completed_empty", turn_status="completed_empty" if source_admission.turn_id is not None else None,
    )
    original = context.saver._storage.storage.read_items(source.session_id, source_thread)
    result = await context.forks.fork(await _fork_request(
        context, source.session_id, creation_kind=creation_kind, mode=mode, item_id="item-" + message.message_id,
    ))
    assert len(result.resources) == 1
    receipt = result.resources[0]
    assert receipt.resource_uri == ref.file_id and receipt.availability == "available"
    assert receipt.action == ("reuse_reference" if creation_kind == "new_child_thread" else "retain_reference")
    assert receipt.target_uri is not None
    assert store.read(result.session.session_id, ref.file_id, owner_thread_id=result.thread_id).data == body
    copied = context.saver._storage.storage.read_items(result.session.session_id, result.thread_id)
    assert {item.item_id: item.to_dict() for item in copied} == {item.item_id: item.to_dict() for item in original}
    blob = store._catalog.get_blob_by_digest("sha256:" + hashlib.sha256(body).hexdigest())
    assert blob is not None
    assert len(store._catalog.list_active_owner_refs_for_blob(blob.blob_id)) == (1 if creation_kind == "new_child_thread" else 2)
    claims = store._catalog._connected().execute(
        "SELECT state FROM fork_attachment_claims WHERE target_session_id=? AND target_thread_id=?",
        (result.session.session_id, result.thread_id),
    ).fetchall()
    control = context.forks._threads.for_owner_session(result.session.session_id).control_store
    settlements = read_copy_attachment_settlements(control)
    assert [row[0] for row in claims] == ([] if creation_kind == "new_child_thread" else ["committed"])
    assert [row.state for row in settlements if row.target_thread_id == result.thread_id] == (
        [] if creation_kind == "new_child_thread" else ["committed"]
    )
    if creation_kind == "new_session_main":
        await context.sessions.path_resolver.delete_subtree(
            idempotency_key="resource-source-delete", root_node_id=source.session_id,
        )
        assert not source_node.exists()
    assert store._catalog.list_gc_candidates(before=datetime.now(UTC)) == ()
    cold_store = AttachmentBlobStore(context.workspace, path_resolver=context.sessions.path_resolver)
    cold = RolloutCheckpointSaver(context.sessions.path_resolver.sessions_root, attachment_store=cold_store)
    try:
        assert cold_store.read(result.session.session_id, ref.file_id, owner_thread_id=result.thread_id).data == body
        new_turn = cold.accept_turn(
            result.session.session_id, thread_id=result.thread_id, accepted_ingress_id="resource-target-input",
            acceptance_idempotency_key="resource-target-accept", job_id="resource-target-job",
            admission_idempotency_key="resource-target-admission", root_item_id="resource-target-root", payload="读取继承附件",
        )
        target_admission = cold.execution_admission(
            result.session.session_id, thread_id=result.thread_id, execution_id=new_turn["initial_execution_id"],
        )
        prepared = fork_provider_prepare(
            replace(context, saver=cold), result.session.session_id, target_admission.execution_id,
            ContextSourceManager(owner=ContextSourceOwnerKey(result.session.session_id, result.thread_id), control_state_port=cold),
            "resource-target", attempt=1, thread_id=result.thread_id,
            selected_tools=ToolSetSnapshot(selection_revision=sha256_jcs({"read_file": True}),
                tools=({"name": "read_file", "parameters": {"type": "object"}},)),
        )
        with (session_thread_owner_scope(MutationIntentOwner(result.session.session_id, result.thread_id), langgraph_thread_id=result.thread_id),
              execution_admission_scope(target_admission, langgraph_thread_id=result.thread_id)):
            cold.ensure_request_items(
                result.session.session_id, result.thread_id, turn_id=new_turn["turn_id"], execution_id=target_admission.execution_id,
                messages=[AIMessage(content="", id="lc_run--" + prepared["snapshot"].model_call_id,
                    tool_calls=[{"id": "read-inherited-attachment", "name": "read_file", "args": {"path": receipt.target_uri}, "type": "tool_call"}])],
            )
            if input_kind == "internal":
                # 后继用户 acceptance 已消费 pending notice，其私有附件不能泄入新工具能力。
                with pytest.raises(PermissionError, match="原始 assembly"):
                    cold.read_tool_call_attachment(
                        result.session.session_id, thread_id=result.thread_id, tool_call_id="read-inherited-attachment",
                        model_call_id=prepared["snapshot"].model_call_id, file_id=receipt.target_uri,
                    )
            else:
                assert cold.read_tool_call_attachment(
                    result.session.session_id, thread_id=result.thread_id, tool_call_id="read-inherited-attachment",
                    model_call_id=prepared["snapshot"].model_call_id, file_id=receipt.target_uri,
                ).data == body
            if creation_kind == "new_child_thread":
                await cold.adelete_thread(result.thread_id)
        if creation_kind == "new_child_thread":
            assert cold_store.read(source.session_id, ref.file_id, owner_thread_id=source_thread).data == body
            assert len(cold_store._catalog.list_active_owner_refs_for_blob(blob.blob_id)) == 1
        else:
            target_node = _main_thread_node(context, result.session.session_id)
            await context.sessions.path_resolver.delete_subtree(
                idempotency_key="resource-target-delete", root_node_id=result.session.session_id,
            )
            assert not target_node.exists()
            assert store._catalog.list_active_owner_refs_for_blob(blob.blob_id) == ()
            assert [row[0] for row in store._catalog._connected().execute(
                "SELECT state FROM fork_attachment_claims WHERE target_session_id=?", (result.session.session_id,),
            )] == ["target_deleted"]
    finally:
        cold_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["published", "aborted"])
async def test_fork_settlement_retries_retained_upload_pin_without_restart(
    production_fork_container: AppContainer, fork_provider_prepare, monkeypatch, outcome: str,
) -> None:
    """先在真实 fork 准备窗口保留 pin，再由发布或中止入口定点恢复。"""
    from app.services.infrastructure.rollout_context.checkpoint import fork_owner

    container = production_fork_container
    context = _production_fork_context(container)
    store = container.attachment_blob_store
    source = await context.sessions.create(SessionCreateRequest(title="pin 来源"))
    target = await context.sessions.create(SessionCreateRequest(title="pin 目标"))
    source_thread, target_thread = _main_thread_id(context, source.session_id), _main_thread_id(context, target.session_id)
    body = b"fork keeps original upload recovery identity"
    request = MessageCreateRequest(content="附件", attachments=[AttachmentRef(
        file_id="inline:pin", name="pin.txt", content_type="text/plain",
        data_url="data:text/plain;base64," + base64.b64encode(body).decode(),
    )])
    message = await container.message_service.create(source.session_id, request, thread_id=source_thread)
    identity = container.job_service.reserve_user_execution_identity()
    container.message_service.accept_user_message(message, execution_identity=identity)
    admission = container.message_service.execution_admission_for_job(source.session_id, thread_id=source_thread, job_id=identity.job_id)
    frozen = context.saver.accepted_user_message(source.session_id, thread_id=source_thread,
        job_id=identity.job_id, message_id=message.message_id)
    with (session_thread_owner_scope(MutationIntentOwner(source.session_id, source_thread), langgraph_thread_id=source_thread),
          execution_admission_scope(admission, langgraph_thread_id=source_thread)):
        await context.saver.aput(build_checkpoint_config(source_thread), _checkpoint("pin-source-cp", [frozen]),
            {"source": "input", "step": 1, "parents": {}}, {"messages": "1", "counter": "1"})
    fork_provider_prepare(context, source.session_id, admission.execution_id,
        ContextSourceManager(owner=ContextSourceOwnerKey(source.session_id, source_thread), control_state_port=context.saver),
        "pin-source", attempt=1, outcome="completed_empty")
    context.saver.converge_execution(source.session_id, thread_id=source_thread, turn_id=admission.turn_id,
        execution_id=admission.execution_id, outcome="completed_empty", turn_status="completed_empty")
    abandoned = await container.message_service.create(target.session_id, request, thread_id=target_thread)
    with closing(store._open_control_store(target.session_id)) as control:
        (pin,) = control.list_non_terminal_leases()
    fork_request = await _fork_request(context, source.session_id, creation_kind="new_child_thread",
        item_id="item-" + message.message_id, target_container_uri=resource_display_uri(scope="workspace", scope_id=target.workspace_id,
            kind="session", tail_segments=(target.session_id,)))
    original_observer = fork_owner.observe_phase
    retained = []

    async def after_capture(operation_id: str, phase: str, **kwargs: object) -> None:
        if phase == "copy.source.captured":
            retained.extend(await store.recover_idle_session(target.session_id, messages=context.saver))
            assert retained == ["retained-pin"]
            with closing(store._open_control_store(target.session_id)) as control:
                assert control.get_lease(pin.lease_id) == pin
            if outcome == "aborted":
                raise ValueError("注入 fork 准备失败")
        await original_observer(operation_id, phase, **kwargs)

    monkeypatch.setattr(fork_owner, "observe_phase", after_capture)
    if outcome == "aborted":
        with pytest.raises(ValueError, match="注入 fork 准备失败"):
            await context.forks.fork(fork_request)
        with pytest.raises(FileNotFoundError):
            store.read(target.session_id, abandoned.attachments[0].file_id)
    else:
        result = await context.forks.fork(fork_request)
        assert result.resources[0].action == "reuse_reference"
        assert store.read(target.session_id, message.attachments[0].file_id, owner_thread_id=result.thread_id).data == body
    assert retained == ["retained-pin"]
    with closing(store._open_control_store(target.session_id)) as control:
        settled = control.get_lease(pin.lease_id)
        assert settled.state == "failed" and settled.fencing_token == pin.fencing_token + 1
    assert store.read(source.session_id, message.attachments[0].file_id).data == body


@pytest.mark.asyncio
async def test_default_fork_publishes_idle_child_without_interrupting_running_main(
    fork_context: ForkIntegrationContext, fork_provider_prepare,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    saver = fork_context.saver
    storage = saver._storage.storage
    main_thread = _main_thread_id(fork_context, source.session_id)
    control = fork_context.forks._threads.for_owner_session(source.session_id).control_store
    main_before = dict(control.get_main_thread())
    children_before = control.list_child_thread_rows()
    initial_intents_before = control.list_initial_execution_intents()
    source_index = storage.index_path(source.session_id, thread_id=main_thread)
    source_jsonl = storage.jsonl_path(source.session_id, thread_id=main_thread)
    with sqlite3.connect(source_index) as connection:
        source_sql_before = tuple(connection.iterdump())
    source_jsonl_before = source_jsonl.read_bytes()
    source_latest = await _get_tuple(fork_context, source.session_id)
    source_completed = await _get_tuple(fork_context, source.session_id, checkpoint_id="cp2")
    assert source_latest is not None and source_completed is not None
    source_admission = saver.execution_admission(
        source.session_id, thread_id=main_thread, execution_id="execution-turn-2",
    )
    point = await fork_context.forks.source_point(session_id=source.session_id)
    # 不传任何可选项，直接证明公开请求的默认创建、关系与模式。
    request = SessionForkRequest(
        idempotency_key="default-idle-child", source_uri=point.source_uri,
        source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
        anchor_item_id=point.anchor_item_id, target_container_uri=point.child_container_uri,
    )
    result = await fork_context.forks.fork(request)
    assert result.session.session_id == source.session_id
    assert result.thread_id != main_thread
    child = result.thread_id
    assert {row.thread_id for row in control.list_child_thread_rows()} == {
        *(row.thread_id for row in children_before), child,
    }
    record = control.get_thread_creation_record(fork_context.forks._creation_key(request.idempotency_key))
    assert (record.state, record.purpose, record.initial_state, record.admission_intent) == (
        "published", "fork", "idle", None,
    )
    assert record.child_thread_id == child
    assert control.list_initial_execution_intents() == initial_intents_before
    assert dict(control.get_main_thread()) == main_before
    with sqlite3.connect(source_index) as connection:
        assert tuple(connection.iterdump()) == source_sql_before
    assert source_jsonl.read_bytes() == source_jsonl_before
    assert saver.execution_admission(
        source.session_id, thread_id=main_thread, execution_id="execution-turn-2",
    ) == source_admission

    # child 发布后，原 admission 仍可追加主线程输出并完成原 Turn。
    with (
        session_thread_owner_scope(MutationIntentOwner(source.session_id, main_thread), langgraph_thread_id=main_thread),
        execution_admission_scope(source_admission, langgraph_thread_id=main_thread),
    ):
        completed_messages = [
                *source_latest.checkpoint["channel_values"]["messages"],
                AIMessage(content="主线程继续回答二", id="continued-main-final"),
            ]
        commit_fixture_output(saver, messages=completed_messages)
        await saver.aput(
            source_latest.config,
            _checkpoint("continued-main-checkpoint", completed_messages),
            {"source": "loop", "step": 4, "parents": {}}, {"messages": "4", "counter": "4"},
        )
    continued = await _get_tuple(fork_context, source.session_id)
    assert continued is not None
    assert continued.checkpoint["channel_values"]["messages"][-1].content == "主线程继续回答二"

    cold = RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root)
    child_owner = MutationIntentOwner(source.session_id, child)
    with session_thread_owner_scope(child_owner, langgraph_thread_id=child):
        child_checkpoint = await cold.aget_tuple(build_checkpoint_config(child))
        child_history = await cold.aget_tuple(build_checkpoint_config(child, checkpoint_id="cp2"))
    assert child_checkpoint is not None
    assert child_history is not None
    assert source_completed.checkpoint["pending_sends"] == [{"node": "pending-node", "attempt": 2}]
    assert source_completed.pending_writes == [("task-pending", "scratchpad", {"pending": True})]
    assert child_history.checkpoint == source_completed.checkpoint
    assert child_history.metadata == source_completed.metadata
    assert child_history.pending_writes == source_completed.pending_writes
    assert child_checkpoint.checkpoint["channel_values"]["messages"] == source_completed.checkpoint["channel_values"]["messages"]
    assert child_checkpoint.checkpoint["pending_sends"] == []
    assert child_checkpoint.pending_writes == []
    child_accepted = cold.accept_turn(
        source.session_id, thread_id=child, accepted_ingress_id="idle-child-ingress",
        acceptance_idempotency_key="idle-child-accept", job_id="idle-child-job",
        admission_idempotency_key="idle-child-admission", payload="child 首次问题",
    )
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(source.session_id, child), control_state_port=cold,
    )
    prepared = fork_provider_prepare(
        replace(fork_context, saver=cold), source.session_id,
        str(child_accepted["initial_execution_id"]), manager, "idle-child-first",
        attempt=1, thread_id=child,
    )
    assert [(type(message), message.content) for message in prepared["messages"]
            if message.content in {"问题一", "回答一", "child 首次问题"}] == [
        (HumanMessage, "问题一"), (AIMessage, "回答一"), (HumanMessage, "child 首次问题"),
    ]
    assert all(message.content not in {"问题二", "主线程继续回答二"} for message in prepared["messages"])
    child_admission = cold.execution_admission(
        source.session_id, thread_id=child, execution_id=str(child_accepted["initial_execution_id"]),
    )
    with (
        session_thread_owner_scope(child_owner, langgraph_thread_id=child),
        execution_admission_scope(child_admission, langgraph_thread_id=child),
    ):
        from app.agents.checkpoint_recovery import persisted_pending_tool_candidate

        assert await persisted_pending_tool_candidate(cold, child_admission) is None
        current = await cold.aget_tuple(build_checkpoint_config(child))
        assert current is not None
        assert current.checkpoint["pending_sends"] == []
        assert current.pending_writes == []
        completed_messages = [
                *current.checkpoint["channel_values"]["messages"],
                AIMessage(content="child 首次回答", id="idle-child-final",
                          response_metadata={"model_call_id": prepared["snapshot"].model_call_id}),
            ]
        commit_fixture_output(cold, messages=completed_messages)
        await cold.aput(
            current.config,
            _checkpoint("idle-child-first-checkpoint", completed_messages),
            {"source": "loop", "step": 1, "parents": {}}, {"messages": "4", "counter": "4"},
        )
    assert dict(control.get_main_thread()) == main_before
    main_after = await _get_tuple(fork_context, source.session_id)
    assert main_after is not None
    assert main_after.checkpoint == continued.checkpoint


@pytest.mark.asyncio
async def test_fork_uses_typed_source_observation_not_extension_controls(
    fork_context: ForkIntegrationContext,
    fork_provider_prepare,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.business import session_context_fork_service as fork_module

    source = await fork_context.sessions.create(
        SessionCreateRequest(title="Typed source observation fork")
    )
    saver = fork_context.saver
    source_thread = _main_thread_id(fork_context, source.session_id)
    source_owner = ContextSourceOwnerKey(source.session_id, source_thread)
    accepted = saver.accept_turn(
        source.session_id,
        thread_id=source_thread,
        accepted_ingress_id="typed-observation-ingress",
        acceptance_idempotency_key="typed-observation-accept",
        job_id="typed-observation-job",
        admission_idempotency_key="typed-observation-admission",
        payload="验证 typed 来源控制",
    )
    source_id = "instruction:typed-observation"
    source_revision = "typed-observation-revision-1"
    observation = SourceObservation(
        owner=source_owner,
        source_id=source_id,
        source_kind="workspace_instructions",
        name="Typed observation",
        revision=source_revision,
        tracking_mode="tracked",
    )
    decision = build_source_lifecycle_decision(
        observation, decision_kind="track"
    )
    saver.consume_mutation_intent(decision)
    (source_state,) = saver.load_context_source_control_states(source_owner)
    assert (
        source_state.owner,
        source_state.source_id,
        source_state.tracking_status,
        source_state.latest_revision,
    ) == (source_owner, source_id, "tracked", source_revision)

    manager = ContextSourceManager(
        owner=source_owner,
        control_state_port=saver,
    )
    manager.register(
        ContextSourceDescriptor(
            source_id=source_id,
            source_kind="workspace_instructions",
            name="Typed observation",
            description="通过 typed lifecycle decision 持久化的来源",
            internal_locator="source-owner://typed-observation",
        ),
        tracking_status="tracked",
    )
    assert manager.source_observation_state(source_id) == (
        "tracked",
        source_revision,
    )

    request_content = "来自 typed registry 的 request-only 正文"
    assert manager.observe(
        source_id, request_content, revision=source_revision
    )
    adversarial_controls = {
        "source_ordinal": 999,
        "replaceable_source": True,
        "replacement_policy": "replaceable",
        "selection_only": True,
        "selection_role": "backing_only",
        "root_placement": "root_eligible",
        "tracking_state": "untracked",
        "tracking_status": "untracked",
        "role": "delta",
        "base_delta_role": "delta",
    }
    extensions = {
        "vendor.future/context": {
            "extension_version": "future-19",
            "protection": "public",
            "encoding": "jcs:v1",
            "value": adversarial_controls,
        }
    }
    register_context_contribution = saver.register_context_contribution
    injected_contributions: list[ContextContribution] = []

    def register_with_opaque_extension(
        session_id: str,
        thread_id: str,
        contribution: ContextContribution,
        *,
        checkpoint_ns: str = "",
        request_content: object | None = None,
        expected_source_revision: str | None = None,
    ) -> None:
        binding = contribution.source_binding
        if (
            contribution.contribution_kind == "prompt"
            and binding is not None
            and binding.source_ref == source_id
        ):
            contribution = replace(contribution, extensions=extensions)
            injected_contributions.append(contribution)
        register_context_contribution(
            session_id,
            thread_id,
            contribution,
            checkpoint_ns=checkpoint_ns,
            request_content=request_content,
            expected_source_revision=expected_source_revision,
        )

    monkeypatch.setattr(
        saver, "register_context_contribution", register_with_opaque_extension
    )

    prepared = fork_provider_prepare(
        fork_context,
        source.session_id,
        str(accepted["initial_execution_id"]),
        manager,
        "typed-observation-source",
        attempt=1,
        selected_tools=ToolSetSnapshot(
            selection_revision=sha256_jcs({"read_file": True}),
            tools=(
                {
                    "name": "read_file",
                    "parameters": {"type": "object"},
                },
            ),
        ),
    )
    source_snapshot = prepared["snapshot"]
    (sealed_source_control_state,) = saver.load_context_source_control_states(
        source_owner
    )
    assert (
        sealed_source_control_state.latest_visible_committed_revision,
        sealed_source_control_state.latest_revision,
    ) == (source_revision, source_revision)
    (contribution,) = injected_contributions
    (registered,) = [
        row
        for row in saver._storage.assemblies.list_context_contributions(
            source.session_id, source_thread
        )
        if row["contribution_id"] == contribution.contribution_id
    ]
    assert (
        registered["extensions"],
        registered["selection_role"],
        registered["replacement_policy"],
        registered["root_placement"],
        registered["source_ordinal"],
    ) == (extensions, "direct", "immutable", "tail_only", 0)
    (source_selection,) = [
        entry
        for entry in source_snapshot.selection
        if entry.contribution_id == contribution.contribution_id
    ]
    assert (
        source_selection.included,
        source_selection.ref.ref_type,
        source_selection.selection_kind,
        source_selection.base_delta_role,
    ) == (True, "request_only", "request_only", "none")
    assert source_selection.plan_ordinal != adversarial_controls["source_ordinal"]
    (sealed_contribution,) = [
        item
        for item in source_snapshot.contributions
        if item.contribution_id == contribution.contribution_id
    ]
    assert sealed_contribution.extensions == extensions
    assert source_selection.contribution_ordinal == sealed_contribution.contribution_ordinal
    assert sealed_contribution.assembly_id == source_snapshot.assembly_id
    assert any(
        isinstance(message, HumanMessage) and message.content == request_content
        for message in prepared["messages"]
    )

    source_user = next(
        message
        for message in prepared["messages"]
        if isinstance(message, HumanMessage)
        and message.content == "验证 typed 来源控制"
    )
    admission = saver.execution_admission(
        source.session_id,
        thread_id=source_thread,
        execution_id=str(accepted["initial_execution_id"]),
    )
    with (
        session_thread_owner_scope(
            MutationIntentOwner(source.session_id, source_thread),
            langgraph_thread_id=source_thread,
        ),
        execution_admission_scope(admission, langgraph_thread_id=source_thread),
    ):
        completed_messages = [
                    source_user,
                    AIMessage(
                        content="typed 来源完成",
                        id="typed-observation-source-answer",
                        response_metadata={
                            "model_call_id": source_snapshot.model_call_id
                        },
                    ),
                ]
        commit_fixture_output(saver, messages=completed_messages)
        await saver.aput(
            build_checkpoint_config(source_thread),
            _checkpoint(
                "typed-observation-source-final",
                completed_messages,
            ),
            {"source": "loop", "step": 1, "parents": {}},
            {"messages": "2", "counter": "2"},
        )
    source_index = _main_thread_node(fork_context, source.session_id) / "rollout" / "index.sqlite"
    with closing(sqlite3.connect(source_index)) as connection:
        (source_assembly_bytes,) = connection.execute(
            "SELECT snapshot_json FROM context_assemblies WHERE assembly_id = ?",
            (source_snapshot.assembly_id,),
        ).fetchone()

    successful_request = await _fork_request(
        fork_context,
        source.session_id,
        creation_kind="new_session_main",
        mode="context_fork",
        title="typed observation cold target",
    )
    successful_result = await fork_context.forks.fork(successful_request)
    catalog = fork_context.sessions.path_resolver.catalog_store
    successful_key = fork_context.forks._creation_key(
        successful_request.idempotency_key
    )
    assert catalog.get_creation_record(successful_key).state == "published"

    target_session_id = successful_result.session.session_id
    target_thread_id = successful_result.thread_id
    target_storage_owner = ContextSourceOwnerKey(
        target_session_id, target_thread_id
    )
    assert target_storage_owner != source_owner
    cold = RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root)
    assert cold.load_context_source_control_states(source_owner) == (
        sealed_source_control_state,
    )
    assert cold.load_context_source_control_states(target_storage_owner) == ()
    cold_snapshot = cold._storage.assemblies.get_context_assembly(
        target_session_id,
        target_thread_id,
        assembly_id=source_snapshot.assembly_id,
    )
    assert (cold_snapshot.session_id, cold_snapshot.thread_id) == (
        source.session_id,
        source_thread,
    )
    assert json.dumps(cold_snapshot.to_dict(), sort_keys=True, ensure_ascii=False) == json.dumps(
        source_snapshot.to_dict(), sort_keys=True, ensure_ascii=False
    )
    (cold_selection,) = [
        entry
        for entry in cold_snapshot.selection
        if entry.contribution_id == contribution.contribution_id
    ]
    assert (
        cold_selection.selection_kind,
        cold_selection.base_delta_role,
        cold_selection.contribution_ordinal,
    ) == (
        "request_only",
        "none",
        source_selection.contribution_ordinal,
    )

    cold_context = replace(fork_context, saver=cold)
    target_manager = ContextSourceManager(
        owner=target_storage_owner,
        control_state_port=cold,
    )
    assert cold.load_context_source_control_states(target_storage_owner) == ()
    target_accepted = cold.accept_turn(
        target_session_id,
        thread_id=cold._storage.storage.main_thread_id(target_session_id), accepted_ingress_id="typed-observation-target-ingress",
        acceptance_idempotency_key="typed-observation-target-accept",
        job_id="typed-observation-target-job",
        admission_idempotency_key="typed-observation-target-admission",
        payload="首次 target 请求",
    )
    target_prepared = fork_provider_prepare(
        cold_context,
        target_session_id,
        str(target_accepted["initial_execution_id"]),
        target_manager,
        "typed-observation-target-first",
        attempt=1,
    )
    target_snapshot = cold._storage.assemblies.get_context_assembly(
        target_session_id,
        target_thread_id,
        assembly_id=target_prepared["assembly_id"],
    )
    (target_selection,) = [
        entry
        for entry in target_snapshot.selection
        if entry.contribution_id == contribution.contribution_id
    ]
    (target_contribution,) = [
        item
        for item in target_snapshot.contributions
        if item.contribution_id == contribution.contribution_id
    ]
    assert (
        target_selection.included,
        target_selection.ref.ref_type,
        target_selection.selection_kind,
        target_selection.base_delta_role,
    ) == (True, "request_only", "request_only", "none")
    assert (
        target_contribution.extensions,
        target_contribution.root_placement,
        target_contribution.selection_role,
        target_contribution.replacement_policy,
        target_contribution.source_ordinal,
    ) == (extensions, "tail_only", "direct", "immutable", None)
    (target_tail_frame,) = [
        frame
        for frame in target_snapshot.seal_binding.prefix.state.frames
        if frame.item_ref.ref_id == target_selection.ref.ref_id
    ]
    assert (
        target_tail_frame.role,
        target_tail_frame.item_ref.source_identity,
        target_tail_frame.item_ref.source_revision,
        target_tail_frame.content,
    ) == ("user", source_id, source_revision, request_content)
    assert target_snapshot.seal_binding.root is None
    (target_registered,) = [
        row
        for row in cold._storage.assemblies.list_context_contributions(
            target_session_id, target_thread_id
        )
        if row["contribution_id"] == contribution.contribution_id
    ]
    assert target_registered["source_ordinal"] == registered["source_ordinal"]
    assert target_registered["source_ordinal"] != adversarial_controls["source_ordinal"]
    assert (
        target_registered["selection_role"],
        target_registered["replacement_policy"],
        target_registered["root_placement"],
    ) == ("direct", "immutable", "tail_only")
    assert cold.load_context_source_control_states(target_storage_owner) == ()
    with closing(sqlite3.connect(source_index)) as connection:
        (source_assembly_bytes_after_target_prepare,) = connection.execute(
            "SELECT snapshot_json FROM context_assemblies WHERE assembly_id = ?",
            (source_snapshot.assembly_id,),
        ).fetchone()
    assert source_assembly_bytes_after_target_prepare == source_assembly_bytes

    invalid_request = await _fork_request(
        fork_context,
        source.session_id,
        creation_kind="new_session_main",
        mode="context_fork",
        title="tampered typed observation target",
    )
    assert invalid_request.idempotency_key != successful_request.idempotency_key
    invalid_key = fork_context.forks._creation_key(invalid_request.idempotency_key)
    original_observer = fork_module.observe_phase
    tampered_phases: list[str] = []

    async def tamper_after_reservation(
        operation_id: str, phase: str, **kwargs: object
    ) -> None:
        await original_observer(operation_id, phase, **kwargs)
        if operation_id != invalid_key or phase != "copy.target.prepared":
            return
        reservation = catalog.get_creation_record(invalid_key)
        assert reservation.state == "preparing"
        with closing(sqlite3.connect(source_index)) as connection:
            (snapshot_json,) = connection.execute(
                "SELECT snapshot_json FROM context_assemblies WHERE assembly_id = ?",
                (source_snapshot.assembly_id,),
            ).fetchone()
            tampered_snapshot = json.loads(snapshot_json)
            (tampered_selection,) = [
                entry
                for entry in tampered_snapshot["selection"]
                if entry["contribution_id"] == contribution.contribution_id
            ]
            assert tampered_selection["ref"]["ref_type"] == "request_only"
            assert tampered_selection["selection_kind"] == "request_only"
            tampered_selection["selection_kind"] = "extension-forged-selection"
            cursor = connection.execute(
                "UPDATE context_assemblies SET snapshot_json = ? WHERE assembly_id = ?",
                (
                    canonical_json_bytes(tampered_snapshot).decode("utf-8"),
                    source_snapshot.assembly_id,
                ),
            )
            assert cursor.rowcount == 1
            connection.commit()
            tampered_phases.append(phase)

    monkeypatch.setattr(fork_module, "observe_phase", tamper_after_reservation)
    published_before_failure = (await fork_context.sessions.list(limit=100)).total
    with pytest.raises(ItemSchemaError, match="未知 selection_kind") as error:
        await fork_context.forks.fork(invalid_request)
    assert tampered_phases == ["copy.target.prepared"]
    assert type(error.value) is ItemSchemaError
    failed_record = catalog.get_creation_record(invalid_key)
    assert failed_record.state == "aborted"
    assert failed_record.session_id != successful_result.session.session_id
    assert (await fork_context.sessions.list(limit=100)).total == published_before_failure


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
@pytest.mark.parametrize("local_override", [False, True])
async def test_fork_first_provider_recompiles_typed_root_without_rewriting_imports(
    fork_context: ForkIntegrationContext, fork_provider_prepare,
    mode: str, local_override: bool,
) -> None:
    source = await fork_context.sessions.create(SessionCreateRequest(title="来源角色边界"))
    saver = fork_context.saver
    source_thread = _main_thread_id(fork_context, source.session_id)
    accepted = saver.accept_turn(
        source.session_id, thread_id=saver._storage.storage.main_thread_id(source.session_id), accepted_ingress_id="typed-root-ingress", acceptance_idempotency_key="typed-root-accept",
        job_id="typed-root-job", admission_idempotency_key="typed-root-admission", payload="保留来源历史",
    )
    manager = ContextSourceManager(owner=ContextSourceOwnerKey(source.session_id, source_thread), control_state_port=saver)
    descriptors = (
        ContextSourceDescriptor("instruction:shared", "agent_instructions", "共享规则", "共享规则", "source-owner://shared", root_placement="root_eligible"),
        ContextSourceDescriptor("instruction:retained", "agent_instructions", "保留规则", "保留规则", "source-owner://retained", root_placement="root_eligible"),
        ContextSourceDescriptor("instruction:tail", "workspace_instructions", "尾部来源", "尾部来源", "source-owner://tail", root_placement="tail_only"),
    )
    for descriptor, content in zip(descriptors, ("源共享规则第一版", "保留规则第一版", "仅尾部正文"), strict=True):
        manager.register(descriptor, tracking_status="tracked")
        assert manager.observe(descriptor.source_id, content, revision=descriptor.source_id + ":1")
    overlay_base = "已封存 overlay base"
    overlay_delta = "已封存 overlay delta"
    saver.register_source_overlay(
        SimpleNamespace(
            overlay_id="typed-root-overlay", session_id=source.session_id, thread_id=source_thread,
            checkpoint_ns="", source_kind="workspace_policy", source_revision="policy-v2",
            source_overlay_epoch=1, base_ref="typed-policy-base", delta_ref="typed-policy-delta",
            base_source_revision="policy-v1", delta_source_revision="policy-v2",
            delta_from_revision="policy-v1", delta_to_revision="policy-v2",
            delta_diff_hash=sha256_jcs(overlay_delta), status="active", idempotency_key="typed-root-overlay-key",
        ),
        session_id=source.session_id, thread_id=source_thread,
        base_content=overlay_base, delta_content=overlay_delta,
    )
    first = fork_provider_prepare(fork_context, source.session_id, str(accepted["initial_execution_id"]), manager, "source-first", attempt=1)
    assert first["messages"][0].content == "源共享规则第一版\n\n保留规则第一版"
    assert manager.observe("instruction:retained", "保留规则第二版", revision="instruction:retained:2")
    second = fork_provider_prepare(fork_context, source.session_id, str(accepted["initial_execution_id"]), manager, "source-second", attempt=2)
    assert second["messages"][0].content == first["messages"][0].content
    delta = second["messages"][-1].content
    assert "保留规则第二版" in delta
    source_user = next(message for message in first["messages"] if isinstance(message, HumanMessage) and message.content == "保留来源历史")
    admission = saver.execution_admission(source.session_id, thread_id=source_thread, execution_id=str(accepted["initial_execution_id"]))
    with (
        session_thread_owner_scope(MutationIntentOwner(source.session_id, source_thread), langgraph_thread_id=source_thread),
        execution_admission_scope(admission, langgraph_thread_id=source_thread),
    ):
        completed_messages = [source_user, AIMessage(
                content="来源完成", id="typed-root-final",
                response_metadata={"model_call_id": second["snapshot"].model_call_id},
            )]
        commit_fixture_output(saver, messages=completed_messages)
        await saver.aput(
            build_checkpoint_config(source_thread), _checkpoint("typed-root-checkpoint", completed_messages),
            {"source": "loop", "step": 1, "parents": {}}, {"messages": "2", "counter": "2"},
        )
    with sqlite3.connect(_main_thread_node(fork_context, source.session_id) / "rollout/index.sqlite") as connection:
        source_bytes = connection.execute("SELECT assembly_id,snapshot_json FROM context_assemblies ORDER BY rowid").fetchall()
    fork_result = await fork_context.forks.fork(await _fork_request(
        fork_context, source.session_id, mode=mode, title="目标首次组装", creation_kind="new_session_main",
    ))
    target = fork_result.session
    target_thread = fork_result.thread_id
    target_database = _main_thread_node(fork_context, target.session_id) / "rollout/index.sqlite"
    with sqlite3.connect(target_database) as connection:
        imported_bytes = connection.execute(
            "SELECT assembly_id,snapshot_json FROM context_assemblies WHERE session_id=? AND thread_id=? ORDER BY rowid",
            (source.session_id, source_thread),
        ).fetchall()
    assert imported_bytes
    assert all(row in source_bytes for row in imported_bytes)
    cold = RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root)
    # prefix 只承诺生产所选回答的请求正文；其它精确前任保留原 header 供冷校验。
    readable_bytes = imported_bytes if mode == "full_rollout_copy" else [
        row for row in imported_bytes if row[0] == second["assembly_id"]
    ]
    assert {row[0] for row in readable_bytes} == (
        {row[0] for row in source_bytes} if mode == "full_rollout_copy" else {second["assembly_id"]}
    )
    for assembly_id, snapshot_json in readable_bytes:
        historical = cold._storage.assemblies.get_context_assembly(target.session_id, target_thread, assembly_id=assembly_id)
        with sqlite3.connect(target_database) as connection:
            restored = load_snapshot(connection, snapshot_json)
        assert json.loads(json.dumps(historical.to_dict(), ensure_ascii=False)) == restored
        assert historical.session_id == source.session_id and historical.thread_id == source_thread
        assert cold.load_sealed_resource_activation(
            target.session_id, thread_id=target_thread, assembly_id=assembly_id,
            plan_hash=historical.plan_hash, request_hash=historical.request_hash,
        ) == saver.load_sealed_resource_activation(
            source.session_id, thread_id=source_thread, assembly_id=assembly_id,
            plan_hash=historical.plan_hash, request_hash=historical.request_hash,
        )
        assert cold._storage.assemblies.get_context_plan_registration(
            target.session_id, thread_id=target_thread,
            plan_id=historical.plan_id,
        ) == saver._storage.assemblies.get_context_plan_registration(
            source.session_id, thread_id=source_thread, plan_id=historical.plan_id,
        )
        audit_ref = cold._storage.assemblies.get_context_assembly_detail_ref(
            target.session_id, target_thread, assembly_id=assembly_id,
        )
        assert audit_ref == saver._storage.assemblies.get_context_assembly_detail_ref(
            source.session_id, source_thread, assembly_id=assembly_id,
        )
        sealed_details = tuple(
            entry.detail_ref for entry in historical.selection
            if entry.included and entry.detail_ref is not None
        )
        assert sealed_details
        for detail_ref in sealed_details:
            detail_ref.require_owner(source.session_id, source_thread)
            assert cold._storage.assemblies.get_context_plan_detail(
                target.session_id, target_thread, detail_ref=detail_ref,
            ) == saver._storage.assemblies.get_context_plan_detail(
                source.session_id, source_thread, detail_ref=detail_ref,
            )
            assert cold.read_context_plan_detail(
                target.session_id, target_thread, detail_ref=detail_ref, include_sensitive=True,
            ) == saver.read_context_plan_detail(
                source.session_id, source_thread, detail_ref=detail_ref, include_sensitive=True,
            )
    target_owner = ContextSourceOwnerKey(target.session_id, target_thread)
    assert cold.load_context_source_control_states(target_owner) == ()
    target_manager = ContextSourceManager(owner=target_owner, control_state_port=cold)
    if local_override:
        target_manager.register(descriptors[0], tracking_status="tracked")
        assert target_manager.observe("instruction:shared", "目标共享规则第三版", revision="instruction:shared:3")
        target_manager.register(descriptors[2], tracking_status="tracked")
        # 原 owner 的控制事实保持历史身份；目标 registration 独立产生首次 observation。
        observed_tail = target_manager.observe("instruction:tail", "仅尾部正文", revision="instruction:tail:1")
        assert observed_tail is True
    cold_context = replace(fork_context, saver=cold)
    target_accepted = cold.accept_turn(
        target.session_id, thread_id=cold._storage.storage.main_thread_id(target.session_id), accepted_ingress_id="typed-target-ingress", acceptance_idempotency_key="typed-target-accept",
        job_id="typed-target-job", admission_idempotency_key="typed-target-admission", payload="开始目标请求",
    )
    result = fork_provider_prepare(cold_context, target.session_id, str(target_accepted["initial_execution_id"]), target_manager, "target-first", attempt=1)
    snapshot = cold._storage.assemblies.get_context_assembly(target.session_id, target_thread, assembly_id=result["assembly_id"])
    root = snapshot.seal_binding.root
    assert root is not None
    expected_sources = {
        "instruction:shared": ("instruction:shared:3" if local_override else "instruction:shared:1",
                               "目标共享规则第三版" if local_override else "源共享规则第一版"),
        "instruction:retained": ("instruction:retained:1", "保留规则第一版"),
    }
    # 顺序只消费目标 registry 分配的 source ordinal，新 revision 不抢旧 slot。
    with sqlite3.connect(target_database) as connection:
        registered_ordinals = {
            (source_id, revision): ordinal for source_id, revision, ordinal in connection.execute(
                "SELECT json_extract(binding_manifest_json,'$.source_binding.source_ref'),source_revision,source_ordinal "
                "FROM context_contributions WHERE contribution_kind='prompt' AND source_ordinal IS NOT NULL",
            )
        }
    expected_order = sorted(expected_sources, key=lambda identity: registered_ordinals[identity, expected_sources[identity][0]])
    assert [(entry.source_identity, entry.revision, entry.source_ordinal) for entry in root.sources] == [
        (identity, expected_sources[identity][0], registered_ordinals[identity, expected_sources[identity][0]])
        for identity in expected_order
    ]
    assert root.content == "\n\n".join(expected_sources[identity][1] for identity in expected_order)
    assert len(root.sources) == len({entry.source_identity for entry in root.sources}) == 2
    assert "保留规则第二版" not in root.content and "仅尾部正文" not in root.content
    assert snapshot.seal_binding.prefix.state.epoch.reason.value == "initial"
    assert snapshot.seal_binding.prefix.state.epoch.ordinal == 1
    assert snapshot.seal_binding.prefix.state.epoch.turn_id == target_accepted["turn_id"]
    assert snapshot.seal_binding.prefix.parent is None
    assert sum(isinstance(message, HumanMessage) and message.content == "仅尾部正文" for message in result["messages"]) == 1
    (tail_frame,) = [frame for frame in snapshot.seal_binding.prefix.state.frames
                     if frame.item_ref.source_identity == "instruction:tail"]
    assert (tail_frame.role, tail_frame.item_ref.source_revision, tail_frame.content) == ("user", "instruction:tail:1", "仅尾部正文")
    assert any(isinstance(message, HumanMessage) and message.content == delta for message in result["messages"])
    (copied_overlay,) = cold.list_source_overlays(target.session_id, target_thread)
    assert [(frame.role, frame.item_ref.source_identity, frame.item_ref.source_revision, frame.content)
            for frame in snapshot.seal_binding.prefix.state.frames
            if frame.item_ref.source_identity in {copied_overlay["base_ref"], copied_overlay["delta_ref"]}] == [
        ("user", copied_overlay["base_ref"], "policy-v1", overlay_base),
        ("user", copied_overlay["delta_ref"], "policy-v2", overlay_delta),
    ]
    selected_refs = [entry.ref.ref_id for entry in snapshot.selection if entry.included]
    assert len(selected_refs) == len(set(selected_refs))
    first_target_bytes = json.dumps(snapshot.to_dict(), sort_keys=True, ensure_ascii=False)

    # 后继选择重新水合 CSM；只把已持久化 owner 的 descriptor 重新绑定。
    restored_manager = ContextSourceManager(owner=target_owner, control_state_port=cold)
    descriptors_by_id = {descriptor.source_id: descriptor for descriptor in descriptors}
    for state in cold.load_context_source_control_states(target_owner):
        restored_manager.register(
            descriptors_by_id[state.source_id], tracking_status=state.tracking_status,
        )
    next_result = fork_provider_prepare(
        cold_context, target.session_id, str(target_accepted["initial_execution_id"]),
        restored_manager, "target-second", attempt=2,
    )
    next_snapshot = cold._storage.assemblies.get_context_assembly(
        target.session_id, target_thread, assembly_id=next_result["assembly_id"],
    )
    assert next_snapshot.seal_binding.root == root
    assert next_snapshot.seal_binding.prefix.state.epoch == snapshot.seal_binding.prefix.state.epoch
    assert next_snapshot.seal_binding.prefix.parent == snapshot.seal_binding.prefix.state.parent
    assert next_snapshot.seal_binding.prefix.state.frames == snapshot.seal_binding.prefix.state.frames
    assert json.dumps(cold._storage.assemblies.get_context_assembly(
        target.session_id, target_thread, assembly_id=result["assembly_id"],
    ).to_dict(), sort_keys=True, ensure_ascii=False) == first_target_bytes
    with sqlite3.connect(target_database) as connection:
        assert connection.execute(
            "SELECT assembly_id,snapshot_json FROM context_assemblies WHERE session_id=? AND thread_id=? ORDER BY rowid",
            (source.session_id, source_thread),
        ).fetchall() == imported_bytes
    with sqlite3.connect(_main_thread_node(fork_context, source.session_id) / "rollout/index.sqlite") as connection:
        assert connection.execute("SELECT assembly_id,snapshot_json FROM context_assemblies ORDER BY rowid").fetchall() == source_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork"])
@pytest.mark.parametrize("failure", ["invalid_json", "body_tamper", "missing_body"])
async def test_prefix_selected_required_detail_rejects_corruption_before_publication(
    fork_context: ForkIntegrationContext, fork_provider_prepare,
    mode: SessionForkMode, failure: str,
) -> None:
    source = await fork_context.sessions.create(SessionCreateRequest(title="必需正文来源"))
    saver = fork_context.saver
    source_thread = _main_thread_id(fork_context, source.session_id)
    accepted = saver.accept_turn(
        source.session_id, thread_id=source_thread,
        accepted_ingress_id="required-detail-ingress", acceptance_idempotency_key="required-detail-accept",
        job_id="required-detail-job", admission_idempotency_key="required-detail-admission", payload="核验必需正文",
    )
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(source.session_id, source_thread), control_state_port=saver,
    )
    descriptor = ContextSourceDescriptor(
        "instruction:tail", "workspace_instructions", "尾部来源", "尾部来源",
        "source-owner://tail", root_placement="tail_only",
    )
    manager.register(descriptor, tracking_status="tracked")
    assert manager.observe(descriptor.source_id, "必须保留的尾部正文", revision="instruction:tail:1")
    prepared = fork_provider_prepare(
        fork_context, source.session_id, str(accepted["initial_execution_id"]),
        manager, "required-detail-source", attempt=1,
    )
    source_user = next(
        message for message in prepared["messages"]
        if isinstance(message, HumanMessage) and message.content == "核验必需正文"
    )
    admission = saver.execution_admission(
        source.session_id, thread_id=source_thread, execution_id=str(accepted["initial_execution_id"]),
    )
    with (
        session_thread_owner_scope(MutationIntentOwner(source.session_id, source_thread), langgraph_thread_id=source_thread),
        execution_admission_scope(admission, langgraph_thread_id=source_thread),
    ):
        completed_messages = [source_user, AIMessage(
                content="来源完成", id="required-detail-final",
                response_metadata={"model_call_id": prepared["snapshot"].model_call_id},
            )]
        commit_fixture_output(saver, messages=completed_messages)
        await saver.aput(
            build_checkpoint_config(source_thread),
            _checkpoint("required-detail-completed", completed_messages),
            {"source": "loop", "step": 1, "parents": {}}, {"messages": "2", "counter": "2"},
        )
    selected = next(
        entry for entry in prepared["snapshot"].selection
        if entry.included and entry.ref.ref_type == "request_only" and entry.detail_ref is not None
    )
    detail_ref = selected.detail_ref
    assert detail_ref is not None
    detail_manifest = detail_record_from_mapping(
        saver._storage.assemblies.get_context_plan_detail(
            source.session_id, source_thread, detail_ref=detail_ref,
        ),
    )
    assert detail_manifest.required is True
    source_detail = saver.read_context_plan_detail(
        source.session_id, source_thread, detail_ref=detail_ref, include_sensitive=True,
    )
    request = await _fork_request(
        fork_context, source.session_id, creation_kind="new_session_main",
        mode=mode, turn_id=str(accepted["turn_id"]),
    )
    healthy = await fork_context.forks.fork(request)
    assert saver.read_context_plan_detail(
        healthy.session.session_id, healthy.thread_id, detail_ref=detail_ref, include_sensitive=True,
    ) == source_detail
    source_root = _main_thread_node(fork_context, source.session_id)
    source_jsonl = (source_root / "rollout/rollout.jsonl").read_bytes()
    detail_path = source_root / detail_relative_path(detail_ref)
    if failure == "invalid_json":
        detail_path.write_bytes(b"{")
        error_type, error_message = DetailUnavailableError, "非法 JSON"
    elif failure == "body_tamper":
        payload = json.loads(detail_path.read_bytes())
        payload["detail"] = [{"type": "text", "text": "篡改后的正文"}]
        detail_path.write_bytes(canonical_json_bytes(payload))
        error_type, error_message = DetailUnavailableError, "content manifest hash/length 不一致"
    else:
        detail_path.unlink()
        error_type, error_message = ValueError, "所需正文缺少捕获文件"
    with pytest.raises(error_type, match=error_message):
        await fork_context.forks.fork(request.model_copy(update={
            "idempotency_key": "required-detail-corrupt-" + failure,
        }))
    assert (await fork_context.sessions.list(limit=100)).total == 2
    assert _retention_refs(fork_context, source.session_id) == []
    assert not tuple(fork_context.sessions.path_resolver.sessions_root.rglob(".fork-staging-*"))
    assert (source_root / "rollout/rollout.jsonl").read_bytes() == source_jsonl


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_public_fork_modes_copy_only_explicit_portable_debug_configurations(
    fork_provider_prepare, fork_context: ForkIntegrationContext,
    mode: SessionForkMode, creation_kind: SessionForkCreationKind,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    active_id, saved_id = _seed_debug_configurations(fork_context, source.session_id)
    request = await _fork_request(fork_context, source.session_id, creation_kind=creation_kind, mode=mode)
    point = await fork_context.forks.source_point(session_id=source.session_id, mode=mode)
    debug = {resource.name: resource for resource in point.resources if "/debug/node/configurations/" in resource.resource_uri}
    assert set(debug) == {"活动方案", "保存方案"}
    assert all(resource.availability == "available" and not resource.required_for_context for resource in debug.values())
    default = await fork_context.forks.fork(request)
    store = fork_context.saver._node_debug_store
    assert default.resources == []
    assert store.read_manifest(default.session.session_id, default.thread_id) is None
    assert store.list_configurations(default.session.session_id, default.thread_id) == []
    selected = request.model_copy(update={
        "idempotency_key": f"fork-{uuid4().hex}",
        "resources": [
            SessionForkResourceChoice(resource_uri=debug["保存方案"].resource_uri, action="copy_snapshot"),
            SessionForkResourceChoice(resource_uri=debug["活动方案"].resource_uri, action="omit"),
        ],
    })
    result = await fork_context.forks.fork(selected)
    target_session, target_thread = result.session.session_id, result.thread_id
    manifest = store.read_manifest(target_session, target_thread)
    assert manifest is not None
    assert (manifest.session_id, manifest.thread_id) == (target_session, target_thread)
    assert manifest.active_configuration_id is None and manifest.actions == []
    configurations = store.list_configurations(target_session, target_thread)
    assert len(configurations) == 1
    configuration = configurations[0]
    original = store.read_configuration(source.session_id, saved_id, _main_thread_id(fork_context, source.session_id))
    assert original is not None
    assert configuration.configuration_id not in {active_id, saved_id}
    assert configuration.model_copy(update={"configuration_id": saved_id}) == original
    assert manifest.configuration_ids == (configuration.configuration_id,)
    assert store.read_launch_claim(target_session, target_thread) is None
    copied, omitted = result.resources
    assert (copied.action, copied.availability, copied.target_uri) == (
        "copy_snapshot", "available",
        debug_configuration_resource_uri(result.thread_uri, configuration.configuration_id),
    )
    assert (omitted.action, omitted.availability, omitted.target_uri) == ("omit", "unavailable", None)
    with fork_context.saver._storage.storage.read_connection(target_session, target_thread, "") as connection:
        journal, = connection.execute("SELECT state,proof_json FROM fork_debug_snapshots").fetchall()
        proof = json.loads(journal[1])
        assert journal[0] == "published"
        assert proof["target_configuration_id_map"] == [[saved_id, configuration.configuration_id]]
        assert [item["configuration_id"] for item in proof["snapshot"]["configuration_artifacts"]] == [saved_id]
    claim = store.read_launch_claim(source.session_id, _main_thread_id(fork_context, source.session_id))
    assert claim is not None and (claim.pid, claim.inspector_port, claim.phase) == (4242, 9311, "running")


@pytest.mark.asyncio
async def test_debug_config_drift_aborts_entire_public_fork(
    fork_provider_prepare, fork_context: ForkIntegrationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    _, saved_id = _seed_debug_configurations(fork_context, source.session_id)
    request = await _fork_request(fork_context, source.session_id, creation_kind="new_session_main")
    request = request.model_copy(update={"resources": [SessionForkResourceChoice(
        resource_uri=debug_configuration_resource_uri(request.source_uri, saved_id), action="copy_snapshot",
    )]})
    provider = fork_context.saver.forks.source.node_debug_workspace_config
    assert provider is not None
    stable = provider()
    drifted = replace(stable, revision="f" * 64, content_hash=f"sha256:{'f' * 64}")
    calls = 0

    def drifting_provider():
        nonlocal calls
        calls += 1
        return stable if calls == 1 else drifted

    monkeypatch.setattr(fork_context.saver.forks.source, "node_debug_workspace_config", drifting_provider)
    before = {item.session_id for item in (await fork_context.sessions.list()).items}
    with pytest.raises(NodeDebugTargetValidationError, match="配置 revision 已漂移"):
        await fork_context.forks.fork(request)
    assert {item.session_id for item in (await fork_context.sessions.list()).items} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["mkdir", "rename", "materialization_commit"])
async def test_debug_frozen_operation_recovers_publication_without_live_source(
    fork_provider_prepare, fork_context: ForkIntegrationContext,
    monkeypatch: pytest.MonkeyPatch, fork_deadlines, fault: str,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    _, saved_id = _seed_debug_configurations(fork_context, source.session_id)
    request = await _fork_request(fork_context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy")
    target = await fork_context.sessions.create(SessionCreateRequest(title="debug crash target"))
    target_thread = _main_thread_id(fork_context, target.session_id)
    target_node = _main_thread_node(fork_context, target.session_id)
    storage = fork_context.saver._storage
    operation_id, materialization_id = uuid4().hex, uuid4().hex
    deadline_target = f"{fork_context.sessions.workspace_id}/{target.session_id}"
    deadline = fork_deadlines.for_operation(target=deadline_target, operation_key=operation_id)
    arguments = {
        "source_session_id": source.session_id, "target_session_id": target.session_id,
        "source_uri": request.source_uri,
        "source_owner": MutationIntentOwner(source.session_id, _main_thread_id(fork_context, source.session_id)),
        "target_owner": MutationIntentOwner(target.session_id, target_thread),
        "mode": request.mode, "source_view_id": request.source_view_id,
        "source_view_revision": request.source_view_revision,
        "fork_id": operation_id, "materialization_id": materialization_id,
        "resource_choices": [{"resource_uri": debug_configuration_resource_uri(request.source_uri, saved_id), "action": "copy_snapshot"}],
    }
    original_rename = Path.rename
    original_publish = node_debug_materialization.publish_target_snapshot
    original_commit = storage.forks.commit_fork_materialization

    def crash_rename(path, target_path):
        if Path(target_path) == target_node / "debug/node":
            assert path.is_dir() and Path(target_path).parent.is_dir()
            assert not any(Path(target_path).parent.iterdir())
            raise RuntimeError("injected debug mkdir crash")
        return original_rename(path, target_path)

    def crash_published(*args, **kwargs):
        original_publish(*args, **kwargs)
        raise RuntimeError("injected debug rename crash")

    def crash_commit(*args, **kwargs):
        raise RuntimeError("injected debug materialization commit crash")

    if fault == "mkdir":
        monkeypatch.setattr(Path, "rename", crash_rename)
    elif fault == "rename":
        monkeypatch.setattr(node_debug_materialization, "publish_target_snapshot", crash_published)
    else:
        monkeypatch.setattr(storage.forks, "commit_fork_materialization", crash_commit)
    with pytest.raises(RuntimeError, match="injected debug"):
        await deadline.wait(
            fork_context.saver.forks.afork(**arguments),
            "RolloutCheckpointSaver.forks.afork.initial",
        )
    assert not deadline._completed
    capture = fork_context.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=operation_id)
    assert capture is not None and capture.state == "source_captured"
    frozen, _ = read_captured_snapshot(storage.storage.sessions_dir, capture)
    assert frozen.node_debug is not None
    with sqlite3.connect(target_node / "rollout/index.sqlite") as connection:
        assert connection.execute("SELECT state FROM fork_debug_snapshots").fetchone() == (
            "published" if fault == "materialization_commit" else "ready",
        )
    monkeypatch.setattr(Path, "rename", original_rename)
    monkeypatch.setattr(node_debug_materialization, "publish_target_snapshot", original_publish)
    monkeypatch.setattr(storage.forks, "commit_fork_materialization", original_commit)
    await fork_context.sessions.path_resolver.delete_subtree(
        idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id,
    )

    def forbid_live_configuration():
        raise AssertionError("ready/published 恢复不得读取当前 debug 配置")

    with RolloutCheckpointSaver(storage.storage.sessions_dir, node_debug_workspace_config=forbid_live_configuration) as restarted:
        recovery_deadline = fork_deadlines.for_operation(target=deadline_target, operation_key=operation_id)
        assert recovery_deadline is deadline and not recovery_deadline._completed
        recovered = await recovery_deadline.wait(
            restarted.forks.afork(**arguments),
            "RolloutCheckpointSaver.forks.afork.recovery",
        )
        assert recovered.journal_state == "committed"
        fork_deadlines.complete(recovery_deadline)
        manifest = restarted._node_debug_store.read_manifest(target.session_id, target_thread)
        assert manifest is not None and manifest.active_configuration_id is None and manifest.actions == []
        configurations = restarted._node_debug_store.list_configurations(target.session_id, target_thread)
        assert len(configurations) == 1 and configurations[0].name == "保存方案"
        assert restarted._node_debug_store.read_launch_claim(target.session_id, target_thread) is None
        with restarted._storage.storage.read_connection(target.session_id, target_thread, "") as connection:
            assert connection.execute("SELECT state FROM fork_debug_snapshots").fetchone() == ("published",)
            assert connection.execute("SELECT status FROM fork_materializations").fetchone() == ("committed",)
        assert not (target_node / "rollout/.fork-debug-staging").exists()




@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
async def test_all_fork_modes_preserve_independent_original_history(
    fork_provider_prepare, fork_context: ForkIntegrationContext, mode: SessionForkMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    source_thread = _main_thread_id(fork_context, source.session_id)
    original_items = {item.item_id: item.to_dict() for item in fork_context.saver._storage.storage.read_items(source.session_id, source_thread)}
    source_clocks = _turn_history_clocks(fork_context, source.session_id)
    source_message_clocks = _message_history_clocks(fork_context, source.session_id)
    request = await _fork_request(fork_context, source.session_id, creation_kind="new_session_main", mode=mode)
    with monkeypatch.context() as copy_guard:
        if mode == "full_rollout_copy":
            def forbid_item_materialization(*_args: object, **_kwargs: object) -> None:
                pytest.fail("full-copy 必须保留磁盘快照，不能全量读取 item 或展开前缀")

            copy_guard.setattr(fork_context.saver._storage.storage, "read_items", forbid_item_materialization)
            copy_guard.setattr(snapshot_copy, "select_view_prefix", forbid_item_materialization)
        result = await fork_context.forks.fork(request)
    child, thread = result.session, result.thread_id
    copied = fork_context.saver._storage.storage.read_items(child.session_id, thread)
    assert copied and all(item.to_dict() == original_items[item.item_id] for item in copied)
    target_clocks = _turn_history_clocks(fork_context, child.session_id)
    assert target_clocks and all(value == source_clocks[turn] for turn, value in target_clocks.items())
    assert all(value == source_message_clocks[message] for message, value in _message_history_clocks(fork_context, child.session_id).items())
    origins = _fork_origins(fork_context, child.session_id)
    assert len(origins) == 1 and origins[0][1] == source.session_id and origins[0][4:] == (mode, "detached")
    target_root = _main_thread_node(fork_context, child.session_id) / "rollout"
    with sqlite3.connect(target_root / "index.sqlite") as connection:
        assert connection.execute("SELECT DISTINCT session_id,thread_id FROM executions").fetchall() == [(source.session_id, source_thread)]
        assert connection.execute("SELECT COUNT(*) FROM turn_records WHERE session_id=?", (child.session_id,)).fetchone() == (0,)
        if mode == "full_rollout_copy":
            assert connection.execute("SELECT turn_id,status FROM turn_records ORDER BY turn_ordinal").fetchall() == [("turn-1", "completed"), ("turn-2", "active")]
    if mode == "full_rollout_copy":
        assert (target_root / "rollout.jsonl").read_bytes() == (_main_thread_node(fork_context, source.session_id) / "rollout/rollout.jsonl").read_bytes()
        assert _pending_write_rows(fork_context, child.session_id) == _pending_write_rows(fork_context, source.session_id)
    history_request = TurnHistoryLoadRequest(turn_ids=["turn-1"], include=["user", "final_response"])
    original = fork_context.saver.load_history(source.session_id, source_thread, history_request).items[0]
    await fork_context.sessions.path_resolver.delete_subtree(idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id)
    with RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root) as restarted:
        assert await _get_tuple(fork_context, child.session_id, saver=restarted) is not None
        cold = restarted.load_history(child.session_id, thread, history_request).items[0]
        assert (cold.turn_id, cold.activity_stats, cold.created_at, cold.updated_at) == (
            original.turn_id, original.activity_stats, original.created_at, original.updated_at,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
async def test_successor_fork_keeps_original_a_and_b_history_after_both_sources_are_deleted(
    fork_context: ForkIntegrationContext, fork_provider_prepare, mode: SessionForkMode,
) -> None:
    current = await fork_context.sessions.create(SessionCreateRequest(title="后继历史 A"))
    saver = fork_context.saver
    history: list[HumanMessage | AIMessage] = []
    original_nodes = []
    expected_history = []
    descriptor = ContextSourceDescriptor(
        "instruction:successor", "agent_instructions", "后继规则", "后继规则",
        "source-owner://successor", root_placement="root_eligible",
    )
    for key in ("a", "b"):
        session_id = current.session_id
        thread_id = _main_thread_id(fork_context, session_id)
        original_nodes.append((session_id, _main_thread_node(fork_context, session_id)))
        accepted = saver.accept_turn(
            session_id, thread_id=thread_id, accepted_ingress_id=f"successor-{key}-ingress",
            acceptance_idempotency_key=f"successor-{key}-accept", job_id=f"successor-{key}-job",
            admission_idempotency_key=f"successor-{key}-admission", payload=f"问题 {key}",
        )
        manager = ContextSourceManager(owner=ContextSourceOwnerKey(session_id, thread_id), control_state_port=saver)
        manager.register(descriptor, tracking_status="tracked")
        assert manager.observe(descriptor.source_id, f"规则 {key}", revision=f"successor-{key}-revision")
        resource_payload = {"content": f"{key} 的冻结资源正文"}
        registry = ResourceRegistry()
        registry.publish(ResourceSnapshot(
            resource_id=f"skill:successor-{key}", resource_kind="skills", facet="activation",
            display_uri=f"boxteam://workspace/test/resources/skills/successor-{key}/SKILL.md",
            revision=sha256_jcs(resource_payload), payload=resource_payload,
            source_lineage=((f"successor-{key}", f"successor-{key}-revision"),), generation=1,
        ))
        prepared = fork_provider_prepare(
            fork_context, session_id, str(accepted["initial_execution_id"]), manager,
            f"successor-{key}", attempt=1, registry=registry,
        )
        snapshot = prepared["snapshot"]
        assert snapshot.seal_binding.root.content == f"规则 {key}"
        history.extend([
            next(message for message in prepared["messages"]
                 if isinstance(message, HumanMessage) and message.content == f"问题 {key}"),
            AIMessage(content=f"回答 {key}", id=f"successor-{key}-final",
                      response_metadata={"model_call_id": snapshot.model_call_id}),
        ])
        admission = saver.execution_admission(
            session_id, thread_id=thread_id, execution_id=str(accepted["initial_execution_id"]),
        )
        with (
            session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id),
            execution_admission_scope(admission, langgraph_thread_id=thread_id),
        ):
            commit_fixture_output(saver, messages=history)
            await saver.aput(
                build_checkpoint_config(thread_id), _checkpoint(f"successor-{key}-checkpoint", history),
                {"source": "loop", "step": len(history), "parents": {}},
                {"messages": str(len(history)), "counter": str(len(history))},
            )
        checkpoint_before_copy = await _get_tuple(fork_context, session_id)
        assert checkpoint_before_copy is not None
        checkpoint_history = checkpoint_before_copy.checkpoint["channel_values"]["messages"]
        activation = saver.load_sealed_resource_activation(
            session_id, thread_id=thread_id, assembly_id=snapshot.assembly_id,
            plan_hash=snapshot.plan_hash, request_hash=snapshot.request_hash,
        )
        assert len(activation.bindings) == 1
        assert saver.read_resource_activation_payload(
            activation, activation.bindings[0], storage_owner=(session_id, thread_id),
        ) == resource_payload
        details = tuple(
            (entry.detail_ref, saver.read_context_plan_detail(
                session_id, thread_id, detail_ref=entry.detail_ref, include_sensitive=True,
            )) for entry in snapshot.selection if entry.included and entry.detail_ref is not None
        )
        assert details
        expected_history.append((
            snapshot,
            saver._storage.assemblies.get_context_plan_registration(
                session_id, thread_id=thread_id, plan_id=snapshot.plan_id,
            ),
            activation, details, resource_payload,
        ))
        with sqlite3.connect(original_nodes[-1][1] / "rollout/index.sqlite") as connection:
            original_bytes = connection.execute(
                "SELECT assembly_id,snapshot_json FROM context_assemblies ORDER BY rowid",
            ).fetchall()
        if key == "a":
            request = await _fork_request(
                fork_context, session_id, creation_kind="new_session_main", mode=mode,
            )
            request = SessionForkRequest.model_validate({
                **request.model_dump(mode="json"),
                "resources": [{"resource_uri": activation.bindings[0].display_uri, "action": "omit"}],
            })
            with pytest.raises(ValueError, match="context_required_resource"):
                await fork_context.forks.fork(request)
            assert (await fork_context.sessions.list(limit=100)).total == 1
            if mode == "full_rollout_copy":
                detail_path = original_nodes[-1][1] / detail_relative_path(details[0][0])
                detail_bytes = detail_path.read_bytes()
                detail_path.unlink()
                try:
                    with pytest.raises(FileNotFoundError, match="context_required_resource_unavailable"):
                        await fork_context.forks.fork(await _fork_request(
                            fork_context, session_id, creation_kind="new_session_main", mode=mode,
                        ))
                finally:
                    detail_path.write_bytes(detail_bytes)
                assert (await fork_context.sessions.list(limit=100)).total == 1
        result = await fork_context.forks.fork(await _fork_request(
            fork_context, session_id, creation_kind="new_session_main",
            mode="full_rollout_copy" if key == "a" else mode, title=f"后继历史 {key} 的目标",
        ))
        current = result.session

    target_thread = result.thread_id
    target_database = _main_thread_node(fork_context, current.session_id) / "rollout/index.sqlite"
    with sqlite3.connect(target_database) as connection:
        assert connection.execute(
            "SELECT assembly_id,snapshot_json FROM context_assemblies ORDER BY rowid",
        ).fetchall() == original_bytes
    for session_id, node in original_nodes:
        await fork_context.sessions.path_resolver.delete_subtree(
            idempotency_key=f"successor-delete-{session_id}", root_node_id=session_id,
        )
        assert not node.exists()
    assert (await fork_context.sessions.list(limit=100)).total == 1

    # A、B 物理节点均已删除；冷 Saver 只从 C 本地读取两种原执行 scope。
    cold = RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root)
    for snapshot, registration, activation, details, resource_payload in expected_history:
        assert cold._storage.assemblies.get_context_assembly(
            current.session_id, target_thread, assembly_id=snapshot.assembly_id,
        ) == snapshot
        assert cold._storage.assemblies.get_context_plan_registration(
            current.session_id, thread_id=target_thread, plan_id=snapshot.plan_id,
        ) == registration
        loaded_activation = cold.load_sealed_resource_activation(
            current.session_id, thread_id=target_thread, assembly_id=snapshot.assembly_id,
            plan_hash=snapshot.plan_hash, request_hash=snapshot.request_hash,
        )
        assert loaded_activation == activation
        assert cold.read_resource_activation_payload(
            loaded_activation, loaded_activation.bindings[0],
            storage_owner=(current.session_id, target_thread),
        ) == resource_payload
        for detail_ref, body in details:
            assert cold.read_context_plan_detail(
                current.session_id, target_thread, detail_ref=detail_ref, include_sensitive=True,
            ) == body
    checkpoint = await _get_tuple(fork_context, current.session_id, saver=cold)
    assert checkpoint is not None
    assert checkpoint.checkpoint["channel_values"]["messages"] == checkpoint_history
    owner = ContextSourceOwnerKey(current.session_id, target_thread)
    assert cold.load_context_source_control_states(owner) == ()
    accepted = cold.accept_turn(
        current.session_id, thread_id=target_thread, accepted_ingress_id="successor-c-ingress",
        acceptance_idempotency_key="successor-c-accept", job_id="successor-c-job",
        admission_idempotency_key="successor-c-admission", payload="C 继续聊天",
    )
    manager = ContextSourceManager(owner=owner, control_state_port=cold)
    prepared = fork_provider_prepare(
        replace(fork_context, saver=cold), current.session_id, str(accepted["initial_execution_id"]),
        manager, "successor-c-first", attempt=1,
    )
    snapshot = prepared["snapshot"]
    assert (snapshot.session_id, snapshot.thread_id) == (current.session_id, target_thread)
    assert snapshot.seal_binding.root.content == "规则 b"
    assert snapshot.seal_binding.prefix.state.epoch.ordinal == 1
    expected_contents = [message.content for message in history] + ["C 继续聊天"]
    assert [message.content for message in prepared["messages"]
            if isinstance(message, (HumanMessage, AIMessage)) and message.content in expected_contents] == expected_contents
    assert cold.load_sealed_resource_activation(
        current.session_id, thread_id=target_thread, assembly_id=snapshot.assembly_id,
        plan_hash=snapshot.plan_hash, request_hash=snapshot.request_hash,
    ).bindings == ()
    with sqlite3.connect(target_database) as connection:
        assert connection.execute(
            "SELECT assembly_id,snapshot_json FROM context_assemblies WHERE session_id<>? ORDER BY rowid",
            (current.session_id,),
        ).fetchall() == original_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_outcome", ["failure", "unknown"])
async def test_full_fork_new_input_restores_original_terminal_tool_group_outside_frozen_checkpoint(
    fork_context: ForkIntegrationContext, fork_provider_prepare, tool_outcome: str,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    saver = fork_context.saver
    source_owner = saver.forks.source.resolve_main_thread_owner(source.session_id)
    source_admission = saver.execution_admission(
        source.session_id, thread_id=source_owner.thread_id, execution_id="execution-turn-2",
    )
    tools = ToolSetSnapshot(selection_revision=sha256_jcs({"read_file": True}), tools=({
        "name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },))
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(source.session_id, source_owner.thread_id), control_state_port=saver,
    )
    prepared = fork_provider_prepare(
        fork_context, source.session_id, source_admission.execution_id, manager,
        "terminal-source-model", attempt=2, selected_tools=tools,
    )
    model_call_id = prepared["snapshot"].model_call_id
    call = AIMessage(
        content="读取终态结果", id="lc_run--" + model_call_id,
        tool_calls=[{"name": "read_file", "args": {"path": "README.md"}, "id": "terminal-provider-tool"}],
        response_metadata={"model_call_id": model_call_id},
    )
    with (
        session_thread_owner_scope(source_owner, langgraph_thread_id=source_owner.thread_id),
        execution_admission_scope(source_admission, langgraph_thread_id=source_owner.thread_id),
    ):
        saver.ensure_request_items(
            source.session_id, source_owner.thread_id, turn_id=source_admission.turn_id,
            execution_id=source_admission.execution_id, messages=[call],
        )
        (call_item,) = [item for item in saver._storage.storage.read_items(source.session_id, source_owner.thread_id)
                        if item.semantic_kind == SemanticKind.TOOL_CALL]
        scoped_tool_id = call_item.payload["tool_calls"][0]["id"]
        result = CanonicalItemRecord.create(
            item_sequence=1, item_id="item-terminal-live-result",
            semantic_kind=SemanticKind.TOOL_RESULT, payload_kind=PayloadKind.TOOL_RESULT,
            status=CanonicalItemStatus.COMPLETED,
            producer_ref={"producer_kind": "tool", "producer_id": "terminal-tool-attempt", "invocation_id": model_call_id},
            payload={
                "tool_call_id": scoped_tool_id,
                "tool_invocation_id": f"tool-invocation:{model_call_id}:terminal-provider-tool",
                "tool_attempt_id": "terminal-tool-attempt", "result_id": "terminal-tool-attempt",
                "name": "read_file", "content": "冻结 checkpoint 后的真实结果",
                "tool_outcome": tool_outcome, "execution_confirmed": tool_outcome != "unknown",
            },
            projection_identity=ItemProjectionIdentity(model_call_id=model_call_id),
            turn_id=source_admission.turn_id, turn_scope=TurnScope.TURN_MEMBER,
            message_group_id="message-terminal-live-result", wire_role="tool",
        )
        saver.append_items(source.session_id, (result,), thread_id=source_owner.thread_id)
    saver.converge_execution(
        source.session_id, thread_id=source_owner.thread_id, turn_id=source_admission.turn_id,
        execution_id=source_admission.execution_id, outcome="failed", turn_status="failed",
    )
    frozen = await _get_tuple(fork_context, source.session_id, checkpoint_id="cp3")
    assert frozen is not None
    assert not any(isinstance(message, ToolMessage) for message in frozen.checkpoint["channel_values"]["messages"])
    copied = await fork_context.forks.fork(await _fork_request(
        fork_context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy",
    ))
    target = copied.session
    target_owner = MutationIntentOwner(target.session_id, copied.thread_id)
    cold = RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root)
    accepted = cold.accept_turn(
        target.session_id, thread_id=copied.thread_id, accepted_ingress_id="terminal-target-ingress",
        acceptance_idempotency_key="terminal-target-accept", job_id="terminal-target-job",
        admission_idempotency_key="terminal-target-admission", payload="继续处理终态工具结果",
    )
    admission = cold.execution_admission(
        target.session_id, thread_id=copied.thread_id, execution_id=str(accepted["initial_execution_id"]),
    )
    with (
        session_thread_owner_scope(target_owner, langgraph_thread_id=copied.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=copied.thread_id),
    ):
        restored = await cold.aget_tuple(build_checkpoint_config(copied.thread_id))
        assert restored is not None
        messages = restored.checkpoint["channel_values"]["messages"]
        (restored_call,) = [message for message in messages if isinstance(message, AIMessage) and message.tool_calls]
        (restored_result,) = [message for message in messages if isinstance(message, ToolMessage)]
        assert restored_call.tool_calls[0]["id"] == restored_result.tool_call_id == scoped_tool_id
        assert restored_result.content == "冻结 checkpoint 后的真实结果"
        assert restored_result.response_metadata["tool_outcome"] == tool_outcome
        assert restored_result.response_metadata["execution_confirmed"] is (tool_outcome != "unknown")
        assert messages.index(restored_call) < messages.index(restored_result) < len(messages) - 1
        assert messages[-1].content == "继续处理终态工具结果"
        target_jsonl = cold._storage.storage.jsonl_path(target.session_id, thread_id=copied.thread_id)
        before_reuse = target_jsonl.read_bytes()
        assert cold.ensure_request_items(
            target.session_id, copied.thread_id, turn_id=admission.turn_id,
            execution_id=admission.execution_id, messages=messages,
        ) == ()
        assert target_jsonl.read_bytes() == before_reuse
    prepared = fork_provider_prepare(
        replace(fork_context, saver=cold), target.session_id, admission.execution_id,
        ContextSourceManager(owner=ContextSourceOwnerKey(target.session_id, copied.thread_id), control_state_port=cold),
        "terminal-target-model", attempt=1, selected_tools=tools,
    )
    assert any(isinstance(message, ToolMessage) and message.content == restored_result.content
               for message in prepared["messages"])


@pytest.mark.asyncio
async def test_history_replay_reuses_turn_root_without_execution(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    source_root = _main_thread_node(fork_context, source.session_id)
    with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
        before_executions = connection.execute(
            "SELECT execution_id FROM executions ORDER BY execution_ordinal"
        ).fetchall()
        turn_id, turn_ordinal, root_item_id, source_branch_id = connection.execute(
            "SELECT turn_id, turn_ordinal, root_input_item_id, source_branch_id "
            "FROM turn_records WHERE turn_id = 'turn-1'"
        ).fetchone()
        before_branches = connection.execute("SELECT * FROM branches ORDER BY branch_id").fetchall()

    latest = await _get_tuple(fork_context, source.session_id)
    assert latest is not None
    source_owner = fork_context.saver.forks.source.resolve_main_thread_owner(source.session_id)
    with session_thread_owner_scope(source_owner, langgraph_thread_id=source_owner.thread_id):
        replay_config = fork_context.saver.history_replay(
            latest.config, turn_id="turn-1", anchor_mode="inclusive",
        )
        history_tuple = fork_context.saver.get_tuple(replay_config)
        assert history_tuple is not None
        assert history_tuple.config["configurable"]["history_view_id"] == replay_config["configurable"]["history_view_id"]
        assert [message.id for message in history_tuple.checkpoint["channel_values"]["messages"]] == ["u1", "a1"]
        with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
            history_dump = tuple(connection.iterdump())
        history_jsonl = (source_root / "rollout" / "rollout.jsonl").read_bytes()
        with pytest.raises(ValueError, match="history-projection-read-only"):
            fork_context.saver.put(
                replay_config, _checkpoint("history-write-must-not-exist", []),
                {"source": "loop", "step": 1}, {"messages": "history-write"},
            )
        with pytest.raises(ValueError, match="history-projection-read-only"):
            await fork_context.saver.aput(
                history_tuple.config,
                _checkpoint("history-async-write-must-not-exist", []),
                {"source": "loop", "step": 1}, {"messages": "history-write"},
            )
        with pytest.raises(ValueError, match="history-projection-read-only"):
            fork_context.saver.put_writes(
                replay_config, [("messages", "历史投影禁止写入执行结果")],
                "history-write-task", "history/write",
            )
        with pytest.raises(ValueError, match="history-projection-read-only"):
            await fork_context.saver.aput_writes(
                history_tuple.config, [("messages", "异步历史投影禁止写入执行结果")],
                "history-async-write-task", "history/async-write",
            )
        with pytest.raises(ValueError, match="history-projection-read-only"):
            fork_context.saver.rewind(replay_config, checkpoint_id="cp1")
        with pytest.raises(ValueError, match="history-projection-read-only"):
            await fork_context.saver.arewind(history_tuple.config, checkpoint_id="cp1")
        with pytest.raises(ValueError, match="history-projection-read-only"):
            fork_context.saver.rewind_to_turn(replay_config, turn_id="turn-1")
        with pytest.raises(ValueError, match="history-projection-read-only"):
            await fork_context.saver.arewind_to_turn(history_tuple.config, turn_id="turn-1")
        readonly_tuple = await fork_context.saver.aget_tuple(history_tuple.config)
        assert readonly_tuple is not None
        assert readonly_tuple.config == history_tuple.config
        assert readonly_tuple.checkpoint == history_tuple.checkpoint
        with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
            assert tuple(connection.iterdump()) == history_dump
        assert (source_root / "rollout" / "rollout.jsonl").read_bytes() == history_jsonl
    assert replay_config["configurable"]["thread_id"] == source_owner.thread_id
    assert replay_config["configurable"]["checkpoint_id"] == "cp3"

    with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
        after_executions = connection.execute(
            "SELECT execution_id FROM executions ORDER BY execution_ordinal"
        ).fetchall()
        history_view = connection.execute(
            "SELECT v.view_id, v.view_kind, cvt.turn_id, cvt.root_input_item_id, "
            "cvt.logical_turn_ordinal FROM context_views AS v "
            "JOIN context_view_turns AS cvt ON cvt.view_id = v.view_id "
            "WHERE v.view_id = ?",
            (replay_config["configurable"]["history_view_id"],),
        ).fetchone()
        copied_turn = connection.execute(
            "SELECT turn_id, turn_ordinal, root_input_item_id, source_branch_id "
            "FROM turn_records WHERE turn_id = 'turn-1'"
        ).fetchone()
        assert connection.execute("SELECT * FROM branches ORDER BY branch_id").fetchall() == before_branches
        assert connection.execute(
            "SELECT count(*) FROM checkpoints WHERE checkpoint_id='history-write-must-not-exist'"
        ).fetchone() == (0,)

    assert after_executions == before_executions
    assert copied_turn == (turn_id, turn_ordinal, root_item_id, source_branch_id)
    assert history_view[1:] == (
        "history_replay",
        "turn-1",
        root_item_id,
        1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("click", ["user_root", "assistant_item"])
async def test_first_fork_click_rejects_stale_actual_history_snapshot(
    fork_context: ForkIntegrationContext, fork_provider_prepare, click: str,
) -> None:
    source, _ = await _seed_retry_tool_source(fork_context, fork_provider_prepare)
    thread_id = _main_thread_id(fork_context, source.session_id)
    page = fork_context.saver.load_history(source.session_id, thread_id, TurnHistoryLoadRequest(
        turn_ids=["turn-retry"], include=["user", "assistant"],
    ))
    assert len(page.items) == 1
    displayed = page.items[0]
    assert (displayed.source_view_id, displayed.source_view_revision) == (
        page.source_view_id, page.source_view_revision,
    )
    selection = {"turn_id": displayed.turn_id} if click == "user_root" else {
        "item_id": next(part.source.item_id for part in displayed.response_parts if part.final),
    }
    with pytest.raises(ValueError, match="source-mismatch: 消息点击必须携带实际历史读取"):
        await fork_context.forks.source_point(session_id=source.session_id, thread_id=thread_id, **selection)
    frozen = await fork_context.forks.source_point(
        session_id=source.session_id, thread_id=thread_id, **selection,
        source_view_id=displayed.source_view_id, source_view_revision=displayed.source_view_revision,
    )
    assert (frozen.source_view_id, frozen.source_view_revision) == (
        displayed.source_view_id, displayed.source_view_revision,
    )
    fork_context.saver.accept_turn(
        source.session_id, thread_id=thread_id, accepted_ingress_id="stale-click-next-input",
        acceptance_idempotency_key="stale-click-next-accept", job_id="stale-click-next-job",
        admission_idempotency_key="stale-click-next-admission", payload="新的真实准入更新历史修订",
        root_item_id="stale-click-next-root",
    )
    with pytest.raises(ValueError, match="source-mismatch: 展示快照 revision 已变化"):
        await fork_context.forks.source_point(
            session_id=source.session_id, thread_id=thread_id, **selection,
            source_view_id=displayed.source_view_id, source_view_revision=displayed.source_view_revision,
        )
    assert _fork_origins(fork_context, source.session_id) == []
    assert (await fork_context.sessions.list(limit=100)).total == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_before_root_fork_keeps_completed_prefix_without_clicked_or_future_input(
    fork_context: ForkIntegrationContext, fork_provider_prepare,
    creation_kind: SessionForkCreationKind,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    source_thread = _main_thread_id(fork_context, source.session_id)
    request = (await _fork_request(
        fork_context, source.session_id, creation_kind=creation_kind, item_id="item-u2",
    )).model_copy(update={"anchor_mode": "before"})
    source_items = fork_context.saver._storage.storage.read_items(source.session_id, source_thread)
    with fork_context.saver._storage.storage.read_connection(source.session_id, source_thread) as connection:
        selected = [row[0] for row in connection.execute(
            "SELECT member.item_id FROM context_view_items member "
            "JOIN context_view_items anchor ON anchor.view_id=member.view_id AND anchor.item_id=? "
            "WHERE member.view_id=? AND member.visible=1 AND member.context_selected=1 "
            "AND member.context_ordinal<anchor.context_ordinal ORDER BY member.context_ordinal",
            (request.anchor_item_id, request.source_view_id),
        )]
    assert selected and "item-u1" in selected and "item-u2" not in selected
    result = await fork_context.forks.fork(request)
    cold = RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root)
    target, thread = result.session.session_id, result.thread_id
    with session_thread_owner_scope(MutationIntentOwner(target, thread), langgraph_thread_id=thread):
        restored = await cold.aget_tuple(build_checkpoint_config(thread))
    assert restored is not None
    assert [message.id for message in restored.checkpoint["channel_values"]["messages"]] == ["u1", "a1"]
    with cold._storage.storage.read_connection(target, thread) as connection:
        active = cold._storage.storage._active_context_view_id(connection, "")
        visible = [row[0] for row in connection.execute(
            "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1 "
            "AND context_selected=1 ORDER BY context_ordinal", (active,),
        )]
    assert visible == selected
    copied = cold._storage.storage.read_items(target, thread)
    source_by_id = {item.item_id: item.to_dict() for item in source_items}
    assert all(item.to_dict() == source_by_id[item.item_id] for item in copied)
    assert "item-u2" not in {item.item_id for item in copied}
    page = cold.load_history(target, thread, TurnHistoryLoadRequest(direction="tail"))
    assert [turn.turn_id for turn in page.items] == ["turn-1"]
    assert page.items[0].final_response == "回答一"
    accepted = cold.accept_turn(
        target, thread_id=thread, accepted_ingress_id="before-target-user",
        acceptance_idempotency_key="before-target-accept", job_id="before-target-job",
        admission_idempotency_key="before-target-admission", payload="边界之后的新问题",
    )
    prepared = fork_provider_prepare(
        replace(fork_context, saver=cold), target, str(accepted["initial_execution_id"]),
        ContextSourceManager(owner=ContextSourceOwnerKey(target, thread), control_state_port=cold),
        "before-target-first", attempt=1, thread_id=thread,
    )
    assert [message.content for message in prepared["messages"] if isinstance(message, (HumanMessage, AIMessage))] == [
        "问题一", "回答一", "边界之后的新问题",
    ]
    assert all(entry.ref.ref_id != "item-u2" for entry in prepared["snapshot"].selection)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_anchor", ["anchor-not-in-source", "u1", "turn-1", "job-turn-1"])
async def test_stale_fork_anchor_is_rejected_before_target_creation(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext,
    invalid_anchor: str,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    before = await fork_context.sessions.list(limit=100)

    request = await _fork_request(fork_context, source.session_id, creation_kind="new_session_main")
    request = request.model_copy(update={"anchor_item_id": invalid_anchor})
    with pytest.raises(ValueError, match="source-mismatch: 点击 item 不属于冻结 source view"):
        await fork_context.forks.fork(request)

    after = await fork_context.sessions.list(limit=100)
    assert after.total == before.total == 1
    assert [session.session_id for session in after.items] == [source.session_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "full_rollout_copy"])
@pytest.mark.parametrize("materialized", [False, True])
@pytest.mark.parametrize("creation_kind", ["new_child_thread", "new_session_main"])
async def test_empty_rollout_forks_without_execution(
    fork_context: ForkIntegrationContext, mode: SessionForkMode, materialized: bool, creation_kind: str,
) -> None:
    source = await fork_context.sessions.create(SessionCreateRequest(title="真实空 rollout"))
    source_thread = _main_thread_id(fork_context, source.session_id)
    storage = fork_context.saver._storage.storage
    root = storage.root(source.session_id, thread_id=source_thread)
    assert fork_context.saver.load_history(source.session_id, source_thread, TurnHistoryLoadRequest(direction="tail")).items == []
    assert not root.exists() and not (root.parent / ".rollout.write.lock").exists()
    if materialized:
        storage.initialize(source.session_id, thread_id=source_thread)
    display, repeated = await asyncio.gather(*(
        fork_context.forks.source_point(session_id=source.session_id) for _ in range(2)
    ))
    assert repeated == display
    assert display.anchor_item_id is None and display.resource_mode == "view_prefix_fork"
    assert root.is_dir() and (root / "index.sqlite").is_file()
    point = await fork_context.forks.source_point(
        session_id=source.session_id, source_view_id=display.source_view_id,
        source_view_revision=display.source_view_revision, mode=mode,
    )
    assert point.anchor_item_id is None and point.resource_mode == mode
    result = await fork_context.forks.fork(SessionForkRequest(
        idempotency_key=f"empty-rollout-{mode}", source_uri=source.uri + "/threads/" + source_thread,
        source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
        anchor_item_id=None, mode=mode,
        target_container_uri=source.uri if creation_kind == "new_child_thread" else display.main_container_uri,
        creation_kind=creation_kind, title="空 rollout 的分支",
    ))
    assert result.thread_id != source_thread
    target_session = result.session.session_id
    assert (target_session == source.session_id) == (creation_kind == "new_child_thread")
    with storage.read_connection(target_session, result.thread_id, "") as connection:
        assert connection.execute(
            "SELECT status FROM fork_materializations WHERE target_session_id=? AND target_thread_id=?",
            (target_session, result.thread_id),
        ).fetchone() == ("committed",)
        for table in ("item_catalog", "executions", "turn_records", "model_calls", "context_assemblies"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    assert storage.read_items(target_session, result.thread_id) == []


@pytest.mark.asyncio
async def test_fresh_source_preflight_uses_real_empty_view(fork_context: ForkIntegrationContext) -> None:
    source = await fork_context.sessions.create(SessionCreateRequest(title="空来源预检"))
    point = await fork_context.saver.forks.source.preflight_fork(source_session_id=source.session_id)
    display = await fork_context.forks.source_point(session_id=source.session_id)
    assert (point.source_view_id, point.source_view_revision, point.source_checkpoint_id, point.anchor_item_id) == (
        display.source_view_id, display.source_view_revision, None, None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("damaged", ["frozen_selector", "partial_root", "removed_root"])
async def test_fork_source_does_not_rebuild_missing_or_damaged_history(
    fork_context: ForkIntegrationContext, damaged: str,
) -> None:
    source = await fork_context.sessions.create(SessionCreateRequest(title="不可重建的来源"))
    storage = fork_context.saver._storage.storage
    thread_id = _main_thread_id(fork_context, source.session_id)
    root = storage.root(source.session_id, thread_id=thread_id)
    if damaged == "frozen_selector":
        with pytest.raises(RolloutNotMaterializedError):
            await fork_context.forks.source_point(
                session_id=source.session_id, mode="full_rollout_copy",
                source_view_id="missing-view", source_view_revision=0,
            )
        assert not root.exists() and not (root.parent / ".rollout.write.lock").exists()
    else:
        if damaged == "partial_root":
            root.mkdir()
        else:
            await fork_context.forks.source_point(session_id=source.session_id)
            shutil.rmtree(root)
        with pytest.raises((RuntimeError, sqlite3.OperationalError)):
            await fork_context.forks.source_point(session_id=source.session_id)
        assert not (root / "index.sqlite").exists()


@pytest.mark.asyncio
async def test_full_copy_without_selector_keeps_complete_history_after_restart(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    child = (await fork_context.forks.fork(await _fork_request(
        fork_context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy",
    ))).session

    restarted = RolloutCheckpointSaver(
        fork_context.sessions.path_resolver.sessions_root
    )
    restored = await _get_tuple(
        fork_context,
        child.session_id,
        saver=restarted,
    )
    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == ["问题一", "回答一", "问题二"]

    source_root = _main_thread_node(fork_context, source.session_id)
    child_root = _main_thread_node(fork_context, child.session_id)
    with (
        sqlite3.connect(source_root / "rollout" / "index.sqlite") as source_connection,
        sqlite3.connect(child_root / "rollout" / "index.sqlite") as child_connection,
    ):
        source_turns = source_connection.execute(
            "SELECT turn_id, status FROM turn_records ORDER BY turn_ordinal"
        ).fetchall()
        target_turns = child_connection.execute(
            "SELECT turn_id, status, turn_ordinal FROM turn_records ORDER BY turn_ordinal"
        ).fetchall()

    assert source_turns == [("turn-1", "completed"), ("turn-2", "active")]
    assert [(row[0], row[1]) for row in target_turns] == source_turns
    assert [row[2] for row in target_turns] == [1, 2]






@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "error"),
    [
        (None, None),
        ("missing_binding", "source-mismatch"),
        ("binding_hash", "namespace binding hash 不一致"),
        ("internal_scope_pair", "source-mismatch"),
        ("turn_scope_pair", "source-mismatch"),
    ],
)
async def test_cold_full_copy_history_preserves_bound_owners_and_rejects_corruption(
    fork_context: ForkIntegrationContext, fork_provider_prepare,
    fault: str | None, error: str | None,
) -> None:
    source, _ = await _seed_retry_tool_source(fork_context, fork_provider_prepare)
    source_thread = _main_thread_id(fork_context, source.session_id)
    source_page = RolloutHistoryReader(RolloutContextReader(fork_context.saver._storage)).load(
        source.session_id, source_thread, TurnHistoryLoadRequest(direction="tail"),
    )
    source_final = source_page.items[0].final_response
    assert "检查完成" in source_final and "结果已确认" in source_final
    result = await fork_context.forks.fork(await _fork_request(
        fork_context, source.session_id, creation_kind="new_session_main",
        mode="full_rollout_copy",
    ))
    await fork_context.sessions.path_resolver.delete_subtree(
        idempotency_key=f"delete-cold-history-source-{source.session_id}",
        root_node_id=source.session_id,
    )
    cold = RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root)
    index = cold._storage.storage.index_path(result.session.session_id, thread_id=result.thread_id)
    with sqlite3.connect(index) as connection:
        if fault == "missing_binding":
            updated = connection.execute(
                "DELETE FROM history_namespace_bindings WHERE session_id=? AND thread_id=?",
                (source.session_id, source_thread),
            )
        elif fault == "binding_hash":
            updated = connection.execute(
                "UPDATE history_namespace_bindings SET binding_hash=? WHERE session_id=? AND thread_id=?",
                ("0" * 64, source.session_id, source_thread),
            )
        elif fault == "internal_scope_pair":
            updated = connection.execute(
                "UPDATE executions SET thread_id=? WHERE execution_id='execution-internal-notice'",
                (result.thread_id,),
            )
        elif fault == "turn_scope_pair":
            updated = connection.execute(
                "UPDATE turn_records SET thread_id=? WHERE turn_id='turn-retry'",
                (result.thread_id,),
            )
            connection.execute(
                "UPDATE executions SET thread_id=? WHERE turn_id='turn-retry'",
                (result.thread_id,),
            )
        if fault is not None:
            assert updated.rowcount == 1
    reader = RolloutHistoryReader(RolloutContextReader(cold._storage))
    request = TurnHistoryLoadRequest(direction="tail")
    if error is not None:
        with pytest.raises(ValueError, match=error):
            reader.load(result.session.session_id, result.thread_id, request)
        return
    page = reader.load(result.session.session_id, result.thread_id, request)
    assert [item.turn_id for item in page.items] == ["turn-retry"]
    assert page.items[0].final_response == source_final
    serialized = page.model_dump(mode="json")
    assert len(serialized["internal_executions"]) == 1
    internal = serialized["internal_executions"][0]
    assert (internal["session_id"], internal["thread_id"], internal["execution_id"], internal["job_id"]) == (
        source.session_id, source_thread, "execution-internal-notice", "job-internal-notice",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor", ["root", "tool_result"])
async def test_cold_clicked_prefix_history_excludes_backing_final(
    fork_context: ForkIntegrationContext, fork_provider_prepare,
    fork_gateway_request_context: None, anchor: str,
) -> None:
    source, _ = await _seed_retry_tool_source(fork_context, fork_provider_prepare)
    source_root = _main_thread_node(fork_context, source.session_id)
    with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
        final_item_id = connection.execute(
            "SELECT final_item_id FROM turn_records WHERE turn_id='turn-retry'",
        ).fetchone()[0]
        anchor_item_id = "item-retry-user" if anchor == "root" else connection.execute(
            "SELECT item_id FROM item_catalog WHERE turn_id='turn-retry' AND semantic_kind='tool_result' ORDER BY item_sequence LIMIT 1",
        ).fetchone()[0]
    result = await fork_context.forks.fork(await _fork_request(
        fork_context, source.session_id, creation_kind="new_child_thread", item_id=anchor_item_id,
    ))
    cold = RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root)
    with sqlite3.connect(cold._storage.storage.index_path(result.session.session_id, thread_id=result.thread_id)) as connection:
        # 非 UTF-8 的 backing 摘要会在 SQLite 正文出列时失败，不能先读入再在 Python 丢弃。
        updated = connection.execute(
            "UPDATE message_projections SET visible_text=CAST(X'FF' AS TEXT) WHERE message_sequence="
            "(SELECT final_message_sequence FROM turns WHERE turn_id='turn-retry')",
        )
        assert updated.rowcount == 1
    reader = RolloutHistoryReader(RolloutContextReader(cold._storage))
    latest, _, _ = reader.bootstrap(result.session.session_id, result.thread_id)
    assert latest is not None and latest.turn_id == "turn-retry"
    assert "检查完成" not in latest.response_preview and "结果已确认" not in latest.response_preview
    assert all(part.source.item_id != final_item_id for part in latest.response_parts)
    page = reader.load(result.session.session_id, result.thread_id, TurnHistoryLoadRequest(
        direction="tail", include=["user", "assistant", "tool_summary", "tool_call", "tool_result", "internal"],
    ))
    assert [item.turn_id for item in page.items] == ["turn-retry"]
    assert page.items[0].final_response == ""
    if anchor == "root":
        assert page.items[0].tool_summary == []
    else:
        assert len(page.items[0].tool_summary) == 2
        assert all(item.tool_name == "read_file" for item in page.items[0].tool_summary)
    with sqlite3.connect(cold._storage.storage.index_path(result.session.session_id, thread_id=result.thread_id)) as connection:
        assert connection.execute(
            "SELECT status, final_item_id FROM turn_records WHERE turn_id='turn-retry'",
        ).fetchone() == ("completed", final_item_id)
    assert all(part.source.item_id != final_item_id for part in page.items[0].response_parts)


@pytest.mark.asyncio
async def test_native_cold_history_rejects_hidden_canonical_final(
    fork_context: ForkIntegrationContext, fork_provider_prepare,
) -> None:
    source, _ = await _seed_retry_tool_source(fork_context, fork_provider_prepare)
    thread_id = _main_thread_id(fork_context, source.session_id)
    cold = RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root)
    with sqlite3.connect(cold._storage.storage.index_path(source.session_id, thread_id=thread_id)) as connection:
        final_item_id = connection.execute(
            "SELECT final_item_id FROM turn_records WHERE turn_id='turn-retry'",
        ).fetchone()[0]
        updated = mutate_view_members(connection,
            "UPDATE context_view_items SET visible=0 WHERE item_id=?", (final_item_id,),
        )
        assert updated.rowcount > 0
    reader = RolloutHistoryReader(RolloutContextReader(cold._storage))
    with pytest.raises(RuntimeError, match="canonical final item 缺少可见 view membership"):
        reader.load(source.session_id, thread_id, TurnHistoryLoadRequest(direction="tail"))


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["historical_binding_hash", "native_sequence_gap"])
async def test_imported_prefix_still_rejects_bad_binding_and_native_sequence_gap(
    fork_context: ForkIntegrationContext, fork_provider_prepare, fault: str,
) -> None:
    source, _ = await _seed_retry_tool_source(fork_context, fork_provider_prepare)
    source_thread = _main_thread_id(fork_context, source.session_id)
    result = await fork_context.forks.fork(await _fork_request(
        fork_context, source.session_id, creation_kind="new_session_main", turn_id="turn-retry",
    ))
    target = result.session
    cold = RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root)
    assert await _get_tuple(fork_context, target.session_id, saver=cold) is not None
    if fault == "native_sequence_gap":
        cold.accept_turn(
            target.session_id, thread_id=result.thread_id, accepted_ingress_id="native-gap-ingress",
            acceptance_idempotency_key="native-gap-accept", job_id="native-gap-job",
            admission_idempotency_key="native-gap-admission", payload="当前目标的新输入",
            root_item_id="native-gap-root",
        )
    index = cold._storage.storage.index_path(target.session_id, thread_id=result.thread_id)
    with sqlite3.connect(index) as connection:
        if fault == "historical_binding_hash":
            updated = connection.execute(
                "UPDATE history_namespace_bindings SET binding_hash=? WHERE session_id=? AND thread_id=?",
                ("0" * 64, source.session_id, source_thread),
            )
        else:
            updated = connection.execute(
                "UPDATE item_catalog SET item_sequence=item_sequence+1 WHERE item_id='native-gap-root'",
            )
            connection.execute("UPDATE database_meta SET last_item_sequence=last_item_sequence+1 WHERE singleton_id=1")
        assert updated.rowcount == 1
    if fault == "historical_binding_hash":
        with pytest.raises(ValueError, match="namespace binding hash 不一致"):
            await _get_tuple(fork_context, target.session_id, saver=cold)
    else:
        with pytest.raises(RuntimeError, match="item_sequence 不连续"):
            await _get_tuple(fork_context, target.session_id, saver=cold)


@pytest.mark.asyncio
@pytest.mark.parametrize("click", ["user_root", "middle_tool_result"])
async def test_view_prefix_click_keeps_exact_completed_turn_root_or_middle_item(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext,
    fork_gateway_request_context: None,
    click: str,
) -> None:
    source, _ = await _seed_retry_tool_source(fork_context, fork_provider_prepare)
    saver = fork_context.saver
    storage = saver._storage.storage
    source_thread = _main_thread_id(fork_context, source.session_id)
    completed = await _get_tuple(fork_context, source.session_id, checkpoint_id="cp-retry-final")
    assert completed is not None
    original_items = storage.read_items(source.session_id, source_thread)
    by_message = {
        item.projection_identity.projection_message_id: item
        for item in original_items if item.projection_identity is not None
    }
    final_group = by_message["retry-final"].message_group_id
    assert final_group is not None
    final_backing = {item.item_id: item.to_dict() for item in original_items if item.message_group_id == final_group}
    assert final_backing
    future = saver.accept_turn(
        source.session_id, thread_id=source_thread, accepted_ingress_id="click-future-ingress",
        acceptance_idempotency_key="click-future-accept", job_id="click-future-job",
        admission_idempotency_key="click-future-admission", payload="后续问题不得扩大点击范围",
        turn_id="click-future-turn", root_item_id="click-future-root",
        initial_execution_id="click-future-execution",
    )
    source_prepared = fork_provider_prepare(
        fork_context, source.session_id, str(future["initial_execution_id"]),
        ContextSourceManager(owner=ContextSourceOwnerKey(source.session_id, source_thread), control_state_port=saver),
        "click-source-projection", attempt=1,
    )
    request = await _fork_request(
        fork_context, source.session_id, creation_kind="new_session_main",
        turn_id="turn-retry" if click == "user_root" else None,
        item_id=by_message["retry-result"].item_id if click == "middle_tool_result" else None,
    )
    with storage.read_connection(source.session_id, source_thread) as connection:
        expected_visible = [row[0] for row in connection.execute(
            "SELECT member.item_id FROM context_view_items member "
            "WHERE member.view_id=? AND member.visible=1 AND member.logical_item_ordinal<="
            "(SELECT logical_item_ordinal FROM context_view_items WHERE view_id=? AND item_id=?) "
            "ORDER BY member.logical_item_ordinal",
            (request.source_view_id, request.source_view_id, request.anchor_item_id),
        )]
    assert expected_visible[-1] == request.anchor_item_id
    result = await fork_context.forks.fork(request)
    target = result.session
    target_thread = result.thread_id
    cold = RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root)
    restored = await _get_tuple(fork_context, target.session_id, saver=cold)
    assert restored is not None
    # 冷读保留冻结历史 notice；新准入的 Provider selection 在下方独立核对。
    expected_message_ids = ["retry-notice", "retry-user"] if click == "user_root" else ["retry-notice", "retry-user", "retry-call", "retry-result"]
    completed_messages = {message.id: message for message in completed.checkpoint["channel_values"]["messages"]}
    expected_messages = [completed_messages[message_id] for message_id in expected_message_ids]
    assert by_message["retry-notice"].item_id in expected_visible
    assert [message.id for message in restored.checkpoint["channel_values"]["messages"]] == expected_message_ids
    assert restored.checkpoint["channel_values"]["messages"] == expected_messages
    with cold._storage.storage.read_connection(target.session_id, target_thread) as connection:
        visible = [row[0] for row in connection.execute(
            "SELECT member.item_id FROM checkpoint_namespace_state namespace "
            "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
            "JOIN context_view_items member ON member.view_id=branch.head_view_id "
            "WHERE namespace.checkpoint_ns='' AND member.visible=1 ORDER BY member.logical_item_ordinal",
        )]
    assert visible == expected_visible
    assert not set(final_backing) & set(visible)
    imported = cold._storage.storage.read_items(target.session_id, target_thread)
    imported_by_id = {item.item_id: item.to_dict() for item in imported}
    assert len(imported_by_id) == len(imported)
    assert {item_id: imported_by_id[item_id] for item_id in final_backing} == final_backing
    assert "click-future-root" not in imported_by_id
    accepted = cold.accept_turn(
        target.session_id, thread_id=target_thread, accepted_ingress_id="click-target-ingress",
        acceptance_idempotency_key="click-target-accept", job_id="click-target-job",
        admission_idempotency_key="click-target-admission", payload="点击后的新问题",
        root_item_id="click-target-root",
    )
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(target.session_id, target_thread), control_state_port=cold,
    )
    prepared = fork_provider_prepare(
        replace(fork_context, saver=cold), target.session_id,
        str(accepted["initial_execution_id"]), manager, "click-target-first", attempt=1,
    )
    selected_ids = [item_id for item_id in expected_visible if item_id != "item-retry-notice"]
    _assert_fork_provider_history(prepared, source_prepared, original_items, selected_ids, "click-target-root")
    excluded_ids = set(completed_messages) - set(expected_message_ids)
    assert all(message.id not in excluded_ids for message in prepared["messages"])
    assert any(isinstance(message, HumanMessage) and message.content == "点击后的新问题" for message in prepared["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("compaction_order", "mode"), [
    ("after_provider", "view_prefix_fork"),
    ("before_provider", "view_prefix_fork"),
    ("before_provider", "context_fork"),
])
async def test_compaction_fork_keeps_only_required_body_backing(
    fork_context: ForkIntegrationContext, fork_provider_prepare,
    fork_gateway_request_context: None,
    mode: SessionForkMode, compaction_order: str,
) -> None:
    saver = fork_context.saver
    source = await fork_context.sessions.create(SessionCreateRequest(title="真实压缩来源"))
    source_thread = _main_thread_id(fork_context, source.session_id)
    source_owner = MutationIntentOwner(source.session_id, source_thread)
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(source.session_id, source_thread), control_state_port=saver,
    )
    manager.register(ContextSourceDescriptor(
        "instruction:compaction", "agent_instructions", "压缩规则", "压缩规则",
        "source-owner://compaction-rule", root_placement="root_eligible",
    ), tracking_status="tracked")
    assert manager.observe("instruction:compaction", "保留真实压缩规则", revision="compaction-rule:1")
    old = saver.accept_turn(
        source.session_id, thread_id=source_thread, accepted_ingress_id="compaction-old-user",
        acceptance_idempotency_key="compaction-old-accept", job_id="compaction-old-job",
        admission_idempotency_key="compaction-old-admission", payload="需要压缩的旧问题",
        root_item_id="compaction-old-root",
    )
    old_first = fork_provider_prepare(
        fork_context, source.session_id, str(old["initial_execution_id"]), manager,
        "compaction-old-first", attempt=1,
    )
    old_second = fork_provider_prepare(
        fork_context, source.session_id, str(old["initial_execution_id"]), manager,
        "compaction-old-second", attempt=2,
    )
    old_user = next(message for message in old_first["messages"]
                    if isinstance(message, HumanMessage) and message.content == "需要压缩的旧问题")
    old_final = AIMessage(content="旧问题已经完成", id="compaction-old-final", response_metadata={
        "model_call_id": old_second["snapshot"].model_call_id,
    })
    old_admission = saver.execution_admission(
        source.session_id, thread_id=source_thread, execution_id=str(old["initial_execution_id"]),
    )
    with (session_thread_owner_scope(source_owner, langgraph_thread_id=source_thread),
          execution_admission_scope(old_admission, langgraph_thread_id=source_thread)):
        commit_fixture_output(saver, messages=[old_user, old_final])
        await saver.aput(
            build_checkpoint_config(source_thread), _checkpoint("compaction-old-checkpoint", [old_user, old_final]),
            {"source": "loop", "step": 1, "parents": {}}, {"messages": "2", "counter": "2"},
        )
    old_checkpoint = await _get_tuple(fork_context, source.session_id)
    assert old_checkpoint is not None
    old_messages = old_checkpoint.checkpoint["channel_values"]["messages"]
    old_items = saver._storage.storage.read_items(source.session_id, source_thread)
    old_item_ids = {item.item_id for item in old_items}
    accepted = saver.accept_turn(
        source.session_id, thread_id=source_thread, accepted_ingress_id="compaction-source-user",
        acceptance_idempotency_key="compaction-source-accept", job_id="compaction-source-job",
        admission_idempotency_key="compaction-source-admission", payload="压缩期间的新问题",
        root_item_id="compaction-source-root",
    )
    admission = saver.execution_admission(
        source.session_id, thread_id=source_thread, execution_id=str(accepted["initial_execution_id"]),
    )
    admitted_root = next(item for item in saver._storage.storage.read_items(source.session_id, source_thread)
                         if item.item_id == "compaction-source-root")
    user = messages_from_dict([LangChainMessageCodec().project_message((admitted_root,))])[0]
    summary = HumanMessage(content="旧上下文摘要", id="compaction-summary", response_metadata={
        "internal": True,
        "compaction_summary": True,
        "provenance_relations": [ItemProvenanceRelation(
            relation="derived_from", target_kind="canonical_item", target_id="compaction-old-root",
            target_session_id=source.session_id, target_thread_id=source_thread,
            target_turn_id=str(old["turn_id"]),
        ).to_dict()],
    })
    if compaction_order == "after_provider":
        produced = fork_provider_prepare(
            fork_context, source.session_id, admission.execution_id, manager,
            "compaction-produced", attempt=1,
        )
        final = AIMessage(content=[{"type": "text", "text": "新回答"}, {"type": "text", "text": "结束"}],
                          id="compaction-source-final", response_metadata={"model_call_id": produced["snapshot"].model_call_id})
        with (session_thread_owner_scope(source_owner, langgraph_thread_id=source_thread),
              execution_admission_scope(admission, langgraph_thread_id=source_thread)):
            commit_fixture_output(saver, messages=[*old_messages, user, final])
            await saver.aput(
                build_checkpoint_config(source_thread),
                _checkpoint("compaction-expanded-checkpoint", [*old_messages, user, final]),
                {"source": "loop", "step": 2, "parents": {}}, {"messages": "5", "counter": "5"},
            )
        retained = [final]
        summarized = [*old_messages, user]
    else:
        retained = [user]
        summarized = old_messages
    event = build_cache_preserving_event(
        CachePreservingPartition(prefix_messages=[], messages_to_summarize=summarized,
                                 preserved_messages=retained, state_cutoff=len(summarized)),
        summary_message=summary,
    )
    compacted = _checkpoint("compaction-selected-checkpoint", [*summarized, *retained])
    frozen_state = {"false_flag": False, "zero_count": 0, "empty_map": {}, "empty_list": [], "none_value": None}
    frozen_versions = {name: f"frozen-{name}" for name in frozen_state}
    compacted["channel_values"].update(frozen_state)
    compacted["channel_versions"].update(frozen_versions)
    compacted["updated_channels"].extend(frozen_state)
    compacted["versions_seen"] = {"historical-node": frozen_versions}
    compacted["pending_sends"] = [{"node": "source-only-pending", "attempt": 3}]
    compacted["channel_values"]["_summarization_event"] = event
    compacted["channel_versions"]["_summarization_event"] = "1"
    compacted["updated_channels"].append("_summarization_event")
    with (session_thread_owner_scope(source_owner, langgraph_thread_id=source_thread),
          execution_admission_scope(admission, langgraph_thread_id=source_thread)):
        await saver.aput(
            build_checkpoint_config(source_thread), compacted,
            {"source": "compaction", "step": 3, "parents": {}},
            {"messages": "6", "counter": "6", "_summarization_event": "1", **frozen_versions},
        )
    if compaction_order == "before_provider":
        produced = fork_provider_prepare(
            fork_context, source.session_id, admission.execution_id, manager,
            "compaction-produced", attempt=1,
        )
        final = AIMessage(content=[{"type": "text", "text": "新回答"}, {"type": "text", "text": "结束"}],
                          id="compaction-source-final", response_metadata={"model_call_id": produced["snapshot"].model_call_id})
        final_checkpoint = _checkpoint("compaction-final-checkpoint", [summary, user, final])
        final_checkpoint["channel_values"].update(frozen_state)
        final_checkpoint["channel_versions"].update(frozen_versions)
        final_checkpoint["updated_channels"].extend(frozen_state)
        final_checkpoint["versions_seen"] = {"historical-node": frozen_versions}
        final_checkpoint["pending_sends"] = compacted["pending_sends"]
        with (session_thread_owner_scope(source_owner, langgraph_thread_id=source_thread),
              execution_admission_scope(admission, langgraph_thread_id=source_thread)):
            commit_fixture_output(saver, messages=[summary, user, final])
            await saver.aput(
                build_checkpoint_config(source_thread), final_checkpoint,
                {"source": "loop", "step": 4, "parents": {}},
                {"messages": "7", "counter": "7", **frozen_versions},
            )
    selected_checkpoint = await _get_tuple(fork_context, source.session_id)
    assert selected_checkpoint is not None
    assert {name: selected_checkpoint.checkpoint["channel_values"][name] for name in frozen_state} == frozen_state
    source_items = saver._storage.storage.read_items(source.session_id, source_thread)
    final_items = [item for item in source_items if item.projection_identity is not None
                   and item.projection_identity.projection_message_id == "compaction-source-final"]
    assert len(final_items) == 1
    assert final_items[0].projection_identity.model_call_id == produced["snapshot"].model_call_id
    assert messages_from_dict([LangChainMessageCodec().project_message(tuple(final_items))])[0].content == final.content
    with saver._storage.storage.read_connection(source.session_id, source_thread) as connection:
        anchor = connection.execute(
            "SELECT member.item_id FROM checkpoint_namespace_state namespace "
            "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
            "JOIN context_view_items member ON member.view_id=branch.head_view_id "
            "WHERE namespace.checkpoint_ns='' AND member.visible=1 AND member.context_selected=1 ORDER BY member.context_ordinal DESC LIMIT 1",
        ).fetchone()
    assert anchor is not None
    probe = saver.accept_turn(
        source.session_id, thread_id=source_thread, accepted_ingress_id="compaction-source-probe",
        acceptance_idempotency_key="compaction-probe-accept", job_id="compaction-probe-job",
        admission_idempotency_key="compaction-probe-admission", payload="冻结前缀投影核对",
        root_item_id="compaction-probe-root",
    )
    source_prepared = fork_provider_prepare(
        fork_context, source.session_id, str(probe["initial_execution_id"]), manager,
        "compaction-probe", attempt=1,
    )
    request = await _fork_request(
        fork_context, source.session_id, mode=mode, creation_kind="new_session_main", item_id=anchor[0],
    )
    with saver._storage.storage.read_connection(source.session_id, source_thread) as connection:
        selected_rows = connection.execute(
            "SELECT item_id,context_ordinal FROM context_view_items WHERE view_id=? AND visible=1 AND context_selected=1 AND context_ordinal<="
            "(SELECT context_ordinal FROM context_view_items WHERE view_id=? AND item_id=?) ORDER BY context_ordinal",
            (request.source_view_id, request.source_view_id, request.anchor_item_id),
        ).fetchall()
    selected_ids = [row[0] for row in selected_rows]
    assert [row[1] for row in selected_rows] == list(range(len(selected_rows)))
    selected_id_set = set(selected_ids)
    provider_selected_ids = [
        entry.ref.ref_id for entry in source_prepared["snapshot"].selection
        if entry.included and entry.ref.ref_type == "canonical_item"
        and entry.ref.ref_id in selected_id_set
    ]
    assert provider_selected_ids == selected_ids
    assert not old_item_ids & set(selected_ids)
    source_by_id = {item.item_id: item for item in source_items}
    selected_items = [source_by_id[item_id] for item_id in selected_ids]
    expected_messages = []
    expected_manifest_groups = []
    offset = 0
    codec = LangChainMessageCodec()
    while offset < len(selected_items):
        first = selected_items[offset]
        group_size = first.projection_group.size if first.projection_group is not None else 1
        group = selected_items[offset:offset + group_size]
        assert len(group) == group_size
        expected_messages.extend(messages_from_dict([codec.project_message(tuple(group))]))
        expected_manifest_groups.append([
            {"item_id": item.item_id, **item.projection_manifest()} for item in group
        ])
        offset += group_size
    result = await fork_context.forks.fork(request)
    target, target_thread = result.session, result.thread_id
    cold = RolloutCheckpointSaver(fork_context.sessions.path_resolver.sessions_root)
    restored = await _get_tuple(fork_context, target.session_id, saver=cold)
    assert restored is not None
    assert "_summarization_event" not in restored.checkpoint["channel_values"]
    assert {name: restored.checkpoint["channel_values"][name] for name in frozen_state} == frozen_state
    assert restored.checkpoint["channel_values"]["counter"] == selected_checkpoint.checkpoint["channel_values"]["counter"]
    assert restored.checkpoint["channel_versions"] == {
        name: version for name, version in selected_checkpoint.checkpoint["channel_versions"].items()
        if name not in {"_summarization_event", "_force_cache_compaction", "_compaction_continuation"}
    }
    assert restored.checkpoint["versions_seen"] == selected_checkpoint.checkpoint["versions_seen"]
    assert restored.checkpoint["pending_sends"] == [] and restored.pending_writes == []
    target_messages = restored.checkpoint["channel_values"]["messages"]
    assert [(message.type, message.id, message.content) for message in target_messages] == [
        (message.type, message.id, message.content) for message in expected_messages
    ]
    assert [message.response_metadata.get("canonical_item_manifests") for message in target_messages] == expected_manifest_groups
    summary_messages = [message for message in target_messages if message.id == "compaction-summary"]
    assert len(summary_messages) == 1
    assert summary_messages[0].content == "旧上下文摘要"
    source_checkpoint_id = selected_checkpoint.checkpoint["id"]
    source_restored = await _get_tuple(
        fork_context, source.session_id, saver=cold, checkpoint_id=source_checkpoint_id,
    )
    assert source_restored is not None
    assert source_restored == selected_checkpoint
    imported = cold._storage.storage.read_items(target.session_id, target_thread)
    imported_by_id = {item.item_id: item.to_dict() for item in imported}
    with cold._storage.storage.read_connection(target.session_id, target_thread) as connection:
        target_view = cold._storage.storage._active_context_view_id(connection, "")
        visible = connection.execute(
            "SELECT item_id,logical_item_ordinal,context_ordinal FROM context_view_items "
            "WHERE view_id=? AND visible=1 ORDER BY logical_item_ordinal", (target_view,),
        ).fetchall()
        original_visible = connection.execute(
            "SELECT item_id,logical_item_ordinal FROM context_view_items "
            "WHERE view_id=? AND item_id IN (SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1) "
            "ORDER BY logical_item_ordinal", (request.source_view_id, target_view),
        ).fetchall()
    assert [(row[0], row[1]) for row in visible] == original_visible
    assert {row[0] for row in visible} == selected_id_set
    assert not old_item_ids & {row[0] for row in visible}
    assert sorted(row[2] for row in visible) == list(range(len(selected_ids)))
    reader = RolloutHistoryReader(RolloutContextReader(cold._storage))
    history_include = ["user", "text", "assistant", "tool_summary", "tool_call", "tool_result", "internal"]
    for history_request in (
        TurnHistoryLoadRequest(direction="tail", include=history_include),
        TurnHistoryLoadRequest(turn_ids=[str(accepted["turn_id"])], include=history_include),
        TurnHistoryLoadRequest(direction="around", anchor_turn_id=str(accepted["turn_id"]),
                               before_turns=0, after_turns=0, include=history_include),
    ):
        page = reader.load(target.session_id, target_thread, history_request)
        assert [turn.turn_id for turn in page.items] == [accepted["turn_id"]]
        assert page.source_view_id == target_view
        assert all((turn.source_view_id, turn.source_view_revision) ==
                   (page.source_view_id, page.source_view_revision) for turn in page.items)
        assert page.items[0].final_response == "新回答结束"
        assert [message.content for message in page.items[0].user_messages] == (
            [] if compaction_order == "after_provider" else ["压缩期间的新问题"]
        )
        assert {part.source.item_id for turn in page.items for part in turn.response_parts} == {
            item.item_id for item in selected_items if item.semantic_kind != "user_input"
        }
    latest, _, _ = reader.bootstrap(target.session_id, target_thread)
    assert latest is not None and latest.turn_id == accepted["turn_id"]
    assert latest.response_preview == "新回答结束"
    assert len(latest.user_messages) == (0 if compaction_order == "after_provider" else 1)
    source_history = reader.load(source.session_id, source_thread, TurnHistoryLoadRequest(
        direction="tail", include=history_include,
    ))
    assert {turn.turn_id for turn in source_history.items} == {
        old["turn_id"], accepted["turn_id"], probe["turn_id"],
    }
    assert next(turn for turn in source_history.items if turn.turn_id == old["turn_id"]).user_messages
    source_records_by_id = {item.item_id: item.to_dict() for item in source_items}
    assert imported_by_id == {item_id: source_records_by_id[item_id] for item_id in imported_by_id}
    assert "compaction-probe-root" not in imported_by_id
    producer_ids = {entry.ref.ref_id for entry in produced["snapshot"].selection
                    if entry.included and entry.ref.ref_type == "canonical_item"}
    assert producer_ids <= set(imported_by_id)
    if compaction_order == "after_provider":
        assert old_item_ids <= set(imported_by_id)
    else:
        assert not old_item_ids & set(imported_by_id)
        with cold._storage.storage.read_connection(target.session_id, target_thread) as connection:
            assert connection.execute("SELECT 1 FROM context_assemblies WHERE assembly_id=?", (old_first["assembly_id"],)).fetchone() is None
            assert connection.execute("SELECT 1 FROM context_plan_details WHERE assembly_id=?", (old_second["assembly_id"],)).fetchone() is None
    historical = cold._storage.assemblies.get_context_assembly(
        target.session_id, target_thread, assembly_id=produced["assembly_id"],
    )
    assert historical == produced["snapshot"]
    assert cold._storage.assemblies.get_context_plan_registration(
        target.session_id, thread_id=target_thread, plan_id=historical.plan_id,
    ) == saver._storage.assemblies.get_context_plan_registration(
        source.session_id, thread_id=source_thread, plan_id=historical.plan_id,
    )
    for entry in historical.selection:
        if entry.included and entry.detail_ref is not None:
            assert cold.read_context_plan_detail(
                target.session_id, target_thread, detail_ref=entry.detail_ref, include_sensitive=True,
            ) == saver.read_context_plan_detail(
                source.session_id, source_thread, detail_ref=entry.detail_ref, include_sensitive=True,
            )
    assert cold.load_sealed_resource_activation(
        target.session_id, thread_id=target_thread, assembly_id=historical.assembly_id,
        plan_hash=historical.plan_hash, request_hash=historical.request_hash,
    ) == saver.load_sealed_resource_activation(
        source.session_id, thread_id=source_thread, assembly_id=historical.assembly_id,
        plan_hash=historical.plan_hash, request_hash=historical.request_hash,
    )
    next_input = cold.accept_turn(
        target.session_id, thread_id=target_thread, accepted_ingress_id="compaction-target-user",
        acceptance_idempotency_key="compaction-target-accept", job_id="compaction-target-job",
        admission_idempotency_key="compaction-target-admission", payload="目标新问题", root_item_id="compaction-target-root",
    )
    prepared = fork_provider_prepare(
        replace(fork_context, saver=cold), target.session_id, str(next_input["initial_execution_id"]),
        ContextSourceManager(owner=ContextSourceOwnerKey(target.session_id, target_thread), control_state_port=cold),
        "compaction-target-first", attempt=1,
    )
    _assert_fork_provider_history(prepared, source_prepared, source_items, selected_ids, "compaction-target-root")
    assert not old_item_ids & {entry.ref.ref_id for entry in prepared["snapshot"].selection
                               if entry.included and entry.ref.ref_type == "canonical_item"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork"])
async def test_fork_rejects_orphan_tool_result_before_publication(
    fork_context: ForkIntegrationContext, fork_provider_prepare, mode: SessionForkMode,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    saver = fork_context.saver
    thread = _main_thread_id(fork_context, source.session_id)
    owner = MutationIntentOwner(source.session_id, thread)
    previous = await _get_tuple(fork_context, source.session_id)
    assert previous is not None
    with saver._storage.storage.read_connection(source.session_id, thread) as connection:
        (call_id,) = connection.execute(
            "SELECT model_call_id FROM model_calls WHERE execution_id='execution-turn-2'",
        ).fetchone()
    result = ToolMessage(content="工具组正文", id="queue-tool-result", tool_call_id="queue-tool")
    final = AIMessage(content="工具组回答完成", id="queue-tool-final", response_metadata={"model_call_id": call_id})
    old_messages = [*previous.checkpoint["channel_values"]["messages"],
                    AIMessage(content="读取工具组", id="queue-tool-call",
                              tool_calls=[{"name": "read_file", "args": {"path": "README.md"}, "id": "queue-tool"}],
                              response_metadata={"model_call_id": call_id}), result, final]
    previous_admission = saver.execution_admission(source.session_id, thread_id=thread, execution_id="execution-turn-2")
    with (session_thread_owner_scope(owner, langgraph_thread_id=thread),
          execution_admission_scope(previous_admission, langgraph_thread_id=thread)):
        commit_fixture_output(saver, messages=old_messages)
        await saver.aput(
            build_checkpoint_config(thread), _checkpoint("queue-tool-completed", old_messages),
            {"source": "loop", "step": 4, "parents": {}},
            {"messages": str(len(old_messages)), "counter": str(len(old_messages))},
        )
    completed = await _get_tuple(fork_context, source.session_id)
    assert completed is not None
    old_messages = completed.checkpoint["channel_values"]["messages"]
    result = next(message for message in old_messages if message.id == "queue-tool-result")
    final = next(message for message in old_messages if message.id == "queue-tool-final")
    original_items = saver._storage.storage.read_items(source.session_id, thread)
    call_item = next(item for item in original_items if item.projection_identity is not None
                     and item.projection_identity.projection_message_id == "queue-tool-call")
    final_item = next(item for item in original_items if item.projection_identity is not None
                      and item.projection_identity.projection_message_id == "queue-tool-final")
    result_item = next(item for item in original_items if item.projection_identity is not None
                      and item.projection_identity.projection_message_id == "queue-tool-result")
    assert call_item.projection_identity is not None and final_item.projection_identity is not None
    call_id = call_item.projection_identity.model_call_id
    assert call_id is not None and final_item.projection_identity.model_call_id == call_id
    safe_request = await _fork_request(
        fork_context, source.session_id, mode=mode, creation_kind="new_session_main", item_id=final_item.item_id,
    )
    accepted = saver.accept_turn(
        source.session_id, thread_id=thread, accepted_ingress_id="queue-new-user",
        acceptance_idempotency_key="queue-new-accept", job_id="queue-new-job",
        admission_idempotency_key="queue-new-admission", payload="工具组压缩后的新问题", root_item_id="queue-new-root",
    )
    admission = saver.execution_admission(source.session_id, thread_id=thread,
                                         execution_id=str(accepted["initial_execution_id"]))
    root = next(item for item in saver._storage.storage.read_items(source.session_id, thread)
                if item.item_id == "queue-new-root")
    user = messages_from_dict([LangChainMessageCodec().project_message((root,))])[0]
    retained = [result, final, user]
    summarized = [message for message in old_messages if message.id not in {"queue-tool-result", "queue-tool-final"}]
    summary = HumanMessage(content="已压缩的前置说明", id="queue-summary")
    event = build_cache_preserving_event(
        CachePreservingPartition(prefix_messages=[], messages_to_summarize=summarized,
                                 preserved_messages=retained, state_cutoff=len(summarized)),
        summary_message=summary,
    )
    compacted = _checkpoint("queue-compacted", [*summarized, *retained])
    compacted["channel_values"]["_summarization_event"] = event
    compacted["channel_versions"]["_summarization_event"] = "1"
    compacted["updated_channels"].append("_summarization_event")
    source_items = tuple(item.to_dict() for item in saver._storage.storage.read_items(source.session_id, thread))
    checkpoint_before = await _get_tuple(fork_context, source.session_id)
    source_index = saver._storage.storage.root(source.session_id, thread_id=thread) / "index.sqlite"
    # 保持同一索引连接，避免 SQLite 的正常 WAL 回收改变文件清单基线。
    with closing(sqlite3.connect(source_index.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("SELECT 1 FROM database_meta WHERE singleton_id=1").fetchone()
        before_compaction = _fork_intent_state(fork_context)
        with (session_thread_owner_scope(owner, langgraph_thread_id=thread),
              execution_admission_scope(admission, langgraph_thread_id=thread),
              pytest.raises(ToolProtocolBoundaryConflict, match="tool-protocol-boundary-conflict")):
            await saver.aput(
                build_checkpoint_config(thread), compacted, {"source": "compaction", "step": 5, "parents": {}},
                compacted["channel_versions"],
            )
        after_compaction = _fork_intent_state(fork_context)
        assert after_compaction == before_compaction, (
            set(after_compaction[1]) - set(before_compaction[1]),
            set(before_compaction[1]) - set(after_compaction[1]),
        )
    assert await _get_tuple(fork_context, source.session_id) == checkpoint_before
    assert tuple(item.to_dict() for item in saver._storage.storage.read_items(source.session_id, thread)) == source_items
    assert call_item.message_group_id is not None
    # 明确注入旧边界曾允许提交的损坏 membership；不把该索引当作合法可执行来源。
    with closing(sqlite3.connect(source_index)) as connection:
        (view_id,) = connection.execute(
            "SELECT branch.head_view_id FROM checkpoint_namespace_state namespace "
            "JOIN branches branch ON branch.branch_id=namespace.active_branch_id WHERE namespace.checkpoint_ns=''",
        ).fetchone()
        mutate_view_members(connection,
            "UPDATE context_view_items SET visible=0 WHERE view_id=? AND item_id IN "
            "(SELECT item_id FROM item_catalog WHERE message_group_id=?)", (view_id, call_item.message_group_id),
        )
        connection.execute("UPDATE database_meta SET history_view_revision=history_view_revision+1 WHERE singleton_id=1")
        connection.commit()
    with saver._storage.storage.read_connection(source.session_id, thread) as connection:
        view_id, revision = connection.execute(
            "SELECT branch.head_view_id,meta.history_view_revision FROM checkpoint_namespace_state namespace "
            "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
            "JOIN database_meta meta ON meta.singleton_id=1 WHERE namespace.checkpoint_ns=''",
        ).fetchone()
        visible = {row[0] for row in connection.execute(
            "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1", (view_id,),
        )}
    assert {final_item.item_id, result_item.item_id} <= visible and call_item.item_id not in visible
    request = safe_request.model_copy(update={"source_view_id": view_id, "source_view_revision": revision})
    before = _fork_intent_state(fork_context)
    source_items = tuple(item.to_dict() for item in saver._storage.storage.read_items(source.session_id, thread))
    with pytest.raises(ToolProtocolBoundaryConflict, match="tool-protocol-boundary-conflict"):
        await fork_context.forks.source_point(
            session_id=source.session_id, item_id=final_item.item_id,
            source_view_id=view_id, source_view_revision=revision,
        )
    assert _fork_intent_state(fork_context) == before
    with pytest.raises(ToolProtocolBoundaryConflict, match="tool-protocol-boundary-conflict"):
        await fork_context.forks.fork(request)
    assert _fork_intent_state(fork_context) == before
    assert tuple(item.to_dict() for item in saver._storage.storage.read_items(source.session_id, thread)) == source_items


@pytest.mark.asyncio
async def test_default_fork_does_not_materialize_running_tail(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)

    child = (await fork_context.forks.fork(await _fork_request(fork_context, source.session_id, creation_kind="new_session_main", mode="context_fork"))).session
    restored = await _get_tuple(fork_context, child.session_id)

    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == ["问题一", "回答一"]
    assert _fork_origins(fork_context, child.session_id)[0][2] == "cp2"
    child_root = _main_thread_node(fork_context, child.session_id)
    with sqlite3.connect(child_root / "rollout" / "index.sqlite") as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM turns WHERE turn_id = 'turn-2'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_view_prefix_fork_accepts_committed_running_turn_root(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)

    source_thread = _main_thread_id(fork_context, source.session_id)
    before = tuple(item.to_dict() for item in fork_context.saver._storage.storage.read_items(source.session_id, source_thread))
    with fork_context.saver._storage.storage.read_connection(source.session_id, source_thread) as connection:
        source_turn = connection.execute("SELECT status,root_input_item_id FROM turn_records WHERE turn_id='turn-2'").fetchone()
    assert source_turn == ("active", "item-u2")
    request = await _fork_request(fork_context, source.session_id, creation_kind="new_session_main", turn_id="turn-2")
    assert request.anchor_item_id == "item-u2"
    result = await fork_context.forks.fork(request)
    restored = await _get_tuple(fork_context, result.session.session_id)
    assert restored is not None
    assert [message.content for message in restored.checkpoint["channel_values"]["messages"]] == ["问题一", "回答一", "问题二"]
    with fork_context.saver._storage.storage.read_connection(result.session.session_id, result.thread_id) as connection:
        assert connection.execute("SELECT status,root_input_item_id FROM turn_records WHERE turn_id='turn-2'").fetchone() == source_turn
        assert connection.execute("SELECT 1 FROM executions WHERE session_id=? AND thread_id=?", (result.session.session_id, result.thread_id)).fetchone() is None
    assert tuple(item.to_dict() for item in fork_context.saver._storage.storage.read_items(source.session_id, source_thread)) == before


@pytest.mark.asyncio
async def test_context_fork_turn_id_resolves_latest_active_lineage_view(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)

    child = (await fork_context.forks.fork(await _fork_request(fork_context, source.session_id, creation_kind="new_session_main", mode="context_fork", turn_id="turn-1"))).session
    restored = await _get_tuple(fork_context, child.session_id)

    assert restored is not None
    assert [
        message.content for message in restored.checkpoint["channel_values"]["messages"]
    ] == ["问题一", "回答一"]
    assert _fork_origins(fork_context, child.session_id)[0][3] is not None




@pytest.mark.asyncio
async def test_pinned_fork_keeps_source_deletion_blocked(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    child = (await fork_context.forks.fork(await _fork_request(fork_context, source.session_id, creation_kind="new_session_main", relationship="pinned", target_container_uri=source.uri))).session
    with pytest.raises(RuntimeError, match=child.session_id):
        await fork_context.sessions.path_resolver.delete_subtree(idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id)


@pytest.mark.asyncio
async def test_pinned_fork_retention_released_when_child_is_deleted(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    child = (await fork_context.forks.fork(await _fork_request(fork_context, source.session_id, creation_kind="new_session_main", relationship="pinned", target_container_uri=source.uri))).session

    active_refs = _retention_refs(fork_context, source.session_id)
    assert len(active_refs) == 1
    assert active_refs[0][0] == "fork"
    assert active_refs[0][2] is not None
    assert active_refs[0][3] == child.session_id
    assert active_refs[0][4] == "active"

    await fork_context.sessions.path_resolver.delete_subtree(idempotency_key=f"fork-delete-{child.session_id}", root_node_id=child.session_id)

    released_refs = _retention_refs(fork_context, source.session_id)
    assert released_refs[0][4] == "released"
    await fork_context.sessions.path_resolver.delete_subtree(idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [
    "fork.retention.release.prepared", "fork.retention.release.source_ref_released",
    "fork.retention.release.source_released",
])
async def test_pinned_target_delete_recovers_release_without_source_reopen_or_double_gate(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    target = (await fork_context.forks.fork(await _fork_request(fork_context, source.session_id, creation_kind="new_session_main", relationship="pinned"))).session
    target_node = _main_thread_node(fork_context, target.session_id)
    catalog = fork_context.sessions.path_resolver.catalog_store
    claim, = catalog.list_fork_retention_claims_for_target(target.session_id)
    original = retention_release.observe_phase
    sessions_root = fork_context.saver._storage.storage.sessions_dir

    async def probe_free_gates() -> None:
        # source ref 阶段允许单独持 source gate，target 必须始终可取。
        async with SessionLifecycleGate(sessions_root).exclusive(target.session_id):
            pass
        if phase != "fork.retention.release.source_ref_released":
            async with SessionLifecycleGate(sessions_root).exclusive(source.session_id):
                pass

    async def crash_at_release(operation_id: str, reached: str, **kwargs: object) -> None:
        if operation_id == claim.fork_retention_claim_id and reached == phase:
            await asyncio.wait_for(probe_free_gates(), timeout=3)
            raise RuntimeError("模拟 retention release 崩溃")
        await original(operation_id, reached, **kwargs)

    monkeypatch.setattr(retention_release, "observe_phase", crash_at_release)
    delete_key = f"fork-delete-{target.session_id}"
    with pytest.raises(RuntimeError, match="模拟 retention release"):
        await fork_context.sessions.path_resolver.delete_subtree(idempotency_key=delete_key, root_node_id=target.session_id)
    assert catalog.get_node(target.session_id).state == "deleting"
    control = SessionControlStore(target_node / "session-control.sqlite", read_only=True)
    try:
        frozen, = read_fork_retention_releases(control)
        assert frozen.state == "preparing"
        assert frozen.fork_retention_claim_id == claim.fork_retention_claim_id
        assert frozen.source_thread_id == await fork_context.sessions.resolve_main_thread(source.session_id)
        assert control.get_fence() == ("active", 1)
    finally:
        control.close()
    current = catalog.get_fork_retention_claim(claim.fork_retention_claim_id)
    assert current.state == ("released" if phase.endswith(".source_released") else "active")
    monkeypatch.setattr(retention_release, "observe_phase", original)
    if current.state == "released":
        await fork_context.sessions.path_resolver.delete_subtree(
            idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id,
        )
        with pytest.raises(KeyError):
            catalog.get_node(source.session_id)
    restarted = RolloutCheckpointSaver(sessions_root)
    await restarted.release_fork_retentions(target.session_id)
    control = SessionControlStore(target_node / "session-control.sqlite", read_only=True)
    try:
        terminal, = read_fork_retention_releases(control)
        assert terminal == replace(frozen, state="committed")
    finally:
        control.close()
    await fork_context.sessions.path_resolver.delete_subtree(idempotency_key=delete_key, root_node_id=target.session_id)
    assert catalog.get_fork_retention_claim(claim.fork_retention_claim_id).state == "released"
    with pytest.raises(KeyError):
        catalog.get_node(target.session_id)


@pytest.mark.asyncio
async def test_active_retention_missing_source_ref_does_not_release_claim(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    target = (await fork_context.forks.fork(await _fork_request(fork_context, source.session_id, creation_kind="new_session_main", relationship="pinned"))).session
    catalog = fork_context.sessions.path_resolver.catalog_store
    claim, = catalog.list_fork_retention_claims_for_target(target.session_id)
    with sqlite3.connect(_main_thread_node(fork_context, source.session_id) / "rollout/index.sqlite") as connection:
        connection.execute("DELETE FROM retention_refs WHERE reference_id = ?", (claim.fork_retention_claim_id,))
    with pytest.raises(RuntimeError, match="source ref 缺失"):
        await fork_context.saver.release_fork_retentions(target.session_id)
    assert catalog.get_fork_retention_claim(claim.fork_retention_claim_id).state == "active"
    assert catalog.get_node(target.session_id).state == "active"


@pytest.mark.asyncio
async def test_context_and_history_preflight_rejects_before_target_creation(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    before = await fork_context.sessions.list(limit=100)
    assert before.total == 1

    request = await _fork_request(fork_context, source.session_id, creation_kind="new_session_main")
    with pytest.raises(ValueError, match="source-mismatch"):
        await fork_context.forks.fork(request.model_copy(update={"source_view_id": "view-does-not-exist"}))
    with pytest.raises(ValueError, match="source view revision 已变化"):
        await fork_context.forks.fork(request.model_copy(update={"source_view_revision": request.source_view_revision + 1}))

    after = await fork_context.sessions.list(limit=100)
    assert after.total == before.total == 1
    assert [session.session_id for session in after.items] == [source.session_id]


@pytest.mark.asyncio
async def test_full_copy_cancelled_historical_is_not_resumable_but_new_replay_is(
    fork_context: ForkIntegrationContext,
) -> None:
    source = await fork_context.sessions.create(
        SessionCreateRequest(title="取消态 full-copy 源会话")
    )
    accepted = fork_context.saver.accept_turn(
        source.session_id,
        thread_id=fork_context.saver._storage.storage.main_thread_id(source.session_id), accepted_ingress_id="cancelled-ingress-1",
        acceptance_idempotency_key="cancelled-acceptance-1",
        job_id="cancelled-job-1",
        admission_idempotency_key="cancelled-admission-1",
        payload="取消后只能新 Turn 回放",
        turn_id="turn-cancelled-source",
        root_item_id="item-cancelled-source",
        initial_execution_id="execution-cancelled-source",
    )
    source_root = _main_thread_node(fork_context, source.session_id)
    with sqlite3.connect(source_root / "rollout" / "index.sqlite") as connection:
        item_offset, item_length = connection.execute(
            "SELECT jsonl_offset, jsonl_length FROM item_catalog WHERE item_id = ?",
            ("item-cancelled-source",),
        ).fetchone()
        item_line = (source_root / "rollout" / "rollout.jsonl").read_bytes()[
            item_offset : item_offset + item_length
        ]
        connection.execute(
            "INSERT INTO item_parts(item_id, part_id, part_ordinal, part_semantic_kind, "
            "content_prefix_hash, content_hash, locator_json, line_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "item-cancelled-source",
                "part-cancelled-source",
                0,
                "text",
                None,
                "sha256:jcs:v1:part-placeholder",
                '{"json_pointer":"/payload"}',
                hashlib.sha256(item_line).hexdigest(),
                "2026-08-15T00:00:00+00:00",
            ),
        )
        connection.commit()
    fork_context.saver.converge_execution(
        source.session_id,
        thread_id=fork_context.saver._storage.storage.main_thread_id(source.session_id), turn_id="turn-cancelled-source",
        execution_id=str(accepted["initial_execution_id"]),
        outcome="cancelled",
        turn_status="cancelled",
    )
    source_clocks = _turn_history_clocks(fork_context, source.session_id)

    display = await fork_context.forks.source_point(session_id=source.session_id)
    assert display.anchor_item_id is None and display.resource_mode == "view_prefix_fork"
    with pytest.raises(ValueError, match="fork_source_not_completed"):
        await fork_context.forks.source_point(
            session_id=source.session_id, source_view_id=display.source_view_id,
            source_view_revision=display.source_view_revision, mode="context_fork",
        )
    point = await fork_context.forks.source_point(
        session_id=source.session_id, source_view_id=display.source_view_id,
        source_view_revision=display.source_view_revision, mode="full_rollout_copy",
    )
    assert point.anchor_item_id is None
    child = (await fork_context.forks.fork(SessionForkRequest(
        idempotency_key="cancelled-full-copy", source_uri=point.source_uri,
        source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
        mode="full_rollout_copy", creation_kind="new_session_main", target_container_uri=point.main_container_uri,
    ))).session
    child_thread_id = fork_context.saver._storage.storage.main_thread_id(child.session_id)
    child_root = _main_thread_node(fork_context, child.session_id)
    with sqlite3.connect(child_root / "rollout" / "index.sqlite") as connection:
        target_turn_id, target_root_id, target_status = connection.execute(
            "SELECT turn_id, root_input_item_id, status FROM turn_records"
        ).fetchone()
        execution_ids = connection.execute(
            "SELECT execution_id FROM executions WHERE turn_id = ?",
            (target_turn_id,),
        ).fetchall()
        part_row = connection.execute(
            "SELECT item_id, part_id, part_ordinal FROM item_parts"
        ).fetchone()
    assert target_turn_id == "turn-cancelled-source"
    assert target_root_id == "item-cancelled-source"
    assert target_status == "cancelled"
    assert _turn_history_clocks(fork_context, child.session_id)[target_turn_id] == source_clocks["turn-cancelled-source"]
    assert _turn_history_clocks(fork_context, source.session_id) == source_clocks
    assert execution_ids
    assert part_row[0] == "item-cancelled-source"
    assert part_row[1] == "part-cancelled-source"
    assert part_row[2] == 0

    with pytest.raises(ValueError, match="turn_not_resumable"):
        fork_context.saver.resume_turn(
            child.session_id,
            thread_id=child_thread_id,
            job_id="job-fork-copy-resume-rejected",
            turn_id=target_turn_id,
        )
    with pytest.raises(ValueError, match="turn_not_resumable"):
        fork_context.saver.dispatch_replay(
            child.session_id,
            thread_id=child_thread_id,
            turn_id=target_turn_id,
        )

    replayed = fork_context.saver.replay_as_new_turn(
        child.session_id,
        thread_id=child_thread_id,
        source_turn_id=target_turn_id,
        acceptance_idempotency_key="child-replay-acceptance-1",
    )
    assert replayed["turn_id"] != target_turn_id
    assert replayed["replay_of_turn_id"] == target_turn_id
    with sqlite3.connect(child_root / "rollout" / "index.sqlite") as connection:
        replay_row = connection.execute(
            "SELECT root_input_item_id, status, replay_of_turn_id FROM turn_records "
            "WHERE turn_id = ?",
            (replayed["turn_id"],),
        ).fetchone()
    assert replay_row[0] != target_root_id
    assert replay_row[1] == "open"
    assert replay_row[2] == target_turn_id
    with sqlite3.connect(child_root / "rollout" / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM model_calls WHERE execution_id IN "
            "(SELECT execution_id FROM executions WHERE turn_id = ?)",
            (replayed["turn_id"],),
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_full_copy_preserves_interrupted_history_and_rejects_old_execution(
    fork_provider_prepare, fork_context: ForkIntegrationContext, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare, tail_outcome="interrupted")
    source_thread = _main_thread_id(fork_context, source.session_id)
    fork_context.saver.converge_execution(
        source.session_id, thread_id=source_thread, turn_id="turn-2",
        execution_id="execution-turn-2", outcome="interrupted", turn_status="interrupted",
    )
    source_root = _main_thread_node(fork_context, source.session_id)
    source_clocks = _turn_history_clocks(fork_context, source.session_id)

    def reject_job_start(*args, **kwargs):
        raise AssertionError("fork 不能启动来源 Job")

    monkeypatch.setattr(JobService, "start_job", reject_job_start)
    result = await fork_context.forks.fork(await _fork_request(
        fork_context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy",
    ))
    target_root = _main_thread_node(fork_context, result.session.session_id)
    with sqlite3.connect(source_root / "rollout/index.sqlite") as original, sqlite3.connect(target_root / "rollout/index.sqlite") as target:
        for table, where in (
            ("turn_records", "turn_id='turn-2'"), ("executions", "execution_id='execution-turn-2'"),
            ("turn_acceptances", "turn_id='turn-2'"),
            ("storage_commits", "commit_kind='terminal_convergence' AND subject_id='turn-2'"),
        ):
            assert target.execute(f"SELECT * FROM {table} WHERE {where}").fetchall() == original.execute(f"SELECT * FROM {table} WHERE {where}").fetchall()
    assert _turn_history_clocks(fork_context, result.session.session_id)["turn-2"] == source_clocks["turn-2"]
    with fork_context.saver._storage.storage.read_connection(result.session.session_id, result.thread_id, "") as connection:
        admission = fork_context.saver._storage.executions._read_execution_admission_connection(
            connection, session_id=source.session_id, thread_id=source_thread, execution_id="execution-turn-2",
        )
    assert (admission.session_id, admission.thread_id, admission.execution_id, admission.job_id) == (
        source.session_id, source_thread, "execution-turn-2", "job-turn-2",
    )
    for action in (
        lambda: fork_context.saver.resume_turn(result.session.session_id, thread_id=result.thread_id, turn_id="turn-2", job_id="target-resume"),
        lambda: fork_context.saver.dispatch_replay(result.session.session_id, thread_id=result.thread_id, turn_id="turn-2"),
    ):
        with pytest.raises(ValueError, match="turn_not_resumable"):
            action()
    replay = fork_context.saver.replay_as_new_turn(
        result.session.session_id, thread_id=result.thread_id, source_turn_id="turn-2",
        acceptance_idempotency_key="interrupted-target-replay",
    )
    assert replay["turn_id"] != "turn-2" and replay["replay_of_turn_id"] == "turn-2"
    assert _turn_history_clocks(fork_context, source.session_id) == source_clocks


@pytest.mark.asyncio
async def test_production_http_pinned_fork_and_preflight_have_exact_side_effects(
    fork_provider_prepare,
    production_fork_container: AppContainer,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context, fork_provider_prepare)
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="context_fork", relationship="pinned", target_container_uri=source.uri)
    catalog = context.sessions.path_resolver.catalog_store
    async with main.lifespan(main.app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://workspace",
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": context.sessions.workspace_id, "X-BoxTeam-Gateway-Id": "gateway_6c8b2f9317a5a2c6c4569958b1e23d84", "X-Request-ID": "fork-http-proof"},
    ) as client:
        before = (await context.sessions.list(limit=100)).total
        with catalog.read_transaction() as connection:
            counts = connection.execute(
                "SELECT (SELECT COUNT(*) FROM session_creation_records), "
                "(SELECT COUNT(*) FROM fork_retention_claims)"
            ).fetchone()
        rejected = await client.post(
            f"/api/v1/sessions/{source.session_id}/fork-context",
            json=request.model_copy(update={"source_view_revision": request.source_view_revision + 1}).model_dump(mode="json"),
        )
        assert rejected.status_code == 400, rejected.text
        assert "revision" in rejected.json()["detail"]
        assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"] == "fork-http-proof"
        assert (await context.sessions.list(limit=100)).total == before
        with catalog.read_transaction() as connection:
            assert connection.execute(
                "SELECT (SELECT COUNT(*) FROM session_creation_records), "
                "(SELECT COUNT(*) FROM fork_retention_claims)"
            ).fetchone() == counts
        assert _retention_refs(context, source.session_id) == []

        response = await client.post(
            f"/api/v1/sessions/{source.session_id}/fork-context",
            json=request.model_dump(mode="json"),
        )
        assert response.status_code == 200, response.text
        child = response.json()["data"]["session"]
        assert child["context_source_session_id"] == source.session_id
        assert child["parent_session_id"] == source.session_id
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "fork-http-proof"
        target_root = _main_thread_node(context, child["session_id"])
        with sqlite3.connect(target_root / "rollout" / "index.sqlite") as connection:
            assert connection.execute("SELECT status FROM fork_materializations").fetchall() == [("committed",)]
            assert connection.execute(
                "SELECT COUNT(*) FROM item_catalog WHERE execution_id IS NULL OR execution_id = ''"
            ).fetchone() == (0,)
        assert len(_retention_refs(context, source.session_id)) == 1
        first = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
        second = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_copy(update={"idempotency_key": f"fork-{uuid4().hex}"}).model_dump(mode="json"))
        assert first.status_code == second.status_code == 200
        assert first.json()["data"]["session"]["session_id"] != second.json()["data"]["session"]["session_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_fork_target", [True, False], ids=["same_scope", "different_scope"], indirect=True)
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_distinct_live_fork_owners_verify_scope_before_capture(
    fork_provider_prepare, production_fork_container: AppContainer,
    remote_fork_target: tuple[AppContainer, SourceForkOwnerTransport],
    fork_gateway_request_context: None, creation_kind: SessionForkCreationKind,
) -> None:
    """独立 catalog 的真实 owner 不能靠不同 Gateway authority 绕过同 scope 冲突。"""
    source_context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(
        source_context, fork_provider_prepare, attachment_store=production_fork_container.attachment_blob_store,
    )
    target, transport = remote_fork_target
    target_context = _production_fork_context(target)
    assert source_context.workspace != target_context.workspace
    source_catalog = source_context.sessions.path_resolver.catalog_store
    target_catalog = target_context.sessions.path_resolver.catalog_store
    assert source_catalog.database_path != target_catalog.database_path
    assert source_context.saver.forks.instance_id != target_context.saver.forks.instance_id
    parent = await target.session_service.create(SessionCreateRequest(title="独立 owner 目标容器"))
    point = await source_context.forks.source_point(session_id=source.session_id, mode="full_rollout_copy")
    assert len(point.resources) == 1
    request = SessionForkRequest(
        idempotency_key="verified-scope-" + creation_kind,
        source_uri=thread_resource_uri(source.workspace_id, source.session_id,
            _main_thread_id(source_context, source.session_id), gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729"),
        target_container_uri=resource_display_uri(
            scope="workspace", scope_id=target.workspace_id, kind="session",
            tail_segments=(parent.session_id,) if creation_kind == "new_child_thread" else (),
            gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744",
        ),
        creation_kind=creation_kind, mode="full_rollout_copy",
        source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
        resources=[SessionForkResourceChoice(resource_uri=point.resources[0].resource_uri, action="copy_snapshot")],
    )
    key = target_context.forks._creation_key(request.idempotency_key)
    before = (_fork_intent_state(source_context), _fork_intent_state(target_context))
    attachment_paths = [container.attachment_blob_store._catalog.database_path
                        for container in (production_fork_container, target)]
    attachment_before = tuple(_session_control_state(path) if path.exists() else None for path in attachment_paths)
    target_control = target_context.sessions.path_resolver.resolve_session_node(parent.session_id) / "session-control.sqlite"
    control_before = _session_control_state(target_control)
    gateway_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
    request_token = set_current_request_id("verified-scope-fork")
    try:
        if source.workspace_id == target.workspace_id:
            with pytest.raises(ValueError, match="fork_owner_overlap"):
                await target_context.forks.fork(request)
            assert [call.action for call in transport.calls] == ["intent"]
            assert (_fork_intent_state(source_context), _fork_intent_state(target_context)) == before
            assert tuple(_session_control_state(path) if path.exists() else None for path in attachment_paths) == attachment_before
            assert _session_control_state(target_control) == control_before
            for catalog in (source_catalog, target_catalog):
                assert catalog.get_fork_source_capture(copy_operation_id=key) is None
                with pytest.raises(KeyError):
                    catalog.get_fork_export_release(key)
            with pytest.raises(KeyError):
                target_catalog.get_creation_record(key)
        else:
            result = await target_context.forks.fork(request)
            assert result.export_release is not None and result.export_release.state == "confirmed"
            assert len(result.resources) == 1 and result.resources[0].action == "copy_snapshot"
            assert result.resources[0].availability == "available"
            assert target.attachment_blob_store.read(
                result.session.session_id, result.resources[0].target_uri, owner_thread_id=result.thread_id,
            ).data == "文件内容".encode()
            assert [call.action for call in transport.calls].count("intent") == 2
            assert [call.action for call in transport.calls].count("capture") == 1
            assert [call.action for call in transport.calls].count("release") == 1
            for catalog in (source_catalog, target_catalog):
                capture = catalog.get_fork_source_capture(copy_operation_id=key)
                assert capture is not None and capture.state == "materialized"
                assert "source_owner_instance_id" not in capture.request_json
                frozen = json.loads(capture.request_json)
                assert (frozen["source_storage_domain_id"], frozen["target_storage_domain_id"]) == (source.workspace_id, target.workspace_id)
            assert target_catalog.get_fork_export_release(key) == ("confirmed", None)
        intents = [reply.intent for reply in transport.replies if reply.intent is not None]
        assert len(intents) == (1 if source.workspace_id == target.workspace_id else 2)
        assert all(intent["source_owner_instance_id"] == source_context.saver.forks.instance_id for intent in intents)
        assert all(intent["source_storage_domain_id"] == source_catalog.get_node(source.session_id).workspace_id for intent in intents)
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_same_live_fork_owner_across_gateways_keeps_shared_capture_and_replay(
    fork_provider_prepare, production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    fork_gateway_request_context: None, creation_kind: SessionForkCreationKind,
) -> None:
    """两个 Gateway 的序列化请求实际回到同一 owner，发布后重放不访问来源。"""
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(
        context, fork_provider_prepare, attachment_store=production_fork_container.attachment_blob_store,
    )
    point = await context.forks.source_point(session_id=source.session_id, mode="full_rollout_copy")
    transport = SourceForkOwnerTransport(context.forks, [])
    monkeypatch.setattr(context.forks, "_transport", transport)
    request = SessionForkRequest(
        idempotency_key="same-owner-gateways-" + creation_kind,
        source_uri=thread_resource_uri(source.workspace_id, source.session_id,
            _main_thread_id(context, source.session_id), gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729"),
        target_container_uri=resource_display_uri(
            scope="workspace", scope_id=source.workspace_id, kind="session",
            tail_segments=(source.session_id,) if creation_kind == "new_child_thread" else (),
            gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744",
        ),
        creation_kind=creation_kind, mode="full_rollout_copy",
        source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
    )
    gateway_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
    request_token = set_current_request_id("same-owner-two-gateway-fork")
    try:
        result = await context.forks.fork(request)
        assert result.export_release is None
        assert len(result.resources) == 1
        assert result.resources[0].action == ("reuse_reference" if creation_kind == "new_child_thread" else "retain_reference")
        assert production_fork_container.attachment_blob_store.read(
            result.session.session_id, result.resources[0].target_uri, owner_thread_id=result.thread_id,
        ).data == "文件内容".encode()
        intents = [reply.intent for reply in transport.replies if reply.intent is not None]
        assert len(intents) == 1
        assert all(intent["source_owner_instance_id"] == context.saver.forks.instance_id for intent in intents)
        assert all(intent["source_storage_domain_id"] == context.sessions.path_resolver.catalog_store.get_node(source.session_id).workspace_id
                   for intent in intents)
        key = context.forks._creation_key(request.idempotency_key)
        catalog = context.sessions.path_resolver.catalog_store
        capture = catalog.get_fork_source_capture(copy_operation_id=key)
        assert capture is not None and capture.state == "materialized"
        frozen = json.loads(capture.request_json)
        assert frozen["source_storage_domain_id"] == frozen["target_storage_domain_id"] == source.workspace_id
        assert catalog.get_fork_export_release(key) == (None, None)
        assert [call.action for call in transport.calls] == ["intent"]
        assert "source_owner_instance_id" not in capture.request_json
        creation = (catalog.get_creation_record(key) if creation_kind == "new_session_main"
                    else context.forks._threads.for_owner_session(source.session_id).control_store.get_thread_creation_record(key))
        assert "source_owner_instance_id" not in creation.request_intent_json
        assert "source_storage_domain_id" not in json.loads(creation.request_intent_json)
        assert json.loads(creation.request_intent_json)["capture_decision"] == {
            "source_storage_domain_id": source.workspace_id, "target_storage_domain_id": source.workspace_id,
            "capture_owner": "local",
        }

        async def source_offline(payload: SessionForkTransferRequest) -> SessionForkTransferResultDTO:
            raise AssertionError("已发布 same-owner replay 不得重新访问 Gateway B")

        monkeypatch.setattr(transport, "fork_transfer", source_offline)
        assert await context.forks.fork(request) == result
        assert catalog.get_fork_source_capture(copy_operation_id=key) == capture
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("crash_phase", [
    "creation_durable", "copy.source.reserved", "copy.source.partial", "copy.attachments.preparing",
    "copy.source.before_captured_cas", "fork.target_committed", "copy.target.published",
])
async def test_shared_owner_cold_recovery_uses_frozen_creation_decision(
    fork_provider_prepare, production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, fork_gateway_request_context: None,
    creation_kind: SessionForkCreationKind, crash_phase: str,
) -> None:
    """新 owner 启动时不经 Gateway 自绕；目录是否已创建决定捕获或精确中止。"""
    from app.core import thread_creation as thread_creation_module
    from app.services.business import session_context_fork_service as service_module

    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(
        context, fork_provider_prepare, attachment_store=production_fork_container.attachment_blob_store,
    )
    source_thread = _main_thread_id(context, source.session_id)
    source_jsonl = (_main_thread_node(context, source.session_id) / "rollout/rollout.jsonl").read_bytes()
    point = await context.forks.source_point(session_id=source.session_id, mode="full_rollout_copy")
    request = SessionForkRequest(
        idempotency_key=f"shared-crash-{creation_kind}-{crash_phase}",
        source_uri=thread_resource_uri(source.workspace_id, source.session_id, source_thread, gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729"),
        target_container_uri=resource_display_uri(
            scope="workspace", scope_id=source.workspace_id, kind="session",
            tail_segments=(source.session_id,) if creation_kind == "new_child_thread" else (),
            gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744",
        ),
        creation_kind=creation_kind, mode="full_rollout_copy",
        source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
    )
    key = context.forks._creation_key(request.idempotency_key)
    transport = SourceForkOwnerTransport(context.forks, [])
    monkeypatch.setattr(context.forks, "_transport", transport)
    captures = 0
    capture_source = context.saver.forks.source._capture_fork_source_snapshot

    async def count_capture(**kwargs: object):
        nonlocal captures
        captures += 1
        return await capture_source(**kwargs)

    class OwnerProcessExit(BaseException):
        pass

    def interrupt_creation(*_args: object, **_kwargs: object):
        raise OwnerProcessExit("creation journal 已持久，物理准备尚未开始")

    monkeypatch.setattr(context.saver.forks.source, "_capture_fork_source_snapshot", count_capture)
    observers = {module: module.observe_phase for module in (service_module, fork_owner, fork_source, thread_creation_module)}
    with monkeypatch.context() as crash:
        if crash_phase == "creation_durable" and creation_kind == "new_session_main":
            crash.setattr(context.sessions._creation_service, "_prepare_staging", interrupt_creation)
        else:
            for module, observe in observers.items():
                async def interrupt_phase(operation_id: str, phase: str, *, original=observe, **kwargs: object):
                    await original(operation_id, phase, **kwargs)
                    expected_phase = "thread.creation.prepared" if crash_phase == "creation_durable" else crash_phase
                    if operation_id == key and phase == expected_phase:
                        raise OwnerProcessExit(f"模拟实际 owner 在 {phase} 后退出")

                crash.setattr(module, "observe_phase", interrupt_phase)
        gateway_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
        request_token = set_current_request_id("shared-crash-frozen-delivery")
        try:
            with pytest.raises(OwnerProcessExit):
                await context.forks.fork(request)
        finally:
            reset_current_request_id(request_token)
            reset_current_gateway_id(gateway_token)
    catalog = context.sessions.path_resolver.catalog_store
    frozen = (catalog.get_creation_record(key) if creation_kind == "new_session_main" else
              context.forks._threads.for_owner_session(source.session_id).control_store.get_thread_creation_record(key))
    intent = json.loads(frozen.request_intent_json)
    incomplete = crash_phase in {"copy.source.partial", "copy.attachments.preparing"}
    assert intent["source_remote"] is True
    assert intent["capture_decision"] == {
        "source_storage_domain_id": source.workspace_id, "target_storage_domain_id": source.workspace_id,
        "capture_owner": "local",
    }
    assert "source_owner_instance_id" not in frozen.request_intent_json
    assert "source_target_creation_preimage_hash" not in frozen.request_intent_json
    assert frozen.fork_delivery_request_id == "shared-crash-frozen-delivery"
    assert [call.action for call in transport.calls] == ["intent"]
    capture = catalog.get_fork_source_capture(copy_operation_id=key)
    root = capture_directory(context.saver._storage.storage.sessions_dir, capture) if capture is not None else None
    if crash_phase == "creation_durable":
        assert capture is None and captures == 0 and frozen.state == "preparing"
        if creation_kind == "new_session_main":
            assert not (context.sessions.path_resolver.sessions_root / ".staging" / frozen.session_id).exists()
        else:
            assert not (context.sessions.path_resolver.resolve_session_node(source.session_id) / frozen.final_relative_locator).exists()
    else:
        assert capture is not None and catalog.get_fork_export_release(key) == (None, None)
        if crash_phase == "copy.source.reserved":
            assert capture.state == "preparing" and not root.exists() and captures == 0
        elif incomplete:
            assert capture.state == "preparing" and root.is_dir() and not (root / "captured.json").exists()
            with pytest.raises(IncompleteForkSourceCaptureError):
                read_captured_snapshot(context.saver._storage.storage.sessions_dir, capture)
        elif crash_phase in {"copy.source.before_captured_cas", "fork.target_committed"}:
            read_captured_snapshot(context.saver._storage.storage.sessions_dir, capture)
        else:
            assert frozen.state == "published" and capture.state == "materialized" and not root.exists()
    claims = [tuple(row) for row in production_fork_container.attachment_blob_store._catalog._connected().execute(
        "SELECT claim_id,state FROM fork_attachment_claims WHERE copy_operation_id=? ORDER BY claim_id", (key,),
    )]
    if crash_phase == "copy.attachments.preparing":
        assert [row[1] for row in claims] == (["preparing"] if creation_kind == "new_session_main" else [])
    prepared_target = (context.sessions.path_resolver.resolve_prepared_session_node(frozen)
                       if incomplete and creation_kind == "new_session_main" else
                       context.sessions.path_resolver.resolve_session_node(source.session_id) / frozen.final_relative_locator
                       if incomplete else None)
    old_instance = context.saver.forks.instance_id
    production_fork_container.workspace_activity_service.store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    recovered = _production_fork_context(restarted)
    assert recovered.saver.forks.instance_id != old_instance
    cold_capture_source = recovered.saver.forks.source._capture_fork_source_snapshot

    def forbid_creation_configuration(*_args: object, **_kwargs: object):
        pytest.fail("恢复必须复用 journal 的 Agent/Provider，不得重新解析当前配置")

    monkeypatch.setattr(recovered.sessions._config_service, "resolve_new_session_agent_id", forbid_creation_configuration)
    monkeypatch.setattr(recovered.sessions._config_service, "resolve_new_session_provider_id", forbid_creation_configuration)

    async def offline_gateway(_payload: SessionForkTransferRequest):
        pytest.fail("shared startup/replay 不得访问 Gateway 或回到尚未监听的同 Backend")

    async def cold_capture(**kwargs: object):
        nonlocal captures
        assert crash_phase in {"creation_durable", "copy.source.reserved"}, "已有私有目录或已发布 snapshot 禁止回读 live source"
        captures += 1
        return await cold_capture_source(**kwargs)

    monkeypatch.setattr(transport, "fork_transfer", offline_gateway)
    monkeypatch.setattr(recovered.forks, "_transport", transport)
    monkeypatch.setattr(recovered.saver.forks.source, "_capture_fork_source_snapshot", cold_capture)
    cold_gateway = set_current_gateway_id(None)
    cold_request = set_current_request_id(None)
    try:
        async with main.lifespan(main.app):
            cold_catalog = recovered.sessions.path_resolver.catalog_store
            terminal = (cold_catalog.get_creation_record(key) if creation_kind == "new_session_main" else
                        recovered.forks._threads.for_owner_session(source.session_id).control_store.get_thread_creation_record(key))
            terminal_capture = cold_catalog.get_fork_source_capture(copy_operation_id=key)
            assert terminal_capture is not None
            assert (terminal.preimage_hash, terminal.request_intent_json, terminal.fork_delivery_request_id) == (
                frozen.preimage_hash, frozen.request_intent_json, frozen.fork_delivery_request_id,
            )
            if creation_kind == "new_session_main":
                assert (terminal.session_id, terminal.main_thread_id, terminal.created_at, terminal.storage_relative_locator,
                        terminal.session_metadata_json, terminal.graph_binding) == (
                    frozen.session_id, frozen.main_thread_id, frozen.created_at, frozen.storage_relative_locator,
                    frozen.session_metadata_json, frozen.graph_binding,
                )
            else:
                assert (terminal.child_thread_id, terminal.child_created_at, terminal.record_created_at,
                        terminal.final_relative_locator, terminal.staging_locator, terminal.graph_binding) == (
                    frozen.child_thread_id, frozen.child_created_at, frozen.record_created_at,
                    frozen.final_relative_locator, frozen.staging_locator, frozen.graph_binding,
                )
            assert terminal.state == ("aborted" if incomplete else "published")
            assert terminal_capture.state == ("aborted" if incomplete else "materialized")
            assert cold_catalog.get_fork_export_release(key) == (None, None)
            assert not capture_directory(recovered.saver._storage.storage.sessions_dir, terminal_capture).exists()
            if capture is not None:
                assert (terminal_capture.snapshot_id, terminal_capture.snapshot_relative_locator, terminal_capture.preimage_hash,
                        terminal_capture.fork_id, terminal_capture.materialization_id) == (
                    capture.snapshot_id, capture.snapshot_relative_locator, capture.preimage_hash, capture.fork_id, capture.materialization_id,
                )
            assert captures == 1
            terminal_claims = [tuple(row) for row in restarted.attachment_blob_store._catalog._connected().execute(
                "SELECT claim_id,state FROM fork_attachment_claims WHERE copy_operation_id=? ORDER BY claim_id", (key,),
            )]
            if incomplete:
                assert terminal_claims == [(claim_id, "aborted") for claim_id, _ in claims]
                assert not prepared_target.exists()
            elif creation_kind == "new_session_main":
                assert len(terminal_claims) == 1 and terminal_claims[0][1] == "committed"
                assert not claims or terminal_claims[0][0] == claims[0][0]
            else:
                assert terminal_claims == []
            replay_gateway = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
            replay_request = set_current_request_id("shared-cold-replay-request")
            try:
                if incomplete:
                    with pytest.raises(RuntimeError, match="已中止|aborted"):
                        await recovered.forks.fork(request)
                else:
                    result = await recovered.forks.fork(request)
                    assert result.export_release is None
                    assert (result.session.session_id, result.thread_id) == (
                        frozen.session_id if creation_kind == "new_session_main" else source.session_id,
                        frozen.main_thread_id if creation_kind == "new_session_main" else frozen.child_thread_id,
                    )
                    resource, = result.resources
                    assert resource.availability == "available"
                    assert restarted.attachment_blob_store.read(
                        result.session.session_id, resource.target_uri, owner_thread_id=result.thread_id,
                    ).data == "文件内容".encode()
                    owner = recovered.saver.forks.source.resolve_thread_owner(result.session.session_id, result.thread_id)
                    with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
                        restored = await recovered.saver.aget_tuple(build_checkpoint_config(owner.thread_id))
                    assert restored is not None
            finally:
                reset_current_request_id(replay_request)
                reset_current_gateway_id(replay_gateway)
            assert await recovered.forks.recover_prepared_forks() == 0
            assert cold_catalog.get_fork_source_capture(copy_operation_id=key) == terminal_capture
            assert [tuple(row) for row in restarted.attachment_blob_store._catalog._connected().execute(
                "SELECT claim_id,state FROM fork_attachment_claims WHERE copy_operation_id=? ORDER BY claim_id", (key,),
            )] == terminal_claims
            assert (_main_thread_node(recovered, source.session_id) / "rollout/rollout.jsonl").read_bytes() == source_jsonl
            assert get_current_gateway_id() is None and get_current_request_id() is None
    finally:
        reset_current_request_id(cold_request)
        reset_current_gateway_id(cold_gateway)


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_phase", ["copy.source.reserved", "copy.source.before_captured_cas"])
async def test_child_capture_missing_final_locator_is_not_rebuilt(
    fork_provider_prepare, production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, fork_gateway_request_context: None, crash_phase: str,
) -> None:
    """capture 已在 final rename 后持久化，外删目录不能伪装成创建前崩溃。"""
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(
        context, fork_provider_prepare, attachment_store=production_fork_container.attachment_blob_store,
    )
    request = await _fork_request(context, source.session_id, creation_kind="new_child_thread", mode="full_rollout_copy")
    key = context.forks._creation_key(request.idempotency_key)
    observer = fork_owner.observe_phase

    class OwnerProcessExit(BaseException):
        pass

    async def interrupt(operation_id: str, phase: str, **kwargs: object):
        await observer(operation_id, phase, **kwargs)
        if operation_id == key and phase == crash_phase:
            raise OwnerProcessExit("capture 已持久，模拟 owner 退出")

    with monkeypatch.context() as crash:
        crash.setattr(fork_owner, "observe_phase", interrupt)
        with pytest.raises(OwnerProcessExit):
            await context.forks.fork(request)
    catalog = context.sessions.path_resolver.catalog_store
    capture = catalog.get_fork_source_capture(copy_operation_id=key)
    assert capture is not None and capture.state == "preparing"
    control = context.forks._threads.for_owner_session(source.session_id).control_store
    frozen = control.get_thread_creation_record(key)
    session_node = context.sessions.path_resolver.resolve_session_node(source.session_id)
    final = session_node / frozen.final_relative_locator
    staging = session_node / frozen.staging_locator
    assert final.is_dir() and not staging.exists() and frozen.state == "preparing"
    root = capture_directory(context.saver._storage.storage.sessions_dir, capture)
    captured_bytes = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    source_jsonl = (_main_thread_node(context, source.session_id) / "rollout/rollout.jsonl").read_bytes()
    shutil.rmtree(final)
    production_fork_container.workspace_activity_service.store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    recovered = _production_fork_context(restarted)

    async def forbid_recapture(**_kwargs: object):
        pytest.fail("已有 capture 的外删 target 不得重新读取 source")

    monkeypatch.setattr(recovered.saver.forks.source, "_capture_fork_source_snapshot", forbid_recapture)
    with pytest.raises(RuntimeError, match="已预留 capture.*最终 locator 目录缺失"):
        async with main.lifespan(main.app):
            pytest.fail("外删 target 不得让 startup ready")
    assert not final.exists() and not staging.exists()
    # 失败的 lifespan 会关闭真实 registry；从持久原件复核，不再使用已关闭容器。
    with closing(SessionCatalogStore(catalog.database_path, catalog.sessions_root)) as cold_catalog:
        assert cold_catalog.get_fork_source_capture(copy_operation_id=key) == capture
    with closing(SessionControlStore(session_node / "session-control.sqlite", read_only=True)) as cold_control:
        assert cold_control.get_thread_creation_record(key) == frozen
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()} == captured_bytes
    assert (_main_thread_node(context, source.session_id) / "rollout/rollout.jsonl").read_bytes() == source_jsonl


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_phase", ["capture_aborted", "directories_removed"])
async def test_child_aborted_capture_resumes_original_cleanup_without_rebuilding(
    fork_provider_prepare, production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, fork_gateway_request_context: None, cleanup_phase: str,
) -> None:
    """资源已中止、creation 尚 preparing 的真实崩溃窗口继续清理，不创建目录。"""
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(
        context, fork_provider_prepare, attachment_store=production_fork_container.attachment_blob_store,
    )
    parent = await context.sessions.create(SessionCreateRequest(title="原 cleanup 目标 owner"))
    request = await _fork_request(
        context, source.session_id, creation_kind="new_child_thread", mode="full_rollout_copy",
        target_container_uri=resource_display_uri(
            scope="workspace", scope_id=source.workspace_id, kind="session", tail_segments=(parent.session_id,),
        ),
    )
    key = context.forks._creation_key(request.idempotency_key)
    observer = fork_source.observe_phase
    cleanup = ThreadCreationService._cleanup_after_failed_publish

    class OwnerProcessExit(BaseException):
        pass

    async def fail_partial_capture(operation_id: str, phase: str, **kwargs: object):
        await observer(operation_id, phase, **kwargs)
        if operation_id == key and phase == "copy.attachments.preparing":
            raise RuntimeError("真实 partial capture 失败，进入原 owner 中止")

    async def interrupt_cleanup(record, **kwargs):
        if cleanup_phase == "capture_aborted":
            abort = kwargs["before_abort"]

            async def abort_then_exit(current):
                await abort(current)
                raise OwnerProcessExit("capture 中止后退出")

            kwargs["before_abort"] = abort_then_exit
        await cleanup(record, **kwargs)
        raise OwnerProcessExit("目录清理后、creation 中止前退出")

    with monkeypatch.context() as crash:
        crash.setattr(fork_source, "observe_phase", fail_partial_capture)
        crash.setattr(ThreadCreationService, "_cleanup_after_failed_publish", staticmethod(interrupt_cleanup))
        with pytest.raises(OwnerProcessExit):
            await context.forks.fork(request)
    capture = context.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
    frozen = context.forks._threads.for_owner_session(parent.session_id).control_store.get_thread_creation_record(key)
    assert capture is not None and capture.state == "aborted" and frozen.state == "preparing"
    session_node = context.sessions.path_resolver.resolve_session_node(parent.session_id)
    final, staging = session_node / frozen.final_relative_locator, session_node / frozen.staging_locator
    assert final.exists() == (cleanup_phase == "capture_aborted") and not staging.exists()
    source_jsonl = (_main_thread_node(context, source.session_id) / "rollout/rollout.jsonl").read_bytes()
    production_fork_container.workspace_activity_service.store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    recovered = _production_fork_context(restarted)
    service = recovered.forks._threads.for_owner_session(parent.session_id)

    def forbid_rebuild(*_args: object, **_kwargs: object):
        pytest.fail("已中止 capture 的原 cleanup 不能重建 staging")

    async def forbid_recapture(**_kwargs: object):
        pytest.fail("已中止 capture 的原 cleanup 不能重读 source")

    monkeypatch.setattr(service, "_prepare_staging", forbid_rebuild)
    monkeypatch.setattr(recovered.saver.forks.source, "_capture_fork_source_snapshot", forbid_recapture)
    async with main.lifespan(main.app):
        current = service.control_store.get_thread_creation_record(key)
        assert current.state == "aborted"
        assert replace(current, state=frozen.state, abort_reason=frozen.abort_reason, record_updated_at=frozen.record_updated_at) == frozen
        assert recovered.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key) == capture
        assert not final.exists() and not staging.exists()
        assert not capture_directory(recovered.saver._storage.storage.sessions_dir, capture).exists()
        assert [tuple(row) for row in restarted.attachment_blob_store._catalog._connected().execute(
            "SELECT state FROM fork_attachment_claims WHERE copy_operation_id=?", (key,),
        )] == [("aborted",)]
        with pytest.raises(RuntimeError, match="已中止"):
            await recovered.forks.fork(request)
        assert await recovered.forks.recover_prepared_forks() == 0
        assert (_main_thread_node(recovered, source.session_id) / "rollout/rollout.jsonl").read_bytes() == source_jsonl


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["operation_id", "target_session", "target_thread", "creation_preimage", "capture_preimage"])
async def test_main_fork_conflicting_capture_is_preserved_before_abort(
    fork_provider_prepare, production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, fork_gateway_request_context: None, fault: str,
) -> None:
    """真实恢复拒绝冲突 capture 后，附件、catalog 和私有快照都不能被错误清理。"""
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(
        context, fork_provider_prepare, attachment_store=production_fork_container.attachment_blob_store,
    )
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy")
    key = context.forks._creation_key(request.idempotency_key)
    observer = fork_owner.observe_phase

    class OwnerProcessExit(BaseException):
        pass

    async def interrupt(operation_id: str, phase: str, **kwargs: object):
        await observer(operation_id, phase, **kwargs)
        if operation_id == key and phase == "copy.source.before_captured_cas":
            raise OwnerProcessExit("快照与附件已准备，尚未写 captured CAS")

    with monkeypatch.context() as crash:
        crash.setattr(fork_owner, "observe_phase", interrupt)
        with pytest.raises(OwnerProcessExit):
            await context.forks.fork(request)
    catalog = context.sessions.path_resolver.catalog_store
    frozen = catalog.get_creation_record(key)
    capture = catalog.get_fork_source_capture(copy_operation_id=key)
    assert capture is not None and capture.state == "preparing"
    root = capture_directory(context.saver._storage.storage.sessions_dir, capture)
    captured_bytes = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    source_jsonl = (_main_thread_node(context, source.session_id) / "rollout/rollout.jsonl").read_bytes()
    claims = [tuple(row) for row in production_fork_container.attachment_blob_store._catalog._connected().execute(
        "SELECT * FROM fork_attachment_claims WHERE copy_operation_id=? ORDER BY claim_id", (key,),
    )]
    attachment_catalog_path = production_fork_container.attachment_blob_store._catalog.database_path
    source_node = _main_thread_node(context, source.session_id)
    assert len(claims) == 1
    with sqlite3.connect(context.workspace / ".boxteam/navigation/session-catalog.sqlite") as connection:
        if fault == "operation_id":
            connection.execute("UPDATE fork_source_captures SET copy_operation_id=? WHERE creation_key=?", (key + "-foreign", key))
        elif fault == "target_session":
            connection.execute("UPDATE fork_source_captures SET target_session_id=? WHERE creation_key=?", (source.session_id, key))
        elif fault == "target_thread":
            connection.execute("UPDATE fork_source_captures SET target_thread_id=? WHERE creation_key=?", (_main_thread_id(context, source.session_id), key))
        elif fault == "creation_preimage":
            payload = json.loads(capture.request_json)
            payload["creation_preimage_hash"] = "0" * 64
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute("UPDATE fork_source_captures SET request_json=?,preimage_hash=? WHERE creation_key=?", (encoded, hashlib.sha256(encoded.encode()).hexdigest(), key))
        else:
            connection.execute("UPDATE fork_source_captures SET preimage_hash=? WHERE creation_key=?", ("0" * 64, key))
    conflicting = catalog.get_fork_source_capture(creation_key=key)
    assert conflicting is not None
    # 直接事务入口也必须核原 creation，不能只因同 key 仍 preparing 就取得清理权限。
    with pytest.raises(RuntimeError, match="owner 冲突"):
        catalog.abort_unpublished_fork_capture(conflicting, creation=frozen, reason="不得清理冲突 owner")
    assert catalog.get_fork_source_capture(creation_key=key) == conflicting
    production_fork_container.workspace_activity_service.store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    with pytest.raises(ValueError, match="fork_owner_overlap"):
        async with main.lifespan(main.app):
            pytest.fail("冲突 capture 不得让 startup ready")
    with closing(SessionCatalogStore(catalog.database_path, catalog.sessions_root)) as cold_catalog:
        assert cold_catalog.get_creation_record(key) == frozen
        assert cold_catalog.get_fork_source_capture(creation_key=key) == conflicting
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()} == captured_bytes
    with closing(sqlite3.connect(attachment_catalog_path.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute(
            "SELECT * FROM fork_attachment_claims WHERE copy_operation_id=? ORDER BY claim_id", (key,),
        ).fetchall() == claims
    assert (source_node / "rollout/rollout.jsonl").read_bytes() == source_jsonl


@pytest.mark.asyncio
async def test_main_fork_abort_rejects_rebound_creation_capability(
    fork_provider_prepare, production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, fork_gateway_request_context: None,
) -> None:
    """同 key 的伪造 record 不能先清附件，再由下游 creation owner 才发现身份冲突。"""
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(
        context, fork_provider_prepare, attachment_store=production_fork_container.attachment_blob_store,
    )
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy")
    key = context.forks._creation_key(request.idempotency_key)
    observer = fork_owner.observe_phase

    class OwnerProcessExit(BaseException):
        pass

    async def interrupt(operation_id: str, phase: str, **kwargs: object):
        await observer(operation_id, phase, **kwargs)
        if operation_id == key and phase == "copy.source.before_captured_cas":
            raise OwnerProcessExit("保留真实附件与 capture")

    with monkeypatch.context() as crash:
        crash.setattr(fork_owner, "observe_phase", interrupt)
        with pytest.raises(OwnerProcessExit):
            await context.forks.fork(request)
    catalog = context.sessions.path_resolver.catalog_store
    frozen = catalog.get_creation_record(key)
    capture = catalog.get_fork_source_capture(copy_operation_id=key)
    assert capture is not None
    root = capture_directory(context.saver._storage.storage.sessions_dir, capture)
    captured_bytes = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    claims = [tuple(row) for row in production_fork_container.attachment_blob_store._catalog._connected().execute(
        "SELECT * FROM fork_attachment_claims WHERE copy_operation_id=?", (key,),
    )]
    for name, value in {
        "session_id": source.session_id, "main_thread_id": _main_thread_id(context, source.session_id),
        "workspace_id": "foreign-workspace", "purpose": "normal", "preimage_hash": "0" * 64,
        "request_intent_json": "{}", "storage_relative_locator": "2000/01/01/foreign",
        "created_at": "2000-01-01T00:00:00+00:00", "record_created_at": "2000-01-01T00:00:00+00:00",
    }.items():
        rebound = replace(frozen, **{name: value})
        with pytest.raises(ValueError, match="creation capability"):
            await context.forks._abort_if_unpublished(rebound, "错误 caller 不得清理")
        with pytest.raises(RuntimeError, match="owner 冲突"):
            catalog.abort_unpublished_fork_capture(capture, creation=rebound, reason="错误 caller 不得清理")
    assert catalog.get_creation_record(key) == frozen
    assert catalog.get_fork_source_capture(copy_operation_id=key) == capture
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()} == captured_bytes
    assert [tuple(row) for row in production_fork_container.attachment_blob_store._catalog._connected().execute(
        "SELECT * FROM fork_attachment_claims WHERE copy_operation_id=?", (key,),
    )] == claims


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("fault", [
    "point", "thread_uri", "thread_owner", "missing_source_uri", "missing_source_point",
    "missing_display_title", "malformed_source_uri", "bad_source_point",
    "bad_source_point_type", "bad_source_graph_binding", "bad_display_title", "bad_source_agent_id",
    "bad_goal_snapshot", "bad_goal_objective",
])
async def test_remote_source_intent_rejects_response_rebinding_before_creation(
    fork_provider_prepare, production_fork_container: AppContainer,
    remote_fork_target: tuple[AppContainer, SourceForkOwnerTransport],
    fork_gateway_request_context: None, monkeypatch: pytest.MonkeyPatch,
    creation_kind: SessionForkCreationKind, fault: str,
) -> None:
    source_context = _production_fork_context(production_fork_container)
    source = await _seed_source(source_context, fork_provider_prepare)
    selected = await _fork_request(
        source_context, source.session_id, creation_kind=creation_kind, turn_id="turn-2",
    )
    earlier = await _fork_request(
        source_context, source.session_id, creation_kind=creation_kind, turn_id="turn-1",
    )
    assert selected.anchor_item_id != earlier.anchor_item_id
    other_thread = None
    if fault in {"thread_uri", "thread_owner"}:
        other_thread = (await source_context.forks.fork(await _fork_request(
            source_context, source.session_id, creation_kind="new_child_thread", turn_id="turn-1",
        ))).thread_id
        assert other_thread != _main_thread_id(source_context, source.session_id)
    target, transport = remote_fork_target
    target_context = _production_fork_context(target)
    parent = await target.session_service.create(SessionCreateRequest(title="绑定拒绝目标"))
    control = target_context.forks._threads.for_owner_session(parent.session_id).control_store
    request = SessionForkRequest.model_validate({
        **selected.model_dump(mode="json"),
        "source_uri": thread_resource_uri(
            source.workspace_id, source.session_id, _main_thread_id(source_context, source.session_id),
            gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729",
        ),
        "target_container_uri": resource_display_uri(
            scope="workspace", scope_id=target.workspace_id, kind="session",
            tail_segments=(parent.session_id,) if creation_kind == "new_child_thread" else (),
            gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744",
        ),
    })
    original_transfer = transport.fork_transfer

    async def rebind_intent(payload: SessionForkTransferRequest) -> SessionForkTransferResultDTO:
        reply = await original_transfer(payload)
        if payload.action != "intent":
            return reply
        assert reply.intent is not None
        intent = dict(reply.intent)
        if fault == "point":
            alternate_request = SessionForkRequest.model_validate({
                **payload.request.model_dump(mode="json"),
                "source_view_id": earlier.source_view_id, "source_view_revision": earlier.source_view_revision,
                "anchor_item_id": earlier.anchor_item_id,
            })
            alternate = await original_transfer(fork_transfer.source_transfer_request(alternate_request, None, action="intent"))
            assert alternate.intent is not None
            assert alternate.intent["source_point"] != intent["source_point"]
            intent["source_point"] = alternate.intent["source_point"]
        elif fault in {"thread_uri", "thread_owner"}:
            assert other_thread is not None
            intent["source_thread_id"] = other_thread
            if fault == "thread_uri":
                intent["source_uri"] = thread_resource_uri(
                    source.workspace_id, source.session_id, other_thread, gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729",
                )
        elif fault.startswith("missing_"):
            del intent[fault.removeprefix("missing_")]
        elif fault == "malformed_source_uri":
            intent["source_uri"] = "boxteam://invalid"
        elif fault == "bad_source_point":
            intent["source_point"] = {"source_view_id": request.source_view_id}
        elif fault == "bad_source_point_type":
            intent["source_point"] = {**intent["source_point"], "source_checkpoint_id": []}
        elif fault == "bad_source_graph_binding":
            intent["source_graph_binding"] = {**intent["source_graph_binding"], "graph_revision": []}
        elif fault in {"bad_display_title", "bad_source_agent_id"}:
            intent[fault.removeprefix("bad_")] = []
        elif fault == "bad_goal_snapshot":
            intent["goal_snapshot"] = []
        else:
            intent["goal_snapshot"] = {"goal_id": "goal-invalid-objective", "objective": [], "token_budget": None}
        return SessionForkTransferResultDTO.model_validate({"intent": intent})

    monkeypatch.setattr(transport, "fork_transfer", rebind_intent)
    key = target_context.forks._creation_key(request.idempotency_key)
    source_before, target_before = _fork_intent_state(source_context), _fork_intent_state(target_context)
    control_path = target_context.sessions.path_resolver.resolve_session_node(parent.session_id) / "session-control.sqlite"
    control_before = _session_control_state(control_path)
    error = {
        "point": "来源 anchor 与固定请求不一致",
        "thread_uri": "来源 URI 与固定请求不一致",
        "thread_owner": "Session/Thread 与来源 URI 不一致",
    }.get(fault, "fork_response_protocol_invalid")
    gateway_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
    request_token = set_current_request_id("response-binding-intent")
    try:
        with pytest.raises(RuntimeError, match=error) as caught:
            if creation_kind == "new_session_main":
                await target_context.forks.fork(request)
            else:
                await target_context.forks.transfer(SessionForkTransferRequest(
                    source_gateway_id="gateway_2d2ab24c1f08e28f5f0217140c8be744", action="create", request=request,
                    fork_target=ForkGrantTarget(
                        gateway_id="gateway_2d2ab24c1f08e28f5f0217140c8be744", uri=request.target_container_uri, fork_role="target",
                    ),
                ))
        protocol_causes = {
            "missing_source_uri": KeyError, "missing_source_point": KeyError,
            "missing_display_title": KeyError,
            "malformed_source_uri": ValueError, "bad_source_point": TypeError,
            "bad_source_point_type": ValueError, "bad_source_graph_binding": TypeError,
            "bad_display_title": ValueError, "bad_source_agent_id": ValueError,
            "bad_goal_snapshot": TypeError, "bad_goal_objective": ValueError,
        }
        if fault in protocol_causes:
            assert isinstance(caught.value.__cause__, protocol_causes[fault])
            assert not isinstance(caught.value, WorkspaceSessionContextAccessError)
        assert _fork_intent_state(source_context) == source_before
        assert _fork_intent_state(target_context) == target_before
        assert _session_control_state(control_path) == control_before
        assert [call.action for call in transport.calls] == ["intent"] * (2 if fault == "point" else 1)
        with pytest.raises(KeyError):
            target_context.sessions.path_resolver.catalog_store.get_creation_record(key)
        with pytest.raises(KeyError):
            control.get_thread_creation_record(key)
        for context in (source_context, target_context):
            assert context.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key) is None
        assert not await target.job_service.list()
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)


@pytest.mark.asyncio
async def test_remote_source_preserves_default_thread_and_context_anchor(
    fork_provider_prepare, production_fork_container: AppContainer,
    remote_fork_target: tuple[AppContainer, SourceForkOwnerTransport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_context = _production_fork_context(production_fork_container)
    source = await _seed_source(source_context, fork_provider_prepare)
    source_main_thread_id = source_context.sessions.path_resolver.main_thread_id(source.session_id)
    selected = await _fork_request(
        source_context, source.session_id, creation_kind="new_session_main", mode="context_fork",
    )
    assert selected.anchor_item_id is not None
    target, transport = remote_fork_target
    request = SessionForkRequest.model_validate({
        **selected.model_dump(mode="json"), "anchor_item_id": None,
        "source_uri": resource_display_uri(
            scope="workspace", scope_id=source.workspace_id, kind="session", tail_segments=(source.session_id,),
            gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729",
        ),
        "target_container_uri": workspace_session_inventory_uri(target.workspace_id, gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744"),
    })
    intents: list[dict[str, object]] = []
    original_transfer = transport.fork_transfer

    async def observe_intent(payload: SessionForkTransferRequest) -> SessionForkTransferResultDTO:
        reply = await original_transfer(payload)
        if payload.action == "intent":
            assert reply.intent is not None
            intents.append(reply.intent)
        return reply

    monkeypatch.setattr(transport, "fork_transfer", observe_intent)
    gateway_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
    request_token = set_current_request_id("response-binding-defaults")
    try:
        result = await target.session_context_fork_service.fork(request)
        assert len(intents) == 2
        assert intents[1]["source_uri"] == intents[0]["source_uri"]
        assert intents[1]["source_point"] == intents[0]["source_point"]
        assert intents[1]["source_owner_instance_id"] == intents[0]["source_owner_instance_id"]
        assert intents[0]["source_thread_id"] == source_main_thread_id
        assert result.source_uri == intents[0]["source_uri"]
        assert result.source_uri == thread_resource_uri(
            source.workspace_id, source.session_id, source_main_thread_id, gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729",
        )
        _, result_source_session_id, result_source_thread_id = parse_session_thread_uri(result.source_uri)
        assert (result_source_session_id, result_source_thread_id) == (source.session_id, source_main_thread_id)
        point = intents[0]["source_point"]
        assert isinstance(point, dict)
        assert result.anchor_item_id == point["anchor_item_id"] == selected.anchor_item_id
        assert (result.source_view_id, result.source_view_revision) == (request.source_view_id, request.source_view_revision)
        assert result.export_release is not None
        assert result.export_release.state == "confirmed"
        assert not await target.job_service.list()
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("cancel_window", ["source_marker", "materialized", "before_publish"])
async def test_remote_fork_task_cancel_recovers_same_durable_owner(
    fork_provider_prepare,
    production_fork_container: AppContainer,
    remote_fork_target: tuple[AppContainer, SourceForkOwnerTransport],
    monkeypatch: pytest.MonkeyPatch,
    creation_kind: str,
    cancel_window: str,
) -> None:
    source_context = _production_fork_context(production_fork_container)
    source = await _seed_source(source_context, fork_provider_prepare)
    source_checkpoint = await _get_tuple(source_context, source.session_id)
    assert source_checkpoint is not None
    target, transport = remote_fork_target
    target_context = _production_fork_context(target)
    parent = await target.session_service.create(SessionCreateRequest(title="取消恢复目标"))
    source_point = await source_context.forks.source_point(session_id=source.session_id, mode="full_rollout_copy")
    source_thread_id = _main_thread_id(source_context, source.session_id)
    request = SessionForkRequest(
        idempotency_key=f"cancel-{creation_kind}-{cancel_window}",
        source_uri=resource_display_uri(
            scope="workspace", scope_id=source.workspace_id, kind="session",
            tail_segments=(source.session_id, "threads", source_thread_id), gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729",
        ),
        target_container_uri=resource_display_uri(
            scope="workspace", scope_id=target.session_service.workspace_id,
            kind="session",
            tail_segments=(parent.session_id,) if creation_kind == "new_child_thread" else (),
            gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744",
        ),
        creation_kind=creation_kind, mode="full_rollout_copy", source_view_id=source_point.source_view_id,
        source_view_revision=source_point.source_view_revision,
    )
    key = target_context.forks._creation_key(request.idempotency_key)
    reached = asyncio.Event()
    released = asyncio.Event()
    materializations = 0
    captures = 0
    source_capture = production_fork_container.checkpointer.forks.source._capture_fork_source_snapshot
    target_copy = target.checkpointer.forks.copy._afork_from_snapshot

    async def capture(**kwargs: object):
        nonlocal captures
        captures += 1
        result = await source_capture(**kwargs)
        if cancel_window == "source_marker":
            reached.set()
            await released.wait()
        return result

    async def copy(**kwargs: object):
        nonlocal materializations
        materializations += 1
        return await target_copy(**kwargs)

    monkeypatch.setattr(production_fork_container.checkpointer.forks.source, "_capture_fork_source_snapshot", capture)
    monkeypatch.setattr(target.checkpointer.forks.copy, "_afork_from_snapshot", copy)
    if cancel_window == "materialized":
        materialize = target_context.forks._materialize

        async def pause_materialized(*args, **kwargs):
            result = await materialize(*args, **kwargs)
            reached.set()
            await released.wait()
            return result

        monkeypatch.setattr(target_context.forks, "_materialize", pause_materialized)
    elif cancel_window == "before_publish":
        publisher = (
            target_context.forks if creation_kind == "new_session_main"
            else target_context.forks._threads.for_owner_session(parent.session_id)
        )
        publish_name = "_publish_prepared_fork" if creation_kind == "new_session_main" else "_publish_in_gate"
        publish = getattr(publisher, publish_name)

        async def pause_publication(*args, **kwargs):
            reached.set()
            await released.wait()
            return await publish(*args, **kwargs)

        monkeypatch.setattr(publisher, publish_name, pause_publication)

    gateway_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
    request_token = set_current_request_id("cancel-frozen-delivery")
    operation = asyncio.create_task(target_context.forks.fork(request))
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        operation.cancel("模拟服务 task 在 frozen journal 边界中断")
        with pytest.raises(asyncio.CancelledError, match="模拟服务 task"):
            await asyncio.wait_for(operation, timeout=10)
        catalog = target_context.sessions.path_resolver.catalog_store
        frozen = (
            catalog.get_creation_record(key) if creation_kind == "new_session_main"
            else target_context.forks._threads.for_owner_session(parent.session_id).control_store.get_thread_creation_record(key)
        )
        target_capture = catalog.get_fork_source_capture(copy_operation_id=key)
        source_export = source_context.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
        assert frozen.state == "preparing" and frozen.abort_reason is None
        assert frozen.fork_delivery_request_id == "cancel-frozen-delivery"
        assert target_capture is not None and source_export is not None
        assert target_capture.state == ("preparing" if cancel_window == "source_marker" else "materialized")
        assert source_export.state == ("preparing" if cancel_window == "source_marker" else "source_captured")
        assert catalog.get_fork_export_release(key) == ("pending", None)
        await target_context.forks.retry_pending_remote_exports()
        assert not [call for call in transport.calls if call.action == "release"]
        assert captures == 1
        assert materializations == (0 if cancel_window == "source_marker" else 1)
        read_captured_snapshot(source_context.saver._storage.storage.sessions_dir, source_export)
        released.set()

        async def forbid_recapture(**_kwargs: object):
            pytest.fail("恢复不得再次复制 live 来源")

        monkeypatch.setattr(production_fork_container.checkpointer.forks.source, "_capture_fork_source_snapshot", forbid_recapture)
        target.workspace_activity_service.store.close()
        target.session_catalog.close()
        await target.workspace_session_context_transport.aclose()
        restarted = _build_owned_container(target_context.workspace, monkeypatch)
        recovered_context = _production_fork_context(restarted)
        monkeypatch.setattr(recovered_context.forks, "_transport", transport)
        cold_copy = restarted.checkpointer.forks.copy._afork_from_snapshot

        async def count_recovered_copy(**kwargs: object):
            nonlocal materializations
            materializations += 1
            return await cold_copy(**kwargs)

        monkeypatch.setattr(restarted.checkpointer.forks.copy, "_afork_from_snapshot", count_recovered_copy)
        monkeypatch.setattr(restarted.job_service, "_start_job_prepared", AsyncMock(side_effect=AssertionError("fork恢复不得启动执行")))
        try:
            cold_gateway_token = set_current_gateway_id(None)
            cold_request_token = set_current_request_id(None)
            try:
                assert await recovered_context.forks.recover_prepared_forks() == 1
                await recovered_context.forks.retry_pending_remote_exports()
                assert get_current_gateway_id() is None and get_current_request_id() is None
            finally:
                reset_current_request_id(cold_request_token)
                reset_current_gateway_id(cold_gateway_token)
            replay = await recovered_context.forks.fork(request)
            assert replay.export_release.state == "confirmed" and replay.export_release.error is None
            assert materializations == 1 and captures == 1
            expected_thread = frozen.main_thread_id if creation_kind == "new_session_main" else frozen.child_thread_id
            assert replay.thread_id == expected_thread
            assert replay.session.session_id == (frozen.session_id if creation_kind == "new_session_main" else parent.session_id)
            assert [(call.action, call.release_state) for call in transport.calls if call.action == "release"] == [("release", "committed")]
            recovered_catalog = recovered_context.sessions.path_resolver.catalog_store
            published = (
                recovered_catalog.get_creation_record(key) if creation_kind == "new_session_main"
                else recovered_context.forks._threads.for_owner_session(parent.session_id).control_store.get_thread_creation_record(key)
            )
            assert published.state == "published"
            assert (published.preimage_hash, published.request_intent_json, published.fork_delivery_request_id) == (
                frozen.preimage_hash, frozen.request_intent_json, frozen.fork_delivery_request_id,
            )
            assert recovered_catalog.get_fork_export_release(key) == ("confirmed", None)
            finalized_capture = recovered_catalog.get_fork_source_capture(copy_operation_id=key)
            assert finalized_capture is not None
            assert (finalized_capture.fork_id, finalized_capture.materialization_id, finalized_capture.snapshot_id) == (
                target_capture.fork_id, target_capture.materialization_id, target_capture.snapshot_id,
            )
            confirmed = source_context.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
            assert confirmed is not None and confirmed.state == "materialized"
            assert not capture_directory(source_context.saver._storage.storage.sessions_dir, confirmed).exists()
            owner = recovered_context.saver.forks.source.resolve_thread_owner(replay.session.session_id, replay.thread_id)
            with session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id):
                restored = await recovered_context.saver.aget_tuple(build_checkpoint_config(owner.thread_id))
            assert restored is not None
            assert restored.checkpoint["channel_values"]["messages"] == source_checkpoint.checkpoint["channel_values"]["messages"]
            restarted.job_service._start_job_prepared.assert_not_awaited()
        finally:
            await restarted.workspace_session_context_transport.aclose()
            restarted.workspace_activity_service.store.close()
            restarted.session_catalog.close()
    finally:
        released.set()
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_remote_partial_import_recovers_by_exact_owner_abort(
    fork_provider_prepare, production_fork_container: AppContainer,
    remote_fork_target: tuple[AppContainer, SourceForkOwnerTransport],
    monkeypatch: pytest.MonkeyPatch, creation_kind: SessionForkCreationKind,
) -> None:
    source_context = _production_fork_context(production_fork_container)
    source = await _seed_source(source_context, fork_provider_prepare)
    source_node = _main_thread_node(source_context, source.session_id)
    source_jsonl = (source_node / "rollout/rollout.jsonl").read_bytes()
    target, transport = remote_fork_target
    target_context = _production_fork_context(target)
    parent = await target.session_service.create(SessionCreateRequest(title="partial import 的原 owner"))
    request = await _fork_request(
        source_context, source.session_id, creation_kind=creation_kind, mode="full_rollout_copy",
        target_container_uri=resource_display_uri(
            scope="workspace", scope_id=target.session_service.workspace_id,
            kind="session",
            tail_segments=(parent.session_id,) if creation_kind == "new_child_thread" else (),
            gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744",
        ),
    )
    request = request.model_copy(update={"source_uri": resource_display_uri(
        scope="workspace", scope_id=source.workspace_id, kind="session",
        tail_segments=(source.session_id, "threads", _main_thread_id(source_context, source.session_id)),
        gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729",
    )})
    key = target_context.forks._creation_key(request.idempotency_key)
    received_files = []
    receive = fork_transfer.receive_transfer_file

    class ImportProcessExit(BaseException):
        pass

    async def receive_then_exit(*args, **kwargs):
        await receive(*args, **kwargs)
        assert kwargs["file_name"] != "captured.json"
        assert kwargs["path"].is_file()
        received_files.append(kwargs["file_name"])
        raise ImportProcessExit("模拟真实文件接收后进程退出")

    def exit_before_cleanup(sessions_root: Path, record) -> None:
        # kill 无法运行 import 的清理；保留已接收文件，不能把普通 task cancel 当硬退出。
        root = capture_directory(sessions_root, record)
        assert root.is_dir() and received_files and not (root / "captured.json").exists()
        raise ImportProcessExit("模拟进程无法执行 partial import 清理")

    gateway_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
    request_token = set_current_request_id("partial-import-frozen-delivery")
    try:
        with monkeypatch.context() as crash:
            crash.setattr(fork_transfer, "receive_transfer_file", receive_then_exit)
            crash.setattr(fork_transfer, "remove_captured_snapshot", exit_before_cleanup)
            with pytest.raises(ImportProcessExit, match="无法执行 partial import 清理"):
                await target_context.forks.fork(request)
        catalog = target_context.sessions.path_resolver.catalog_store
        frozen = (catalog.get_creation_record(key) if creation_kind == "new_session_main" else
                  target_context.forks._threads.for_owner_session(parent.session_id).control_store.get_thread_creation_record(key))
        capture = catalog.get_fork_source_capture(copy_operation_id=key)
        source_export = source_context.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
        assert capture is not None and source_export is not None
        assert frozen.state == capture.state == "preparing"
        assert capture.snapshot_sha256 is None and capture.marker_sha256 is None
        assert source_export.state == "source_captured"
        assert source_export.snapshot_sha256 is not None and source_export.marker_sha256 is not None
        target_root = capture_directory(target_context.saver._storage.storage.sessions_dir, capture)
        assert target_root.is_dir() and received_files and not (target_root / "captured.json").exists()
        with pytest.raises(IncompleteForkSourceCaptureError):
            read_captured_snapshot(target_context.saver._storage.storage.sessions_dir, capture)
        assert catalog.get_fork_export_release(key) == ("pending", None)
        assert not [call for call in transport.calls if call.action == "release"]
        assert (await target_context.sessions.list(limit=100)).total == 1
        assert (source_node / "rollout/rollout.jsonl").read_bytes() == source_jsonl
        target.workspace_activity_service.store.close()
        target.session_catalog.close()
        await target.workspace_session_context_transport.aclose()
        restarted = _build_owned_container(target_context.workspace, monkeypatch)
        recovered = _production_fork_context(restarted)
        monkeypatch.setattr(recovered.forks, "_transport", transport)

        async def forbid_recopy(**_kwargs):
            pytest.fail("partial import 恢复只能中止原 owner，不能重传或物化")

        monkeypatch.setattr(source_context.saver.forks.source, "_capture_fork_source_snapshot", forbid_recopy)
        monkeypatch.setattr(recovered.saver.forks.copy, "_afork_from_snapshot", forbid_recopy)
        try:
            cold_gateway_token = set_current_gateway_id(None)
            cold_request_token = set_current_request_id(None)
            try:
                assert await recovered.forks.recover_prepared_forks() == 0
                assert get_current_gateway_id() is None and get_current_request_id() is None
            finally:
                reset_current_request_id(cold_request_token)
                reset_current_gateway_id(cold_gateway_token)
            cold_catalog = recovered.sessions.path_resolver.catalog_store
            aborted = (cold_catalog.get_creation_record(key) if creation_kind == "new_session_main" else
                       recovered.forks._threads.for_owner_session(parent.session_id).control_store.get_thread_creation_record(key))
            aborted_capture = cold_catalog.get_fork_source_capture(copy_operation_id=key)
            assert aborted.state == "aborted" and aborted.preimage_hash == frozen.preimage_hash
            assert aborted_capture is not None and aborted_capture.state == "aborted"
            assert (aborted_capture.preimage_hash, aborted_capture.snapshot_id) == (capture.preimage_hash, capture.snapshot_id)
            assert aborted_capture.snapshot_sha256 is None and aborted_capture.marker_sha256 is None
            assert not target_root.exists() and cold_catalog.get_fork_export_release(key) == ("confirmed", None)
            released = source_context.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
            assert released is not None and released.state == "aborted"
            assert (released.snapshot_sha256, released.marker_sha256) == (source_export.snapshot_sha256, source_export.marker_sha256)
            assert not capture_directory(source_context.saver._storage.storage.sessions_dir, released).exists()
            for drift in (
                replace(released, source_thread_id=_main_thread_id(recovered, parent.session_id)),
                replace(released, state="materialized"),
            ):
                with pytest.raises(RuntimeError, match="冻结 identity 不一致|目标终态不一致"):
                    cold_catalog.record_fork_export_release(aborted_capture, receipt=drift)
            with pytest.raises(RuntimeError, match="冻结 identity 不一致"):
                cold_catalog.record_fork_export_release(
                    replace(aborted_capture, marker_sha256=source_export.marker_sha256),
                    receipt=replace(released, marker_sha256="0" * 64),
                )
            assert cold_catalog.get_fork_export_release(key) == ("confirmed", None)
            assert [(call.action, call.release_state) for call in transport.calls if call.action == "release"] == [("release", "aborted")]
            if creation_kind == "new_child_thread":
                parent_node = recovered.sessions.path_resolver.resolve_session_node(parent.session_id)
                assert not (parent_node / frozen.final_relative_locator).exists()
                assert not (parent_node / frozen.staging_locator).exists()
                with sqlite3.connect(parent_node / "session-control.sqlite") as connection:
                    assert connection.execute("SELECT COUNT(*) FROM thread_catalog WHERE thread_id=?", (frozen.child_thread_id,)).fetchone() == (0,)
            assert (await recovered.sessions.list(limit=100)).total == 1
            with pytest.raises(RuntimeError, match="已中止|aborted"):
                await recovered.forks.fork(request)
            assert await recovered.forks.recover_prepared_forks() == 0
            assert len([call for call in transport.calls if call.action == "capture"]) == 1
            assert (source_node / "rollout/rollout.jsonl").read_bytes() == source_jsonl
        finally:
            await restarted.workspace_session_context_transport.aclose()
            restarted.workspace_activity_service.store.close()
            restarted.session_catalog.close()
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("creation_kind", "release_receipt_fault"),
    [
        ("new_session_main", "none"),
        ("new_child_thread", "none"),
        ("new_session_main", "identity"),
        ("new_session_main", "terminal_state"),
    ],
)
async def test_remote_fork_source_release_waits_for_durable_target_abort(
    fork_provider_prepare,
    production_fork_container: AppContainer,
    remote_fork_target: tuple[AppContainer, SourceForkOwnerTransport],
    monkeypatch: pytest.MonkeyPatch,
    creation_kind: str,
    release_receipt_fault: str,
) -> None:
    source_context = _production_fork_context(production_fork_container)
    source = await _seed_source(source_context, fork_provider_prepare)
    target, transport = remote_fork_target
    context = _production_fork_context(target)
    parent = await target.session_service.create(SessionCreateRequest(title="失败清理目标"))
    source_point = await source_context.forks.source_point(session_id=source.session_id, mode="full_rollout_copy")
    request = SessionForkRequest(
        idempotency_key=f"abort-{creation_kind}", creation_kind=creation_kind, mode="full_rollout_copy",
        source_uri=resource_display_uri(
            scope="workspace", scope_id=source.workspace_id, kind="session",
            tail_segments=(source.session_id, "threads", _main_thread_id(source_context, source.session_id)),
            gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729",
        ),
        target_container_uri=resource_display_uri(
            scope="workspace", scope_id=target.session_service.workspace_id,
            kind="session",
            tail_segments=(parent.session_id,) if creation_kind == "new_child_thread" else (),
            gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744",
        ),
        source_view_id=source_point.source_view_id, source_view_revision=source_point.source_view_revision,
    )
    key = context.forks._creation_key(request.idempotency_key)
    catalog = context.sessions.path_resolver.catalog_store
    failure = ValueError("确定性物化后合同失败")
    materialize = context.forks._materialize
    cleaning = asyncio.Event()
    finish_cleanup = asyncio.Event()

    async def fail_materialized(*args, **kwargs):
        await materialize(*args, **kwargs)
        raise failure

    monkeypatch.setattr(context.forks, "_materialize", fail_materialized)
    if creation_kind == "new_session_main":
        cleanup_owner = context.sessions
        cleanup_name = "abort_prepared_context_fork"
    else:
        cleanup_owner = target.checkpointer.forks
        cleanup_name = "abort_prepared_child_fork"
    cleanup = getattr(cleanup_owner, cleanup_name)

    async def pause_cleanup(*args, **kwargs):
        cleaning.set()
        await finish_cleanup.wait()
        return await cleanup(*args, **kwargs)

    monkeypatch.setattr(cleanup_owner, cleanup_name, pause_cleanup)
    original_transfer = transport.fork_transfer

    async def return_release_receipt(payload: SessionForkTransferRequest) -> SessionForkTransferResultDTO:
        reply = await original_transfer(payload)
        if payload.action != "release" or release_receipt_fault == "none":
            return reply
        assert reply.capture is not None
        if release_receipt_fault == "identity":
            capture_reply = reply.capture.model_copy(update={"copy_operation_id": "other-operation"})
        else:
            capture_reply = reply.capture.model_copy(update={"state": "source_captured"})
        return reply.model_copy(update={"capture": capture_reply})

    monkeypatch.setattr(transport, "fork_transfer", return_release_receipt)
    gateway_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
    request_token = set_current_request_id("abort-frozen-delivery")
    operation = asyncio.create_task(context.forks.fork(request))
    try:
        await asyncio.wait_for(cleaning.wait(), timeout=10)
        frozen = (
            catalog.get_creation_record(key) if creation_kind == "new_session_main"
            else context.forks._threads.for_owner_session(parent.session_id).control_store.get_thread_creation_record(key)
        )
        capture = catalog.get_fork_source_capture(copy_operation_id=key)
        assert frozen.state == "preparing" and capture is not None
        await context.forks.retry_pending_remote_exports()
        assert catalog.get_fork_export_release(key) == ("pending", None)
        assert not [call for call in transport.calls if call.action == "release"]
        exported = source_context.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
        assert exported is not None and exported.state == "source_captured"
        finish_cleanup.set()
        if release_receipt_fault == "none":
            with pytest.raises(ValueError, match="确定性物化后合同失败") as caught:
                await asyncio.wait_for(operation, timeout=10)
            assert caught.value is failure
        elif release_receipt_fault == "identity":
            with pytest.raises(RuntimeError, match="operation/preimage/snapshot identity 漂移"):
                await asyncio.wait_for(operation, timeout=10)
        else:
            with pytest.raises(RuntimeError, match="终结回执"):
                await asyncio.wait_for(operation, timeout=10)
        terminal = (
            catalog.get_creation_record(key) if creation_kind == "new_session_main"
            else context.forks._threads.for_owner_session(parent.session_id).control_store.get_thread_creation_record(key)
        )
        assert terminal.state == "aborted"
        aborted_capture = catalog.get_fork_source_capture(copy_operation_id=key)
        assert aborted_capture is not None and aborted_capture.state == "aborted"
        assert catalog.get_fork_export_release(key) == (
            ("confirmed", None) if release_receipt_fault == "none" else ("pending", None)
        )
        assert [(call.action, call.release_state) for call in transport.calls if call.action == "release"] == [("release", "aborted")]
        source_terminal = source_context.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key)
        assert source_terminal is not None and source_terminal.state == "aborted"
        assert not capture_directory(source_context.saver._storage.storage.sessions_dir, source_terminal).exists()
        assert not await target.job_service.list()
    finally:
        finish_cleanup.set()
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)


@pytest.mark.asyncio
async def test_lifespan_retries_pending_remote_export_without_restart_or_fork_replay(
    fork_provider_prepare,
    production_fork_container: AppContainer,
    remote_fork_target: tuple[AppContainer, SourceForkOwnerTransport],
    monkeypatch: pytest.MonkeyPatch,
    live_resource_transport_factory,
) -> None:
    source_context = _production_fork_context(production_fork_container)
    source = await _seed_source(source_context, fork_provider_prepare)
    live_resource_transport_factory(production_fork_container, "terminal")
    _, live_resource = await _create_fork_live_resource(production_fork_container, "terminal", source.session_id)
    target, transport = remote_fork_target
    target_context = _production_fork_context(target)
    source_point = await source_context.forks.source_point(
        session_id=source.session_id,
        mode="full_rollout_copy",
    )
    source_thread_id = _main_thread_id(source_context, source.session_id)
    request = SessionForkRequest(
        idempotency_key="lifespan-pending-remote-export",
        source_uri=resource_display_uri(
            scope="workspace",
            scope_id=source.workspace_id,
            kind="session",
            tail_segments=(source.session_id, "threads", source_thread_id),
            gateway_authority="gateway_4aae160f813ed87bcf65a8bb8bfd3729",
        ),
        target_container_uri=resource_display_uri(
            scope="workspace", scope_id=target.workspace_id, kind="session", tail_segments=(),
            gateway_authority="gateway_2d2ab24c1f08e28f5f0217140c8be744",
        ),
        creation_kind="new_session_main",
        mode="full_rollout_copy",
        source_view_id=source_point.source_view_id,
        source_view_revision=source_point.source_view_revision,
    )
    key = target_context.forks._creation_key(request.idempotency_key)

    class RecordingDeadline:
        _completed = False
        _timed_out = False
        target = "pending-export-regression"
        operation_key = request.idempotency_key

        async def wait(self, awaitable, _stage: str):
            return await awaitable

        def complete(self) -> None:
            self._completed = True

    deadline = RecordingDeadline()
    monkeypatch.setattr(ForkDeadlineRegistry, "for_operation", lambda _registry, **_kwargs: deadline)
    rejected_retry = request.model_copy(update={"resources": [_fork_live_choice(live_resource)]})
    original_transfer = transport.fork_transfer
    release_error = (
        "Gateway 上下文查询失败: workspace_id=None, method=POST, "
        "path=/api/gateway/federation/relay, status=409, detail={\"code\":\"source_owner_unavailable\"}"
    )
    release_attempts = 0
    third_release_attempt = asyncio.Event()
    source_recovered = asyncio.Event()
    release_completed = asyncio.Event()
    delivery_contexts: list[tuple[str | None, str | None]] = []

    async def fail_release_until_source_recovers(
        payload: SessionForkTransferRequest,
    ) -> SessionForkTransferResultDTO:
        nonlocal release_attempts
        if payload.action == "release":
            release_attempts += 1
            delivery_contexts.append((get_current_gateway_id(), get_current_request_id()))
            if release_attempts < 3:
                raise WorkspaceSessionContextAccessError(release_error)
            third_release_attempt.set()
            await source_recovered.wait()
            result = await original_transfer(payload)
            release_completed.set()
            return result
        return await original_transfer(payload)

    monkeypatch.setattr(transport, "fork_transfer", fail_release_until_source_recovers)
    _isolate_non_navigation_services(target, monkeypatch)
    worker_tasks: list[asyncio.Task[None]] = []
    original_worker = main._retry_pending_fork_exports_periodically

    async def capture_worker(container: AppContainer) -> None:
        task = asyncio.current_task()
        assert task is not None
        worker_tasks.append(task)
        await original_worker(container)

    monkeypatch.setattr(main, "_retry_pending_fork_exports_periodically", capture_worker)
    cold_gateway_token = set_current_gateway_id(None)
    cold_request_token = set_current_request_id(None)
    try:
        async with main._workspace_lifespan(target):
            assert get_current_gateway_id() is None and get_current_request_id() is None
            gateway_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
            request_token = set_current_request_id("frozen-export-delivery")
            try:
                result = await target_context.forks.fork(request)
            finally:
                reset_current_request_id(request_token)
                reset_current_gateway_id(gateway_token)

            assert result.export_release is not None
            assert result.export_release.state == "pending"
            assert result.export_release.error == f"WorkspaceSessionContextAccessError: {release_error}"

            target_catalog = target_context.sessions.path_resolver.catalog_store
            published = target_catalog.get_creation_record(key)
            assert published.state == "published"
            assert published.fork_delivery_request_id == "frozen-export-delivery"
            target_capture = target_catalog.get_fork_source_capture(copy_operation_id=key)
            source_capture = source_context.sessions.path_resolver.catalog_store.get_fork_source_capture(
                copy_operation_id=key,
            )
            assert target_capture is not None and target_capture.state == "materialized"
            assert source_capture is not None and source_capture.state == "source_captured"
            source_snapshot_dir = capture_directory(
                source_context.saver._storage.storage.sessions_dir,
                source_capture,
            )
            assert source_snapshot_dir.is_dir()

            await asyncio.wait_for(third_release_attempt.wait(), timeout=5)
            assert target_catalog.get_fork_export_release(key)[0] == "pending"
            assert target_catalog.get_creation_record(key).state == "published"
            gateway_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
            try:
                with pytest.raises(ValueError, match="fork_live_cross_workspace_unsupported:"):
                    await target_context.forks.fork(rejected_retry)
                assert not deadline._completed
            finally:
                reset_current_gateway_id(gateway_token)
            source_recovered.set()
            await asyncio.wait_for(release_completed.wait(), timeout=5)
            assert target_catalog.get_fork_export_release(key) == ("confirmed", None)
            gateway_token = set_current_gateway_id("gateway_2d2ab24c1f08e28f5f0217140c8be744")
            try:
                with pytest.raises(ValueError, match="fork_live_cross_workspace_unsupported:"):
                    await target_context.forks.fork(rejected_retry)
                assert deadline._completed
            finally:
                reset_current_gateway_id(gateway_token)
            finalized_source_capture = source_context.sessions.path_resolver.catalog_store.get_fork_source_capture(
                copy_operation_id=key,
            )
            assert finalized_source_capture is not None
            assert finalized_source_capture.state == "materialized"
            assert not source_snapshot_dir.exists()
            assert release_attempts == 3
            assert delivery_contexts == [
                ("gateway_2d2ab24c1f08e28f5f0217140c8be744", "frozen-export-delivery"),
                ("gateway_2d2ab24c1f08e28f5f0217140c8be744", "frozen-export-delivery"),
                ("gateway_2d2ab24c1f08e28f5f0217140c8be744", "frozen-export-delivery"),
            ]
            assert sum(call.action == "intent" for call in transport.calls) == 2
            assert sum(call.action == "capture" for call in transport.calls) == 1
            assert sum(call.action == "release" for call in transport.calls) == 1
            assert await target.job_service.list() == []
        assert len(worker_tasks) == 1 and worker_tasks[0].cancelled()
    finally:
        source_recovered.set()
        reset_current_request_id(cold_request_token)
        reset_current_gateway_id(cold_gateway_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_state", ["prepared", "target_committed", "committed"])
async def test_production_lifespan_recovers_unpublished_fork(
    fork_provider_prepare,
    production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch,
    crash_state: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context, fork_provider_prepare)
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="context_fork", relationship="pinned")
    operation_deadlines: list[ForkOperationDeadline] = []
    for_operation = ForkDeadlineRegistry.for_operation

    def record_deadline(registry: ForkDeadlineRegistry, *, target: str, operation_key: str):
        deadline = for_operation(registry, target=target, operation_key=operation_key)
        operation_deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(ForkDeadlineRegistry, "for_operation", record_deadline)
    storage = context.saver._storage
    method = "materialize_fork_checkpoint" if crash_state == "prepared" else "complete_fork_materialization"

    class OwnerProcessExit(BaseException):
        pass

    def crash(**_kwargs: object) -> None:
        raise OwnerProcessExit("模拟进程在 fork journal 边界中断")

    async def crash_before_publish(*_args: object) -> None:
        raise OwnerProcessExit("模拟进程在 fork journal 边界中断")

    # 走公开入口冻结经过 Gateway 规范化的真实创建意图，异常不执行进程内补偿。
    with monkeypatch.context() as failure:
        if crash_state == "committed":
            failure.setattr(context.forks, "_publish_prepared_fork", crash_before_publish)
        else:
            failure.setattr(storage.forks, method, crash)
        with pytest.raises(OwnerProcessExit, match="模拟进程"):
            await context.forks.fork(request)
    record = context.sessions.path_resolver.catalog_store.get_creation_record(
        context.forks._creation_key(request.idempotency_key)
    )
    assert record.state == "preparing"
    if crash_state == "target_committed":
        capture = storage.forks._catalog_store().get_fork_source_capture(
            creation_key=record.session_creation_idempotency_key,
        )
        assert capture is not None
        snapshot, marker_hash = read_captured_snapshot(storage.storage.sessions_dir, capture)
        assert marker_hash == capture.marker_sha256
        # 物化已消费原 frozen bytes；只读 finalization 查询不能切换 SQLite journal。
        with (
            storage.storage.fork_source_snapshot_scope(
                session_id=capture.source_session_id, thread_id=capture.source_thread_id,
                captured_root=snapshot.captured_root,
            ),
            pytest.raises(RuntimeError, match="禁止可写 SQLite"),
        ):
            storage.storage._connect(capture.source_thread_id, session_id=capture.source_session_id)
    # 新容器从同一 durable record 装配，实际 main lifespan 必须在 ready 前收敛。
    production_fork_container.workspace_activity_service.store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    async with main.lifespan(main.app):
        durable = restarted.session_service.path_resolver.catalog_store.get_creation_record(
            record.session_creation_idempotency_key
        )
        capture = restarted.session_service.path_resolver.catalog_store.get_fork_source_capture(creation_key=record.session_creation_idempotency_key)
        assert capture is not None
        claim = restarted.session_service.path_resolver.catalog_store.get_fork_retention_claim(capture.fork_id)
        assert durable.state == "published"
        assert claim.state == "active"
        assert (await restarted.session_service.get(record.session_id)).context_source_session_id == source.session_id
        restored = await _get_tuple(_production_fork_context(restarted), record.session_id)
        assert restored is not None
        assert [message.content for message in restored.checkpoint["channel_values"]["messages"]] == ["问题一", "回答一"]
    assert len(operation_deadlines) == 2
    assert operation_deadlines[0] is operation_deadlines[1]
    assert operation_deadlines[0]._completed
    assert operation_deadlines[0].deadline - operation_deadlines[0].started_at == 60.0


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_qualified_local_fork_lifespan_recovers_without_http_context(
    fork_provider_prepare, production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, creation_kind: SessionForkCreationKind,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context, fork_provider_prepare)
    gateway_token = set_current_gateway_id("gateway_bc453186347b952303da1c0634d46150")
    request_token = set_current_request_id("qualified-local-fork-preparation")
    try:
        request = await _fork_request(
            context, source.session_id, creation_kind=creation_kind, mode="context_fork",
            target_container_uri=(
                workspace_session_inventory_uri(
                    source.workspace_id, gateway_authority="gateway_bc453186347b952303da1c0634d46150",
                )
                if creation_kind == "new_session_main"
                else resource_display_uri(
                    scope="workspace", scope_id=source.workspace_id, kind="session",
                    tail_segments=(source.session_id,), gateway_authority="gateway_bc453186347b952303da1c0634d46150",
                )
            ),
        )

        async def interrupt_before_capture(*_args: object, **_kwargs: object) -> None:
            raise asyncio.CancelledError("模拟本地 qualified fork 准备后进程退出")

        monkeypatch.setattr(context.forks, "_materialize", interrupt_before_capture)
        with pytest.raises(asyncio.CancelledError, match="准备后进程退出"):
            await context.forks.fork(request)
        key = context.forks._creation_key(request.idempotency_key)
        frozen = (
            context.sessions.path_resolver.catalog_store.get_creation_record(key)
            if creation_kind == "new_session_main" else
            context.forks._threads.for_owner_session(source.session_id).control_store.get_thread_creation_record(key)
        )
        intent = json.loads(frozen.request_intent_json)
        assert frozen.state == "preparing" and frozen.fork_delivery_request_id is None
        assert intent["source_remote"] is False
        assert intent["target_gateway_id"] == "gateway_bc453186347b952303da1c0634d46150"
        qualified_request = request.model_copy(update={
            "source_uri": thread_resource_uri(
                source.workspace_id, source.session_id, _main_thread_id(context, source.session_id),
                gateway_authority="gateway_bc453186347b952303da1c0634d46150",
            ),
        })
        assert intent["request"] == qualified_request.model_dump(mode="json")
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)

    production_fork_container.workspace_activity_service.store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    recovered = _production_fork_context(restarted)
    materializations = 0
    copy = restarted.checkpointer.forks.copy._afork_from_snapshot

    async def count_materialization(**kwargs: object):
        nonlocal materializations
        materializations += 1
        return await copy(**kwargs)

    monkeypatch.setattr(restarted.checkpointer.forks.copy, "_afork_from_snapshot", count_materialization)
    cold_gateway_token = set_current_gateway_id(None)
    cold_request_token = set_current_request_id(None)
    try:
        async with main.lifespan(main.app):
            assert get_current_gateway_id() is None and get_current_request_id() is None
            durable = (
                recovered.sessions.path_resolver.catalog_store.get_creation_record(key)
                if creation_kind == "new_session_main" else
                recovered.forks._threads.for_owner_session(source.session_id).control_store.get_thread_creation_record(key)
            )
            assert durable.state == "published"
            assert (durable.preimage_hash, durable.request_intent_json, durable.fork_delivery_request_id) == (
                frozen.preimage_hash, frozen.request_intent_json, None,
            )
            session_id = frozen.session_id if creation_kind == "new_session_main" else source.session_id
            thread_id = frozen.main_thread_id if creation_kind == "new_session_main" else frozen.child_thread_id
            result = await recovered.forks._result(
                intent, await recovered.sessions.get(session_id), thread_id, key, creation=durable,
            )
            assert result.target_gateway_id == "gateway_bc453186347b952303da1c0634d46150"
            assert result.thread_uri == resource_display_uri(
                scope="workspace", scope_id=source.workspace_id, kind="session",
                tail_segments=(session_id, "threads", thread_id), gateway_authority="gateway_bc453186347b952303da1c0634d46150",
            )
            replay_token = set_current_gateway_id("gateway_bc453186347b952303da1c0634d46150")
            try:
                alias_request = qualified_request.model_copy(update={
                    "target_container_uri": (
                        workspace_session_inventory_uri(source.workspace_id)
                        if creation_kind == "new_session_main" else
                        resource_display_uri(
                            scope="workspace", scope_id=source.workspace_id, kind="session",
                            tail_segments=(source.session_id,),
                        )
                    ),
                })
                replay = await recovered.forks.fork(alias_request)
                assert replay == result
                replay_record = (
                    recovered.sessions.path_resolver.catalog_store.get_creation_record(key)
                    if creation_kind == "new_session_main" else
                    recovered.forks._threads.for_owner_session(source.session_id).control_store.get_thread_creation_record(key)
                )
                assert (replay_record.preimage_hash, replay_record.request_intent_json) == (
                    frozen.preimage_hash, frozen.request_intent_json,
                )
            finally:
                reset_current_gateway_id(replay_token)
            assert await recovered.forks.recover_prepared_forks() == 0
            assert get_current_gateway_id() is None and get_current_request_id() is None
            assert materializations == 1
            assert (await recovered.sessions.list(limit=100)).total == (2 if creation_kind == "new_session_main" else 1)
            if creation_kind == "new_child_thread":
                assert len(recovered.forks._threads.for_owner_session(source.session_id).control_store.list_fork_thread_creations()) == 1
            with recovered.saver._storage.storage.read_connection(session_id, thread_id, "") as connection:
                assert connection.execute("SELECT COUNT(*) FROM fork_materializations WHERE status='committed'").fetchone() == (1,)
            owner = recovered.saver.forks.source.resolve_thread_owner(session_id, thread_id)
            with session_thread_owner_scope(owner, langgraph_thread_id=thread_id):
                restored = await recovered.saver.aget_tuple(build_checkpoint_config(thread_id))
            assert restored is not None
            assert [message.content for message in restored.checkpoint["channel_values"]["messages"]] == ["问题一", "回答一"]
    finally:
        reset_current_request_id(cold_request_token)
        reset_current_gateway_id(cold_gateway_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("partial_phase", ["copy.source.partial", "copy.attachments.preparing"])
async def test_local_partial_capture_abort_does_not_block_complete_marker_recovery(
    fork_provider_prepare, production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, creation_kind: SessionForkCreationKind, partial_phase: str,
    fork_gateway_request_context: None,
) -> None:
    context = _production_fork_context(production_fork_container)
    store = production_fork_container.attachment_blob_store
    source, _ = await _seed_retry_tool_source(context, fork_provider_prepare, attachment_store=store)
    source_thread = _main_thread_id(context, source.session_id)
    source_jsonl = (_main_thread_node(context, source.session_id) / "rollout/rollout.jsonl").read_bytes()
    parent = (await context.sessions.create(SessionCreateRequest(title="不完整 child fork 的 owner"))
              if creation_kind == "new_child_thread" else source)
    target_uri = (resource_display_uri(
        scope="workspace", scope_id=source.workspace_id, kind="session", tail_segments=(parent.session_id,),
    ) if creation_kind == "new_child_thread" else None)
    partial_request = await _fork_request(
        context, source.session_id, creation_kind=creation_kind, mode="context_fork",
        relationship="pinned", target_container_uri=target_uri,
    )
    complete_request = await _fork_request(
        context, source.session_id, creation_kind=creation_kind, mode="context_fork", target_container_uri=target_uri,
    )
    partial_key = context.forks._creation_key(partial_request.idempotency_key)
    complete_key = context.forks._creation_key(complete_request.idempotency_key)
    pauses = {partial_key: partial_phase, complete_key: "copy.source.before_captured_cas"}
    reached = asyncio.Event()
    release = asyncio.Event()
    source_observer, owner_observer = fork_source.observe_phase, fork_owner.observe_phase
    captures = 0
    capture_source = context.saver.forks.source._capture_fork_source_snapshot

    async def count_capture(**kwargs):
        nonlocal captures
        captures += 1
        return await capture_source(**kwargs)

    async def source_phase(operation_id: str, phase: str, **kwargs):
        await source_observer(operation_id, phase, **kwargs)
        if pauses.get(operation_id) == phase:
            reached.set()
            await release.wait()

    async def owner_phase(operation_id: str, phase: str, **kwargs):
        await owner_observer(operation_id, phase, **kwargs)
        if pauses.get(operation_id) == phase:
            reached.set()
            await release.wait()

    monkeypatch.setattr(context.saver.forks.source, "_capture_fork_source_snapshot", count_capture)
    monkeypatch.setattr(fork_source, "observe_phase", source_phase)
    monkeypatch.setattr(fork_owner, "observe_phase", owner_phase)
    for request in (partial_request, complete_request):
        reached.clear()
        operation = asyncio.create_task(context.forks.fork(request))
        try:
            await asyncio.wait_for(reached.wait(), timeout=10)
            operation.cancel("模拟 source capture 边界的进程退出")
            with pytest.raises(asyncio.CancelledError, match="source capture 边界"):
                await asyncio.wait_for(operation, timeout=10)
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
    monkeypatch.setattr(fork_source, "observe_phase", source_observer)
    monkeypatch.setattr(fork_owner, "observe_phase", owner_observer)
    catalog = context.sessions.path_resolver.catalog_store
    frozen = (catalog.get_creation_record(partial_key) if creation_kind == "new_session_main" else
              context.forks._threads.for_owner_session(parent.session_id).control_store.get_thread_creation_record(partial_key))
    complete = (catalog.get_creation_record(complete_key) if creation_kind == "new_session_main" else
                context.forks._threads.for_owner_session(parent.session_id).control_store.get_thread_creation_record(complete_key))
    partial_capture = catalog.get_fork_source_capture(copy_operation_id=partial_key)
    complete_capture = catalog.get_fork_source_capture(copy_operation_id=complete_key)
    assert partial_capture is not None and complete_capture is not None
    assert frozen.state == complete.state == partial_capture.state == complete_capture.state == "preparing"
    assert partial_capture.snapshot_sha256 is None and partial_capture.marker_sha256 is None
    partial_root = capture_directory(context.saver._storage.storage.sessions_dir, partial_capture)
    assert partial_root.is_dir() and not partial_root.is_symlink() and not (partial_root / "captured.json").exists()
    with pytest.raises(IncompleteForkSourceCaptureError):
        read_captured_snapshot(context.saver._storage.storage.sessions_dir, partial_capture)
    complete_snapshot, complete_marker_hash = read_captured_snapshot(context.saver._storage.storage.sessions_dir, complete_capture)
    assert complete_capture.snapshot_sha256 is None and complete_capture.marker_sha256 is None
    claims = store._catalog._connected().execute(
        "SELECT claim_id,state FROM fork_attachment_claims WHERE copy_operation_id=? ORDER BY claim_id", (partial_key,),
    ).fetchall()
    assert [row[1] for row in claims] == (["preparing"] if partial_phase == "copy.attachments.preparing" else [])
    assert catalog.get_fork_retention_claim(partial_capture.fork_id) is not None
    partial_target = (context.sessions.path_resolver.resolve_prepared_session_node(frozen)
                      if creation_kind == "new_session_main" else
                      context.sessions.path_resolver.resolve_session_node(parent.session_id) / frozen.final_relative_locator)
    baseline_sessions = (await context.sessions.list(limit=100)).total
    assert captures == 2
    assert (_main_thread_node(context, source.session_id) / "rollout/rollout.jsonl").read_bytes() == source_jsonl

    production_fork_container.workspace_activity_service.store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    recovered = _production_fork_context(restarted)
    recovery_counts = []
    recovery = recovered.forks.recover_prepared_forks
    materializations = 0
    copy = recovered.saver.forks.copy._afork_from_snapshot

    async def forbid_recapture(**_kwargs):
        pytest.fail("partial 中止或完整 marker 恢复不得再次捕获 live source")

    async def count_recovery():
        count = await recovery()
        recovery_counts.append(count)
        return count

    async def count_materialization(**kwargs):
        nonlocal materializations
        materializations += 1
        return await copy(**kwargs)

    monkeypatch.setattr(recovered.saver.forks.source, "_capture_fork_source_snapshot", forbid_recapture)
    monkeypatch.setattr(recovered.forks, "recover_prepared_forks", count_recovery)
    monkeypatch.setattr(recovered.saver.forks.copy, "_afork_from_snapshot", count_materialization)
    async with main.lifespan(main.app):
        cold_catalog = recovered.sessions.path_resolver.catalog_store
        aborted = (cold_catalog.get_creation_record(partial_key) if creation_kind == "new_session_main" else
                   recovered.forks._threads.for_owner_session(parent.session_id).control_store.get_thread_creation_record(partial_key))
        published = (cold_catalog.get_creation_record(complete_key) if creation_kind == "new_session_main" else
                     recovered.forks._threads.for_owner_session(parent.session_id).control_store.get_thread_creation_record(complete_key))
        aborted_capture = cold_catalog.get_fork_source_capture(copy_operation_id=partial_key)
        materialized_capture = cold_catalog.get_fork_source_capture(copy_operation_id=complete_key)
        assert aborted.state == "aborted" and aborted.preimage_hash == frozen.preimage_hash
        assert aborted_capture is not None and aborted_capture.state == "aborted"
        assert (aborted_capture.snapshot_id, aborted_capture.preimage_hash) == (partial_capture.snapshot_id, partial_capture.preimage_hash)
        assert not partial_root.exists() and not partial_target.exists()
        assert cold_catalog.get_fork_retention_claim(partial_capture.fork_id).state == "released"
        assert [tuple(row) for row in restarted.attachment_blob_store._catalog._connected().execute(
            "SELECT claim_id,state FROM fork_attachment_claims WHERE copy_operation_id=? ORDER BY claim_id", (partial_key,),
        )] == [(row[0], "aborted") for row in claims]
        assert published.state == "published" and published.preimage_hash == complete.preimage_hash
        assert materialized_capture is not None and materialized_capture.state == "materialized"
        assert (materialized_capture.snapshot_id, materialized_capture.snapshot_sha256, materialized_capture.marker_sha256) == (
            complete_capture.snapshot_id, complete_snapshot.snapshot_sha256, complete_marker_hash,
        )
        assert recovery_counts == [1] and materializations == 1
        assert (await recovered.sessions.list(limit=100)).total == baseline_sessions + (creation_kind == "new_session_main")
        if creation_kind == "new_child_thread":
            with sqlite3.connect(recovered.sessions.path_resolver.resolve_session_node(parent.session_id) / "session-control.sqlite") as connection:
                assert connection.execute("SELECT COUNT(*) FROM thread_catalog WHERE thread_id=?", (frozen.child_thread_id,)).fetchone() == (0,)
        with pytest.raises(RuntimeError, match="已中止|aborted"):
            await recovered.forks.fork(partial_request)
        assert await recovered.forks.recover_prepared_forks() == 0
        assert recovery_counts == [1, 0] and materializations == 1 and captures == 2
        assert (_main_thread_node(recovered, source.session_id) / "rollout/rollout.jsonl").read_bytes() == source_jsonl
        assert recovered.saver.forks.source.resolve_main_thread_owner(source.session_id).thread_id == source_thread


@pytest.mark.skipif(os.name != "posix", reason="真实 source copy worker 的取消与共享锁生命周期")
async def test_cancelled_source_copy_holds_read_lease_until_worker_finishes(
    fork_provider_prepare, production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, fork_gateway_request_context: None,
) -> None:
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(
        context, fork_provider_prepare, attachment_store=production_fork_container.attachment_blob_store,
    )
    thread_id = _main_thread_id(context, source.session_id)
    storage = context.saver._storage.storage
    source_root = storage.root(source.session_id, thread_id=thread_id)
    original_jsonl = (source_root / "rollout.jsonl").read_bytes()
    request = await _fork_request(
        context, source.session_id, creation_kind="new_session_main", mode="context_fork",
    )
    operation_key = context.forks._creation_key(request.idempotency_key)
    started = asyncio.Event()
    release_worker, worker_finished = threading.Event(), threading.Event()
    writer_ready, release_writer = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original_ignore = source_snapshot._copy_ignore
    original_capture = fork_source.capture_fork_rollout_snapshot

    def pause_live_copy(directory: str, names: list[str]) -> set[str]:
        if Path(directory) == source_root:
            loop.call_soon_threadsafe(started.set)
            if not release_worker.wait(timeout=10):
                raise TimeoutError("测试未及时释放真实 source copy worker")
        return original_ignore(directory, names)

    def record_capture(*args, **kwargs):
        try:
            return original_capture(*args, **kwargs)
        finally:
            worker_finished.set()

    def write() -> None:
        with storage._lock(thread_id, "", session_id=source.session_id):
            writer_ready.set()
            assert worker_finished.is_set(), "worker 仍在读取 live source 时 writer 不得获准"
            assert release_writer.wait(timeout=10)

    monkeypatch.setattr(source_snapshot, "_copy_ignore", pause_live_copy)
    monkeypatch.setattr(fork_source, "capture_fork_rollout_snapshot", record_capture)
    operation = asyncio.create_task(context.forks.fork(request))
    probe = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = None
        try:
            await asyncio.wait_for(started.wait(), timeout=10)
            assert not worker_finished.is_set()
            probe = storage.read_lock(source.session_id, thread_id)
            await probe.acquire_async()
            admission = probe._admission
            assert admission is not None
            probe_owner = (threading.get_ident(), asyncio.current_task())
            with admission._condition:
                assert probe_owner in admission._holders
                source_holds = [held for owner, held in admission._holders.items() if owner != probe_owner]
                assert len(source_holds) == 1
                source_hold = source_holds[0]
            assert source_hold.owner[1] is not None
            writer = executor.submit(write)
            async with asyncio.timeout(2):
                while True:
                    with admission._condition:
                        if len(admission._queue) == 1:
                            break
                    await asyncio.sleep(0)
            probe.release()
            for reason in ("首次取消真实 source copy", "重复取消真实 source copy"):
                operation.cancel(reason)
                await asyncio.sleep(0)
                assert not operation.done(), "取消父 Task 不能提前结束 worker 的锁生命周期"
                with admission._condition:
                    assert admission._holders.get(source_hold.owner) is source_hold
                    assert len(admission._queue) == 1
                assert not writer_ready.is_set() and not worker_finished.is_set()
            release_worker.set()
            with pytest.raises(asyncio.CancelledError, match="首次取消真实 source copy"):
                await asyncio.wait_for(operation, timeout=10)
            assert worker_finished.is_set()
            assert await asyncio.to_thread(writer_ready.wait, 2)
            release_writer.set()
            await asyncio.wait_for(asyncio.wrap_future(writer), timeout=2)
            capture = context.sessions.path_resolver.catalog_store.get_fork_source_capture(
                copy_operation_id=operation_key,
            )
            assert capture is not None and capture.state == "preparing"
            with pytest.raises(IncompleteForkSourceCaptureError):
                read_captured_snapshot(storage.sessions_dir, capture)
            assert (source_root / "rollout.jsonl").read_bytes() == original_jsonl
            assert not admission._holders and not admission._queue
        finally:
            release_worker.set()
            release_writer.set()
            if probe is not None:
                probe.release()
            if started.is_set():
                assert await asyncio.to_thread(worker_finished.wait, 10), "真实 source copy worker 未退出"
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            if writer is not None:
                await asyncio.gather(asyncio.wrap_future(writer), return_exceptions=True)


@pytest.mark.skipif(os.name != "posix", reason="真实 source capture 的共享锁与异步 Task 公平性")
async def test_local_capture_waiters_do_not_borrow_another_task_shared_hold(
    fork_provider_prepare, production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, fork_gateway_request_context: None,
) -> None:
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(
        context, fork_provider_prepare, attachment_store=production_fork_container.attachment_blob_store,
    )
    thread_id = _main_thread_id(context, source.session_id)
    storage = context.saver._storage.storage
    original = storage.jsonl_path(source.session_id, thread_id=thread_id).read_bytes()
    request = await _fork_request(
        context, source.session_id, creation_kind="new_session_main", mode="context_fork",
    )
    operation_key = context.forks._creation_key(request.idempotency_key)
    reached, release_capture = asyncio.Event(), asyncio.Event()
    observer = fork_source.observe_phase

    async def pause_capture(operation_id: str, phase: str, **kwargs):
        await observer(operation_id, phase, **kwargs)
        if operation_id == operation_key and phase == "copy.source.partial":
            reached.set()
            await release_capture.wait()

    monkeypatch.setattr(fork_source, "observe_phase", pause_capture)
    writer_ready, release_writer = threading.Event(), threading.Event()

    def write() -> None:
        with storage._lock(thread_id, "", session_id=source.session_id):
            writer_ready.set()
            assert release_writer.wait(timeout=2)

    async def read() -> TurnHistoryPageDTO:
        async with context.saver.history_read_lease(source.session_id, thread_id):
            return context.saver.load_history(source.session_id, thread_id, TurnHistoryLoadRequest())

    operation = asyncio.create_task(context.forks.fork(request))
    probe = None
    reader: asyncio.Task[TurnHistoryPageDTO] | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            await asyncio.wait_for(reached.wait(), timeout=10)
            probe = storage.read_lock(source.session_id, thread_id)
            await probe.acquire_async()
            admission = probe._admission
            assert admission is not None

            async def wait_queue(size: int) -> None:
                async with asyncio.timeout(2):
                    while True:
                        with admission._condition:
                            if len(admission._queue) == size:
                                return
                        await asyncio.sleep(0)

            writer = executor.submit(write)
            await wait_queue(1)
            reader = asyncio.create_task(read())
            await wait_queue(2)
            probe.release()
            assert not writer_ready.is_set() and not reader.done()
            operation.cancel("验证真实 source capture 的取消清理")
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert await asyncio.to_thread(writer_ready.wait, 1)
            assert not reader.done()
            release_writer.set()
            page = await asyncio.wait_for(reader, timeout=2)
            writer.result(timeout=1)
            assert page.items
            assert storage.jsonl_path(source.session_id, thread_id=thread_id).read_bytes() == original
        finally:
            release_capture.set()
            release_writer.set()
            if probe is not None:
                probe.release()
            operation.cancel()
            if reader is not None:
                reader.cancel()
            await asyncio.gather(operation, *([reader] if reader is not None else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("fault", ["committed_missing", "marker_directory", "marker_symlink", "marker_identity", "marker_hash", "root_symlink"])
async def test_fork_startup_rejects_corrupted_capture_proof(
    fork_provider_prepare, production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, creation_kind: SessionForkCreationKind, fault: str,
    fork_gateway_request_context: None,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context, fork_provider_prepare)
    source_node = _main_thread_node(context, source.session_id)
    source_jsonl = (source_node / "rollout/rollout.jsonl").read_bytes()
    request = await _fork_request(context, source.session_id, creation_kind=creation_kind, mode="context_fork")
    key = context.forks._creation_key(request.idempotency_key)
    phase = "copy.source.captured" if fault in {"committed_missing", "marker_hash"} else "copy.source.before_captured_cas"
    reached = asyncio.Event()
    release = asyncio.Event()
    observer = fork_owner.observe_phase

    async def pause_capture(operation_id: str, current_phase: str, **kwargs):
        await observer(operation_id, current_phase, **kwargs)
        if operation_id == key and current_phase == phase:
            reached.set()
            await release.wait()

    monkeypatch.setattr(fork_owner, "observe_phase", pause_capture)
    operation = asyncio.create_task(context.forks.fork(request))
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        operation.cancel("模拟 marker proof 边界的进程退出")
        with pytest.raises(asyncio.CancelledError, match="marker proof"):
            await asyncio.wait_for(operation, timeout=10)
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        monkeypatch.setattr(fork_owner, "observe_phase", observer)
    catalog = context.sessions.path_resolver.catalog_store
    capture = catalog.get_fork_source_capture(copy_operation_id=key)
    assert capture is not None
    assert capture.state == ("source_captured" if phase == "copy.source.captured" else "preparing")
    root = capture_directory(context.saver._storage.storage.sessions_dir, capture)
    marker = root / "captured.json"
    snapshot, marker_hash = read_captured_snapshot(context.saver._storage.storage.sessions_dir, capture)
    if phase == "copy.source.captured":
        assert (capture.snapshot_sha256, capture.marker_sha256) == (snapshot.snapshot_sha256, marker_hash)
    else:
        assert capture.snapshot_sha256 is None and capture.marker_sha256 is None
    if fault == "committed_missing":
        marker.unlink()
        expected = "已冻结 journal 缺少 captured marker"
    elif fault == "marker_directory":
        marker.unlink()
        marker.mkdir()
        expected = "缺少普通 captured marker"
    elif fault == "marker_symlink":
        marker.unlink()
        marker.symlink_to(root / "nonexistent-marker")
        expected = "captured marker 是符号链接"
    elif fault in {"marker_identity", "marker_hash"}:
        raw = json.loads(marker.read_bytes())
        raw["source_snapshot_id" if fault == "marker_identity" else "corruption_probe"] = "invalid-capture-proof"
        marker.write_bytes(canonical_json_bytes(raw))
        expected = "marker identity/preimage 冲突" if fault == "marker_identity" else "marker hash 与 journal 不一致"
    else:
        shutil.rmtree(root)
        root.symlink_to(root.with_name(root.name + "-absent"), target_is_directory=True)
        expected = "缺少普通 snapshot 目录"
    with pytest.raises(RuntimeError, match=expected) as reader_error:
        read_captured_snapshot(context.saver._storage.storage.sessions_dir, capture)
    assert type(reader_error.value) is RuntimeError
    production_fork_container.workspace_activity_service.store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    recovered = _production_fork_context(restarted)

    async def forbid_recapture(**_kwargs):
        pytest.fail("损坏 capture proof 不得读取 live source 修补")

    monkeypatch.setattr(recovered.saver.forks.source, "_capture_fork_source_snapshot", forbid_recapture)
    startup_error = "清理拒绝符号链接" if fault == "root_symlink" else expected
    with pytest.raises(RuntimeError, match=startup_error) as recovery_error:
        async with main.lifespan(main.app):
            pytest.fail("损坏 capture proof 不得让 startup ready")
    assert type(recovery_error.value) is RuntimeError
    if creation_kind == "new_session_main":
        with sqlite3.connect(context.workspace / ".boxteam/navigation/session-catalog.sqlite") as connection:
            assert connection.execute(
                "SELECT state FROM session_creation_records WHERE session_creation_idempotency_key=?", (key,),
            ).fetchone()[0] != "published"
    else:
        with sqlite3.connect(source_node / "session-control.sqlite") as connection:
            assert connection.execute(
                "SELECT state FROM thread_creation_records WHERE thread_creation_idempotency_key=?", (key,),
            ).fetchone()[0] != "published"
    assert (source_node / "rollout/rollout.jsonl").read_bytes() == source_jsonl


@pytest.mark.asyncio
async def test_fork_notification_failure_never_aborts_published_target(
    fork_provider_prepare,
    fork_context: ForkIntegrationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await _seed_source(fork_context, fork_provider_prepare)
    original = fork_context.sessions._notify_changed

    def fail_notification(action: str, session_id: str) -> None:
        original(action, session_id)
        raise RuntimeError("通知失败")

    monkeypatch.setattr(fork_context.sessions, "_notify_changed", fail_notification)
    with pytest.raises(RuntimeError, match="通知失败"):
        await fork_context.forks.fork(await _fork_request(fork_context, source.session_id, creation_kind="new_session_main", relationship="pinned"))
    assert (await fork_context.sessions.list(limit=100)).total == 2
    refs = _retention_refs(fork_context, source.session_id)
    assert len(refs) == 1 and refs[0][4] == "active"


async def _seed_retry_tool_source(
    context: ForkIntegrationContext, fork_provider_prepare, *,
    attachment_store: AttachmentBlobStore | None = None,
    registry: ResourceRegistry | None = None,
    activation_policy: ResourceActivationPolicySnapshot | None = None,
) -> tuple[SessionDTO, str]:
    """通过真实 notice/user admission、lost/resume 和 checkpoint 生成执行谱系。"""
    source = await context.sessions.create(SessionCreateRequest(title="重试和工具源会话"))
    owner = context.saver.forks.source.resolve_main_thread_owner(source.session_id)
    prepared = internal_message_factory.build(
        kind="generated_session_result", control="检查内部任务结果",
        sections=(PromptSection("control_context", {"source": "fork-test"}),
                  PromptSection("generated_session_result", "内部任务已经完成")),
        display_content="内部任务已经完成。",
    )
    notice = HumanMessage(
        content=prepared.content, id="retry-notice",
        response_metadata={"message_id": "retry-notice", "created_at": "2026-10-04T12:00:00+00:00",
                           "message_metadata": prepared.metadata},
    )
    internal = context.saver.admit_internal_execution(
        source.session_id, thread_id=owner.thread_id, job_id="job-internal-notice",
        intent=InternalExecutionIntent(message_id="retry-notice", execution_id="execution-internal-notice",
                                       admission_idempotency_key="admission-internal-notice"),
        message=notice,
    )
    with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
          execution_admission_scope(internal, langgraph_thread_id=owner.thread_id)):
        config = await context.saver.aput(
            build_checkpoint_config(owner.thread_id), _checkpoint("cp-notice", [notice]),
            {"source": "input", "step": 1, "parents": {}}, {"messages": "1", "counter": "1"},
        )
    context.saver.converge_execution(
        source.session_id, thread_id=owner.thread_id, turn_id=None,
        execution_id=internal.execution_id, outcome="completed_empty", turn_status=None,
    )
    content: str | list[dict[str, object]] = "检查工作区"
    attachments: list[AttachmentRef] = []
    if attachment_store is not None:
        attachments = await attachment_store.persist_inline(source.session_id, [AttachmentRef(
            file_id="inline:fork-source", name='source-&"attachment.txt', content_type="text/plain",
            data_url="data:text/plain;base64," + base64.b64encode("文件内容".encode()).decode("ascii"),
        )], thread_id=owner.thread_id, message_id="msg_" + create_uuid_hex(namespace="message"))
        content = UserContentBuilder(attachment_service=AttachmentContentService(
            workspace_root=context.workspace, attachment_store=attachment_store,
        )).build("检查工作区，保留正文中的源 URI " + attachments[0].file_id, attachments).content
    attachment_metadata = [attachment.model_dump(mode="json", exclude={"data_url"}) for attachment in attachments]
    accepted = context.saver.accept_turn(
        source.session_id, thread_id=owner.thread_id, accepted_ingress_id="retry-user",
        acceptance_idempotency_key="acceptance-retry-user", job_id="job-retry-initial",
        admission_idempotency_key="admission-retry-initial", payload=user_input_payload(content, attachment_metadata),
        payload_kind="structured_content" if attachments else "text",
        turn_id="turn-retry", root_item_id="item-retry-user",
        initial_execution_id="execution-retry-initial",
    )
    initial = context.saver.execution_admission(
        source.session_id, thread_id=owner.thread_id, execution_id=str(accepted["initial_execution_id"]),
    )
    user = HumanMessage(content=content, id="retry-user", response_metadata={
        "turn_id": "turn-retry", **({"attachments": attachment_metadata} if attachments else {}),
    })
    with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
          execution_admission_scope(initial, langgraph_thread_id=owner.thread_id)):
        config = await context.saver.aput(
            config, _checkpoint("cp-retry-user", [notice, user]),
            {"source": "input", "step": 2, "parents": {}}, {"messages": "2", "counter": "2"},
        )
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(source.session_id, owner.thread_id),
        control_state_port=context.saver,
    )
    fork_provider_prepare(context, source.session_id, initial.execution_id,
                          manager, "retry-initial", attempt=1, registry=registry,
                          activation_policy=activation_policy)
    context.saver.mark_execution_lost(
        source.session_id, thread_id=owner.thread_id, turn_id="turn-retry",
        execution_id=initial.execution_id,
    )
    resumed = context.saver.resume_turn(
        source.session_id, thread_id=owner.thread_id, turn_id="turn-retry", job_id="job-retry-resumed",
    )
    retry_id = str(resumed["execution_id"])
    retry = context.saver.execution_admission(
        source.session_id, thread_id=owner.thread_id, execution_id=retry_id,
    )
    prepared = fork_provider_prepare(context, source.session_id, retry.execution_id,
                                     manager, "retry-resumed", attempt=1, registry=registry,
                                     activation_policy=activation_policy)
    model_call_id = prepared["snapshot"].model_call_id
    messages = [
        notice, user,
        AIMessage(content="检查文件", id="retry-call",
                  tool_calls=[{"name": "read_file", "args": {"path": "README.md"}, "id": "retry-tool-call"}],
                  response_metadata={"model_call_id": model_call_id}),
        ToolMessage(content="文件内容", id="retry-result", tool_call_id="retry-tool-call",
                    response_metadata={"model_call_id": model_call_id}),
        AIMessage(content=[{"type": "text", "text": "检查完成"}, {"type": "text", "text": "结果已确认"}],
                  id="retry-final", response_metadata={"model_call_id": model_call_id}),
    ]
    with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
          execution_admission_scope(retry, langgraph_thread_id=owner.thread_id)):
        commit_fixture_output(context.saver, messages=messages)
        await context.saver.aput(
            config, _checkpoint("cp-retry-final", messages),
            {"source": "loop", "step": 3, "parents": {}}, {"messages": "5", "counter": "5"},
        )
    return source, retry_id


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork"])
@pytest.mark.parametrize("reverse_membership", [False, True])
async def test_fork_preserves_stream_member_time_across_checkpoint_carriers(
    fork_provider_prepare,
    production_fork_container: AppContainer,
    mode: str,
    reverse_membership: bool,
) -> None:
    """同一内容在 checkpoint carrier 出现后，复制仍保留原流式成员及其时间。"""
    context = _production_fork_context(production_fork_container)
    source = await context.sessions.create(SessionCreateRequest(title="原流式成员时间"))
    owner = context.saver.forks.source.resolve_main_thread_owner(source.session_id)
    accepted = context.saver.accept_turn(
        source.session_id, thread_id=owner.thread_id,
        accepted_ingress_id="stream-user", acceptance_idempotency_key="stream-acceptance",
        job_id="stream-job", admission_idempotency_key="stream-admission", payload="保留原成员",
        turn_id="stream-turn", root_item_id="item-stream-user", initial_execution_id="stream-execution",
    )
    admission = context.saver.execution_admission(
        source.session_id, thread_id=owner.thread_id, execution_id=accepted["initial_execution_id"],
    )
    user = HumanMessage(content="保留原成员", id="stream-user")
    with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
          execution_admission_scope(admission, langgraph_thread_id=owner.thread_id)):
        config = await context.saver.aput(
            build_checkpoint_config(owner.thread_id), _checkpoint("stream-input", [user]),
            {"source": "input", "step": 1, "parents": {}}, {"messages": "1", "counter": "1"},
        )
    manager = ContextSourceManager(
        owner=ContextSourceOwnerKey(source.session_id, owner.thread_id), control_state_port=context.saver,
    )
    prepared = fork_provider_prepare(context, source.session_id, admission.execution_id,
                                     manager, "stream-source", attempt=1)
    model_call = prepared["snapshot"].model_call_id
    stream = CanonicalBlockAccumulator(
        turn_id=admission.turn_id, execution_id=admission.execution_id,
        producer_id=model_call, model_call_id=model_call,
    )
    stream.accept({"block_id": f"{model_call}:block:stream-reasoning", "block_index": 0,
                   "carrier_type": "reasoning_content", "text": "保留原推理。"})
    if reverse_membership:
        stream.accept({"block_id": f"{model_call}:block:stream-second-reasoning", "block_index": 1,
                       "carrier_type": "reasoning_content", "text": "逻辑排序后的另一份推理。"})
    stream.accept({"block_id": f"{model_call}:block:stream-text", "block_index": 2 if reverse_membership else 1,
                   "carrier_type": "text", "text": "复制完成。"})
    final = AIMessage(
        content=[{"type": "reasoning_content", "text": "保留原推理。"}, {"type": "text", "text": "复制完成。"}],
        id="stream-final", response_metadata={"model_call_id": model_call, "phase": "final_answer",
            "content_part_refs": [{"id": "stream-reasoning", "index": 0, "type": "reasoning_content"},
                                  {"id": "stream-text", "index": 1, "type": "text"}]},
    )
    with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
          execution_admission_scope(admission, langgraph_thread_id=owner.thread_id)):
        stream_items = stream.finalize(first_item_sequence=1)
        context.saver.append_items(source.session_id, stream_items, thread_id=owner.thread_id)
        completed_messages = [user, final]
        commit_fixture_output(context.saver, messages=completed_messages)
        await context.saver.aput(
            config, _checkpoint("stream-completed", completed_messages),
            {"source": "loop", "step": 2, "parents": {}}, {"messages": "2", "counter": "2"},
        )
    if reverse_membership:
        storage = context.saver._storage.storage
        with storage._connect(owner.thread_id, session_id=source.session_id) as connection:
            view_id = storage._active_context_view_id(connection, "")
            reasoning_ids = [item.item_id for item in stream_items if item.semantic_kind == "reasoning"]
            assert len(reasoning_ids) == 2
            mutate_view_members(connection,
                "UPDATE context_view_items SET logical_item_ordinal=logical_item_ordinal+100 "
                "WHERE view_id=? AND item_id IN (?,?)", (view_id, *reasoning_ids),
            )
            for ordinal, item_id in enumerate(reversed(reasoning_ids), 1):
                mutate_view_members(connection, "UPDATE context_view_items SET logical_item_ordinal=? WHERE view_id=? AND item_id=?",
                                   (ordinal, view_id, item_id))
            connection.execute("UPDATE database_meta SET history_view_revision=history_view_revision+1 WHERE singleton_id=1")
    request = TurnHistoryLoadRequest(include=["user", "reasoning_detail", "final_response"])
    original = context.saver.load_history(source.session_id, owner.thread_id, request).items[0]
    assert original.activity_stats.item_count == (2 if reverse_membership else 1)
    assert original.response_parts[0].source.item_id.endswith(
        "block:stream-second-reasoning" if reverse_membership else "block:stream-reasoning"
    )
    assert original.response_parts[0].source.created_at != original.response_parts[-1].source.created_at
    child = (await context.forks.fork(await _fork_request(context, source.session_id, creation_kind="new_session_main", mode=mode))).session
    target_owner = context.saver.forks.source.resolve_main_thread_owner(child.session_id)
    restarted = RolloutCheckpointSaver(context.sessions.path_resolver.sessions_root)
    for saver in (context.saver, restarted):
        copied = saver.load_history(child.session_id, target_owner.thread_id, request).items[0]
        assert copied.turn_id == original.turn_id
        assert (copied.activity_stats.duration_ms, copied.activity_stats.item_count) == (
            original.activity_stats.duration_ms, original.activity_stats.item_count,
        )
        assert [(part.source.item_id, part.source.part_ordinal, part.kind, part.source.created_at, part.source.elapsed_ms)
                for part in copied.response_parts] == [
            (part.source.item_id, part.source.part_ordinal, part.kind, part.source.created_at, part.source.elapsed_ms)
            for part in original.response_parts
        ]
        activity = [part for part in copied.response_parts if part.kind != "final_text"]
        assert copied.activity_stats.first_item_sequence == min(part.source.item_sequence for part in activity)
        assert copied.activity_stats.last_item_sequence == max(part.source.item_sequence for part in activity)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork"])
async def test_completed_turn_fork_excludes_later_internal_input_header(
    fork_provider_prepare,
    production_fork_container: AppContainer,
    mode: str,
) -> None:
    """后继内部准入尚未追加 checkpoint 消息时，也不能覆盖完成 Turn 的状态。"""
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(context, fork_provider_prepare)
    owner = context.saver.forks.source.resolve_main_thread_owner(source.session_id)
    completed = await _get_tuple(context, source.session_id, checkpoint_id="cp-retry-final")
    assert completed is not None
    selected_items = context.saver._storage.storage.read_items(source.session_id, owner.thread_id)
    probe = context.saver.accept_turn(
        source.session_id, thread_id=owner.thread_id, accepted_ingress_id="header-probe-ingress",
        acceptance_idempotency_key="header-probe-accept", job_id="header-probe-job",
        admission_idempotency_key="header-probe-admission", payload="只用于封存源端真实工具投影",
        root_item_id="header-probe-root",
    )
    source_prepared = fork_provider_prepare(
        context, source.session_id, str(probe["initial_execution_id"]),
        ContextSourceManager(owner=ContextSourceOwnerKey(source.session_id, owner.thread_id), control_state_port=context.saver),
        "header-source-projection", attempt=1,
    )
    prepared = internal_message_factory.build(
        kind="generated_session_result", control="处理后继内部回报",
        sections=(PromptSection("control_context", {"source": "later-fork-test"}),
                  PromptSection("generated_session_result", "只属于下一次内部执行")),
        display_content="后继内部回报。",
    )
    notice = HumanMessage(
        content=prepared.content, id="later-internal-notice",
        response_metadata={"message_id": "later-internal-notice", "created_at": "2026-10-04T12:01:00+00:00",
                           "message_metadata": prepared.metadata},
    )
    internal = context.saver.admit_internal_execution(
        source.session_id, thread_id=owner.thread_id, job_id="job-later-internal",
        intent=InternalExecutionIntent(
            message_id="later-internal-notice", execution_id="execution-later-internal",
            admission_idempotency_key="admission-later-internal",
        ),
        message=notice,
    )
    later = _checkpoint("cp-later-internal-input", completed.checkpoint["channel_values"]["messages"])
    later["channel_values"]["counter"] = 999
    later["channel_versions"]["counter"] = "999"
    with (session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
          execution_admission_scope(internal, langgraph_thread_id=owner.thread_id)):
        await context.saver.aput(
            completed.config, later, {"source": "input", "step": 4, "parents": {}},
            {"messages": "5", "counter": "999"},
        )
    context.saver.converge_execution(
        source.session_id, thread_id=owner.thread_id, turn_id=None,
        execution_id=internal.execution_id, outcome="completed_empty", turn_status=None,
    )
    source_database = _main_thread_node(context, source.session_id) / "rollout/index.sqlite"
    with sqlite3.connect(source_database) as connection:
        headers = connection.execute(
            "SELECT checkpoint_id,message_sequence,commit_id,metadata_json FROM checkpoints "
            "WHERE checkpoint_id IN ('cp-retry-final','cp-later-internal-input') ORDER BY commit_id"
        ).fetchall()
        assert headers[0][1] == headers[1][1]
        assert headers[0][2] < headers[1][2]
        assert json.loads(headers[1][3])["boxteam_execution_binding"]["execution_id"] == internal.execution_id
    request = await _fork_request(context, source.session_id, mode=mode, creation_kind="new_session_main")
    result = await context.forks.fork(request)
    target = result.session
    target_items = context.saver._storage.storage.read_items(target.session_id, result.thread_id)
    assert {item.item_id: item.to_dict() for item in target_items} == {
        item.item_id: item.to_dict() for item in selected_items
    }
    with context.saver._storage.storage.read_connection(target.session_id, result.thread_id) as connection:
        assert connection.execute(
            "SELECT 1 FROM executions WHERE execution_id=?", (internal.execution_id,),
        ).fetchone() is None
    restored = await _get_tuple(context, target.session_id)
    assert restored is not None
    assert restored.checkpoint["channel_values"]["counter"] == completed.checkpoint["channel_values"]["counter"]
    assert {name: value for name, value in restored.checkpoint["channel_values"].items() if name != "messages"} == {
        name: value for name, value in completed.checkpoint["channel_values"].items() if name != "messages"
    }
    assert restored.checkpoint["channel_versions"] == completed.checkpoint["channel_versions"]
    assert restored.checkpoint["versions_seen"] == completed.checkpoint["versions_seen"]
    assert restored.checkpoint["pending_sends"] == completed.checkpoint["pending_sends"]
    assert "boxteam_execution_binding" not in restored.metadata
    historical = await _get_tuple(context, target.session_id, checkpoint_id="cp-retry-final")
    assert historical is not None
    assert historical.checkpoint == completed.checkpoint
    assert historical.metadata == completed.metadata
    assert historical.pending_writes == completed.pending_writes
    origin = _fork_origins(context, target.session_id)[0]
    assert origin[:2] == (target.session_id, source.session_id)
    assert origin[2] == "cp-retry-final"
    assert origin[3:5] == (request.source_view_id, mode)
    restarted = replace(context, saver=RolloutCheckpointSaver(context.sessions.path_resolver.sessions_root))
    cold = await _get_tuple(restarted, target.session_id)
    assert cold is not None
    assert cold.checkpoint == restored.checkpoint
    assert cold.metadata == restored.metadata
    accepted = restarted.saver.accept_turn(
        target.session_id, thread_id=result.thread_id, accepted_ingress_id="header-target-ingress",
        acceptance_idempotency_key="header-target-accept", job_id="header-target-job",
        admission_idempotency_key="header-target-admission", payload="内部回报后的目标新问题",
        root_item_id="header-target-root",
    )
    target_prepared = fork_provider_prepare(
        restarted, target.session_id, str(accepted["initial_execution_id"]),
        ContextSourceManager(owner=ContextSourceOwnerKey(target.session_id, result.thread_id), control_state_port=restarted.saver),
        "header-target-first", attempt=1,
    )
    # 源、目标新 acceptance 均排除已消费 notice，canonical 历史上方仍逐字段保留。
    effective_ids = [item.item_id for item in selected_items if item.item_id != "item-retry-notice"]
    _assert_fork_provider_history(target_prepared, source_prepared, selected_items, effective_ids, "header-target-root")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
async def test_fork_preserves_real_retry_internal_and_closed_tool_lineage(
    fork_provider_prepare,
    production_fork_container: AppContainer, mode: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source, retry_id = await _seed_retry_tool_source(
        context, fork_provider_prepare, attachment_store=production_fork_container.attachment_blob_store,
    )
    source_owner = context.saver.forks.source.resolve_main_thread_owner(source.session_id)
    source_items = context.saver._storage.storage.read_items(source.session_id, source_owner.thread_id)
    fork_result = await context.forks.fork(await _fork_request(context, source.session_id, creation_kind="new_session_main", mode=mode))
    child = fork_result.session
    target_owner = context.saver.forks.source.resolve_main_thread_owner(child.session_id)
    target_items = context.saver._storage.storage.read_items(child.session_id, target_owner.thread_id)
    assert len(target_items) == len(source_items)
    with sqlite3.connect(_main_thread_node(context, child.session_id) / "rollout" / "index.sqlite") as connection:
        executions = connection.execute(
            "SELECT execution_id, turn_id, execution_origin, job_id, admission_idempotency_key, "
            "admission_payload_hash, resumed_from_execution_id, accepted_ingress_id FROM executions"
        ).fetchall()
        assert len(executions) == 3
        by_id = {row[0]: row for row in executions}
        initial = by_id["execution-retry-initial"]
        retry = by_id[retry_id]
        internal = by_id["execution-internal-notice"]
        assert initial[2] == retry[2] == "user_turn"
        assert initial[3] == "job-retry-initial" and retry[3] == "job-retry-resumed"
        assert retry[6] == initial[0]
        assert internal[1:4] == (None, "internal", "job-internal-notice")
        assert internal[7] is None
        turn = connection.execute(
            "SELECT turn_id, initial_execution_id, last_execution_id, status, final_item_id "
            "FROM turn_records"
        ).fetchone()
        assert turn[:4] == (initial[1], initial[0], retry[0], "completed")
        assert connection.execute("SELECT COUNT(*) FROM turn_acceptances").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM item_catalog WHERE execution_id IS NULL OR execution_id NOT IN "
            "(SELECT execution_id FROM executions)"
        ).fetchone() == (0,)
        payload_hash = connection.execute("SELECT payload_hash FROM turn_acceptances").fetchone()[0]
        for row in (initial, retry):
            assert row[5] == user_turn_admission_payload_hash(
                session_id=source.session_id, thread_id=source_owner.thread_id, turn_id=row[1],
                execution_id=row[0], job_id=row[3], admission_idempotency_key=row[4],
                accepted_ingress_id=row[7], payload_hash=payload_hash,
            )
        preimage = context.saver._storage.executions.internal_execution_admission_preimage(
            connection, session_id=source.session_id, thread_id=source_owner.thread_id,
            checkpoint_ns="", execution_id=internal[0], job_id=internal[3],
            admission_key=internal[4], admission_hash=internal[5],
        )
        assert preimage["session_id"] == source.session_id
        assert preimage["thread_id"] == source_owner.thread_id
        assert preimage["execution_id"] == "execution-internal-notice"
        assert preimage["input"]["kind"] == "runtime_notice"
        assert internal[5] == sha256_jcs(preimage)
        assert context.saver._storage.executions.internal_execution_request_input(
            child.session_id, target_owner.thread_id, internal[0],
        ) == preimage["input"]
        cursors = {}
        for row in (internal, initial, retry):
            admission = context.saver._storage.executions._read_execution_admission_connection(
                connection, session_id=source.session_id, thread_id=source_owner.thread_id, execution_id=row[0],
            )
            cursor = context.saver._storage.executions.execution_admission_cursor(
                admission, connection=connection,
            )
            cursors[row[0]] = cursor.commit_id
            assert cursor.commit_kind == ("execution_admission" if row is internal else "acceptance")
            excluded = context.saver._storage.executions.execution_context_excluded_item_ids(
                admission, connection=connection,
            )
            # internal 排除未来用户 Turn；同一用户 Turn 的 retry 复用 acceptance 冻结的 notice 排除集。
            expected = ({item.item_id for item in target_items if item.turn_id == initial[1]}
                        if row is internal else {"item-retry-notice"})
            assert excluded == expected
        assert cursors[internal[0]] < cursors[initial[0]] == cursors[retry[0]]
        root_commit = connection.execute(
            "SELECT commit_id FROM item_catalog WHERE item_id=(SELECT root_input_item_id FROM turn_records)",
        ).fetchone()[0]
        assert root_commit == cursors[initial[0]]
        assert connection.execute(
            "SELECT commit_id FROM item_catalog WHERE execution_id=? AND semantic_kind='runtime_notice'",
            (internal[0],),
        ).fetchone() == (cursors[internal[0]],)
    with sqlite3.connect(_main_thread_node(context, child.session_id) / "rollout" / "index.sqlite") as connection:
        connection_execution_by_item = dict(connection.execute("SELECT item_id, execution_id FROM item_catalog"))
    calls = [item for item in target_items if item.semantic_kind == SemanticKind.TOOL_CALL]
    results = [item for item in target_items if item.semantic_kind == SemanticKind.TOOL_RESULT]
    assert len(calls) == len(results) == 1
    call_id = calls[0].payload["tool_calls"][0]["id"]
    assert call_id == results[0].tool_call_id == results[0].payload["tool_call_id"]
    assert call_id == "retry-tool-call"
    assert connection_execution_by_item[calls[0].item_id] == connection_execution_by_item[results[0].item_id] == retry[0]
    assert results[0].producer_ref["invocation_id"] == retry[0]
    restored = await _get_tuple(context, child.session_id)
    assert restored is not None
    messages = restored.checkpoint["channel_values"]["messages"]
    assistant_call = next(message for message in messages if isinstance(message, AIMessage) and message.tool_calls)
    result = next(message for message in messages if isinstance(message, ToolMessage))
    assert assistant_call.tool_calls[0]["id"] == result.tool_call_id == call_id
    assert messages[-1].content == [{"type": "text", "text": "检查完成"}, {"type": "text", "text": "结果已确认"}]
    source_user = next(item for item in source_items if item.semantic_kind == SemanticKind.USER_INPUT)
    target_user = next(item for item in target_items if item.semantic_kind == SemanticKind.USER_INPUT)
    assert target_user.payload == source_user.payload and target_user.content_hash == source_user.content_hash
    source_file_id = source_user.payload["attachments"][0]["file_id"]
    target_reference = production_fork_container.attachment_blob_store.project_attachment(child.session_id, target_owner.thread_id, source_file_id)
    target_file_id = target_reference["file_id"]
    assert target_reference["availability"] == "available"
    assert [(resource.resource_uri, resource.action, resource.target_uri, resource.availability) for resource in fork_result.resources] == [
        (source_file_id, "retain_reference", target_file_id, "available"),
    ]
    source_ref, source_blob = production_fork_container.attachment_blob_store._resolve_published(
        source.session_id, source_file_id, owner_thread_id=source_owner.thread_id,
    )
    target_ref, target_blob = production_fork_container.attachment_blob_store._resolve_published(
        child.session_id, target_file_id, owner_thread_id=target_owner.thread_id,
    )
    assert target_ref.owner_session_id == child.session_id and target_ref.owner_thread_id == target_owner.thread_id
    assert target_ref.owner_ref_id != source_ref.owner_ref_id and target_blob == source_blob
    restored_user = next(message for message in messages if message.response_metadata.get("attachments"))
    assert restored_user.content == source_user.payload["content"]
    assert restored_user.response_metadata["attachments"] == source_user.payload["attachments"]
    assert restored_user.response_metadata["canonical_item_manifests"] == [{
        "item_id": target_user.item_id, **target_user.projection_manifest(),
    }]
    restarted = RolloutCheckpointSaver(
        context.sessions.path_resolver.sessions_root,
        attachment_store=production_fork_container.attachment_blob_store,
    )
    cold = await _get_tuple(context, child.session_id, saver=restarted)
    assert cold is not None and cold.checkpoint["channel_values"]["messages"] == messages
    history = restarted.load_history(
        child.session_id, target_owner.thread_id,
        TurnHistoryLoadRequest(turn_ids=[target_user.turn_id], include=["user", "final_response"]),
    )
    history_json = json.dumps(history.model_dump(mode="json"), ensure_ascii=False)
    assert f'"file_id": "{target_file_id}"' in history_json
    assert source_user.payload["content"][0]["text"] in history_json
    target_jsonl = _main_thread_node(context, child.session_id) / "rollout" / "rollout.jsonl"
    copied_bytes = target_jsonl.read_bytes()
    # target 后继请求消费旧 imported lineage，并继续按同一准入游标隔离新输入。
    successor = context.saver.accept_turn(
        child.session_id, thread_id=target_owner.thread_id, accepted_ingress_id="fork-successor-user",
        acceptance_idempotency_key="fork-successor-acceptance", job_id="fork-successor-job",
        admission_idempotency_key="fork-successor-admission", payload="继续检查副本",
        root_item_id="item-fork-successor-user",
    )
    admission = context.saver.execution_admission(
        child.session_id, thread_id=target_owner.thread_id, execution_id=successor["initial_execution_id"],
    )
    with (session_thread_owner_scope(target_owner, langgraph_thread_id=target_owner.thread_id),
          execution_admission_scope(admission, langgraph_thread_id=target_owner.thread_id)):
        latest = await context.saver.aget_tuple(build_checkpoint_config(target_owner.thread_id))
        assert latest is not None and latest.checkpoint["channel_values"]["messages"][-1].content == "继续检查副本"
        plan = context.saver.compose_committed_context_plan(
            child.session_id, target_owner.thread_id, plan_id="fork-successor-plan",
            tool_set=ToolSetSnapshot(selection_revision=sha256_jcs([]), tools=[]),
        )
        assert "item-fork-successor-user" in {ref.ref_id for ref in plan.refs}
        request_messages = latest.checkpoint["channel_values"]["messages"]
        assert context.saver.ensure_request_items(
            child.session_id, target_owner.thread_id, turn_id=admission.turn_id,
            execution_id=admission.execution_id, messages=request_messages,
        ) == ()
        unchanged = target_jsonl.read_bytes()
        changed_call = assistant_call.model_copy(deep=True)
        changed_call.response_metadata["canonical_item_manifests"][0]["item_id"] = "unregistered-copy-item"
        with pytest.raises(ValueError, match="canonical group manifest item identity"):
            context.saver.ensure_request_items(
                child.session_id, target_owner.thread_id, turn_id=admission.turn_id,
                execution_id=admission.execution_id, messages=[changed_call],
            )
        assert target_jsonl.read_bytes() == unchanged
        changed_result = result.model_copy(update={"content": "复制结果正文被改动"})
        with pytest.raises(RuntimeError, match="canonical message group"):
            context.saver.ensure_request_items(
                child.session_id, target_owner.thread_id, turn_id=admission.turn_id,
                execution_id=admission.execution_id, messages=[assistant_call, changed_result],
            )
        assert target_jsonl.read_bytes() == unchanged
        changed_user = restored_user.model_copy(deep=True)
        changed_user.content[1]["metadata"]["availability"] = "available"
        with pytest.raises(ValueError, match="canonical item 与 message projection 内容不一致"):
            await context.saver.aput(
                latest.config,
                _checkpoint("fork-tampered-attachment", [
                    changed_user if message.id == restored_user.id else message
                    for message in request_messages
                ]),
                {"source": "loop", "step": 1, "parents": {}},
                {"messages": "tampered", "counter": "tampered"},
            )
        assert target_jsonl.read_bytes() == unchanged
        after_rejection = await context.saver.aget_tuple(build_checkpoint_config(target_owner.thread_id))
        assert after_rejection is not None and after_rejection.config == latest.config
        assert after_rejection.checkpoint["channel_values"]["messages"] == request_messages
        manager = ContextSourceManager(
            owner=ContextSourceOwnerKey(child.session_id, target_owner.thread_id),
            control_state_port=context.saver,
        )
        prepared = fork_provider_prepare(
            context, child.session_id, admission.execution_id, manager,
            "fork-successor-model", attempt=1,
            selected_tools=ToolSetSnapshot(selection_revision=sha256_jcs({"read_file": True}), tools=({
                "name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            },)),
        )
        assert any(isinstance(message, ToolMessage) and message.content == result.content
                   for message in prepared["messages"])
        # Provider 的附件坐标来自 generated block 正文；不要求保留 checkpoint 展示 metadata。
        projected_user = next(message for message in prepared["messages"]
                              if isinstance(message, HumanMessage) and isinstance(message.content, list)
                              and any(isinstance(block, dict) and block.get("metadata", {}).get("kind") == "attachment_manifest"
                                      for block in message.content))
        assert projected_user.content[0] == restored_user.content[0]
        projected_manifest = projected_user.content[1]
        assert projected_manifest["metadata"]["file_id"] == projected_manifest["metadata"]["path"] == target_file_id
        assert projected_manifest["metadata"]["availability"] == "available"
        manifest_xml = fromstring(projected_manifest["text"] + "</attachment>")
        assert manifest_xml.attrib["id"] == manifest_xml.attrib["path"] == target_file_id
        assert manifest_xml.attrib["name"] == 'source-&"attachment.txt'
        callback_id = prepared["snapshot"].model_call_id
        read_call = AIMessage(content="读取副本附件", id="lc_run--" + callback_id,
                              tool_calls=[{"name": "read_file", "args": {"path": target_file_id}, "id": "successor-attachment-read"}])
        context.saver.ensure_request_items(
            child.session_id, target_owner.thread_id, turn_id=admission.turn_id,
            execution_id=admission.execution_id, messages=[read_call],
        )
        read_content = context.saver.read_tool_call_attachment(
            child.session_id, thread_id=target_owner.thread_id, model_call_id=callback_id,
            tool_call_id="successor-attachment-read", file_id=target_file_id,
        )
        assert read_content.data == "文件内容".encode() and read_content.content_type == "text/plain"
        completed_messages = [
                *request_messages, read_call,
                ToolMessage(content=read_content.data.decode(), id="successor-attachment-result", tool_call_id="successor-attachment-read"),
                AIMessage(content="副本后继执行完成", id="fork-successor-final"),
            ]
        commit_fixture_output(context.saver, messages=completed_messages)
        await context.saver.aput(
            latest.config,
            _checkpoint("fork-successor-checkpoint", completed_messages),
            {"source": "loop", "step": 1, "parents": {}},
            {"messages": "successor", "counter": "successor"},
        )
        restored_successor = await _get_tuple(context, child.session_id)
        assert restored_successor is not None
        assert restored_successor.checkpoint["channel_values"]["messages"][-1].content == "副本后继执行完成"
        assert target_jsonl.read_bytes().startswith(copied_bytes)
        assert context.saver._storage.storage.read_items(
            child.session_id, target_owner.thread_id,
        )[:len(target_items)] == target_items
    with context.saver._storage.storage.read_connection(child.session_id, target_owner.thread_id, "") as connection:
        old_admission = context.saver._storage.executions._read_execution_admission_connection(
            connection, session_id=source.session_id, thread_id=source_owner.thread_id, execution_id=retry[0],
        )
    with (
        pytest.raises(ValueError, match="execution admission 与 LangGraph thread_id 冲突"),
        session_thread_owner_scope(target_owner, langgraph_thread_id=target_owner.thread_id),
        execution_admission_scope(old_admission, langgraph_thread_id=target_owner.thread_id),
    ):
        context.saver.compose_committed_context_plan(
            child.session_id, target_owner.thread_id, plan_id="fork-old-execution-plan",
            tool_set=ToolSetSnapshot(selection_revision=sha256_jcs([]), tools=[]),
        )
    with context.saver._storage.storage.read_connection(child.session_id, target_owner.thread_id, "") as connection:
        assert connection.execute("SELECT COUNT(*) FROM context_plans WHERE plan_id='fork-old-execution-plan'").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
async def test_detached_fork_uses_frozen_lineage_after_source_deletion(
    fork_provider_prepare,
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source, retry_id = await _seed_retry_tool_source(context, fork_provider_prepare)
    source_node = _main_thread_node(context, source.session_id)
    capture = context.saver.forks.source._capture_fork_source_snapshot

    async def capture_then_delete(**kwargs: object):
        snapshot = await capture(**kwargs)
        await context.sessions.path_resolver.delete_subtree(idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id)
        assert not source_node.exists()
        return snapshot

    monkeypatch.setattr(context.saver.forks.source, "_capture_fork_source_snapshot", capture_then_delete)
    child = (await context.forks.fork(await _fork_request(context, source.session_id, creation_kind="new_session_main", mode=mode))).session
    assert child.context_source_session_id == source.session_id
    assert (await context.sessions.list(limit=100)).total == 1
    with context.saver._storage.storage.read_connection(child.session_id, _main_thread_id(context, child.session_id), "") as connection:
        execution_sources = {row[0] for row in connection.execute("SELECT execution_id FROM executions")}
    assert execution_sources == {"execution-retry-initial", retry_id, "execution-internal-notice"}
    assert _fork_materialization_rows(context, child.session_id)[0][2] == "committed"
    assert await _get_tuple(context, child.session_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
async def test_generation_fork_reuses_one_durable_target_after_source_deletion(
    fork_provider_prepare,
    production_fork_container: AppContainer,
    mode: SessionForkMode,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context, fork_provider_prepare)
    origin = SessionGenerationOriginDTO(
        generator_id="generator-fork", run_id="run-fork", idempotency_key="generation-key-fork",
        generator_type_id="context-fork", generator_type_version="1",
    )
    request = await context.forks.prepare_generation_request(
        source_session_id=source.session_id, source_thread_id=None,
        origin=origin, title="幂等生成副本", parent_node_id=None,
    )
    request = SessionForkRequest.model_validate(request.model_dump(mode="json") | {
        "mode": mode, "anchor_item_id": None if mode == "full_rollout_copy" else request.anchor_item_id,
    })
    source_thread = _main_thread_id(context, source.session_id)
    original_items = {item.item_id: item.to_dict() for item in context.saver._storage.storage.read_items(
        source.session_id, source_thread,
    )}
    results = await asyncio.gather(*(context.forks.fork(request, generation_origin=origin) for _ in range(3)))
    assert len({result.session.session_id for result in results}) == 1
    assert len({result.thread_id for result in results}) == 1
    target = results[0].session
    assert target.generation_origin == origin
    catalog = context.sessions.path_resolver.catalog_store
    with catalog.read_transaction() as connection:
        assert tuple(connection.execute(
            "SELECT COUNT(*) FROM session_creation_records WHERE purpose = 'context_fork'"
        ).fetchone()) == (1,)
    assert len(_fork_materialization_rows(context, target.session_id)) == 1
    imported_items = {item.item_id: item.to_dict() for item in context.saver._storage.storage.read_items(
        target.session_id, results[0].thread_id,
    )}
    assert imported_items == {item_id: original_items[item_id] for item_id in imported_items}
    if mode == "full_rollout_copy":
        assert set(imported_items) == set(original_items)
    else:
        assert set(imported_items) < set(original_items)
    await context.sessions.path_resolver.delete_subtree(idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id)
    with RolloutCheckpointSaver(context.sessions.path_resolver.sessions_root) as restarted:
        restarted_service = SessionContextForkService(
            session_service=context.sessions, checkpointer=restarted,
            thread_creation_factory=context.forks._threads, goal_service=production_fork_container.goal_service,
            terminal_resource_owner=context.forks._terminal_owner, browser_resource_owner=context.forks._browser_owner,
        )
        repeated = await restarted_service.fork(request, generation_origin=origin)
        assert await _get_tuple(context, repeated.session.session_id, saver=restarted) is not None
        assert {item.item_id: item.to_dict() for item in restarted._storage.storage.read_items(
            repeated.session.session_id, repeated.thread_id,
        )} == imported_items
    assert repeated.session.session_id == target.session_id
    assert repeated.thread_id == results[0].thread_id
    assert len(_fork_materialization_rows(context, target.session_id)) == 1
    assert (await context.sessions.list(limit=100)).total == 1


def _fork_intent_state(context: ForkIntegrationContext) -> tuple[object, ...]:
    """比较权威 intent 与持久业务文件；忽略经运行原件确认的 SQLite/锁辅助文件。"""
    with context.sessions.path_resolver.catalog_store.read_transaction() as connection:
        rows = tuple(tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                     for table in ("session_creation_records", "fork_retention_claims", "nodes", "catalog_metadata"))
    root = context.workspace / ".boxteam"
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        name = path.name
        if name.endswith((".lock", ".write.lock", ".sqlite-shm", ".sqlite-wal")):
            continue
        if path.is_dir() and relative == Path("navigation/session-lifecycle-gates"):
            continue
        if path.is_file():
            data = path.read_bytes()
            # SQLite 主文件的权威业务行由上方 catalog 查询及调用方的 control/job 断言核对。
            content = None if name.endswith(".sqlite") else hashlib.sha256(data).hexdigest()
            entries.append((relative.as_posix(), "file", content))
        else:
            entries.append((relative.as_posix(), "directory", None))
    entries = tuple(entries)
    return rows, entries


def _session_control_state(path: Path) -> tuple[object, ...]:
    """只读快照 session-control 中所有表，包含未发布的 child creation intent。"""
    uri = path.as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        tables = tuple(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        ))
        state = []
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            rows = tuple(sorted(
                (tuple(row) for row in connection.execute(f"SELECT * FROM {quoted}")),
                key=repr,
            ))
            state.append((table, rows))
    return tuple(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_live_fork_defaults_to_zero_attach(
    production_fork_container: AppContainer, fork_provider_prepare, live_resource_transport_factory,
    fork_gateway_request_context: None,
    kind: ResourceKind, creation_kind: SessionForkCreationKind,
) -> None:
    context = _production_fork_context(production_fork_container)
    external = live_resource_transport_factory(production_fork_container, kind)
    source = await _seed_source(context, fork_provider_prepare)
    owner, resource = await _create_fork_live_resource(production_fork_container, kind, source.session_id)
    rid = resource["resource_id"]
    point = await context.forks.source_point(session_id=source.session_id, mode="context_fork")
    candidate = next(item for item in point.live_resources if item.resource_id == rid)
    assert candidate.model_dump(mode="json")["source_thread"] == resource["source_thread"]
    assert candidate.owner_revision == resource["owner_revision"] and candidate.kind == kind
    request = await _fork_request(context, source.session_id, creation_kind=creation_kind, mode="context_fork")
    result = await context.forks.fork(request)
    current = await owner.get_resource(rid)
    assert current["thread_associations"] == resource["thread_associations"]
    assert current["owner_revision"] == resource["owner_revision"]
    assert owner._store.records[rid].fork_attachments == {}
    assert not any(item.owner_reference is not None for item in result.resources)
    assert external.creates == 1 and external.revisions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_live_fork_explicit_attach_reuses_instance_and_frozen_retry(
    production_fork_container: AppContainer, fork_provider_prepare, live_resource_transport_factory,
    fork_gateway_request_context: None,
    kind: ResourceKind, creation_kind: SessionForkCreationKind,
) -> None:
    context = _production_fork_context(production_fork_container)
    external = live_resource_transport_factory(production_fork_container, kind)
    source = await _seed_source(context, fork_provider_prepare)
    owner, resource = await _create_fork_live_resource(production_fork_container, kind, source.session_id)
    rid = resource["resource_id"]
    request = (await _fork_request(context, source.session_id, creation_kind=creation_kind, mode="context_fork")).model_copy(
        update={"resources": [_fork_live_choice(resource)]},
    )
    source_ref = owner._store.records[rid].source_thread
    lease = owner._leases.acquire_operation(resource_id=rid, turn_stream_id="live-source-turn", operation_id="live-source-use",
        holder_session_id=source_ref.session_id, holder_thread_id=source_ref.thread_id)
    try:
        result = await context.forks.fork(request)
        live = next(item for item in result.resources if item.owner_reference is not None)
        assert live.availability == "available" and live.operation_state == "committed"
        assert live.committed_revision == resource["owner_revision"] + 1
        current = live.owner_resource
        assert current is not None and current.resource_id == rid and current.workspace_id == source.workspace_id
        assert current.source_thread.model_dump() == resource["source_thread"]
        assert current.retention_scope == "workspace" and current.status == "running"
        assert {tuple(ref.model_dump().values()) for ref in current.thread_associations} == {
            (source_ref.session_id, source_ref.thread_id), (result.session.session_id, result.thread_id),
        }
        assert owner._leases.get_lease(lease.lease_id).status == "active"
        repeated = await context.forks.fork(request)
        assert (repeated.session.session_id, repeated.thread_id) == (result.session.session_id, result.thread_id)
        assert repeated.resources == result.resources
        assert external.revisions == [(rid, live.committed_revision)] and external.creates == 1
    finally:
        owner._leases.release(lease.lease_id)
    target = ThreadRef(result.session.session_id, result.thread_id)
    detached = await owner.command(rid, action="detach", expected_revision=current.owner_revision,
        holder_id="live-detach", operation_id="live-detach", thread=target)
    revisions = list(external.revisions)
    later = await context.forks.fork(request)
    live_later = next(item for item in later.resources if item.owner_reference is not None)
    assert live_later.operation_state == "committed" and live_later.availability == "unavailable"
    assert live_later.owner_resource.owner_revision == detached["owner_revision"]
    assert target not in owner._store.records[rid].thread_associations
    assert external.revisions == revisions and external.creates == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_live_fork_source_delete_retains_committed_target_and_instance(
    production_fork_container: AppContainer, fork_provider_prepare, live_resource_transport_factory, kind: ResourceKind,
    fork_gateway_request_context: None,
) -> None:
    context = _production_fork_context(production_fork_container)
    external = live_resource_transport_factory(production_fork_container, kind)
    source = await _seed_source(context, fork_provider_prepare)
    _owner, resource = await _create_fork_live_resource(production_fork_container, kind, source.session_id)
    request = (await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="context_fork")).model_copy(
        update={"resources": [_fork_live_choice(resource)]},
    )
    result = await context.forks.fork(request)
    await context.sessions.path_resolver.delete_subtree(idempotency_key=f"live-source-delete-{source.session_id}", root_node_id=source.session_id)
    repeated = await context.forks.fork(request)
    live = next(item for item in repeated.resources if item.owner_reference is not None)
    assert live.availability == "available" and live.operation_state == "committed"
    assert live.owner_resource.resource_id == resource["resource_id"]
    assert live.owner_resource.source_thread.session_id == source.session_id
    assert live.owner_resource.thread_associations[0].model_dump() == {
        "session_id": result.session.session_id, "thread_id": result.thread_id,
    }
    assert len(live.owner_resource.thread_associations) == 1
    assert external.creates == 1 and external.records[resource["resource_id"]]["status"] == "running"
    assert (await context.sessions.list(limit=100)).total == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("fault", ["thread_scope", "stale_revision", "unverified", "cross_workspace", "cross_gateway"])
async def test_live_fork_preflight_rejects_before_target_or_attach(
    production_fork_container: AppContainer, fork_provider_prepare, live_resource_transport_factory,
    fork_gateway_request_context: None,
    monkeypatch: pytest.MonkeyPatch, kind: ResourceKind, fault: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    external = live_resource_transport_factory(production_fork_container, kind)
    source = await _seed_source(context, fork_provider_prepare)
    owner, resource = await _create_fork_live_resource(production_fork_container, kind, source.session_id,
        scope="thread" if fault == "thread_scope" else "workspace")
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="context_fork")
    choice = _fork_live_choice(resource)
    if fault == "stale_revision":
        await owner.command(resource["resource_id"], action="attach", expected_revision=resource["owner_revision"],
            holder_id="live-stale", operation_id="live-stale", thread=owner._store.records[resource["resource_id"]].source_thread)
    elif fault == "unverified":
        external.records[resource["resource_id"]]["reconciliation_status"] = "unknown"
    elif fault == "cross_workspace":
        request = request.model_copy(update={"target_container_uri": workspace_session_inventory_uri(
            "wksp_00000000400070008000000000000099")})
    elif fault == "cross_gateway":
        request = request.model_copy(update={"target_container_uri": workspace_session_inventory_uri(
            source.workspace_id, gateway_authority="other-gateway")})
    request = request.model_copy(update={"resources": [choice]})
    # 完整快照保留 lease 账本；固定观察时间，避免合法读取仅因时钟变化改写 updated_at。
    monkeypatch.setattr(owner._leases, "_now", lambda: "2026-10-18T00:00:00Z")
    owner_before = await owner.get_resource(resource["resource_id"])
    expected_revision = resource["owner_revision"] + (1 if fault == "stale_revision" else 0)
    assert owner_before["owner_revision"] == expected_revision
    assert owner_before["retention_scope"] == ("thread" if fault == "thread_scope" else "workspace")
    assert owner_before["reconciliation_status"] == ("unknown" if fault == "unverified" else "verified")
    assert owner._store.records[resource["resource_id"]].fork_attachments == {}
    before = _fork_intent_state(context)
    revisions = list(external.revisions)
    with pytest.raises(ValueError, match="fork_live_"):
        await context.forks.fork(request)
    owner_after = await owner.get_resource(resource["resource_id"])
    assert owner_after == owner_before
    assert _fork_intent_state(context) == before
    assert owner._store.records[resource["resource_id"]].fork_attachments == {}
    assert external.revisions == revisions and external.creates == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_thread_scope_live_fork_rejects_child_before_thread_creation(
    production_fork_container: AppContainer, fork_provider_prepare, live_resource_transport_factory,
    fork_gateway_request_context: None, monkeypatch: pytest.MonkeyPatch, kind: ResourceKind,
) -> None:
    context = _production_fork_context(production_fork_container)
    external = live_resource_transport_factory(production_fork_container, kind)
    source = await _seed_source(context, fork_provider_prepare)
    owner, resource = await _create_fork_live_resource(
        production_fork_container, kind, source.session_id, scope="thread",
    )
    request = (await _fork_request(
        context, source.session_id, creation_kind="new_child_thread", mode="context_fork",
    )).model_copy(update={"resources": [_fork_live_choice(resource)]})
    session_node = context.sessions.path_resolver.resolve_session_node(source.session_id)
    control_path = session_node / "session-control.sqlite"
    monkeypatch.setattr(owner._leases, "_now", lambda: "2026-10-18T00:00:00Z")
    owner_before = await owner.get_resource(resource["resource_id"])
    assert owner_before["retention_scope"] == "thread"
    assert owner_before["reconciliation_status"] == "verified"
    assert owner_before["owner_revision"] == resource["owner_revision"]
    assert "attach" in owner_before["available_actions"]
    assert owner._store.records[resource["resource_id"]].fork_attachments == {}
    control_before = _session_control_state(control_path)
    intent_before = _fork_intent_state(context)
    revisions = list(external.revisions)
    child_key = context.forks._creation_key(request.idempotency_key)

    with pytest.raises(ValueError, match="fork_live_resource_rejected:") as rejection:
        await context.forks.fork(request)
    assert "thread 范围资源不能关联其它 Thread" in str(rejection.value)

    owner_after = await owner.get_resource(resource["resource_id"])
    assert owner_after == owner_before
    control_after = _session_control_state(control_path)
    intent_after = _fork_intent_state(context)
    assert control_after == control_before
    assert intent_after == intent_before
    with pytest.raises(KeyError):
        context.forks._threads.for_owner_session(source.session_id).control_store.get_thread_creation_record(child_key)
    assert owner._store.records[resource["resource_id"]].fork_attachments == {}
    assert external.revisions == revisions and external.creates == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("crash_phase", ["before_attach", "dispatched", "committed"])
async def test_live_fork_published_startup_preserves_dispatch_and_commit_facts(
    production_fork_container: AppContainer, fork_provider_prepare, live_resource_transport_factory,
    fork_gateway_request_context: None,
    monkeypatch: pytest.MonkeyPatch, kind: ResourceKind, creation_kind: SessionForkCreationKind, crash_phase: str,
) -> None:
    from app.services.business import session_context_fork_service as fork_module
    from app.services.business.workspace_resources import owner as owner_module

    context = _production_fork_context(production_fork_container)
    external = live_resource_transport_factory(production_fork_container, kind)
    source = await _seed_source(context, fork_provider_prepare)
    owner, resource = await _create_fork_live_resource(production_fork_container, kind, source.session_id)
    rid = resource["resource_id"]
    request = (await _fork_request(context, source.session_id, creation_kind=creation_kind, mode="context_fork")).model_copy(
        update={"resources": [_fork_live_choice(resource)]},
    )
    key = context.forks._creation_key(request.idempotency_key)
    boundary = fork_module if crash_phase == "before_attach" else owner_module
    original_observer = boundary.observe_phase

    async def crash(operation_id: str, phase: str, **kwargs: object) -> None:
        if operation_id == key and phase == f"fork.live.{crash_phase}.{kind}":
            raise asyncio.CancelledError("live fork 发布后模拟进程退出")
        await original_observer(operation_id, phase, **kwargs)

    original_release = owner._leases.release
    if crash_phase == "committed":
        # 模拟已提交后进程退出，原 finally 没有执行；不手造 owner 回执或关联。
        monkeypatch.setattr(owner._leases, "release", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(boundary, "observe_phase", crash)
    with pytest.raises(asyncio.CancelledError, match="发布后模拟进程退出"):
        await context.forks.fork(request)
    monkeypatch.setattr(boundary, "observe_phase", original_observer)
    monkeypatch.setattr(owner._leases, "release", original_release)
    frozen = (context.sessions.path_resolver.catalog_store.get_creation_record(key) if creation_kind == "new_session_main" else
        context.forks._threads.for_owner_session(source.session_id).control_store.get_thread_creation_record(key))
    target_session = frozen.session_id if creation_kind == "new_session_main" else source.session_id
    target_thread = frozen.main_thread_id if creation_kind == "new_session_main" else frozen.child_thread_id
    target_ref = ThreadRef(target_session, target_thread)
    assert frozen.state == "published"
    receipt = owner.fork_attach_receipt(rid, creation_key=key, expected_revision=resource["owner_revision"], thread=target_ref)
    assert (None if receipt is None else receipt.state) == {
        "before_attach": None, "dispatched": "dispatching", "committed": "committed",
    }[crash_phase]
    assert len(external.revisions) == (1 if crash_phase == "committed" else 0)
    unrelated = None
    if crash_phase == "committed":
        assert owner._leases.get_lease(receipt.lease_id).status == "active"
        unrelated = owner._leases.acquire_operation(resource_id=rid, turn_stream_id="unrelated-live-operation", operation_id="other",
            holder_session_id=source.session_id, holder_thread_id=resource["source_thread"]["thread_id"])
    production_fork_container.workspace_activity_service.store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    for current_kind in ("terminal", "browser"):
        live_resource_transport_factory(restarted, current_kind, external if current_kind == kind else None)
    recovered_owner = restarted.terminal_resource_owner if kind == "terminal" else restarted.browser_resource_owner
    cold_gateway = set_current_gateway_id(None)
    cold_request = set_current_request_id(None)
    try:
        async with main.lifespan(main.app):
            # lifespan 在无 HTTP context 下恢复；公开 replay 仍带真实路由的 Gateway 身份。
            replay_gateway = set_current_gateway_id("gateway_6c8b2f9317a5a2c6c4569958b1e23d84")
            try:
                repeated = await restarted.session_context_fork_service.fork(request)
                assert (repeated.session.session_id, repeated.thread_id) == (target_session, target_thread)
                live = next(item for item in repeated.resources if item.owner_reference is not None)
                current = live.owner_resource
                assert external.creates == 1 and current.resource_id == rid
                # 冷 recover 本身加一次版本；原冻结 CAS 不得自动换成新 revision。
                assert current.owner_revision == resource["owner_revision"] + (2 if crash_phase == "committed" else 1)
                assert len(external.revisions) == (2 if crash_phase == "committed" else 1)
                if crash_phase == "before_attach":
                    assert live.operation_state == "rejected" and live.availability == "unavailable"
                    assert target_ref not in recovered_owner._store.records[rid].thread_associations
                elif crash_phase == "dispatched":
                    assert live.operation_state == "dispatching" and live.availability == "unavailable"
                    assert target_ref not in recovered_owner._store.records[rid].thread_associations
                    assert current.reconciliation_status != "verified"
                else:
                    assert live.operation_state == "committed" and live.committed_revision == resource["owner_revision"] + 1
                    assert target_ref in recovered_owner._store.records[rid].thread_associations
                    assert recovered_owner._leases.get_lease(receipt.lease_id).status == "released"
                    assert recovered_owner._leases.get_lease(unrelated.lease_id).status == "reconcile_required"
                    assert live.availability == "unavailable" and current.reconciliation_status != "verified"
                revisions = list(external.revisions)
                again = await restarted.session_context_fork_service.fork(request)
                assert again.resources == repeated.resources and external.revisions == revisions
                assert await restarted.session_context_fork_service.recover_prepared_forks() == 0
                assert (await restarted.session_service.list(limit=100)).total == (2 if creation_kind == "new_session_main" else 1)
            finally:
                reset_current_gateway_id(replay_gateway)
    finally:
        reset_current_request_id(cold_request)
        reset_current_gateway_id(cold_gateway)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
async def test_http_live_fork_serializes_complete_owner_and_partial_failure_without_new_target(
    production_fork_container: AppContainer, fork_provider_prepare, live_resource_transport_factory,
    fork_gateway_request_context: None, monkeypatch: pytest.MonkeyPatch,
    kind: ResourceKind, creation_kind: SessionForkCreationKind,
) -> None:
    from app.services.business import session_context_fork_service as fork_module

    context = _production_fork_context(production_fork_container)
    external = live_resource_transport_factory(production_fork_container, kind)
    source = await _seed_source(context, fork_provider_prepare)
    owner, resource = await _create_fork_live_resource(production_fork_container, kind, source.session_id)
    request = (await _fork_request(context, source.session_id, creation_kind=creation_kind, mode="context_fork")).model_copy(
        update={"resources": [_fork_live_choice(resource)]},
    )
    async with main.lifespan(main.app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://workspace",
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": source.workspace_id,
                 "X-BoxTeam-Gateway-Id": "gateway_6c8b2f9317a5a2c6c4569958b1e23d84", "X-Request-ID": "live-fork-wire"},
    ) as client:
        point = await client.get(f"/api/v1/sessions/{source.session_id}/fork-source-point", params={"mode": "context_fork"})
        assert point.status_code == 200, point.text
        candidate = next(item for item in point.json()["data"]["live_resources"] if item["resource_id"] == resource["resource_id"])
        assert candidate["kind"] == kind and candidate["source_thread"] == resource["source_thread"]
        response = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "live-fork-wire"
        result = response.json()["data"]
        live = next(item for item in result["resources"] if "owner_reference" in item)
        assert "resource_uri" not in live
        assert live["owner_reference"] == request.resources[0].owner_reference.model_dump(mode="json")
        assert live["operation_state"] == "committed" and live["availability"] == "available"
        expected = {"session_id": result["session"]["session_id"], "thread_id": result["thread_id"]}
        assert expected in live["owner_resource"]["thread_associations"]
        assert live["owner_resource"]["resource_id"] == resource["resource_id"]
        assert live["owner_resource"]["owner_revision"] == live["committed_revision"] == resource["owner_revision"] + 1
        original = fork_module.observe_phase
        conflicted = request.model_copy(update={"idempotency_key": "http-live-conflict-" + uuid4().hex,
            "resources": [_fork_live_choice(await owner.get_resource(resource["resource_id"]))]})
        conflict_key = context.forks._creation_key(conflicted.idempotency_key)

        async def advance_after_publication(operation_id: str, phase: str, **kwargs: object) -> None:
            if operation_id == conflict_key and phase == f"fork.live.before_attach.{kind}":
                current = await owner.get_resource(resource["resource_id"])
                await owner.command(resource["resource_id"], action="attach", expected_revision=current["owner_revision"],
                    thread=owner._store.records[resource["resource_id"]].source_thread,
                    holder_id="http-concurrent", operation_id="http-concurrent")
            await original(operation_id, phase, **kwargs)

        monkeypatch.setattr(fork_module, "observe_phase", advance_after_publication)
        failure = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=conflicted.model_dump(mode="json"))
        assert failure.status_code == 200, failure.text
        failed_result = failure.json()["data"]
        failed_live = next(item for item in failed_result["resources"] if "owner_reference" in item)
        assert failed_live["availability"] == "unavailable" and failed_live["operation_state"] == "rejected"
        assert "版本冲突" in failed_live["reason"] and failed_live["owner_resource"]["resource_id"] == resource["resource_id"]
        assert failed_result["thread_id"] != result["thread_id"]
        monkeypatch.setattr(fork_module, "observe_phase", original)
        revisions = list(external.revisions)
        retry = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=conflicted.model_dump(mode="json"))
        assert retry.status_code == 200, retry.text
        assert retry.json()["data"]["thread_id"] == failed_result["thread_id"]
        assert retry.json()["data"]["resources"] == failed_result["resources"]
        assert external.revisions == revisions and external.creates == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["published", "preparing"])
@pytest.mark.parametrize("drift", ["mode", "relationship", "title", "target_container_uri", "source_uri",
                                 "source_view_id", "source_view_revision", "anchor_item_id", "anchor_mode"])
async def test_generation_fork_intent_drift_rejects_before_any_side_effect(
    fork_provider_prepare,
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
    fork_gateway_request_context: None,
    state: str, drift: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context, fork_provider_prepare)
    alternate_session = await context.sessions.create(SessionCreateRequest(title="漂移目标 Session"))
    origin = SessionGenerationOriginDTO(generator_id="generator-intent", run_id="run-intent",
                                       idempotency_key="key-intent", generator_type_id="context-fork",
                                       generator_type_version="1")
    request = await context.forks.prepare_generation_request(
        source_session_id=source.session_id, source_thread_id=None,
        origin=origin, title="冻结分叉意图", parent_node_id=None,
    )
    alternative_container_uri = alternate_session.uri
    other_source_id = source.session_id[:-1] + ("0" if source.session_id[-1] != "0" else "1")
    overrides: dict[str, object] = {
        "mode": "full_rollout_copy", "relationship": "pinned", "title": "另一标题",
        "target_container_uri": alternative_container_uri,
        "source_uri": request.source_uri.replace(source.session_id, other_source_id),
        "source_view_id": request.source_view_id + "-other", "source_view_revision": request.source_view_revision + 1,
        "anchor_item_id": "item-u2", "anchor_mode": "before",
    }
    changed = request.model_dump(mode="json") | {drift: overrides[drift]}
    if drift == "mode":
        changed["anchor_item_id"] = None
    changed_request = SessionForkRequest.model_validate(changed)
    entered, release = asyncio.Event(), asyncio.Event()
    capture = context.saver.forks.source._capture_fork_source_snapshot

    async def wait_before_capture(**kwargs: object):
        entered.set()
        await release.wait()
        return await capture(**kwargs)

    if state == "preparing":
        monkeypatch.setattr(context.saver.forks.source, "_capture_fork_source_snapshot", wait_before_capture)
    initial = asyncio.create_task(context.forks.fork(request, generation_origin=origin))
    if state == "preparing":
        await asyncio.wait_for(entered.wait(), timeout=10)
    else:
        await initial
        await context.sessions.path_resolver.delete_subtree(idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id)
    repeated_service = SessionContextForkService(
        session_service=context.sessions, checkpointer=context.saver,
        thread_creation_factory=context.forks._threads, goal_service=production_fork_container.goal_service,
        terminal_resource_owner=context.forks._terminal_owner, browser_resource_owner=context.forks._browser_owner,
    )
    before = _fork_intent_state(context)
    try:
        with pytest.raises(RuntimeError, match="^fork request conflict: 同 idempotency_key 不同冻结请求$"):
            await repeated_service.fork(changed_request, generation_origin=origin)
        assert _fork_intent_state(context) == before
    finally:
        release.set()
        await initial


@pytest.mark.asyncio
async def test_generation_implicit_latest_fork_replays_frozen_request_after_source_deletion(
    fork_provider_prepare,
    production_fork_container: AppContainer,
    fork_gateway_request_context: None,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context, fork_provider_prepare)
    origin = SessionGenerationOriginDTO(generator_id="generator-latest", run_id="run-latest",
                                       idempotency_key="key-latest", generator_type_id="context-fork",
                                       generator_type_version="1")
    request = await context.forks.prepare_generation_request(
        source_session_id=source.session_id, source_thread_id=None,
        origin=origin, title="", parent_node_id=None,
    )
    assert request.source_uri == thread_resource_uri(
        source.workspace_id, source.session_id, _main_thread_id(context, source.session_id),
        gateway_authority="gateway_6c8b2f9317a5a2c6c4569958b1e23d84",
    )
    assert request.target_container_uri == workspace_session_inventory_uri(
        source.workspace_id, gateway_authority="gateway_6c8b2f9317a5a2c6c4569958b1e23d84",
    )
    result = await context.forks.fork(request, generation_origin=origin)
    target = result.session
    with context.sessions.path_resolver.catalog_store.read_transaction() as connection:
        (raw_intent,) = connection.execute(
            "SELECT request_intent_json FROM session_creation_records WHERE session_id = ?", (target.session_id,)
        ).fetchone()
    intent = json.loads(raw_intent)
    assert intent["request"] == request.model_dump(mode="json")
    assert intent["source_session_id"] == source.session_id
    assert intent["source_thread_id"] == _main_thread_id(context, source.session_id)
    assert request.mode == "context_fork" and request.title == ""
    assert request.source_view_id and request.source_view_revision >= 0 and request.anchor_item_id is not None
    assert "request_intent_json" not in json.loads((context.sessions.path_resolver.resolve_session_node(target.session_id)
                                                   / "session.json").read_text())
    await context.sessions.path_resolver.delete_subtree(idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id)
    repeated_service = SessionContextForkService(
        session_service=context.sessions, checkpointer=context.saver,
        thread_creation_factory=context.forks._threads, goal_service=production_fork_container.goal_service,
        terminal_resource_owner=context.forks._terminal_owner, browser_resource_owner=context.forks._browser_owner,
    )
    before = _fork_intent_state(context)
    frozen = await repeated_service.prepare_generation_request(
        source_session_id=source.session_id, source_thread_id=None,
        origin=origin, title="", parent_node_id=None,
    )
    assert frozen == request
    repeated = await repeated_service.fork(frozen, generation_origin=origin)
    assert repeated.session.session_id == target.session_id
    assert repeated.thread_id == result.thread_id
    assert _fork_intent_state(context) == before


@pytest.mark.asyncio
async def test_folder_move_keeps_context_fork_kind_across_restart(
    fork_provider_prepare,
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 fork 发布后移动 folder，只改 catalog 父关系，重启保持来源与 manifest。"""
    context = _production_fork_context(production_fork_container)
    parent = await _seed_source(context, fork_provider_prepare)
    child = (await context.forks.fork(await _fork_request(context, parent.session_id, creation_kind="new_session_main", title="上下文副本", mode="context_fork", target_container_uri=parent.uri, turn_id="turn-1"))).session
    resolver = context.sessions.path_resolver
    manifest_path = resolver.resolve_session_node(child.session_id) / "session.json"
    manifest_before = manifest_path.read_bytes()
    journal_before = _fork_materialization_rows(context, child.session_id)
    assert len(journal_before) == 1 and journal_before[0][2] == "committed"
    assert child.parent_session_id == parent.session_id
    catalog = production_fork_container.session_catalog_service
    folder_receipt = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "create_folder", name="子目录", parent_node_id=parent.session_id,
    ))
    assert folder_receipt.state == "committed", folder_receipt.error_detail
    folder_id = folder_receipt.result_node_id
    assert folder_id is not None
    receipt = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "move_node", target_node_id=child.session_id, parent_node_id=folder_id,
    ))
    assert receipt.state == "committed", receipt.error_detail
    assert (await context.sessions.get(child.session_id)).parent_session_id == parent.session_id
    receipt = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "move_node", target_node_id=folder_id, parent_node_id=None,
    ))
    assert receipt.state == "committed", receipt.error_detail
    moved = await context.sessions.get(child.session_id)
    assert moved.kind == "context_fork" and moved.title == "上下文副本"
    assert moved.parent_session_id is None
    assert moved.context_source_session_id == parent.session_id
    assert manifest_path.read_bytes() == manifest_before
    assert resolver.catalog_store.get_node(child.session_id).session_kind == "context_fork"
    assert resolver.get_node(folder_id).parent_node_id is None
    assert resolver.get_node(child.session_id).parent_node_id == folder_id
    assert _fork_materialization_rows(context, child.session_id) == journal_before

    # 清空原 catalog 装配后从同一物理工作区重启，不用拷贝或修改 manifest 造状态。
    production_fork_container.workspace_activity_service.store.close()
    resolver.catalog_store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    async with main.lifespan(main.app):
        restored = await restarted.session_service.get(child.session_id)
        assert restored.kind == "context_fork" and restored.title == "上下文副本"
        assert restored.parent_session_id is None
        assert restored.context_source_session_id == parent.session_id
        restarted_resolver = restarted.session_service.path_resolver
        assert restarted_resolver.get_node(child.session_id).parent_node_id == folder_id
        assert restarted_resolver.get_node(folder_id).parent_node_id is None
        assert (restarted_resolver.resolve_session_node(child.session_id)
                / "session.json").read_bytes() == manifest_before
        assert _fork_materialization_rows(_production_fork_context(restarted), child.session_id) == journal_before


@pytest.mark.asyncio
async def test_moving_parent_session_carries_complete_child_tree(
    fork_provider_prepare,
    production_fork_container: AppContainer,
) -> None:
    """真实 fork 随父会话逻辑移动，物理节点、子关系与来源保持一致。"""
    context = _production_fork_context(production_fork_container)
    parent = await _seed_source(context, fork_provider_prepare)
    child = (await context.forks.fork(await _fork_request(context, parent.session_id, creation_kind="new_session_main", title="Child", mode="context_fork", target_container_uri=parent.uri, turn_id="turn-1"))).session
    resolver = context.sessions.path_resolver
    child_path_before = resolver.resolve_session_node(child.session_id)
    manifest_before = (child_path_before / "session.json").read_bytes()
    catalog = production_fork_container.session_catalog_service
    folder_receipt = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "create_folder", name="目标文件夹", parent_node_id=None,
    ))
    assert folder_receipt.state == "committed", folder_receipt.error_detail
    folder_id = folder_receipt.result_node_id
    assert folder_id is not None
    receipt = await submit_catalog_intent(catalog, create_catalog_intent(
        catalog, "move_node", target_node_id=parent.session_id, parent_node_id=folder_id,
    ))
    assert receipt.state == "committed", receipt.error_detail
    moved_parent_path = resolver.resolve_session_node(parent.session_id)
    moved_child_path = resolver.resolve_session_node(child.session_id)
    assert (await context.sessions.get(parent.session_id)).parent_session_id is None
    moved = await context.sessions.get(child.session_id)
    assert moved.parent_session_id == parent.session_id
    assert moved.kind == "context_fork" and moved.title == "Child"
    assert moved.context_source_session_id == parent.session_id
    assert moved_parent_path.parent == child_path_before.parent
    assert moved_child_path.parent == moved_parent_path.parent
    assert resolver.get_node(parent.session_id).parent_node_id == folder_id
    assert resolver.get_node(child.session_id).parent_node_id == parent.session_id
    assert moved_child_path.name == child_path_before.name
    assert moved_child_path == child_path_before
    assert (moved_child_path / "session.json").read_bytes() == manifest_before
    assert _fork_materialization_rows(context, child.session_id)[0][2] == "committed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
async def test_fork_preserves_internal_admission_audit_and_target_reader_rejects(
    fork_provider_prepare,
    production_fork_container: AppContainer, mode: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(context, fork_provider_prepare)
    source_root = _main_thread_node(context, source.session_id) / "rollout"
    source_jsonl = (source_root / "rollout.jsonl").read_bytes()
    with sqlite3.connect(source_root / "index.sqlite") as connection:
        connection.execute(
            "UPDATE executions SET admission_payload_hash = (SELECT content_hash FROM item_catalog "
            "WHERE item_id = 'item-retry-user') WHERE execution_id = 'execution-internal-notice'"
        )
        connection.commit()
        historical_execution = connection.execute(
            "SELECT * FROM executions WHERE execution_id = 'execution-internal-notice'",
        ).fetchone()
        source_notice = connection.execute(
            "SELECT item_id,content_hash,jsonl_offset,jsonl_length FROM item_catalog "
            "WHERE execution_id = 'execution-internal-notice' AND semantic_kind = 'runtime_notice'",
        ).fetchone()
    assert historical_execution is not None and source_notice is not None
    source_notice_bytes = source_jsonl[source_notice[2]:source_notice[2] + source_notice[3]]
    result = await context.forks.fork(await _fork_request(
        context, source.session_id, creation_kind="new_session_main", mode=mode,
    ))
    target_root = _main_thread_node(context, result.session.session_id) / "rollout"
    target_jsonl = (target_root / "rollout.jsonl").read_bytes()
    with sqlite3.connect(target_root / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT * FROM executions WHERE execution_id = 'execution-internal-notice'",
        ).fetchone() == historical_execution
        target_notice = connection.execute(
            "SELECT item_id,content_hash,jsonl_offset,jsonl_length FROM item_catalog "
            "WHERE execution_id = 'execution-internal-notice' AND semantic_kind = 'runtime_notice'",
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM executions WHERE session_id = ? AND thread_id = ?",
            (result.session.session_id, result.thread_id),
        ).fetchone() == (0,)
    assert target_notice is not None
    assert target_notice[:2] == source_notice[:2]
    assert target_jsonl[target_notice[2]:target_notice[2] + target_notice[3]] == source_notice_bytes
    if mode == "full_rollout_copy":
        assert target_jsonl == source_jsonl
    with pytest.raises(RuntimeError, match="internal admission hash 与冻结 request 不一致"):
        context.saver._storage.executions.internal_execution_request_input(
            result.session.session_id, result.thread_id, "execution-internal-notice",
        )
    with pytest.raises(ValueError, match="turn_not_resumable"):
        context.saver.resume_turn(
            result.session.session_id, thread_id=result.thread_id,
            turn_id="turn-retry", job_id="target-historical-resume",
        )
    with pytest.raises(ValueError, match="turn_not_resumable"):
        context.saver.dispatch_replay(
            result.session.session_id, thread_id=result.thread_id, turn_id="turn-retry",
        )
    accepted = context.saver.accept_turn(
        result.session.session_id, thread_id=result.thread_id,
        accepted_ingress_id="target-new-ingress", acceptance_idempotency_key="target-new-accept",
        job_id="target-new-job", admission_idempotency_key="target-new-admission", payload="目标的新输入",
    )
    admission = context.saver.execution_admission(
        result.session.session_id, thread_id=result.thread_id,
        execution_id=str(accepted["initial_execution_id"]),
    )
    assert (admission.session_id, admission.thread_id) == (result.session.session_id, result.thread_id)
    assert admission.turn_id == accepted["turn_id"] and admission.turn_id != "turn-retry"
    with sqlite3.connect(target_root / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT * FROM executions WHERE execution_id = 'execution-internal-notice'",
        ).fetchone() == historical_execution
        assert connection.execute(
            "SELECT COUNT(*) FROM executions WHERE session_id = ? AND thread_id = ?",
            (result.session.session_id, result.thread_id),
        ).fetchone() == (1,)
    with sqlite3.connect(source_root / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT * FROM executions WHERE execution_id = 'execution-internal-notice'",
        ).fetchone() == historical_execution
    assert (source_root / "rollout.jsonl").read_bytes() == source_jsonl
    assert (await context.sessions.list(limit=100)).total == 2
    assert _retention_refs(context, source.session_id) == []


@pytest.mark.asyncio
async def test_pinned_preparing_claim_blocks_source_deletion_before_capture(
    fork_provider_prepare,
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context, fork_provider_prepare)
    generation = context.saver._storage.forks._source_lifecycle_generation(source.session_id)
    entered, proceed = asyncio.Event(), asyncio.Event()
    capture = context.saver.forks.source._capture_fork_source_snapshot

    async def paused_capture(**kwargs: object):
        entered.set()
        await proceed.wait()
        return await capture(**kwargs)

    monkeypatch.setattr(context.saver.forks.source, "_capture_fork_source_snapshot", paused_capture)
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", relationship="pinned")
    pending = asyncio.create_task(context.forks.fork(request))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        catalog = context.sessions.path_resolver.catalog_store
        with catalog.read_transaction() as connection:
            claim = tuple(connection.execute(
                "SELECT fork_retention_claim_id, target_session_id, source_lifecycle_generation, state "
                "FROM fork_retention_claims WHERE source_session_id = ?", (source.session_id,),
            ).fetchone())
        assert claim[2:] == (generation, "preparing")
        assert (await context.sessions.get(source.session_id)).session_id == source.session_id
        with pytest.raises(RuntimeError, match="source_retention_operation_pending"):
            await context.sessions.path_resolver.delete_subtree(idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id)
        assert context.saver._storage.forks._source_lifecycle_generation(source.session_id) == generation
        assert _main_thread_node(context, source.session_id).is_dir()
    finally:
        proceed.set()
        child = (await pending).session
    active = catalog.get_fork_retention_claim(claim[0])
    assert active.target_session_id == child.session_id == claim[1]
    assert active.source_lifecycle_generation == generation and active.state == "active"
    assert len(_retention_refs(context, source.session_id)) == 1
    control = SessionControlStore(_main_thread_node(context, source.session_id) / "session-control.sqlite", read_only=True)
    try:
        lease = control.find_lease_by_operation("fork-retention:" + claim[0])
        assert lease is not None and lease.operation_kind == "fork_retention"
        assert lease.state == "active" and lease.captured_lifecycle_generation == generation
        capture_record = catalog.get_fork_source_capture(fork_id=claim[0])
        assert capture_record is not None
        assert lease.preimage_hash == capture_record.preimage_hash
        assert lease.recovery_ref == capture_record.copy_operation_id
    finally:
        control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_kind", ["new_session_main", "new_child_thread"])
@pytest.mark.parametrize("replay", [False, True], ids=["fresh", "published_replay"])
@pytest.mark.parametrize("finalizer_first", [True, False], ids=["commit_first", "delete_first"])
async def test_http_fork_reply_preserves_committed_copy_across_target_delete(
    fork_provider_prepare, production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, creation_kind: SessionForkCreationKind,
    replay: bool, finalizer_first: bool,
) -> None:
    """真实 POST 以原 finalizer 和真实删除决定先后，不在删除后重新投影成功回执。"""
    context = _production_fork_context(production_fork_container)
    store = production_fork_container.attachment_blob_store
    source, _ = await _seed_retry_tool_source(context, fork_provider_prepare, attachment_store=store)
    parent = await context.sessions.create(SessionCreateRequest(title="独立 child 目标")) if creation_kind == "new_child_thread" else None
    request = await _fork_request(
        context, source.session_id, creation_kind=creation_kind, mode="full_rollout_copy",
        target_container_uri=parent.uri if parent is not None else None,
    )
    catalog = context.sessions.path_resolver.catalog_store
    key = context.forks._creation_key(request.idempotency_key)
    entered, proceed = asyncio.Event(), asyncio.Event()
    observer = session_subtree_delete.observe_phase
    finalizer = store.finalize_copy_attachments
    target_session_id = target_thread_id = target_uri = ""
    target_path: Path | None = None
    expected_resources: list[dict[str, object]] = []
    body = "文件内容".encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()

    async def paused_delete(operation_id: str, phase: str, **kwargs: object) -> None:
        if phase == "deletion.catalog_deleting" and not finalizer_first:
            entered.set()
            await proceed.wait()
        await observer(operation_id, phase, **kwargs)

    async def ordered_finalizer(capture: ForkSourceCaptureRecord, *, publication_preimage_hash: str) -> None:
        nonlocal target_session_id, target_thread_id, target_uri, target_path, expected_resources
        target_session_id, target_thread_id = capture.target_session_id, capture.target_thread_id
        target_path = _main_thread_node(context, target_session_id)
        snapshots = store._catalog.fork_attachment_snapshots(
            capture.copy_operation_id, target_session_id=target_session_id, target_thread_id=target_thread_id,
        )
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert (snapshot.source_session_id, snapshot.source_thread_id) == (source.session_id, _main_thread_id(context, source.session_id))
        assert (snapshot.target_session_id, snapshot.target_thread_id, snapshot.target_owner_thread_id) == (target_session_id, target_thread_id, target_thread_id)
        assert (snapshot.digest, snapshot.length, snapshot.variant) == (digest, len(body), "original")
        assert snapshot.claim_id is not None and snapshot.availability == "available"
        target_uri = snapshot.target_file_id
        expected_resources = [{
            "resource_uri": snapshot.source_file_id, "action": "retain_reference", "availability": "available",
            "target_uri": target_uri, "reason": None,
        }]
        if finalizer_first:
            await finalizer(capture, publication_preimage_hash=publication_preimage_hash)
            control = store._open_control_store(target_session_id)
            try:
                record = next(item for item in read_copy_attachment_settlements(control) if item.copy_operation_id == capture.copy_operation_id)
                assert (record.state, record.target_session_id, record.target_thread_id, record.preimage_hash, record.publication_preimage_hash) == (
                    "committed", target_session_id, target_thread_id, capture.preimage_hash, publication_preimage_hash,
                )
                assert record.claim_ids == (snapshot.claim_id,)
                assert control.get_lease(record.lease_id).state == "completed"
            finally:
                control.close()
            await context.sessions.path_resolver.delete_subtree(
                idempotency_key=f"http-copy-delete-{target_session_id}", root_node_id=target_session_id,
            )
        else:
            deleting = asyncio.create_task(context.sessions.path_resolver.delete_subtree(
                idempotency_key=f"http-copy-delete-{target_session_id}", root_node_id=target_session_id,
            ))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                assert catalog.get_node(target_session_id).state == "deleting" and target_path.is_dir()
                await finalizer(capture, publication_preimage_hash=publication_preimage_hash)
            finally:
                proceed.set()
                await deleting
        assert not target_path.exists()

    async with main.lifespan(main.app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://workspace",
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": context.sessions.workspace_id,
                 "X-BoxTeam-Gateway-Id": "gateway_6c8b2f9317a5a2c6c4569958b1e23d84", "X-Request-ID": "copy-delete-order"},
    ) as client:
        if replay:
            first = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
            assert first.status_code == 200, first.text
            before_delete = await client.get(
                f"/api/v1/sessions/{first.json()['data']['session']['session_id']}/attachments/content",
                params={"file_id": first.json()["data"]["resources"][0]["target_uri"], "variant": "original"},
            )
            assert before_delete.status_code == 200 and before_delete.content == body
        monkeypatch.setattr(session_subtree_delete, "observe_phase", paused_delete)
        monkeypatch.setattr(store, "finalize_copy_attachments", ordered_finalizer)
        response = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
        assert response.status_code == (200 if finalizer_first else 409), response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "copy-delete-order"
        assert target_path is not None and not target_path.exists()
        if finalizer_first:
            result = response.json()["data"]
            assert (result["session"]["session_id"], result["thread_id"], result["creation_kind"]) == (target_session_id, target_thread_id, creation_kind)
            assert result["resources"] == expected_resources
        else:
            assert "Session catalog 节点非 active" in response.json()["detail"]
        capture = catalog.get_fork_source_capture(creation_key=key)
        assert capture is not None and capture.state == "materialized"
        assert [row[0] for row in store._catalog._connected().execute(
            "SELECT state FROM fork_attachment_claims WHERE copy_operation_id=?", (capture.copy_operation_id,),
        )] == ["target_deleted"]
        deleted_content = await client.get(
            f"/api/v1/sessions/{target_session_id}/attachments/content", params={"file_id": target_uri, "variant": "original"},
        )
        assert deleted_content.status_code == 404, deleted_content.text
        retry = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
        assert retry.status_code == 404, retry.text
        assert catalog.get_fork_source_capture(creation_key=key) == capture
        assert not target_path.exists()
        source_content = await client.get(
            f"/api/v1/sessions/{source.session_id}/attachments/content",
            params={"file_id": expected_resources[0]["resource_uri"], "variant": "original"},
        )
        assert source_content.status_code == 200 and source_content.content == body
        assert "sha256:" + hashlib.sha256(source_content.content).hexdigest() == digest


@pytest.mark.asyncio
async def test_full_copy_missing_selected_attachment_aborts_without_publication(
    fork_provider_prepare, production_fork_container: AppContainer, fork_gateway_request_context: None,
) -> None:
    """当前 selection 仍使用的附件缺字节时，不发布目标或任何附件 claim。"""
    context = _production_fork_context(production_fork_container)
    store = production_fork_container.attachment_blob_store
    source, _ = await _seed_retry_tool_source(context, fork_provider_prepare, attachment_store=store)
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy")
    point = await context.forks.source_point(session_id=source.session_id, mode="full_rollout_copy")
    assert len(point.resources) == 1 and point.resources[0].required_for_context
    source_thread = _main_thread_id(context, source.session_id)
    with context.saver._storage.storage.read_connection(source.session_id, source_thread) as connection:
        assert connection.execute(
            "SELECT visible,context_selected FROM context_view_items WHERE view_id=? AND item_id='item-retry-user'",
            (point.source_view_id,),
        ).fetchone() == (1, 1)
    _, blob = store._resolve_published(source.session_id, point.resources[0].resource_uri, owner_thread_id=source_thread)
    (context.workspace / ".boxteam" / "attachments" / blob.relative_locator).unlink()
    catalog = context.sessions.path_resolver.catalog_store
    before = (await context.sessions.list(limit=100)).total
    with pytest.raises(FileNotFoundError):
        await context.forks.fork(request)
    capture = catalog.get_fork_source_capture(creation_key=context.forks._creation_key(request.idempotency_key))
    assert capture is not None and capture.state == "aborted"
    with catalog.read_transaction() as connection:
        assert connection.execute("SELECT node_id FROM nodes WHERE node_id=?", (capture.target_session_id,)).fetchall() == []
        assert connection.execute(
            "SELECT state FROM session_creation_records WHERE session_creation_idempotency_key=?", (capture.creation_key,),
        ).fetchone()[0] == "aborted"
    assert (await context.sessions.list(limit=100)).total == before
    assert store._catalog.fork_attachment_claim_ids(capture.copy_operation_id) == ()
    assert store._catalog.fork_attachment_snapshots(
        capture.copy_operation_id, target_session_id=capture.target_session_id, target_thread_id=capture.target_thread_id,
    ) == ()
    assert store._catalog.get_blob(blob.blob_id).availability == "available"


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_case", ["empty", "omit", "unavailable", "missing", "reuse"])
async def test_http_published_fork_without_attachment_claim_rejects_deleting_target(
    fork_provider_prepare, production_fork_container: AppContainer,
    monkeypatch: pytest.MonkeyPatch, resource_case: str, fork_gateway_request_context: None,
) -> None:
    """无 claim 的空资源、历史缺正文、omit 和引用复用仍受真实删除 gate 约束。"""
    context = _production_fork_context(production_fork_container)
    store = production_fork_container.attachment_blob_store
    source, _ = await _seed_retry_tool_source(
        context, fork_provider_prepare, attachment_store=None if resource_case == "empty" else store,
    )
    if resource_case in {"omit", "unavailable", "missing"}:
        # 原附件进入历史后，通过真实 compaction checkpoint 将它移出 required replay。
        thread_id = _main_thread_id(context, source.session_id)
        owner = MutationIntentOwner(source.session_id, thread_id)
        previous = await _get_tuple(context, source.session_id)
        assert previous is not None
        old_messages = previous.checkpoint["channel_values"]["messages"]
        accepted = context.saver.accept_turn(
            source.session_id, thread_id=thread_id, accepted_ingress_id="unclaimed-copy-input",
            acceptance_idempotency_key="unclaimed-copy-accept", job_id="unclaimed-copy-job",
            admission_idempotency_key="unclaimed-copy-admit", payload="旧附件只保留在历史中",
            root_item_id="unclaimed-copy-root",
        )
        admission = context.saver.execution_admission(
            source.session_id, thread_id=thread_id, execution_id=str(accepted["initial_execution_id"]),
        )
        admitted = next(item for item in context.saver._storage.storage.read_items(source.session_id, thread_id)
                        if item.item_id == "unclaimed-copy-root")
        user = messages_from_dict([LangChainMessageCodec().project_message((admitted,))])[0]
        summary = HumanMessage(content="旧附件的检查已经完成", id="unclaimed-copy-summary", response_metadata={
            "provenance_relations": [ItemProvenanceRelation(
                relation="derived_from", target_kind="canonical_item", target_id="item-retry-user",
                target_session_id=source.session_id, target_thread_id=thread_id, target_turn_id="turn-retry",
            ).to_dict()],
        })
        event = build_cache_preserving_event(
            CachePreservingPartition(prefix_messages=[], messages_to_summarize=old_messages,
                                     preserved_messages=[user], state_cutoff=len(old_messages)),
            summary_message=summary,
        )
        compacted = _checkpoint("cp-unclaimed-copy-compaction", [*old_messages, user])
        compacted["channel_values"]["_summarization_event"] = event
        compacted["channel_versions"]["_summarization_event"] = "1"
        compacted["updated_channels"].append("_summarization_event")
        with (session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
              execution_admission_scope(admission, langgraph_thread_id=thread_id)):
            await context.saver.aput(
                build_checkpoint_config(thread_id), compacted, {"source": "compaction", "step": 4, "parents": {}},
                {"messages": "6", "counter": "6", "_summarization_event": "1"},
            )
        prepared = fork_provider_prepare(
            context, source.session_id, admission.execution_id,
            ContextSourceManager(owner=ContextSourceOwnerKey(source.session_id, thread_id), control_state_port=context.saver),
            "unclaimed-copy-after-compaction", attempt=1,
        )
        assert "item-retry-user" not in {
            entry.ref.ref_id for entry in prepared["snapshot"].selection
            if entry.included and entry.ref.ref_type == "canonical_item"
        }
        assert all(message.id != "retry-user" for message in prepared["messages"])
        final = AIMessage(content="历史附件已移出当前输入", id="unclaimed-copy-final", response_metadata={
            "model_call_id": prepared["snapshot"].model_call_id,
        })
        with (session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
              execution_admission_scope(admission, langgraph_thread_id=thread_id)):
            completed_messages = [*old_messages, user, final]
            commit_fixture_output(context.saver, messages=completed_messages)
            await context.saver.aput(
                build_checkpoint_config(thread_id), _checkpoint("cp-unclaimed-copy-final", completed_messages),
                {"source": "loop", "step": 5, "parents": {}}, {"messages": "7", "counter": "7"},
            )
    creation_kind: SessionForkCreationKind = "new_child_thread" if resource_case == "reuse" else "new_session_main"
    request = await _fork_request(context, source.session_id, creation_kind=creation_kind, mode="full_rollout_copy")
    if resource_case in {"omit", "unavailable", "missing"}:
        point = await context.forks.source_point(session_id=source.session_id, mode="full_rollout_copy")
        assert len(point.resources) == 1 and not point.resources[0].required_for_context
        with context.saver._storage.storage.read_connection(source.session_id, thread_id) as connection:
            assert connection.execute(
                "SELECT visible,context_selected,context_ordinal FROM context_view_items WHERE view_id=? AND item_id=?",
                (point.source_view_id, "item-retry-user"),
            ).fetchone() == (1, 0, None)
        visible = await production_fork_container.message_service.list(source.session_id)
        assert any(attachment.file_id == point.resources[0].resource_uri
                   for message in visible.items for attachment in message.attachments)
        if resource_case == "missing":
            _, blob = store._resolve_published(source.session_id, point.resources[0].resource_uri, owner_thread_id=thread_id)
            (context.workspace / ".boxteam" / "attachments" / blob.relative_locator).unlink()
            assert request.resources == []
        else:
            request = request.model_copy(update={"resources": [SessionForkResourceChoice(
                resource_uri=point.resources[0].resource_uri, action="omit",
            )]})
    entered, proceed = asyncio.Event(), asyncio.Event()
    observer = session_subtree_delete.observe_phase
    finalizer = store.finalize_copy_attachments
    invocations = 0
    catalog = context.sessions.path_resolver.catalog_store

    async with main.lifespan(main.app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://workspace",
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": context.sessions.workspace_id,
                 "X-BoxTeam-Gateway-Id": "gateway_6c8b2f9317a5a2c6c4569958b1e23d84", "X-Request-ID": "unclaimed-copy-delete"},
    ) as client:
        if resource_case == "unavailable":
            omitted = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
            assert omitted.status_code == 200, omitted.text
            source = await context.sessions.get(omitted.json()["data"]["session"]["session_id"])
            request = await _fork_request(context, source.session_id, creation_kind=creation_kind, mode="full_rollout_copy")
        first = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
        assert first.status_code == 200, first.text
        target_session_id = first.json()["data"]["session"]["session_id"]
        target_path = _main_thread_node(context, target_session_id)
        key = context.forks._creation_key(request.idempotency_key)
        capture = catalog.get_fork_source_capture(creation_key=key)
        assert capture is not None and capture.state == "materialized"
        assert store._catalog.fork_attachment_claim_ids(capture.copy_operation_id) == ()
        resources = first.json()["data"]["resources"]
        if resource_case == "empty":
            assert resources == []
        else:
            assert len(resources) == 1
            assert (resources[0]["action"], resources[0]["availability"]) == (
                ("reuse_reference", "available") if resource_case == "reuse" else ("omit", "unavailable")
            )

        if resource_case == "missing":
            snapshots = store._catalog.fork_attachment_snapshots(
                capture.copy_operation_id, target_session_id=target_session_id, target_thread_id=first.json()["data"]["thread_id"],
            )
            assert len(snapshots) == 1
            assert (snapshots[0].required, snapshots[0].availability, snapshots[0].claim_id) == (False, "unavailable", None)
            target_thread = first.json()["data"]["thread_id"]
            cold_store = AttachmentBlobStore(context.workspace, path_resolver=context.sessions.path_resolver)
            try:
                with RolloutCheckpointSaver(context.sessions.path_resolver.sessions_root, attachment_store=cold_store) as cold:
                    history = cold.load_history(target_session_id, target_thread,
                        TurnHistoryLoadRequest(turn_ids=["turn-retry"], include=["user", "final_response"]))
                    references = [ref for turn in history.items for message in turn.user_messages for ref in message.attachments]
                    assert len(references) == 1 and references[0].availability == "unavailable"
                    accepted = cold.accept_turn(
                        target_session_id, thread_id=target_thread, accepted_ingress_id="missing-copy-successor",
                        acceptance_idempotency_key="missing-copy-successor-accept", job_id="missing-copy-successor-job",
                        admission_idempotency_key="missing-copy-successor-admit", payload="继续压缩后的副本",
                        root_item_id="missing-copy-successor-root",
                    )
                    successor = fork_provider_prepare(
                        replace(context, saver=cold), target_session_id, str(accepted["initial_execution_id"]),
                        ContextSourceManager(owner=ContextSourceOwnerKey(target_session_id, target_thread), control_state_port=cold),
                        "missing-copy-successor", attempt=1,
                    )
                    assert "item-retry-user" not in {
                        entry.ref.ref_id for entry in successor["snapshot"].selection
                        if entry.included and entry.ref.ref_type == "canonical_item"
                    }
                    assert all(message.id != "retry-user" for message in successor["messages"])
            finally:
                cold_store.close()

        async def paused_delete(operation_id: str, phase: str, **kwargs: object) -> None:
            if phase == "deletion.catalog_deleting":
                entered.set()
                await proceed.wait()
            await observer(operation_id, phase, **kwargs)

        async def deleting_finalizer(current: ForkSourceCaptureRecord, *, publication_preimage_hash: str) -> None:
            nonlocal invocations
            invocations += 1
            assert current == capture
            deleting = asyncio.create_task(context.sessions.path_resolver.delete_subtree(
                idempotency_key=f"http-unclaimed-delete-{target_session_id}", root_node_id=target_session_id,
            ))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                assert catalog.get_node(target_session_id).state == "deleting" and target_path.is_dir()
                await finalizer(current, publication_preimage_hash=publication_preimage_hash)
            finally:
                proceed.set()
                await deleting

        monkeypatch.setattr(session_subtree_delete, "observe_phase", paused_delete)
        monkeypatch.setattr(store, "finalize_copy_attachments", deleting_finalizer)
        response = await client.post(f"/api/v1/sessions/{source.session_id}/fork-context", json=request.model_dump(mode="json"))
        assert invocations == 1
        assert response.status_code == 409, response.text
        assert "Session catalog 节点非 active" in response.json()["detail"]
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "unclaimed-copy-delete"
        assert not target_path.exists()
        assert catalog.get_fork_source_capture(creation_key=key) == capture
        assert store._catalog.fork_attachment_claim_ids(capture.copy_operation_id) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
async def test_catalog_deleting_rejects_fork_before_local_fence_closes(
    fork_provider_prepare,
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """真实删除先提交 catalog 时，新 fork 不得靠仍 active 的本地目录准入。"""
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context, fork_provider_prepare)
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode=mode)
    catalog = context.sessions.path_resolver.catalog_store
    source_path = _main_thread_node(context, source.session_id)
    entered, proceed = asyncio.Event(), asyncio.Event()
    observer = session_subtree_delete.observe_phase

    async def paused_delete(operation_id: str, phase: str, **kwargs: object) -> None:
        if phase == "deletion.catalog_deleting":
            entered.set()
            await proceed.wait()
        await observer(operation_id, phase, **kwargs)

    monkeypatch.setattr(session_subtree_delete, "observe_phase", paused_delete)
    deleting = asyncio.create_task(context.sessions.path_resolver.delete_subtree(
        idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id,
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert catalog.get_node(source.session_id).state == "deleting"
        assert source_path.is_dir()
        before = _fork_intent_state(context)
        with pytest.raises(SessionDeletionPendingError, match="session_deletion_pending"):
            await context.forks.fork(request)
        assert _fork_intent_state(context) == before
    finally:
        proceed.set()
        await deleting


@pytest.mark.asyncio
async def test_source_deletion_before_pinned_claim_aborts_prepared_target(
    fork_provider_prepare,
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context, fork_provider_prepare)
    prepare = context.sessions.prepare_context_fork

    async def prepare_then_delete(**kwargs: object):
        prepared = await prepare(**kwargs)
        await context.sessions.path_resolver.delete_subtree(idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id)
        return prepared

    monkeypatch.setattr(context.sessions, "prepare_context_fork", prepare_then_delete)
    with pytest.raises(KeyError, match=source.session_id):
        await context.forks.fork(await _fork_request(context, source.session_id, creation_kind="new_session_main", relationship="pinned"))
    assert (await context.sessions.list(limit=100)).total == 0
    with context.sessions.path_resolver.catalog_store.read_transaction() as connection:
        assert tuple(connection.execute("SELECT COUNT(*) FROM fork_retention_claims").fetchone()) == (0,)
        assert [row[0] for row in connection.execute(
            "SELECT state FROM session_creation_records WHERE purpose = 'context_fork'"
        )] == ["aborted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "full_rollout_copy"])
@pytest.mark.parametrize("fault", ["completion_proof", "publish_parent_cas"])
async def test_unpublished_fork_proof_or_publish_failure_releases_exact_retention(
    fork_provider_prepare,
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch, mode: str, fault: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = await _seed_source(context, fork_provider_prepare)
    if fault == "completion_proof":
        afork = context.saver.forks.afork

        async def corrupt_completion(**kwargs: object):
            result = await afork(**kwargs)
            return replace(result, source_snapshot_sha256="0" * 64)

        monkeypatch.setattr(context.saver.forks, "afork", corrupt_completion)
    else:
        capture = context.saver.forks.source._capture_fork_source_snapshot

        async def drift_parent_revision(**kwargs: object):
            snapshot = await capture(**kwargs)
            context.sessions.path_resolver.get_node(context.sessions.path_resolver.catalog_store.rename_node(source.session_id, '发布前修改父节点').node_id)
            return snapshot

        monkeypatch.setattr(context.saver.forks.source, "_capture_fork_source_snapshot", drift_parent_revision)
    failure = "committed fork journal.*snapshot 合同不一致" if fault == "completion_proof" else "父节点 revision 已漂移"
    with pytest.raises(RuntimeError, match=failure):
        (await context.forks.fork(await _fork_request(context, source.session_id, creation_kind="new_session_main", mode=mode, relationship="pinned", target_container_uri=source.uri)))
    assert (await context.sessions.list(limit=100)).total == 1
    assert context.sessions.path_resolver.child_nodes(source.session_id) == []
    refs = _retention_refs(context, source.session_id)
    assert len(refs) == 1 and refs[0][4] == "released"
    with context.sessions.path_resolver.catalog_store.read_transaction() as connection:
        assert [row[0] for row in connection.execute("SELECT state FROM fork_retention_claims")] == ["released"]
        assert [row[0] for row in connection.execute(
            "SELECT state FROM session_creation_records WHERE purpose = 'context_fork'"
        )] == ["aborted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_state", ["target_committed", "committed"])
async def test_production_lifespan_recovers_detached_fork_after_source_deletion(
    fork_provider_prepare,
    production_fork_container: AppContainer, monkeypatch: pytest.MonkeyPatch, crash_state: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source, _ = await _seed_retry_tool_source(context, fork_provider_prepare)
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode="context_fork")
    capture = context.saver.forks.source._capture_fork_source_snapshot

    async def capture_then_delete(**kwargs: object):
        snapshot = await capture(**kwargs)
        await context.sessions.path_resolver.delete_subtree(idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id)
        return snapshot

    monkeypatch.setattr(context.saver.forks.source, "_capture_fork_source_snapshot", capture_then_delete)
    class OwnerProcessExit(BaseException):
        pass

    def crash(**_kwargs: object) -> None:
        raise OwnerProcessExit("模拟 detached fork owner 退出")

    async def crash_before_publish(*_args: object) -> None:
        raise OwnerProcessExit("模拟 detached fork owner 退出")

    # 公开入口冻结规范化意图；源删除后在真实 journal 边界模拟进程退出。
    with monkeypatch.context() as failure:
        if crash_state == "target_committed":
            failure.setattr(context.saver._storage.forks, "complete_fork_materialization", crash)
        else:
            failure.setattr(context.forks, "_publish_prepared_fork", crash_before_publish)
        with pytest.raises(OwnerProcessExit, match="detached fork owner"):
            await context.forks.fork(request)
    record = context.sessions.path_resolver.catalog_store.get_creation_record(
        context.forks._creation_key(request.idempotency_key)
    )
    assert record.state == "preparing"
    production_fork_container.workspace_activity_service.store.close()
    production_fork_container.session_catalog.close()
    restarted = _build_owned_container(context.workspace, monkeypatch)
    _isolate_non_navigation_services(restarted, monkeypatch)
    async with main.lifespan(main.app):
        durable = restarted.session_service.path_resolver.catalog_store.get_creation_record(
            record.session_creation_idempotency_key
        )
        assert durable.state == "published"
        assert (await restarted.session_service.list(limit=100)).total == 1
        assert (await restarted.session_service.get(record.session_id)).context_source_session_id == source.session_id
        restored = await _get_tuple(_production_fork_context(restarted), record.session_id)
        assert restored is not None
        assert [message.id for message in restored.checkpoint["channel_values"]["messages"]] == [
            "retry-notice", "retry-user", "retry-call", "retry-result", "retry-final",
        ]
        recovered = _production_fork_context(restarted)
        target_thread = _main_thread_id(recovered, record.session_id)
        accepted = recovered.saver.accept_turn(
            record.session_id, thread_id=target_thread, accepted_ingress_id="restart-successor-input",
            acceptance_idempotency_key="restart-successor-accept", job_id="restart-successor-job",
            admission_idempotency_key="restart-successor-admission", payload="恢复后的目标新问题",
        )
        prepared = fork_provider_prepare(
            recovered, record.session_id, str(accepted["initial_execution_id"]),
            ContextSourceManager(owner=ContextSourceOwnerKey(record.session_id, target_thread), control_state_port=recovered.saver),
            "restart-successor", attempt=1,
        )
        assert all(message.id != "retry-notice" for message in prepared["messages"])
        assert all(entry.ref.ref_id != "item-retry-notice" for entry in prepared["snapshot"].selection if entry.included and entry.ref.ref_type == "canonical_item")
        assert [message.tool_call_id for message in prepared["messages"] if isinstance(message, ToolMessage)] == ["retry-tool-call"]
        assert any(isinstance(message, HumanMessage) and message.content == "恢复后的目标新问题" for message in prepared["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "full_rollout_copy"])
@pytest.mark.parametrize("fault", ["lineage_digest", "lineage_members", "snapshot_hash"])
async def test_fork_empty_activation_rejects_corruption_before_publication(
    fork_context: ForkIntegrationContext, fork_provider_prepare, mode: SessionForkMode, fault: str,
) -> None:
    source = await fork_context.sessions.create(SessionCreateRequest(title="空 activation 来源"))
    saver = fork_context.saver
    source_thread = _main_thread_id(fork_context, source.session_id)
    accepted = saver.accept_turn(
        source.session_id, thread_id=source_thread, accepted_ingress_id="empty-activation-input",
        acceptance_idempotency_key="empty-activation-accept", job_id="empty-activation-job",
        admission_idempotency_key="empty-activation-admission", payload="核验空资源快照",
    )
    prepared = fork_provider_prepare(
        fork_context, source.session_id, str(accepted["initial_execution_id"]),
        ContextSourceManager(owner=ContextSourceOwnerKey(source.session_id, source_thread), control_state_port=saver),
        "empty-activation", attempt=1,
    )
    activation = saver.load_sealed_resource_activation(
        source.session_id, thread_id=source_thread, assembly_id=prepared["assembly_id"],
        plan_hash=prepared["snapshot"].plan_hash, request_hash=prepared["snapshot"].request_hash,
    )
    assert activation.bindings == ()
    source_user = next(message for message in prepared["messages"]
                       if isinstance(message, HumanMessage) and message.content == "核验空资源快照")
    admission = saver.execution_admission(
        source.session_id, thread_id=source_thread, execution_id=str(accepted["initial_execution_id"]),
    )
    with (
        session_thread_owner_scope(MutationIntentOwner(source.session_id, source_thread), langgraph_thread_id=source_thread),
        execution_admission_scope(admission, langgraph_thread_id=source_thread),
    ):
        completed_messages = [
                source_user, AIMessage(content="空快照来源完成", id="empty-activation-final",
                                       response_metadata={"model_call_id": prepared["snapshot"].model_call_id}),
            ]
        commit_fixture_output(saver, messages=completed_messages)
        await saver.aput(
            build_checkpoint_config(source_thread), _checkpoint("empty-activation-completed", completed_messages),
            {"source": "loop", "step": 1, "parents": {}}, {"messages": "2", "counter": "2"},
        )
    request = await _fork_request(fork_context, source.session_id, creation_kind="new_session_main", mode=mode)
    healthy = await fork_context.forks.fork(request)
    assert saver.load_sealed_resource_activation(
        healthy.session.session_id, thread_id=healthy.thread_id, assembly_id=prepared["assembly_id"],
        plan_hash=prepared["snapshot"].plan_hash, request_hash=prepared["snapshot"].request_hash,
    ) == activation
    source_root = _main_thread_node(fork_context, source.session_id)
    source_jsonl = (source_root / "rollout/rollout.jsonl").read_bytes()
    with sqlite3.connect(source_root / "rollout/index.sqlite") as connection:
        lineage_key, = connection.execute(
            "SELECT lineage_detail_ref FROM resource_activation_snapshots WHERE activation_snapshot_id=?",
            (activation.activation_snapshot_id,),
        ).fetchone()
        if fault == "lineage_members":
            ref = detail_ref_from_key(lineage_key)
            path = source_root / detail_relative_path(ref)
            envelope = json.loads(path.read_bytes())
            manifest = envelope["detail"]
            assert manifest["lineages"] == []
            manifest["lineages"] = [{
                "resource_id": "skill:undeclared", "activation_ordinal": 0,
                "source_lineage_ref": [], "source_lineage_digest": sha256_jcs([]), "members": [],
            }]
            envelope["source_revision"] = envelope["detail_content_hash"] = sha256_jcs(manifest)
            envelope["length"] = len(canonical_json_bytes(manifest))
            raw = canonical_json_bytes(envelope)
            _, record = parse_detail_payload(raw, detail_ref=ref)
            path.write_bytes(raw)
            connection.execute(
                "UPDATE context_plan_details SET source_revision=?,content_length=?,content_hash=? WHERE detail_ref=?",
                (record.source_revision, record.length, record.content_hash, detail_ref_key(ref)),
            )
            connection.execute(
                "UPDATE resource_activation_snapshots SET lineage_manifest_digest=? WHERE activation_snapshot_id=?",
                (sha256_jcs(manifest), activation.activation_snapshot_id),
            )
        elif fault == "lineage_digest":
            connection.execute(
                "UPDATE resource_activation_snapshots SET lineage_manifest_digest=? WHERE activation_snapshot_id=?",
                (sha256_jcs({"wrong": "lineage"}), activation.activation_snapshot_id),
            )
        else:
            connection.execute(
                "UPDATE resource_activation_snapshots SET bindings_hash=? WHERE activation_snapshot_id=?",
                (sha256_jcs({"wrong": "bindings"}), activation.activation_snapshot_id),
            )
    with pytest.raises(ResourceActivationStoreError) as error:
        await fork_context.forks.fork(request.model_copy(update={"idempotency_key": "empty-activation-corrupt-" + fault}))
    assert error.value.code == HASH_MISMATCH_CODE
    assert (await fork_context.sessions.list(limit=100)).total == 2
    assert _retention_refs(fork_context, source.session_id) == []
    assert not tuple(fork_context.sessions.path_resolver.sessions_root.rglob(".fork-staging-*"))
    assert (source_root / "rollout/rollout.jsonl").read_bytes() == source_jsonl


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
@pytest.mark.parametrize("fault", ["missing_call", "null_assembly", "missing_assembly", "item_execution", "assembly_execution"])
async def test_imported_fork_rejects_missing_or_misbound_model_call(
    fork_context: ForkIntegrationContext, fork_provider_prepare, mode: SessionForkMode, fault: str,
) -> None:
    original, retry_id = await _seed_retry_tool_source(fork_context, fork_provider_prepare)
    imported = await fork_context.forks.fork(await _fork_request(
        fork_context, original.session_id, creation_kind="new_session_main", mode="full_rollout_copy",
    ))
    source_root = _main_thread_node(fork_context, imported.session.session_id)
    source_jsonl = (source_root / "rollout/rollout.jsonl").read_bytes()
    request = await _fork_request(fork_context, imported.session.session_id, creation_kind="new_session_main", mode=mode)
    with sqlite3.connect(source_root / "rollout/index.sqlite") as connection:
        call_id, session_id, thread_id = connection.execute(
            "SELECT model_call_id,session_id,thread_id FROM model_calls WHERE execution_id=?", (retry_id,),
        ).fetchone()
        assert session_id == original.session_id and (session_id, thread_id) != (imported.session.session_id, imported.thread_id)
        if fault == "missing_call":
            assert connection.execute("DELETE FROM model_calls WHERE model_call_id=?", (call_id,)).rowcount == 1
            assert connection.execute("SELECT model_call_id FROM model_calls WHERE model_call_id=?", (call_id,)).fetchone() is None
        elif fault == "null_assembly":
            assert connection.execute("UPDATE model_calls SET assembly_id=NULL WHERE model_call_id=?", (call_id,)).rowcount == 1
            assert connection.execute("SELECT assembly_id FROM model_calls WHERE model_call_id=?", (call_id,)).fetchone() == (None,)
        elif fault == "missing_assembly":
            assert connection.execute("UPDATE model_calls SET assembly_id='missing-fork-assembly' WHERE model_call_id=?", (call_id,)).rowcount == 1
            assert connection.execute("SELECT assembly_id FROM model_calls WHERE model_call_id=?", (call_id,)).fetchone() == ("missing-fork-assembly",)
            assert connection.execute("SELECT assembly_id FROM context_assemblies WHERE assembly_id='missing-fork-assembly'").fetchone() is None
        elif fault == "item_execution":
            # E1/E2 都是同 Turn 的真实历史 execution，仅 producer 边被错绑。
            changed = connection.execute(
                "UPDATE item_catalog SET execution_id='execution-retry-initial' "
                "WHERE json_extract(projection_manifest_json,'$.projection_identity.model_call_id')=?", (call_id,),
            )
            bound = connection.execute(
                "SELECT execution_id FROM item_catalog "
                "WHERE json_extract(projection_manifest_json,'$.projection_identity.model_call_id')=?", (call_id,),
            ).fetchall()
            assert changed.rowcount == len(bound) and bound and set(bound) == {("execution-retry-initial",)}
        else:
            initial_assembly, = connection.execute(
                "SELECT assembly_id FROM model_calls WHERE execution_id='execution-retry-initial'",
            ).fetchone()
            assert connection.execute("UPDATE model_calls SET assembly_id=? WHERE model_call_id=?", (initial_assembly, call_id)).rowcount == 1
            assert connection.execute(
                "SELECT model_calls.assembly_id,context_assemblies.execution_id FROM model_calls "
                "JOIN context_assemblies USING(assembly_id) WHERE model_calls.model_call_id=?", (call_id,),
            ).fetchone() == (initial_assembly, "execution-retry-initial")
        source_index_state = tuple(connection.execute(query).fetchall() for query in (
            "SELECT * FROM model_calls ORDER BY model_call_id",
            "SELECT * FROM context_assemblies ORDER BY assembly_id",
            "SELECT * FROM item_catalog ORDER BY item_id",
        ))
    with pytest.raises(ValueError, match="source-mismatch: canonical item.*ModelCall"):
        await fork_context.forks.fork(request)
    assert (await fork_context.sessions.list(limit=100)).total == 2
    assert _retention_refs(fork_context, imported.session.session_id) == []
    assert not tuple(fork_context.sessions.path_resolver.sessions_root.rglob(".fork-staging-*"))
    assert (source_root / "rollout/rollout.jsonl").read_bytes() == source_jsonl
    with sqlite3.connect(source_root / "rollout/index.sqlite") as connection:
        assert tuple(connection.execute(query).fetchall() for query in (
            "SELECT * FROM model_calls ORDER BY model_call_id",
            "SELECT * FROM context_assemblies ORDER BY assembly_id",
            "SELECT * FROM item_catalog ORDER BY item_id",
        )) == source_index_state


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
async def test_fork_preserves_failed_call_retry_reusing_one_assembly(
    fork_context: ForkIntegrationContext, fork_provider_prepare, mode: SessionForkMode,
) -> None:
    source = await fork_context.sessions.create(SessionCreateRequest(title="共享封存 assembly 重试"))
    saver = fork_context.saver
    thread_id = _main_thread_id(fork_context, source.session_id)
    accepted = saver.accept_turn(
        source.session_id, thread_id=thread_id, accepted_ingress_id="shared-assembly-user",
        acceptance_idempotency_key="shared-assembly-accept", job_id="shared-assembly-job",
        admission_idempotency_key="shared-assembly-admission", payload="复用失败调用的封存请求",
    )
    execution_id = str(accepted["initial_execution_id"])
    prepared = fork_provider_prepare(
        fork_context, source.session_id, execution_id,
        ContextSourceManager(owner=ContextSourceOwnerKey(source.session_id, thread_id), control_state_port=saver),
        "shared-assembly-first", attempt=1, outcome="failed",
    )
    first_call = prepared["snapshot"].model_call_id
    retry_call = "shared-assembly-retry-call"
    saver.register_model_call(
        source.session_id, thread_id=thread_id, execution_id=execution_id, model_call_id=retry_call,
        attempt=2, provider="fork-provider", retry_of_model_call_id=first_call, assembly_id=prepared["assembly_id"],
    )
    saver.update_model_call_outcome(
        source.session_id, thread_id=thread_id, model_call_id=retry_call, outcome="completed", dispatch_state="completed",
    )
    user = next(message for message in prepared["messages"] if isinstance(message, HumanMessage))
    admission = saver.execution_admission(source.session_id, thread_id=thread_id, execution_id=execution_id)
    with (
        session_thread_owner_scope(MutationIntentOwner(source.session_id, thread_id), langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        completed_messages = [
                user, AIMessage(content="重试成功", id="shared-assembly-final", response_metadata={"model_call_id": retry_call}),
            ]
        commit_fixture_output(saver, messages=completed_messages)
        await saver.aput(
            build_checkpoint_config(thread_id), _checkpoint("shared-assembly-completed", completed_messages), {"source": "loop", "step": 1, "parents": {}}, {"messages": "2", "counter": "2"},
        )
    expected_calls = [
        (first_call, prepared["assembly_id"], None, execution_id, "failed", "failed"),
        (retry_call, prepared["assembly_id"], first_call, execution_id, "completed", "completed"),
    ]
    with saver._storage.storage.read_connection(source.session_id, thread_id, "") as connection:
        assert connection.execute(
            "SELECT model_call_id,assembly_id,retry_of_model_call_id,execution_id,outcome,dispatch_state "
            "FROM model_calls ORDER BY attempt",
        ).fetchall() == expected_calls
    target = await fork_context.forks.fork(await _fork_request(
        fork_context, source.session_id, creation_kind="new_session_main", mode=mode,
    ))
    with saver._storage.storage.read_connection(target.session.session_id, target.thread_id, "") as connection:
        rows = connection.execute(
            "SELECT model_call_id,assembly_id,retry_of_model_call_id,execution_id,outcome,dispatch_state "
            "FROM model_calls ORDER BY attempt",
        ).fetchall()
        assert rows == expected_calls
        pointer, snapshot_json = connection.execute(
            "SELECT model_call_id,snapshot_json FROM context_assemblies WHERE assembly_id=?", (prepared["assembly_id"],),
        ).fetchone()
        assert pointer == retry_call and json.loads(snapshot_json)["model_call_id"] == first_call
    restored = await _get_tuple(fork_context, target.session.session_id)
    assert restored is not None
    assert [message.content for message in restored.checkpoint["channel_values"]["messages"]] == [user.content, "重试成功"]


@pytest.mark.asyncio
async def test_turn_finalize_keeps_each_provider_assembly_outcome_in_full_fork(
    fork_context: ForkIntegrationContext, fork_provider_prepare,
) -> None:
    source = await fork_context.sessions.create(SessionCreateRequest(title="独立封存 assembly 的失败重试"))
    saver = fork_context.saver
    thread_id = _main_thread_id(fork_context, source.session_id)
    accepted = saver.accept_turn(
        source.session_id, thread_id=thread_id, accepted_ingress_id="distinct-assembly-user",
        acceptance_idempotency_key="distinct-assembly-accept", job_id="distinct-assembly-job",
        admission_idempotency_key="distinct-assembly-admission", payload="保留各次请求的真实结果",
    )
    execution_id = str(accepted["initial_execution_id"])
    manager = ContextSourceManager(owner=ContextSourceOwnerKey(source.session_id, thread_id), control_state_port=saver)
    first = fork_provider_prepare(fork_context, source.session_id, execution_id, manager,
                                  "distinct-assembly-failed", attempt=1, outcome="failed")
    retry = fork_provider_prepare(fork_context, source.session_id, execution_id, manager,
                                  "distinct-assembly-retry", attempt=2)
    assert first["assembly_id"] != retry["assembly_id"]
    user = next(message for message in retry["messages"] if isinstance(message, HumanMessage))
    admission = saver.execution_admission(source.session_id, thread_id=thread_id, execution_id=execution_id)
    with (
        session_thread_owner_scope(MutationIntentOwner(source.session_id, thread_id), langgraph_thread_id=thread_id),
        execution_admission_scope(admission, langgraph_thread_id=thread_id),
    ):
        completed_messages = [
                user, AIMessage(content="第二次请求成功", id="distinct-assembly-final",
                                response_metadata={"model_call_id": retry["snapshot"].model_call_id}),
            ]
        commit_fixture_output(saver, messages=completed_messages)
        await saver.aput(
            build_checkpoint_config(thread_id), _checkpoint("distinct-assembly-completed", completed_messages), {"source": "loop", "step": 1, "parents": {}}, {"messages": "2", "counter": "2"},
        )
    target = await fork_context.forks.fork(await _fork_request(
        fork_context, source.session_id, creation_kind="new_session_main", mode="full_rollout_copy",
    ))
    for session_id, owner_thread in ((source.session_id, thread_id), (target.session.session_id, target.thread_id)):
        with saver._storage.storage.read_connection(session_id, owner_thread, "") as connection:
            assert connection.execute(
                "SELECT model_call_id,execution_id,outcome,dispatch_state FROM model_calls ORDER BY attempt",
            ).fetchall() == [
                (first["snapshot"].model_call_id, execution_id, "failed", "failed"),
                (retry["snapshot"].model_call_id, execution_id, "completed", "completed"),
            ]
            assert dict(connection.execute(
                "SELECT assembly_id,outcome FROM context_assemblies WHERE status='terminal'",
            )) == {first["assembly_id"]: "failed", retry["assembly_id"]: "completed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["context_fork", "view_prefix_fork", "full_rollout_copy"])
async def test_fork_of_imported_internal_lineage_uses_local_committed_proof(
    fork_provider_prepare,
    production_fork_container: AppContainer, mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _production_fork_context(production_fork_container)
    payload = {"content": "原 Turn 的受保护资源正文"}
    registry = ResourceRegistry()
    registry.publish(ResourceSnapshot(
        resource_id="skill:refork-turn", resource_kind="skills", facet="activation",
        display_uri="boxteam://workspace/test/resources/skills/refork-turn/SKILL.md",
        revision=sha256_jcs(payload), payload=payload,
        source_lineage=(("refork-turn", "refork-turn-revision"),), generation=1,
    ))
    source, retry_id = await _seed_retry_tool_source(
        context, fork_provider_prepare, registry=registry,
        activation_policy=ResourceActivationPolicySnapshot(
            default_boundary="model_call", boundaries={"skills": "turn"},
        ),
    )
    source_thread = _main_thread_id(context, source.session_id)
    first_request = await _fork_request(context, source.session_id, creation_kind="new_session_main", mode=mode)
    with context.saver._storage.storage.read_connection(source.session_id, source_thread) as connection:
        expected_visible = connection.execute(
            "SELECT item_id,logical_item_ordinal FROM context_view_items "
            "WHERE view_id=? AND visible=1 ORDER BY logical_item_ordinal",
            (first_request.source_view_id,),
        ).fetchall()
        keys = connection.execute(
            "SELECT activation_snapshot_id FROM resource_activation_snapshots WHERE execution_id=?",
            (retry_id,),
        ).fetchall()
        final_assemblies = connection.execute(
            "SELECT assembly_id,plan_hash,request_hash FROM context_assemblies WHERE execution_id=?",
            (retry_id,),
        ).fetchall()
    if first_request.anchor_item_id is not None:
        cutoff = next(ordinal for item_id, ordinal in expected_visible if item_id == first_request.anchor_item_id)
        expected_visible = [row for row in expected_visible if row[1] <= cutoff]
    snapshots = [context.saver.read_resource_activation_snapshot(
        source.session_id, thread_id=source_thread, activation_snapshot_id=key,
    ) for (key,) in keys]
    execution = next(snapshot for snapshot in snapshots if snapshot.snapshot_kind == "execution")
    model_call = next(snapshot for snapshot in snapshots if snapshot.snapshot_kind == "model_call")
    assert model_call.parent == execution and model_call.bindings == execution.bindings
    assert len(model_call.bindings) == 1
    binding = model_call.bindings[0]
    assert binding.detail_ref is not None
    original = context.saver.read_resource_activation_snapshot(
        source.session_id, thread_id=source_thread, activation_snapshot_id=binding.detail_ref.assembly_id,
    )
    assert original.execution_id == "execution-retry-initial"
    assert original.turn_id == execution.turn_id and original.bindings == execution.bindings
    assert binding.detail_ref.assembly_id not in {execution.activation_snapshot_id, model_call.activation_snapshot_id}
    assert len(final_assemblies) == 1
    final_assembly = final_assemblies[0]

    def reject_runtime_lineage(**kwargs: object) -> None:
        raise AssertionError("fork stage 不得回读 runtime lineage")

    with monkeypatch.context() as patch:
        patch.setattr(context.saver._activation_store()._lineage_body, "read_lineage_manifest", reject_runtime_lineage)
        first = (await context.forks.fork(first_request)).session
    first_checkpoint = await _get_tuple(context, first.session_id)
    assert first_checkpoint is not None
    with sqlite3.connect(_main_thread_node(context, first.session_id) / "rollout" / "index.sqlite") as connection:
        head = connection.execute(
            "SELECT head_checkpoint_id FROM branches WHERE branch_id = "
            "(SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = '')"
        ).fetchone()
        first_view = context.saver._storage.storage._active_context_view_id(connection, "")
        assert first_view is not None
        assert connection.execute(
            "SELECT item_id,logical_item_ordinal FROM context_view_items "
            "WHERE view_id=? AND visible=1 ORDER BY logical_item_ordinal", (first_view,),
        ).fetchall() == expected_visible
    assert head == (first_checkpoint.checkpoint["id"],)
    assert first_checkpoint.checkpoint["id"] != "cp-retry-final"
    await context.sessions.path_resolver.delete_subtree(idempotency_key=f"fork-delete-{source.session_id}", root_node_id=source.session_id)
    second_request = await _fork_request(context, first.session_id, creation_kind="new_session_main", mode="context_fork")
    cutoff = next(ordinal for item_id, ordinal in expected_visible if item_id == second_request.anchor_item_id)
    expected_visible = [row for row in expected_visible if row[1] <= cutoff]
    with monkeypatch.context() as patch:
        patch.setattr(context.saver._activation_store()._lineage_body, "read_lineage_manifest", reject_runtime_lineage)
        second = (await context.forks.fork(second_request)).session
    assert second.context_source_session_id == first.session_id
    restored = await _get_tuple(context, second.session_id)
    assert restored is not None
    assert [message.id for message in restored.checkpoint["channel_values"]["messages"]] == [
        "retry-notice", "retry-user", "retry-call", "retry-result", "retry-final",
    ]
    with sqlite3.connect(_main_thread_node(context, second.session_id) / "rollout" / "index.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone() == (3,)
        assert connection.execute("SELECT COUNT(*) FROM turn_records").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM item_catalog WHERE execution_id NOT IN (SELECT execution_id FROM executions)"
        ).fetchone() == (0,)
    await context.sessions.path_resolver.delete_subtree(
        idempotency_key=f"fork-delete-{first.session_id}", root_node_id=first.session_id,
    )
    second_thread = _main_thread_id(context, second.session_id)
    with RolloutCheckpointSaver(context.sessions.path_resolver.sessions_root) as cold:
        activation = cold.load_sealed_resource_activation(
            second.session_id, thread_id=second_thread, assembly_id=final_assembly[0],
            plan_hash=final_assembly[1], request_hash=final_assembly[2],
        )
        assert activation == model_call
        sealed = cold.get_context_assembly(
            second.session_id, second_thread, assembly_id=final_assembly[0],
        )
        expected_resources = project_sealed_resource_refs(model_call)
        for surface in ("messages", "history", "native", "provider"):
            project = getattr(cold, f"project_context_plan_to_{surface}_with_evidence")
            result = project(
                second.session_id, second_thread, sealed.as_sealed_plan(),
                **({"target_format": sealed.target_format} if surface == "provider" else {}),
            )
            assert result[-1].resource_refs == expected_resources, surface
            assert result[-1].assembly_id == sealed.assembly_id
            if surface == "native":
                assert result[0]["resource_refs"] == [ref.model_dump(mode="json") for ref in expected_resources]
        assert cold.read_resource_activation_payload(
            activation, activation.bindings[0], storage_owner=(second.session_id, second_thread),
        ) == payload
        with cold._storage.storage.read_connection(second.session_id, second_thread) as connection:
            second_view = cold._storage.storage._active_context_view_id(connection, "")
            assert second_view is not None
            assert connection.execute(
                "SELECT item_id,logical_item_ordinal FROM context_view_items "
                "WHERE view_id=? AND visible=1 ORDER BY logical_item_ordinal", (second_view,),
            ).fetchall() == expected_visible
        accepted = cold.accept_turn(
            second.session_id, thread_id=second_thread, accepted_ingress_id="refork-successor-input",
            acceptance_idempotency_key="refork-successor-accept", job_id="refork-successor-job",
            admission_idempotency_key="refork-successor-admission", payload="删源副本继续聊天",
        )
        prepared = fork_provider_prepare(
            replace(context, saver=cold), second.session_id, str(accepted["initial_execution_id"]),
            ContextSourceManager(owner=ContextSourceOwnerKey(second.session_id, second_thread), control_state_port=cold),
            "refork-successor", attempt=1,
        )
        assert (prepared["snapshot"].session_id, prepared["snapshot"].thread_id) == (second.session_id, second_thread)
        assert all(message.id != "retry-notice" for message in prepared["messages"])
        assert all(entry.ref.ref_id != "item-retry-notice" for entry in prepared["snapshot"].selection if entry.included and entry.ref.ref_type == "canonical_item")
        assert any(isinstance(message, HumanMessage) and message.content == "检查工作区" for message in prepared["messages"])
        assert any(isinstance(message, ToolMessage) and message.content == "文件内容" for message in prepared["messages"])
        assert any(isinstance(message, HumanMessage) and message.content == "删源副本继续聊天" for message in prepared["messages"])
