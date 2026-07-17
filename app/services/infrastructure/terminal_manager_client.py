from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.core.path_utils import get_boxteam_root, get_workspace_root


DEFAULT_TERMINAL_BACKEND_URL = "http://127.0.0.1:8012"


class TerminalManagerClient:
    def __init__(
        self,
        *,
        backend_url: str | None = None,
        state_file: Path | None = None,
    ) -> None:
        self._backend_url = (
            backend_url
            or os.environ.get("BOXTEAM_TERMINAL_BACKEND_URL")
            or DEFAULT_TERMINAL_BACKEND_URL
        ).rstrip("/")
        self._state_file = state_file or get_boxteam_root() / "terminal-manager" / "terminals.json"

    @property
    def backend_url(self) -> str:
        return self._backend_url

    def list_terminals_from_state(self, session_id: str) -> list[dict[str, Any]]:
        if not self._state_file.exists():
            return []
        raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        terminals = raw.get("terminals")
        if not isinstance(terminals, list):
            raise RuntimeError(f"终端状态文件格式错误: {self._state_file}")
        result = []
        for terminal in terminals:
            if not isinstance(terminal, dict):
                raise RuntimeError(f"终端状态文件包含非对象记录: {self._state_file}")
            if terminal.get("session_id") == session_id:
                normalized = dict(terminal)
                normalized.pop("attach_url", None)
                result.append(normalized)
        return sorted(
            result,
            key=lambda terminal: str(terminal.get("updated_at") or terminal.get("created_at") or ""),
            reverse=True,
        )

    async def create_terminal(
        self,
        *,
        session_id: str,
        title: str,
        cwd: str | None = None,
        cols: int = 100,
        rows: int = 30,
    ) -> dict[str, Any]:
        payload = {
            "session_id": session_id,
            "title": title,
            "cwd": cwd or str(get_workspace_root()),
            "cols": cols,
            "rows": rows,
        }
        response = await self._json_request("POST", "/api/terminals", payload)
        return self._require_data(response)

    async def get_terminal(self, terminal_id: str) -> dict[str, Any]:
        response = await self._json_request("GET", f"/api/terminals/{terminal_id}")
        return self._require_data(response)

    async def write_terminal(
        self,
        *,
        terminal_id: str,
        data: str,
        source: str = "agent",
        command: str | None = None,
    ) -> dict[str, Any]:
        response = await self._json_request(
            "POST",
            f"/api/terminals/{terminal_id}/write",
            {
                "data": data,
                "source": source,
                "command": command,
            },
        )
        return self._require_data(response)

    async def kill_terminal(self, terminal_id: str) -> dict[str, Any]:
        response = await self._json_request("POST", f"/api/terminals/{terminal_id}/kill")
        return self._require_data(response)

    async def delete_terminal(self, terminal_id: str) -> dict[str, Any]:
        response = await self._json_request("DELETE", f"/api/terminals/{terminal_id}")
        return self._require_data(response)

    def _require_data(self, response: dict[str, Any]) -> dict[str, Any]:
        data = response.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"终端管理器返回格式错误: {response}")
        normalized = dict(data)
        normalized.pop("attach_url", None)
        return normalized

    async def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._json_request_sync, method, path, payload)

    def _json_request_sync(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self._backend_url}{path}",
            data=body,
            method=method,
            headers={"content-type": "application/json"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"终端管理器请求失败: method={method}, path={path}, status={exc.code}, detail={detail}"
            ) from exc
