from __future__ import annotations

import io
import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from uuid import uuid4

import portalocker
import psutil

from app.core.file_permissions import restrict_private_path_access
from app.core.logging_contract import RuntimeLogContract, load_runtime_log_contract
from app.gateway.runtime.process_groups import (
    _capture_posix_group_leader_start_marker,
    _posix_group_is_alive,
)

logger = logging.getLogger(__name__)
_OWNERSHIP_LOCK = RLock()


@dataclass(frozen=True, slots=True)
class ProcessLogOwnership:
    path: Path
    owner_id: str

    @property
    def record_path(self) -> Path:
        return self.path.with_name(self.path.name + ".owner.json")

    @contextmanager
    def guard(self) -> Iterator[None]:
        # 短临界区锁文件保持原 inode，不删除锁文件制造另一个锁域。
        guard_path = self.path.with_name(self.path.name + ".owner.lock")
        with _OWNERSHIP_LOCK:
            if guard_path.is_symlink() or self.record_path.is_symlink():
                raise RuntimeError("日志所有权文件不允许是符号链接")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(guard_path, flags, 0o600), "a+b") as stream:
                restrict_private_path_access(guard_path, descriptor=stream.fileno())
                portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
                yield

    def read(self) -> dict[str, object] | None:
        try:
            payload = json.loads(self.record_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise RuntimeError("进程日志所有权记录非法")
        return payload

    def write(self, payload: dict[str, object]) -> None:
        temporary = self.record_path.with_name(self.record_path.name + f".{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                restrict_private_path_access(temporary, descriptor=stream.fileno())
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.record_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _owned(self) -> dict[str, object]:
        payload = self.read()
        if payload is None or payload.get("owner_id") != self.owner_id:
            raise RuntimeError("进程日志所有权已变化")
        return payload

    def begin_spawn(self) -> None:
        with self.guard():
            payload = self._owned()
            if payload.get("phase") != "controller":
                raise RuntimeError("进程日志不能重复交付子进程")
            self.write({**payload, "phase": "spawning"})

    def bind_process(self, pid: int) -> None:
        with self.guard():
            payload = self._owned()
            if payload.get("phase") != "spawning":
                raise RuntimeError("进程日志交付阶段不匹配")
            process = psutil.Process(pid)
            marker = _capture_posix_group_leader_start_marker(pid) if os.name == "posix" else None
            if os.name == "posix" and marker is None:
                raise RuntimeError("子进程已退出，无法确认日志写入组")
            self.write({**payload, "phase": "process", "pid": pid,
                        "created_at": process.create_time(), "group_start_marker": marker})

    def release(self) -> None:
        """只有原控制器或已核实整个进程组关闭的 owner 才可释放。"""
        with self.guard():
            payload = self.read()
            if payload is None:
                return
            if payload.get("owner_id") != self.owner_id:
                raise RuntimeError("进程日志所有权已变化")
            self.record_path.unlink()

    def discard(self) -> None:
        """构造失败时仍在原 guard 内，仅撤销本次写入。"""
        payload = self.read()
        if payload is not None and payload.get("owner_id") == self.owner_id:
            self.record_path.unlink()


class ProcessLogFile(io.TextIOWrapper):
    def __init__(self, descriptor: int, ownership: ProcessLogOwnership, sink: dict[str, str]):
        self.ownership = ownership
        self.sink = sink
        self.child_started = False
        self.ownership_released = False
        self._constructed = False
        self.descriptor = descriptor
        super().__init__(io.FileIO(descriptor, "a", closefd=False), encoding="utf-8")
        self._constructed = True

    def begin_spawn(self) -> None:
        self.ownership.begin_spawn()
        self.child_started = True

    def release_ownership(self) -> None:
        if not self.ownership_released:
            self.ownership.release()
            self.ownership_released = True

    def close(self) -> None:
        if not self._constructed or self.closed:
            return
        try:
            super().close()
        finally:
            os.close(self.descriptor)
        if not self.child_started:
            self.release_ownership()


def _previous_writer_is_alive(payload: dict[str, object]) -> bool:
    phase = payload.get("phase")
    if phase == "spawning":
        raise RuntimeError("日志交付曾中断，无法证明子进程是否仍持有描述符；拒绝轮转")
    pid = payload.get("pid")
    created_at = payload.get("created_at")
    if type(pid) is not int or not isinstance(created_at, (int, float)):
        raise RuntimeError("日志写入者身份记录不完整")
    if phase == "process" and os.name == "posix":
        marker = payload.get("group_start_marker")
        if not isinstance(marker, (int, float)):
            raise RuntimeError("日志写入组身份记录不完整")
        return _posix_group_is_alive(pid, marker)
    try:
        process = psutil.Process(pid)
        same_process = process.create_time() == created_at
        alive = same_process and process.status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
    except psutil.NoSuchProcess:
        alive = False
    if phase == "controller":
        return alive
    if phase != "process":
        raise RuntimeError("日志所有权阶段非法")
    if not alive and sys.platform == "win32":
        # Windows 根退出不代表后代排空；独占打开可核实是否仍有继承句柄。
        import pywintypes
        import win32file

        try:
            handle = win32file.CreateFile(
                str(payload["path"]), win32file.GENERIC_WRITE, 0, None,
                win32file.OPEN_EXISTING, 0, None,
            )
        except pywintypes.error as error:
            if error.winerror == 32:  # ERROR_SHARING_VIOLATION
                return True
            raise
        handle.Close()
        return False
    return True


@dataclass(frozen=True, slots=True)
class ProcessLogStore:
    root: Path
    contract: RuntimeLogContract = field(default_factory=load_runtime_log_contract)

    def path_for(self, role: str, instance: str | int | None = None) -> Path:
        return self.contract.path_for(self.root, role, instance)

    def open(self, role: str, instance: str | int | None = None) -> ProcessLogFile:
        log_path = self.path_for(role, instance)
        file_name = log_path.name
        stage = "create_directory"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            stage = "directory_permissions"
            restrict_private_path_access(log_path.parent, is_directory=True)
            if log_path.is_symlink():
                logger.error("进程日志文件不允许是符号链接: %s", log_path)
                raise RuntimeError(f"进程日志文件不允许是符号链接: log={file_name}")
            ownership = ProcessLogOwnership(log_path.absolute(), uuid4().hex)
            stage = "ownership"
            with ownership.guard():
                previous = ownership.read()
                if previous is not None and _previous_writer_is_alive(previous):
                    raise RuntimeError("进程日志仍有活动写入者，拒绝重开或轮转")
                stage = "rotate"
                self._rotate_if_needed(log_path)
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                stage = "open"
                descriptor = os.open(log_path, flags, 0o600)
                try:
                    stage = "file_permissions"
                    restrict_private_path_access(log_path, descriptor=descriptor)
                    identity = os.fstat(descriptor)
                    sink = {"path": str(ownership.path), "owner_id": ownership.owner_id,
                            "role": role, "source": "gateway", "device": str(identity.st_dev),
                            "inode": str(identity.st_ino)}
                    stage = "ownership_record"
                    ownership.write({"version": 1, **sink, "phase": "controller",
                                     "pid": os.getpid(), "created_at": psutil.Process().create_time()})
                    return ProcessLogFile(descriptor, ownership, sink)
                except BaseException:
                    os.close(descriptor)
                    ownership.discard()
                    raise
        except OSError as error:
            logger.exception("进程日志访问失败: root=%s log=%s stage=%s", self.root, file_name, stage)
            detail = f"进程日志访问失败: log={file_name}, stage={stage}, error_type={type(error).__name__}"
            winerror = getattr(error, "winerror", None)
            if winerror is not None:
                detail += f", winerror={winerror}"
            raise type(error)(error.errno, detail) from error

    def adopt(self, role: str, instance: int, proof: dict[str, object]) -> ProcessLogOwnership:
        sink = proof.get("log_sink")
        if not isinstance(sink, dict) or not isinstance(sink.get("owner_id"), str):
            raise TypeError("受管进程缺少已核实的日志去向")
        path = self.path_for(role, instance).absolute()
        ownership = ProcessLogOwnership(path, sink["owner_id"])
        try:
            with ownership.guard():
                payload = ownership._owned()
                if payload.get("phase") != "process" or payload.get("pid") != proof.get("pid"):
                    raise RuntimeError("受管进程日志写入者不匹配")
                for key in ("path", "owner_id", "role", "source", "device", "inode"):
                    if sink.get(key) != payload.get(key):
                        raise RuntimeError(f"受管进程日志去向不匹配: field={key}")
                if sink.get("path") != str(path) or not _previous_writer_is_alive(payload):
                    raise RuntimeError("受管进程日志写入者已变化")
                identity = path.stat()
                if (str(identity.st_dev), str(identity.st_ino)) != (sink.get("device"), sink.get("inode")):
                    raise RuntimeError("受管进程日志文件已被替换")
        except OSError as error:
            logger.exception("接管进程日志访问失败: role=%s path=%s", role, path)
            raise type(error)(error.errno, f"接管进程日志访问失败: role={role}, stage=adopt") from error
        return ownership

    def _rotate_if_needed(self, log_path: Path) -> None:
        if not log_path.exists():
            return
        if log_path.stat().st_size < self.contract.max_bytes:
            return
        for index in range(self.contract.archives - 1, 0, -1):
            source = log_path.with_name(f"{log_path.name}.{index}")
            target = log_path.with_name(f"{log_path.name}.{index + 1}")
            if source.is_symlink() or target.is_symlink():
                logger.error("进程日志轮转目标不允许是符号链接: source=%s target=%s", source, target)
                raise RuntimeError(
                    f"进程日志轮转目标不允许是符号链接: source={source.name} target={target.name}"
                )
            if source.exists():
                source.replace(target)
        rotated_path = log_path.with_name(f"{log_path.name}.1")
        if rotated_path.is_symlink():
            logger.error("进程日志轮转目标不允许是符号链接: %s", rotated_path)
            raise RuntimeError(f"进程日志轮转目标不允许是符号链接: log={rotated_path.name}")
        log_path.replace(rotated_path)
