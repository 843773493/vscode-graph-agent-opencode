from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from app.core.path_utils import get_session_path_resolver


class HistoricalTerminalRecordReader:
    def __init__(
        self,
        *,
        sessions_dir: Path,
    ) -> None:
        self._sessions_dir = sessions_dir
        self._path_resolver = get_session_path_resolver(sessions_dir)

    def read_records(
        self,
        *,
        session_id: str,
        active_terminals: Sequence[Mapping[str, object]],
        agent_state_records: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        existing_ids = {
            str(terminal.get("terminal_id"))
            for terminal in active_terminals
            if terminal.get("terminal_id")
        }
        seen_ids = set(existing_ids)
        terminals: list[dict[str, object]] = []
        for record in agent_state_records:
            terminal = self._from_agent_state_record(
                session_id=session_id,
                record=record,
            )
            if terminal is None:
                continue
            terminal_id = str(terminal["terminal_id"])
            if terminal_id in seen_ids:
                continue
            seen_ids.add(terminal_id)
            terminals.append(terminal)
        return terminals

    def _event_time(
        self,
        *,
        session_id: str,
        terminal_id: str,
    ) -> str | None:
        trace_file = (
            self._path_resolver.resolve_session_node(session_id)
            / "logs"
            / "traces"
            / "messages.jsonl"
        )
        if not trace_file.exists():
            return None
        with trace_file.open("r", encoding="utf-8") as file:
            for line in file:
                if terminal_id not in line:
                    continue
                record = json.loads(line)
                if record.get("type") != "tool_call_end":
                    continue
                timestamp = record.get("timestamp")
                if isinstance(timestamp, str) and timestamp:
                    return timestamp
        return None

    def _from_agent_state_record(
        self,
        *,
        session_id: str,
        record: Mapping[str, object],
    ) -> dict[str, object] | None:
        if record.get("type") != "tool" or record.get("name") != "exec_command":
            return None

        response_metadata = record.get("response_metadata")
        if isinstance(response_metadata, Mapping):
            fork_source_session_id = response_metadata.get(
                "context_fork_source_session_id"
            )
            if isinstance(fork_source_session_id, str) and fork_source_session_id:
                return None

        content = record.get("content")
        if isinstance(content, str):
            stripped_content = content.strip()
            if not stripped_content or not stripped_content.startswith("{"):
                return None
            payload = self._load_tool_payload(stripped_content)
        elif isinstance(content, Mapping):
            payload = {str(key): value for key, value in content.items()}
        else:
            return None

        if not isinstance(payload, Mapping):
            raise TypeError(
                f"exec_command 工具结果应为 object，实际类型: {type(payload).__name__}"
            )

        terminal: dict[str, object] = {}
        terminal_id = payload.get("terminal_id") or payload.get("session_id")
        # TODO: 兼容迁移前 chunk_id 同时充当 terminal_id 的历史工具结果。
        if not terminal_id:
            terminal_id = payload.get("chunk_id")
        if not isinstance(terminal_id, str) or not terminal_id:
            return None

        created_at = terminal.get("created_at")
        updated_at = terminal.get("updated_at") or created_at
        fallback_time = (
            self._event_time(
                session_id=session_id,
                terminal_id=terminal_id,
            )
            or "1970-01-01T00:00:00+00:00"
        )
        terminal.update(
            {
                "terminal_id": terminal_id,
                "session_id": session_id,
                "status": "deleted",
                "created_at": created_at
                if isinstance(created_at, str) and created_at
                else fallback_time,
                "updated_at": updated_at
                if isinstance(updated_at, str) and updated_at
                else fallback_time,
                "ended_at": terminal.get("ended_at")
                or updated_at
                or created_at
                or fallback_time,
                "historical_only": True,
                "historical_status": (
                    "running" if payload.get("session_id") else "completed"
                ),
                "last_command": None,
                "last_command_status": (
                    "deleted" if payload.get("session_id") else "completed"
                ),
                "last_command_exit_code": payload.get("exit_code"),
            }
        )
        terminal["last_command_completed_at"] = terminal.get("ended_at")
        return terminal

    @staticmethod
    def _load_tool_payload(content: str) -> object:
        try:
            return json.loads(content)
        except json.JSONDecodeError as error:
            if "Invalid control character" not in error.msg:
                raise
            # TODO: 兼容旧版 ToolOutputStore 将截断提示以裸换行插入 JSON 字符串的记录；
            # 新记录由结构化截断逻辑保证写入合法 JSON，待旧记录迁移后删除此分支。
            return json.loads(content, strict=False)
