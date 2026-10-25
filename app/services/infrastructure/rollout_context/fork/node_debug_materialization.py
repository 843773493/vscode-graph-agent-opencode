"""Node debug 可移植方案接入统一 fork materialization 的唯一实现。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.atomic_fs import fsync_directory, fsync_file
from app.domain.itemized.hashing import canonical_json_bytes
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.node_debug.session.fork import (
    NodeDebugSourceCopySnapshot,
    NodeDebugWorkspaceForkConfig,
    validate_target_prepublication,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    _storage_failure,
    _storage_io,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.store import (
        RolloutContextStore,
    )


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
    with _storage_io(staging_root, "fork debug 文件操作失败", "staging_inspection", artifact="fork_debug"):
        exists = staging_root.exists() or staging_root.is_symlink()
    if exists:
        raise FileExistsError(_storage_failure(staging_root, "fork debug staging 已存在", "staging", artifact="fork_debug"))
    with _storage_io(staging_root, "fork debug 文件操作失败", "staging", artifact="fork_debug"):
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
            fsync_file(target_path)
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
        fsync_file(manifest_path)
        fsync_directory(configurations_root)
        fsync_directory(staging_root)
        fsync_directory(staging_root.parent)
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if manifest_sha256 != expected_manifest_sha256:
            raise RuntimeError("fork debug staging manifest hash 与 prepared journal 不一致")
        return configuration_id_map, manifest_sha256




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


def materialize_node_debug_fork(
    storage: RolloutContextStore, snapshot: NodeDebugSourceCopySnapshot | None,
    workspace_config_provider: Callable[[], NodeDebugWorkspaceForkConfig] | None,
    *, materialization_id: str, fork_id: str, target_session_id: str,
    target_thread_id: str, checkpoint_ns: str,
) -> None:
    """显式资源与重试共用 prepared→ready→published；恢复不读取 live source。"""
    if snapshot is None:
        return
    if not snapshot.configuration_artifacts:
        raise RuntimeError("显式 debug snapshot 缺少所选方案")
    target_node = storage.storage.fork_paths.thread_node(target_session_id, target_thread_id)
    staging_root = target_staging_root(storage.storage.root(
        target_session_id, checkpoint_ns, thread_id=target_thread_id,
    ), materialization_id)
    existing = storage.forks.read_fork_debug_snapshot(
        materialization_id, target_session_id=target_session_id,
        target_thread_id=target_thread_id, checkpoint_ns=checkpoint_ns,
    )
    if existing is None:
        if workspace_config_provider is None:
            raise RuntimeError("缺少 Workspace debug 配置 provider")
        config = workspace_config_provider()
        prepublication = validate_target_prepublication(
            snapshot, target_workspace_root=config.workspace_root,
            target_workspace_config_revision=config.revision,
            target_workspace_config_hash=config.content_hash,
            target_workspace_id=config.workspace_id, launch_profiles=config.launch_profiles,
            configuration_id_mapper=lambda source_id: target_configuration_id(fork_id, source_id),
        )
        manifest_sha256 = _target_manifest_sha256(snapshot,
            target_session_id=target_session_id, target_thread_id=target_thread_id,
            configuration_id_map=prepublication.configuration_id_map,
        )
        storage.forks.prepare_fork_debug_snapshot(
            materialization_id, fork_id, snapshot, prepublication,
            target_session_id=target_session_id, target_thread_id=target_thread_id,
            target_manifest_sha256=manifest_sha256, checkpoint_ns=checkpoint_ns,
        )
        existing = storage.forks.read_fork_debug_snapshot(
            materialization_id, target_session_id=target_session_id,
            target_thread_id=target_thread_id, checkpoint_ns=checkpoint_ns,
        )
    if existing is None:
        raise RuntimeError("fork debug prepared journal 未持久化")
    state, proof = existing
    if proof["fork_id"] != fork_id or canonical_json_bytes(proof["snapshot"]) != canonical_json_bytes(snapshot.to_payload()):
        raise RuntimeError("fork debug 重试与冻结方案 proof 不一致")
    manifest_sha256 = proof["target_manifest_sha256"]
    if state == "prepared":
        if workspace_config_provider is None:
            raise RuntimeError("缺少 Workspace debug 配置 provider")
        config = workspace_config_provider()
        if (config.revision, config.content_hash) != (
            proof["target_workspace_config_revision"], proof["target_workspace_config_hash"],
        ):
            raise RuntimeError("target Workspace debug 配置在预发布期间漂移")
        validate_target_prepublication(
            snapshot, target_workspace_root=config.workspace_root,
            target_workspace_config_revision=config.revision,
            target_workspace_config_hash=config.content_hash,
            target_workspace_id=config.workspace_id, launch_profiles=config.launch_profiles,
        )
        with _storage_io(staging_root, "fork debug 文件操作失败", "staging_cleanup", artifact="fork_debug"):
            if staging_root.is_symlink():
                raise RuntimeError("fork debug staging 是符号链接")
            if staging_root.exists():
                shutil.rmtree(staging_root)
        _stage_target_snapshot(snapshot, fork_id=fork_id, target_session_id=target_session_id,
            target_thread_id=target_thread_id, staging_root=staging_root,
            expected_manifest_sha256=manifest_sha256,
        )
        storage.forks.advance_fork_debug_snapshot(
            materialization_id, target_session_id=target_session_id,
            target_thread_id=target_thread_id, checkpoint_ns=checkpoint_ns, state="ready",
        )
        state = "ready"
    target_root = target_node / "debug" / "node"
    if state == "ready":
        if target_root.exists():
            if staging_root.exists():
                raise RuntimeError("fork debug 同时存在 staging 和已发布正文")
        else:
            publish_target_snapshot(staging_root, target_node)
    elif state != "published":
        raise RuntimeError(f"fork debug journal 状态非法: {state}")
    verify_published_target_snapshot(
        target_node, target_session_id=target_session_id, target_thread_id=target_thread_id,
        manifest_sha256=manifest_sha256,
        source_configurations_json=json.dumps(proof["snapshot"]["configuration_artifacts"]),
        configuration_id_map_json=json.dumps(proof["target_configuration_id_map"]),
    )
    storage.forks.advance_fork_debug_snapshot(
        materialization_id, target_session_id=target_session_id,
        target_thread_id=target_thread_id, checkpoint_ns=checkpoint_ns, state="published",
    )


def publish_target_snapshot(staging_root: Path, target_node: Path) -> Path:
    with _storage_io(staging_root, "fork debug 文件操作失败", "publication", artifact="fork_debug"):
        if not staging_root.is_dir() or staging_root.is_symlink():
            raise RuntimeError(_storage_failure(staging_root, "Node debug staging 不存在或不安全", "publication", artifact="fork_debug"))
        expected_parent = target_node / "rollout" / ".fork-debug-staging"
        if not staging_root.resolve().is_relative_to(expected_parent.resolve()):
            raise RuntimeError("Node debug staging 不属于受检 target")
        debug_root = target_node / "debug"
        if debug_root.is_symlink() or (debug_root.exists() and not debug_root.is_dir()):
            raise RuntimeError(_storage_failure(debug_root, "target debug 父目录不是普通目录", "publication", artifact="fork_debug"))
        if debug_root.exists() and any(debug_root.iterdir()):
            raise RuntimeError(_storage_failure(debug_root, "target 已存在 debug 正文，拒绝覆盖", "publication", artifact="fork_debug"))
        # ready proof 重放允许补完 mkdir 与 rename 之间留下的精确空父目录。
        debug_root.mkdir(exist_ok=True)
        target_root = debug_root / "node"
        staging_root.rename(target_root)
        fsync_directory(debug_root)
        fsync_directory(target_node)
        materialization_root = staging_root.parent
        if materialization_root.exists() and not any(materialization_root.iterdir()):
            materialization_root.rmdir()
        if expected_parent.exists() and not any(expected_parent.iterdir()):
            expected_parent.rmdir()
        return target_root


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
    with _storage_io(target_root, "fork debug 文件操作失败", "verification", artifact="fork_debug"):
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


__all__ = ["materialize_node_debug_fork", "verify_published_target_snapshot"]
