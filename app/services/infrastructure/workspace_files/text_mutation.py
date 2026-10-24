"""工作区文本修改的唯一条件执行链；补丁与精确编辑共用。"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from app.core.exceptions import ForbiddenError
from app.core.hashing import sha256_hex
from app.core.path_utils import is_resource_uri_input, resolve_native_file_path
from app.core.resource_uri import ResourceUriGrammarError, parse_resource_uri
from app.core.trace_middleware import get_current_gateway_id
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from app.services.infrastructure.workspace_files.mutation_lock import (
    workspace_file_mutation_lock,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.services.infrastructure.workspace_files.text_journal import (
    update_text_mutation_journal,
    write_text_mutation_journal,
)
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


class TextMutationOutputLimitError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    uri: str
    real_path: Path
    before: FilePreimage | None

    @property
    def version(self) -> str:
        return "absent" if self.before is None else self.before.content_hash


@dataclass(frozen=True, slots=True)
class TextPublication:
    snapshot: FileSnapshot
    content: bytes | None
    operation: str
    mode: int | None = None


def resolve_text_target(paths: WorkspaceFilePaths, target: str) -> tuple[Path, str]:
    if is_resource_uri_input(target):
        parsed = parse_resource_uri(target, local_gateway_id=get_current_gateway_id())
        if parsed.gateway_authority is not None:
            raise ResourceUriResolveError("capability_denied", "本机条件发布入口不接受另一个 Gateway 的目标")
        if parsed.scope != "workspace" or parsed.kind not in {"files", "attachments"}:
            raise ResourceUriResolveError("capability_denied", "该资源没有工作区文本写入能力")
        if parsed.scope_id != paths.workspace_id:
            raise ResourceUriResolveError("unknown_resource", "不是本次固定工作区目标")
    try:
        path, uri = paths.resolve(target, reject_symlink=True, allow_external=False)
    except ForbiddenError as error:
        raise ResourceUriResolveError("capability_denied", "文本目标越出工作区写入能力") from error
    return path, uri


def execute_text_mutation(*, paths: WorkspaceFilePaths, target: str, declared: list[str],
                          expected_versions: dict[str, str],
                          prepare: Callable[[dict[str, FileSnapshot]], list[TextPublication]],
                          authorize: Callable[[str], None] | None = None,
                          explanation: str = "", max_journal_bytes: int | None = None) -> dict[str, object]:
    workspace = paths.workspace
    root, root_uri = resolve_text_target(paths, target)
    if not root.is_dir():
        raise NotADirectoryError("文本修改目标必须是已存在的目录根")
    result: dict[str, object] | None = None
    try:
        with workspace_file_mutation_lock(workspace):
            validate_text_paths(declared, root)
            if set(expected_versions) != set(declared):
                raise ValueError("expected_versions 必须精确覆盖全部文件路径，包含移动的源与目的")
            snapshots = _capture_snapshots(declared, root, root_uri, paths, expected_versions, authorize, max_journal_bytes)
            steps = prepare(snapshots)
            touched: set[str] = set()
            for step in steps:
                if step.snapshot.uri in touched:
                    raise ValueError(f"重复修改同一文件: {step.snapshot.uri}")
                touched.add(step.snapshot.uri)
                if step.content is not None and (len(step.content) > _MAX_FILE_BYTES or b"\x00" in step.content):
                    raise ValueError(f"文本超过预算或包含二进制内容: {step.snapshot.uri}")
            if not any((step.snapshot.before is None) != (step.content is None) or
                       (step.snapshot.before is not None and step.snapshot.before.content != step.content) for step in steps):
                raise ValueError("文本修改校验失败: 工作区文件字节未发生变化")
            records = [{"uri": item.uri, "before_exists": item.before is not None,
                        "before_content": "" if item.before is None else item.before.content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")}
                       for item in snapshots.values()]
            if max_journal_bytes is not None:
                planned = [{"uri": step.snapshot.uri, "after_content": "" if step.content is None else step.content.decode("utf-8")}
                           for step in steps]
                if len(json.dumps([records, planned], ensure_ascii=False).encode("utf-8")) + 4096 * len(steps) > max_journal_bytes:
                    raise TextMutationOutputLimitError("文本修改的原件、后件和回执超过本次传输预算，尚未发布")
            journal_id = write_text_mutation_journal(records, workspace_root=workspace)
            publications, observations, failure = _publish_steps(steps, observation_budget=max_journal_bytes)
            try:
                update_text_mutation_journal(journal_id, publications, observations=observations, workspace_root=workspace)
            except (OSError, RuntimeError, ValueError) as error:
                failure = error
            states = {item["state"] for item in publications}
            effect = ("unknown" if "unknown" in states else "completed" if states == {"published"} else
                      "partial" if "published" in states else "not_executed")
            result: dict[str, object] = {
                "status": "success" if failure is None else "error", "effect": effect,
                "explanation": explanation, "target": root_uri, "journal_id": journal_id,
                "files": publications, "retryable": False,
            }
            if failure is not None:
                result.update(code="revision_conflict" if isinstance(failure, WorkspaceFileConflictError) else "execution_failed",
                              error_type=type(failure).__name__, error=text_mutation_error_detail(failure),
                              recovery="重新读取原目标，核实逐文件结果后再决定后续操作；禁止自动重放或恢复旧内容")
    except (OSError, RuntimeError) as error:
        if result is None:
            raise
        # 文件效果已有回执；释放锁的失败不能将它改报为“未执行”。
        result.update(status="error", lock_release_error=text_mutation_error_detail(error))
        result.setdefault("code", "execution_failed")
    return result


def _capture_snapshots(names: list[str], root: Path, root_uri: str, paths: WorkspaceFilePaths,
                       expected: dict[str, str], authorize: Callable[[str], None] | None, max_bytes: int | None) -> dict[str, FileSnapshot]:
    snapshots = {}
    captured_bytes = 0
    for name in names:
        version = expected[name]
        if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
            raise ValueError(f"文件预期版本必须是 sha256:<hex> 或 absent: {name}")
        path = _resolve_workspace_file(name, root)
        try:
            paths.require_access(path, root_uri=root_uri, for_write=True)
            uri = paths.reference(path, root_uri=root_uri)
        except ForbiddenError as error:
            raise ResourceUriResolveError("capability_denied", "私有存储只允许由业务 owner 访问") from error
        if authorize is not None:
            authorize(uri)
        before = read_file_preimage(path, reference=uri, max_bytes=min(_MAX_FILE_BYTES, max_bytes) if max_bytes is not None else _MAX_FILE_BYTES) if path.exists() or path.is_symlink() else None
        if before is not None:
            # 删除和移动也只属于文本能力，不能借补丁扩成任意节点 mutation。
            if b"\x00" in before.content:
                raise ValueError(f"补丁目标不是文本文件: {uri}")
            before.content.decode("utf-8")
        if max_bytes is not None:
            captured_bytes += len(json.dumps("" if before is None else before.content.decode("utf-8"), ensure_ascii=False).encode("utf-8")) + len(uri.encode("utf-8")) + 128
            if captured_bytes > max_bytes:
                raise TextMutationOutputLimitError("文本修改原件超过本次传输预算，尚未发布")
        snapshot = FileSnapshot(uri, path, before)
        if snapshot.version != version:
            raise WorkspaceFileConflictError(f"补丁目标版本冲突，请重新读取: {uri}; expected={version}; actual={snapshot.version}")
        snapshots[name] = snapshot
    return snapshots


def _publish_steps(steps: list[TextPublication], *, observation_budget: int | None = None) -> tuple[list[dict[str, object]], dict[str, dict[str, object]], Exception | None]:
    attempted = 0
    failure: Exception | None = None
    confirmed: set[int] = set()
    unverified: set[int] = set()
    for step in steps:
        attempted += 1
        try:
            if step.content is None:
                if step.snapshot.before is None:
                    raise ValueError(f"删除源文件不存在: {step.snapshot.uri}")
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
    observations = {}
    for index, step in enumerate(steps):
        after, observation_error = _observe(step.snapshot, max_bytes=observation_budget)
        if after is not None and observation_budget is not None:
            observation_budget = max(0, observation_budget - len(after.content))
        observed = None if observation_error is not None else "absent" if after is None else after.content_hash
        if observation_error is None:
            try:
                text = "" if after is None else after.content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            except UnicodeError:
                # 外部 writer 发布了非文本；保留实际 hash 和未知效果，不能伪造可读 diff。
                observations[step.snapshot.uri] = {"after_error": "invalid_encoding"}
            else:
                observations[step.snapshot.uri] = {"after_exists": after is not None, "after_content": text}
        else:
            observations[step.snapshot.uri] = {"after_error": type(observation_error).__name__}
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
        if observations[step.snapshot.uri].get("after_error"):
            publications[-1]["content_observation_error"] = observations[step.snapshot.uri]["after_error"]
    return publications, observations, failure


def _observe(snapshot: FileSnapshot, *, max_bytes: int | None = None) -> tuple[FilePreimage | None, Exception | None]:
    try:
        if not snapshot.real_path.exists() and not snapshot.real_path.is_symlink():
            return None, None
        return read_file_preimage(snapshot.real_path, reference=snapshot.uri, max_bytes=min(_MAX_FILE_BYTES, max_bytes) if max_bytes is not None else _MAX_FILE_BYTES), None
    except (OSError, RuntimeError, ValueError) as error:
        return None, error


def text_mutation_error_detail(error: Exception) -> str:
    if isinstance(error, OSError) and (error.filename is not None or error.filename2 is not None):
        return f"{type(error).__name__}: {error.strerror}, errno={error.errno}"
    return str(error)


def validate_text_paths(paths: list[str], workspace_root: Path) -> None:
    targets = [_resolve_workspace_file(path, workspace_root) for path in paths]
    if len(targets) != len(set(targets)):
        raise ValueError("补丁中的多个路径指向同一文件")
    target_set = set(targets)
    for target in targets:
        parent = target.parent
        while parent != workspace_root:
            if parent in target_set:
                raise ValueError("补丁文件目标不能互为祖先与后代")
            if parent.exists() and not parent.is_dir():
                raise ValueError("补丁文件的现有父路径必须是目录")
            parent = parent.parent


def _resolve_workspace_file(file_path: str, workspace_root: Path) -> Path:
    if "://" in file_path or Path(file_path).is_absolute() or PureWindowsPath(file_path).drive:
        raise ValueError("apply_patch 文件路径无效: 补丁内文件名必须相对固定根，禁止绝对路径和 URI")
    if not file_path or ".." in Path(file_path).parts or "\x00" in file_path:
        raise ValueError("文本文件相对路径无效: 文件名不能为空、包含空字符或上级目录段")
    try:
        path = resolve_native_file_path(workspace_root, file_path)
    except ForbiddenError as error:
        raise ValueError("文本文件路径越出固定根") from error
    if path == workspace_root or not path.is_relative_to(workspace_root):
        raise ValueError("apply_patch 文件路径无效: 文件必须位于固定根内且不能等于根目录")
    return path


def text_mutation_failure(error: Exception) -> dict[str, object]:
    if isinstance(error, (ResourceUriGrammarError, ResourceUriResolveError)):
        owner = "resource_uri_grammar" if isinstance(error, ResourceUriGrammarError) else "resource_uri_resolver"
        code = error.reason_code
    else:
        owner = "resource_file_operations"
        code = ("output_limit_exceeded" if isinstance(error, TextMutationOutputLimitError) else
                "revision_conflict" if isinstance(error, WorkspaceFileConflictError) else
                "invalid_encoding" if isinstance(error, UnicodeError) else
                "invalid_patch" if isinstance(error, ValueError) else "execution_failed")
    return {"status": "error", "effect": "not_executed", "owner": owner, "code": code,
            "error_type": type(error).__name__, "error": text_mutation_error_detail(error), "retryable": False}
