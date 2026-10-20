"""固定根内先完整校验，再逐文件发布；部分效果永不自动回滚。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from app.agents.tools.apply_patch.journal import (
    update_apply_patch_journal,
    write_apply_patch_journal,
)
from app.agents.tools.apply_patch.models import ActionType, Commit, DiffError
from app.agents.tools.apply_patch.parser import (
    identify_files_added,
    identify_files_affected,
    identify_files_needed,
    parse_patch,
)
from app.agents.workspace_tool_paths import normalize_workspace_relative_path
from app.core.hashing import sha256_hex
from app.core.path_utils import get_workspace_root
from app.core.resource_uri import parse_resource_uri
from app.core.trace_middleware import get_current_gateway_id
from app.core.workspace_identity import read_workspace_id
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from app.services.infrastructure.workspace_files.mutation_lock import (
    workspace_file_mutation_lock,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.services.infrastructure.workspace_files.text_publication import (
    FilePreimage,
    FilePublicationUnverifiedError,
    WorkspaceFileConflictError,
    delete_file_content,
    publish_file_content,
    read_file_preimage,
)

_MAX_FILE_BYTES = 33554432
_VERSION = re.compile(r"(?:absent|sha256:[0-9a-f]{64})\Z")


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    virtual_path: str
    uri: str
    real_path: Path
    before: FilePreimage | None

    @property
    def version(self) -> str:
        return "absent" if self.before is None else self.before.content_hash


@dataclass(frozen=True, slots=True)
class PatchStep:
    snapshot: FileSnapshot
    content: bytes | None
    operation: str
    mode: int | None = None


def apply_patch_text(patch_text: str, *, explanation: str, expected_versions: dict[str, str],
                     workspace_root: Path | None = None, target: str = ".") -> dict[str, object]:
    workspace = (workspace_root or get_workspace_root()).resolve()
    paths = WorkspaceFilePaths(workspace=workspace, workspace_id=read_workspace_id(workspace), bindings=None)
    if "://" in target:
        parsed = parse_resource_uri(target, local_gateway_id=get_current_gateway_id())
        if parsed.gateway_authority is not None:
            # TODO：文件写入联邦路由接通前明确拒绝，绝不向本机同名目录回退。
            raise ResourceUriResolveError("capability_denied", "目标 Gateway 的补丁执行尚未接通")
        if parsed.scope != "workspace" or parsed.kind != "files":
            raise ResourceUriResolveError("capability_denied", "该资源没有工作区文本补丁能力")
        if parsed.scope_id != paths.workspace_id:
            raise ResourceUriResolveError("unknown_resource", "不是本次固定工作区目标")
    root, root_uri = paths.resolve(target, reject_symlink=True)
    if root.is_relative_to(workspace / ".boxteam"):
        raise ValueError("补丁禁止访问 .boxteam 私有存储")
    if not root.is_dir():
        raise NotADirectoryError("apply_patch 目标必须是已存在的目录根")
    result: dict[str, object] | None = None
    try:
        with workspace_file_mutation_lock(workspace):
            declared = identify_files_affected(patch_text)
            _validate_declared_paths(declared, root)
            if set(expected_versions) != set(declared):
                raise DiffError("expected_versions 必须精确覆盖全部文件路径，包含移动的源与目的")
            snapshots = _capture_snapshots(declared, root, workspace, paths, expected_versions)
            current = {name: snapshots[name].before.content.decode("utf-8").replace(_line_ending(snapshots[name].before), "\n") for name in identify_files_needed(patch_text)
                       if snapshots[name].before is not None}
            for name in identify_files_added(patch_text):
                if snapshots[name].before is not None:
                    raise WorkspaceFileConflictError(f"新增目标不满足 expected_absent: {snapshots[name].uri}")
            commit = parse_patch(patch_text, current)
            steps = _prepare_steps(commit, snapshots)
            if not any((step.snapshot.before is None) != (step.content is None) or
                       (step.snapshot.before is not None and step.snapshot.before.content != step.content) for step in steps):
                raise DiffError("apply_patch 校验失败: 工作区文件字节未发生变化")
            journal_id = write_apply_patch_journal([
                {"file_path": item.virtual_path, "uri": item.uri, "before_exists": item.before is not None,
                 "before_content": "" if item.before is None else item.before.content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")}
                for item in snapshots.values()
            ], workspace_root=workspace)
            publications, failure = _publish_steps(steps)
            try:
                update_apply_patch_journal(journal_id, publications, workspace_root=workspace)
            except (OSError, RuntimeError, ValueError) as error:
                failure = error
            states = {item["state"] for item in publications}
            effect = ("unknown" if "unknown" in states else "completed" if states == {"published"} else
                      "partial" if "published" in states else "not_executed")
            result: dict[str, object] = {
                "status": "success" if failure is None else "error", "effect": effect,
                "explanation": explanation, "target": root_uri, "journal_id": journal_id,
                "fuzz": int(commit.fuzz), "files": publications, "retryable": False,
            }
            if failure is not None:
                result.update(code="revision_conflict" if isinstance(failure, WorkspaceFileConflictError) else "execution_failed",
                              error_type=type(failure).__name__, error=_error_detail(failure),
                              recovery="重新读取原目标，核实逐文件结果后再决定后续操作；禁止自动重放或恢复旧内容")
    except (OSError, RuntimeError) as error:
        if result is None:
            raise
        # 文件效果已有回执；释放锁的失败不能将它改报为“未执行”。
        result.update(status="error", lock_release_error=_error_detail(error))
        result.setdefault("code", "execution_failed")
    return result


def extract_apply_patch_file_paths(patch_text: str, *, workspace_root: Path | None = None) -> list[str]:
    paths = identify_files_affected(patch_text)
    _validate_declared_paths(paths, (workspace_root or get_workspace_root()).resolve())
    return paths


def _capture_snapshots(names: list[str], root: Path, workspace: Path, paths: WorkspaceFilePaths,
                       expected: dict[str, str]) -> dict[str, FileSnapshot]:
    snapshots = {}
    for name in names:
        version = expected[name]
        if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
            raise DiffError(f"文件预期版本必须是 sha256:<hex> 或 absent: {name}")
        path = _resolve_workspace_file(name, root)
        uri = paths.reference(path)
        before = read_file_preimage(path, reference=uri, max_bytes=_MAX_FILE_BYTES) if path.exists() or path.is_symlink() else None
        if before is not None:
            # 删除和移动也只属于文本能力，不能借补丁扩成任意节点 mutation。
            if b"\x00" in before.content:
                raise ValueError(f"补丁目标不是文本文件: {uri}")
            before.content.decode("utf-8")
        snapshot = FileSnapshot("/" + path.relative_to(workspace).as_posix(), uri, path, before)
        if snapshot.version != version:
            raise WorkspaceFileConflictError(f"补丁目标版本冲突，请重新读取: {uri}; expected={version}; actual={snapshot.version}")
        snapshots[name] = snapshot
    return snapshots


def _prepare_steps(commit: Commit, snapshots: dict[str, FileSnapshot]) -> list[PatchStep]:
    steps = []
    touched: set[str] = set()
    for name, change in commit.changes.items():
        source = snapshots[name]
        # 补丁匹配逻辑行；纯 CRLF 原件发布时保留其行结尾，版本始终按原字节。
        content = (change.new_content or "").replace("\n", _line_ending(source.before)).encode("utf-8")
        if change.type == ActionType.DELETE:
            steps.append(PatchStep(source, None, "delete"))
        elif change.move_path:
            destination = snapshots[change.move_path]
            steps.append(PatchStep(destination, content, "move", source.before.mode))
            steps.append(PatchStep(source, None, "move"))
        else:
            steps.append(PatchStep(source, content, change.type.value))
    for step in steps:
        if step.snapshot.uri in touched:
            raise DiffError(f"补丁重复修改同一文件: {step.snapshot.uri}")
        touched.add(step.snapshot.uri)
        if step.content is not None and (len(step.content) > _MAX_FILE_BYTES or b"\x00" in step.content):
            raise ValueError(f"补丁文本超过预算或包含二进制内容: {step.snapshot.uri}")
    return steps


def _line_ending(before: FilePreimage | None) -> str:
    return "\r\n" if before is not None and b"\r\n" in before.content and b"\n" not in before.content.replace(b"\r\n", b"") else "\n"


def _publish_steps(steps: list[PatchStep]) -> tuple[list[dict[str, object]], Exception | None]:
    attempted = 0
    failure: Exception | None = None
    confirmed: set[int] = set()
    unverified: set[int] = set()
    for step in steps:
        attempted += 1
        try:
            if step.content is None:
                if step.snapshot.before is None:
                    raise DiffError(f"删除源文件不存在: {step.snapshot.uri}")
                delete_file_content(step.snapshot.real_path, before=step.snapshot.before, reference=step.snapshot.uri)
            else:
                publish_file_content(step.snapshot.real_path, step.content, before=step.snapshot.before,
                                     reference=step.snapshot.uri, mode=step.mode)
            confirmed.add(attempted - 1)
        except (OSError, RuntimeError, ValueError) as error:
            failure = error
            if isinstance(error, FilePublicationUnverifiedError):
                unverified.add(attempted - 1)
            break
    publications = []
    for index, step in enumerate(steps):
        observed, observation_error = _observe(step.snapshot)
        after_version = "absent" if step.content is None else "sha256:" + sha256_hex(step.content)
        if index >= attempted:
            state = "not_attempted"
        elif observation_error is not None:
            state = "unknown"
        elif observed == after_version:
            state = "published" if index in confirmed else "unknown"
        elif observed == step.snapshot.version and index not in confirmed and index not in unverified:
            state = "not_published"
        else:
            state = "unknown"
        if index < attempted and state != "published" and failure is None:
            failure = FilePublicationUnverifiedError(step.snapshot.uri, after_version, observed)
        publications.append({"path": step.snapshot.uri, "operation": step.operation, "state": state,
                             "expected_version": step.snapshot.version, "intended_version": after_version,
                             "exists": None if observed is None else observed != "absent",
                             "content_hash": observed if observed != "absent" else None})
    return publications, failure


def _observe(snapshot: FileSnapshot) -> tuple[str | None, Exception | None]:
    try:
        if not snapshot.real_path.exists() and not snapshot.real_path.is_symlink():
            return "absent", None
        return read_file_preimage(snapshot.real_path, reference=snapshot.uri, max_bytes=_MAX_FILE_BYTES).content_hash, None
    except (OSError, RuntimeError, ValueError) as error:
        return None, error


def _error_detail(error: Exception) -> str:
    if isinstance(error, OSError) and (error.filename is not None or error.filename2 is not None):
        return f"{type(error).__name__}: {error.strerror}, errno={error.errno}"
    return str(error)


def _validate_declared_paths(paths: list[str], workspace_root: Path) -> None:
    targets = [_resolve_workspace_file(path, workspace_root) for path in paths]
    if len(targets) != len(set(targets)):
        raise DiffError("补丁中的多个路径指向同一文件")
    target_set = set(targets)
    for target in targets:
        parent = target.parent
        while parent != workspace_root:
            if parent in target_set:
                raise DiffError("补丁文件目标不能互为祖先与后代")
            if parent.exists() and not parent.is_dir():
                raise DiffError("补丁文件的现有父路径必须是目录")
            parent = parent.parent


def _resolve_workspace_file(file_path: str, workspace_root: Path) -> Path:
    if "://" in file_path or Path(file_path).is_absolute() or PureWindowsPath(file_path).drive:
        raise ValueError(f"apply_patch 文件路径无效: 补丁内文件名必须相对固定根: {file_path}")
    try:
        relative = normalize_workspace_relative_path(file_path, workspace_root=workspace_root)
    except ValueError as error:
        raise ValueError(f"apply_patch 文件路径无效: {file_path} ({error})") from error
    path = (workspace_root / relative).resolve()
    if path == workspace_root or not path.is_relative_to(workspace_root):
        raise ValueError(f"apply_patch 文件路径无效: 文件必须位于固定根内: {file_path}")
    if path.relative_to(workspace_root).parts[0] == ".boxteam":
        raise ValueError("补丁禁止访问 .boxteam 私有存储")
    return path
