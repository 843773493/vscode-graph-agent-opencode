"""旧 Session ``debug/node/`` 目录的显式迁移（调试域维护步骤）。

OpenSpec ``add-agent-debug-tool-group`` 任务 2.5 / itemized 8.2-A：R3a 把
``NodeDebugSessionStore`` 收紧为精确 ``(session_id, thread_id)`` 归属后，旧格式
manifest（无 ``thread_id``）对新 store 不可读。本模块提供**显式维护操作**把这些
旧数据定点迁移到 main thread（``thread_id="main"``），不接入正常 runtime 路径：
正常 runtime 继续只读新格式，旧格式由本步骤一次性升级。

职责边界：
- 调试 domain 拥有方案语义（manifest/方案校验、hash 登记、staging 原子切换）；
  itemized（8.2-A）拥有共享 maintenance gate/journal/发布门槛。本模块通过
  ``NodeDebugLegacyMigrationSessionIndex`` 最小 Protocol 与目录索引解耦，未来
  共享 gate 接入时只替换 journal 与外层编排，不改动本模块的调试域语义。
- 只做纯文件操作：不触碰任何 Node/Inspector 进程或端口，不建立第二迁移
  协调器，不新增 runtime 旧路径 alias，不扫盘（索引外目录一律不触碰）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError

from app.core.session_tree.support import SessionPhysicalNode
from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationDTO,
    NodeDebugSessionManifestDTO,
)
from app.services.infrastructure.node_debug_thread_owner import MAIN_THREAD_ID

#: journal 结构版本；结构不兼容时必须显式报错而不是重置台账。
JOURNAL_SCHEMA_VERSION = 1

#: journal 中标识本次迁移语义的固定名称。
MIGRATION_NAME = "node-debug-legacy-main-thread"

MANIFEST_FILE_NAME = "manifest.json"
CONFIGURATIONS_DIRECTORY_NAME = "configurations"
CONFIGURATION_ID_PATTERN = re.compile(r"^dbgcfg_[0-9a-f]{32}$")

NodeDebugLegacyMigrationStatus = Literal[
    "pending",
    "migrated",
    "failed",
    "skipped",
]


class NodeDebugLegacyMigrationSessionIndex(Protocol):
    """迁移枚举所需的最小权威目录索引能力（生产由 ``SessionPathResolver`` 实现）。

    itemized 8.2-A 拥有共享 maintenance gate/journal/发布门槛；本模块只拥有
    调试域迁移步骤，因此只依赖这份最小缝：枚举必须来自权威索引投影，
    物理路径必须来自受检解析，任何实现都不得扫盘。
    """

    def list_authoritative_nodes(self) -> list[SessionPhysicalNode]: ...

    def resolve_session_node(self, session_id: str) -> Path: ...


@dataclass(frozen=True, slots=True)
class NodeDebugLegacyFileDigest:
    """迁移台账中的单文件证据：文件名 + bytes + sha256。"""

    file: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class NodeDebugLegacyConfigurationDigest(NodeDebugLegacyFileDigest):
    """方案文件证据：在文件 hash 之上登记方案 ID 与 revision。"""

    configuration_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class NodeDebugLegacyMigrationSummary:
    """一次 ``run`` 的会话级结果汇总。"""

    migrated: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    noop: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()


def default_journal_path(boxteam_root: Path) -> Path:
    """返回工作区 ``.boxteam/`` 下语义明确的迁移台账默认位置。"""
    return (
        boxteam_root
        / "maintenance"
        / "node-debug-legacy-migration"
        / "journal.json"
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _fsync_directory(directory: Path) -> None:
    """fsync 目录项，保证 rename 后的持久性（Linux 上目录可以打开 fsync）。"""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """staging 写入 + fsync + 原子 rename（与 store 的原子写语义对齐）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with staging.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staging, path)
    _fsync_directory(path.parent)


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest_file(path: Path) -> NodeDebugLegacyFileDigest:
    payload = path.read_bytes()
    return NodeDebugLegacyFileDigest(
        file=path.name,
        size_bytes=len(payload),
        sha256=_digest_bytes(payload),
    )


def _digest_dict(
    digest: NodeDebugLegacyFileDigest | NodeDebugLegacyConfigurationDigest,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "file": digest.file,
        "size_bytes": digest.size_bytes,
        "sha256": digest.sha256,
    }
    if isinstance(digest, NodeDebugLegacyConfigurationDigest):
        payload["configuration_id"] = digest.configuration_id
        payload["revision"] = digest.revision
    return payload


def _fail(session_id: str, stage: str, detail: str) -> str:
    """构造带会话、阶段与明细的失败原因文本（journal 与异常共用）。"""
    return f"session={session_id}, stage={stage}: {detail}"


def _session_debug_directory(session_node: Path) -> Path:
    """main thread 的调试目录即会话节点自身的 ``debug/node/``（路径不变，不搬目录）。"""
    return session_node / "debug" / "node"


class NodeDebugLegacyMigrationJournal:
    """旧 ``debug/node/`` 迁移台账：每会话一条记录，原子持久化，可跨进程重读。

    重跑语义：``migrated`` 直接跳过（journal 不重复记录）；``failed`` 可重试并
    覆盖最新结果；``skipped`` 保持既有记录不重写；``pending``/缺失记录按待处理
    检测。
    """

    def __init__(self, journal_path: Path) -> None:
        self._journal_path = journal_path
        self._records: dict[str, dict[str, object]] = {}
        self._dirty = False
        self._loaded = False

    @property
    def journal_path(self) -> Path:
        return self._journal_path

    def load(self) -> None:
        """加载既有台账；结构不兼容时显式报错，绝不静默重置。"""
        if not self._journal_path.exists():
            self._records = {}
            self._loaded = True
            self._dirty = False
            return
        raw = json.loads(self._journal_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise RuntimeError(  # noqa: TRY004 - 数据损坏语义对齐 store，不用 TypeError
                f"调试迁移台账必须是 JSON object: {self._journal_path}"
            )
        if raw.get("schema_version") != JOURNAL_SCHEMA_VERSION:
            raise RuntimeError(
                "调试迁移台账 schema 版本不兼容，拒绝重置或静默升级: "
                f"path={self._journal_path}, "
                f"schema_version={raw.get('schema_version')!r}"
            )
        if raw.get("migration") != MIGRATION_NAME:
            raise RuntimeError(
                "调试迁移台账 migration 名称不匹配: "
                f"path={self._journal_path}, expected={MIGRATION_NAME}"
            )
        records = raw.get("records")
        if not isinstance(records, dict):
            raise RuntimeError(  # noqa: TRY004 - 数据损坏语义对齐 store，不用 TypeError
                f"调试迁移台账缺少 records 映射: {self._journal_path}"
            )
        self._records = records
        self._loaded = True
        self._dirty = False

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def get_record(self, session_id: str) -> dict[str, object] | None:
        self._ensure_loaded()
        record = self._records.get(session_id)
        return dict(record) if isinstance(record, dict) else None

    def upsert_record(self, session_id: str, record: dict[str, object]) -> None:
        """写入会话记录（仅内存）；状态真正变化时由 :meth:`save` 原子落盘。"""
        self._ensure_loaded()
        self._records[session_id] = record
        self._dirty = True

    def save(self) -> None:
        """只有存在状态变化时才重写台账文件，保证幂等重跑 bytes 不变。"""
        self._ensure_loaded()
        if not self._dirty:
            return
        payload = json.dumps(
            {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "migration": MIGRATION_NAME,
                "records": self._records,
                "updated_at": _utc_now_iso(),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        _atomic_write_bytes(self._journal_path, payload.encode("utf-8"))
        self._dirty = False


class NodeDebugLegacyDirectoryMigrator:
    """把旧格式 ``<session_node>/debug/node/manifest.json`` 显式定点迁入 main thread。

    main thread 的调试目录物理路径就是会话节点自身（``resolve_thread_node`` 对
    ``main`` 返回会话节点），因此迁移只是 manifest 格式升级（补
    ``thread_id="main"``）+ 完整性校验，不搬目录、不改写方案文件。
    """

    def __init__(
        self,
        session_index: NodeDebugLegacyMigrationSessionIndex,
        journal: NodeDebugLegacyMigrationJournal,
    ) -> None:
        self._session_index = session_index
        self._journal = journal

    def run(self) -> NodeDebugLegacyMigrationSummary:
        """枚举权威索引中的会话并逐个检测/迁移；存在失败会话时 fail-loud。"""
        migrated: list[str] = []
        skipped: list[str] = []
        noop: list[str] = []
        failed: list[tuple[str, str]] = []
        for node in self._session_index.list_authoritative_nodes():
            if node.kind != "session":
                # 只处理会话节点；folder 节点不承载调试数据。
                continue
            session_id = node.node_id
            session_node = self._session_index.resolve_session_node(session_id)
            previous = self._journal.get_record(session_id)
            if previous is not None and previous.get("status") == "migrated":
                # 已迁移会话重跑：完全 no-op，journal 不重复记录。
                noop.append(session_id)
                continue
            record = self._process_session(
                session_id=session_id,
                session_node=session_node,
            )
            if record is None:
                # skipped 且台账已有相同记录：不重复写台账。
                skipped.append(session_id)
                continue
            self._journal.upsert_record(session_id, record)
            status = str(record.get("status"))
            if status == "migrated":
                migrated.append(session_id)
            elif status == "skipped":
                skipped.append(session_id)
            else:
                failed.append((session_id, str(record.get("reason"))))
        self._journal.save()
        summary = NodeDebugLegacyMigrationSummary(
            migrated=tuple(migrated),
            skipped=tuple(skipped),
            noop=tuple(noop),
            failed=tuple(failed),
        )
        if summary.failed:
            details = "; ".join(
                f"session={session_id}: {reason}"
                for session_id, reason in summary.failed
            )
            raise RuntimeError(
                "Node 调试旧数据迁移存在失败会话（原件已保留，可修复后重试）: "
                f"journal={self._journal.journal_path}: {details}"
            )
        return summary

    def _process_session(
        self,
        *,
        session_id: str,
        session_node: Path,
    ) -> dict[str, object] | None:
        """检测并迁移单个会话，返回 journal 记录。

        ``None`` 表示无需写入台账（无调试数据且已有相同 skipped 记录）。
        失败一律走 ``_failed_record``（status="failed"），由 :meth:`run` 汇总
        fail-loud，绝不静默吞掉。
        """
        manifest_path = _session_debug_directory(session_node) / MANIFEST_FILE_NAME
        if not manifest_path.exists():
            record: dict[str, object] = {
                "session_id": session_id,
                "status": "skipped",
                "note": "no-debug-data",
                "updated_at": _utc_now_iso(),
            }
            previous = self._journal.get_record(session_id)
            if (
                previous is not None
                and previous.get("status") == "skipped"
                and previous.get("note") == "no-debug-data"
            ):
                return None
            return record

        manifest_before = _digest_file(manifest_path)
        try:
            raw_text = manifest_path.read_text(encoding="utf-8")
        except OSError as error:
            return self._failed_record(
                session_id,
                manifest_before,
                _fail(session_id, "read-manifest", f"{manifest_path}: {error}"),
            )
        try:
            raw_manifest = json.loads(raw_text)
        except json.JSONDecodeError as error:
            return self._failed_record(
                session_id,
                manifest_before,
                _fail(
                    session_id,
                    "parse-manifest",
                    f"旧 manifest 不是合法 JSON: {manifest_path}: {error}",
                ),
            )
        if not isinstance(raw_manifest, dict):
            return self._failed_record(
                session_id,
                manifest_before,
                _fail(
                    session_id,
                    "parse-manifest",
                    f"旧 manifest 必须是 JSON object: {manifest_path}",
                ),
            )

        if "thread_id" in raw_manifest:
            return self._process_current_format_manifest(
                session_id=session_id,
                manifest_path=manifest_path,
                manifest_before=manifest_before,
                raw_manifest=raw_manifest,
            )
        return self._migrate_legacy_manifest(
            session_id=session_id,
            session_node=session_node,
            manifest_path=manifest_path,
            manifest_before=manifest_before,
            raw_manifest=raw_manifest,
        )

    def _process_current_format_manifest(
        self,
        *,
        session_id: str,
        manifest_path: Path,
        manifest_before: NodeDebugLegacyFileDigest,
        raw_manifest: dict[str, object],
    ) -> dict[str, object]:
        """会话节点级 manifest 已携带 thread_id 时的检测。

        合法 ``thread_id="main"`` → 已迁移，幂等 no-op；其它值说明数据被外部
        改动或归属损坏（会话节点级 manifest 只能属于 main thread），fail-loud
        记 failed，不迁移。
        """
        existing_thread_id = raw_manifest.get("thread_id")
        if existing_thread_id != MAIN_THREAD_ID:
            return self._failed_record(
                session_id,
                manifest_before,
                _fail(
                    session_id,
                    "detect-owner",
                    "会话节点级 manifest 携带非法 thread_id（只允许 main），"
                    f"拒绝迁移: path={manifest_path}, "
                    f"thread_id={existing_thread_id!r}",
                ),
            )
        try:
            NodeDebugSessionManifestDTO.model_validate(raw_manifest)
        except ValidationError as error:
            return self._failed_record(
                session_id,
                manifest_before,
                _fail(
                    session_id,
                    "validate-current",
                    f"已是新格式的 manifest 未通过校验（视为损坏）: "
                    f"{manifest_path}: {error}",
                ),
            )
        return {
            "session_id": session_id,
            "status": "migrated",
            "note": "already-new-format",
            "manifest_before": _digest_dict(manifest_before),
            "updated_at": _utc_now_iso(),
        }

    def _migrate_legacy_manifest(
        self,
        *,
        session_id: str,
        session_node: Path,
        manifest_path: Path,
        manifest_before: NodeDebugLegacyFileDigest,
        raw_manifest: dict[str, object],
    ) -> dict[str, object]:
        """旧格式（无 thread_id）→ 校验 + staging 重写为 main thread 新格式。"""
        if raw_manifest.get("session_id") != session_id:
            return self._failed_record(
                session_id,
                manifest_before,
                _fail(
                    session_id,
                    "validate-owner",
                    "旧 manifest 的 session_id 与所属会话不一致: "
                    f"path={manifest_path}, "
                    f"expected={session_id}, actual={raw_manifest.get('session_id')!r}",
                ),
            )
        if raw_manifest.get("schema_version") != 1:
            return self._failed_record(
                session_id,
                manifest_before,
                _fail(
                    session_id,
                    "validate-schema",
                    f"旧 manifest 的 schema_version 不是 1: path={manifest_path}, "
                    f"actual={raw_manifest.get('schema_version')!r}",
                ),
            )

        configuration_digests, failure_reason = self._validate_configurations(
            session_id=session_id,
            session_node=session_node,
        )
        if failure_reason is not None:
            return self._failed_record(session_id, manifest_before, failure_reason)

        migrated_manifest, failure_reason = self._build_migrated_manifest(
            session_id=session_id,
            manifest_path=manifest_path,
            raw_manifest=raw_manifest,
        )
        if failure_reason is not None or migrated_manifest is None:
            return self._failed_record(
                session_id,
                manifest_before,
                failure_reason
                or _fail(session_id, "migrate-manifest", "未知迁移失败"),
            )

        payload = migrated_manifest.model_dump_json(indent=2).encode("utf-8")
        _atomic_write_bytes(manifest_path, payload)
        manifest_after = NodeDebugLegacyFileDigest(
            file=MANIFEST_FILE_NAME,
            size_bytes=len(payload),
            sha256=_digest_bytes(payload),
        )
        return {
            "session_id": session_id,
            "status": "migrated",
            "manifest_before": _digest_dict(manifest_before),
            "manifest_after": _digest_dict(manifest_after),
            "configurations": [
                _digest_dict(digest) for digest in configuration_digests
            ],
            "updated_at": _utc_now_iso(),
        }

    def _validate_configurations(
        self,
        *,
        session_id: str,
        session_node: Path,
    ) -> tuple[list[NodeDebugLegacyConfigurationDigest], str | None]:
        """逐个校验方案文件并登记证据；格式不变，只校验不改写。"""
        configurations_dir = (
            _session_debug_directory(session_node) / CONFIGURATIONS_DIRECTORY_NAME
        )
        if not configurations_dir.exists():
            return [], None
        if configurations_dir.is_symlink() or not configurations_dir.is_dir():
            return [], _fail(
                session_id,
                "validate-configurations",
                f"调试方案目录必须是普通目录: {configurations_dir}",
            )
        digests: list[NodeDebugLegacyConfigurationDigest] = []
        for path in sorted(configurations_dir.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                return [], _fail(
                    session_id,
                    "validate-configurations",
                    f"调试方案文件必须是普通文件: {path}",
                )
            try:
                configuration = NodeDebugConfigurationDTO.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (ValidationError, json.JSONDecodeError, ValueError, OSError) as error:
                return [], _fail(
                    session_id,
                    "validate-configurations",
                    f"调试方案文件损坏: {path}: {error}",
                )
            expected_name = f"{configuration.configuration_id}.json"
            if path.name != expected_name:
                return [], _fail(
                    session_id,
                    "validate-configurations",
                    "调试方案文件名必须等于 configuration_id: "
                    f"path={path}, expected={expected_name}",
                )
            if CONFIGURATION_ID_PATTERN.fullmatch(
                configuration.configuration_id
            ) is None:
                return [], _fail(
                    session_id,
                    "validate-configurations",
                    f"非法调试方案 ID: path={path}, "
                    f"configuration_id={configuration.configuration_id!r}",
                )
            payload = path.read_bytes()
            digests.append(
                NodeDebugLegacyConfigurationDigest(
                    file=path.name,
                    size_bytes=len(payload),
                    sha256=_digest_bytes(payload),
                    configuration_id=configuration.configuration_id,
                    revision=configuration.revision,
                )
            )
        return digests, None

    def _build_migrated_manifest(
        self,
        *,
        session_id: str,
        manifest_path: Path,
        raw_manifest: dict[str, object],
    ) -> tuple[NodeDebugSessionManifestDTO | None, str | None]:
        """构造 ``thread_id="main"`` 的新 manifest；其余字段语义逐项保留。"""
        migrated_raw = dict(raw_manifest)
        migrated_raw["thread_id"] = MAIN_THREAD_ID
        raw_actions = migrated_raw.get("actions")
        if raw_actions is None:
            raw_actions = []
        if not isinstance(raw_actions, list):
            return None, _fail(
                session_id,
                "migrate-manifest",
                f"旧 manifest 的 actions 必须是列表: {manifest_path}",
            )
        migrated_actions: list[dict[str, object]] = []
        for item in raw_actions:
            if not isinstance(item, dict):
                return None, _fail(
                    session_id,
                    "migrate-manifest",
                    f"动作记录必须是 JSON object: {manifest_path}",
                )
            action = dict(item)
            existing_thread_id = action.get("thread_id")
            if existing_thread_id is not None and existing_thread_id != MAIN_THREAD_ID:
                return None, _fail(
                    session_id,
                    "migrate-manifest",
                    "动作记录携带非法 thread_id（只允许 main）: "
                    f"path={manifest_path}, thread_id={existing_thread_id!r}",
                )
            action["thread_id"] = MAIN_THREAD_ID
            migrated_actions.append(action)
        migrated_raw["actions"] = migrated_actions
        try:
            # extra="forbid" 同时拒绝未知顶层字段；thread_id 必填由 DTO 保证。
            migrated = NodeDebugSessionManifestDTO.model_validate(migrated_raw)
        except ValidationError as error:
            return None, _fail(
                session_id,
                "migrate-manifest",
                f"迁移后的 manifest 未通过新格式校验（原件未动）: "
                f"{manifest_path}: {error}",
            )
        if migrated.session_id != session_id or migrated.thread_id != MAIN_THREAD_ID:
            return None, _fail(
                session_id,
                "migrate-manifest",
                "迁移后的 manifest owner 不一致: "
                f"path={manifest_path}, "
                f"actual=({migrated.session_id}, {migrated.thread_id})",
            )
        return migrated, None

    @staticmethod
    def _failed_record(
        session_id: str,
        manifest_before: NodeDebugLegacyFileDigest | None,
        reason: str,
    ) -> dict[str, object]:
        """构造 failed 记录：保留迁移前 manifest 证据，原件不动可重试。"""
        record: dict[str, object] = {
            "session_id": session_id,
            "status": "failed",
            "reason": reason,
            "updated_at": _utc_now_iso(),
        }
        if manifest_before is not None:
            record["manifest_before"] = _digest_dict(manifest_before)
        return record


__all__ = [
    "CONFIGURATION_ID_PATTERN",
    "JOURNAL_SCHEMA_VERSION",
    "MIGRATION_NAME",
    "NodeDebugLegacyConfigurationDigest",
    "NodeDebugLegacyDirectoryMigrator",
    "NodeDebugLegacyFileDigest",
    "NodeDebugLegacyMigrationJournal",
    "NodeDebugLegacyMigrationSessionIndex",
    "NodeDebugLegacyMigrationStatus",
    "NodeDebugLegacyMigrationSummary",
    "default_journal_path",
]
