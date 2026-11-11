"""Gateway 自有插件包的唯一目标安装操作。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import httpx
import psutil

from app.core.async_channel import run_channel
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.plugins.packages import PluginInstallCancelled, PluginPackageStore
from app.gateway.plugins.sources import enabled_source
from app.schemas.gateway.plugin_installation import (
    FrozenPluginInstallArtifact,
    InstallPluginRequest,
    PluginInstallOperationDTO,
)
from app.services.infrastructure.config.state import ConfigConflictError

_MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
_CANCEL_CHECK_INTERVAL_SECONDS = 0.25


class PluginInstallations:
    def __init__(
        self,
        gateway_id: str,
        packages: PluginPackageStore,
        state_store: GatewayStateStore,
        client: httpx.AsyncClient,
        configuration: Callable[[], dict[str, object]],
    ) -> None:
        self.gateway_id = gateway_id
        self.packages = packages
        self.state_store = state_store
        self.client = client
        self.configuration = configuration
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self._owner_created_at = psutil.Process(os.getpid()).create_time()

    async def start(self, request: InstallPluginRequest) -> PluginInstallOperationDTO:
        if self._closed:
            raise RuntimeError("插件安装操作 owner 已关闭")
        if request.gateway_id != self.gateway_id:
            raise ValueError("插件安装目标 Gateway 不匹配")
        request_json = _request_identity(request)
        existing = self.state_store.get_plugin_install_operation(
            gateway_id=self.gateway_id, operation_id=request.operation_id,
        )
        if existing is not None:
            if _dump(existing["request"]) != request_json:
                raise ConfigConflictError(f"插件安装 operation_id 已绑定不同输入: {request.operation_id}")
            return self.get(request.operation_id)
        if request.source_id is not None:
            enabled_source(self.configuration(), request.source_id)
        source_kind: Literal["local_zip", "development_directory"]
        if request.source_id is None:
            path = Path(request.path)
            if request.development_directory:
                manifest, digest = await asyncio.to_thread(self.packages.inspect_directory, path)
                source_kind = "development_directory"
            else:
                manifest, digest = await asyncio.to_thread(self.packages.inspect_zip, path, request.expected_digest)
                source_kind = "local_zip"
            frozen = FrozenPluginInstallArtifact(
                source_kind=source_kind, package_id=manifest.package_id,
                package_version=manifest.version, target=manifest.target,
                digest=f"sha256:{digest}", path=str(path.resolve(strict=True)),
            )
        else:
            artifact = request.artifact
            if artifact is None:
                raise ValueError("来源安装缺少目录制品")
            frozen = FrozenPluginInstallArtifact(
                source_kind="source", package_id=artifact.package_id,
                package_version=artifact.package_version, target=artifact.target,
                digest=artifact.digest, source_id=request.source_id,
                catalog_revision=request.catalog_revision,
                filename=artifact.filename,
            )
        artifact_json = _dump(frozen.model_dump(mode="json"))
        record, created = self.state_store.create_plugin_install_operation(
            gateway_id=self.gateway_id, operation_id=request.operation_id,
            request_json=request_json, artifact_json=artifact_json,
            owner_pid=os.getpid(), owner_created_at=self._owner_created_at,
        )
        if created:
            task = asyncio.create_task(self._execute(request, frozen), name=f"plugin-install:{request.operation_id}")
            self._tasks[request.operation_id] = task
        return self._dto(record)

    def get(self, operation_id: str) -> PluginInstallOperationDTO:
        record = self.state_store.get_plugin_install_operation(gateway_id=self.gateway_id, operation_id=operation_id)
        if record is None:
            raise KeyError(f"插件安装操作不存在: {operation_id}")
        if record["status"] == "running" and not _process_identity_alive(record.get("owner_pid"), record.get("owner_created_at")):
            owner_pid = record.get("owner_pid")
            owner_created_at = record.get("owner_created_at")
            if not isinstance(owner_pid, int) or not isinstance(owner_created_at, (int, float)):
                raise RuntimeError(f"插件安装操作缺少有效执行者身份: {operation_id}")
            self.state_store.finish_plugin_install_operation(
                gateway_id=self.gateway_id, operation_id=operation_id,
                owner_pid=owner_pid, owner_created_at=float(owner_created_at),
                status="unknown", error="执行者已退出；发布结果未确认，不会自动重放",
            )
            record = self.state_store.get_plugin_install_operation(gateway_id=self.gateway_id, operation_id=operation_id)
            if record is None:
                raise RuntimeError("插件安装操作状态更新后无法读取")
        return self._dto(record)

    def cancel(self, operation_id: str) -> PluginInstallOperationDTO:
        record = self.state_store.request_plugin_install_cancel(gateway_id=self.gateway_id, operation_id=operation_id)
        return self._dto(record)

    async def close(self) -> None:
        self._closed = True
        for operation_id, task in tuple(self._tasks.items()):
            if not task.done():
                self.state_store.request_plugin_install_cancel(
                    gateway_id=self.gateway_id, operation_id=operation_id,
                )
        tasks = tuple(self._tasks.values())
        if tasks:
            pending = asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(asyncio.shield(pending), timeout=60)
            except TimeoutError as error:
                raise RuntimeError("关闭插件安装 owner 超时；在途下载或实际安装线程仍未结清") from error

    async def _execute(self, request: InstallPluginRequest, artifact: FrozenPluginInstallArtifact) -> None:
        operation_id = request.operation_id
        temporary: Path | None = None
        try:
            if artifact.source_kind == "source":
                source_artifact = request.artifact
                if source_artifact is None:
                    raise RuntimeError("来源安装操作缺少已冻结的来源制品")
                temporary = await self._download(
                    source_artifact.url, artifact.filename, operation_id,
                )
                self._require_not_cancelled(operation_id)
                path = temporary
            else:
                if artifact.path is None:
                    raise RuntimeError("本地插件安装操作缺少已冻结路径")
                path = Path(artifact.path)
            install = (
                self.packages.install_directory
                if artifact.source_kind == "development_directory"
                else self.packages.install_zip
            )
            await asyncio.to_thread(
                install,
                path,
                artifact.digest,
                expected_package_id=artifact.package_id,
                expected_package_version=artifact.package_version,
                expected_target=artifact.target,
                commit_publication=lambda publish: self.state_store.commit_plugin_install_publication(
                    gateway_id=self.gateway_id,
                    operation_id=operation_id,
                    owner_pid=os.getpid(),
                    owner_created_at=self._owner_created_at,
                    publish=publish,
                ),
            )
            record = self.state_store.get_plugin_install_operation(gateway_id=self.gateway_id, operation_id=operation_id)
            if record is not None and record["status"] == "running":
                self.state_store.finish_plugin_install_operation(
                    gateway_id=self.gateway_id, operation_id=operation_id,
                    owner_pid=os.getpid(), owner_created_at=self._owner_created_at,
                    status="unknown", error="包安装结束但终态未能在发布事务中确认；不会自动重放",
                )
        except PluginInstallCancelled:
            self.state_store.finish_plugin_install_operation(
                gateway_id=self.gateway_id, operation_id=operation_id,
                owner_pid=os.getpid(), owner_created_at=self._owner_created_at,
                status="cancelled", error=None,
            )
        except Exception as error:  # noqa: BLE001 - 必须把任何执行失败结清为可查询终态
            record = self.state_store.get_plugin_install_operation(gateway_id=self.gateway_id, operation_id=operation_id)
            if record is not None and record["status"] == "running":
                status = "failed"
                detail: str | None = _operation_error(error)
                try:
                    published = self.packages.get(artifact.package_id, artifact.package_version)
                except FileNotFoundError:
                    pass
                else:
                    if f"sha256:{published.digest}" == artifact.digest:
                        status = "unknown"
                        detail = "包原子发布已发生，但操作终态未确认；不会自动重放"
                if record["cancel_requested"] and status == "failed":
                    status = "cancelled"
                    detail = None
                self.state_store.finish_plugin_install_operation(
                    gateway_id=self.gateway_id, operation_id=operation_id,
                    owner_pid=os.getpid(), owner_created_at=self._owner_created_at,
                    status=status, error=detail,
                )
        finally:
            if temporary is not None:
                # TODO: 硬杀进程会跳过此处而留下部分 ZIP；只在能证明原 owner 已退出后做定向清理。
                temporary.unlink(missing_ok=True)
            self._tasks.pop(operation_id, None)

    async def _download(self, url: str | None, filename: str | None, operation_id: str) -> Path:
        if not url or not filename:
            raise ValueError("来源安装冻结制品缺少下载地址或文件名")
        descriptor, name = tempfile.mkstemp(prefix=".source-download-", suffix=".zip", dir=self.packages.root)
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                async def transfer() -> None:
                    total = 0
                    async with self.client.stream("GET", url) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > _MAX_DOWNLOAD_BYTES:
                                raise ValueError("插件来源 ZIP 下载超过 256 MiB")
                            output.write(chunk)

                async def observe_cancel() -> None:
                    # 取消沿原SQLite意图，也覆盖等待响应头和停滞响应体；不取消兄弟操作。
                    while True:
                        self._require_not_cancelled(operation_id)
                        await asyncio.sleep(_CANCEL_CHECK_INTERVAL_SECONDS)

                await run_channel(transfer(), observe_cancel())
                output.flush()
                os.fsync(output.fileno())
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def _require_not_cancelled(self, operation_id: str) -> None:
        record = self.state_store.get_plugin_install_operation(gateway_id=self.gateway_id, operation_id=operation_id)
        if record is not None and record["cancel_requested"]:
            raise PluginInstallCancelled("插件安装已取消")

    def _dto(self, record: dict[str, object]) -> PluginInstallOperationDTO:
        return PluginInstallOperationDTO.model_validate({
            "gateway_id": self.gateway_id,
            "operation_id": record["operation_id"],
            "status": record["status"],
            "artifact": record["artifact"],
            "cancel_requested": record["cancel_requested"],
            "error": record["error"],
        })

def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request_identity(request: InstallPluginRequest) -> str:
    request_json = _dump(request.model_dump(mode="json"))
    digest = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    return _dump({"sha256": digest})


def _operation_error(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"插件来源请求失败: HTTP {error.response.status_code}"
    if isinstance(error, httpx.HTTPError):
        return f"插件来源传输失败: {type(error).__name__}"
    return str(error)[:2000]


def _process_identity_alive(pid: object, created_at: object) -> bool:
    if not isinstance(pid, int) or not isinstance(created_at, (float, int)):
        return False
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    return (
        process.create_time() == float(created_at)
        and process.status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
    )


__all__ = ["PluginInstallations"]
