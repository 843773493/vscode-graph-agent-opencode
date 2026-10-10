"""真实导航 HTTP 进程的读调用观察器；所有响应仍由生产 ASGI app 生成。"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, deque
from contextvars import ContextVar
from pathlib import Path
from threading import Lock, get_ident, local
from time import perf_counter_ns, time_ns
from typing import cast

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_reads: ContextVar[Counter[str] | None] = ContextVar("navigation_read_calls", default=None)
_total_reads: Counter[str] = Counter()
_read_lock = Lock()
_trace_context: ContextVar[dict[str, str] | None] = ContextVar("navigation_observation_context", default=None)
_trace_lock = Lock()
_trace_events: deque[dict[str, object]] = deque()
_trace_sequence = 0
_trace_dropped = 0
_trace_capacity = 16_000
_trace_directory_state: dict[str, dict[str, object]] = {}
_scan_state = local()
_kind = os.environ["BOXTEAM_MEASUREMENT_RUNTIME"]
_trace_enabled = _kind == "backend" and os.environ.get("BOXTEAM_NAVIGATION_TRACE") == "concurrent"
_roots = tuple(json.loads(os.environ["BOXTEAM_MEASUREMENT_READ_ROOTS"]))


def _record(kind: str, path: str | bytes | os.PathLike[str]) -> None:
    value = os.fsdecode(path)
    if any(value == root or value.startswith(root + os.sep) for root in _roots):
        # lifespan worker 与 asyncio.to_thread 不属于发起 POST 的 ContextVar；
        # 累计计数由前后观测窗口相减，覆盖真实异步执行与后续 UI 同步。
        with _read_lock:
            _total_reads[kind] += 1
            counts = _reads.get()
            if counts is not None:
                counts[kind] += 1


def _audit(event: str, args: tuple[object, ...]) -> None:
    if event in {"os.scandir", "os.listdir"} and args and isinstance(args[0], (str, bytes, Path)):
        _record(event, args[0])
    if event == "open" and isinstance(args[0], (str, bytes, Path)):
        flags = args[2]
        if isinstance(flags, int) and flags & os.O_ACCMODE != os.O_WRONLY:
            _record("open_read", args[0])


sys.addaudithook(_audit)
_stat = Path.stat


def _observed_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
    _record("path_stat", path)
    return _stat(path, follow_symlinks=follow_symlinks)


Path.stat = _observed_stat

if _kind == "backend":
    from app.main import app as _production_app
    from app.services.infrastructure.workspace_files.operations import (
        WorkspaceFileOperations,
    )
    from app.services.infrastructure.workspace_files.store import WorkspaceFileStore
    from app.services.infrastructure.workspace_service import WorkspaceService

    def _record_concurrent(event: str, **details: object) -> None:
        global _trace_sequence, _trace_dropped
        if not _trace_enabled:
            return
        with _trace_lock:
            _trace_sequence += 1
            if len(_trace_events) >= _trace_capacity:
                _trace_dropped += 1
                return
            _trace_events.append({
                "sequence": _trace_sequence,
                "monotonic_ns": perf_counter_ns(),
                "utc_ns": time_ns(),
                "thread_id": get_ident(),
                **(_trace_context.get() or {}),
                "event": event,
                **details,
            })

    def _trace_scope(phase: str, operation_id: str | None = None):
        prior = _trace_context.get() or {}
        context = {**prior, "phase": phase}
        if operation_id is not None:
            context["operation_id"] = operation_id
        return _trace_context.set(context)

    _scan_directory = WorkspaceService._scan_directory

    def _observed_scan_directory(self, target_path, display_path, scope, limit, cursor_key):
        if not _trace_enabled or display_path != "concurrent" or scope != "workspace":
            return _scan_directory(self, target_path, display_path, scope, limit, cursor_key)
        started_ns = perf_counter_ns()
        prior_members = getattr(_scan_state, "members", None)
        members: list[dict[str, object]] = []
        _scan_state.members = members
        try:
            result = _scan_directory(self, target_path, display_path, scope, limit, cursor_key)
        finally:
            _scan_state.members = prior_members
        items, next_key, signature = result
        if display_path == "concurrent" and scope == "workspace":
            _record_concurrent(
                "scan_directory",
                started_ns=started_ns,
                finished_ns=perf_counter_ns(),
                path=display_path,
                limit=limit,
                cursor_key=cursor_key,
                signature=signature,
                next_key=next_key,
                members=members,
                returned_members=[{
                    "name": item.name,
                    "path": item.path,
                    "kind": item.kind,
                    "size": item.size,
                    "has_children": item.has_children,
                } for item in items],
            )
        return result

    WorkspaceService._scan_directory = _observed_scan_directory

    _list_files = WorkspaceService.list_files

    async def _observed_list_files(self, *args, **kwargs):
        path = kwargs.get("path", "")
        scope = kwargs.get("scope", "workspace")
        concurrent_path = path == "concurrent" and scope == "workspace"
        if not _trace_enabled or not concurrent_path:
            return await _list_files(self, *args, **kwargs)
        token = _trace_scope("list_files", None)
        if concurrent_path:
            _record_concurrent("list_files_begin", path=path, scope=scope)
        try:
            result = await _list_files(self, *args, **kwargs)
            if concurrent_path:
                _record_concurrent(
                    "list_files_end",
                    path=path,
                    scope=scope,
                    directory_revision=result.directory_revision,
                    signature=None,
                    members=[item.model_dump() for item in result.items],
                )
            return result
        except BaseException as error:
            if concurrent_path:
                _record_concurrent("list_files_error", path=path, scope=scope,
                                   error_type=type(error).__name__, error=str(error))
            raise
        finally:
            _trace_context.reset(token)

    WorkspaceService.list_files = _observed_list_files

    _prepare = WorkspaceFileOperations._prepare

    def _observed_prepare(self, kind, key, target, intent, actor, data, incoming, request_hash):
        if not _trace_enabled or key.path != "concurrent" or key.scope != "workspace":
            return _prepare(self, kind, key, target, intent, actor, data, incoming, request_hash)
        operation_id = intent.client_operation_id
        token = _trace_scope("prepare", operation_id)
        try:
            return _prepare(self, kind, key, target, intent, actor, data, incoming, request_hash)
        finally:
            _trace_context.reset(token)

    WorkspaceFileOperations._prepare = _observed_prepare

    _check_preconditions = WorkspaceFileOperations._check_preconditions

    def _observed_check_preconditions(self, record):
        if not _trace_enabled or record.payload["directory_key"]["path"] != "concurrent":
            return _check_preconditions(self, record)
        token = _trace_scope("check_preconditions", record.operation_id)
        try:
            result = _check_preconditions(self, record)
        except BaseException as error:
            _record_concurrent(
                "preconditions_result",
                result="failed",
                expected=record.payload["preconditions"],
                intent=record.payload["intent"],
                error_type=type(error).__name__,
                error=str(error),
            )
            raise
        else:
            _record_concurrent(
                "preconditions_result",
                result="passed",
                expected=record.payload["preconditions"],
                intent=record.payload["intent"],
            )
            return result
        finally:
            _trace_context.reset(token)

    WorkspaceFileOperations._check_preconditions = _observed_check_preconditions

    _settle = WorkspaceFileOperations._settle

    def _observed_settle(self, record, **result):
        if not _trace_enabled or record.payload["directory_key"]["path"] != "concurrent":
            return _settle(self, record, **result)
        token = _trace_scope("settle", record.operation_id)
        try:
            return _settle(self, record, **result)
        finally:
            _trace_context.reset(token)

    WorkspaceFileOperations._settle = _observed_settle

    _store_observe = WorkspaceFileStore.observe

    def _observed_store_observe(self, observations, *, db=None):
        if db is None:
            return _store_observe(self, observations, db=db)
        if not _trace_enabled or not any(
            key.scope == "workspace" and key.path == "concurrent"
            for key, _, _, _ in observations
        ):
            return _store_observe(self, observations, db=db)
        relevant = [entry for entry in observations if entry[0].scope == "workspace" and entry[0].path == "concurrent"]
        started_ns = perf_counter_ns()
        try:
            result = _store_observe(self, observations, db=db)
        except BaseException as error:
            if relevant:
                _record_concurrent(
                    "store_observe_error",
                    started_ns=started_ns,
                    finished_ns=perf_counter_ns(),
                    inputs=[{
                        "directory_key": key.model_dump(),
                        "real_path": real_path,
                        "signature": signature,
                        "present": present,
                    } for key, real_path, signature, present in relevant],
                    error_type=type(error).__name__,
                    error=str(error),
                )
            raise
        if relevant:
            result_by_key = {entry.directory_key: entry for entry in result}
            transitions = []
            with _trace_lock:
                for key, _real_path, signature, present in relevant:
                    identity = json.dumps(key.model_dump(), sort_keys=True, separators=(",", ":"))
                    previous = _trace_directory_state.get(identity)
                    current = {
                        "signature": signature,
                        "exists": present,
                        "directory_revision": result_by_key[key].directory_revision,
                    }
                    transitions.append({
                        "directory_key": key.model_dump(),
                        "previous": previous,
                        "current": current,
                        "signature_changed": previous is not None and previous["signature"] != signature,
                        "revision_changed": previous is not None
                            and previous["directory_revision"] != current["directory_revision"],
                    })
                    _trace_directory_state[identity] = current
            _record_concurrent(
                "store_observe",
                started_ns=started_ns,
                finished_ns=perf_counter_ns(),
                transaction_supplied=db is not None,
                inputs=[{
                    "directory_key": key.model_dump(),
                    "real_path": real_path,
                    "signature": signature,
                    "present": present,
                } for key, real_path, signature, present in relevant],
                outputs=[{
                    "directory_key": key.model_dump(),
                    "directory_revision": result_by_key[key].directory_revision,
                    "exists": result_by_key[key].exists,
                } for key, _, _, _ in relevant],
                transitions=transitions,
            )
        return result

    WorkspaceFileStore.observe = _observed_store_observe

    _entry_to_node = WorkspaceService._entry_to_file_node

    class _ObservedEntry:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry

        def __getattr__(self, name: str) -> object:
            return getattr(self._entry, name)

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            _record("direntry_stat", self._entry.path)
            return self._entry.stat(follow_symlinks=follow_symlinks)

    def _observed_entry_to_node(self, entry, parent_path, *, scope):
        node = _entry_to_node(self, cast("os.DirEntry[str]", _ObservedEntry(entry)), parent_path, scope=scope)
        members = getattr(_scan_state, "members", None)
        if _trace_enabled and members is not None and parent_path == "concurrent" and scope == "workspace":
            members.append({
                "name": node.name,
                "path": node.path,
                "kind": node.kind,
                "size": node.size,
                "has_children": node.has_children,
                "modified_at": node.modified_at,
            })
        return node

    WorkspaceService._entry_to_file_node = _observed_entry_to_node
elif _kind == "gateway":
    from app.gateway.main import app as _production_app
else:
    raise ValueError(f"未知量化运行面: {_kind}")


def _trace_probe(probe_id: str | None, stage: str, **details: object) -> None:
    if probe_id is None:
        return
    # 仅写现有 stderr；锁内不做日志 I/O，也不新增落盘或业务 owner。
    print(json.dumps({"navigation_fs_probe": probe_id, "runtime": _kind,
                      "stage": stage, "monotonic_ns": perf_counter_ns(),
                      "utc_ns": time_ns(), **details}, separators=(",", ":")),
          file=sys.stderr, flush=True)


class _MeasuredApp:
    def __init__(self, application: ASGIApp) -> None:
        self._application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._application(scope, receive, send)
            return
        headers = dict(scope["headers"])
        request_id = headers.get(b"x-request-id", b"").decode("ascii", errors="replace")
        request_token = (
            _trace_context.set({"request_id": request_id} if request_id else {})
            if _kind == "backend"
            else None
        )
        probe_id = next((value.decode("ascii") for key, value in scope["headers"]
                         if key.lower() == b"x-test-navigation-probe-id"), None)
        if probe_id is not None:
            connection_headers = {b"connection", b"keep-alive", b"transfer-encoding", b"content-length"}
            _trace_probe(probe_id, "asgi_enter", path=scope["path"], client=scope.get("client"),
                         server=scope.get("server"), http_version=scope.get("http_version"),
                         headers={key.decode("ascii"): value.decode("latin-1")
                                  for key, value in scope["headers"] if key.lower() in connection_headers})
            original_send = send

            async def probe_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    _trace_probe(probe_id, "asgi_response_headers", status=message["status"],
                                 headers={key.decode("ascii"): value.decode("latin-1")
                                          for key, value in message.get("headers", [])
                                          if key.lower() in connection_headers})
                elif message["type"] == "http.response.body":
                    _trace_probe(probe_id, "asgi_response_body", bytes=len(message.get("body", b"")),
                                 more_body=message.get("more_body", False))
                await original_send(message)
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    _trace_probe(probe_id, "asgi_body_end")

            send = probe_send
        prefix = "/api/v1" if _kind == "backend" else "/api/gateway"
        if _kind == "backend" and scope["path"] == f"{prefix}/test/navigation-measurements/concurrent-trace":
            with _trace_lock:
                snapshot = {"events": list(_trace_events), "dropped_events": _trace_dropped,
                            "capacity": _trace_capacity}
            try:
                await JSONResponse(snapshot)(scope, receive, send)
            finally:
                if request_token is not None:
                    _trace_context.reset(request_token)
            return
        if scope["path"] == f"{prefix}/test/navigation-measurements/fs-reads":
            _trace_probe(probe_id, "asgi_lock_wait")
            wait_started = perf_counter_ns()
            with _read_lock:
                acquired = perf_counter_ns()
                snapshot = dict(_total_reads)
            released = perf_counter_ns()
            _trace_probe(probe_id, "asgi_lock_released", wait_ns=acquired - wait_started,
                         held_ns=released - acquired, acquired_ns=acquired, released_ns=released)
            try:
                await JSONResponse({"runtime": _kind, "reads": snapshot})(scope, receive, send)
            finally:
                _trace_probe(probe_id, "asgi_exit")
                if request_token is not None:
                    _trace_context.reset(request_token)
            return
        counts: Counter[str] = Counter()
        token = _reads.set(counts)

        async def observed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                with _read_lock:
                    request_reads = dict(counts)
                message = {**message, "headers": [*message.get("headers", []), (
                    f"x-test-{_kind}-fs-reads".encode(),
                    json.dumps(request_reads, separators=(",", ":")).encode(),
                )]}
            await send(message)

        try:
            await self._application(scope, receive, observed_send)
        finally:
            _reads.reset(token)
            _trace_probe(probe_id, "asgi_exit")
            if request_token is not None:
                _trace_context.reset(request_token)


app = _MeasuredApp(_production_app)
