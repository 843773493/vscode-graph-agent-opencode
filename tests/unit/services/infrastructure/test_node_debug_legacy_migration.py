"""旧 Session ``debug/node/`` 目录显式迁移的定向单元测试。

覆盖 R3c（OpenSpec 任务 2.5 debug-domain 迁移步骤）：
- 旧格式 manifest（无 thread_id）+ 方案文件 → 迁移后新 store 按
  ``(session_id, "main")`` 可读，thread_id 补齐、actions 逐条补齐；
- 校验失败注入（方案文件损坏 / manifest session_id 不匹配 / thread_id 非 main
  的损坏形态）→ 原件逐字节不变、journal 记 failed、修复后重试成功；
- 幂等：已迁移会话重跑 no-op，journal 不重复记录；
- 不扫盘：索引外目录含 ``debug/node/`` 也不触碰；
- 无调试数据会话 skipped；journal 跨进程重载状态保留；
- 迁移后方案文件 bytes/hash 不变。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_paths import SessionPathResolver
from app.core.session_tree.support import SessionPhysicalNode
from app.schemas.internal_v2.node_debug import NodeDebugSessionManifestDTO
from app.services.infrastructure import node_debug_legacy_migration
from app.services.infrastructure.node_debug_legacy_migration import (
    NodeDebugLegacyDirectoryMigrator,
    NodeDebugLegacyMigrationJournalPort,
    NodeDebugLegacyMigrationSessionIndex,
)
from app.services.infrastructure.node_debug_session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.node_debug_thread_owner import MAIN_THREAD_ID

_SESSION_ID = "ses_legacy"
_SESSION_ID_B = "ses_plain"
_CONFIGURATION_ID = "dbgcfg_11111111111111111111111111111111"
_CONFIGURATION_ID_2 = "dbgcfg_22222222222222222222222222222222"

_OLD_UPDATED_AT = "2026-01-01T00:00:00+00:00"
_ACTION_CREATED_AT = "2026-01-01T00:01:00+00:00"


def _create_session(
    resolver: SessionPathResolver,
    session_id: str,
    *,
    parent_session_id: str | None = None,
) -> Path:
    """在权威目录索引中创建最小合法会话节点（与同目录既有测试一致）。"""
    title = f"测试会话 {session_id}"
    session_dir = resolver.allocate_session_dir(
        session_id=session_id,
        title=title,
        parent_node_id=parent_session_id,
    )
    now = datetime.now(UTC).isoformat()
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "title": title,
                "parent_session_id": parent_session_id,
                "created_at": now,
                "updated_at": now,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    resolver.register_session(session_id, session_dir)
    return session_dir


class _IndexOnlySessionIndex:
    """只按权威索引枚举的替身：没有扫盘能力，用于证明迁移不触碰索引外目录。"""

    def __init__(self, nodes: dict[str, Path]) -> None:
        self._nodes = nodes
        self.listed_session_ids: list[str] = []
        self.resolved_session_ids: list[str] = []

    def list_authoritative_nodes(self) -> list[SessionPhysicalNode]:
        self.listed_session_ids = sorted(self._nodes)
        return [
            SessionPhysicalNode(
                node_id=session_id,
                kind="session",
                path=path,
                parent_node_id=None,
                name=session_id,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            for session_id, path in sorted(self._nodes.items())
        ]

    def resolve_session_node(self, session_id: str) -> Path:
        self.resolved_session_ids.append(session_id)
        if session_id not in self._nodes:
            raise KeyError(f"权威会话目录索引不存在: session_id={session_id}")
        return self._nodes[session_id]


def _legacy_manifest_payload(
    session_id: str,
    *,
    actions: list[dict[str, object]] | None = None,
    active_configuration_id: str | None = None,
    session_id_override: str | None = None,
) -> bytes:
    """构造旧格式 manifest 字节（无 thread_id；actions 记录同样无 thread_id）。"""
    payload = {
        "schema_version": 1,
        "session_id": session_id_override or session_id,
        "active_configuration_id": active_configuration_id,
        "actions": actions
        if actions is not None
        else [
            {
                "action_id": "act_legacy_1",
                "session_id": session_id_override or session_id,
                "action": "set_breakpoint",
                "message": "设置断点 main.mjs:3",
                "actor": "human",
                "result": "success",
                "created_at": _ACTION_CREATED_AT,
            }
        ],
        "updated_at": _OLD_UPDATED_AT,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _legacy_configuration_payload(
    configuration_id: str,
    *,
    name: str = "旧方案",
    revision: int = 3,
) -> bytes:
    """构造旧格式方案文件字节（与新 NodeDebugConfigurationDTO 兼容，格式不变）。"""
    payload = {
        "schema_version": 1,
        "configuration_id": configuration_id,
        "name": name,
        "revision": revision,
        "script_path": "main.mjs",
        "working_directory": "",
        "launch_profile_name": None,
        "args": ["--verbose"],
        "breakpoints": [
            {
                "breakpoint_id": "bp_legacy_1",
                "path": "main.mjs",
                "line": 3,
                "column": 1,
                "condition": None,
                "hit_condition": None,
                "log_message": None,
                "created_at": _ACTION_CREATED_AT,
            }
        ],
        "created_at": _OLD_UPDATED_AT,
        "updated_at": _OLD_UPDATED_AT,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _write_legacy_debug_data(
    session_node: Path,
    session_id: str,
    *,
    manifest_payload: bytes | None = None,
    configuration_payloads: dict[str, bytes] | None = None,
) -> None:
    """在会话节点下手工放置旧格式 debug/node 数据（物理路径与 main thread 一致）。"""
    debug_dir = session_node / "debug" / "node"
    debug_dir.mkdir(parents=True, exist_ok=True)
    manifest_bytes = (
        manifest_payload
        if manifest_payload is not None
        else _legacy_manifest_payload(
            session_id, active_configuration_id=_CONFIGURATION_ID
        )
    )
    (debug_dir / "manifest.json").write_bytes(manifest_bytes)
    configurations_dir = debug_dir / "configurations"
    configurations_dir.mkdir(exist_ok=True)
    for name, payload in (configuration_payloads or {}).items():
        (configurations_dir / name).write_bytes(payload)


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _MemoryJournal(NodeDebugLegacyMigrationJournalPort):
    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.saved_records: list[dict[str, dict[str, object]]] = []

    @property
    def journal_path(self) -> Path:
        return Path("<shared-catalog-journal>")

    def get_record(self, session_id: str) -> dict[str, object] | None:
        record = self.records.get(session_id)
        return dict(record) if record is not None else None

    def upsert_record(self, session_id: str, record: dict[str, object]) -> None:
        self.records[session_id] = dict(record)

    def save(self) -> None:
        self.saved_records.append(json.loads(json.dumps(self.records)))

    def read_bytes(self) -> bytes:
        return json.dumps(self.records, ensure_ascii=False, sort_keys=True).encode()


def _read_journal_records(journal: _MemoryJournal) -> dict[str, dict[str, object]]:
    return journal.records


@pytest.fixture
def journal_path() -> _MemoryJournal:
    return _MemoryJournal()


@pytest.fixture
def session_tree(tmp_path: Path) -> tuple[SessionPathResolver, Path, Path]:
    """建立工作区 tmp 工作区 + 权威索引中的两个会话（其一有旧调试数据）。"""
    workspace_root = tmp_path / "workspace"
    boxteam_root = workspace_root / ".boxteam"
    resolver = SessionPathResolver(boxteam_root / "sessions")
    resolver.initialize()
    legacy_dir = _create_session(resolver, _SESSION_ID)
    plain_dir = _create_session(resolver, _SESSION_ID_B)
    _write_legacy_debug_data(
        legacy_dir,
        _SESSION_ID,
        configuration_payloads={
            f"{_CONFIGURATION_ID}.json": _legacy_configuration_payload(
                _CONFIGURATION_ID
            ),
            f"{_CONFIGURATION_ID_2}.json": _legacy_configuration_payload(
                _CONFIGURATION_ID_2,
                name="第二个旧方案",
                revision=5,
            ),
        },
    )
    return resolver, legacy_dir, plain_dir


def _migrator(
    session_index: NodeDebugLegacyMigrationSessionIndex,
    journal: _MemoryJournal,
) -> NodeDebugLegacyDirectoryMigrator:
    return NodeDebugLegacyDirectoryMigrator(
        session_index=session_index,
        journal=journal,
    )


def test_legacy_manifest_migrates_to_main_and_new_store_reads_it(
    session_tree: tuple[SessionPathResolver, Path, Path],
    journal_path: _MemoryJournal,
) -> None:
    """旧格式 fixture → 迁移 → 新 store read_manifest(session, "main") 成功。"""
    resolver, legacy_dir, _plain_dir = session_tree
    store = NodeDebugSessionStore(resolver)

    # 迁移前：新 store 读旧格式必须失败（thread_id 缺失），证明确实需要迁移。
    with pytest.raises(RuntimeError, match="会话源码调试数据损坏"):
        store.read_manifest(_SESSION_ID, MAIN_THREAD_ID)

    manifest_bytes_before = (
        legacy_dir / "debug" / "node" / "manifest.json"
    ).read_bytes()
    summary = _migrator(resolver, journal_path).run()

    assert summary.migrated == (_SESSION_ID,)
    assert summary.failed == ()
    manifest = store.read_manifest(_SESSION_ID, MAIN_THREAD_ID)
    assert manifest is not None
    assert manifest.session_id == _SESSION_ID
    assert manifest.thread_id == MAIN_THREAD_ID
    assert manifest.active_configuration_id == _CONFIGURATION_ID
    # 旧 manifest 的其余字段语义逐项保留：updated_at、schema_version、动作明细。
    assert manifest.updated_at.isoformat() == _OLD_UPDATED_AT
    assert [action.action_id for action in manifest.actions] == ["act_legacy_1"]
    assert [action.message for action in manifest.actions] == ["设置断点 main.mjs:3"]
    assert [action.created_at.isoformat() for action in manifest.actions] == [
        _ACTION_CREATED_AT
    ]
    # actions 逐条补齐 thread_id="main"。
    assert all(action.thread_id == MAIN_THREAD_ID for action in manifest.actions)

    # 迁移只是格式升级：manifest 物理路径不变（main thread 目录即会话节点自身）。
    assert (
        legacy_dir / "debug" / "node" / "manifest.json"
    ).read_bytes() != manifest_bytes_before
    assert (
        legacy_dir / "debug" / "node" / "manifest.json"
    ).is_file()

    # journal 记录迁移证据：前后 manifest hash + 方案清单（ID/revision/hash）。
    records = _read_journal_records(journal_path)
    record = records[_SESSION_ID]
    assert record["status"] == "migrated"
    assert record["manifest_before"]["sha256"] == hashlib.sha256(
        manifest_bytes_before
    ).hexdigest()
    assert record["manifest_after"]["sha256"] != record["manifest_before"]["sha256"]
    assert {
        (item["configuration_id"], item["revision"])
        for item in record["configurations"]
    } == {(_CONFIGURATION_ID, 3), (_CONFIGURATION_ID_2, 5)}

    # 迁移后方案文件经新 store 正常可读。
    configurations = store.list_configurations(_SESSION_ID, MAIN_THREAD_ID)
    assert [item.configuration_id for item in configurations] == [
        _CONFIGURATION_ID,
        _CONFIGURATION_ID_2,
    ]
    assert [item.revision for item in configurations] == [3, 5]

    # 迁移后的 manifest 文件本身可通过新 DTO 严格校验（extra=forbid）。
    NodeDebugSessionManifestDTO.model_validate_json(
        (legacy_dir / "debug" / "node" / "manifest.json").read_text(encoding="utf-8")
    )


def test_configuration_corruption_fails_without_touching_originals(
    session_tree: tuple[SessionPathResolver, Path, Path],
    journal_path: _MemoryJournal,
) -> None:
    """方案文件损坏 → journal 记 failed、原件逐字节不变、fail-loud 抛错。"""
    resolver, legacy_dir, _plain_dir = session_tree
    broken_payload = "{ 不是合法 JSON".encode()
    configuration_path = (
        legacy_dir / "debug" / "node" / "configurations" / f"{_CONFIGURATION_ID}.json"
    )
    configuration_path.write_bytes(broken_payload)
    manifest_path = legacy_dir / "debug" / "node" / "manifest.json"
    manifest_bytes_before = manifest_path.read_bytes()
    configuration_bytes_before = configuration_path.read_bytes()

    with pytest.raises(RuntimeError, match="存在失败会话"):
        _migrator(resolver, journal_path).run()

    # 原件逐字节不变。
    assert manifest_path.read_bytes() == manifest_bytes_before
    assert configuration_path.read_bytes() == configuration_bytes_before

    records = _read_journal_records(journal_path)
    record = records[_SESSION_ID]
    assert record["status"] == "failed"
    assert "validate-configurations" in record["reason"]
    assert str(configuration_path) in record["reason"]
    assert record["manifest_before"]["sha256"] == hashlib.sha256(
        manifest_bytes_before
    ).hexdigest()

    # 修复方案文件后重试：迁移成功，manifest 被升级。
    configuration_path.write_bytes(_legacy_configuration_payload(_CONFIGURATION_ID))
    summary = _migrator(resolver, journal_path).run()
    assert summary.migrated == (_SESSION_ID,)
    manifest = NodeDebugSessionStore(resolver).read_manifest(
        _SESSION_ID, MAIN_THREAD_ID
    )
    assert manifest is not None
    assert manifest.thread_id == MAIN_THREAD_ID


def test_manifest_session_id_mismatch_fails_and_keeps_original(
    session_tree: tuple[SessionPathResolver, Path, Path],
    journal_path: _MemoryJournal,
) -> None:
    """manifest session_id 与所属会话不一致 → failed、原件不动。"""
    resolver, legacy_dir, _plain_dir = session_tree
    forged = _legacy_manifest_payload(
        _SESSION_ID, session_id_override="ses_other", actions=[]
    )
    manifest_path = legacy_dir / "debug" / "node" / "manifest.json"
    manifest_path.write_bytes(forged)

    with pytest.raises(RuntimeError, match="存在失败会话"):
        _migrator(resolver, journal_path).run()

    assert manifest_path.read_bytes() == forged
    records = _read_journal_records(journal_path)
    record = records[_SESSION_ID]
    assert record["status"] == "failed"
    assert "validate-owner" in record["reason"]
    assert "ses_other" in record["reason"]


def test_non_main_thread_id_is_corruption_and_fails_loud(
    session_tree: tuple[SessionPathResolver, Path, Path],
    journal_path: _MemoryJournal,
) -> None:
    """会话节点级 manifest 携带 thread_id≠main 的损坏形态 → fail-loud 不迁移。"""
    resolver, legacy_dir, _plain_dir = session_tree
    corrupted = json.loads(
        _legacy_manifest_payload(_SESSION_ID).decode("utf-8")
    )
    corrupted["thread_id"] = "ses_child_thread"
    corrupted_bytes = json.dumps(corrupted, ensure_ascii=False, indent=2).encode(
        "utf-8"
    )
    manifest_path = legacy_dir / "debug" / "node" / "manifest.json"
    manifest_path.write_bytes(corrupted_bytes)

    with pytest.raises(RuntimeError, match="存在失败会话"):
        _migrator(resolver, journal_path).run()

    assert manifest_path.read_bytes() == corrupted_bytes
    records = _read_journal_records(journal_path)
    record = records[_SESSION_ID]
    assert record["status"] == "failed"
    assert "detect-owner" in record["reason"]
    assert "ses_child_thread" in record["reason"]


def test_rerun_after_migration_is_noop_and_journal_unchanged(
    session_tree: tuple[SessionPathResolver, Path, Path],
    journal_path: _MemoryJournal,
) -> None:
    """幂等：已迁移会话重跑 → no-op，journal 不重复记录（bytes 不变）。"""
    resolver, legacy_dir, _plain_dir = session_tree
    migrator = _migrator(resolver, journal_path)
    first = migrator.run()
    assert first.migrated == (_SESSION_ID,)
    manifest_after_first = (
        legacy_dir / "debug" / "node" / "manifest.json"
    ).read_bytes()
    journal_bytes_after_first = journal_path.read_bytes()
    records_after_first = _read_journal_records(journal_path)

    second = migrator.run()

    assert second.migrated == ()
    assert second.noop == (_SESSION_ID,)
    assert second.failed == ()
    assert (
        legacy_dir / "debug" / "node" / "manifest.json"
    ).read_bytes() == manifest_after_first
    # journal 不重复记录：重跑后台账文件逐字节不变。
    assert journal_path.read_bytes() == journal_bytes_after_first
    assert _read_journal_records(journal_path) == records_after_first


def test_crash_after_manifest_replace_recovers_from_shared_applying_intent(
    session_tree: tuple[SessionPathResolver, Path, Path],
    journal_path: _MemoryJournal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """替换后崩溃只能按 durable before/after 证据完成，不能伪装既有新格式。"""
    resolver, legacy_dir, _plain_dir = session_tree
    manifest_path = legacy_dir / "debug" / "node" / "manifest.json"
    manifest_before = manifest_path.read_bytes()
    original_write = node_debug_legacy_migration._atomic_write_bytes

    def crash_after_replace(path: Path, payload: bytes) -> None:
        original_write(path, payload)
        raise RuntimeError("injected crash after manifest replace")

    monkeypatch.setattr(
        node_debug_legacy_migration,
        "_atomic_write_bytes",
        crash_after_replace,
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        _migrator(resolver, journal_path).run()

    applying = journal_path.records[_SESSION_ID]
    assert applying["status"] == "applying"
    assert applying["manifest_before"]["sha256"] == hashlib.sha256(
        manifest_before
    ).hexdigest()
    assert applying["manifest_after"]["sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()

    monkeypatch.setattr(
        node_debug_legacy_migration,
        "_atomic_write_bytes",
        original_write,
    )
    summary = _migrator(resolver, journal_path).run()

    assert summary.migrated == (_SESSION_ID,)
    recovered = journal_path.records[_SESSION_ID]
    assert recovered["status"] == "migrated"
    assert recovered.get("note") != "already-new-format"
    assert "manifest_before_base64" not in recovered
    assert recovered["manifest_before"] == applying["manifest_before"]
    assert recovered["manifest_after"] == applying["manifest_after"]
    assert recovered["configurations"] == applying["configurations"]


def test_applying_intent_rejects_configuration_drift_before_replace(
    session_tree: tuple[SessionPathResolver, Path, Path],
    journal_path: _MemoryJournal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """intent 已落盘但 manifest 未替换时，方案 bytes 漂移必须阻断重试。"""
    resolver, legacy_dir, _plain_dir = session_tree
    original_write = node_debug_legacy_migration._atomic_write_bytes

    def crash_before_replace(_path: Path, _payload: bytes) -> None:
        raise RuntimeError("injected crash before manifest replace")

    monkeypatch.setattr(
        node_debug_legacy_migration,
        "_atomic_write_bytes",
        crash_before_replace,
    )
    with pytest.raises(RuntimeError, match="injected crash"):
        _migrator(resolver, journal_path).run()
    assert journal_path.records[_SESSION_ID]["status"] == "applying"

    configuration_path = (
        legacy_dir
        / "debug"
        / "node"
        / "configurations"
        / f"{_CONFIGURATION_ID}.json"
    )
    configuration_path.write_bytes(configuration_path.read_bytes() + b"\n")
    monkeypatch.setattr(
        node_debug_legacy_migration,
        "_atomic_write_bytes",
        original_write,
    )

    with pytest.raises(RuntimeError, match="存在失败会话"):
        _migrator(resolver, journal_path).run()
    failed = journal_path.records[_SESSION_ID]
    assert failed["status"] == "failed"
    assert "configuration bytes" in failed["reason"]

    # 保留 intent 的 failed 状态不能被合法 main manifest 或普通重试绕过。
    with pytest.raises(RuntimeError, match="存在失败会话"):
        _migrator(resolver, journal_path).run()
    assert journal_path.records[_SESSION_ID]["status"] == "failed"


def test_already_new_format_manifest_is_recorded_once(
    session_tree: tuple[SessionPathResolver, Path, Path],
    journal_path: _MemoryJournal,
) -> None:
    """已是新格式（thread_id=main）的会话 → 幂等 no-op，manifest 不改写。"""
    resolver, legacy_dir, _plain_dir = session_tree
    store = NodeDebugSessionStore(resolver)
    # 用新 store 写出合法新格式 manifest（覆盖旧格式 fixture）。
    store.write_manifest(
        NodeDebugSessionManifestDTO(
            session_id=_SESSION_ID,
            thread_id=MAIN_THREAD_ID,
            active_configuration_id=_CONFIGURATION_ID,
            actions=[],
            updated_at=datetime.now(UTC),
        )
    )
    manifest_path = legacy_dir / "debug" / "node" / "manifest.json"
    new_format_bytes = manifest_path.read_bytes()
    migrator = _migrator(resolver, journal_path)

    summary = migrator.run()

    assert summary.migrated == (_SESSION_ID,)
    assert summary.failed == ()
    # 新格式 manifest 不被改写。
    assert manifest_path.read_bytes() == new_format_bytes
    records = _read_journal_records(journal_path)
    record = records[_SESSION_ID]
    assert record["status"] == "migrated"
    assert record["note"] == "already-new-format"
    assert record["manifest_before"]["sha256"] == hashlib.sha256(
        new_format_bytes
    ).hexdigest()

    # 重跑：journal 已记 migrated → 完全 no-op，不重复记录。
    second = migrator.run()
    assert second.noop == (_SESSION_ID,)
    assert manifest_path.read_bytes() == new_format_bytes


def test_out_of_index_directory_is_not_touched(tmp_path: Path) -> None:
    """索引外目录即使含 debug/node/ 也不触碰：枚举只来自权威索引，不扫盘。"""
    sessions_root = tmp_path / "workspace" / ".boxteam" / "sessions"
    sessions_root.mkdir(parents=True)
    in_index_node = sessions_root / _SESSION_ID
    in_index_node.mkdir()
    _write_legacy_debug_data(in_index_node, _SESSION_ID)
    # 索引外目录：物理上存在旧格式调试数据，但不在权威索引中。
    out_of_index_node = sessions_root / "ses_orphan"
    _write_legacy_debug_data(out_of_index_node, "ses_orphan")
    orphan_manifest = out_of_index_node / "debug" / "node" / "manifest.json"
    orphan_bytes_before = orphan_manifest.read_bytes()

    # 替身没有任何扫盘能力：只返回手工登记的权威索引节点。
    session_index = _IndexOnlySessionIndex({_SESSION_ID: in_index_node})
    journal_path = _MemoryJournal()
    summary = _migrator(session_index, journal_path).run()

    assert summary.migrated == (_SESSION_ID,)
    assert session_index.listed_session_ids == [_SESSION_ID]
    assert session_index.resolved_session_ids == [_SESSION_ID]
    # 索引外目录逐字节不变，journal 中无任何记录。
    assert orphan_manifest.read_bytes() == orphan_bytes_before
    assert _read_journal_records(journal_path).keys() == {_SESSION_ID}


def test_session_without_debug_data_is_skipped(
    session_tree: tuple[SessionPathResolver, Path, Path],
    journal_path: _MemoryJournal,
) -> None:
    """无调试数据的会话 → skipped，且重跑不重复记录。"""
    resolver, _legacy_dir, plain_dir = session_tree
    migrator = _migrator(resolver, journal_path)

    summary = migrator.run()

    assert summary.migrated == (_SESSION_ID,)
    assert summary.skipped == (_SESSION_ID_B,)
    assert (plain_dir / "debug").exists() is False
    records = _read_journal_records(journal_path)
    assert records[_SESSION_ID_B]["status"] == "skipped"
    assert records[_SESSION_ID_B]["note"] == "no-debug-data"

    # 重跑：skipped 记录保持，不产生新的台账写入。
    journal_bytes_before = journal_path.read_bytes()
    second = migrator.run()
    assert second.skipped == (_SESSION_ID_B,)
    assert second.migrated == ()
    assert journal_path.read_bytes() == journal_bytes_before


def test_journal_state_survives_reload(
    session_tree: tuple[SessionPathResolver, Path, Path],
    journal_path: _MemoryJournal,
) -> None:
    """journal 持久性：模拟重启（重新加载台账）→ migrated 状态保留 → 重跑 no-op。"""
    resolver, legacy_dir, _plain_dir = session_tree
    first_summary = _migrator(resolver, journal_path).run()
    assert first_summary.migrated == (_SESSION_ID,)
    manifest_bytes = (
        legacy_dir / "debug" / "node" / "manifest.json"
    ).read_bytes()

    # 模拟重启：全新的 migrator/journal 实例，仅靠磁盘台账恢复状态。
    reloaded_migrator = _migrator(resolver, journal_path)
    reloaded_summary = reloaded_migrator.run()

    assert reloaded_summary.noop == (_SESSION_ID,)
    assert reloaded_summary.migrated == ()
    assert reloaded_summary.failed == ()
    assert (
        legacy_dir / "debug" / "node" / "manifest.json"
    ).read_bytes() == manifest_bytes
    # 重载后的台账状态与新写入一致。
    records = _read_journal_records(journal_path)
    assert records[_SESSION_ID]["status"] == "migrated"


def test_configuration_files_bytes_unchanged_after_migration(
    session_tree: tuple[SessionPathResolver, Path, Path],
    journal_path: _MemoryJournal,
) -> None:
    """迁移后方案文件 bytes 不变（hash 对比），journal 登记相同 hash。"""
    resolver, legacy_dir, _plain_dir = session_tree
    configurations_dir = legacy_dir / "debug" / "node" / "configurations"
    configuration_paths = sorted(configurations_dir.glob("*.json"))
    hashes_before = {path.name: _sha256_of(path) for path in configuration_paths}
    sizes_before = {path.name: path.stat().st_size for path in configuration_paths}

    _migrator(resolver, journal_path).run()

    for path in configuration_paths:
        assert _sha256_of(path) == hashes_before[path.name]
        assert path.stat().st_size == sizes_before[path.name]
    records = _read_journal_records(journal_path)
    registered = {
        item["file"]: item["sha256"]
        for item in records[_SESSION_ID]["configurations"]
    }
    assert registered == hashes_before
