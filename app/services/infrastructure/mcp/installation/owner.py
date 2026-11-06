"""Backend 共享的制品安装 owner；工作区配置与 MCP 连接继续归原 owner。"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.core.workspace_identity import read_workspace_id
from app.domain.itemized.hashing import sha256_jcs
from app.domain.mcp_candidates import McpCandidateConflict
from app.schemas.internal_v2.mcp import (
    McpArtifactReferencesDTO,
    McpCandidatePlanDTO,
    McpInstallationCapabilitiesDTO,
    McpInstallationOperationDTO,
    McpInstallationRequestDTO,
    McpInstallationTargetDTO,
    McpInstalledArtifactDTO,
    McpManagedLaunchDTO,
    McpUninstallationRequestDTO,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp.candidate_source import native_mcp_platform
from app.services.infrastructure.mcp.installation.python import (
    InstallationUnavailable,
    artifact_digest,
    prepare_python_artifact,
    python_package,
)
from app.services.infrastructure.mcp.installation.references import (
    McpArtifactInUse,
    workspace_references,
)
from app.services.infrastructure.mcp.installation.store import (
    InstallationCancelled,
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

        executor = asyncio.current_task()
        assert executor is not None
        monitoring_error: Exception | None = None

        async def watch_cancellation() -> None:
            nonlocal monitoring_error
            try:
                while not await run_sync_shielded(self._store.cancellation_pending, operation.operation_id):
                    await asyncio.sleep(0.25)
            except Exception as error:  # noqa: BLE001 - 原后台错误交还执行 owner，不能静默丢失。
                # 监测失败交给原执行 task 结算，不能让后台 task 静默失效。
                monitoring_error = error
            executor.cancel("所有安装操作均已请求取消" if monitoring_error is None else "原安装取消监测失败")

        watcher = asyncio.create_task(watch_cancellation(), name=f"mcp-cancellation:{operation.operation_id}")
        try:
            artifact = reservation.artifact
            if artifact is not None:
                await progress("verify", "核验已有精确制品，复用原 generation；不重新下载或解析依赖\n")
                await run_sync_shielded(self.resolve, artifact.binding, command=artifact.command)
                await progress("publish")
            else:
                preparation = reservation.preparation
                if preparation is None:
                    raise RuntimeError("MCP 制品准备缺少事务准入，不能自行分配新 generation")
                installation_id, generation = preparation.installation_id, preparation.generation
                root = self._root / "generations" / installation_id / generation
                command, lock_digest, distributions = await prepare_python_artifact(root,
                    name=name, version=version, index=index, entrypoint=request.entrypoint, progress=progress,
                    execution_directory=self._root)
                await progress("publish")
                digest = await run_sync_shielded(artifact_digest, root)
                artifact = McpInstalledArtifactDTO(
                    binding=McpManagedLaunchDTO(backend_id=self.backend_id, installation_id=installation_id,
                        generation=generation, artifact_digest=digest),
                    source=plan.source, manifest_digest=plan.manifest_digest, variant=plan.variant, platform=plan.platform,
                    identifier=name, version=version, entrypoint=request.entrypoint, command=command,
                    lock_digest=lock_digest, installed_distributions=distributions)
            await run_sync_shielded(self._store.publish, operation, artifact)
        except BaseException as cause:
            error = monitoring_error or cause
            # 未发布目录保留给原操作核实，绝不返回可启动 binding，也不删除已发布旧版。
            cancelled = isinstance(error, InstallationCancelled) or (isinstance(error, asyncio.CancelledError)
                and await run_sync_shielded(self._store.cancellation_pending, operation.operation_id))
            operation = operation.model_copy(update={"status": "cancelled" if cancelled else
                "interrupted" if isinstance(error, asyncio.CancelledError) else "failed",
                "updated_at": time.time(), "error": None if cancelled else str(error) or type(error).__name__})
            try:
                await run_sync_shielded(self._store.update, operation)
            except BaseException as settlement_error:
                raise error from settlement_error
            if isinstance(error, asyncio.CancelledError) and not cancelled:
                raise
        finally:
            watcher.cancel()

            async def drain_watcher() -> None:
                try:
                    await watcher
                except asyncio.CancelledError:
                    pass

            await run_cleanup_shielded(drain_watcher)

    async def cancel(self, operation_id: str) -> McpInstallationOperationDTO:
        return await run_sync_shielded(self._store.request_cancel, operation_id)

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
        if artifact.state != "published":
            raise McpCandidateConflict(f"MCP 制品当前为 {artifact.state}，不能作为 ready launch；原卸载操作: {artifact.removal_operation_id}")
        root = self._root / "generations" / binding.installation_id / binding.generation
        expected_command = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
            artifact.entrypoint + ".exe" if os.name == "nt" else artifact.entrypoint)
        if artifact.command != str(expected_command) or not expected_command.is_file():
            raise ValueError("受管 MCP 制品发布路径丢失或与唯一安装记录不一致")
        return artifact

    def resolve(self, binding: McpManagedLaunchDTO, *, command: str,
        args: Sequence[str] = ()) -> tuple[str, list[str]]:
        artifact = self.artifact(binding)
        if command != artifact.command:
            raise ValueError("受管 MCP 连接配置与已发布 generation 的实际启动入口不一致")
        root = self._root / "generations" / binding.installation_id / binding.generation
        if artifact_digest(root) != binding.artifact_digest:
            raise ValueError("受管 MCP 制品内容已改变；拒绝启动，必须重新验证并发布新 generation")
        # Python 直接执行生态生成的脚本／Windows ZIP 入口，保留其 argv 与 entry point 语义。
        # 避免 Windows console trampoline 内部再次启动长路径 Python 时触发 MAX_PATH。
        python = Path(command).with_name("python.exe" if os.name == "nt" else "python")
        return str(python), ["-B", command, *args]

    def register_workspace(self, root: Path, leases: ExternalResourceLeaseLedger) -> None:
        root = root.resolve(strict=True)
        workspace_id = read_workspace_id(root)
        state_path = root / ".boxteam" / "resources.json"
        with self._store.reference_write() as connection:
            created = self._store.register_workspace(connection, workspace_id, root)
            if not created and not state_path.is_file():
                raise RuntimeError("已登记 MCP 工作区的原资源账本缺失，禁止重建空事实")
            leases.ensure_persisted(state_path=state_path)

    @contextmanager
    def reference_write(self, bindings: Sequence[McpManagedLaunchDTO]) -> Iterator[None]:
        """原配置 writer 与原连接 preparing 在短安装事务内写入其唯一事实。"""
        with self._store.reference_write() as connection:
            for binding in bindings:
                if binding.backend_id != self.backend_id:
                    raise McpCandidateConflict("受管 MCP 引用不属于当前 Backend 安装域")
                self._store.require_artifact(connection, binding)
            yield

    def references(self, installation_id: str) -> McpArtifactReferencesDTO:
        with self._store.reference_write():
            artifact = self._store.artifact(installation_id)
            return self._references(artifact.binding)

    def _references(self, binding: McpManagedLaunchDTO) -> McpArtifactReferencesDTO:
        return McpArtifactReferencesDTO(binding=binding, references=[
            reference for workspace_id, root in self._store.workspaces()
            for reference in workspace_references(root, workspace_id, binding)
        ])

    async def uninstall(self, request: McpUninstallationRequestDTO, *, actual_target: McpInstallationTargetDTO) -> McpInstallationOperationDTO:
        if self._closed:
            raise InstallationUnavailable("Backend 制品安装 owner 已关闭准入")
        if request.target != actual_target or request.binding.backend_id != self.backend_id:
            raise McpCandidateConflict("MCP 卸载必须绑定原生目标与精确制品")
        now = time.time()
        operation = McpInstallationOperationDTO(operation_id=request.operation_id, kind="uninstall", status="running", stage="remove",
            intent_digest=sha256_jcs({"schema": "mcp-uninstall:v1", "target": request.target.model_dump(), "binding": request.binding.model_dump()}),
            created_at=now, updated_at=now)

        def check_references() -> None:
            references = self._references(request.binding)
            if references.references:
                raise McpArtifactInUse(references)

        reservation: tuple[McpInstallationOperationDTO, bool] | None = None

        def reserve() -> tuple[McpInstallationOperationDTO, bool]:
            nonlocal reservation
            reservation = self._store.reserve_uninstall(operation, request.binding, check_references)
            return reservation

        try:
            operation, accepted = await run_sync_shielded(reserve)
        except asyncio.CancelledError as error:
            # worker 必须先结算；若准入已经提交，明确中断原操作，不留下活 PID 的悬挂 removing。
            if reservation is not None and reservation[1]:
                try:
                    await run_sync_shielded(self._store.finish_uninstall, reservation[0],
                        error="原卸载请求在准入后、删除开始前取消；请核实原操作后创建新清理意图", interrupted=True)
                except BaseException as settlement_error:
                    raise error from settlement_error
            raise
        if accepted:
            task = asyncio.create_task(self._remove(operation), name=f"mcp-uninstall:{operation.operation_id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return operation

    @staticmethod
    def _remove_directory(root: Path) -> None:
        # 新意图可继续清理原失败目录；目录已核实不存在时只结算原记录，不重放安装。
        if root.exists():
            shutil.rmtree(root)
        if root.exists() or root.is_symlink():
            raise RuntimeError("MCP 卸载结束后精确 generation 目录仍存在")

    async def _remove(self, operation: McpInstallationOperationDTO) -> None:
        if operation.artifact is None:
            raise RuntimeError("MCP 卸载操作缺少原制品身份")
        binding = operation.artifact.binding
        root = self._root / "generations" / binding.installation_id / binding.generation
        try:
            # removing 已先提交；删目录失败／进程中断均不再允许该制品启动。
            await run_sync_shielded(self._remove_directory, root)
            await run_sync_shielded(self._store.finish_uninstall, operation)
        except BaseException as error:
            try:
                await run_sync_shielded(self._store.finish_uninstall, operation,
                    error=str(error) or type(error).__name__, interrupted=isinstance(error, asyncio.CancelledError))
            except BaseException as settlement_error:
                raise error from settlement_error
            if isinstance(error, asyncio.CancelledError):
                raise

    async def close(self) -> None:
        self._closed = True
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks))
