from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class ToolSelectionStore:
    """持久化每个 Agent 的执行禁用项和模型可见性覆盖。"""

    def __init__(self, *, boxteam_root: Path) -> None:
        self._path = boxteam_root / "settings" / "tool_selection.json"
        self._lock_path = boxteam_root / "settings" / "tool_selection.lock"

    def disabled_tools(self, agent_id: str) -> set[str]:
        return set(self._agent_settings(agent_id)["execution_disabled"])

    def model_hidden_tools(
        self,
        agent_id: str,
        *,
        default_hidden_tool_names: set[str],
    ) -> set[str]:
        settings = self._agent_settings(agent_id)
        overrides = settings["model_visibility"]
        hidden = {
            name
            for name in default_hidden_tool_names
            if overrides.get(name, False) is not True
        }
        hidden.update(
            name for name, visible in overrides.items() if visible is False
        )
        return hidden

    def model_visible(
        self,
        agent_id: str,
        *,
        tool_id: str,
        kind: str,
    ) -> bool:
        overrides = self._agent_settings(agent_id)["model_visibility"]
        default = kind not in {"extension", "debugging"}
        return bool(overrides.get(tool_id, default))

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
            disabled = set(settings["execution_disabled"])
            visibility = dict(settings["model_visibility"])
            for tool_id, (execution_enabled, model_visible) in changes.items():
                if not execution_enabled and model_visible:
                    raise ValueError(
                        f"工具 {tool_id!r} 未启用执行能力时不能对模型可见"
                    )
                if execution_enabled:
                    disabled.discard(tool_id)
                else:
                    disabled.add(tool_id)
                visibility[tool_id] = model_visible
            payload[agent_id] = {
                "execution_disabled": sorted(disabled),
                "model_visibility": dict(sorted(visibility.items())),
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
        disabled = raw_settings.get("execution_disabled", [])
        visibility = raw_settings.get("model_visibility", {})
        if not isinstance(disabled, list) or not all(
            isinstance(name, str) for name in disabled
        ):
            raise TypeError(f"工具选择配置 execution_disabled 格式错误: agent={agent_id}")
        if not isinstance(visibility, dict) or not all(
            isinstance(name, str) and isinstance(value, bool)
            for name, value in visibility.items()
        ):
            raise TypeError(f"工具选择配置 model_visibility 格式错误: agent={agent_id}")
        return {
            "execution_disabled": list(dict.fromkeys(disabled)),
            "model_visibility": dict(visibility),
        }

    def _read(self) -> dict[str, object]:
        with self._file_lock(shared=True):
            return self._read_unlocked()

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
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(
                    lock_file.fileno(),
                    msvcrt.LK_LOCK,
                    1,
                )
            else:
                fcntl.flock(
                    lock_file.fileno(),
                    fcntl.LOCK_SH if shared else fcntl.LOCK_EX,
                )
            try:
                yield
            finally:
                if os.name == "nt":
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
