"""Backend 安装域唯一账本；准备准入、共享进度和发布均由事务结算。"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import portalocker
import psutil

from app.core.identifier import create_uuid_hex
from app.core.sqlite_state import load_sql
from app.domain.itemized.hashing import sha256_jcs
from app.domain.mcp_candidates import McpCandidateConflict
from app.schemas.internal_v2.mcp import (
    McpCandidatePlanDTO,
    McpInstallationOperationDTO,
    McpInstalledArtifactDTO,
)

_INTERRUPTED = "原安装进程已退出；仅保留已确认阶段，未发布制品不可启动，请检查原操作后创建新意图"


def artifact_key(candidate: McpCandidatePlanDTO | McpInstalledArtifactDTO, *, identifier: str, version: str, entrypoint: str) -> str:
    # 安装域由账本路径隔离；工作区 args/env 和 plan_digest 不参与制品身份。
    return sha256_jcs({"schema": "mcp-artifact:v1", "source": candidate.source.model_dump(),
        "manifest_digest": candidate.manifest_digest, "variant": candidate.variant.model_dump(),
        "platform": candidate.platform.model_dump(), "identifier": identifier, "version": version, "entrypoint": entrypoint})


@dataclass(frozen=True)
class ArtifactPreparation:
    installation_id: str
    generation: str


@dataclass(frozen=True)
class InstallationReservation:
    operation: McpInstallationOperationDTO
    artifact: McpInstalledArtifactDTO | None = None
    preparation: ArtifactPreparation | None = None


class InstallationStore:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._path = root / "installations.sqlite"
        # 同一安装域可有多个 Backend 进程；仅 schema 初始化串行，账本事务唯一准入。
        with portalocker.Lock(root / "schema.lock", timeout=10):
            self._initialize()
        self._process_started = psutil.Process(os.getpid()).create_time()

    def _execute_sql(self, connection: sqlite3.Connection, resource: str) -> None:
        # 两份仓库固定资产均为简单 DDL；逐条执行，避免 executescript 隐式提交。
        for statement in load_sql("app.services.infrastructure.mcp.installation", resource).split(";"):
            if statement.strip():
                connection.execute(statement)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version, = connection.execute("PRAGMA user_version").fetchone()
            if version == 0:
                self._execute_sql(connection, "schema.sql")
            elif version == 1:
                self._migrate_v1(connection)
            elif version != 2:
                raise RuntimeError(f"MCP 安装账本版本不受支持: {version}")

    def _migrate_v1(self, connection: sqlite3.Connection) -> None:
        artifacts: dict[str, McpInstalledArtifactDTO] = {}
        keyed_artifacts: dict[str, McpInstalledArtifactDTO] = {}
        conflicts: dict[str, list[McpInstalledArtifactDTO]] = {}
        for installation_id, payload in connection.execute("SELECT installation_id, payload FROM artifacts"):
            artifact = McpInstalledArtifactDTO.model_validate_json(payload)
            if artifact.binding.installation_id != installation_id:
                raise McpCandidateConflict(f"MCP 旧账本制品身份不一致，迁移已拒绝: {installation_id}")
            key = artifact_key(artifact, identifier=artifact.identifier, version=artifact.version, entrypoint=artifact.entrypoint)
            if key in keyed_artifacts:
                conflicts.setdefault(key, [keyed_artifacts[key]]).append(artifact)
            else:
                keyed_artifacts[key] = artifact
            artifacts[installation_id] = artifact
        if conflicts:
            details = "; ".join(f"{key}: {[artifact.binding.model_dump() for artifact in matches]}" for key, matches in conflicts.items())
            raise McpCandidateConflict(f"MCP 精确制品匹配多个已发布 generation，迁移已拒绝: {details}")
        owners = {operation_id: (pid, started) for operation_id, pid, started in
            connection.execute("SELECT operation_id, pid, started FROM operation_owners")}
        operations: list[tuple[str, str | None, str]] = []
        for operation_id, payload in connection.execute("SELECT operation_id, payload FROM operations"):
            operation = McpInstallationOperationDTO.model_validate_json(payload)
            if operation.operation_id != operation_id:
                raise McpCandidateConflict(f"MCP 旧账本操作身份不一致，迁移已拒绝: {operation_id}")
            key = None
            if operation.status == "running":
                if self._owner_alive(owners.get(operation_id), operation_id=operation_id):
                    raise McpCandidateConflict(f"MCP 旧账本仍有活进程安装，须等待原操作结算后迁移: {operation_id}")
                operation = operation.model_copy(update={"status": "interrupted", "updated_at": time.time(), "error": _INTERRUPTED})
            if operation.status == "succeeded":
                artifact = operation.artifact
                if artifact is None or artifacts.get(artifact.binding.installation_id) != artifact:
                    raise McpCandidateConflict(f"MCP 旧账本成功操作与已发布制品不一致，迁移已拒绝: {operation_id}")
                key = artifact_key(artifact, identifier=artifact.identifier, version=artifact.version, entrypoint=artifact.entrypoint)
            elif operation.artifact is not None:
                raise McpCandidateConflict(f"MCP 旧账本未成功操作含制品，迁移已拒绝: {operation_id}")
            operations.append((operation_id, key, operation.model_dump_json()))
        self._execute_sql(connection, "upgrade_v1.sql")
        self._execute_sql(connection, "schema.sql")
        connection.executemany("INSERT INTO artifacts VALUES (?, ?, ?)",
            [(artifact.binding.installation_id, key, artifact.model_dump_json()) for key, artifact in keyed_artifacts.items()])
        connection.executemany("INSERT INTO operations VALUES (?, ?, NULL, ?)", operations)
        connection.executemany("INSERT INTO operation_owners VALUES (?, ?, ?)",
            [(operation_id, pid, started) for operation_id, (pid, started) in owners.items()])

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _owner_alive(owner: tuple[int, float] | None, *, operation_id: str) -> bool:
        if owner is None:
            raise McpCandidateConflict(f"MCP 安装缺少原进程 PID／创建时间证据，不能推定已退出: {operation_id}")
        try:
            process = psutil.Process(owner[0])
            return process.create_time() == owner[1] and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    def reserve(self, operation: McpInstallationOperationDTO, *, key: str) -> InstallationReservation:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT artifact_key, payload FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()
            if row is not None:
                existing = McpInstallationOperationDTO.model_validate_json(row[1])
                if existing.intent_digest != operation.intent_digest or row[0] not in {None, key}:
                    raise McpCandidateConflict("MCP 安装 operation_id 已绑定另一目标、来源、制品或入口意图")
                return InstallationReservation(self._reconcile(connection, existing))
            artifact_row = connection.execute("SELECT payload FROM artifacts WHERE artifact_key=?", (key,)).fetchone()
            artifact = McpInstalledArtifactDTO.model_validate_json(artifact_row[0]) if artifact_row is not None else None
            preparation = None
            producer_id = operation.operation_id
            if artifact is None:
                row = connection.execute("SELECT operations.payload FROM artifact_preparations JOIN operations "
                    "ON operations.operation_id=artifact_preparations.operation_id WHERE artifact_preparations.artifact_key=?", (key,)).fetchone()
                if row is not None:
                    producer = self._reconcile(connection, McpInstallationOperationDTO.model_validate_json(row[0]))
                    if producer.status == "running":
                        producer_id = producer.operation_id
                        operation = self._execution(operation, producer).model_copy(update={"updated_at": max(operation.updated_at, producer.updated_at)})
                if producer_id == operation.operation_id:
                    preparation = ArtifactPreparation(create_uuid_hex(namespace="mcp-installation"), create_uuid_hex(namespace="mcp-artifact"))
            connection.execute("INSERT INTO operations VALUES (?, ?, ?, ?)",
                (operation.operation_id, key, producer_id, operation.model_dump_json()))
            if producer_id == operation.operation_id:
                connection.execute("INSERT INTO operation_owners VALUES (?, ?, ?)",
                    (operation.operation_id, os.getpid(), self._process_started))
            if preparation is not None:
                connection.execute("INSERT INTO artifact_preparations VALUES (?, ?, ?, ?)",
                    (key, operation.operation_id, preparation.installation_id, preparation.generation))
            return InstallationReservation(operation, artifact, preparation)

    def operation(self, operation_id: str) -> McpInstallationOperationDTO:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._reconcile(connection, self._read_operation(connection, operation_id))

    @staticmethod
    def _read_operation(connection: sqlite3.Connection, operation_id: str) -> McpInstallationOperationDTO:
        row = connection.execute("SELECT payload FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise KeyError(f"MCP 安装操作不存在: {operation_id}")
        return McpInstallationOperationDTO.model_validate_json(row[0])

    def _reconcile(self, connection: sqlite3.Connection, operation: McpInstallationOperationDTO) -> McpInstallationOperationDTO:
        if operation.status != "running":
            return operation
        producer_id, = connection.execute("SELECT producer_operation_id FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()
        if producer_id is None:
            raise RuntimeError(f"MCP 运行操作缺少原执行者: {operation.operation_id}")
        producer = operation if producer_id == operation.operation_id else self._read_operation(connection, producer_id)
        if producer.status != "running":
            raise RuntimeError(f"MCP 等待操作未随原执行者结算: {operation.operation_id}; {producer_id}")
        owner = connection.execute("SELECT pid, started FROM operation_owners WHERE operation_id=?", (producer_id,)).fetchone()
        if self._owner_alive(owner, operation_id=producer_id):
            return operation
        interrupted = producer.model_copy(update={"status": "interrupted", "updated_at": time.time(), "error": _INTERRUPTED})
        self._write_execution(connection, interrupted)
        return self._read_operation(connection, operation.operation_id)

    @staticmethod
    def _execution(operation: McpInstallationOperationDTO, producer: McpInstallationOperationDTO) -> McpInstallationOperationDTO:
        return operation.model_copy(update={"status": producer.status, "stage": producer.stage, "updated_at": producer.updated_at,
            "artifact": producer.artifact, "error": producer.error, "log": producer.log})

    def _write_execution(self, connection: sqlite3.Connection, producer: McpInstallationOperationDTO) -> None:
        rows = connection.execute("SELECT payload FROM operations WHERE producer_operation_id=?", (producer.operation_id,)).fetchall()
        for payload, in rows:
            operation = McpInstallationOperationDTO.model_validate_json(payload)
            if operation.status == "running":
                updated = self._execution(operation, producer)
                connection.execute("UPDATE operations SET payload=? WHERE operation_id=?", (updated.model_dump_json(), updated.operation_id))
        if producer.status != "running":
            connection.execute("DELETE FROM artifact_preparations WHERE operation_id=?", (producer.operation_id,))

    def _current(self, connection: sqlite3.Connection, operation: McpInstallationOperationDTO) -> tuple[McpInstallationOperationDTO, str]:
        row = connection.execute("SELECT payload, artifact_key, producer_operation_id FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()
        if row is None:
            raise RuntimeError("MCP 安装操作在写入阶段消失")
        current = McpInstallationOperationDTO.model_validate_json(row[0])
        if current.intent_digest != operation.intent_digest:
            raise McpCandidateConflict("MCP 安装写入与原操作意图不一致")
        if row[2] != operation.operation_id:
            raise McpCandidateConflict("MCP 等待操作不持有制品执行写入权，必须观察原执行者")
        owner = connection.execute("SELECT pid, started FROM operation_owners WHERE operation_id=?", (operation.operation_id,)).fetchone()
        if current.status == "running" and owner != (os.getpid(), self._process_started):
            raise McpCandidateConflict("MCP 安装写入不属于原执行进程")
        return current, row[1]

    def update(self, operation: McpInstallationOperationDTO) -> McpInstallationOperationDTO:
        if operation.status == "succeeded" or operation.artifact is not None:
            raise ValueError("MCP 安装成功只能随制品发布提交")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current, _ = self._current(connection, operation)
            # 迟到进度、取消或失败只能观察原终态，不能撤回已提交的真实结果。
            if current.status != "running":
                return current
            self._write_execution(connection, operation)
            return self._read_operation(connection, operation.operation_id)

    def publish(self, operation: McpInstallationOperationDTO, artifact: McpInstalledArtifactDTO) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current, key = self._current(connection, operation)
            if current.status != "running":
                raise McpCandidateConflict(f"MCP 安装操作已结算为 {current.status}，不能迟到发布制品")
            if artifact_key(artifact, identifier=artifact.identifier, version=artifact.version, entrypoint=artifact.entrypoint) != key:
                raise McpCandidateConflict("MCP 发布制品与原准备身份不一致")
            row = connection.execute("SELECT payload FROM artifacts WHERE artifact_key=? OR installation_id=?",
                (key, artifact.binding.installation_id)).fetchall()
            if row:
                if len(row) != 1 or McpInstalledArtifactDTO.model_validate_json(row[0][0]) != artifact:
                    raise McpCandidateConflict("MCP 已发布制品记录与本次发布不一致，不能改写原 generation")
            else:
                preparation = connection.execute("SELECT installation_id, generation FROM artifact_preparations WHERE artifact_key=? AND operation_id=?",
                    (key, operation.operation_id)).fetchone()
                if preparation != (artifact.binding.installation_id, artifact.binding.generation):
                    raise McpCandidateConflict("MCP 发布路径不属于原事务预留的 installation／generation")
                connection.execute("INSERT INTO artifacts VALUES (?, ?, ?)", (artifact.binding.installation_id, key, artifact.model_dump_json()))
            complete = current.model_copy(update={"status": "succeeded", "stage": "complete", "updated_at": time.time(), "artifact": artifact, "error": None})
            self._write_execution(connection, complete)

    def artifacts(self) -> list[McpInstalledArtifactDTO]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM artifacts ORDER BY installation_id").fetchall()
        return [McpInstalledArtifactDTO.model_validate_json(row[0]) for row in rows]

    def artifact(self, installation_id: str) -> McpInstalledArtifactDTO:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM artifacts WHERE installation_id=?", (installation_id,)).fetchone()
        if row is None:
            raise ValueError(f"MCP 制品尚未发布或不存在: {installation_id}")
        return McpInstalledArtifactDTO.model_validate_json(row[0])
