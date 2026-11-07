"""Backend 通过目标 Gateway 调用同一原生管理组件；不持有管理进程。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.core.backend_identity import validate_backend_id
from app.core.shielded_cleanup import run_cleanup_shielded
from app.core.trace_middleware import get_current_request_id
from app.schemas.gateway.extension_host import NativeFileURI
from app.schemas.gateway.extension_installation import (
    ExtensionUninstallResult,
    ProfileExtensionInventory,
    VsixInstallResult,
)
from app.schemas.internal_v2.common import APIResponse


class InstalledProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    installed: VsixInstallResult
    inventory: ProfileExtensionInventory


class UninstalledProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    uninstalled: ExtensionUninstallResult
    inventory: ProfileExtensionInventory


class ExtensionManagementError(RuntimeError):
    """原安装／扫描服务已经明确回报失败，传输本身仍可继续查询。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class NativeReferenceService(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    url: str
    backend_id: str
    runtime_instance_id: str


class NativeHostReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    generation: str = Field(min_length=1)
    capability: str = Field(min_length=1)


class NativeManagementEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    app_root_uri: NativeFileURI
    global_storage_home_uri: NativeFileURI
    workspace_storage_home_uri: NativeFileURI
    untitled_workspaces_home_uri: NativeFileURI
    profile_id: str
    settings_native_uri: NativeFileURI
    reference_service: NativeReferenceService


class NativeFileStat(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    type: int
    ctime: float
    mtime: float
    size: int
    permissions: int | None


class NativeFileRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    content_base64: str


class NativeExtensionPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    extension_id: str = Field(min_length=1)
    extension_key: str = Field(min_length=1)
    native_uri: NativeFileURI
    extensions_root_uri: NativeFileURI


class HostExtensionPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    inventory: ProfileExtensionInventory
    publications: list[NativeExtensionPublication]
    environment: NativeManagementEnvironment


Result = TypeVar("Result", bound=BaseModel)


class ExtensionManagementClient:
    """只连接已启动的原生 Gateway；关闭本客户端不会停止共享管理服务。"""

    def __init__(self, *, backend_id: str, gateway_url: str, token: str, operation_timeout: float) -> None:
        parsed = urlsplit(gateway_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
            raise ValueError("扩展管理只能连接明确的本机原生 Gateway HTTP 入口")
        if not token:
            raise ValueError("扩展管理缺少原运行时控制凭据")
        self.backend_id = validate_backend_id(backend_id)
        self._url = gateway_url.rstrip("/") + "/api/gateway/internal/extension-management/" + self.backend_id
        self._http = httpx.AsyncClient(trust_env=False, timeout=operation_timeout, headers={"X-Local-Token": token})
        self._lock = asyncio.Lock()
        self._closing = False
        self._closed = False

    @classmethod
    def from_environment(cls, backend_id: str) -> ExtensionManagementClient | None:
        gateway_url = os.environ.get("BOXTEAM_GATEWAY_URL")
        token = os.environ.get("BOXTEAM_RUNTIME_CONTROL_TOKEN")
        if not gateway_url or not token:
            return None
        return cls(backend_id=backend_id, gateway_url=gateway_url, token=token, operation_timeout=120)

    def close_admission(self) -> None:
        self._closing = True

    @property
    def drained(self) -> bool:
        return self._closed

    async def _request(self, command: Mapping[str, JsonValue], *, response_type: type[Result]) -> Result:
        async with self._lock:
            if self._closing:
                raise RuntimeError("Backend 扩展管理客户端已关闭")
            async def exchange() -> Result:
                request_id = get_current_request_id()
                headers = {} if request_id is None else {"X-Request-ID": request_id}
                response = await self._http.post(self._url, json=command, headers=headers)
                response.raise_for_status()
                if request_id is not None and response.headers.get("X-Request-ID") != request_id:
                    raise ValueError("扩展管理响应不属于原请求")
                envelope = APIResponse[dict[str, JsonValue]].model_validate_json(response.content)
                if envelope.code != 0 or envelope.data is None or request_id is not None and envelope.request_id != request_id:
                    raise ValueError("扩展管理响应不属于原请求或缺少控制对象")
                reply = envelope.data
                if set(reply) == {"error", "code"} and isinstance(reply["error"], str) and isinstance(reply["code"], str):
                    raise ExtensionManagementError(reply["error"], reply["code"])
                if set(reply) != {"result"}:
                    raise ValueError("扩展管理控制回执不完整")
                return response_type.model_validate(reply["result"])

            pending = asyncio.create_task(exchange())
            try:
                return await asyncio.shield(pending)
            except asyncio.CancelledError:
                async def settle() -> None:
                    await pending

                # Backend 先核本次原回执再结清客户端；不会取消共享管理进程或重放写入。
                await run_cleanup_shielded(settle)
                raise

    async def inventory(self) -> ProfileExtensionInventory:
        return await self._request({"method": "inventory"}, response_type=ProfileExtensionInventory)

    async def environment(self) -> NativeManagementEnvironment:
        return await self._request({"method": "environment"}, response_type=NativeManagementEnvironment)

    def reference_environment(self, service: NativeReferenceService, reference: NativeHostReference, *, gateway_id: str, profile_id: str) -> dict[str, str]:
        return {"BOXTEAM_EXTENSION_HOST_REFERENCE": json.dumps({
            "service": service.model_dump(), "reference": reference.model_dump(),
            "gateway_id": gateway_id, "profile_id": profile_id,
            "timeout_seconds": self._http.timeout.read,
        })}

    async def host_reference(self, service: NativeReferenceService, reference: NativeHostReference,
                             *, operation: Literal["open-host", "close-host"]) -> NativeHostReference:
        return await self._reference_request(service, reference, operation=operation, response_type=NativeHostReference)

    async def host_publication(self, service: NativeReferenceService, reference: NativeHostReference,
                               *, profile_id: str, extension_keys: list[str]) -> HostExtensionPublication:
        return await self._reference_request(service, reference, operation="host-publication",
            response_type=HostExtensionPublication, profile_id=profile_id, extension_keys=list(extension_keys))

    async def _reference_request(self, service: NativeReferenceService, reference: NativeHostReference,
                                 *, operation: str, response_type: type[Result], **selection: JsonValue) -> Result:
        parsed = urlsplit(service.url)
        if (service.backend_id != self.backend_id or parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.port is None or parsed.path != "/references" or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("运行引用服务不属于目标本机安装域")

        async def exchange() -> Result:
            async with self._lock:
                # 原宿主关闭仍须结清引用；Backend 停止新准入不撤销已有引用的释放资格。
                if self._closing and operation != "close-host":
                    raise RuntimeError("Backend 扩展管理客户端已停止运行准入")
                response = await self._http.post(service.url, json={
                    "method": operation, "backend_id": service.backend_id,
                    "runtime_instance_id": service.runtime_instance_id, **reference.model_dump(), **selection,
                })
                response.raise_for_status()
                reply = response.json()
                if set(reply) == {"error", "code"}:
                    raise ExtensionManagementError(reply["error"], reply["code"])
                if set(reply) != {"result"}:
                    raise ValueError("运行引用回执不完整")
                return response_type.model_validate(reply["result"])

        pending = asyncio.create_task(exchange())
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            async def settle() -> None:
                await pending

            await run_cleanup_shielded(settle)
            raise

    async def extension_file_stat(self, *, profile_id: str, extension_key: str, extension_id: str,
                                  relative_segments: list[str], current_profile_member: bool) -> NativeFileStat:
        return await self._request({"method": "extension-file-stat", "profile_id": profile_id,
                                   "extension_key": extension_key, "extension_id": extension_id, "relative_segments": list(relative_segments),
                                   "current_profile_member": current_profile_member},
                                   response_type=NativeFileStat)

    async def extension_file_read(self, *, profile_id: str, extension_key: str, extension_id: str,
                                  relative_segments: list[str], current_profile_member: bool) -> NativeFileRead:
        return await self._request({"method": "extension-file-read", "profile_id": profile_id,
                                   "extension_key": extension_key, "extension_id": extension_id, "relative_segments": list(relative_segments),
                                   "current_profile_member": current_profile_member},
                                   response_type=NativeFileRead)

    async def install(self, vsix_path: Path) -> InstalledProfile:
        if not vsix_path.is_absolute():
            raise ValueError("VSIX 必须由目标文件 owner 解析为本机绝对路径")
        return await self._request({"method": "install", "vsix_path": str(vsix_path)}, response_type=InstalledProfile)

    async def uninstall(self, *, profile_id: str, extension_id: str, version: str) -> UninstalledProfile:
        return await self._request({"method": "uninstall", "profile_id": profile_id,
                                   "extension_id": extension_id, "version": version}, response_type=UninstalledProfile)

    async def close(self) -> None:
        self.close_admission()
        async with self._lock:
            await self._http.aclose()
            self._closed = True
