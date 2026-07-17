from __future__ import annotations

import os
from typing import TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from app.abstractions.session_context import WorkspaceSessionContextAccessError
from app.schemas.public_v2.session_context import (
    SessionContextGrepRequest,
    SessionContextGrepResultDTO,
    SessionContextReadResultDTO,
    SessionRecentTextMessagesDTO,
)


ResponseDTO = TypeVar("ResponseDTO", bound=BaseModel)
_MODEL_RECOVERABLE_HTTP_STATUSES = frozenset(
    {400, 401, 403, 404, 409, 422, 502, 503, 504}
)


class GatewaySessionContextClient:
    """通过 Gateway 查询指定工作区后端的会话上下文。"""

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        local_token: str = "local-dev-token",
        timeout_seconds: float = 30,
    ) -> None:
        configured_url = gateway_url or os.environ.get("BOXTEAM_GATEWAY_URL")
        self._gateway_url = configured_url.rstrip("/") if configured_url else None
        self._local_token = local_token
        self._timeout_seconds = timeout_seconds

    async def recent_text_in_workspace(
        self,
        workspace_id: str,
        session_id: str,
        *,
        rounds: int = 5,
    ) -> SessionRecentTextMessagesDTO:
        return await self._request(
            "GET",
            f"/api/v1/sessions/{quote(session_id, safe='')}/context/recent-text",
            workspace_id=workspace_id,
            response_type=SessionRecentTextMessagesDTO,
            params={"rounds": rounds},
        )

    async def grep_in_workspace(
        self,
        workspace_id: str,
        session_id: str,
        *,
        pattern: str,
        case_sensitive: bool = False,
        max_matches: int = 20,
        expected_snapshot_id: str | None = None,
    ) -> SessionContextGrepResultDTO:
        payload = SessionContextGrepRequest(
            pattern=pattern,
            case_sensitive=case_sensitive,
            max_matches=max_matches,
            expected_snapshot_id=expected_snapshot_id,
        )
        return await self._request(
            "POST",
            f"/api/v1/sessions/{quote(session_id, safe='')}/context/grep",
            workspace_id=workspace_id,
            response_type=SessionContextGrepResultDTO,
            json_body=payload.model_dump(mode="json"),
        )

    async def read_lines_in_workspace(
        self,
        workspace_id: str,
        session_id: str,
        *,
        line_start: int = 1,
        line_count: int = 20,
        max_chars_per_line: int = 4000,
        expected_snapshot_id: str | None = None,
    ) -> SessionContextReadResultDTO:
        params: dict[str, str | int] = {
            "line_start": line_start,
            "line_count": line_count,
            "max_chars_per_line": max_chars_per_line,
        }
        if expected_snapshot_id is not None:
            params["expected_snapshot_id"] = expected_snapshot_id
        return await self._request(
            "GET",
            f"/api/v1/sessions/{quote(session_id, safe='')}/context/lines",
            workspace_id=workspace_id,
            response_type=SessionContextReadResultDTO,
            params=params,
        )

    def _require_gateway_url(self) -> str:
        if self._gateway_url is None:
            raise WorkspaceSessionContextAccessError(
                "跨工作区会话查询需要配置 BOXTEAM_GATEWAY_URL；"
                "通过 scripts/dev.mjs 启动时会自动注入该地址。"
                "请提醒用户检查 Gateway 启动方式或改为读取当前工作区"
            )
        return self._gateway_url

    async def _request(
        self,
        method: str,
        path: str,
        *,
        workspace_id: str,
        response_type: type[ResponseDTO],
        params: dict[str, str | int] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> ResponseDTO:
        target_workspace_id = workspace_id.strip()
        if not target_workspace_id:
            raise WorkspaceSessionContextAccessError(
                "workspace_id 不能为空；请传入已注册的 Gateway 工作区 ID，"
                "读取当前工作区时应省略 workspace_id"
            )
        gateway_url = self._require_gateway_url()
        async with httpx.AsyncClient(
            base_url=gateway_url,
            timeout=self._timeout_seconds,
            headers={
                "X-Local-Token": self._local_token,
                "X-BoxTeam-Workspace-Id": target_workspace_id,
            },
        ) as client:
            try:
                response = await client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                )
            except httpx.RequestError as error:
                raise WorkspaceSessionContextAccessError(
                    "无法连接 Workspace Gateway: "
                    f"gateway_url={gateway_url}, workspace_id={target_workspace_id}, "
                    f"method={method}, path={path}, "
                    f"error_type={type(error).__name__}, error={error}。"
                    "请提醒用户检查 Gateway 是否运行及网络连接"
                ) from error
        if not response.is_success:
            error_message = (
                "跨工作区会话查询失败: "
                f"workspace_id={target_workspace_id}, method={method}, path={path}, "
                f"status={response.status_code}, detail={response.text[:2000]}"
            )
            if response.status_code in _MODEL_RECOVERABLE_HTTP_STATUSES:
                raise WorkspaceSessionContextAccessError(
                    f"{error_message}。请检查并修正 workspace_id 或 session_id 后重试；"
                    "无法确认正确标识时请提醒用户"
                )
            raise RuntimeError(error_message)
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(
                "Gateway 会话查询响应不是 JSON object: "
                f"workspace_id={target_workspace_id}, path={path}"
            )
        data = payload.get("data")
        if data is None:
            raise RuntimeError(
                "Gateway 会话查询响应缺少 data: "
                f"workspace_id={target_workspace_id}, path={path}, payload={payload}"
            )
        return response_type.model_validate(data)
