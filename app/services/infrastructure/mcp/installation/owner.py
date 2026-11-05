"""Backend 共享的制品安装 owner；工作区配置与 MCP 连接继续归原 owner。"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import cast

from app.core.shielded_cleanup import run_sync_shielded
from app.domain.itemized.hashing import sha256_jcs
from app.domain.mcp_candidates import McpCandidateConflict
from app.schemas.internal_v2.mcp import (
    McpCandidatePlanDTO,
    McpInstallationCapabilitiesDTO,
    McpInstallationOperationDTO,
    McpInstallationRequestDTO,
    McpInstalledArtifactDTO,
    McpManagedLaunchDTO,
)
from app.services.infrastructure.mcp.candidate_source import native_mcp_platform
from app.services.infrastructure.mcp.installation.python import (
    InstallationUnavailable,
    artifact_digest,
    prepare_python_artifact,
    python_package,
)
from app.services.infrastructure.mcp.installation.store import (
    InstallationReservation,
    InstallationStore,
    artifact_key,
)


class McpInstallationOwner:
    def __init__(self, *, home: Path, backend_id: str) -> None:
        self.backend_id = backend_id
        self._root = home / "artifacts" / "mcp" / backend_id
        self._store = InstallationStore(self._root)
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def capabilities(self) -> McpInstallationCapabilitiesDTO:
        reason = None
        try:
            python_package({"registry_type": "pypi", "identifier": "capability-check", "version": "1"})
        except InstallationUnavailable as error:
            reason = str(error)
        if self._closed:
            reason = "Backend 制品安装 owner 已关闭准入"
        return McpInstallationCapabilitiesDTO(platform=native_mcp_platform(),
            pypi_wheel_available=reason is None, unavailable_reason=reason)

    async def install(self, request: McpInstallationRequestDTO, *, plan: McpCandidatePlanDTO,
        parameters: dict[str, object]) -> McpInstallationOperationDTO:
        if self._closed:
            raise InstallationUnavailable("Backend 制品安装 owner 已关闭准入")
        if request.candidate.target.backend_id != self.backend_id or plan.plan_digest != request.plan_digest:
            raise McpCandidateConflict("MCP 安装请求与原 Backend／候选计划不一致")
        if plan.disposition != "requires_artifact":
            raise InstallationUnavailable("该候选属于配置或显式动态命令，没有可安装制品")
        name, version, index = python_package(parameters)
        intent_digest = sha256_jcs({"target": request.candidate.target.model_dump(), "plan_digest": plan.plan_digest,
            "entrypoint": request.entrypoint, "registry": index, "package": name, "version": version})
        now = time.time()
        reservation = self._store.reserve(McpInstallationOperationDTO(
            operation_id=request.operation_id, intent_digest=intent_digest, status="running", stage="prepare", created_at=now, updated_at=now),
            key=artifact_key(plan, identifier=name, version=version, entrypoint=request.entrypoint))
        if reservation.artifact is not None or reservation.preparation is not None:
            task = asyncio.create_task(self._prepare(reservation, request, plan, name, version, index),
                name=f"mcp-installation:{request.operation_id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return reservation.operation

    async def _prepare(self, reservation: InstallationReservation, request: McpInstallationRequestDTO,
        plan: McpCandidatePlanDTO, name: str, version: str, index: str) -> None:
        operation = reservation.operation
        async def progress(stage: str, output: str = "") -> None:
            nonlocal operation
            operation = operation.model_copy(update={"stage": cast(str, stage), "updated_at": time.time(),
                "log": (operation.log + output)[-65536:]})
            operation = await run_sync_shielded(self._store.update, operation)

        try:
            artifact = reservation.artifact
            if artifact is not None:
                await progress("verify", "核验已有精确制品，复用原 generation；不重新下载或解析依赖\n")
                await run_sync_shielded(self.resolve, artifact.binding, verify=True)
                await progress("publish")
            else:
                preparation = reservation.preparation
                if preparation is None:
                    raise RuntimeError("MCP 制品准备缺少事务准入，不能自行分配新 generation")
                installation_id, generation = preparation.installation_id, preparation.generation
                root = self._root / "generations" / installation_id / generation
                command, lock_digest, distributions = await prepare_python_artifact(root,
                    name=name, version=version, index=index, entrypoint=request.entrypoint, progress=progress)
                await progress("publish")
                digest = await run_sync_shielded(artifact_digest, root)
                artifact = McpInstalledArtifactDTO(
                    binding=McpManagedLaunchDTO(backend_id=self.backend_id, installation_id=installation_id,
                        generation=generation, artifact_digest=digest),
                    source=plan.source, manifest_digest=plan.manifest_digest, variant=plan.variant, platform=plan.platform,
                    identifier=name, version=version, entrypoint=request.entrypoint, command=command,
                    lock_digest=lock_digest, installed_distributions=distributions)
            # TODO(2.5)：工作区持久引用及物理卸载保护仍需实现，不能据此删除 generation。
            await run_sync_shielded(self._store.publish, operation, artifact)
        except BaseException as error:
            # 未发布目录保留给原操作核实，绝不返回可启动 binding，也不删除已发布旧版。
            operation = operation.model_copy(update={"status": "interrupted" if isinstance(error, asyncio.CancelledError) else "failed",
                "updated_at": time.time(), "error": str(error) or type(error).__name__})
            try:
                await run_sync_shielded(self._store.update, operation)
            except BaseException as settlement_error:
                raise error from settlement_error
            if isinstance(error, asyncio.CancelledError):
                raise

    def operation(self, operation_id: str) -> McpInstallationOperationDTO:
        return self._store.operation(operation_id)

    def artifacts(self) -> list[McpInstalledArtifactDTO]:
        # 暖读只读唯一账本，文件全量校验仅在安装发布与实际 launch 准入执行。
        return self._store.artifacts()

    def artifact(self, binding: McpManagedLaunchDTO) -> McpInstalledArtifactDTO:
        if binding.backend_id != self.backend_id:
            raise ValueError("受管 MCP launch 不属于当前 Backend，不能使用同名本机路径")
        artifact = self._store.artifact(binding.installation_id)
        if artifact.binding != binding:
            raise ValueError("受管 MCP launch 的 generation／digest 与唯一已发布记录不匹配")
        root = self._root / "generations" / binding.installation_id / binding.generation
        expected_command = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
            artifact.entrypoint + ".exe" if os.name == "nt" else artifact.entrypoint)
        if artifact.command != str(expected_command) or not expected_command.is_file():
            raise ValueError("受管 MCP 制品发布路径丢失或与唯一安装记录不一致")
        return artifact

    def resolve(self, binding: McpManagedLaunchDTO, *, verify: bool = False) -> str:
        artifact = self.artifact(binding)
        root = self._root / "generations" / binding.installation_id / binding.generation
        if verify and artifact_digest(root) != binding.artifact_digest:
            raise ValueError("受管 MCP 制品内容已改变；拒绝启动，必须重新验证并发布新 generation")
        return artifact.command

    async def close(self) -> None:
        self._closed = True
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks))
