"""Node 调试方案 source capture 与 fork target 预发布边界。

本模块只冻结可移植方案正文及其完整性证据。它不复制运行时、Inspector
连接、端口、launch claim、active 指针或动作审计，也不发布 target Session。
source capture 由 ``NodeDebugSessionStore`` 按 manifest 登记的方案 ID 定点读取；
读前后任何 manifest/方案 bytes 或 revision 漂移都会 fail-closed。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from app.core.exceptions import ForbiddenError
from app.core.hashing import sha256_hex as _raw_sha256
from app.core.resource_uri import parse_session_thread_uri
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugLaunchProfileDTO,
)
from app.services.infrastructure.node_debug.configuration.configuration_factory import (
    NodeDebugConfigurationFactory,
)
from app.services.infrastructure.node_debug.configuration.runtime_config import (
    parse_launch_profile_configs,
)
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.node_debug.session.thread_owner import (
    resolve_node_debug_owner,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths


class NodeDebugSourceCaptureError(RuntimeError):
    """source 调试方案不能形成一致、可验证快照。"""


class NodeDebugSourceDriftError(NodeDebugSourceCaptureError):
    """source capture 读前后 revision/bytes 发生漂移。"""


class NodeDebugTargetValidationError(RuntimeError):
    """target staging 中的方案或 Workspace debug 配置无法通过预发布校验。"""


@dataclass(frozen=True, slots=True)
class NodeDebugWorkspaceForkConfig:
    """公开 fork capture/发布共同冻结的 Workspace debug 有效配置。"""

    workspace_root: Path
    workspace_id: str
    revision: str
    content_hash: str
    launch_profiles: Mapping[str, NodeDebugLaunchProfileDTO]


def build_workspace_fork_config(
    workspace_root: Path, debug_config: Mapping[str, object]
) -> NodeDebugWorkspaceForkConfig:
    canonical = json.dumps(
        debug_config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    revision = hashlib.sha256(canonical).hexdigest()
    raw_profiles = debug_config.get("launch_profiles")
    if not isinstance(raw_profiles, Mapping):
        raise TypeError("runtime.debug.launch_profiles 必须是 mapping")
    parsed_profiles = parse_launch_profile_configs(raw_profiles, fill_defaults=True)
    profiles: dict[str, NodeDebugLaunchProfileDTO] = {}
    for name, profile in parsed_profiles.items():
        profiles[name] = NodeDebugLaunchProfileDTO(
            name=name,
            adapter=profile.adapter,
            runtime=profile.runtime,
            supported=(profile.adapter == "node_inspector" and profile.runtime == "node"),
            program=profile.program,
            working_directory=profile.working_directory,
            args=list(profile.args),
        )
    return NodeDebugWorkspaceForkConfig(
        workspace_root=workspace_root.resolve(),
        workspace_id=load_or_create_workspace_id(workspace_root),
        revision=revision,
        content_hash=f"sha256:{revision}",
        launch_profiles=profiles,
    )


@dataclass(frozen=True, slots=True)
class NodeDebugConfigurationCopyArtifact:
    """单个可移植方案正文的 immutable source artifact。

    ``payload_bytes`` 是原始 JSON bytes，target 必须从它重新校验 DTO；不把
    source owner 写进方案正文。``source_lineage`` 只保留审计坐标，不参与运行时
    owner 解析。
    """

    configuration_id: str
    revision: int
    payload_bytes: bytes
    size_bytes: int
    sha256: str
    source_lineage: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.configuration_id:
            raise ValueError("调试方案 artifact 缺少 configuration_id")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("调试方案 artifact revision 必须是正整数")
        if type(self.payload_bytes) is not bytes:
            raise TypeError("调试方案 artifact payload_bytes 必须是 bytes")
        if self.size_bytes != len(self.payload_bytes):
            raise ValueError("调试方案 artifact size_bytes 与正文长度不一致")
        if self.sha256 != hashlib.sha256(self.payload_bytes).hexdigest():
            raise ValueError("调试方案 artifact sha256 与正文不一致")
        if any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(value, str) and value for value in item)
            for item in self.source_lineage
        ):
            raise ValueError("调试方案 artifact source_lineage 必须是非空字符串元组")

    def configuration(self) -> NodeDebugConfigurationDTO:
        """从冻结正文解析 portable DTO；正文损坏时直接失败。"""
        try:
            configuration = NodeDebugConfigurationDTO.model_validate_json(
                self.payload_bytes
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise NodeDebugSourceCaptureError(
                f"调试方案 artifact 正文损坏: configuration_id={self.configuration_id}"
            ) from error
        if configuration.configuration_id != self.configuration_id:
            raise NodeDebugSourceCaptureError(
                "调试方案 artifact ID 与正文不一致: "
                f"artifact={self.configuration_id}, "
                f"payload={configuration.configuration_id}"
            )
        if configuration.revision != self.revision:
            raise NodeDebugSourceCaptureError(
                "调试方案 artifact revision 与正文不一致: "
                f"configuration_id={self.configuration_id}"
            )
        return configuration


@dataclass(frozen=True, slots=True)
class NodeDebugSourceCopySnapshot:
    """一次 source debug capture 的不可变 manifest/artifact 清单。"""

    source_snapshot_id: str
    source_session_id: str
    source_thread_id: str
    manifest_bytes: bytes
    manifest_size_bytes: int
    manifest_sha256: str
    manifest_revision: str
    configuration_artifacts: tuple[NodeDebugConfigurationCopyArtifact, ...] = ()
    source_lineage: tuple[tuple[str, str], ...] = ()
    workspace_config_revision: str | None = None
    workspace_config_hash: str | None = None
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        if not self.source_snapshot_id:
            raise ValueError("NodeDebugSourceCopySnapshot 缺少 source_snapshot_id")
        if not self.source_session_id or not self.source_thread_id:
            raise ValueError("NodeDebugSourceCopySnapshot 缺少 source owner")
        if type(self.manifest_bytes) is not bytes:
            raise TypeError("NodeDebugSourceCopySnapshot manifest_bytes 必须是 bytes")
        if self.manifest_size_bytes != len(self.manifest_bytes):
            raise ValueError("source manifest size_bytes 与正文长度不一致")
        if self.manifest_sha256 != hashlib.sha256(self.manifest_bytes).hexdigest():
            raise ValueError("source manifest sha256 与正文不一致")
        if self.manifest_revision != self.manifest_sha256:
            raise ValueError("source manifest_revision 必须等于 manifest_sha256")
        ids = [item.configuration_id for item in self.configuration_artifacts]
        if len(ids) != len(set(ids)):
            raise ValueError("source snapshot 不得包含重复调试方案")
        if self.workspace_config_revision is None and self.workspace_config_hash is not None:
            raise ValueError("workspace_config_hash 不能脱离 workspace_config_revision")
        if self.workspace_config_revision is not None and not self.workspace_config_revision:
            raise ValueError("workspace_config_revision 不能为空")
        if self.workspace_config_hash is not None and not self.workspace_config_hash:
            raise ValueError("workspace_config_hash 不能为空")
        if self.workspace_id is not None and not self.workspace_id:
            raise ValueError("workspace_id 不能为空")
        if any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(value, str) and value for value in item)
            for item in self.source_lineage
        ):
            raise ValueError("source snapshot source_lineage 必须是非空字符串元组")

    @property
    def active_configuration_id(self) -> None:
        """source snapshot 永不携带可执行 active pointer。"""
        return None

    def to_payload(self) -> dict[str, object]:
        """冻结 marker 与目标专属 journal 共用同一静态资源 wire 合同。"""
        return {
            "source_snapshot_id": self.source_snapshot_id,
            "source_session_id": self.source_session_id,
            "source_thread_id": self.source_thread_id,
            "manifest_bytes": self.manifest_bytes.hex(),
            "manifest_size_bytes": self.manifest_size_bytes,
            "manifest_sha256": self.manifest_sha256,
            "manifest_revision": self.manifest_revision,
            "configuration_artifacts": [
                {"configuration_id": artifact.configuration_id, "revision": artifact.revision,
                 "payload_bytes": artifact.payload_bytes.hex(), "size_bytes": artifact.size_bytes,
                 "sha256": artifact.sha256, "source_lineage": artifact.source_lineage}
                for artifact in self.configuration_artifacts
            ],
            "source_lineage": self.source_lineage,
            "workspace_config_revision": self.workspace_config_revision,
            "workspace_config_hash": self.workspace_config_hash,
            "workspace_id": self.workspace_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> NodeDebugSourceCopySnapshot:
        values = dict(payload)
        values["manifest_bytes"] = bytes.fromhex(values["manifest_bytes"])
        values["source_lineage"] = tuple(tuple(item) for item in values["source_lineage"])
        artifacts = []
        for raw in values["configuration_artifacts"]:
            artifact = dict(raw)
            artifact["payload_bytes"] = bytes.fromhex(artifact["payload_bytes"])
            artifact["source_lineage"] = tuple(tuple(item) for item in artifact["source_lineage"])
            artifacts.append(NodeDebugConfigurationCopyArtifact(**artifact))
        values["configuration_artifacts"] = tuple(artifacts)
        return cls(**values)


@dataclass(frozen=True, slots=True)
class NodeDebugTargetPrepublication:
    """target staging 通过校验后可交给 materialization 的只读结果。

    该结果没有 active pointer；``configuration_id_map`` 只描述待发布的
    source→target-local identity，不执行写入。
    """

    source_snapshot_id: str
    configuration_id_map: tuple[tuple[str, str], ...]
    validated_configuration_ids: tuple[str, ...]
    target_workspace_config_revision: str
    target_workspace_config_hash: str | None

    @property
    def active_configuration_id(self) -> None:
        return None


def _snapshot_id(
    *,
    source_session_id: str,
    source_thread_id: str,
    manifest_sha256: str,
    configuration_artifacts: tuple[NodeDebugConfigurationCopyArtifact, ...],
) -> str:
    digest = hashlib.sha256()
    digest.update(source_session_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(source_thread_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(manifest_sha256.encode("ascii"))
    for artifact in configuration_artifacts:
        digest.update(artifact.configuration_id.encode("ascii"))
        digest.update(artifact.sha256.encode("ascii"))
    return f"node-debug-source-snapshot_{digest.hexdigest()}"


def debug_configuration_resource_uri(thread_uri: str, configuration_id: str) -> str:
    """方案地址由已解析的精确 Thread 与 owner 登记 ID 组成。"""
    if parse_session_thread_uri(thread_uri)[2] is None:
        raise ValueError("调试方案地址必须绑定精确 Thread")
    if re.fullmatch(r"dbgcfg_[0-9a-f]{32}", configuration_id) is None:
        raise ValueError(f"非法调试方案 ID: {configuration_id}")
    return f"{thread_uri}/debug/node/configurations/{configuration_id}"


def capture_source_copy_snapshot(
    store: NodeDebugSessionStore,
    *,
    session_id: str,
    thread_id: str,
    configuration_ids: tuple[str, ...],
    workspace_config_revision: str | None = None,
    workspace_config_hash: str | None = None,
    workspace_id: str | None = None,
    source_lineage: tuple[tuple[str, str], ...] = (),
) -> NodeDebugSourceCopySnapshot:
    """在 source store 上执行一次 manifest/方案 bytes 前后漂移校验。

    方案枚举只使用 manifest.configuration_ids。该方法不会读取 launch claim、
    manifest active/actions 之外的运行状态，也不会扫描目录补齐未登记方案。
    """

    owner = resolve_node_debug_owner(
        store.path_resolver,
        session_id=session_id,
        thread_id=thread_id,
    )
    canonical_session_id, canonical_thread_id = owner.key
    manifest_before_bytes, manifest_before = store.read_manifest_payload(
        canonical_session_id, canonical_thread_id
    )
    if manifest_before is None or manifest_before_bytes is None:
        raise NodeDebugSourceCaptureError(
            "source debug manifest 不存在，不能形成可审计 snapshot: "
            f"session_id={canonical_session_id}, thread_id={canonical_thread_id}"
        )
    registered_ids = tuple(manifest_before.configuration_ids)
    if len(registered_ids) != len(set(registered_ids)):
        raise NodeDebugSourceCaptureError("source manifest 登记了重复调试方案 ID")
    if not configuration_ids or len(configuration_ids) != len(set(configuration_ids)):
        raise ValueError("debug capture 必须显式选择非空、无重复方案清单")
    if missing := set(configuration_ids) - set(registered_ids):
        raise NodeDebugSourceCaptureError(f"source manifest 未登记所选调试方案: {sorted(missing)}")
    selected_ids = configuration_ids
    before = store.read_registered_configuration_payloads(
        canonical_session_id,
        canonical_thread_id,
        selected_ids,
    )
    manifest_after_bytes, manifest_after = store.read_manifest_payload(
        canonical_session_id, canonical_thread_id
    )
    if manifest_after is None or manifest_after_bytes is None:
        raise NodeDebugSourceDriftError("source manifest 在 capture 期间消失")
    if manifest_after_bytes != manifest_before_bytes:
        raise NodeDebugSourceDriftError("source manifest 在 capture 前后发生漂移")
    after = store.read_registered_configuration_payloads(
        canonical_session_id,
        canonical_thread_id,
        selected_ids,
    )
    if before != after:
        raise NodeDebugSourceDriftError("source 调试方案在 capture 前后发生 bytes/revision 漂移")
    artifacts = tuple(
        NodeDebugConfigurationCopyArtifact(
            configuration_id=configuration_id,
            revision=configuration.revision,
            payload_bytes=payload,
            size_bytes=len(payload),
            sha256=_raw_sha256(payload),
            source_lineage=(
                ("source_session_id", canonical_session_id),
                ("source_thread_id", canonical_thread_id),
                ("source_configuration_id", configuration_id),
                ("source_revision", str(configuration.revision)),
            ),
        )
        for configuration_id, (configuration, payload) in before.items()
    )
    manifest_sha256 = _raw_sha256(manifest_before_bytes)
    return NodeDebugSourceCopySnapshot(
        source_snapshot_id=_snapshot_id(
            source_session_id=canonical_session_id,
            source_thread_id=canonical_thread_id,
            manifest_sha256=manifest_sha256,
            configuration_artifacts=artifacts,
        ),
        source_session_id=canonical_session_id,
        source_thread_id=canonical_thread_id,
        manifest_bytes=manifest_before_bytes,
        manifest_size_bytes=len(manifest_before_bytes),
        manifest_sha256=manifest_sha256,
        manifest_revision=manifest_sha256,
        configuration_artifacts=artifacts,
        source_lineage=source_lineage
        or (
            ("source_session_id", canonical_session_id),
            ("source_thread_id", canonical_thread_id),
        ),
        workspace_config_revision=workspace_config_revision,
        workspace_config_hash=workspace_config_hash,
        workspace_id=workspace_id,
    )


def _target_path(
    factory: NodeDebugConfigurationFactory, value: str, *, field: str,
) -> Path:
    try:
        return factory.resolve_workspace_path(
            value, script=field != "working_directory",
        )[0]
    except (ValueError, ForbiddenError, RuntimeError) as error:
        raise NodeDebugTargetValidationError(
            f"target {field} 无法在目标 workspace 解析: {error}"
        ) from error


def validate_target_prepublication(
    snapshot: NodeDebugSourceCopySnapshot,
    *,
    target_workspace_root: Path,
    target_workspace_config_revision: str,
    target_workspace_config_hash: str | None,
    target_workspace_id: str,
    launch_profiles: Mapping[str, NodeDebugLaunchProfileDTO],
    configuration_id_mapper: Callable[[str], str] | None = None,
) -> NodeDebugTargetPrepublication:
    """在 target 不可见 staging 中重验配置；失败时整个操作 fail-closed。"""

    same_workspace = snapshot.workspace_id is not None and snapshot.workspace_id == target_workspace_id
    if same_workspace and snapshot.workspace_config_revision is not None and (
        target_workspace_config_revision != snapshot.workspace_config_revision
    ):
        raise NodeDebugTargetValidationError("target Workspace debug 配置 revision 已漂移")
    if same_workspace and snapshot.workspace_config_hash is not None and (
        target_workspace_config_hash != snapshot.workspace_config_hash
    ):
        raise NodeDebugTargetValidationError("target Workspace debug 配置 hash 已漂移")
    root = target_workspace_root.resolve()
    factory = NodeDebugConfigurationFactory(
        file_paths=WorkspaceFilePaths(
            workspace=root, workspace_id=target_workspace_id, bindings=None,
        ),
    )
    mapped: list[tuple[str, str]] = []
    validated: list[str] = []
    for artifact in snapshot.configuration_artifacts:
        configuration = artifact.configuration()
        profile: NodeDebugLaunchProfileDTO | None = None
        profile_name = configuration.launch_profile_name
        if profile_name is not None:
            profile = launch_profiles.get(profile_name)
            if profile is None:
                raise NodeDebugTargetValidationError(
                    f"target launch profile 不存在: {profile_name}"
                )
            if (
                not profile.supported
                or profile.adapter != "node_inspector"
                or profile.runtime != "node"
            ):
                raise NodeDebugTargetValidationError(
                    f"target launch profile 不支持 Node Inspector: {profile_name}"
                )
        script_value = configuration.script_path or (
            profile.program if profile is not None else ""
        )
        if not script_value:
            raise NodeDebugTargetValidationError("target 调试方案缺少有效入口")
        script = _target_path(factory, script_value, field="script_path")
        if not script.is_file():
            raise NodeDebugTargetValidationError(
                f"target 调试入口不存在: {script_value}"
            )
        working_directory_value = configuration.working_directory or (
            profile.working_directory if profile is not None else ""
        )
        working_directory = _target_path(
            factory, working_directory_value, field="working_directory"
        )
        if not working_directory.is_dir():
            raise NodeDebugTargetValidationError(
                f"target 调试工作目录不存在: {working_directory_value}"
            )
        for breakpoint in configuration.breakpoints:
            path = _target_path(factory, breakpoint.path, field="breakpoint.path")
            if not path.is_file():
                raise NodeDebugTargetValidationError(
                    f"target 断点路径不存在: {breakpoint.path}"
                )
        target_id = (
            configuration_id_mapper(artifact.configuration_id)
            if configuration_id_mapper is not None
            else artifact.configuration_id
        )
        if not target_id:
            raise NodeDebugTargetValidationError(
                f"target 调试方案映射 ID 为空: {artifact.configuration_id}"
            )
        mapped.append((artifact.configuration_id, target_id))
        validated.append(artifact.configuration_id)
    if len({target for _, target in mapped}) != len(mapped):
        raise NodeDebugTargetValidationError("target 调试方案映射 ID 重复")
    return NodeDebugTargetPrepublication(
        source_snapshot_id=snapshot.source_snapshot_id,
        configuration_id_map=tuple(mapped),
        validated_configuration_ids=tuple(validated),
        target_workspace_config_revision=target_workspace_config_revision,
        target_workspace_config_hash=target_workspace_config_hash,
    )


__all__ = [
    "NodeDebugConfigurationCopyArtifact",
    "NodeDebugSourceCaptureError",
    "NodeDebugSourceCopySnapshot",
    "NodeDebugSourceDriftError",
    "NodeDebugTargetPrepublication",
    "NodeDebugTargetValidationError",
    "NodeDebugWorkspaceForkConfig",
    "build_workspace_fork_config",
    "capture_source_copy_snapshot",
    "debug_configuration_resource_uri",
    "validate_target_prepublication",
]
