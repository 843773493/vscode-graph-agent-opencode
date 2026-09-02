from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import BinaryIO, ClassVar, ParamSpec, Self, TypeVar

FOLDER_MANIFEST_NAME = ".boxteam-folder.json"
SESSION_MANIFEST_NAME = "session.json"
SESSION_CHILDREN_DIR_NAME = "children"
SESSION_ALLOCATION_MARKER_NAME = ".boxteam-session-allocating.json"
SESSION_ALLOCATION_TEMP_PREFIX = ".boxteam-session-allocating-"
PHYSICAL_LAYOUT_VERSION = 1
SESSION_TREE_LOCK_TIMEOUT_SECONDS = 5.0
SESSION_TREE_LOCK_POLL_INTERVAL_SECONDS = 0.01
_INVALID_SEGMENT_CHARS = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_STABLE_ID_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

P = ParamSpec("P")
R = TypeVar("R")


class _SessionTreeLockState:
    def __init__(self) -> None:
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.handle: BinaryIO | None = None


class SessionTreeLockTimeoutError(RuntimeError):
    """会话目录锁在有界时间内未释放。"""


class SessionTreeOperationLock:
    """为会话目录索引与物理树提供有界、可重入的进程间互斥。"""

    _registry_guard = threading.Lock()
    _states: ClassVar[dict[str, _SessionTreeLockState]] = {}

    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = SESSION_TREE_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("session tree lock timeout_seconds 必须大于 0")
        self._path = path.resolve()
        self._timeout_seconds = timeout_seconds
        with self._registry_guard:
            self._state = self._states.setdefault(
                str(self._path),
                _SessionTreeLockState(),
            )

    def __enter__(self) -> Self:
        if not self._state.thread_lock.acquire(timeout=self._timeout_seconds):
            raise SessionTreeLockTimeoutError(
                "会话目录锁获取超时: "
                f"path={self._path} timeout_seconds={self._timeout_seconds:g}"
            )
        if self._state.depth == 0:
            try:
                self._state.handle = self._acquire_file_lock()
            except BaseException:
                self._state.thread_lock.release()
                raise
        self._state.depth += 1
        return self

    def __exit__(self, *_: object) -> None:
        self._state.depth -= 1
        try:
            if self._state.depth == 0:
                handle = self._state.handle
                self._state.handle = None
                if handle is not None:
                    self._release_file_lock(handle)
        finally:
            self._state.thread_lock.release()

    def _acquire_file_lock(self) -> BinaryIO:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        deadline = time.monotonic() + self._timeout_seconds
        try:
            if os.name == "nt":
                # TODO: Windows CI 覆盖 session catalog 进程锁的并发行为。
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b" ")
                    handle.flush()
                handle.seek(0)
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError as error:
                        if time.monotonic() >= deadline:
                            raise SessionTreeLockTimeoutError(
                                "会话目录锁获取超时: "
                                f"path={self._path} timeout_seconds={self._timeout_seconds:g}"
                            ) from error
                        time.sleep(SESSION_TREE_LOCK_POLL_INTERVAL_SECONDS)
                    else:
                        break
            else:
                import fcntl

                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as error:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise SessionTreeLockTimeoutError(
                                "会话目录锁获取超时: "
                                f"path={self._path} timeout_seconds={self._timeout_seconds:g}"
                            ) from error
                        time.sleep(min(SESSION_TREE_LOCK_POLL_INTERVAL_SECONDS, remaining))
                    else:
                        break
        except BaseException:
            handle.close()
            raise
        return handle

    @staticmethod
    def _release_file_lock(handle: BinaryIO) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def session_tree_operation_locked(function: Callable[P, R]) -> Callable[P, R]:
    """在不改变业务方法签名的情况下保护 resolver 的公开操作。"""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        if not args:
            raise TypeError("session tree operation 缺少 resolver 实例")
        with args[0]._session_tree_operation_lock:
            return function(*args, **kwargs)

    return wrapped


@dataclass(frozen=True, slots=True)
class SessionPhysicalNode:
    node_id: str
    kind: str
    path: Path
    parent_node_id: str | None
    name: str
    created_at: datetime
    updated_at: datetime

def physical_segment(name: str, stable_id: str) -> str:
    """返回由稳定 ID 独占的物理路径段；显示名不得参与路径。"""
    del name
    if _STABLE_ID_SEGMENT.fullmatch(stable_id) is None:
        raise ValueError(f"稳定 ID 不能作为物理路径段: {stable_id!r}")
    return stable_id

def physical_display_segment(name: str) -> str:
    """返回物理路径段的显示名部分，预留固定稳定 ID 后缀空间。"""
    normalized = unicodedata.normalize("NFKC", name).strip().rstrip(". ")
    normalized = _INVALID_SEGMENT_CHARS.sub("_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().rstrip(". ")
    if not normalized:
        normalized = "未命名"
    if normalized.upper() in _WINDOWS_RESERVED_NAMES:
        normalized = f"_{normalized}"
    max_name_length = 96 - len("--12345678")
    normalized = normalized[:max_name_length].rstrip(". ") or "未命名"
    return normalized

def validate_generator_physical_segment(value: str) -> None:
    """校验生成器显示名；显示名不再参与物理路径。"""
    normalized = value.strip()
    if not normalized or normalized in {".", ".."}:
        raise ValueError(f"命名路径段非法: {value!r}")

def display_name_from_segment(segment: str, stable_id: str) -> str:
    for suffix in (f"--{stable_id}", f"--{stable_id[-8:]}"):
        if segment.endswith(suffix):
            return segment[: -len(suffix)] or "未命名"
    return segment

def _process_identity(pid: int) -> str | None:
    """在支持 `/proc` 的系统上读取可抵御 PID 重用的进程启动标识。"""
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        raw = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    closing_parenthesis = raw.rfind(")")
    if closing_parenthesis < 0:
        raise RuntimeError(f"无法解析进程 stat: {stat_path}")
    fields_after_name = raw[closing_parenthesis + 2 :].split()
    if len(fields_after_name) <= 19:
        raise RuntimeError(f"进程 stat 缺少启动时间字段: {stat_path}")
    return fields_after_name[19]

def _process_matches_identity(pid: int, expected_identity: object) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # TODO: Windows 对无效或超出范围的 PID 抛出 WinError 87，而不是 ProcessLookupError。
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 87:
            return False
        raise
    if not isinstance(expected_identity, str) or not expected_identity:
        return True
    actual_identity = _process_identity(pid)
    return actual_identity is None or actual_identity == expected_identity

def _navigation_signature(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size

def _read_json_object(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"JSON 文件必须是 object: {path}")
    return {str(key): value for key, value in raw.items()}

def _parse_datetime(value: object, path: Path) -> datetime:
    parsed = _parse_optional_datetime(value)
    if parsed is None:
        raise RuntimeError(f"manifest 缺少合法时间字段: {path}")
    return parsed

def _parse_optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value)

def _rewrite_legacy_locator_value(
    value: object,
    session_id: str,
    *,
    field_name: str | None = None,
    attachment_locators: dict[str, str] | None = None,
) -> tuple[object, bool]:
    if isinstance(value, str):
        if field_name not in {"file_id", "read_path", "artifact_path"}:
            return value, False
        if field_name == "file_id" and value.startswith("inline:"):
            if attachment_locators is None:
                return value, False
            locator = attachment_locators.get(value)
            if locator is None:
                raise RuntimeError(
                    "旧会话数据引用了无法从请求日志恢复的 inline 附件: "
                    f"session_id={session_id}, file_id={value!r}"
                )
            return locator, True
        return _rewrite_legacy_locator_string(
            value,
            session_id,
            field_name=field_name,
        )
    if isinstance(value, list):
        changed = False
        items: list[object] = []
        for item in value:
            rewritten, item_changed = _rewrite_legacy_locator_value(
                item,
                session_id,
                field_name=field_name,
                attachment_locators=attachment_locators,
            )
            items.append(rewritten)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, tuple):
        rewritten, changed = _rewrite_legacy_locator_value(
            list(value),
            session_id,
            attachment_locators=attachment_locators,
        )
        if not isinstance(rewritten, list):
            raise TypeError("tuple 定位符迁移结果必须是 list")
        return tuple(rewritten), changed
    if isinstance(value, dict):
        changed = False
        result: dict[object, object] = {}
        for key, item in value.items():
            rewritten, item_changed = _rewrite_legacy_locator_value(
                item,
                session_id,
                field_name=key if isinstance(key, str) else None,
                attachment_locators=attachment_locators,
            )
            result[key] = rewritten
            changed = changed or item_changed
        return result, changed
    if hasattr(value, "model_fields") and hasattr(value, "model_copy"):
        changed = False
        updates: dict[str, object] = {}
        for name in value.__class__.model_fields:
            rewritten, field_changed = _rewrite_legacy_locator_value(
                getattr(value, name),
                session_id,
                field_name=name,
                attachment_locators=attachment_locators,
            )
            if field_changed:
                updates[name] = rewritten
                changed = True
        return value.model_copy(update=updates), changed
    return value, False

def _rewrite_legacy_locator_string(
    value: str,
    session_id: str,
    *,
    field_name: str | None,
) -> tuple[str, bool]:
    escaped_session_id = re.escape(session_id)
    absolute_pattern = re.compile(
        rf"(?:[A-Za-z]:)?(?:[/\\][^/\\\s\"'<>]+)*"
        rf"[/\\]\.boxteam[/\\]sessions[/\\]{escaped_session_id}[/\\]"
    )
    relative_pattern = re.compile(
        rf"(?<![\w.-])\.boxteam[/\\]sessions[/\\]{escaped_session_id}[/\\]"
    )
    replacement = (
        f"boxteam-session://{session_id}/"
        if field_name == "file_id"
        else f"session-artifacts/{session_id}/"
    )
    updated = absolute_pattern.sub(replacement, value)
    updated = relative_pattern.sub(replacement, updated)
    return updated, updated != value

def _rewrite_checkpoint_blob(
    path: Path,
    session_id: str,
    *,
    attachment_locators: dict[str, str] | None = None,
) -> bool:
    raw = path.read_bytes()
    if not raw:
        return False
    try:
        value = json.loads(raw.decode("utf-8"))
        serialization = "json"
    except (UnicodeDecodeError, json.JSONDecodeError):
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        serializer = JsonPlusSerializer()
        value = serializer.loads_typed(("msgpack", raw))
        serialization = "msgpack"
    rewritten, changed = _rewrite_legacy_locator_value(
        value,
        session_id,
        attachment_locators=attachment_locators,
    )
    if not changed:
        return False
    if serialization == "json":
        encoded = json.dumps(
            rewritten,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    else:
        serializer = JsonPlusSerializer()
        type_tag, encoded = serializer.dumps_typed(rewritten)
        if type_tag != "msgpack":
            raise RuntimeError(
                f"checkpoint blob 重写后序列化类型变化: path={path}, type={type_tag}"
            )
    _atomic_write_bytes(path, encoded)
    return True

def _atomic_write_json_value(path: Path, value: object) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

def _atomic_write_text(path: Path, value: str) -> None:
    _atomic_write_bytes(path, value.encode("utf-8"))

def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)

def _atomic_write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
