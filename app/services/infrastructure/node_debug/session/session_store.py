from __future__ import annotations

import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugLaunchClaimDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.node_debug.session.thread_owner import (
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
        _payload, manifest = self.read_manifest_payload(session_id, thread_id)
        return manifest

    def read_manifest_payload(
        self, session_id: str, thread_id: str
    ) -> tuple[bytes | None, NodeDebugSessionManifestDTO | None]:
        """读取 manifest 原始 bytes 与 DTO，供 source capture 做前后漂移校验。"""
        reference = f"session_id={session_id}, thread_id={thread_id}, artifact=manifest"
        payload = self._read_payload(self._manifest_path(session_id, thread_id), reference)
        if payload is None:
            return None, None
        manifest = self._read_model_bytes(payload, NodeDebugSessionManifestDTO, reference)
        if manifest.session_id != session_id or manifest.thread_id != thread_id:
            raise RuntimeError(
                "源码调试 manifest 的 SessionThread 不匹配: "
                f"expected=({session_id}, {thread_id}), "
                "reason=owner_mismatch"
            )
        return payload, manifest

    def write_manifest(self, manifest: NodeDebugSessionManifestDTO) -> None:
        self._assert_canonical_owner(manifest.session_id, manifest.thread_id)
        # manifest 是方案登记的唯一清单；每次写入都从当前持久方案重新生成，
        # 避免新增/删除方案后沿用旧 manifest 的陈旧 ID。source capture 只读取
        # 已落盘清单，不在 capture 阶段扫描目录补齐。
        configuration_ids = tuple(
            item.configuration_id
            for item in self.list_configurations(
                manifest.session_id, manifest.thread_id
            )
        )
        manifest = manifest.model_copy(
            update={"configuration_ids": configuration_ids}
        )
        self._atomic_write(
            self._manifest_path(manifest.session_id, manifest.thread_id),
            manifest.model_copy(update={"updated_at": datetime.now(UTC)}),
            f"session_id={manifest.session_id}, thread_id={manifest.thread_id}, artifact=manifest",
        )

    def read_launch_claim(
        self, session_id: str, thread_id: str
    ) -> NodeDebugLaunchClaimDTO | None:
        reference = f"session_id={session_id}, thread_id={thread_id}, artifact=launch_claim"
        payload = self._read_payload(self._launch_claim_path(session_id, thread_id), reference)
        if payload is None:
            return None
        claim = self._read_model_bytes(payload, NodeDebugLaunchClaimDTO, reference)
        if claim.session_id != session_id or claim.thread_id != thread_id:
            raise RuntimeError(
                "调试启动登记的 SessionThread 不匹配: "
                f"expected=({session_id}, {thread_id}), "
                "reason=owner_mismatch"
            )
        return claim

    def write_launch_claim(self, claim: NodeDebugLaunchClaimDTO) -> None:
        self._assert_canonical_owner(claim.session_id, claim.thread_id)
        self._atomic_write(
            self._launch_claim_path(claim.session_id, claim.thread_id),
            claim.model_copy(update={"updated_at": datetime.now(UTC)}),
            f"session_id={claim.session_id}, thread_id={claim.thread_id}, artifact=launch_claim",
        )

    def list_configurations(
        self, session_id: str, thread_id: str
    ) -> list[NodeDebugConfigurationDTO]:
        directory = self._configurations_directory(session_id, thread_id)
        reference = f"session_id={session_id}, thread_id={thread_id}, artifact=configurations"
        try:
            try:
                mode = directory.lstat().st_mode
            except FileNotFoundError:
                return []
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"调试方案目录不允许符号链接: {reference}")
            paths = sorted(path for path in directory.iterdir() if path.suffix == ".json")
        except OSError as error:
            raise type(error)(
                error.errno,
                f"调试存储 list 失败: {reference}, error={type(error).__name__}, errno={error.errno}"
            ) from error
        configurations: list[NodeDebugConfigurationDTO] = []
        for path in paths:
            reference = f"session_id={session_id}, thread_id={thread_id}, artifact=configuration"
            payload = self._read_payload(path, reference)
            if payload is None:
                raise RuntimeError(f"调试方案枚举后文件缺失: {reference}")
            configuration = self._read_model_bytes(payload, NodeDebugConfigurationDTO, reference)
            expected_name = f"{configuration.configuration_id}.json"
            if path.name != expected_name:
                raise RuntimeError(
                    "调试方案文件名必须等于 configuration_id: "
                    f"{reference}, expected={expected_name}"
                )
            configurations.append(configuration)
        return configurations

    def read_registered_configuration_payloads(
        self,
        session_id: str,
        thread_id: str,
        configuration_ids: tuple[str, ...],
    ) -> dict[str, tuple[NodeDebugConfigurationDTO, bytes]]:
        """只按 manifest 已登记 ID 定点读取配置正文及 bytes。

        该方法禁止 glob/目录枚举；登记缺失、文件名漂移、symlink 或 DTO
        owner/ID 不一致都会直接失败，供 source capture 前后两次调用比较。
        """
        result: dict[str, tuple[NodeDebugConfigurationDTO, bytes]] = {}
        for configuration_id in configuration_ids:
            if configuration_id in result:
                raise RuntimeError(
                    f"source manifest 登记重复调试方案 ID: {configuration_id}"
                )
            path = self._configuration_path(session_id, thread_id, configuration_id)
            reference = f"session_id={session_id}, thread_id={thread_id}, configuration_id={configuration_id}"
            payload = self._read_payload(path, reference)
            if payload is None:
                raise RuntimeError(
                    "source manifest 登记的调试方案文件不存在: "
                    f"{reference}"
                )
            configuration = self._read_model_bytes(
                payload, NodeDebugConfigurationDTO, reference
            )
            if configuration.configuration_id != configuration_id:
                raise RuntimeError(
                    "调试方案文件内容与 manifest 登记不匹配: "
                    f"{reference}, expected={configuration_id}, "
                    f"actual={configuration.configuration_id}"
                )
            result[configuration_id] = (configuration, payload)
        return result

    def read_configuration(
        self,
        session_id: str,
        configuration_id: str,
        thread_id: str,
    ) -> NodeDebugConfigurationDTO | None:
        path = self._configuration_path(session_id, thread_id, configuration_id)
        reference = f"session_id={session_id}, thread_id={thread_id}, configuration_id={configuration_id}"
        payload = self._read_payload(path, reference)
        if payload is None:
            return None
        configuration = self._read_model_bytes(payload, NodeDebugConfigurationDTO, reference)
        if configuration.configuration_id != configuration_id:
            raise RuntimeError(
                "调试方案文件内容与文件名不匹配: "
                f"{reference}, actual={configuration.configuration_id}"
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
            f"session_id={session_id}, thread_id={thread_id}, configuration_id={configuration.configuration_id}",
        )

    def delete_configuration(
        self, session_id: str, configuration_id: str, thread_id: str
    ) -> None:
        self._assert_canonical_owner(session_id, thread_id)
        path = self._configuration_path(session_id, thread_id, configuration_id)
        try:
            path.unlink()
        except OSError as error:
            raise type(error)(
                error.errno,
                "调试存储 delete 失败: "
                f"session_id={session_id}, thread_id={thread_id}, configuration_id={configuration_id}, "
                f"error={type(error).__name__}, errno={error.errno}",
            ) from error

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
            raise ValueError(
                f"非法调试方案 ID: session_id={session_id}, thread_id={thread_id}, "
                "expected=dbgcfg_[0-9a-f]{32}"
            )
        return self._configurations_directory(session_id, thread_id) / f"{configuration_id}.json"

    @staticmethod
    def _read_payload(path: Path, reference: str) -> bytes | None:
        """所有调试读取共用文件边界；原生位置只留在异常链中。"""
        try:
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"调试存储必须是普通文件，不允许符号链接: {reference}")
            if not stat.S_ISREG(mode):
                raise RuntimeError(f"调试存储必须是普通文件: {reference}")
            return path.read_bytes()
        except OSError as error:
            raise type(error)(
                error.errno,
                f"调试存储 read 失败: {reference}, error={type(error).__name__}, errno={error.errno}"
            ) from error

    @staticmethod
    def _read_model_bytes(
        payload: bytes, model_type: type[ModelT], reference: str
    ) -> ModelT:
        try:
            return model_type.model_validate_json(payload)
        except ValidationError as error:
            # Pydantic 原文包含输入值和任意扩展字段，不能进入普通错误回执。
            failures = [
                {
                    "field": (
                        failure["loc"][0]
                        if failure["loc"] and failure["loc"][0] in model_type.model_fields
                        else "<record>"
                    ),
                    "type": failure["type"],
                }
                for failure in error.errors(include_input=False, include_context=False, include_url=False)
            ]
            raise RuntimeError(f"会话源码调试数据损坏: {reference}, failures={failures}") from error

    @staticmethod
    def _atomic_write(path: Path, model: BaseModel, reference: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(model.model_dump_json(indent=2), encoding="utf-8")
            os.replace(temporary, path)
        except OSError as error:
            raise type(error)(
                error.errno,
                f"调试存储 write 失败: {reference}, error={type(error).__name__}, errno={error.errno}",
            ) from error


__all__ = ["NodeDebugSessionStore", "SessionNodePathResolver"]
