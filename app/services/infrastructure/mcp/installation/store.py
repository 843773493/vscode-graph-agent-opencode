"""Backend 安装域唯一账本；发布制品与操作完成在同一事务中提交。"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

import portalocker
import psutil

from app.domain.mcp_candidates import McpCandidateConflict
from app.schemas.internal_v2.mcp import (
    McpInstallationOperationDTO,
    McpInstalledArtifactDTO,
)


class InstallationStore:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._path = root / "installations.sqlite"
        # 同一安装域可有多个 Backend 进程；仅 schema 初始化串行，账本事务唯一准入。
        with portalocker.Lock(root / "schema.lock", timeout=10):
            self._initialize()
        self._process_started = psutil.Process(os.getpid()).create_time()

    def _initialize(self) -> None:
        with self._connect() as connection:
            version, = connection.execute("PRAGMA user_version").fetchone()
            if version not in {0, 1}:
                raise RuntimeError(f"MCP 安装账本版本不受支持: {version}")
            connection.executescript(files("app.services.infrastructure.mcp.installation").joinpath("schema.sql").read_text(encoding="utf-8"))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def reserve(self, operation: McpInstallationOperationDTO) -> tuple[McpInstallationOperationDTO, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()
            if row is not None:
                existing = McpInstallationOperationDTO.model_validate_json(row[0])
                if existing.intent_digest != operation.intent_digest:
                    raise McpCandidateConflict("MCP 安装 operation_id 已绑定另一目标、来源、制品或入口意图")
                return self._reconcile(connection, existing), False
            connection.execute("INSERT INTO operations VALUES (?, ?)", (operation.operation_id, operation.model_dump_json()))
            connection.execute("INSERT INTO operation_owners VALUES (?, ?, ?)",
                (operation.operation_id, os.getpid(), self._process_started))
            return operation, True

    def operation(self, operation_id: str) -> McpInstallationOperationDTO:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise KeyError(f"MCP 安装操作不存在: {operation_id}")
            return self._reconcile(connection, McpInstallationOperationDTO.model_validate_json(row[0]))

    def _reconcile(self, connection: sqlite3.Connection, operation: McpInstallationOperationDTO) -> McpInstallationOperationDTO:
        if operation.status != "running":
            return operation
        row = connection.execute("SELECT pid, started FROM operation_owners WHERE operation_id=?", (operation.operation_id,)).fetchone()
        alive = False
        if row is not None:
            try:
                process = psutil.Process(row[0])
                alive = process.create_time() == row[1] and process.status() != psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                pass
        if alive:
            return operation
        interrupted = operation.model_copy(update={"status": "interrupted", "updated_at": time.time(),
            "error": "原安装进程已退出；仅保留已确认阶段，未发布制品不可启动，请检查原操作后创建新意图"})
        connection.execute("UPDATE operations SET payload=? WHERE operation_id=?", (interrupted.model_dump_json(), operation.operation_id))
        return interrupted

    def _current(self, connection: sqlite3.Connection, operation: McpInstallationOperationDTO) -> McpInstallationOperationDTO:
        row = connection.execute("SELECT payload FROM operations WHERE operation_id=?", (operation.operation_id,)).fetchone()
        if row is None:
            raise RuntimeError("MCP 安装操作在写入阶段消失")
        current = McpInstallationOperationDTO.model_validate_json(row[0])
        if current.intent_digest != operation.intent_digest:
            raise McpCandidateConflict("MCP 安装写入与原操作意图不一致")
        return current

    def update(self, operation: McpInstallationOperationDTO) -> McpInstallationOperationDTO:
        if operation.status == "succeeded" or operation.artifact is not None:
            raise ValueError("MCP 安装成功只能随制品发布提交")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._current(connection, operation)
            # 迟到进度、取消或失败只能观察原终态，不能撤回已提交的真实结果。
            if current.status != "running":
                return current
            connection.execute("UPDATE operations SET payload=? WHERE operation_id=?", (operation.model_dump_json(), operation.operation_id))
            return operation

    def publish(self, operation: McpInstallationOperationDTO, artifact: McpInstalledArtifactDTO) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._current(connection, operation)
            if current.status != "running":
                raise McpCandidateConflict(f"MCP 安装操作已结算为 {current.status}，不能迟到发布制品")
            complete = current.model_copy(update={"status": "succeeded", "stage": "complete", "updated_at": time.time(), "artifact": artifact})
            connection.execute("INSERT INTO artifacts VALUES (?, ?)", (artifact.binding.installation_id, artifact.model_dump_json()))
            connection.execute("UPDATE operations SET payload=? WHERE operation_id=?", (complete.model_dump_json(), operation.operation_id))

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
