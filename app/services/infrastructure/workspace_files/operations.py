"""WorkspaceService 唯一文件 operation owner；受理不执行集合成员写入。"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from threading import RLock
from typing import BinaryIO
from uuid import uuid4

from app.core.atomic_fs import fsync_directory, native_io_path
from app.core.exceptions import ForbiddenError
from app.core.path_utils import is_resource_uri_input, safe_join
from app.core.resource_uri import file_uri_child, file_uri_root, parse_resource_uri
from app.core.session_lifecycle_gate import OperationHolderGate
from app.schemas.internal_v2.session_navigation.operations import (
    NAVIGATION_MUTATION_TERMINAL_STATES,
)
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceDirectoryKeyDTO,
    WorkspaceFileMutationIntentDTO,
    WorkspaceFileOperationReceiptDTO,
    WorkspaceFileOperationStatusDTO,
)
from app.services.infrastructure.workspace_files.filesystem import (
    FileEffectUnverifiedError,
    captured_source,
    cleanup_steps,
    publish_step,
    stage_step,
    sync_tree,
    verify_published,
    write_upload,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.services.infrastructure.workspace_files.store import (
    FileOperationRecord,
    WorkspaceFileOperationConflictError,
    WorkspaceFileStore,
    canonical_json,
    digest,
)

logger = logging.getLogger(__name__)
DirectoryObservation = tuple[WorkspaceDirectoryKeyDTO, str, str, bool]


class WorkspaceFileOperations:
    def __init__(self, *, workspace: Path, workspace_id: str,
                 resolve: Callable[..., tuple[Path, str]],
                 validate_name: Callable[[str], str],
                 observe: Callable[[WorkspaceDirectoryKeyDTO, Path], DirectoryObservation],
                 snapshot: Callable[[WorkspaceDirectoryKeyDTO, Path], tuple[dict, str]],
                 paths: WorkspaceFilePaths) -> None:
        self.workspace, self.workspace_id = workspace, workspace_id
        self.store = WorkspaceFileStore(workspace, workspace_id)
        # 扫描与发布观察必须同序；迟到的旧扫描不能把新事实反向发布。
        self.observation_lock = RLock()
        self._resolve, self._validate_name, self._observe = resolve, validate_name, observe
        self._snapshot = snapshot
        self.paths = paths
        self._directory_paths: dict[Path, set[WorkspaceDirectoryKeyDTO]] = {}
        # 一个已验证的 workspace worker 执行原有序意图；所有 scope 别名、源/目标
        # 与祖先链共享此 OS 执行归属，CAS 仍只核受影响目录。
        self._holder = OperationHolderGate(workspace / ".boxteam/sessions", namespace="workspace_files")
        self._accept_lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._ready = asyncio.Event()
        self._stopping = False
        self._worker: asyncio.Task[None] | None = None
        self.worker_state = "stopped"
        self.worker_error: str | None = None
        self._incoming = workspace / ".boxteam/navigation/workspace-files-staging"

    async def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("文件 operation worker 已启动")
        self._stopping = False
        self._ready.clear()
        self.worker_state, self.worker_error = "starting", None
        self._worker = asyncio.create_task(self._run(), name="workspace-file-operations")
        self._worker.add_done_callback(self._observe_exit)
        await asyncio.wait_for(self._ready.wait(), timeout=30)
        self.require_ready()

    def _observe_exit(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            self.worker_state, self.worker_error = "failed", "文件 operation worker 被意外取消"
        elif task.exception() is not None:
            error = task.exception()
            self.worker_state, self.worker_error = "failed", f"{type(error).__name__}: workspace_id={self.workspace_id}"
            logger.error("文件 operation worker 失败: workspace_id=%s", self.workspace_id,
                         exc_info=(type(error), error, error.__traceback__))
        self._ready.set()

    def require_ready(self) -> None:
        if self.worker_state != "running" or self._worker is None or self._worker.done():
            raise RuntimeError(f"文件 operation worker 不可用: state={self.worker_state}, error={self.worker_error}")

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._stopping = True
        self._changed.set()
        task, self._worker = self._worker, None
        await task
        self.worker_state = "stopped"

    def close(self) -> None:
        if self._worker is not None and not self._worker.done():
            raise RuntimeError("文件 operation owner 尚未排空，不能关闭存储")
        self.store.close()

    async def _run(self) -> None:
        async with self._holder.lock_for(self.workspace_id):
            # URI 在每次恢复时重新取原生绑定，不复制根到业务存储。
            await asyncio.to_thread(self._restore_directory_paths)
            # 恢复只按原数据库记录与 staging 证明执行；不扫业务树推测操作。
            for operation_id, actor in self.store.cleanup_pending():
                await asyncio.to_thread(self._cleanup, self.store.get(operation_id, actor))
            await asyncio.to_thread(self._prune_unaccepted_uploads)
            for operation_id, actor in self.store.pending():
                await asyncio.to_thread(self._execute, self.store.get(operation_id, actor))
            self.worker_state = "running"
            self._ready.set()
            while not self._stopping:
                self._changed.clear()
                pending = self.store.pending()
                for operation_id, actor in pending:
                    await asyncio.to_thread(self._execute, self.store.get(operation_id, actor))
                    if self._stopping:
                        break
                remaining = [self.store.get(operation_id, actor) for operation_id, actor in self.store.pending()]
                if any(value.state == "running" for value in remaining):
                    raise RuntimeError("原文件 operation 未核实收口，禁止释放其执行归属或继续后继")
                if remaining:
                    continue
                if not self._stopping:
                    try:
                        await asyncio.wait_for(self._changed.wait(), timeout=0.2)
                    except TimeoutError:
                        pass

    def status(self, operation_ids: list[str], *, actor: str) -> WorkspaceFileOperationStatusDTO:
        return WorkspaceFileOperationStatusDTO(operations=[self.store.get(value, actor).receipt for value in operation_ids],
                                                worker_state=self.worker_state, worker_error=self.worker_error)

    def directory_key(self, path: str, scope: str) -> tuple[WorkspaceDirectoryKeyDTO, Path]:
        target, display = self._resolve(path, scope=scope, allow_empty=True)
        root = file_uri_root(display)
        scope = "workspace" if parse_resource_uri(display).scope == "workspace" else "filesystem"
        key = WorkspaceDirectoryKeyDTO(workspace_id=self.workspace_id, scope=scope, root=root, path=display)
        with self.observation_lock:
            self._directory_paths.setdefault(target, set()).add(key)
        return key, target

    def _restore_directory_paths(self) -> None:
        with self.observation_lock:
            for key, uri in self.store.observed_directories():
                self._directory_paths.setdefault(self.paths.native(uri), set()).add(key)

    def observed_directories(self, paths: set[Path], *, removed: set[Path] | None = None):
        with self.observation_lock:
            return tuple((key, native) for native, keys in self._directory_paths.items()
                         if native in paths or any(native == root or native.is_relative_to(root) for root in removed or ())
                         for key in keys)

    def _incoming_path(self, record: FileOperationRecord) -> Path | None:
        name = record.payload["incoming"]
        return None if name is None else safe_join(self._incoming, self._validate_name(name))

    def _source_path(self, record: FileOperationRecord, entry: dict) -> Path | None:
        source = entry["source"]
        if source is None:
            return None
        if "uri" in source:
            return self.paths.native(source["uri"])
        incoming = self._incoming_path(record)
        if incoming is None:
            raise RuntimeError("上传来源缺少原 operation staging 归属")
        return safe_join(incoming / "body", source["upload"])

    def _capture_source(self, source: Path, entry: dict) -> dict:
        # 上传暂存属于 owner 私有载体；公开来源递归校验，上传内容在目的路径校验。
        validate = (lambda path: self.paths.require_access(path, root_uri=entry["source"]["uri"])) if "uri" in entry["source"] else None
        return captured_source(source, validate=validate)

    def _validate_destination(self, target: Path, manifest: dict, *, root_uri: str) -> None:
        self.paths.require_access(target, root_uri=root_uri, for_write=True)
        for name, child in manifest.get("children", {}).items():
            self._validate_destination(target / name, child, root_uri=root_uri)

    def _stage_root(self, record: FileOperationRecord, index: int) -> Path:
        target = self.paths.native(record.steps[index]["target"], for_write=True)
        return target.parent / f".boxteam-file-{record.operation_id}-{index}"

    def observe_directories(self, directories: list[tuple[WorkspaceDirectoryKeyDTO, Path]]):
        with self.observation_lock:
            for key, path in directories:
                self._directory_paths.setdefault(path, set()).add(key)
            return self.store.observe([self._observe(key, path) for key, path in directories])

    @staticmethod
    def _relative_child_path(value: str) -> str:
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if (normalized.startswith("/") or PureWindowsPath(value).drive
                or is_resource_uri_input(value) or "\x00" in value or ".." in parts):
            logger.warning("文件后继路径校验失败: relative_child_path=%r", value)
            raise ValueError("relative_child_path 必须是原创建输出下不含上级目录的相对路径")
        return "/".join(part for part in parts if part not in ("", "."))

    def _target(self, directory_path: str, scope: str, intent: WorkspaceFileMutationIntentDTO,
                actor: str) -> tuple[WorkspaceDirectoryKeyDTO, Path]:
        if intent.created_by_operation_id is None:
            return self.directory_key(directory_path, scope)
        creator = self.store.get(intent.created_by_operation_id, actor)
        outputs = [entry for entry in creator.payload["entries"] if entry["kind"] == "directory"]
        if not outputs:
            raise ValueError("created_by_operation_id 必须指向原目录输出")
        original_key = WorkspaceDirectoryKeyDTO.model_validate(creator.payload["directory_key"])
        relative = self._relative_child_path(intent.relative_child_path)
        base = creator.payload["target"]
        if len(outputs) == 1:
            base = file_uri_child(base, *Path(outputs[0]["relative_path"]).parts)
        elif not any(relative == entry["relative_path"] or relative.startswith(entry["relative_path"] + "/") for entry in outputs):
            raise ValueError("多目录输出的后继必须用 relative_child_path 选择原目录输出")
        display = file_uri_child(base, *Path(relative).parts) if relative else base
        target = self.paths.native(display)
        if directory_path and self._resolve(directory_path, scope=scope, allow_empty=True)[1] != display:
            raise ValueError("后继路径与原创建者的输出不一致")
        return original_key.model_copy(update={"path": display}), target

    async def enqueue(self, *, kind: str, directory_path: str, scope: str,
                      intent: WorkspaceFileMutationIntentDTO, actor: str, data: dict,
                      uploads: list[tuple[str, BinaryIO]] | None = None) -> WorkspaceFileOperationReceiptDTO:
        self.require_ready()
        if actor != "local":
            raise PermissionError("文件 operation 必须使用原 local-token 认证 scope")
        async with self._accept_lock:
            key, target = await asyncio.to_thread(self._target, directory_path, scope, intent, actor)
            if intent.directory_key != key:
                raise WorkspaceFileOperationConflictError("文件 intent 的原 workspace/scope/root/path 与路由目标不一致，禁止改投")
            request = {"workspace_id": self.workspace_id, "directory_key": key.model_dump(),
                       "kind": kind, "intent": intent.model_dump(), "data": data}
            incoming = None
            accepted = False
            try:
                if uploads is not None:
                    paths = []
                    for raw_path, _stream in uploads:
                        normalized = raw_path.replace("\\", "/")
                        if (PureWindowsPath(raw_path).drive or "\x00" in normalized
                                or any(part in ("", ".", "..") for part in normalized.split("/"))):
                            raise ValueError("上传文件相对路径无效：必须是目标目录下的非空相对文件路径")
                        if normalized in paths:
                            raise FileExistsError("上传内容包含重复相对路径")
                        paths.append(normalized)
                    if not paths:
                        raise ValueError("上传内容不能为空")
                    candidate = self._incoming / (intent.client_operation_id + "-" + uuid4().hex)
                    native_io_path(candidate).mkdir(parents=True)
                    incoming = candidate
                    with native_io_path(incoming / "owner.json").open("x", encoding="utf-8") as marker:
                        marker.write(canonical_json({"workspace_id": self.workspace_id,
                                                     "operation_id": intent.client_operation_id}))
                        marker.flush()
                        os.fsync(marker.fileno())
                    fsync_directory(incoming)
                    fsync_directory(incoming.parent)
                    for normalized, (_raw_path, stream) in zip(paths, uploads, strict=True):
                        proof = await asyncio.to_thread(write_upload, safe_join(incoming / "body", normalized), stream)
                        request.setdefault("uploads", []).append({"path": normalized, **proof})
                    await asyncio.to_thread(sync_tree, incoming)
                    fsync_directory(incoming.parent)
                request_hash = digest(request)
                replay = self.store.replay(intent.client_operation_id, actor, request_hash)
                if replay is not None:
                    return replay
                payload = await asyncio.to_thread(self._prepare, kind, key, target, intent, actor, data, incoming, request_hash)
                receipt = self.store.accept(actor=actor, kind=kind, intent=intent, request_hash=request_hash, payload=payload)
                accepted = True
                self._changed.set()
                return receipt
            finally:
                if incoming is not None and not accepted:
                    original_error = sys.exception()
                    try:
                        shutil.rmtree(native_io_path(incoming))
                        fsync_directory(incoming.parent)
                    except OSError as cleanup_error:
                        if original_error is not None:
                            raise BaseExceptionGroup("文件受理及本次上传暂存回收均失败",
                                                     [original_error, cleanup_error]) from None
                        raise

    def _prepare(self, kind: str, key: WorkspaceDirectoryKeyDTO, target: Path,
                 intent: WorkspaceFileMutationIntentDTO, actor: str, data: dict,
                 incoming: Path | None, request_hash: str) -> dict:
        if intent.created_by_operation_id is None:
            if not native_io_path(target).exists():
                raise FileNotFoundError(f"目标目录不存在: {key.path or '.'}")
            if not native_io_path(target).is_dir():
                raise NotADirectoryError(f"目标路径不是目录: {key.path or '.'}")
        entries = []
        preconditions = []
        if native_io_path(target).is_dir():
            observation = self.observe_directories([(key, target)])[0]
            preconditions.append({"key": key.model_dump(), "path": key.path,
                                  "revision": intent.expected_directory_revision or observation.directory_revision})
        if kind == "create":
            name = self._validate_name(data["name"])
            if data["kind"] not in ("file", "directory"):
                raise ValueError(f"不支持的文件条目类型: {data['kind']}")
            entries.append({"relative_path": name, "kind": data["kind"], "source": None, "source_proof": None})
        elif kind in ("copy", "paste"):
            raw_paths = [data["source_path"]] if kind == "copy" else data["source_paths"]
            source_scope = data["source_scope"] if kind == "copy" else "workspace"
            paths = [self._resolve(raw, scope=source_scope, allow_empty=False) for raw in raw_paths]
            for source, uri in paths:
                proof = captured_source(source, validate=lambda path, uri=uri: self.paths.require_access(path, root_uri=uri))
                if not source.name or (native_io_path(source).is_dir() and target.is_relative_to(source)):
                    raise ValueError(f"不能把目录复制到自身内部或复制文件系统根: {uri}")
                entries.append({"relative_path": source.name, "kind": proof["manifest"]["kind"],
                                "source": {"uri": uri}, "source_proof": proof})
        else:
            body = incoming / "body"
            physical_body = native_io_path(body)
            groups = set()
            for source in physical_body.rglob("*"):
                if not source.is_file():
                    continue
                relative = source.relative_to(physical_body)
                parent = target
                first_missing = None
                for index, part in enumerate(relative.parts[:-1]):
                    parent /= part
                    if not native_io_path(parent).exists():
                        first_missing = Path(*relative.parts[:index + 1])
                        break
                    if native_io_path(parent).is_symlink() or not native_io_path(parent).is_dir():
                        raise ValueError(f"上传父路径不是普通目录: {file_uri_child(key.path, *parent.relative_to(target).parts)}")
                groups.add(str(first_missing or relative))
            for relative in sorted(groups):
                source = body / relative
                proof = captured_source(source)
                entries.append({"relative_path": relative, "kind": proof["manifest"]["kind"],
                                "source": {"upload": relative}, "source_proof": proof})
                parent = (target / relative).parent
                if parent != target and native_io_path(parent).is_dir():
                    parent_uri = file_uri_child(key.path, *parent.relative_to(target).parts)
                    parent_key, _ = self.directory_key(parent_uri, key.scope)
                    version = self.observe_directories([(parent_key, parent)])[0]
                    preconditions.append({"key": parent_key.model_dump(), "path": parent_key.path, "revision": version.directory_revision})
        destinations = set()
        for entry in entries:
            destination = safe_join(target, entry["relative_path"])
            manifest = entry["source_proof"]["manifest"] if entry["source_proof"] is not None else {}
            self._validate_destination(destination, manifest, root_uri=key.path)
            if destination in destinations:
                raise FileExistsError(f"多个来源具有相同名称: {destination.name}")
            if native_io_path(destination).exists() or native_io_path(destination).is_symlink():
                raise FileExistsError(f"目标已存在: {file_uri_child(key.path, *Path(entry['relative_path']).parts)}")
            destinations.add(destination)
        payload = {"workspace_id": self.workspace_id, "actor": actor, "directory_key": key.model_dump(),
                   "target": key.path, "intent": intent.model_dump(), "entries": entries,
                   "preconditions": preconditions, "request_hash": request_hash,
                   "incoming": None if incoming is None else incoming.name}
        if kind == "create":
            payload["created_path"] = file_uri_child(key.path, entries[0]["relative_path"])
        return payload

    def _check_preconditions(self, record: FileOperationRecord) -> None:
        payload = record.payload
        key = WorkspaceDirectoryKeyDTO.model_validate(payload["directory_key"])
        resolved, _ = self._resolve(key.path, scope=key.scope, allow_empty=True)
        if key.path != payload["target"] or not native_io_path(resolved).is_dir():
            raise WorkspaceFileOperationConflictError("文件 operation 的原目录路径事实已失效")
        intent = WorkspaceFileMutationIntentDTO.model_validate(payload["intent"])
        if intent.created_by_operation_id is not None:
            creator = self.store.get(intent.created_by_operation_id, record.actor)
            proofs = [(step, str(resolved.relative_to(self.paths.native(step["target"], for_write=True)))) for step in creator.steps
                      if step["kind"] == "directory" and resolved.is_relative_to(self.paths.native(step["target"], for_write=True))]
            if len(proofs) != 1:
                raise WorkspaceFileOperationConflictError("原创建结果没有后继目录的唯一发布证明")
            step, relative = proofs[0]
            original = step["proof"]["directories"].get(relative)
            actual = native_io_path(resolved).stat(follow_symlinks=False)
            if original != {"device": actual.st_dev, "inode": actual.st_ino}:
                raise WorkspaceFileOperationConflictError("原创建目录或子目录已被替换，拒绝使用当前同名路径")
        expectations = list(payload["preconditions"])
        if not any(item["key"] == key.model_dump() for item in expectations):
            expectations.insert(0, {"key": key.model_dump(), "path": key.path, "revision": None})
        for expectation in expectations:
            directory_key = WorkspaceDirectoryKeyDTO.model_validate(expectation["key"])
            expected = expectation["revision"]
            if directory_key == key and intent.directory_revision_from_operation_id is not None:
                preceding = self.store.get(intent.directory_revision_from_operation_id, record.actor)
                candidates = [item.directory_revision for item in preceding.receipt.directories if item.directory_key == directory_key and item.exists]
                if len(candidates) != 1:
                    raise WorkspaceFileOperationConflictError("原前序结果缺少精确目录 revision，禁止借当前版本补缺")
                expected = candidates[0]
            observed = self.observe_directories([(directory_key, self.paths.native(expectation["path"]))])[0]
            if not observed.exists or observed.directory_revision != expected:
                raise WorkspaceFileOperationConflictError(f"文件目录 revision 冲突: {directory_key.path}, expected={expected}, actual={observed.directory_revision}")
        for entry in payload["entries"]:
            target = self.paths.native(file_uri_child(payload["target"], *Path(entry["relative_path"]).parts), for_write=True)
            if native_io_path(target).exists() or native_io_path(target).is_symlink():
                raise WorkspaceFileOperationConflictError(f"执行前目标已存在: {file_uri_child(key.path, *Path(entry['relative_path']).parts)}")
            if entry["source"] is not None and self._capture_source(self._source_path(record, entry), entry) != entry["source_proof"]:
                raise WorkspaceFileOperationConflictError(f"执行前原来源事实已变化: {entry['source']}")

    def staging_roots(self) -> set[Path]:
        return {self._stage_root(record, index) for operation_id, actor in self.store.staging_records()
                for record in (self.store.get(operation_id, actor),) for index in range(len(record.steps))}

    def _observations(self, record: FileOperationRecord) -> list[DirectoryObservation]:
        key = WorkspaceDirectoryKeyDTO.model_validate(record.payload["directory_key"])
        root = self.paths.native(record.payload["target"])
        paths = {root}
        for entry in record.payload["entries"]:
            target = root / entry["relative_path"]
            paths.add(target.parent)
            manifest = entry["source_proof"]["manifest"] if entry["source_proof"] is not None else {"kind": entry["kind"], "children": {}}
            def add_directories(base: Path, value: dict) -> None:
                if value["kind"] == "directory":
                    paths.add(base)
                    for name, child in value["children"].items():
                        add_directories(base / name, child)
            add_directories(target, manifest)
        observations = []
        aliases = dict(self.observed_directories(paths))
        for path in sorted(paths):
            display = file_uri_child(key.path, *path.relative_to(root).parts)
            directory_key = key.model_copy(update={"path": display})
            aliases[directory_key] = path
        for directory_key, real in aliases.items():
            if real in paths:
                self._directory_paths.setdefault(real, set()).add(directory_key)
                observations.append(self._observe(directory_key, real))
        return observations

    def _execute(self, original: FileOperationRecord) -> None:
        intent = WorkspaceFileMutationIntentDTO.model_validate(original.payload["intent"])
        dependencies = [self.store.get(value, original.actor) for value in intent.depends_on]
        if any(value.state not in NAVIGATION_MUTATION_TERMINAL_STATES for value in dependencies):
            return
        if any(value.state != "committed" for value in dependencies):
            self._settle(original, state="dependency_failed", effect="none", items=[],
                         error_code="dependency_failed", error_detail="原前序操作未提交，后继不以当前同名路径代替原输出")
            self._cleanup(original)
            return
        record = self.store.claim(original)
        steps = record.steps
        try:
            if steps and all(step["state"] in ("preparing", "staged") for step in steps):
                self._check_preconditions(record)
            if not steps:
                self._check_preconditions(record)
                for index, entry in enumerate(record.payload["entries"]):
                    steps.append({"target": file_uri_child(record.payload["target"], *Path(entry["relative_path"]).parts),
                                  "kind": entry["kind"], "relative_path": entry["relative_path"],
                                  "state": "preparing", "proof": None})
                self.store.save_steps(record, steps)
            for step_index, step in enumerate(steps):
                if step["state"] != "preparing":
                    continue
                entry = record.payload["entries"][step_index]
                source = self._source_path(record, entry)
                if source is not None and self._capture_source(source, entry) != entry["source_proof"]:
                    raise WorkspaceFileOperationConflictError(f"完整 staging 前原来源已变化: {entry['source']}")
                _stage, proof = stage_step(stage_root=self._stage_root(record, step_index), operation_id=record.operation_id,
                                            request_hash=record.payload["request_hash"], source=source, kind=step["kind"])
                if source is not None and (self._capture_source(source, entry) != entry["source_proof"] or proof["manifest"] != entry["source_proof"]["manifest"]):
                    raise WorkspaceFileOperationConflictError(f"staging 与原来源内容不一致: {entry['source']}")
                step.update(state="staged", proof=proof)
                self.store.save_steps(record, steps)
            # 全部目标完成 preflight/staging 后才发布第一个成员。
            if all(step["state"] == "staged" for step in steps):
                self._check_preconditions(record)
            for step_index, step in enumerate(steps):
                target, stage = self.paths.native(step["target"], for_write=True), self._stage_root(record, step_index) / "entry"
                if step["state"] in ("publishing", "published"):
                    if verify_published(target, step["proof"]):
                        step["state"] = "published"
                        self.store.save_steps(record, steps)
                        continue
                    if step["state"] == "publishing" and verify_published(stage, step["proof"]):
                        # 原 inode 仍在 staging，独占 rename 尚未发生；可沿原证明发布。
                        step["state"] = "staged"
                    else:
                        raise FileEffectUnverifiedError(f"原发布效果的 inode/内容无法核实: {target}")
                if step["state"] != "staged" or not verify_published(stage, step["proof"]):
                    raise FileEffectUnverifiedError(f"原 staging 证明失效: {stage}")
                entry = record.payload["entries"][step_index]
                if entry["source"] is not None and self._capture_source(self._source_path(record, entry), entry) != entry["source_proof"]:
                    raise WorkspaceFileOperationConflictError(f"发布前原来源事实已变化: {entry['source']}")
                with self.observation_lock:
                    step["state"] = "publishing"
                    self.store.save_steps(record, steps)
                    try:
                        publish_step(stage, target, step["kind"])
                    except FileExistsError as error:
                        step.update(state="conflict", error=self._error_detail(record, error))
                        self.store.save_steps(record, steps)
                        raise WorkspaceFileOperationConflictError(f"原发布目标被外部创建，拒绝覆盖: {step['target']}") from error
                    step["state"] = "published"
                    self.store.save_steps(record, steps)
            self._settle(record, state="committed", effect="complete", items=self._result_items(record, steps))
        except FileEffectUnverifiedError as error:
            self._settle(record, state="running", effect="unverified", items=self._result_items(record, steps, unverified=True),
                         error_code="reconcile_required", error_detail=self._error_detail(record, error))
            raise
        except (OSError, ValueError, ForbiddenError, WorkspaceFileOperationConflictError) as error:
            for step_index, step in enumerate(steps):
                if step["state"] != "publishing":
                    continue
                if verify_published(self.paths.native(step["target"], for_write=True), step["proof"]):
                    step["state"] = "published"
                elif verify_published(self._stage_root(record, step_index) / "entry", step["proof"]):
                    step["state"] = "staged"
                else:
                    self._settle(record, state="running", effect="unverified", items=self._result_items(record, steps, unverified=True),
                                 error_code="reconcile_required", error_detail=self._error_detail(record, error))
                    raise FileEffectUnverifiedError(f"文件发布失败且原效果待核实: {error}") from error
            self.store.save_steps(record, steps)
            effect = "partial" if any(step["state"] == "published" for step in steps) else "none"
            self._settle(record, state="rejected", effect=effect, items=self._result_items(record, steps),
                         error_code="file_operation_conflict" if isinstance(error, WorkspaceFileOperationConflictError) else "file_operation_failed", error_detail=self._error_detail(record, error))
        # terminal 与目录/outbox 已先持久化；清理失败透明停止 worker，重启只回收原暂存。
        self._cleanup(self.store.get(record.operation_id, record.actor))

    def _settle(self, record: FileOperationRecord, **result):
        with self.observation_lock:
            observations = self._observations(record)
            parents = {record.payload["target"]} | {
                file_uri_child(record.payload["target"], *Path(entry["relative_path"]).parent.parts)
                for entry in record.payload["entries"]
            }
            snapshots = []
            for index, (key, real, _signature, present) in enumerate(observations):
                # 只投影受影响父目录及其已观察别名，不把新建 cold 子树全部送前端。
                if real in parents and present:
                    snapshot, signature = self._snapshot(key, self.paths.native(real))
                    observations[index] = key, real, signature, present
                    snapshots.append((key, snapshot))
            return self.store.settle(record, observations=observations, snapshots=snapshots, **result)

    @staticmethod
    def _error_detail(record: FileOperationRecord, error: BaseException) -> str:
        logger.error("文件 operation 执行失败: operation_id=%s target=%s", record.operation_id,
                     record.payload["target"], exc_info=(type(error), error, error.__traceback__))
        if isinstance(error, WorkspaceFileOperationConflictError):
            return str(error)
        return f"{type(error).__name__}: operation_id={record.operation_id}, target={record.payload['target']}, errno={getattr(error, 'errno', None)}"

    @staticmethod
    def _result_items(record: FileOperationRecord, steps: list[dict], *, unverified: bool = False) -> list[dict]:
        return [{"path": file_uri_child(record.payload["target"], *Path(entry["relative_path"]).parts), "kind": entry["kind"],
                 "state": "published" if index < len(steps) and steps[index]["state"] == "published" else
                 "unverified" if unverified and index < len(steps) and steps[index]["state"] == "publishing" else
                 "conflict" if index < len(steps) and steps[index]["state"] == "conflict" else "pending",
                 "error": None if index >= len(steps) else steps[index].get("error")}
                for index, entry in enumerate(record.payload["entries"])]

    def _cleanup_incoming(self, record: FileOperationRecord) -> None:
        if record.payload["incoming"] is not None:
            root = self._incoming_path(record)
            physical = native_io_path(root)
            if physical.exists():
                expected = canonical_json({"workspace_id": self.workspace_id, "operation_id": record.operation_id})
                marker = physical / "owner.json"
                if root.parent != self._incoming or physical.is_symlink() or not marker.is_file() or marker.read_text(encoding="utf-8") != expected:
                    raise RuntimeError(f"文件上传暂存的原 owner 证明失效: {root}")
                shutil.rmtree(physical)
                fsync_directory(root.parent)

    def _cleanup(self, record: FileOperationRecord) -> None:
        cleanup_steps([{**step, "stage_root": str(self._stage_root(record, index))} for index, step in enumerate(record.steps)],
                      operation_id=record.operation_id, request_hash=record.payload["request_hash"])
        self._cleanup_incoming(record)
        self.store.complete_cleanup(record)

    def _prune_unaccepted_uploads(self) -> None:
        physical = native_io_path(self._incoming)
        if not physical.exists():
            return
        retained = {self._incoming_path(record) for operation_id, actor in self.store.staging_records()
                    if (record := self.store.get(operation_id, actor)).payload["incoming"] is not None}
        # 只清本 owner 专用 staging；未接受请求没有业务效果，不扫描工作区树。
        for path in physical.iterdir():
            if self._incoming / path.name in retained:
                continue
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError(f"上传 staging 包含未知条目: {path}")
            shutil.rmtree(path)
        fsync_directory(self._incoming)
