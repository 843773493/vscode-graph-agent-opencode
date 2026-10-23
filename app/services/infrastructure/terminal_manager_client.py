from __future__ import annotations

import asyncio
import json
import logging
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.abstractions.terminal_resources import TerminalRequestError
from app.core.workspace_identity import validate_workspace_id
from app.protocol.codecs.terminal import (
    terminal_session_to_json,
    terminal_session_to_proto,
)
from app.services.infrastructure.config_service import ConfigService

DEFAULT_TERMINAL_BACKEND_URL = "http://127.0.0.1:8012"
logger = logging.getLogger(__name__)


class TerminalManagerClient:
    def __init__(
        self,
        *,
        backend_url: str | None = None,
        workspace_id: str,
        config_service: ConfigService | None = None,
    ) -> None:
        configured_backend_url = backend_url
        self._config_service = (
            config_service if configured_backend_url is None else None
        )
        self._backend_url = (
            configured_backend_url
            or DEFAULT_TERMINAL_BACKEND_URL
        ).rstrip("/")
        self._workspace_id = validate_workspace_id(workspace_id)

    @property
    def backend_url(self) -> str:
        return self._resolve_backend_url()

    def _resolve_backend_url(self) -> str:
        if self._config_service is not None:
            return self._config_service.get_terminal_backend_url().rstrip("/")
        return self._backend_url

    async def create_terminal(
        self,
        *,
        terminal_id: str,
        owner_revision: int,
        title: str,
        cwd: str,
        agent_id: str | None = None,
        cols: int = 100,
        rows: int = 30,
    ) -> dict[str, object]:
        payload = {
            "terminal_id": terminal_id,
            "owner_revision": owner_revision,
            "workspace_id": self._workspace_id,
            "agent_id": agent_id,
            "title": title,
            "cwd": cwd,
            "cols": cols,
            "rows": rows,
        }
        response = await self._json_request("POST", "/api/terminals", payload)
        return self._require_normalized_terminal_record(response)

    async def get_resource(self, terminal_id: str) -> dict[str, object]:
        response = await self._json_request("GET", f"/api/terminals/{terminal_id}")
        return self._require_normalized_terminal_record(response)

    async def list_resources(self) -> list[dict[str, object]]:
        path = "/api/terminals"
        response = await self._json_request("GET", path)
        data = response.get("data")
        if not isinstance(data, list):
            raise self._protocol_error("list.data", response)
        result: list[dict[str, object]] = []
        for item in data:
            if not isinstance(item, dict):
                raise self._protocol_error("list.item", response)
            result.append(self._normalize_terminal_record(item))
        return result

    async def read_terminal(self, terminal_id: str) -> dict[str, object]:
        response = await self._json_request(
            "POST", f"/api/terminals/{terminal_id}/read"
        )
        return self._require_terminal_envelope(response)

    async def mark_terminal_backgrounded(self, terminal_id: str) -> dict[str, object]:
        response = await self._json_request(
            "POST", f"/api/terminals/{terminal_id}/background"
        )
        return self._require_normalized_terminal_record(response)

    async def claim_terminal_steering(
        self, terminal_id: str, *, completion_event_id: str, command_start_sequence: int,
    ) -> dict[str, object]:
        response = await self._json_request(
            "POST", f"/api/terminals/{terminal_id}/claim-steering",
            {"completion_event_id": completion_event_id, "command_start_sequence": command_start_sequence},
        )
        return self._require_terminal_envelope(response)

    async def finish_terminal_steering(
        self,
        terminal_id: str,
        *,
        completion_event_id: str,
        command_start_sequence: int,
        dispatched: bool,
    ) -> dict[str, object]:
        response = await self._json_request(
            "POST",
            f"/api/terminals/{terminal_id}/finish-steering",
            {"completion_event_id": completion_event_id, "command_start_sequence": command_start_sequence,
             "dispatched": dispatched},
        )
        return self._require_terminal_envelope(response)

    async def write_terminal(
        self,
        *,
        terminal_id: str,
        data: str,
        source: str = "agent",
        command: str | None = None,
    ) -> dict[str, object]:
        response = await self._json_request(
            "POST",
            f"/api/terminals/{terminal_id}/write",
            {
                "data": data,
                "source": source,
                "command": command,
            },
        )
        return self._require_normalized_terminal_record(response)

    async def stop_resource(
        self,
        terminal_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, object]:
        response = await self._json_request(
            "POST",
            f"/api/terminals/{terminal_id}/kill",
            None if reason is None else {"reason": reason},
        )
        return self._require_terminal_envelope(response)

    async def delete_resource(self, terminal_id: str) -> dict[str, object]:
        response = await self._json_request("DELETE", f"/api/terminals/{terminal_id}")
        return self._require_terminal_envelope(response)

    async def sync_owner_revision(self, terminal_id: str, owner_revision: int) -> None:
        await self._json_request(
            "POST", f"/api/terminals/{terminal_id}/owner-revision",
            {"owner_revision": owner_revision},
        )

    async def issue_transport_grant(
        self, terminal_id: str, *, owner_revision: int, association: str | None,
    ) -> dict[str, object]:
        result = await self._json_request(
            "POST", f"/api/terminals/{terminal_id}/transport-grant",
            {"owner_revision": owner_revision, "association": association},
        )
        data = result.get("data")
        if not isinstance(data, dict):
            raise self._protocol_error("grant.data", result)
        return data

    def _require_normalized_terminal_record(
        self, response: dict[str, object]
    ) -> dict[str, object]:
        data = response.get("data")
        if not isinstance(data, dict):
            raise self._protocol_error("record.data", response)
        return self._normalize_terminal_record(data)

    def _require_terminal_envelope(self, response: dict[str, object]) -> dict[str, object]:
        data = response.get("data")
        if not isinstance(data, dict):
            raise self._protocol_error("envelope.data", response)
        terminal = data.get("terminal")
        if not isinstance(terminal, dict):
            raise self._protocol_error("envelope.terminal", response)
        terminal_envelope = dict(data)
        terminal_envelope["terminal"] = self._normalize_terminal_record(terminal)
        return terminal_envelope

    def _normalize_terminal_record(self, data: dict[str, object]) -> dict[str, object]:
        try:
            normalized = terminal_session_to_json(terminal_session_to_proto(data))
        except (TypeError, ValueError, OverflowError) as exc:
            raise self._protocol_error("terminal", data, cause=exc) from exc
        normalized.pop("attach_url", None)
        return normalized

    @staticmethod
    def _protocol_error(
        stage: str, response: object, *, cause: Exception | None = None,
    ) -> TypeError | ValueError | OverflowError:
        logger.error("终端传输响应协议错误: stage=%s response=%r", stage, response, exc_info=cause)
        message = f"终端管理器响应协议错误: stage={stage}；请查看工作区终端服务日志"
        if isinstance(cause, ValueError):
            return ValueError(message)
        if isinstance(cause, OverflowError):
            return OverflowError(message)
        return TypeError(message)

    async def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return await asyncio.to_thread(self._json_request_sync, method, path, payload)

    def _json_request_sync(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
    ) -> dict[str, object]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self._resolve_backend_url()}{path}",
            data=body,
            method=method,
            headers={
                "content-type": "application/json",
                "X-BoxTeam-Workspace-Id": self._workspace_id,
                "X-BoxTeam-Resource-Owner": "workspace-terminal",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                response_body = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.exception(
                "终端传输 HTTP 请求失败: method=%s path=%s status=%s response=%s",
                method, path, exc.code, detail,
            )
            raise TerminalRequestError(
                method=method, path=path, status=exc.code, response_body=detail,
            ) from exc
        try:
            result = json.loads(response_body.decode("utf-8"))
        except ValueError as exc:
            raise self._protocol_error("json", response_body, cause=exc) from exc
        if not isinstance(result, dict):
            raise self._protocol_error("response", result)
        return result
