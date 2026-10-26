"""通过既有本机管理通道读取、应用并核实真实进程的日志状态。"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from app.core.logging_config import RuntimeLoggingConflictError
from app.schemas.runtime_logging import (
    RuntimeLoggingApply,
    RuntimeLoggingHttpService,
    RuntimeLoggingStatus,
)

_CONTROL_PATHS: dict[RuntimeLoggingHttpService, str] = {
    "gateway": "/api/gateway/internal/runtime-logging",
    "backend": "/api/v1/internal/runtime-logging",
    "terminal": "/internal/runtime-logging",
    "browser": "/internal/runtime-logging",
}


class RuntimeLoggingProcessControl:
    def __init__(self, *, client: httpx.AsyncClient, token: str) -> None:
        if not token:
            raise ValueError("日志进程控制缺少本机凭据")
        self._client = client
        self._token = token

    @staticmethod
    def _url(base_url: str, service: RuntimeLoggingHttpService) -> str:
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.port is None or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("日志进程控制只接受带端口的本机 HTTP 服务根地址")
        return f"{base_url.rstrip('/')}{_CONTROL_PATHS[service]}"

    @staticmethod
    def _status(response: httpx.Response, service: RuntimeLoggingHttpService, pid: int) -> RuntimeLoggingStatus:
        response.raise_for_status()
        envelope = response.json()
        if not isinstance(envelope, dict) or "data" not in envelope:
            raise ValueError("日志进程控制响应缺少状态")
        state = RuntimeLoggingStatus.model_validate(envelope["data"])
        if state.service != service or state.pid != pid:
            raise RuntimeLoggingConflictError("日志进程身份与受管实例不匹配")
        return state

    async def status(
        self, *, base_url: str, service: RuntimeLoggingHttpService, pid: int,
    ) -> RuntimeLoggingStatus:
        response = await self._client.get(
            self._url(base_url, service), headers={"X-Local-Token": self._token},
            follow_redirects=False,
        )
        return self._status(response, service, pid)

    async def apply(
        self, *, base_url: str, service: RuntimeLoggingHttpService, pid: int, request: RuntimeLoggingApply,
    ) -> RuntimeLoggingStatus:
        response = await self._client.post(
            self._url(base_url, service), headers={"X-Local-Token": self._token},
            json=request.model_dump(mode="json"), follow_redirects=False,
        )
        state = self._status(response, service, pid)
        if (state.runtime_instance_id != request.runtime_instance_id
            or state.applied_revision != request.revision
            or state.level != request.level or state.pretty != request.pretty):
            raise RuntimeLoggingConflictError("日志应用响应未确认请求的运行实例、revision 与实际设置")
        return state
