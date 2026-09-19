from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugLaunchClaimDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.node_debug_thread_owner import (
    SessionNodePathResolver,
    resolve_debug_thread_node,
    resolve_node_debug_owner,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class NodeDebugSessionStore:
    """保存会话 manifest、可移植调试方案与线程级启动登记。

    写路径（manifest/方案/启动登记）只接受权威折叠后的 owner 形态：``(P, child)``
    这类别名必须由服务入口先折叠为 ``(child, main)``，否则拒绝写入，避免两个 key
    共写同一 ``debug/node/`` 目录。
    """

    MANIFEST_FILE_NAME = "manifest.json"
    CONFIGURATIONS_DIRECTORY_NAME = "configurations"
    LAUNCH_CLAIM_FILE_NAME = "launch-claim.json"

    def __init__(self, path_resolver: SessionNodePathResolver) -> None:
        self._path_resolver = path_resolver

    @property
    def path_resolver(self) -> SessionNodePathResolver:
        """暴露受检目录索引解析器，供服务层入口统一归一 owner（含别名折叠）。"""
        return self._path_resolver

    def read_manifest(
        self, session_id: str, thread_id: str
    ) -> NodeDebugSessionManifestDTO | None:
        path = self._manifest_path(session_id, thread_id)
        if not path.exists():
            return None
        manifest = self._read_model(path, NodeDebugSessionManifestDTO)
        if manifest.session_id != session_id or manifest.thread_id != thread_id:
            raise RuntimeError(
                "源码调试 manifest 的 SessionThread 不匹配: "
                f"path={path}, expected=({session_id}, {thread_id}), "
                f"actual=({manifest.session_id}, {manifest.thread_id})"
            )
        return manifest

    def write_manifest(self, manifest: NodeDebugSessionManifestDTO) -> None:
        self._assert_canonical_owner(manifest.session_id, manifest.thread_id)
        self._atomic_write(
            self._manifest_path(manifest.session_id, manifest.thread_id),
            manifest.model_copy(update={"updated_at": datetime.now(UTC)}),
        )

    def read_launch_claim(
        self, session_id: str, thread_id: str
    ) -> NodeDebugLaunchClaimDTO | None:
        path = self._launch_claim_path(session_id, thread_id)
        if not path.exists():
            return None
        claim = self._read_model(path, NodeDebugLaunchClaimDTO)
        if claim.session_id != session_id or claim.thread_id != thread_id:
            raise RuntimeError(
                "调试启动登记的 SessionThread 不匹配: "
                f"path={path}, expected=({session_id}, {thread_id}), "
                f"actual=({claim.session_id}, {claim.thread_id})"
            )
        return claim

    def write_launch_claim(self, claim: NodeDebugLaunchClaimDTO) -> None:
        self._assert_canonical_owner(claim.session_id, claim.thread_id)
        self._atomic_write(
            self._launch_claim_path(claim.session_id, claim.thread_id),
            claim.model_copy(update={"updated_at": datetime.now(UTC)}),
        )

    def list_configurations(
        self, session_id: str, thread_id: str
    ) -> list[NodeDebugConfigurationDTO]:
        directory = self._configurations_directory(session_id, thread_id)
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
        thread_id: str,
    ) -> NodeDebugConfigurationDTO | None:
        path = self._configuration_path(session_id, thread_id, configuration_id)
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
        thread_id: str,
    ) -> None:
        self._assert_canonical_owner(session_id, thread_id)
        self._atomic_write(
            self._configuration_path(
                session_id, thread_id, configuration.configuration_id
            ),
            configuration.model_copy(update={"updated_at": datetime.now(UTC)}),
        )

    def delete_configuration(
        self, session_id: str, configuration_id: str, thread_id: str
    ) -> None:
        self._assert_canonical_owner(session_id, thread_id)
        path = self._configuration_path(session_id, thread_id, configuration_id)
        if not path.exists():
            raise FileNotFoundError(f"调试方案不存在: {configuration_id}")
        path.unlink()

    def _assert_canonical_owner(self, session_id: str, thread_id: str) -> None:
        """写路径只接受权威折叠后的 owner，拒绝绕过服务入口的别名写入。"""
        canonical = resolve_node_debug_owner(
            self._path_resolver,
            session_id=session_id,
            thread_id=thread_id,
        )
        if canonical.key != (session_id, thread_id):
            raise ValueError(
                "调试存储 owner 必须是权威折叠形态，拒绝别名写入: "
                f"given=({session_id}, {thread_id}), "
                f"canonical=({canonical.session_id}, {canonical.thread_id})"
            )

    def _debug_directory(self, session_id: str, thread_id: str) -> Path:
        """定位 ``<thread_node>/debug/node/``；thread 节点一律由目录索引解析。"""
        if not thread_id:
            raise ValueError("Node 调试持久化必须显式提供非空 thread_id")
        return (
            resolve_debug_thread_node(
                self._path_resolver,
                session_id=session_id,
                thread_id=thread_id,
            )
            / "debug"
            / "node"
        )

    def _manifest_path(self, session_id: str, thread_id: str) -> Path:
        return self._debug_directory(session_id, thread_id) / self.MANIFEST_FILE_NAME

    def _launch_claim_path(self, session_id: str, thread_id: str) -> Path:
        return (
            self._debug_directory(session_id, thread_id)
            / self.LAUNCH_CLAIM_FILE_NAME
        )

    def _configurations_directory(self, session_id: str, thread_id: str) -> Path:
        return self._debug_directory(session_id, thread_id) / self.CONFIGURATIONS_DIRECTORY_NAME

    def _configuration_path(
        self, session_id: str, thread_id: str, configuration_id: str
    ) -> Path:
        if re.fullmatch(r"dbgcfg_[0-9a-f]{32}", configuration_id) is None:
            raise ValueError(f"非法调试方案 ID: {configuration_id}")
        return self._configurations_directory(session_id, thread_id) / f"{configuration_id}.json"

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
