from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_TOOL_SELECTION_LOCK_TIMEOUT_SECONDS = 10.0
_TOOL_SELECTION_LOCK_POLL_INTERVAL_SECONDS = 0.05


class ToolSelectionStore:
    """持久化当前工作区内每个 Agent 的工具能力运行时覆盖。"""

    def __init__(self, *, boxteam_root: Path) -> None:
        self._path = boxteam_root / "settings" / "tool_selection.json"
        self._lock_path = boxteam_root / "settings" / "tool_selection.lock"

    def execution_overrides(self, agent_id: str) -> dict[str, bool]:
        settings = self._agent_settings(agent_id)
        return dict(settings["execution_overrides"])

    def model_visibility_overrides(self, agent_id: str) -> dict[str, bool]:
        settings = self._agent_settings(agent_id)
        return dict(settings["model_visibility_overrides"])

    def disabled_tools(self, agent_id: str) -> set[str]:
        return {
            tool_id
            for tool_id, enabled in self.execution_overrides(agent_id).items()
            if enabled is False
        }

    def apply_changes(
        self,
        *,
        agent_id: str,
        changes: Mapping[str, tuple[bool, bool]],
    ) -> None:
        if not changes:
            raise ValueError("工具能力变更不能为空")
        with self._file_lock(shared=False):
            payload = self._read_unlocked()
            settings = self._agent_settings_from_payload(payload, agent_id)
            execution = dict(settings["execution_overrides"])
            visibility = dict(settings["model_visibility_overrides"])
            for tool_id, (execution_enabled, model_visible) in changes.items():
                if not execution_enabled and model_visible:
                    raise ValueError(
                        f"工具 {tool_id!r} 未启用执行能力时不能对模型可见"
                    )
                execution[tool_id] = execution_enabled
                visibility[tool_id] = model_visible
            payload[agent_id] = {
                "execution_overrides": dict(sorted(execution.items())),
                "model_visibility_overrides": dict(sorted(visibility.items())),
            }
            self._write(payload)

    def _agent_settings(self, agent_id: str) -> dict[str, object]:
        with self._file_lock(shared=True):
            return self._agent_settings_from_payload(self._read_unlocked(), agent_id)

    @staticmethod
    def _agent_settings_from_payload(
        payload: dict[str, object],
        agent_id: str,
    ) -> dict[str, object]:
        raw_settings = payload.get(agent_id, {})
        if not isinstance(raw_settings, dict):
            raise TypeError(f"工具选择配置的 Agent 项必须是对象: agent={agent_id}")
        execution = raw_settings.get("execution_overrides", {})
        visibility = raw_settings.get("model_visibility_overrides", {})
        if not isinstance(execution, dict) or not all(
            isinstance(name, str) and isinstance(value, bool)
            for name, value in execution.items()
        ):
            raise TypeError(
                f"工具选择配置 execution_overrides 格式错误: agent={agent_id}"
            )
        if not isinstance(visibility, dict) or not all(
            isinstance(name, str) and isinstance(value, bool)
            for name, value in visibility.items()
        ):
            raise TypeError(
                "工具选择配置 model_visibility_overrides 格式错误: "
                f"agent={agent_id}"
            )
        return {
            "execution_overrides": dict(execution),
            "model_visibility_overrides": dict(visibility),
        }

    def _read_unlocked(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(f"工具选择配置必须是对象: {self._path}")
        for agent_id, settings in payload.items():
            if not isinstance(agent_id, str) or not isinstance(settings, dict):
                raise TypeError(f"工具选择配置 Agent 项格式错误: {self._path}")
            self._agent_settings_from_payload(payload, agent_id)
        return payload

    def _write(self, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._path)

    @contextmanager
    def _file_lock(self, *, shared: bool) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+", encoding="utf-8") as lock_file:
            acquired = False
            try:
                _acquire_file_lock(lock_file, shared=shared, path=self._lock_path)
                acquired = True
                yield
            finally:
                if acquired:
                    _release_file_lock(lock_file)


def _acquire_file_lock(
    lock_file: BinaryIO,
    *,
    shared: bool,
    path: Path,
) -> None:
    """有界获取配置锁，避免 Agent 初始化无限等待一个失联写者。"""
    deadline = time.monotonic() + _TOOL_SELECTION_LOCK_TIMEOUT_SECONDS
    if os.name == "nt":
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(" ")
            lock_file.flush()
        lock_file.seek(0)
        mode = msvcrt.LK_NBLCK
        while True:
            try:
                msvcrt.locking(lock_file.fileno(), mode, 1)
            except OSError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "工具选择配置锁获取超时: "
                        f"path={path} "
                        f"timeout_seconds={_TOOL_SELECTION_LOCK_TIMEOUT_SECONDS:g}"
                    ) from error
                time.sleep(min(_TOOL_SELECTION_LOCK_POLL_INTERVAL_SECONDS, remaining))
            else:
                return

    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    while True:
        try:
            fcntl.flock(lock_file.fileno(), mode | fcntl.LOCK_NB)
        except BlockingIOError as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "工具选择配置锁获取超时: "
                    f"path={path} "
                    f"timeout_seconds={_TOOL_SELECTION_LOCK_TIMEOUT_SECONDS:g}"
                ) from error
            time.sleep(min(_TOOL_SELECTION_LOCK_POLL_INTERVAL_SECONDS, remaining))
        else:
            return


def _release_file_lock(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
