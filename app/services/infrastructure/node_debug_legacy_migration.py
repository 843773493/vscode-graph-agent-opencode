"""旧 Session ``debug/node/`` 目录的显式迁移（调试域维护步骤）。

OpenSpec ``add-agent-debug-tool-group`` 任务 2.5 / itemized 8.2-A：R3a 把
``NodeDebugSessionStore`` 收紧为精确 ``(session_id, thread_id)`` 归属后，旧格式
manifest（无 ``thread_id``）对新 store 不可读。本模块提供**显式维护操作**把这些
旧数据定点迁移到 main thread（``thread_id="main"``），不接入正常 runtime 路径：
正常 runtime 继续只读新格式，旧格式由本步骤一次性升级。

职责边界：
- 调试 domain 拥有方案语义（manifest/方案校验、hash 登记、staging 原子切换）；
  itemized（8.2-A）拥有共享 maintenance gate/journal/发布门槛。本模块通过
  ``NodeDebugLegacyMigrationSessionIndex`` 与 ``NodeDebugLegacyMigrationJournalPort``
  接收冻结索引和共享 journal 投影，不创建独立协调器。
- 只做纯文件操作：不触碰任何 Node/Inspector 进程或端口，不建立第二迁移
  协调器，不新增 runtime 旧路径 alias，不扫盘（索引外目录一律不触碰）。
"""

from __future__ import annotations

import base64
import binascii
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

MANIFEST_FILE_NAME = "manifest.json"
CONFIGURATIONS_DIRECTORY_NAME = "configurations"
CONFIGURATION_ID_PATTERN = re.compile(r"^dbgcfg_[0-9a-f]{32}$")

NodeDebugLegacyMigrationStatus = Literal[
    "pending",
    "applying",
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


class NodeDebugLegacyMigrationJournalPort(Protocol):
    """共享 maintenance journal 对调试迁移器暴露的最小端口。"""

    @property
    def journal_path(self) -> Path: ...

    def get_record(self, session_id: str) -> dict[str, object] | None: ...

    def upsert_record(self, session_id: str, record: dict[str, object]) -> None: ...

    def save(self) -> None: ...


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


def _record_file_digest(
    value: object, *, expected_file: str
) -> tuple[int, str] | None:
    if not isinstance(value, dict) or value.get("file") != expected_file:
        return None
    size = value.get("size_bytes")
    sha256 = value.get("sha256")
    if (
        type(size) is not int
        or size < 0
        or not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
    ):
        return None
    return size, sha256


def _fail(session_id: str, stage: str, detail: str) -> str:
    """构造带会话、阶段与明细的失败原因文本（journal 与异常共用）。"""
    return f"session={session_id}, stage={stage}: {detail}"


def _session_debug_directory(session_node: Path) -> Path:
    """main thread 的调试目录即会话节点自身的 ``debug/node/``（路径不变，不搬目录）。"""
    return session_node / "debug" / "node"


"""调试域迁移步骤只消费共享 SessionCatalog journal。"""

class NodeDebugLegacyDirectoryMigrator:
    """把旧格式 ``<session_node>/debug/node/manifest.json`` 显式定点迁入 main thread。

    main thread 的调试目录物理路径就是会话节点自身（``resolve_thread_node`` 对
    ``main`` 返回会话节点），因此迁移只是 manifest 格式升级（补
    ``thread_id="main"``）+ 完整性校验，不搬目录、不改写方案文件。
    """

    def __init__(
        self,
        session_index: NodeDebugLegacyMigrationSessionIndex,
        journal: NodeDebugLegacyMigrationJournalPort,
    ) -> None:
        self._session_index = session_index
        self._journal = journal
        self._last_summary: NodeDebugLegacyMigrationSummary | None = None

    @property
    def last_summary(self) -> NodeDebugLegacyMigrationSummary | None:
        """最近一次运行结果。

        共享 catalog 迁移 journal 的适配器需要在迁移步骤失败时把失败摘要
        一并写入同一份 journal。保留该投影不会引入第二份持久化事实源。
        """
        return self._last_summary

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
            # 每个 item 都先进入共享 catalog journal，再处理下一个 Session。
            # 对会修改 manifest 的 item，_migrate_legacy_manifest 还会在替换前
            # 单独持久化 applying intent，避免文件与 journal 之间出现崩溃空窗。
            self._journal.save()
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
        self._last_summary = summary
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
        previous = self._journal.get_record(session_id)
        if (
            previous is not None
            and (
                previous.get("status") == "applying"
                or (
                    previous.get("status") == "failed"
                    and "manifest_before_base64" in previous
                )
            )
        ):
            recovered = self._recover_applying_record(
                session_id=session_id,
                manifest_path=manifest_path,
                record=previous,
            )
            if recovered is not None:
                return recovered
        if not manifest_path.exists():
            record: dict[str, object] = {
                "session_id": session_id,
                "status": "skipped",
                "note": "no-debug-data",
                "updated_at": _utc_now_iso(),
            }
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
            configuration_ids=tuple(
                digest.configuration_id for digest in configuration_digests
            ),
        )
        if failure_reason is not None or migrated_manifest is None:
            return self._failed_record(
                session_id,
                manifest_before,
                failure_reason
                or _fail(session_id, "migrate-manifest", "未知迁移失败"),
            )

        payload = migrated_manifest.model_dump_json(indent=2).encode("utf-8")
        manifest_after = NodeDebugLegacyFileDigest(
            file=MANIFEST_FILE_NAME,
            size_bytes=len(payload),
            sha256=_digest_bytes(payload),
        )
        configuration_evidence = [
            _digest_dict(digest) for digest in configuration_digests
        ]
        previous = self._journal.get_record(session_id)
        if (
            previous is not None
            and (
                previous.get("status") == "applying"
                or (
                    previous.get("status") == "failed"
                    and "manifest_before_base64" in previous
                )
            )
            and (
                previous.get("manifest_after") != _digest_dict(manifest_after)
                or previous.get("configurations") != configuration_evidence
            )
        ):
            return self._failed_applying_record(
                previous,
                reason=_fail(
                    session_id,
                    "recover-applying",
                    "重算的 expected after/configuration 证据与 durable intent 不一致",
                ),
            )
        applying_record: dict[str, object] = {
            "session_id": session_id,
            "status": "applying",
            "manifest_before": _digest_dict(manifest_before),
            "manifest_before_base64": base64.b64encode(
                manifest_path.read_bytes()
            ).decode("ascii"),
            "manifest_after": _digest_dict(manifest_after),
            "configurations": configuration_evidence,
            "updated_at": _utc_now_iso(),
        }
        # intent 必须先通过共享 catalog journal durable 落盘，再替换原文件。
        self._journal.upsert_record(session_id, applying_record)
        self._journal.save()
        _atomic_write_bytes(manifest_path, payload)
        completed = dict(applying_record)
        completed["status"] = "migrated"
        completed.pop("manifest_before_base64")
        completed["updated_at"] = _utc_now_iso()
        self._journal.upsert_record(session_id, completed)
        self._journal.save()
        return completed

    def _recover_applying_record(
        self,
        *,
        session_id: str,
        manifest_path: Path,
        record: dict[str, object],
    ) -> dict[str, object] | None:
        """仅按 durable intent 的 before/after hash 恢复中断的单文件替换。"""
        before = record.get("manifest_before")
        after = record.get("manifest_after")
        preimage_base64 = record.get("manifest_before_base64")
        before_digest = _record_file_digest(
            before, expected_file=MANIFEST_FILE_NAME
        )
        after_digest = _record_file_digest(after, expected_file=MANIFEST_FILE_NAME)
        if (
            before_digest is None
            or after_digest is None
            or not isinstance(preimage_base64, str)
        ):
            return self._failed_applying_record(
                record,
                reason=_fail(
                    session_id, "recover-applying", "applying intent 结构损坏"
                ),
            )
        try:
            preimage = base64.b64decode(preimage_base64, validate=True)
        except (binascii.Error, ValueError):
            return self._failed_applying_record(
                record,
                reason=_fail(
                    session_id, "recover-applying", "manifest preimage 编码损坏"
                ),
            )
        before_size, before_sha256 = before_digest
        after_size, after_sha256 = after_digest
        if len(preimage) != before_size or _digest_bytes(preimage) != before_sha256:
            return self._failed_applying_record(
                record,
                reason=_fail(
                    session_id, "recover-applying", "manifest preimage hash 不一致"
                ),
            )
        if not manifest_path.is_file() or manifest_path.is_symlink():
            return self._failed_applying_record(
                record,
                reason=_fail(
                    session_id,
                    "recover-applying",
                    "manifest 缺失或不是普通文件",
                ),
            )
        current = _digest_file(manifest_path)
        if current.size_bytes == after_size and current.sha256 == after_sha256:
            configuration_error = self._verify_applying_configurations(
                manifest_path=manifest_path,
                record=record,
            )
            if configuration_error is not None:
                return self._failed_applying_record(
                    record,
                    reason=_fail(
                        session_id, "recover-applying", configuration_error
                    ),
                )
            completed = dict(record)
            completed["status"] = "migrated"
            completed.pop("manifest_before_base64", None)
            completed["updated_at"] = _utc_now_iso()
            self._journal.upsert_record(session_id, completed)
            self._journal.save()
            return completed
        if current.size_bytes == before_size and current.sha256 == before_sha256:
            configuration_error = self._verify_applying_configurations(
                manifest_path=manifest_path,
                record=record,
            )
            if configuration_error is not None:
                return self._failed_applying_record(
                    record,
                    reason=_fail(
                        session_id, "recover-applying", configuration_error
                    ),
                )
            # 替换尚未发生；保留同一 intent，重算结果必须与其完全一致。
            return None
        return self._failed_applying_record(
            record,
            reason=_fail(
                session_id,
                "recover-applying",
                "manifest 既不匹配 intent before 也不匹配 expected after，拒绝猜测",
            ),
        )

    @staticmethod
    def _failed_applying_record(
        record: dict[str, object], *, reason: str
    ) -> dict[str, object]:
        """保留 durable intent 全部证据，并显式转为失败态。"""
        failed = dict(record)
        failed["status"] = "failed"
        failed["reason"] = reason
        failed["updated_at"] = _utc_now_iso()
        return failed

    @staticmethod
    def _verify_applying_configurations(
        *, manifest_path: Path, record: dict[str, object]
    ) -> str | None:
        raw = record.get("configurations")
        if not isinstance(raw, list):
            return "applying intent 缺少 configurations 证据"
        expected: dict[str, tuple[int, str]] = {}
        for item in raw:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("file"), str)
                or type(item.get("size_bytes")) is not int
                or item["size_bytes"] < 0
                or not isinstance(item.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
                or not isinstance(item.get("configuration_id"), str)
                or CONFIGURATION_ID_PATTERN.fullmatch(item["configuration_id"]) is None
                or item["file"] != f"{item['configuration_id']}.json"
                or type(item.get("revision")) is not int
                or item["revision"] < 1
            ):
                return "applying intent 的 configuration 证据损坏"
            if item["file"] in expected:
                return "applying intent 包含重复 configuration 文件证据"
            expected[item["file"]] = (item["size_bytes"], item["sha256"])
        directory = manifest_path.parent / CONFIGURATIONS_DIRECTORY_NAME
        actual_paths = sorted(directory.glob("*.json")) if directory.exists() else []
        if {path.name for path in actual_paths} != set(expected):
            return "configuration 文件集合与 applying intent 不一致"
        for path in actual_paths:
            if path.is_symlink() or not path.is_file():
                return f"configuration 不是普通文件: {path.name}"
            payload = path.read_bytes()
            size, digest = expected[path.name]
            if len(payload) != size or _digest_bytes(payload) != digest:
                return f"configuration bytes 与 applying intent 不一致: {path.name}"
        return None

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
        configuration_ids: tuple[str, ...],
    ) -> tuple[NodeDebugSessionManifestDTO | None, str | None]:
        """构造 ``thread_id="main"`` 的新 manifest；其余字段语义逐项保留。"""
        migrated_raw = dict(raw_manifest)
        migrated_raw["thread_id"] = MAIN_THREAD_ID
        migrated_raw["configuration_ids"] = configuration_ids
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
    "NodeDebugLegacyConfigurationDigest",
    "NodeDebugLegacyDirectoryMigrator",
    "NodeDebugLegacyFileDigest",
    "NodeDebugLegacyMigrationJournalPort",
    "NodeDebugLegacyMigrationSessionIndex",
    "NodeDebugLegacyMigrationStatus",
    "NodeDebugLegacyMigrationSummary",
]
