from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.schemas.public_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugSessionManifestDTO,
)


class SessionNodePathResolver(Protocol):
    def resolve_session_node(self, session_id: str) -> Path: ...


ModelT = TypeVar("ModelT", bound=BaseModel)


class NodeDebugSessionStore:
    """保存会话 manifest 和可独立复制的源码调试方案文件。"""

    MANIFEST_FILE_NAME = "manifest.json"
    CONFIGURATIONS_DIRECTORY_NAME = "configurations"

    def __init__(self, path_resolver: SessionNodePathResolver) -> None:
        self._path_resolver = path_resolver

    def read_manifest(self, session_id: str) -> NodeDebugSessionManifestDTO | None:
        path = self._manifest_path(session_id)
        if not path.exists():
            return None
        manifest = self._read_model(path, NodeDebugSessionManifestDTO)
        if manifest.session_id != session_id:
            raise RuntimeError(
                "会话源码调试 manifest 的 session_id 不匹配: "
                f"path={path}, expected={session_id}, actual={manifest.session_id}"
            )
        return manifest

    def write_manifest(self, manifest: NodeDebugSessionManifestDTO) -> None:
        self._atomic_write(
            self._manifest_path(manifest.session_id),
            manifest.model_copy(update={"updated_at": datetime.now(UTC)}),
        )

    def list_configurations(self, session_id: str) -> list[NodeDebugConfigurationDTO]:
        directory = self._configurations_directory(session_id)
        if not directory.exists():
            return []
        configurations: list[NodeDebugConfigurationDTO] = []
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"调试方案文件必须是普通文件: {path}")
            configuration = self._read_model(path, NodeDebugConfigurationDTO)
            expected_name = f"{configuration.configuration_id}.json"
            if path.name != expected_name:
                raise RuntimeError(
                    "调试方案文件名必须等于 configuration_id: "
                    f"path={path}, expected={expected_name}"
                )
            configurations.append(configuration)
        return configurations

    def read_configuration(
        self,
        session_id: str,
        configuration_id: str,
    ) -> NodeDebugConfigurationDTO | None:
        path = self._configuration_path(session_id, configuration_id)
        if not path.exists():
            return None
        configuration = self._read_model(path, NodeDebugConfigurationDTO)
        if configuration.configuration_id != configuration_id:
            raise RuntimeError(
                "调试方案文件内容与文件名不匹配: "
                f"path={path}, actual={configuration.configuration_id}"
            )
        return configuration

    def write_configuration(
        self,
        session_id: str,
        configuration: NodeDebugConfigurationDTO,
    ) -> None:
        self._atomic_write(
            self._configuration_path(session_id, configuration.configuration_id),
            configuration.model_copy(update={"updated_at": datetime.now(UTC)}),
        )

    def delete_configuration(self, session_id: str, configuration_id: str) -> None:
        path = self._configuration_path(session_id, configuration_id)
        if not path.exists():
            raise FileNotFoundError(f"调试方案不存在: {configuration_id}")
        path.unlink()

    def _debug_directory(self, session_id: str) -> Path:
        return self._path_resolver.resolve_session_node(session_id) / "debug" / "node"

    def _manifest_path(self, session_id: str) -> Path:
        return self._debug_directory(session_id) / self.MANIFEST_FILE_NAME

    def _configurations_directory(self, session_id: str) -> Path:
        return self._debug_directory(session_id) / self.CONFIGURATIONS_DIRECTORY_NAME

    def _configuration_path(self, session_id: str, configuration_id: str) -> Path:
        if re.fullmatch(r"dbgcfg_[0-9a-f]{32}", configuration_id) is None:
            raise ValueError(f"非法调试方案 ID: {configuration_id}")
        return self._configurations_directory(session_id) / f"{configuration_id}.json"

    @staticmethod
    def _read_model(path: Path, model_type: type[ModelT]) -> ModelT:
        try:
            return model_type.model_validate_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"会话源码调试数据损坏: {path}: {error}") from error

    @staticmethod
    def _atomic_write(path: Path, model: BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(model.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, path)


__all__ = ["NodeDebugSessionStore", "SessionNodePathResolver"]
