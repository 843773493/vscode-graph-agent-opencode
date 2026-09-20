"""Node debug 可移植方案接入统一 fork materialization 的唯一实现。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.node_debug_fork import (
    NodeDebugSourceCopySnapshot,
    NodeDebugWorkspaceForkConfig,
    validate_target_prepublication,
)
from app.services.infrastructure.node_debug_thread_owner import (
    MAIN_THREAD_ID,
    resolve_node_debug_owner,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.service import (
        RolloutStorage,
    )


@dataclass(frozen=True, slots=True)
class PreparedNodeDebugFork:
    staging_root: Path
    target_node: Path
    target_manifest_sha256: str


def target_configuration_id(fork_id: str, source_configuration_id: str) -> str:
    digest = hashlib.sha256(
        f"{fork_id}\0{source_configuration_id}".encode()
    ).hexdigest()[:32]
    return f"dbgcfg_{digest}"


def target_staging_root(target_node: Path, materialization_id: str) -> Path:
    if len(materialization_id) != 32 or any(
        character not in "0123456789abcdef" for character in materialization_id
    ):
        raise ValueError("fork materialization_id 必须是 32 位小写十六进制")
    return target_node / ".fork-debug-staging" / materialization_id / "node"


def _stage_target_snapshot(
    snapshot: NodeDebugSourceCopySnapshot,
    *,
    fork_id: str,
    target_session_id: str,
    target_thread_id: str,
    staging_root: Path,
    expected_manifest_sha256: str,
) -> tuple[tuple[tuple[str, str], ...], str]:
    if staging_root.exists() or staging_root.is_symlink():
        raise FileExistsError(staging_root)
    configuration_id_map = tuple(
        (
            artifact.configuration_id,
            target_configuration_id(fork_id, artifact.configuration_id),
        )
        for artifact in snapshot.configuration_artifacts
    )
    staging_root.mkdir(parents=True)
    configurations_root = staging_root / "configurations"
    configurations_root.mkdir()
    mapping = dict(configuration_id_map)
    for artifact in snapshot.configuration_artifacts:
        configuration = artifact.configuration().model_copy(
            update={"configuration_id": mapping[artifact.configuration_id]}
        )
        target_path = configurations_root / f"{configuration.configuration_id}.json"
        temporary = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
        temporary.write_text(configuration.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, target_path)
        _fsync_file(target_path)
    source_manifest = NodeDebugSessionManifestDTO.model_validate_json(
        snapshot.manifest_bytes
    )
    target_manifest = source_manifest.model_copy(
        update={
            "session_id": target_session_id,
            "thread_id": target_thread_id,
            "active_configuration_id": None,
            "configuration_ids": tuple(mapping.values()),
            "actions": [],
            "updated_at": source_manifest.updated_at,
        }
    )
    manifest_path = staging_root / "manifest.json"
    temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    temporary.write_text(target_manifest.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, manifest_path)
    _fsync_file(manifest_path)
    _fsync_directory(configurations_root)
    _fsync_directory(staging_root)
    _fsync_directory(staging_root.parent)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if manifest_sha256 != expected_manifest_sha256:
        raise RuntimeError("fork debug staging manifest hash 与 prepared journal 不一致")
    return configuration_id_map, manifest_sha256


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _target_manifest_sha256(
    snapshot: NodeDebugSourceCopySnapshot,
    *,
    target_session_id: str,
    target_thread_id: str,
    configuration_id_map: tuple[tuple[str, str], ...],
) -> str:
    source_manifest = NodeDebugSessionManifestDTO.model_validate_json(
        snapshot.manifest_bytes
    )
    mapping = dict(configuration_id_map)
    target_manifest = source_manifest.model_copy(
        update={
            "session_id": target_session_id,
            "thread_id": target_thread_id,
            "active_configuration_id": None,
            "configuration_ids": tuple(mapping.values()),
            "actions": [],
            "updated_at": source_manifest.updated_at,
        }
    )
    return hashlib.sha256(
        target_manifest.model_dump_json(indent=2).encode()
    ).hexdigest()


def prepare_node_debug_fork(
    storage: RolloutStorage,
    snapshot: NodeDebugSourceCopySnapshot | None,
    workspace_config_provider: Callable[[], NodeDebugWorkspaceForkConfig] | None,
    *,
    materialization_id: str,
    fork_id: str,
    target_session_id: str,
    checkpoint_ns: str,
) -> PreparedNodeDebugFork | None:
    if snapshot is None or not snapshot.configuration_artifacts:
        return None
    if workspace_config_provider is None:
        raise RuntimeError("缺少 Workspace debug 配置 provider")
    workspace_config = workspace_config_provider()
    prepublication = validate_target_prepublication(
        snapshot,
        target_workspace_root=workspace_config.workspace_root,
        target_workspace_config_revision=workspace_config.revision,
        target_workspace_config_hash=workspace_config.content_hash,
        target_workspace_id=workspace_config.workspace_id,
        launch_profiles=workspace_config.launch_profiles,
        configuration_id_mapper=lambda source_id: target_configuration_id(
            fork_id, source_id
        ),
    )
    target_owner = resolve_node_debug_owner(
        storage._path_resolver,
        session_id=target_session_id,
        thread_id=MAIN_THREAD_ID,
    )
    publish_target_node = target_owner.thread_node
    staging_root = target_staging_root(
        storage.root(target_session_id, checkpoint_ns), materialization_id
    )
    configuration_map = prepublication.configuration_id_map
    manifest_sha256 = _target_manifest_sha256(
        snapshot,
        target_session_id=target_owner.session_id,
        target_thread_id=target_owner.thread_id,
        configuration_id_map=configuration_map,
    )
    storage.prepare_fork_debug_snapshot(
        materialization_id,
        fork_id,
        snapshot,
        prepublication,
        target_session_id=target_session_id,
        staging_relative_path=(
            Path("rollout") / ".fork-debug-staging" / materialization_id / "node"
        ),
        target_manifest_sha256=manifest_sha256,
        checkpoint_ns=checkpoint_ns,
    )
    staged_map, staged_manifest_sha256 = _stage_target_snapshot(
        snapshot,
        fork_id=fork_id,
        target_session_id=target_owner.session_id,
        target_thread_id=target_owner.thread_id,
        staging_root=staging_root,
        expected_manifest_sha256=manifest_sha256,
    )
    if staged_map != configuration_map or staged_manifest_sha256 != manifest_sha256:
        raise RuntimeError("fork debug staging 的 ID 映射与预发布校验不一致")
    return PreparedNodeDebugFork(
        staging_root=staging_root,
        target_node=publish_target_node,
        target_manifest_sha256=manifest_sha256,
    )


def publish_node_debug_fork(
    storage: RolloutStorage,
    snapshot: NodeDebugSourceCopySnapshot,
    prepared: PreparedNodeDebugFork,
    workspace_config_provider: Callable[[], NodeDebugWorkspaceForkConfig] | None,
    *,
    materialization_id: str,
    target_session_id: str,
    checkpoint_ns: str,
) -> None:
    ready_node_debug_fork(
        storage,
        snapshot,
        prepared,
        workspace_config_provider,
        materialization_id=materialization_id,
        target_session_id=target_session_id,
        checkpoint_ns=checkpoint_ns,
    )
    publish_ready_node_debug_fork(
        storage,
        prepared,
        materialization_id=materialization_id,
        target_session_id=target_session_id,
        checkpoint_ns=checkpoint_ns,
    )


def ready_node_debug_fork(
    storage: RolloutStorage,
    snapshot: NodeDebugSourceCopySnapshot,
    prepared: PreparedNodeDebugFork,
    workspace_config_provider: Callable[[], NodeDebugWorkspaceForkConfig] | None,
    *,
    materialization_id: str,
    target_session_id: str,
    checkpoint_ns: str,
) -> None:
    """发布前复核 Workspace 配置并把冻结 target proof 标为 ready。"""
    if workspace_config_provider is None:
        raise RuntimeError("缺少 Workspace debug 配置 provider")
    current = workspace_config_provider()
    validate_target_prepublication(
        snapshot,
        target_workspace_root=current.workspace_root,
        target_workspace_config_revision=current.revision,
        target_workspace_config_hash=current.content_hash,
        target_workspace_id=current.workspace_id,
        launch_profiles=current.launch_profiles,
    )
    storage.mark_fork_debug_snapshot_ready(
        materialization_id,
        target_session_id=target_session_id,
        checkpoint_ns=checkpoint_ns,
        target_manifest_sha256=prepared.target_manifest_sha256,
    )


def publish_ready_node_debug_fork(
    storage: RolloutStorage,
    prepared: PreparedNodeDebugFork,
    *,
    materialization_id: str,
    target_session_id: str,
    checkpoint_ns: str,
) -> None:
    """只消费已冻结 ready proof 发布 target，不再读取 source。"""
    publish_target_snapshot(prepared.staging_root, prepared.target_node)
    storage.mark_fork_debug_snapshot_published(
        materialization_id,
        target_session_id=target_session_id,
        checkpoint_ns=checkpoint_ns,
        target_manifest_sha256=prepared.target_manifest_sha256,
    )


def publish_target_snapshot(staging_root: Path, target_node: Path) -> Path:
    if not staging_root.is_dir() or staging_root.is_symlink():
        raise RuntimeError(f"Node debug staging 不存在或不安全: {staging_root}")
    expected_parent = target_node / "rollout" / ".fork-debug-staging"
    if not staging_root.resolve().is_relative_to(expected_parent.resolve()):
        raise RuntimeError("Node debug staging 不属于受检 target")
    debug_root = target_node / "debug"
    if debug_root.exists() or debug_root.is_symlink():
        raise RuntimeError(f"target 已存在 debug 目录，拒绝覆盖: {debug_root}")
    debug_root.mkdir()
    target_root = debug_root / "node"
    staging_root.rename(target_root)
    materialization_root = staging_root.parent
    if materialization_root.exists() and not any(materialization_root.iterdir()):
        materialization_root.rmdir()
    if expected_parent.exists() and not any(expected_parent.iterdir()):
        expected_parent.rmdir()
    return target_root


def remove_target_snapshot(
    staging_root: Path, target_node: Path, *, remove_published: bool
) -> None:
    expected_parent = target_node / "rollout" / ".fork-debug-staging"
    if not staging_root.resolve().is_relative_to(expected_parent.resolve()):
        raise RuntimeError("Node debug staging 不属于受检 target")
    if staging_root.exists() or staging_root.is_symlink():
        shutil.rmtree(staging_root)
    published_root = target_node / "debug" / "node"
    if remove_published and (published_root.exists() or published_root.is_symlink()):
        shutil.rmtree(published_root)
        debug_root = published_root.parent
        if debug_root.exists() and not any(debug_root.iterdir()):
            debug_root.rmdir()


def verify_published_target_snapshot(
    target_node: Path,
    *,
    target_session_id: str,
    target_thread_id: str,
    manifest_sha256: str,
    source_configurations_json: str,
    configuration_id_map_json: str,
) -> None:
    target_root = target_node / "debug" / "node"
    manifest_path = target_root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RuntimeError("fork target debug manifest 缺失或不安全")
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
        raise RuntimeError("fork target debug manifest 与 journal hash 不一致")
    manifest = NodeDebugSessionManifestDTO.model_validate_json(manifest_bytes)
    if (manifest.session_id, manifest.thread_id) != (
        target_session_id,
        target_thread_id,
    ):
        raise RuntimeError("fork target debug manifest owner 与 journal 不一致")
    if manifest.active_configuration_id is not None or manifest.actions:
        raise RuntimeError("fork target debug manifest 携带 active/actions")
    raw_configurations = json.loads(source_configurations_json)
    raw_mapping = json.loads(configuration_id_map_json)
    if not isinstance(raw_configurations, list) or not isinstance(raw_mapping, list):
        raise TypeError("fork debug journal 配置 proof 结构损坏")
    mapping = dict(raw_mapping)
    if len(mapping) != len(raw_mapping):
        raise RuntimeError("fork debug journal ID 映射重复")
    expected_ids: list[str] = []
    configurations_root = target_root / "configurations"
    for raw in raw_configurations:
        if not isinstance(raw, dict):
            raise TypeError("fork debug journal 配置 artifact 损坏")
        source_id = raw.get("configuration_id")
        payload_hex = raw.get("payload_bytes")
        size_bytes = raw.get("size_bytes")
        source_sha256 = raw.get("sha256")
        source_revision = raw.get("revision")
        target_id = mapping.get(source_id)
        if (
            not isinstance(source_id, str)
            or not isinstance(payload_hex, str)
            or type(size_bytes) is not int
            or not isinstance(source_sha256, str)
            or type(source_revision) is not int
            or not isinstance(target_id, str)
        ):
            raise TypeError("fork debug journal 配置 artifact 字段损坏")
        payload = bytes.fromhex(payload_hex)
        if len(payload) != size_bytes or hashlib.sha256(payload).hexdigest() != source_sha256:
            raise RuntimeError(f"fork debug journal 配置 artifact 完整性损坏: {source_id}")
        source_configuration = NodeDebugConfigurationDTO.model_validate_json(
            payload
        )
        if (
            source_configuration.configuration_id != source_id
            or source_configuration.revision != source_revision
        ):
            raise RuntimeError(f"fork debug journal 配置 artifact identity 损坏: {source_id}")
        expected = source_configuration.model_copy(
            update={"configuration_id": target_id}
        )
        target_path = configurations_root / f"{target_id}.json"
        if not target_path.is_file() or target_path.is_symlink():
            raise RuntimeError(f"fork target 调试方案缺失: {target_id}")
        actual = NodeDebugConfigurationDTO.model_validate_json(target_path.read_bytes())
        if actual != expected:
            raise RuntimeError(f"fork target 调试方案与 journal proof 不一致: {target_id}")
        expected_ids.append(target_id)
    if tuple(expected_ids) != manifest.configuration_ids:
        raise RuntimeError("fork target manifest configuration_ids 与 journal 映射不一致")


__all__ = [
    "PreparedNodeDebugFork",
    "prepare_node_debug_fork",
    "publish_node_debug_fork",
    "publish_ready_node_debug_fork",
    "ready_node_debug_fork",
    "remove_target_snapshot",
    "verify_published_target_snapshot",
]
