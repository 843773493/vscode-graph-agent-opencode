"""详情文件、安全边界和 Saver registry 的跨模块持久化合同。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
    DetailRecord,
    DetailUnavailableError,
    detail_record_from_mapping,
    detail_relative_path,
)

SESSION_ID = "ses_019c205391bb7a4b81b6728df70f63e6"
OTHER_SESSION_ID = "ses_019b9187b87f78c78ef242bbd6043bfc"


@pytest.fixture
def tmp_path(integration_workspace_root_path, request) -> Path:
    """会话数据与故障注入文件均放在正式 workspace 的独立 case 目录。"""
    node = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    target = Path(integration_workspace_root_path) / "detail-cases" / node
    target.mkdir(parents=True)
    return target


def _main_thread_id(sessions_dir: Path, session_id: str) -> str:
    return get_session_path_resolver(sessions_dir).main_thread_id(session_id)


def _thread_node(sessions_dir: Path, session_id: str) -> Path:
    resolver = get_session_path_resolver(sessions_dir)
    return resolver.resolve_thread_node(
        session_id, _main_thread_id(sessions_dir, session_id)
    )


def _session_rollout(sessions_dir: Path, session_id: str) -> Path:
    return _thread_node(sessions_dir, session_id) / "rollout"


def _initialize_rollout_owner(sessions_dir: Path, session_id: str) -> None:
    """经 Saver 初始化目标本地 rollout owner，供历史 scope 校验使用。"""
    thread_id = _main_thread_id(sessions_dir, session_id)
    with RolloutCheckpointSaver(sessions_dir) as saver:
        storage = saver._storage.storage
        storage.initialize(session_id, thread_id=thread_id)
        # 建立只读 SQLite 连接产生的 WAL sidecar 属于该目标的既有运行态；
        # 先用 Saver 正式读快照初始化它们，再比较被测拒绝路径前后的状态。
        with storage.open_read_snapshot(session_id, thread_id):
            pass


def _rollout_state(root: Path) -> dict[str, tuple[str, bytes | None]]:
    """记录目标 rollout 的目录与文件状态，验证拒绝读取没有副作用。"""
    return {
        path.relative_to(root).as_posix(): (
            ("directory", None) if path.is_dir() else ("file", path.read_bytes())
        )
        for path in sorted(root.rglob("*"))
    }


def test_detail_store_round_trip_uses_session_relative_assembly_path(
    detail_sessions: Path,
) -> None:
    store = ContextPlanDetailStore(detail_sessions)
    detail = {"blocks": [{"type": "text", "text": "请求详情"}]}

    record = store.write(
        session_id=SESSION_ID,
        thread_id=_main_thread_id(detail_sessions, SESSION_ID),
        assembly_id="assembly-1",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail=detail,
        required=True,
        source_revision="workspace-rev-1",
    )

    target = (
        _session_rollout(detail_sessions, SESSION_ID)
        / "context-plan-details"
        / (f"assembly-1/{record.detail_id}")
    )
    assert (
        record.relative_path
        == f"rollout/context-plan-details/assembly-1/{record.detail_id}"
    )
    assert target.is_file()
    assert (
        store.read(
            session_id=SESSION_ID,
            thread_id=_main_thread_id(detail_sessions, SESSION_ID),
            record=record,
        )["detail"]
        == detail
    )



@pytest.mark.parametrize("protection", ["public", "redacted", "protected"])
def test_normal_write_uses_current_typed_envelope(detail_sessions: Path, protection) -> None:
    store = ContextPlanDetailStore(detail_sessions)
    body = {"text": "normal-new-write"}
    record = store.write(
        session_id=SESSION_ID, thread_id=_main_thread_id(detail_sessions, SESSION_ID),
        assembly_id="normal-assembly", detail_kind="request_source",
        retention_class="request_replay", visibility="internal", detail=body,
        sensitive=protection != "public", protection=protection,
        source_revision="normal-producer",
    )
    payload = json.loads((_thread_node(detail_sessions, SESSION_ID) / record.relative_path).read_bytes())
    assert payload["format_version"] == 3
    assert payload["detail_ref"] == record.detail_ref.to_dict()
    assert payload["detail"] == body
    assert payload["detail_content_hash"] == sha256_jcs(body)
    assert sha256_jcs(payload) == record.content_hash
    assert store.read(
        session_id=SESSION_ID, thread_id=record.thread_id,
        record=record, include_sensitive=True,
    )["detail"] == body
    if protection != "public":
        with pytest.raises(PermissionError):
            store.read(session_id=SESSION_ID, thread_id=record.thread_id, record=record)


def test_sensitive_detail_persists_exact_body_and_requires_explicit_access(detail_sessions: Path) -> None:
    store = ContextPlanDetailStore(detail_sessions)
    body = {"secret": "sensitive-provider-prompt"}
    record = store.write(
        session_id=SESSION_ID, thread_id=_main_thread_id(detail_sessions, SESSION_ID),
        assembly_id="assembly-sensitive", detail_kind="request_source",
        retention_class="request_replay", visibility="internal", detail=body, sensitive=True,
    )
    target = _thread_node(detail_sessions, SESSION_ID) / record.relative_path
    assert json.loads(target.read_bytes())["detail"] == body
    with pytest.raises(PermissionError, match="未授权读取"):
        store.read(session_id=SESSION_ID, thread_id=record.thread_id, record=record)
    assert store.read(
        session_id=SESSION_ID, thread_id=record.thread_id, record=record,
        include_sensitive=True,
    )["detail"] == body


def test_sensitive_detail_survives_restart_and_rejects_body_tamper(detail_sessions: Path) -> None:
    body = {"secret": "精确正文"}
    store = ContextPlanDetailStore(detail_sessions)
    record = store.write(
        session_id=SESSION_ID, thread_id=_main_thread_id(detail_sessions, SESSION_ID),
        assembly_id="assembly-protected", detail_kind="request_source",
        retention_class="request_replay", visibility="internal", detail=body,
        required=True, sensitive=True, protection="protected", source_revision="secret-revision-1",
    )
    target = _thread_node(detail_sessions, SESSION_ID) / record.relative_path
    assert tuple(target.parent.iterdir()) == (target,)
    restarted = ContextPlanDetailStore(detail_sessions)
    assert restarted.read(
        session_id=SESSION_ID, thread_id=record.thread_id,
        record=record, include_sensitive=True,
    )["detail"] == body
    payload = json.loads(target.read_bytes())
    payload["detail"] = {"secret": "另一个正文"}
    target.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(DetailUnavailableError):
        restarted.read(
            session_id=SESSION_ID, thread_id=record.thread_id,
            record=record, include_sensitive=True,
        )


def test_detail_read_rejects_symlink_target(
    detail_sessions: Path,
    tmp_path: Path,
) -> None:
    store = ContextPlanDetailStore(detail_sessions)
    record = store.write(
        session_id=SESSION_ID,
        thread_id=_main_thread_id(detail_sessions, SESSION_ID),
        assembly_id="assembly-symlink",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail={"value": "safe"},
    )
    target = _thread_node(detail_sessions, SESSION_ID) / record.relative_path
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(DetailUnavailableError, match="符号链接"):
        store.read(
            session_id=SESSION_ID,
            thread_id=_main_thread_id(detail_sessions, SESSION_ID),
            record=record,
        )


def test_owner_gc_removes_expired_file_and_leaves_unavailable_tombstone(
    detail_sessions: Path,
) -> None:
    saver = RolloutCheckpointSaver(detail_sessions)
    record = saver._detail_store.write(
        session_id=SESSION_ID,
        thread_id=_main_thread_id(detail_sessions, SESSION_ID),
        assembly_id="assembly-gc",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail={"value": "过期详情"},
        retention_days=0,
    )
    saver._storage.assemblies.register_context_plan_detail(record)
    target = _thread_node(detail_sessions, SESSION_ID) / record.relative_path
    assert target.is_file()

    removed = saver.gc_context_plan_details(
        SESSION_ID,
        _main_thread_id(detail_sessions, SESSION_ID),
        expired_before=datetime.now(UTC) + timedelta(seconds=1),
    )

    assert removed == (record.detail_ref,)
    assert not target.exists()
    with sqlite3.connect(
        _session_rollout(detail_sessions, SESSION_ID) / "index.sqlite"
    ) as connection:
        status, availability = connection.execute(
            "SELECT status, availability FROM context_plan_details WHERE detail_ref = ?",
            (detail_ref_key(record.detail_ref),),
        ).fetchone()
    assert (status, availability) == ("unavailable", "unavailable")


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("required", 2),
        ("sensitive", 2),
        ("content_length", "not-a-length"),
        ("status", "corrupted"),
        ("protection", "corrupted"),
    ],
)
def test_detail_manifest_restore_rejects_coerced_or_unknown_values(
    detail_sessions: Path,
    column: str,
    value: object,
) -> None:
    saver = RolloutCheckpointSaver(detail_sessions)
    record = saver._detail_store.write(
        session_id=SESSION_ID,
        thread_id=_main_thread_id(detail_sessions, SESSION_ID),
        assembly_id="assembly-corrupt-manifest",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail={"value": "safe"},
    )
    saver._storage.assemblies.register_context_plan_detail(record)

    with sqlite3.connect(
        _session_rollout(detail_sessions, SESSION_ID) / "index.sqlite"
    ) as connection:
        connection.execute(
            f"UPDATE context_plan_details SET {column} = ? WHERE detail_ref = ?",
            (value, detail_ref_key(record.detail_ref)),
        )
        connection.commit()

    # SQLite registry 会先拒绝非法基础列；其余保护语义由 detail manifest 拒绝。
    with pytest.raises(RuntimeError, match="detail manifest|context_plan_details"):
        saver.read_context_plan_detail(
            SESSION_ID,
            _main_thread_id(detail_sessions, SESSION_ID),
            detail_ref=record.detail_ref,
        )


@pytest.fixture
def detail_sessions(tmp_path: Path, session_bundle_factory) -> Path:
    sessions = tmp_path / "workspace" / ".boxteam" / "sessions"
    session_bundle_factory(sessions, SESSION_ID)
    session_bundle_factory(sessions, OTHER_SESSION_ID)
    return sessions


@pytest.fixture
def detail_arguments(detail_sessions: Path) -> dict[str, object]:
    return {
        "session_id": SESSION_ID,
        "thread_id": _main_thread_id(detail_sessions, SESSION_ID),
        "assembly_id": "assembly-detail",
        "detail_kind": "request_source",
        "retention_class": "request_replay",
        "visibility": "internal",
        "detail": {"text": "请求正文"},
        "source_revision": "producer-revision-1",
    }


@pytest.fixture
def detail_store(detail_sessions: Path) -> ContextPlanDetailStore:
    return ContextPlanDetailStore(detail_sessions)


@pytest.fixture
def public_record(
    detail_store: ContextPlanDetailStore, detail_arguments
) -> DetailRecord:
    return detail_store.write(**detail_arguments)


@pytest.fixture
def typed_manifest(public_record: DetailRecord) -> dict[str, object]:
    return {
        **asdict(public_record),
        "detail_ref": public_record.detail_ref,
        "required": int(public_record.required),
        "sensitive": int(public_record.sensitive),
    }


def test_typed_record_and_registry_roundtrip(
    detail_sessions, detail_store, public_record, typed_manifest
):
    ref = public_record.detail_ref
    assert ref == DetailRef(
        SESSION_ID,
        _main_thread_id(detail_sessions, SESSION_ID),
        "assembly-detail",
        public_record.detail_id,
    )
    assert "detail_ref" not in asdict(public_record)
    assert not hasattr(public_record, "content_length")
    assert not hasattr(public_record, "gc_after")
    assert detail_record_from_mapping(typed_manifest) == public_record
    assert detail_relative_path(ref).parts == (
        "rollout",
        "context-plan-details",
        "assembly-detail",
        public_record.detail_id,
    )
    saver = RolloutCheckpointSaver(detail_sessions)
    saver._storage.assemblies.register_context_plan_detail(public_record)
    restored = saver._storage.assemblies.get_context_plan_detail(
        SESSION_ID, ref.thread_id, detail_ref=ref
    )
    assert restored["detail_ref"] == ref
    assert detail_record_from_mapping(restored) == public_record
    assert saver.read_context_plan_detail(
        SESSION_ID, ref.thread_id, detail_ref=ref
    ) == detail_store.read(
        session_id=SESSION_ID,
        thread_id=_main_thread_id(detail_sessions, SESSION_ID),
        record=public_record,
    )
    with pytest.raises((AttributeError, TypeError)):
        public_record.detail_ref = DetailRef(
            OTHER_SESSION_ID, ref.thread_id, ref.assembly_id, ref.detail_id
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("detail_ref", "legacy-id"),
        ("detail_ref", {"session_id": SESSION_ID}),
        (
            "detail_ref",
            DetailRef(
                OTHER_SESSION_ID,
                "thread-other",
                "assembly-detail",
                "other",
            ),
        ),
        ("detail_id", "other"),
        ("assembly_id", "other"),
        ("session_id", OTHER_SESSION_ID),
        ("relative_path", "rollout/context-plan-details/assembly-detail/legacy.json"),
        ("content_length", 1),
        ("gc_after", None),
        ("detail_kind", ""),
        ("retention_class", ""),
        ("visibility", "secret"),
        ("visibility", []),
        ("protection", []),
        ("status", []),
        ("length", True),
        ("length", 1.0),
        ("length", -1),
        ("required", True),
        ("required", "1"),
        ("sensitive", 2),
        ("expires_at", "2026-09-01T00:00:00"),
        ("expires_at", "not-a-time"),
        ("availability", "unavailable"),
        ("removed_field", "unexpected"),
    ],
)
def test_registry_mapping_rejects_old_coerced_or_mismatched_fields(
    typed_manifest, field, value
):
    with pytest.raises(DetailUnavailableError):
        detail_record_from_mapping({**typed_manifest, field: value})


@pytest.mark.parametrize(
    "field",
    [
        "detail_id",
        "detail_kind",
        "retention_class",
        "visibility",
        "length",
        "expires_at",
    ],
)
def test_registry_mapping_requires_all_new_fields(typed_manifest, field):
    del typed_manifest[field]
    with pytest.raises(DetailUnavailableError, match="缺少字段"):
        detail_record_from_mapping(typed_manifest)


@pytest.mark.parametrize("field", ["detail_kind", "retention_class", "visibility"])
def test_write_has_no_classification_defaults(detail_store, detail_arguments, field):
    del detail_arguments[field]
    with pytest.raises(TypeError, match=field):
        detail_store.write(**detail_arguments)


@pytest.mark.parametrize("value", ["detail-legacy", {"session_id": SESSION_ID}, None])
def test_locator_only_accepts_typed_identity(value):
    with pytest.raises(TypeError, match="DetailRef"):
        detail_relative_path(value)


@pytest.mark.parametrize("operation", ["read", "remove", "gc"])
def test_owner_failure_cannot_touch_either_session(
    detail_sessions, detail_store, public_record, operation
):
    root = _thread_node(detail_sessions, SESSION_ID)
    target = root / public_record.relative_path
    original = target.read_bytes()
    second = _thread_node(detail_sessions, OTHER_SESSION_ID)
    second_rollout = second / "rollout"
    if operation in {"read", "gc"}:
        # 历史 ref 必须在真实目标 rollout 中做本地 binding 校验；空目录会把
        # 未授权误报成 SQLite 打开失败，无法证明 source-mismatch 合同。
        _initialize_rollout_owner(detail_sessions, OTHER_SESSION_ID)
        assert (second_rollout / "index.sqlite").is_file()
        second_rollout_before = _rollout_state(second_rollout)
    else:
        assert not second_rollout.exists()
    with pytest.raises(ValueError, match="source-mismatch"):
        if operation == "gc":
            detail_store.gc(
                session_id=OTHER_SESSION_ID,
                thread_id=_main_thread_id(detail_sessions, OTHER_SESSION_ID),
                expired_before=datetime.now(UTC),
                allowed_refs=[public_record.detail_ref],
            )
        else:
            getattr(detail_store, operation)(
                session_id=OTHER_SESSION_ID,
                thread_id=_main_thread_id(detail_sessions, OTHER_SESSION_ID),
                record=public_record,
            )
    assert target.read_bytes() == original
    if operation in {"read", "gc"}:
        assert _rollout_state(second_rollout) == second_rollout_before
    else:
        assert not second_rollout.exists()


@pytest.mark.parametrize(
    "body,value_type",
    [
        (None, "null"),
        (True, "boolean"),
        (1, "number"),
        ("", "string"),
        ([], "array"),
        ({}, "object"),
        ({"secret": "令牌"}, "object"),
    ],
)
def test_sensitive_exact_body_hash_is_owner_independent(
    detail_sessions, detail_store, detail_arguments, body, value_type
):
    arguments = {**detail_arguments, "detail": body, "sensitive": True}
    first = detail_store.write(**arguments)
    restarted = ContextPlanDetailStore(detail_sessions)
    same = restarted.write(**arguments)
    other = restarted.write(**{
        **arguments, "session_id": OTHER_SESSION_ID,
        "thread_id": _main_thread_id(detail_sessions, OTHER_SESSION_ID),
    })
    for record in (first, same, other):
        payload = restarted.read(
            session_id=record.session_id, thread_id=record.thread_id,
            record=record, include_sensitive=True,
        )
        assert payload["detail"] == body
        assert payload["detail_content_hash"] == sha256_jcs(body)
        assert payload["length"] == record.length == len(canonical_json_bytes(body))
        assert payload["detail_ref"] == record.detail_ref.to_dict()
    assert value_type in {"null", "boolean", "number", "string", "array", "object"}
    assert first.detail_ref != other.detail_ref


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", OTHER_SESSION_ID),
        ("assembly_id", "other-assembly"),
        ("detail_id", "other-detail"),
        ("detail_kind", "assembly_snapshot"),
        ("retention_class", "assembly_audit"),
        ("visibility", "private"),
        ("length", 999),
        ("source_revision", "different-revision"),
        ("expires_at", None),
        ("checkpoint_ns", "branch"),
        ("required", True),
        ("content_hash", "different-envelope-hash"),
    ],
)
def test_detail_binds_typed_owner_and_manifest(
    detail_sessions, detail_store, detail_arguments, field, value
):
    record = detail_store.write(**detail_arguments, sensitive=True)
    changes = {field: value}
    if field in {"assembly_id", "detail_id"}:
        changes["relative_path"] = detail_relative_path(
            DetailRef.from_dict({**record.detail_ref.to_dict(), field: value})
        ).as_posix()
    different = replace(record, **changes)
    if field == "session_id":
        # 历史 ref 只能通过目标 rollout 的冻结 binding 读取；此 fixture 没有
        # 捕获该历史 scope，因此按新的 source-mismatch 合同拒绝。
        _initialize_rollout_owner(detail_sessions, SESSION_ID)
        expected_error = ValueError
        expected_message = (
            "source-mismatch: 历史 SessionThread 不属于目标本地冻结 snapshot"
        )
    else:
        expected_error = DetailUnavailableError
        expected_message = None
    with pytest.raises(expected_error, match=expected_message):
        detail_store.read(
            session_id=SESSION_ID, thread_id=record.thread_id,
            record=different, include_sensitive=True,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("format_version", 1),
        ("detail_ref", "legacy-id"),
        ("length", 1),
        ("detail_kind", "assembly_snapshot"),
        ("visibility", "private"),
        ("expires_at", None),
        ("detail", {"text": "被替换"}),
        ("extra", 1),
    ],
)
def test_read_rejects_corrupt_envelope(
    detail_sessions, detail_store, public_record, field, value
):
    root = _thread_node(detail_sessions, SESSION_ID)
    target = root / public_record.relative_path
    payload = json.loads(target.read_bytes())
    payload[field] = value
    target.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(DetailUnavailableError):
        detail_store.read(
            session_id=SESSION_ID,
            thread_id=_main_thread_id(detail_sessions, SESSION_ID),
            record=public_record,
        )


def test_expired_read_fails_and_gc_requires_explicit_typed_allowlist(
    detail_sessions, detail_store, detail_arguments
):
    expired = detail_store.write(**detail_arguments, retention_days=0)
    retained = detail_store.write(**detail_arguments)
    root = _thread_node(detail_sessions, SESSION_ID)
    target = root / expired.relative_path
    with pytest.raises(DetailUnavailableError, match="已过期"):
        detail_store.read(
            session_id=SESSION_ID,
            thread_id=_main_thread_id(detail_sessions, SESSION_ID),
            record=expired,
        )
    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    with pytest.raises(TypeError, match="allowed_refs"):
        detail_store.gc(
            session_id=SESSION_ID,
            thread_id=_main_thread_id(detail_sessions, SESSION_ID),
            expired_before=cutoff,
        )
    with pytest.raises(TypeError, match="DetailRef"):
        detail_store.gc(
            session_id=SESSION_ID,
            thread_id=_main_thread_id(detail_sessions, SESSION_ID),
            expired_before=cutoff,
            allowed_refs=[expired.detail_id],
        )
    with pytest.raises(ValueError, match="时区"):
        detail_store.gc(
            session_id=SESSION_ID,
            thread_id=_main_thread_id(detail_sessions, SESSION_ID),
            expired_before=cutoff.replace(tzinfo=None),
            allowed_refs=[expired.detail_ref],
        )
    assert (
        detail_store.gc(
            session_id=SESSION_ID,
            thread_id=_main_thread_id(detail_sessions, SESSION_ID),
            expired_before=cutoff,
            allowed_refs=[],
        )
        == ()
    )
    assert target.exists()
    assert detail_store.gc(
        session_id=SESSION_ID,
        thread_id=_main_thread_id(detail_sessions, SESSION_ID),
        expired_before=cutoff,
        allowed_refs=[expired.detail_ref, retained.detail_ref, expired.detail_ref],
    ) == (expired.detail_ref,)
    assert not target.exists()
    assert (root / retained.relative_path).is_file()


@pytest.mark.parametrize("missing", ["none", "body"])
def test_gc_retries_tombstoned_exact_files_without_source_sharing(
    detail_sessions, detail_store, detail_arguments, missing
):
    expired = detail_store.write(**detail_arguments, sensitive=True, retention_days=0)
    independent = detail_store.write(
        **{**detail_arguments, "assembly_id": "other-assembly"}, sensitive=True,
    )
    target = _thread_node(detail_sessions, SESSION_ID) / expired.relative_path
    if missing == "body":
        target.unlink()
    removed = detail_store.gc(
        session_id=SESSION_ID, thread_id=expired.thread_id,
        expired_before=datetime.now(UTC), allowed_refs=[expired.detail_ref],
    )
    assert removed == (() if missing == "body" else (expired.detail_ref,))
    assert not target.exists()
    assert detail_store.read(
        session_id=SESSION_ID, thread_id=independent.thread_id,
        record=independent, include_sensitive=True,
    )["detail"] == detail_arguments["detail"]


@pytest.mark.parametrize("operation", ["read", "remove", "gc"])
@pytest.mark.parametrize("location", ["body", "parent"])
def test_symlink_preflight_preserves_other_files(
    detail_sessions, detail_store, detail_arguments, tmp_path, operation, location
):
    record = detail_store.write(**detail_arguments, sensitive=True)
    root = _thread_node(detail_sessions, SESSION_ID)
    manifest = root / record.relative_path
    victim = manifest if location == "body" else manifest.parent
    saved = tmp_path / "artifacts" / "original"
    saved.parent.mkdir()
    victim.rename(saved)
    victim.symlink_to(saved, target_is_directory=location == "parent")
    manifest_exists = manifest.exists()
    with pytest.raises(DetailUnavailableError, match="符号链接"):
        if operation == "gc":
            detail_store.gc(
                session_id=SESSION_ID,
                thread_id=_main_thread_id(detail_sessions, SESSION_ID),
                expired_before=datetime.now(UTC) + timedelta(days=31),
                allowed_refs=[record.detail_ref],
            )
        else:
            options = {"include_sensitive": True} if operation == "read" else {}
            getattr(detail_store, operation)(
                session_id=SESSION_ID,
                thread_id=_main_thread_id(detail_sessions, SESSION_ID),
                record=record,
                **options,
            )
    assert victim.is_symlink() and saved.exists()
    assert manifest.exists() == manifest_exists




@pytest.mark.parametrize("failure", ["collision", "symlink", "io"])
def test_publish_failure_preserves_existing_target(
    detail_sessions, detail_store, detail_arguments, tmp_path, monkeypatch, failure,
):
    original_publish = detail_store._files.publish
    attempted = []
    outside = tmp_path / "artifacts" / "existing-body"
    outside.parent.mkdir()
    outside.write_bytes(b"existing-owner-content")

    def fail_publish(ref, raw, *, creation_record=None):
        attempted.append(ref)
        target = detail_store._files.path(ref, create=True)
        if failure == "collision":
            target.write_bytes(b"existing-owner-content")
        elif failure == "symlink":
            target.symlink_to(outside)
        else:
            raise OSError("injected publication failure")
        return original_publish(ref, raw, creation_record=creation_record)

    monkeypatch.setattr(detail_store._files, "publish", fail_publish)
    with pytest.raises((DetailUnavailableError, OSError)):
        detail_store.write(**detail_arguments, sensitive=True)
    assert len(attempted) == 1
    root = _thread_node(detail_sessions, SESSION_ID)
    target = root / detail_relative_path(attempted[0])
    assert not list(root.rglob("*.tmp"))
    if failure != "io":
        assert target.read_bytes() == b"existing-owner-content"
        assert target.is_symlink() == (failure == "symlink")
    assert outside.read_bytes() == b"existing-owner-content"


@pytest.mark.parametrize("field", ["assembly_id", "session_id"])
def test_same_leaf_copy_cannot_replay_another_typed_owner(
    detail_sessions,
    detail_store,
    detail_arguments,
    field,
):
    record = detail_store.write(**detail_arguments, sensitive=True)
    identity = {
        **record.detail_ref.to_dict(),
        field: "other-assembly" if field == "assembly_id" else OTHER_SESSION_ID,
    }
    if field == "session_id":
        identity["thread_id"] = _main_thread_id(detail_sessions, OTHER_SESSION_ID)
    ref = DetailRef.from_dict(identity)
    changes = {
        field: identity[field],
        "relative_path": detail_relative_path(ref).as_posix(),
    }
    if field == "session_id":
        changes["thread_id"] = identity["thread_id"]
    different = replace(record, **changes)
    assert different.detail_id == record.detail_id
    raw = detail_store._files.read(
        record.detail_ref,
        session_id=SESSION_ID,
        thread_id=_main_thread_id(detail_sessions, SESSION_ID),
    )
    detail_store._files.publish(ref, raw)
    with pytest.raises(DetailUnavailableError, match="source-mismatch"):
        detail_store.read(
            session_id=ref.session_id,
            thread_id=ref.thread_id,
            record=different,
            include_sensitive=True,
        )
    detail_store.remove(
        session_id=ref.session_id, thread_id=ref.thread_id, record=different
    )
    assert (
        detail_store.read(
            session_id=SESSION_ID,
            thread_id=_main_thread_id(detail_sessions, SESSION_ID),
            record=record,
            include_sensitive=True,
        )["detail"]
        == detail_arguments["detail"]
    )


@pytest.mark.parametrize(
    "raw", [b"{", b"[]", b"{}", b'{"format_version":NaN}', b"\xff"]
)
def test_invalid_file_gc_fails_without_deleting(
    detail_sessions, detail_store, detail_arguments, raw
):
    record = detail_store.write(**detail_arguments, retention_days=0)
    root = _thread_node(detail_sessions, SESSION_ID)
    target = root / record.relative_path
    target.write_bytes(raw)
    with pytest.raises(DetailUnavailableError):
        detail_store.gc(
            session_id=SESSION_ID,
            thread_id=_main_thread_id(detail_sessions, SESSION_ID),
            expired_before=datetime.now(UTC),
            allowed_refs=[record.detail_ref],
        )
    assert target.read_bytes() == raw


def test_missing_body_fails_without_recreating_paths(
    detail_sessions, detail_store, detail_arguments,
):
    record = detail_store.write(**detail_arguments, sensitive=True)
    target = detail_store._files.path(record.detail_ref)
    target.unlink()
    target.parent.rmdir()
    with pytest.raises(DetailUnavailableError, match="缺失"):
        detail_store.read(
            session_id=SESSION_ID, thread_id=record.thread_id,
            record=record, include_sensitive=True,
        )
    assert not target.parent.exists()
