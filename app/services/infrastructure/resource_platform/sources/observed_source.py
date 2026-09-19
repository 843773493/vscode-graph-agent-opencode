"""不可变 observed source 合同:descriptor、owner 私有可重建 handle、

ObservedSourceRevision、内置文件 StableSourceReader 与有界 SourceReconciler。

OpenSpec add-context-injection-lifecycle 3.2/3.3:来源观察、稳定快照与语义
发布的统一底层。文件来源使用允许根/no-follow/普通文件、前后 signature、
双读同 hash、最多三次 attempt、固定 byte 上限、严格 UTF-8 与完整原始
byte hash;Gateway 内部快照与权威内存状态使用各自可验证版本 token,
不套用文件双读。失败保留上一份 valid revision 并显式 unavailable。

本模块是 sources 域 owner:CSM、middleware、skill_load 与 model-call
preparation 不得直接调用 reader,只消费 Registry published revision。
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

# 固定阈值:与历史 workspace_file_resources 常量保持一致,调用方不可覆盖。
MAX_STABLE_READ_BYTES = 512 * 1024
STABLE_READ_ATTEMPTS = 3

_SOURCE_KINDS = frozenset({"file", "gateway_snapshot", "memory_state"})
_REVISION_KINDS = frozenset({"file_byte_hash", "version_token"})
_REASON_CODES = frozenset(
    {
        "outside_allowed_root",
        "symlink_rejected",
        "not_regular_file",
        "oversize",
        "invalid_utf8",
        "missing",
        "unstable_read",
        "invalid_version_token",
    }
)


class StableSourceReadError(RuntimeError):
    """稳定读取失败的显式错误;reason_code 是闭合集合。"""

    def __init__(self, reason_code: str, message: str) -> None:
        if reason_code not in _REASON_CODES:
            raise ValueError(f"未知 StableSourceReadError reason_code: {reason_code}")
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ObservedSourceDescriptor:
    """owner scope 内登记来源的不可变描述;不携带 locator。"""

    source_id: str
    source_kind: str
    display_uri: str
    entry_identity: str

    def __post_init__(self) -> None:
        for field_name in ("source_id", "entry_identity"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"ObservedSourceDescriptor.{field_name} 必须是非空字符串"
                )
        if self.source_kind not in _SOURCE_KINDS:
            raise ValueError(
                f"未知 ObservedSourceDescriptor.source_kind: {self.source_kind}"
            )
        if not isinstance(self.display_uri, str) or not self.display_uri.startswith(
            "boxteam://"
        ):
            raise ValueError(
            "ObservedSourceDescriptor.display_uri 必须是 boxteam:// 虚拟 URI"
        )


@dataclass(frozen=True, slots=True)
class ObservedSourceHandle:
    """实际 owner 私有的可重建读取句柄;不进入 CSM/middleware/工具结果。

    文件来源携带 file_path 与 allowed_root(均为绝对路径字符串);
    gateway_snapshot/memory_state 携带 version_token_reader,返回
    "(version_token, content)" 二元组。entry_identity 是持久 catalog entry
    identity,进程重启后由 owner 据此重建同一 handle。
    """

    descriptor: ObservedSourceDescriptor
    file_path: str | None = None
    allowed_root: str | None = None
    version_token_reader: Callable[[], tuple[str, str]] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, ObservedSourceDescriptor):
            raise TypeError(
            "ObservedSourceHandle.descriptor 必须是 ObservedSourceDescriptor"
        )
        if self.descriptor.source_kind == "file":
            if self.version_token_reader is not None:
                raise ValueError("文件来源 handle 不得携带 version_token_reader")
            for field_name in ("file_path", "allowed_root"):
                value = getattr(self, field_name)
                if not isinstance(value, str) or not value:
                    raise ValueError(
                    f"文件来源 handle.{field_name} 必须是绝对路径字符串"
                )
            resolved_root = Path(self.allowed_root or "").resolve()
            resolved_path = Path(self.file_path or "").resolve()
            if not resolved_path.is_relative_to(resolved_root):
                raise StableSourceReadError(
                    "outside_allowed_root",
                    f"来源路径越过允许根目录: {self.file_path}",
                )
            return
        if self.file_path is not None or self.allowed_root is not None:
            raise ValueError("非文件来源 handle 不得携带文件 locator")
        if self.version_token_reader is None or not callable(self.version_token_reader):
            raise ValueError("gateway/memory 来源 handle 必须携带 version_token_reader")


@dataclass(frozen=True, slots=True)
class ObservedSourceRevision:
    """一次稳定观察发布的不可变来源 revision。

    文件来源 revision 是完整原始 byte 的 sha256;token 来源 revision 是
    owner 提供的可验证版本 token。available=False 时保留上一份 valid
    revision(retained_revision)并携带显式 reason_code 诊断,不得冒充
    新版本。
    """

    source_id: str
    revision: str
    revision_kind: str
    content: str
    byte_length: int
    available: bool = True
    error: str | None = None
    error_code: str | None = None
    retained_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id:
            raise ValueError("ObservedSourceRevision.source_id 必须是非空字符串")
        if self.revision_kind not in _REVISION_KINDS:
            raise ValueError(
            f"未知 ObservedSourceRevision.revision_kind: {self.revision_kind}"
        )
        if not isinstance(self.revision, str):
            raise TypeError("ObservedSourceRevision.revision 必须是字符串")
        if (
            not isinstance(self.byte_length, int)
            or isinstance(self.byte_length, bool)
            or self.byte_length < 0
        ):
            raise ValueError("ObservedSourceRevision.byte_length 必须是非负整数")
        if self.available:
            if not self.revision or self.error is not None or self.retained_revision is not None:
                raise ValueError(
                "available revision 必须有 revision 且不得携带错误/保留字段"
            )
            if len(self.content.encode("utf-8")) != self.byte_length:
                raise ValueError(
                "available revision 的 byte_length 与 UTF-8 内容长度不一致"
            )
            return
        if self.error is None or self.error_code not in _REASON_CODES:
            raise ValueError("unavailable revision 必须携带显式 reason_code 诊断")
        if self.retained_revision is not None and not self.retained_revision:
            raise ValueError("retained_revision 必须是非空字符串或 None")


class StableSourceReader:
    """内置文件来源的稳定读取协议。

    每次读取:允许根校验、最终组件 no-follow(islink + O_NOFOLLOW)、
    普通文件校验、固定 byte 上限;最多三次 attempt,每次 attempt 内做
    前后 fstat signature 比对与同 fd 双读 hash 比对;严格 UTF-8 解码;
    revision 为完整原始 byte 的 sha256。任何失败抛出带 reason_code 的
    StableSourceReadError,由 Reconciler 保留上一份 valid revision。
    """

    def read(self, handle: ObservedSourceHandle) -> ObservedSourceRevision:
        if handle.descriptor.source_kind != "file":
            raise ValueError("StableSourceReader 只接受文件来源 handle")
        file_path = Path(handle.file_path or "")
        if os.path.islink(file_path):
            raise StableSourceReadError(
            "symlink_rejected", f"来源路径是符号链接，拒绝跟随: {handle.file_path}"
        )
        open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(file_path, open_flags)
        except FileNotFoundError as error:
            raise StableSourceReadError(
            "missing", f"来源文件不存在: {handle.file_path}"
            ) from error
        except OSError as error:
            if getattr(error, "errno", None) == getattr(os, "ELOOP", None):
                raise StableSourceReadError(
                "symlink_rejected",
                f"打开时检测到符号链接(O_NOFOLLOW): {handle.file_path}",
                ) from error
            raise
        try:
            st_first = os.fstat(fd)
            if not stat_module.S_ISREG(st_first.st_mode):
                raise StableSourceReadError(
                "not_regular_file", f"来源不是普通文件: {handle.file_path}"
                )
            if st_first.st_size > MAX_STABLE_READ_BYTES:
                raise StableSourceReadError(
                "oversize",
                f"来源文件 {st_first.st_size} 字节超过固定上限 {MAX_STABLE_READ_BYTES}",
                )
            last_error: StableSourceReadError | None = None
            for _attempt in range(STABLE_READ_ATTEMPTS):
                first = self._read_all(fd, st_first.st_size)
                st_between = os.fstat(fd)
                second = self._read_all(fd, st_between.st_size)
                st_after = os.fstat(fd)
                signature_before = (
                st_first.st_ino, st_first.st_size, st_first.st_mtime_ns
                )
                signature_middle = (
                st_between.st_ino, st_between.st_size, st_between.st_mtime_ns
                )
                signature_after = (
                st_after.st_ino, st_after.st_size, st_after.st_mtime_ns
                )
                if (
                signature_before != signature_middle
                or signature_middle != signature_after
                ):
                    last_error = StableSourceReadError(
                    "unstable_read",
                    f"来源文件在读取期间持续变化: {handle.file_path}",
                    )
                    continue
                if first != second:
                    last_error = StableSourceReadError(
                    "unstable_read", f"双读内容不一致: {handle.file_path}"
                    )
                    continue
                try:
                    content = first.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise StableSourceReadError(
                    "invalid_utf8", f"来源文件不是有效 UTF-8: {handle.file_path}"
                    ) from error
                digest = hashlib.sha256(first).hexdigest()
                return ObservedSourceRevision(
                source_id=handle.descriptor.source_id,
                revision=f"sha256:{digest}",
                revision_kind="file_byte_hash",
                content=content,
                byte_length=len(first),
                )
            assert last_error is not None
            raise last_error
        finally:
            os.close(fd)

    @staticmethod
    def _read_all(fd: int, size: int) -> bytes:
        chunks: list[bytes] = []
        offset = 0
        while True:
            chunk = os.pread(fd, 65536, offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
            if offset > size:
            # 读取超过 signature 声明的大小:留给 signature 比对判定不稳定。
                break
        return b"".join(chunks)


class SourceReconciler:
    """已知来源的有界 reconcile:发布不可变 ObservedSourceRevision。

    - reconcile/reconcile_all 只处理已登记 handle,不扫描目录;
    - 文件失败(token 来源 owner 也可抛 StableSourceReadError)时保留
      上一份 valid revision 并标记 unavailable;
    - mark_committed 由消费方在 revision 进入已提交上下文后回执,
      revision_states 区分 observed/pending/committed;diff 基准是
      已提交(可见)revision。rewind 感知的 latest-visible-committed
      收敛属于 CSM/owner 层(OpenSpec 3.4/3.6/3.7),不在本模块。
    """

    def __init__(
        self,
        *,
        handles: Mapping[str, ObservedSourceHandle] | None = None,
        notify: Callable[[ObservedSourceRevision], None] | None = None,
    ) -> None:
        self._reader = StableSourceReader()
        self._handles: dict[str, ObservedSourceHandle] = dict(handles or {})
        self._revisions: dict[str, ObservedSourceRevision] = {}
        self._committed: dict[str, str] = {}
        self._notify = notify

    def bind_handle(self, handle: ObservedSourceHandle) -> None:
        """登记或重绑来源 handle(重启重建入口);重复绑定同一 identity 幂等。"""
        source_id = handle.descriptor.source_id
        existing = self._handles.get(source_id)
        if existing is not None and existing != handle:
            raise ValueError(f"来源 handle 绑定冲突: source_id={source_id}")
        self._handles[source_id] = handle

    def reconcile(self, source_id: str) -> ObservedSourceRevision:
        handle = self._handles.get(source_id)
        if handle is None:
            raise KeyError(f"来源尚未登记: source_id={source_id}")
        try:
            revision = self._read_once(handle)
        except StableSourceReadError as error:
            revision = self._retain_previous(source_id, handle, error)
        self._store_and_notify(revision)
        return revision

    def reconcile_all(self) -> tuple[ObservedSourceRevision, ...]:
        """对全部已登记来源做一次有界 reconcile(不扫描、不递归)。"""
        return tuple(self.reconcile(source_id) for source_id in tuple(self._handles))

    def observed_revision(self, source_id: str) -> ObservedSourceRevision:
        revision = self._revisions.get(source_id)
        if revision is None:
            raise KeyError(f"来源尚未完成首次 reconcile: source_id={source_id}")
        return revision

    def mark_committed(self, source_id: str, revision: str) -> None:
        """消费方回执:该 revision 已进入已提交(可见)上下文。"""
        if not isinstance(revision, str) or not revision:
            raise ValueError("mark_committed.revision 必须是非空字符串")
        observed = self._revisions.get(source_id)
        if observed is None:
            raise KeyError(f"来源尚未完成首次 reconcile: source_id={source_id}")
        if revision not in {observed.revision, observed.retained_revision}:
            raise ValueError(
            f"回执 revision 不是该来源已观察事实: source_id={source_id} revision={revision}"
        )
        self._committed[source_id] = revision

    def revision_states(self, source_id: str) -> tuple[str, str | None, str | None]:
        """返回 (observed, pending, committed)。

        observed 是最近一次成功稳定读取的 revision;unavailable 时与
        retained_revision 同值。pending 仅在 observed != committed 时非空;
        多次未提交变化在这里自然合并为 committed→observed 一个待消费事实。
        """
        observed = self.observed_revision(source_id)
        committed = self._committed.get(source_id)
        if observed.available and observed.revision != committed:
            pending: str | None = observed.revision
        elif (
            not observed.available
            and observed.retained_revision is not None
            and observed.retained_revision != committed
        ):
            # 失败保留的旧 valid revision 仍是最新已知事实。
            pending = observed.retained_revision
        else:
            pending = None
        return observed.revision, pending, committed

    def _read_once(self, handle: ObservedSourceHandle) -> ObservedSourceRevision:
        if handle.descriptor.source_kind == "file":
            return self._reader.read(handle)
        token, content = handle.version_token_reader()
        if not isinstance(token, str) or not token:
            raise StableSourceReadError(
            "invalid_version_token",
            f"来源版本 token 必须是非空字符串: {handle.descriptor.source_id}",
            )
        if not isinstance(content, str):
            raise TypeError(f"来源版本内容必须是字符串: {handle.descriptor.source_id}")
        return ObservedSourceRevision(
        source_id=handle.descriptor.source_id,
        revision=token,
        revision_kind="version_token",
        content=content,
        byte_length=len(content.encode("utf-8")),
        )

    def _retain_previous(
        self,
        source_id: str,
        handle: ObservedSourceHandle,
        error: StableSourceReadError,
    ) -> ObservedSourceRevision:
        previous = self._revisions.get(source_id)
        previous_valid = previous is not None and previous.available
        retained = previous.revision if previous_valid else None
        kind = handle.descriptor.source_kind
        revision_kind = "file_byte_hash" if kind == "file" else "version_token"
        return ObservedSourceRevision(
        source_id=source_id,
        revision=retained or "",
        revision_kind=revision_kind,
        content=previous.content if previous_valid else "",
        byte_length=previous.byte_length if previous_valid else 0,
        available=False,
        error=f"{error.reason_code}: {error}",
        error_code=error.reason_code,
        retained_revision=retained,
        )

    def _store_and_notify(self, revision: ObservedSourceRevision) -> None:
        previous = self._revisions.get(revision.source_id)
        self._revisions[revision.source_id] = revision
        if self._notify is None:
            return
        if previous is not None and (previous.revision, previous.available) == (
        revision.revision,
        revision.available,
        ):
            return
        self._notify(revision)


__all__ = [
    "MAX_STABLE_READ_BYTES",
    "STABLE_READ_ATTEMPTS",
    "ObservedSourceDescriptor",
    "ObservedSourceHandle",
    "ObservedSourceRevision",
    "SourceReconciler",
    "StableSourceReadError",
    "StableSourceReader",
]
