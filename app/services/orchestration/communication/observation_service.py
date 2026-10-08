"""持久冷观察：冻结分页、短生命周期事务和删除后的定点只读恢复。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import secrets
import time
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.exceptions import NotFoundError
from app.core.session_control_communication_ledger.observations import (
    RemoteObservation,
    RemoteObservationError,
    canonical_json,
    observation_error_code,
)
from app.core.session_control_primitives import CONTROL_DATABASE_NAME
from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import (
    SESSION_OPERATION_LEASE_TERMINAL_STATES,
    OperationHolderGate,
    SessionDeletionPendingError,
    SessionLifecycleGate,
    SessionOperationLease,
)
from app.core.trace_middleware import require_current_gateway_id
from app.schemas.internal_v2.communication import CommunicationObservationRequest
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextReadResultDTO,
)
from app.services.business.communication.addresses import GlobalThreadAddress
from app.services.business.communication.wait import (
    DurableDeadline,
    WaitObservation,
    WaitSelector,
    WaitSelectorKind,
    WaitState,
    WaitUntil,
    freeze_wait_deadline,
    remaining_wait_budget_seconds,
    resolve_wait_status,
)
from app.services.business.session_context_projection import SessionContextReadSnapshot
from app.services.business.session_context_query_service import (
    SessionContextQueryService,
)
from app.services.business.session_context_resource import (
    parse_session_context_ref,
    session_resource_identity,
    session_resource_uri,
    set_exact_returned_chars,
)

if TYPE_CHECKING:
    from app.schemas.event import Event
    from app.services.orchestration.communication.service import (
        SessionCommunicationService,
    )

RECOVERY_WINDOW = timedelta(days=1)
_DELETED = (SessionDeletionPendingError, NotFoundError)


class SystemWaitClock:
    def __init__(self) -> None:
        boot_file = Path("/proc/sys/kernel/random/boot_id")
        self.origin_id = boot_file.read_text().strip() if boot_file.exists() else "process-" + uuid4().hex

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()

    def utc_now(self) -> datetime:
        return datetime.now(UTC)


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _page_identity(source: GlobalThreadAddress, source_call_id: str) -> str:
    return "page_" + _hash([asdict(source), source_call_id])


def _read_parameters(query: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in query.items() if key not in {"cursor", "revision"}}


def _lease(store: SessionControlStore, identity: str, preimage: object) -> SessionOperationLease:
    previous = store.find_lease_by_operation(identity)
    lease = store.create_or_get_lease(operation_kind="remote_observation", operation_identity=identity, preimage_hash=_hash(preimage), recovery_ref=identity)
    if previous is not None and lease.state in {"active", "settling"}:
        # 调用方已独占同一 OS holder 锁；旧 holder 停止后才能换 durable token。
        lease = store.takeover_lease(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
    if lease.state == "active":
        lease = store.mark_lease_settling(lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token)
    return lease


def _assert_current(store: SessionControlStore, lease: SessionOperationLease, *, allow_completed: bool = False) -> SessionOperationLease:
    try:
        current = store.get_lease(lease.lease_id)
    except KeyError:
        raise RemoteObservationError("operation-retry-expired") from None
    if current.fencing_token != lease.fencing_token or current.state not in ({"settling", "completed"} if allow_completed else {"settling"}):
        raise RemoteObservationError("operation-retry-expired")
    return current


def _settle(store: SessionControlStore, lease: SessionOperationLease, *, outcome: str = "completed") -> None:
    try:
        current = store.get_lease(lease.lease_id)
    except KeyError:
        raise RemoteObservationError("operation-retry-expired") from None
    if current.fencing_token != lease.fencing_token:
        raise RemoteObservationError("operation-retry-expired")
    if current.state in SESSION_OPERATION_LEASE_TERMINAL_STATES:
        return
    if current.state == "active":
        current = store.mark_lease_settling(lease_id=current.lease_id, expected_fencing_token=current.fencing_token)
    store.settle_lease(lease_id=current.lease_id, expected_fencing_token=current.fencing_token, outcome=outcome)


def _terminalize(store: SessionControlStore, identity: str) -> None:
    lease = store.find_lease_by_operation(identity)
    if lease is not None:
        # 删除 owner 已提交真实取消证明；原 token 终态即可阻断晚回调，不冒称 holder 失效。
        _settle(store, lease, outcome="cancelled")


def _observation_identities(store: SessionControlStore, observation: RemoteObservation) -> tuple[str, ...]:
    return (observation.side + "-observation|" + observation.observation_id, *(observation.side + "-page|" + page.page_identity for page in store.list_remote_pages(observation.observation_id, side=observation.side)))


def _all_terminal(store: SessionControlStore, identities: tuple[str, ...]) -> bool:
    for identity in identities:
        lease = store.find_lease_by_operation(identity)
        if lease is None or lease.state not in SESSION_OPERATION_LEASE_TERMINAL_STATES:
            return False
        if lease.operation_kind != "remote_observation":
            raise RuntimeError("observation 引用了其它 operation kind 的 lease")
    return True


def _result(value: dict[str, object]) -> dict[str, object]:
    code = observation_error_code(value.get("error"))
    if code is not None:
        raise RemoteObservationError(code)
    return value


class CommunicationObservationService:
    def __init__(self, communication: SessionCommunicationService, query: SessionContextQueryService) -> None:
        self._communication = communication
        self._query = query
        self._clock = SystemWaitClock()
        self._changed = asyncio.Condition()
        self._change_revision = 0
        self._gc_changed = asyncio.Event()
        sessions_root = communication._sessions.path_resolver.sessions_root
        self._source_locks = OperationHolderGate(sessions_root, namespace="observation_source")
        self._target_locks = OperationHolderGate(sessions_root, namespace="observation_target")
        self._recoveries: dict[tuple[str, str, str], asyncio.Task[None]] = {}

    def _gate(self) -> SessionLifecycleGate:
        return SessionLifecycleGate(self._communication._sessions.path_resolver.sessions_root)

    @asynccontextmanager
    async def _hold(self, side: str, observation_id: str) -> AsyncIterator[None]:
        locks = self._source_locks if side == "source" else self._target_locks
        try:
            async with locks.lock_for(observation_id):
                yield
        finally:
            # 真实 owner 完成或停止后唤醒 GC/恢复；不把过去期限当成零等待定时器。
            self._gc_changed.set()

    def _replay_store(self, session_id: str) -> SessionControlStore:
        resolver = self._communication._sessions.path_resolver
        path = resolver.resolve_operation_replay_node(session_id, now=self._clock.utc_now())
        return SessionControlStore(path / CONTROL_DATABASE_NAME, read_only=True)

    async def notify(self) -> None:
        async with self._changed:
            self._change_revision += 1
            self._changed.notify_all()

    async def on_event(self, event: Event) -> None:
        if event.type in {"job_created", "job_started", "job_completed", "job_failed", "job_cancelled", "status_change"}:
            await self.notify()

    @staticmethod
    def _encode_token(store: SessionControlStore, source: GlobalThreadAddress, value: dict[str, object], *, prefix: str) -> str:
        nonce = secrets.token_bytes(12)
        raw = zlib.compress(canonical_json(value).encode())
        encrypted = AESGCM(store.observation_cursor_key(create=True)).encrypt(nonce, raw, canonical_json([prefix, asdict(source)]).encode())
        return prefix + base64.urlsafe_b64encode(nonce + encrypted).decode()

    def _decode_token(self, store: SessionControlStore, source: GlobalThreadAddress, cursor: str, *, prefix: str, recovery: bool = False) -> dict[str, object]:
        try:
            if not cursor.startswith(prefix):
                raise ValueError("cursor 版本错误")
            encoded = base64.b64decode(cursor[len(prefix):].encode(), altchars=b"-_", validate=True)
            raw = AESGCM(store.observation_cursor_key()).decrypt(encoded[:12], encoded[12:], canonical_json([prefix, asdict(source)]).encode())
            value = json.loads(zlib.decompress(raw))
            fields = {"o", "t", "r", "u", "p", "b", "k", "n", "c", "e"}
            if prefix == "rc1.":
                fields.add("f")
            if not isinstance(value, dict) or set(value) != fields:
                raise TypeError("cursor 结构错误")
            if not all(isinstance(value[key], str) for key in ("o", "r", "p", "e")):
                raise TypeError("cursor identity 结构错误")
            if type(value["n"]) is not int or value["n"] < 1 or type(value["c"]) is not int or value["c"] < 0:
                raise TypeError("cursor ordinal/预算结构错误")
            if not isinstance(value["k"], str) or not isinstance(value["u"], list) or not isinstance(value["b"], list):
                raise TypeError("cursor offset/上界结构错误")
            GlobalThreadAddress(**value["t"])
            if prefix == "rc1." and not isinstance(value["f"], str):
                raise TypeError("cursor 原 page 结构错误")
            expires = datetime.fromisoformat(value["e"])
            if expires.tzinfo is None:
                raise ValueError("cursor 无时区")
        except RemoteObservationError:
            raise
        except (InvalidTag, binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError, zlib.error) as error:
            raise RemoteObservationError("read-snapshot-invalid") from error
        if not recovery and expires <= self._clock.utc_now():
            raise RemoteObservationError("read-snapshot-expired")
        return value

    def _source_cursor(self, store: SessionControlStore, source: GlobalThreadAddress, cursor: str) -> tuple[str, str, int]:
        value = self._decode_token(store, source, cursor, prefix="rc1.")
        observation_id, page_id = cast(str, value["o"]), cast(str, value["f"])
        observation = store.get_remote_observation(observation_id, side="source")
        if observation is None:
            raise RemoteObservationError("read-snapshot-expired")
        page = store.get_remote_page(observation_id, side="source", page_identity=page_id)
        if page.target_result is None or page.target_result.get("binding") != {key: item for key, item in value.items() if key != "f"}:
            raise RemoteObservationError("read-snapshot-mismatch")
        target_page = cast(dict[str, object], page.target_result["result"])
        if not isinstance(target_page.get("next_cursor"), str):
            raise RemoteObservationError("read-snapshot-invalid")
        return observation_id, cast(str, target_page["next_cursor"]), cast(int, value["n"])

    async def _source_call(
        self, *, session_id: str, thread_id: str, source_call_id: str,
        source_call_created_at: datetime, operation: Literal["read", "wait"],
        target_session_id: str, target_workspace_id: str | None, query: dict[str, object],
    ) -> dict[str, object]:
        service = self._communication
        service._check_failure()
        if source_call_created_at.tzinfo is None:
            raise ValueError("source call 创建时刻必须包含时区")
        now = self._clock.utc_now()
        if source_call_created_at + RECOVERY_WINDOW <= now:
            raise RemoteObservationError("operation-retry-expired")
        if source_call_created_at > now + timedelta(seconds=30):
            raise RemoteObservationError("read-snapshot-mismatch")
        source = GlobalThreadAddress(require_current_gateway_id("通信 source"), service._sessions.workspace_id, session_id, thread_id)
        page_id = _page_identity(source, source_call_id)
        observation_id = "obs_" + _hash([asdict(source), source_call_id])
        input_value = {"target_session_id": target_session_id, "target_workspace_id": target_workspace_id, "operation": operation, "query": _read_parameters(query) if operation == "read" else query}
        cursor = query.get("cursor") if operation == "read" else None
        ordinal, target_cursor = 0, None
        if cursor is not None:
            async with self._gate().shared(session_id):
                try:
                    service._sessions.assert_session_active(session_id)
                    store = service._store(session_id)
                except _DELETED:
                    store = self._replay_store(session_id)
                with closing(store):
                    observation_id, target_cursor, ordinal = self._source_cursor(store, source, cast(str, cursor))
        async with self._hold("source", observation_id):
            now = self._clock.utc_now()
            if source_call_created_at + RECOVERY_WINDOW <= self._clock.utc_now():
                raise RemoteObservationError("operation-retry-expired")
            async with self._gate().shared(session_id):
                try:
                    service._sessions.assert_session_active(session_id)
                    service.source_address(session_id, thread_id)
                    store = service._store(session_id)
                    deleted = False
                except _DELETED:
                    store = self._replay_store(session_id)
                    deleted = True
                with closing(store):
                    if cursor is not None:
                        observation_id, target_cursor, ordinal = self._source_cursor(store, source, cast(str, cursor))
                    existing = store.get_remote_observation(observation_id, side="source")
                    if existing is not None:
                        if existing.request["input"] != input_value:
                            raise RemoteObservationError("read-snapshot-mismatch")
                        envelope = CommunicationObservationRequest.model_validate(existing.request["envelope"])
                        if envelope.expires_at <= now:
                            raise RemoteObservationError("operation-retry-expired")
                    elif deleted:
                        raise RemoteObservationError("target-deleted")
                    elif cursor is not None:
                        raise RemoteObservationError("read-snapshot-expired")
                    else:
                        envelope = None
            if envelope is None:
                try:
                    target = (await service.resolve_main(source, target_session_id, target_workspace_id)).target
                except _DELETED as error:
                    raise RemoteObservationError("target-deleted") from error
                frozen_query = {"read": {**query, "resource_identity": session_resource_identity(target.workspace_id, target.session_id), "uri": session_resource_uri(target.workspace_id, target.session_id), "cursor": None}} if operation == "read" else {**query, "deadline": asdict(freeze_wait_deadline(clock=self._clock, timeout_seconds=cast(int, query["timeout_seconds"])))}
                envelope = CommunicationObservationRequest(observation_id=observation_id, source_call_id=source_call_id, page_identity=page_id, page_ordinal=0, expires_at=source_call_created_at + RECOVERY_WINDOW, source=source, target=target, operation=operation, query=frozen_query)
            request_query = envelope.query
            if operation == "read":
                request_query = {"read": {**cast(dict[str, object], envelope.query["read"]), "cursor": target_cursor, "revision": query.get("revision")}}
            request = envelope.model_copy(update={"source_call_id": source_call_id, "page_identity": page_id, "page_ordinal": ordinal, "query": request_query})
            page_request = {"input": query, "envelope": request.model_dump(mode="json")}
            if deleted:
                async with self._gate().shared(session_id):
                    with closing(self._replay_store(session_id)) as store:
                        page = store.get_remote_page(observation_id, side="source", page_identity=page_id)
                        if page.request != page_request or page.result is None:
                            raise RemoteObservationError("target-deleted")
                        previous = page.result
            else:
                try:
                    async with service._sessions.session_write_admission(session_id):
                        with closing(service._store(session_id)) as store:
                            store.admit_remote_observation(observation_id, side="source", request={"input": input_value, "envelope": envelope.model_dump(mode="json")}, expires_at=envelope.expires_at.isoformat())
                            root_lease = _lease(store, "source-observation|" + observation_id, input_value)
                            previous = store.admit_remote_page(observation_id, side="source", page_identity=page_id, page_ordinal=ordinal, request=page_request)
                            page_lease = _lease(store, "source-page|" + page_id, page_request)
                except _DELETED as error:
                    raise RemoteObservationError("target-deleted") from error
                self._gc_changed.set()
            budget = remaining_wait_budget_seconds(DurableDeadline(**cast(dict[str, object], envelope.query["deadline"])), self._clock) if operation == "wait" else None
            # 原结果重放仍经过新授权，source/target gate 与网络等待完全分离。
            try:
                target_result = await service._transport.operation(operation=operation, target=request.target, body=request.model_dump(mode="json"), timeout=max(budget or 0, 0) + 15 if operation == "wait" else None)
            except RemoteObservationError as error:
                if not deleted and previous is None:
                    await self._record_failure(session_id, request, root_lease, page_lease, error, side="source")
                raise
            if deleted:
                return _result(cast(dict[str, object], previous))
            try:
                if previous is not None:
                    async with service._sessions.session_write_admission(session_id):
                        with closing(service._store(session_id)) as store:
                            page = store.get_remote_page(observation_id, side="source", page_identity=page_id)
                            if page.target_result != target_result:
                                raise RemoteObservationError("read-snapshot-mismatch")
                            outcome = "failed" if observation_error_code(previous.get("error")) else "completed"
                            _settle(store, page_lease, outcome=outcome)
                            _settle(store, root_lease, outcome=outcome)
                    if envelope.expires_at <= self._clock.utc_now():
                        raise RemoteObservationError("operation-retry-expired")
                    return _result(previous)
                async with service._sessions.session_write_admission(session_id):
                    with closing(service._store(session_id)) as store:
                        _assert_current(store, page_lease)
                        _assert_current(store, root_lease, allow_completed=True)
                        result = dict(target_result)
                        if operation == "read":
                            result = dict(cast(dict[str, object], target_result["result"]))
                            if result.get("next_cursor") is not None:
                                result["next_cursor"] = self._encode_token(store, source, {**cast(dict[str, object], target_result["binding"]), "f": page_id}, prefix="rc1.")
                            result = set_exact_returned_chars(SessionContextReadResultDTO.model_validate(result), operation="read_context").model_dump(mode="json")
                            if result["returned_chars"] > query["max_chars"]:
                                raise RuntimeError("remote observation cursor 超出冻结 max_chars")
                        store.commit_remote_page(observation_id, side="source", page_identity=page_id, result=result, target_result=target_result)
                        _settle(store, page_lease)
                        _settle(store, root_lease)
                if envelope.expires_at <= self._clock.utc_now():
                    raise RemoteObservationError("operation-retry-expired")
                return _result(result)
            except _DELETED:
                async with self._gate().shared(session_id):
                    with closing(self._replay_store(session_id)) as store:
                        page = store.get_remote_page(observation_id, side="source", page_identity=page_id)
                        if page.request != page_request or page.result is None:
                            raise RemoteObservationError("target-deleted") from None
                        return _result(page.result)

    async def read(self, *, session_id: str, thread_id: str, source_call_id: str, source_call_created_at: datetime, request: SessionContextReadRequest) -> SessionContextReadResultDTO:
        resource = parse_session_context_ref(resource_identity=request.resource_identity, uri=request.uri, scope=request.scope)
        if resource.kind != "session" or resource.session_id is None:
            raise ValueError("通信 read 只接受单个 Session 资源")
        result = await self._source_call(session_id=session_id, thread_id=thread_id, source_call_id=source_call_id, source_call_created_at=source_call_created_at, operation="read", target_session_id=resource.session_id, target_workspace_id=resource.scope_id, query=request.model_dump(mode="json"))
        return SessionContextReadResultDTO.model_validate(result)

    async def wait(self, *, session_id: str, thread_id: str, source_call_id: str, source_call_created_at: datetime, target_session_id: str, selector: WaitSelector | None, until: WaitUntil, timeout_seconds: int) -> dict[str, object]:
        freeze_wait_deadline(clock=self._clock, timeout_seconds=timeout_seconds)
        if until not in {"terminal", "state_change"}:
            raise ValueError("wait until 必须为 terminal|state_change")
        return await self._source_call(session_id=session_id, thread_id=thread_id, source_call_id=source_call_id, source_call_created_at=source_call_created_at, operation="wait", target_session_id=target_session_id, target_workspace_id=None, query={"selector": asdict(selector) if selector is not None else None, "until": until, "timeout_seconds": timeout_seconds})

    @staticmethod
    def _root_request(request: CommunicationObservationRequest) -> dict[str, object]:
        query = request.query
        if request.operation == "read":
            query = {"read": _read_parameters(cast(dict[str, object], request.query["read"]))}
        return {"source": asdict(request.source), "target": asdict(request.target), "operation": request.operation, "query": query, "expires_at": request.expires_at.isoformat()}

    def _binding(self, request: CommunicationObservationRequest, snapshot: SessionContextReadSnapshot, *, inner: str | None, ordinal: int, spent: int) -> dict[str, object]:
        return {
            "o": request.observation_id, "t": asdict(request.target), "r": snapshot.revision,
            "u": [len(snapshot.items), snapshot.raw_message_count, snapshot.effective_record_count, snapshot.compaction_cutoff],
            "p": _hash(_read_parameters(cast(dict[str, object], request.query["read"]))),
            "b": [request.query["read"]["limit"], request.query["read"]["max_chars"]],
            "k": inner, "n": ordinal, "c": spent, "e": request.expires_at.isoformat(),
        }

    def _read_page(self, request: CommunicationObservationRequest, frozen: dict[str, object], store: SessionControlStore, *, recovery: bool = False) -> dict[str, object]:
        snapshot = SessionContextReadSnapshot.model_validate(frozen)
        read = SessionContextReadRequest.model_validate(request.query["read"])
        spent = 0
        if read.cursor is not None:
            binding = self._decode_token(store, request.source, read.cursor, prefix="rt1.", recovery=recovery)
            expected = self._binding(request, snapshot, inner=cast(str, binding.get("k")), ordinal=request.page_ordinal, spent=cast(int, binding.get("c")))
            if binding != expected:
                raise RemoteObservationError("read-snapshot-mismatch")
            read = read.model_copy(update={"cursor": binding["k"]})
            spent = cast(int, binding["c"])
        elif request.page_ordinal != 0:
            raise RemoteObservationError("read-snapshot-mismatch")
        if read.revision is not None and read.revision != snapshot.revision:
            raise RemoteObservationError("read-snapshot-mismatch")
        remaining = read.max_chars - spent
        # 给完整 AEAD 及 source 层不嵌套的 opaque token 留出有界序列化空间。
        try:
            page = snapshot.page(read.model_copy(update={"max_chars": max(remaining - 768, 1)}))
        except ValueError as error:
            raise RemoteObservationError("read-snapshot-mismatch") from error
        spent += page.returned_chars + (768 if page.next_cursor is not None else 0)
        binding = self._binding(request, snapshot, inner=page.next_cursor, ordinal=request.page_ordinal + 1, spent=spent)
        result = page.model_dump(mode="json")
        if page.next_cursor is not None:
            result["next_cursor"] = self._encode_token(store, request.source, binding, prefix="rt1.")
        result = set_exact_returned_chars(SessionContextReadResultDTO.model_validate(result), operation="read_context").model_dump(mode="json")
        if result["returned_chars"] > remaining:
            raise RemoteObservationError("read-snapshot-mismatch")
        return {"result": result, "binding": binding}

    async def _replay_target(self, request: CommunicationObservationRequest) -> dict[str, object]:
        async with self._gate().shared(request.target.session_id):
            with closing(self._replay_store(request.target.session_id)) as store:
                observation = store.get_remote_observation(request.observation_id, side="target")
                if observation is None:
                    raise RemoteObservationError("target-deleted")
                if observation.request != self._root_request(request):
                    raise RemoteObservationError("read-snapshot-mismatch")
                pages = store.list_remote_pages(request.observation_id, side="target")
                page = next((page for page in pages if page.page_identity == request.page_identity), None)
                if page is None:
                    raise RemoteObservationError("target-deleted")
                if page.page_ordinal != request.page_ordinal or page.request != request.model_dump(mode="json"):
                    raise RemoteObservationError("read-snapshot-mismatch")
                if page.result is None:
                    raise RuntimeError("删除 drain 未终结原 observation page")
                return _result(page.result)

    async def observe(self, request: CommunicationObservationRequest) -> dict[str, object]:
        service = self._communication
        target = request.target
        if request.page_identity != _page_identity(request.source, request.source_call_id):
            raise RemoteObservationError("read-snapshot-mismatch")
        if request.expires_at <= self._clock.utc_now():
            raise RemoteObservationError("operation-retry-expired")
        if request.expires_at > self._clock.utc_now() + RECOVERY_WINDOW + timedelta(seconds=30) or target.workspace_id != service._sessions.workspace_id:
            raise RemoteObservationError("read-snapshot-mismatch")
        if request.operation == "read":
            read_query = SessionContextReadRequest.model_validate(request.query["read"])
            if (read_query.resource_identity, read_query.uri, read_query.scope) != (session_resource_identity(target.workspace_id, target.session_id), session_resource_uri(target.workspace_id, target.session_id), "workspace"):
                raise RemoteObservationError("read-snapshot-mismatch")
        async with self._hold("target", request.observation_id):
            if request.expires_at <= self._clock.utc_now():
                raise RemoteObservationError("operation-retry-expired")
            result = await self._observe_target(request)
            if request.expires_at <= self._clock.utc_now():
                raise RemoteObservationError("operation-retry-expired")
            return result

    async def _observe_target(self, request: CommunicationObservationRequest, *, recovery: bool = False) -> dict[str, object]:
        """已持原 holder guard；冷恢复接续原请求，不重新开放外部重试窗口。"""
        service = self._communication
        target = request.target
        read_query = SessionContextReadRequest.model_validate(request.query["read"]) if request.operation == "read" else None
        root_request = self._root_request(request)
        root_lease = page_lease = None
        try:
            async with service._sessions.session_write_admission(target.session_id):
                if not recovery and await service._sessions.resolve_main_thread(target.session_id) != target.thread_id:
                    raise RemoteObservationError("read-snapshot-mismatch")
                with closing(service._store(target.session_id)) as store:
                    prepared = None
                    if not recovery and request.operation == "read" and read_query.cursor is not None:
                        existing = store.get_remote_observation(request.observation_id, side="target")
                        if existing is None or existing.snapshot is None:
                            raise RemoteObservationError("read-snapshot-expired")
                        # 认证先于页准入；坏 cursor 不得占据 ordinal 或留下新 lease。
                        prepared = self._read_page(request, existing.snapshot, store)
                    observation = store.admit_remote_observation(request.observation_id, side="target", request=root_request, expires_at=request.expires_at.isoformat())
                    root_lease = _lease(store, "target-observation|" + request.observation_id, root_request)
                    previous = store.admit_remote_page(request.observation_id, side="target", page_identity=request.page_identity, page_ordinal=request.page_ordinal, request=request.model_dump(mode="json"))
                    page_lease = _lease(store, "target-page|" + request.page_identity, request.model_dump(mode="json"))
                    if recovery and await service._sessions.resolve_main_thread(target.session_id) != target.thread_id:
                        raise RemoteObservationError("read-snapshot-mismatch")
            self._gc_changed.set()
            if previous is not None:
                async with service._sessions.session_write_admission(target.session_id):
                    with closing(service._store(target.session_id)) as store:
                        outcome = "failed" if observation_error_code(previous.get("error")) else "completed"
                        _settle(store, page_lease, outcome=outcome)
                        _settle(store, root_lease, outcome=outcome)
                return _result(previous)
            if request.operation == "read":
                frozen = observation.snapshot
                if frozen is None:
                    async with self._gate().shared(target.session_id):
                        service._sessions.assert_session_active(target.session_id)
                        snapshot = await self._query.freeze_read_context(read_query)
                    frozen = snapshot.model_dump(mode="json")
                    async with service._sessions.session_write_admission(target.session_id):
                        with closing(service._store(target.session_id)) as store:
                            _assert_current(store, root_lease, allow_completed=True)
                            store.commit_remote_observation(request.observation_id, side="target", snapshot=frozen)
                async with self._gate().shared(target.session_id):
                    service._sessions.assert_session_active(target.session_id)
                    with closing(service._store(target.session_id)) as store:
                        result = prepared if prepared is not None else self._read_page(request, frozen, store, recovery=recovery)
            else:
                result = observation.result if observation.result is not None else await self._wait_target(request, observation.snapshot, root_lease)
            async with service._sessions.session_write_admission(target.session_id):
                with closing(service._store(target.session_id)) as store:
                    _assert_current(store, page_lease)
                    _assert_current(store, root_lease, allow_completed=True)
                    store.commit_remote_page(request.observation_id, side="target", page_identity=request.page_identity, result=result)
                    if request.operation == "wait":
                        store.commit_remote_observation(request.observation_id, side="target", result=result)
                    _settle(store, page_lease)
                    _settle(store, root_lease)
            return _result(result)
        except _DELETED:
            return await self._replay_target(request)
        except Exception as error:
            if root_lease is not None and page_lease is not None:
                await self._record_failure(target.session_id, request, root_lease, page_lease, error, side="target")
            raise

    async def _record_failure(
        self, session_id: str, request: CommunicationObservationRequest,
        root_lease: SessionOperationLease, page_lease: SessionOperationLease,
        error: Exception, *, side: str,
    ) -> None:
        code = error.code if isinstance(error, RemoteObservationError) else "read-snapshot-mismatch"
        result = {"error": RemoteObservationError(code).to_wire()}
        if not isinstance(error, RemoteObservationError):
            result["owner_failure"] = {"type": type(error).__name__, "message": str(error)}
        try:
            async with self._communication._sessions.session_write_admission(session_id):
                with closing(self._communication._store(session_id)) as store:
                    try:
                        current = store.get_lease(page_lease.lease_id)
                    except KeyError:
                        raise RemoteObservationError("operation-retry-expired") from None
                    if current.fencing_token != page_lease.fencing_token:
                        raise RemoteObservationError("operation-retry-expired")
                    if current.state in SESSION_OPERATION_LEASE_TERMINAL_STATES:
                        return
                    _assert_current(store, page_lease)
                    root = _assert_current(store, root_lease, allow_completed=True)
                    store.commit_remote_page(request.observation_id, side=side, page_identity=request.page_identity, result=result)
                    outcome = "cancelled" if code == "target-deleted" else "failed"
                    _settle(store, page_lease, outcome=outcome)
                    if root.state != "completed":
                        store.commit_remote_observation(request.observation_id, side=side, result=result)
                        _settle(store, root_lease, outcome=outcome)
        except _DELETED:
            # 原生产删除 callback 已在同一 gate 中提交并终结这些记录。
            return

    def _snapshot(self, request: CommunicationObservationRequest, identities: list[dict[str, str]] | None) -> tuple[list[WaitObservation], list[dict[str, str]]]:
        service = self._communication
        target = request.target
        selector_value = request.query["selector"]
        selector = WaitSelector(**selector_value) if selector_value is not None else None
        with closing(service._store(target.session_id)) as store:
            if identities is None:
                if selector is not None and selector.kind == "communication":
                    store.get_communication_inbox(selector.selector_id)
                    identities = [{"kind": "communication", "id": selector.selector_id}]
                else:
                    rows = service._saver.cold_execution_observations(target.session_id, target.thread_id, job_ids=[selector.selector_id] if selector is not None and selector.kind == "job" else None, turn_id=selector.selector_id if selector is not None and selector.kind == "turn" else None)
                    if selector is not None and not rows:
                        raise ValueError(f"selector_not_found: {selector.kind}={selector.selector_id}")
                    if selector is None:
                        rows = [row for row in rows if row["state"] in {"pending", "running"}]
                    identities = [{"kind": selector.kind if selector is not None else "job", "id": selector.selector_id if selector is not None else row["job_id"], "job_id": row["job_id"], "execution_id": row["execution_id"]} for row in rows]
                    if selector is None:
                        identities.extend({"kind": "communication", "id": inbox.communication_id} for inbox in store.list_target_accepted_communication_inboxes())
            observed = []
            for identity in identities:
                if identity["kind"] == "communication":
                    inbox = store.get_communication_inbox(identity["id"])
                    if inbox.job_id is None:
                        observed.append(WaitObservation("communication", inbox.communication_id, "pending", inbox.state))
                        continue
                    job_id, execution_id = inbox.job_id, inbox.execution_id
                else:
                    job_id, execution_id = identity["job_id"], identity["execution_id"]
                rows = service._saver.cold_execution_observations(target.session_id, target.thread_id, job_ids=[job_id])
                if len(rows) != 1 or rows[0]["execution_id"] != execution_id:
                    raise RuntimeError(f"冻结 wait identity 没有唯一 execution: {job_id}")
                row = rows[0]
                observed.append(WaitObservation(cast(WaitSelectorKind, identity["kind"]), identity["id"], cast(WaitState, row["state"]), row["revision"]))
        return observed, identities


    async def _wait_target(self, request: CommunicationObservationRequest, frozen: dict[str, object] | None, lease: SessionOperationLease) -> dict[str, object]:
        service = self._communication
        deadline = DurableDeadline(**cast(dict[str, object], request.query["deadline"]))
        until = cast(WaitUntil, request.query["until"])
        if frozen is None:
            async with self._gate().shared(request.target.session_id):
                service._sessions.assert_session_active(request.target.session_id)
                with closing(service._store(request.target.session_id)) as store:
                    _assert_current(store, lease)
                observed, identities = self._snapshot(request, None)
            frozen = {"identities": identities, "baseline_revision": _hash([asdict(item) for item in observed])}
            async with service._sessions.session_write_admission(request.target.session_id):
                with closing(service._store(request.target.session_id)) as store:
                    _assert_current(store, lease)
                    store.commit_remote_observation(request.observation_id, side="target", snapshot=frozen)
        identities = frozen["identities"]
        while True:
            revision = self._change_revision
            async with self._gate().shared(request.target.session_id):
                service._sessions.assert_session_active(request.target.session_id)
                with closing(service._store(request.target.session_id)) as store:
                    _assert_current(store, lease)
                observed, _identities = self._snapshot(request, identities)
            latest = _hash([asdict(item) for item in observed])
            remaining = remaining_wait_budget_seconds(deadline, self._clock)
            expired = remaining is None or remaining <= 0
            changed = latest != frozen["baseline_revision"]
            status = resolve_wait_status(observed=observed, until=until, deadline_expired=expired, state_changed=changed)
            terminal = all(item.state in {"completed", "failed", "cancelled"} for item in observed)
            if expired or (terminal if until == "terminal" else changed) or not identities:
                return {"status": status, "target_session_id": request.target.session_id, "target": asdict(request.target), "observed": [asdict(item) for item in observed], "baseline_revision": frozen["baseline_revision"], "latest_revision": latest, "observation_id": request.observation_id}
            # 不持 gate/store 等待；revision 复核覆盖快照与订阅间的通知竞态。
            async with self._changed:
                if revision != self._change_revision:
                    continue
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=remaining)
                except TimeoutError:
                    pass

    def _drain_observations(self, store: SessionControlStore) -> None:
        for observation in store.list_remote_observations():
            error = {"error": RemoteObservationError("target-deleted").to_wire()}
            identities = _observation_identities(store, observation)
            for page in store.list_remote_pages(observation.observation_id, side=observation.side):
                if page.result is None:
                    result = error
                    if observation.side == "target":
                        request = CommunicationObservationRequest.model_validate(page.request)
                        if observation.result is not None:
                            result = observation.result
                        elif request.operation == "read" and observation.snapshot is not None:
                            result = self._read_page(request, observation.snapshot, store, recovery=True)
                    store.commit_remote_page(observation.observation_id, side=observation.side, page_identity=page.page_identity, result=result)
            if observation.result is None:
                store.commit_remote_observation(observation.observation_id, side=observation.side, result=error)
            for identity in identities:
                _terminalize(store, identity)

    def _collect_observations(self, store: SessionControlStore) -> datetime | None:
        now = self._clock.utc_now()
        next_expiry = None
        for observation in store.list_remote_observations():
            moment = datetime.fromisoformat(observation.expires_at)
            if moment > now:
                next_expiry = moment if next_expiry is None else min(next_expiry, moment)
                continue
            identities = _observation_identities(store, observation)
            if _all_terminal(store, identities):
                store.delete_remote_observation(observation.observation_id, side=observation.side)
        return next_expiry

    async def drain_session(self, session_id: str) -> None:
        """生产删除 callback 已持有 gate；只完成原记录，不再准入/重入 gate。"""
        with closing(self._communication._store(session_id)) as store:
            self._drain_observations(store)
            store.drain_communication_outboxes()
            store.drain_communication_inboxes()
            for lease in store.list_non_terminal_leases():
                if lease.operation_kind in {"communication_source", "communication_target", "federated_call"}:
                    _terminalize(store, lease.operation_identity)
        self._gc_changed.set()
        await self.notify()

    async def collect_expired(self) -> datetime | None:
        resolver = self._communication._sessions.path_resolver
        now = self._clock.utc_now()
        next_expiry = None
        _revision, nodes = resolver.catalog_store.read_nodes_snapshot()
        for node in nodes:
            if node.kind != "session" or node.state != "active":
                continue
            try:
                async with self._communication._sessions.session_write_admission(node.node_id):
                    with closing(self._communication._store(node.node_id)) as store:
                        moment = self._collect_observations(store)
                        if moment is not None:
                            next_expiry = moment if next_expiry is None else min(next_expiry, moment)
            except _DELETED:
                continue
        replay_expiry = await resolver.collect_expired_operation_replays(now=now)
        if replay_expiry is not None:
            next_expiry = replay_expiry if next_expiry is None else min(next_expiry, replay_expiry)
        return next_expiry

    async def _schedule_recovery(self) -> None:
        service = self._communication
        _revision, nodes = service._sessions.path_resolver.catalog_store.read_nodes_snapshot()
        for node in nodes:
            if node.kind != "session" or node.state != "active":
                continue
            try:
                async with self._gate().shared(node.node_id):
                    service._sessions.assert_session_active(node.node_id)
                    with closing(service._store(node.node_id)) as store:
                        pending = [item for item in store.list_remote_observations() if not _all_terminal(store, _observation_identities(store, item))]
            except _DELETED:
                continue
            for item in pending:
                if item.side == "source" and datetime.fromisoformat(item.expires_at) > self._clock.utc_now():
                    continue
                key = (node.node_id, item.side, item.observation_id)
                if key in self._recoveries:
                    continue
                task = asyncio.create_task(self._recover_observation(*key, wait=True), name="communication-observation-recovery")
                self._recoveries[key] = task
                service._tasks.add(task)
                task.add_done_callback(service._finished)
                task.add_done_callback(lambda _task, key=key: self._recoveries.pop(key))

    async def _recover_observation(self, session_id: str, side: str, observation_id: str, *, wait: bool = False) -> None:
        locks = self._source_locks if side == "source" else self._target_locks
        async with locks.lock_for(observation_id, wait=wait) as guard:
            if not guard.acquired:
                return
            resumed = False
            try:
                async with self._gate().shared(session_id):
                    self._communication._sessions.assert_session_active(session_id)
                    with closing(self._communication._store(session_id)) as store:
                        observation = store.get_remote_observation(observation_id, side=side)
                        if observation is None:
                            return
                        if _all_terminal(store, _observation_identities(store, observation)):
                            # 其它进程的原 holder 刚完成；OS 释放也唤醒本进程 GC。
                            self._gc_changed.set()
                            return
                        pages = store.list_remote_pages(observation_id, side=side)
                if side == "target" and pages:
                    resumed = True
                    for page in pages:
                        try:
                            await self._observe_target(CommunicationObservationRequest.model_validate(page.request), recovery=True)
                        except RemoteObservationError:
                            # 错误闭集已经由原 owner 落库；删除也只读原 drain 结果。
                            continue
                elif datetime.fromisoformat(observation.expires_at) <= self._clock.utc_now():
                    resumed = True
                    async with self._communication._sessions.session_write_admission(session_id):
                        with closing(self._communication._store(session_id)) as store:
                            self._cancel_stopped_holder(store, observation)
            except _DELETED:
                return
            finally:
                if resumed:
                    self._gc_changed.set()

    def _cancel_stopped_holder(self, store: SessionControlStore, observation: RemoteObservation) -> None:
        """OS 锁证明旧 holder 停止且无法重放；取消证据先提交，再终结原 lease。"""
        side, observation_id = observation.side, observation.observation_id
        current = store.get_remote_observation(observation_id, side=side)
        if current is None:
            return
        preimage = current.request["input"] if side == "source" else current.request
        root = _lease(store, side + "-observation|" + observation_id, preimage)
        result = current.result
        for page in store.list_remote_pages(observation_id, side=side):
            lease = _lease(store, side + "-page|" + page.page_identity, page.request)
            value = page.result
            if value is None:
                value = {"error": RemoteObservationError("operation-retry-expired").to_wire(), "cancellation": {"reason": "holder-stopped", "lease_id": lease.lease_id, "fencing_token": lease.fencing_token}}
                store.commit_remote_page(observation_id, side=side, page_identity=page.page_identity, result=value)
            _settle(store, lease, outcome="cancelled" if observation_error_code(value.get("error")) else "completed")
            if result is None:
                result = value
        if root.state not in SESSION_OPERATION_LEASE_TERMINAL_STATES:
            if result is None:
                result = {"error": RemoteObservationError("operation-retry-expired").to_wire(), "cancellation": {"reason": "holder-stopped-before-page-admission", "lease_id": root.lease_id, "fencing_token": root.fencing_token}}
            store.commit_remote_observation(observation_id, side=side, result=result)
            _settle(store, root, outcome="cancelled" if observation_error_code(result.get("error")) else "completed")

    async def gc_loop(self) -> None:
        while True:
            self._communication._check_failure()
            self._gc_changed.clear()
            await self._schedule_recovery()
            next_expiry = await self.collect_expired()
            timeout = None if next_expiry is None else max((next_expiry - self._clock.utc_now()).total_seconds(), 0)
            try:
                await asyncio.wait_for(self._gc_changed.wait(), timeout=timeout)
            except TimeoutError:
                pass
