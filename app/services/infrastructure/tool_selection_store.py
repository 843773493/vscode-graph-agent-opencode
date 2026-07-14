from __future__ import annotations

import json
from pathlib import Path


class ToolSelectionStore:
    """持久化每个 Agent 被用户关闭的工具；新增工具因此默认保持开启。"""

    def __init__(self, *, boxteam_root: Path) -> None:
        self._path = boxteam_root / "settings" / "tool_selection.json"

    def disabled_tools(self, agent_id: str) -> set[str]:
        payload = self._read()
        raw_names = payload.get(agent_id, [])
        if not isinstance(raw_names, list) or not all(
            isinstance(name, str) for name in raw_names
        ):
            raise TypeError(f"工具选择配置格式错误: agent={agent_id}, path={self._path}")
        return set(raw_names)

    def apply_changes(
        self,
        *,
        agent_id: str,
        changes: dict[str, bool],
    ) -> set[str]:
        payload = self._read()
        raw_names = payload.get(agent_id, [])
        if not isinstance(raw_names, list) or not all(
            isinstance(name, str) for name in raw_names
        ):
            raise TypeError(f"工具选择配置格式错误: agent={agent_id}, path={self._path}")
        disabled = set(raw_names)
        for tool_id, enabled in changes.items():
            if enabled:
                disabled.discard(tool_id)
            else:
                disabled.add(tool_id)
        payload[agent_id] = sorted(disabled)
        self._write(payload)
        return disabled

    def _read(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(f"工具选择配置必须是对象: {self._path}")
        return payload

    def _write(self, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._path)
