from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

import portalocker

from app.abstractions.tool_selection import ToolSelectionSnapshot

_TOOL_SELECTION_LOCK_TIMEOUT_SECONDS = 10.0
_TOOL_SELECTION_LOCK_POLL_INTERVAL_SECONDS = 0.05


class ToolSelectionStore:
    """持久化当前工作区内每个 Agent 的工具能力运行时覆盖。"""

    def __init__(self, *, boxteam_root: Path) -> None:
        self._path = boxteam_root / "settings" / "tool_selection.json"
        self._lock_path = boxteam_root / "settings" / "tool_selection.lock"

    def snapshot(self, agent_id: str) -> ToolSelectionSnapshot:
        """同一把共享锁下取得精确的执行/可见性选择事实。"""
        with self._file_lock(shared=True):
            settings = self._agent_settings_from_payload(self._read_unlocked(), agent_id)
            return ToolSelectionSnapshot(
                agent_id,
                settings["execution_overrides"],
                settings["model_visibility_overrides"],
            )

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

    @staticmethod
    def _agent_settings_from_payload(
        payload: dict[str, object],
        agent_id: str,
    ) -> dict[str, object]:
        raw_settings = payload.get(agent_id, {})
        if not isinstance(raw_settings, dict):
            raise TypeError("工具选择配置的 Agent 项必须是对象: artifact=tool_selection")
        execution = raw_settings.get("execution_overrides", {})
        visibility = raw_settings.get("model_visibility_overrides", {})
        if not isinstance(execution, dict) or not all(
            isinstance(name, str) and isinstance(value, bool)
            for name, value in execution.items()
        ):
            raise TypeError(
                "工具选择配置 execution_overrides 格式错误: artifact=tool_selection"
            )
        if not isinstance(visibility, dict) or not all(
            isinstance(name, str) and isinstance(value, bool)
            for name, value in visibility.items()
        ):
            raise TypeError(
                "工具选择配置 model_visibility_overrides 格式错误: "
                "artifact=tool_selection"
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
            raise TypeError("工具选择配置必须是对象: artifact=tool_selection")
        normalized: dict[str, object] = {}
        for agent_id, settings in payload.items():
            if not isinstance(agent_id, str):
                raise TypeError("工具选择配置 Agent 项格式错误: artifact=tool_selection")
            if not isinstance(settings, dict):
                raise TypeError("工具选择配置 Agent 项格式错误: artifact=tool_selection")
            normalized[agent_id] = settings

        for agent_id in normalized:
            self._agent_settings_from_payload(normalized, agent_id)
        return normalized

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
        # 锁与读改写仍共用一个 owner；只有对外诊断不携带原生位置或损坏输入。
        operation = "snapshot" if shared else "apply_changes"
        detail = f"artifact=tool_selection, operation={operation}"
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock_path.open("a+b") as lock_file:
                acquired = False
                try:
                    _acquire_file_lock(lock_file, shared=shared)
                    acquired = True
                    yield
                finally:
                    if acquired:
                        _release_file_lock(lock_file)
        except OSError as error:
            detail += f", error={type(error).__name__}, errno={error.errno}"
            if isinstance(error, TimeoutError):
                detail = (
                    f"工具选择配置锁获取超时: {detail}, "
                    f"timeout_seconds={_TOOL_SELECTION_LOCK_TIMEOUT_SECONDS:g}"
                )
            raise type(error)(error.errno, detail) from error
        except portalocker.LockException as error:
            if error.args and isinstance(error.args[0], OSError):
                detail += f", errno={error.args[0].errno}"
            raise type(error)(f"{detail}, error={type(error).__name__}") from error
        except json.JSONDecodeError as error:
            raise ValueError(
                f"{detail}, error=JSONDecodeError, line={error.lineno}, column={error.colno}"
            ) from error
        except UnicodeDecodeError as error:
            raise ValueError(
                f"{detail}, error=UnicodeDecodeError, start={error.start}, end={error.end}"
            ) from error


def _acquire_file_lock(
    lock_file: BinaryIO,
    *,
    shared: bool,
) -> None:
    """有界获取配置锁，避免 Agent 初始化无限等待一个失联写者。"""
    deadline = time.monotonic() + _TOOL_SELECTION_LOCK_TIMEOUT_SECONDS
    lock_file.seek(0, 2)
    if lock_file.tell() == 0:
        lock_file.write(b" ")
        lock_file.flush()
    lock_file.seek(0)
    mode = portalocker.LOCK_SH if shared else portalocker.LOCK_EX
    while True:
        try:
            portalocker.lock(lock_file, mode | portalocker.LOCK_NB)
        except portalocker.AlreadyLocked as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "工具选择配置锁获取超时: "
                    "artifact=tool_selection "
                    f"timeout_seconds={_TOOL_SELECTION_LOCK_TIMEOUT_SECONDS:g}"
                ) from error
            time.sleep(min(_TOOL_SELECTION_LOCK_POLL_INTERVAL_SECONDS, remaining))
        else:
            return


def _release_file_lock(lock_file: BinaryIO) -> None:
    portalocker.unlock(lock_file)
