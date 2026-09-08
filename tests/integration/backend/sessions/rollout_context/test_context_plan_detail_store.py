"""详情文件、安全边界和 Saver registry 的跨模块持久化合同。"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
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
    protected_detail_relative_path,
)
from app.services.infrastructure.rollout_context.runtime.protected_detail import (
    ProtectedDetailBackend,
    ProtectedDetailError,
)


@pytest.fixture
def tmp_path(integration_workspace_root_path, request) -> Path:
    """会话数据与故障注入文件均放在正式 workspace 的独立 case 目录。"""
    node = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    target = Path(integration_workspace_root_path) / "detail-cases" / node
    target.mkdir(parents=True)
    return target


def _session_rollout(sessions_dir: Path, session_id: str) -> Path:
    return (
        get_session_path_resolver(sessions_dir).resolve_session_node(session_id)
        / "rollout"
    )


def test_detail_store_round_trip_uses_session_relative_assembly_path(
    detail_sessions: Path,
) -> None:
    store = ContextPlanDetailStore(detail_sessions)
    detail = {"blocks": [{"type": "text", "text": "请求详情"}]}

    record = store.write(
        session_id="session_1",
        assembly_id="assembly-1",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail=detail,
        required=True,
        source_revision="workspace-rev-1",
    )

    target = (
        _session_rollout(detail_sessions, "session_1")
        / "context-plan-details"
        / (f"assembly-1/{record.detail_id}")
    )
    assert (
        record.relative_path
        == f"rollout/context-plan-details/assembly-1/{record.detail_id}"
    )
    assert target.is_file()
    assert store.read(session_id="session_1", record=record)["detail"] == detail


def test_sensitive_detail_never_writes_plaintext_or_claims_protected_body(
    detail_sessions: Path,
) -> None:
    store = ContextPlanDetailStore(detail_sessions)
    secret = "sensitive-provider-prompt"
    record = store.write(
        session_id="session_1",
        assembly_id="assembly-sensitive",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail={"secret": secret},
        sensitive=True,
    )
    target = (
        get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
        / record.relative_path
    )
    assert secret.encode() not in target.read_bytes()
    with pytest.raises(PermissionError, match="未授权读取"):
        store.read(session_id="session_1", record=record)
    with pytest.raises(DetailUnavailableError, match="没有 protected body"):
        store.read(session_id="session_1", record=record, include_sensitive=True)


def test_protected_detail_round_trip_survives_restart_and_rejects_wrong_key(
    detail_sessions: Path,
) -> None:
    key = b"0123456789abcdef0123456789abcdef"
    store = ContextPlanDetailStore(detail_sessions, protected_key=key)
    detail = {"secret": "必须进入 provider，但不能进入普通 manifest"}
    record = store.write(
        session_id="session_1",
        assembly_id="assembly-protected",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail=detail,
        required=True,
        sensitive=True,
        protection="protected",
        source_revision="secret-revision-1",
    )

    session_root = get_session_path_resolver(detail_sessions).resolve_session_node(
        "session_1"
    )
    manifest = session_root / record.relative_path
    protected = session_root / protected_detail_relative_path(
        record.detail_ref,
    )
    assert record.protection == "protected"
    assert manifest.is_file()
    assert protected.is_file()
    assert b"provider" not in manifest.read_bytes()
    assert detail["secret"].encode() not in manifest.read_bytes()
    assert detail["secret"].encode() not in protected.read_bytes()
    assert (
        store.read(
            session_id="session_1",
            record=record,
            include_sensitive=True,
        )["detail"]
        == detail
    )

    restarted = ContextPlanDetailStore(detail_sessions, protected_key=key)
    assert (
        restarted.read(
            session_id="session_1",
            record=record,
            include_sensitive=True,
        )["detail"]
        == detail
    )

    assert store._protected_backend is not None
    altered = {"secret": "另一个正文"}
    protected.write_bytes(
        store._protected_backend.encrypt(
            record=record,
            detail=altered,
            detail_content_hash=sha256_jcs(altered),
        )
    )
    with pytest.raises(DetailUnavailableError, match="provenance"):
        restarted.read(
            session_id="session_1",
            record=record,
            include_sensitive=True,
        )

    wrong_key = ContextPlanDetailStore(
        detail_sessions,
        protected_key=b"fedcba9876543210fedcba9876543210",
    )
    with pytest.raises(DetailUnavailableError, match="校验失败"):
        wrong_key.read(
            session_id="session_1",
            record=record,
            include_sensitive=True,
        )

    protected.write_bytes(protected.read_bytes()[:-1] + b"x")
    with pytest.raises(DetailUnavailableError, match="校验失败"):
        restarted.read(
            session_id="session_1",
            record=record,
            include_sensitive=True,
        )


def test_detail_read_rejects_symlink_target(
    detail_sessions: Path,
    tmp_path: Path,
) -> None:
    store = ContextPlanDetailStore(detail_sessions)
    record = store.write(
        session_id="session_1",
        assembly_id="assembly-symlink",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail={"value": "safe"},
    )
    target = (
        get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
        / record.relative_path
    )
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(DetailUnavailableError, match="符号链接"):
        store.read(session_id="session_1", record=record)


def test_owner_gc_removes_expired_file_and_leaves_unavailable_tombstone(
    detail_sessions: Path,
) -> None:
    saver = RolloutCheckpointSaver(detail_sessions)
    record = saver._detail_store.write(
        session_id="session_1",
        assembly_id="assembly-gc",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail={"value": "过期详情"},
        retention_days=0,
    )
    saver._storage.register_context_plan_detail(record)
    target = (
        get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
        / record.relative_path
    )
    assert target.is_file()

    removed = saver.gc_context_plan_details(
        "session_1",
        expired_before=datetime.now(UTC) + timedelta(seconds=1),
    )

    assert removed == (record.detail_ref,)
    assert not target.exists()
    with sqlite3.connect(
        _session_rollout(detail_sessions, "session_1") / "index.sqlite"
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
        session_id="session_1",
        assembly_id="assembly-corrupt-manifest",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail={"value": "safe"},
    )
    saver._storage.register_context_plan_detail(record)

    with sqlite3.connect(
        _session_rollout(detail_sessions, "session_1") / "index.sqlite"
    ) as connection:
        connection.execute(
            f"UPDATE context_plan_details SET {column} = ? WHERE detail_ref = ?",
            (value, detail_ref_key(record.detail_ref)),
        )
        connection.commit()

    # SQLite registry 会先拒绝非法基础列；其余保护语义由 detail manifest 拒绝。
    with pytest.raises(RuntimeError, match="detail manifest|context_plan_details"):
        saver.read_context_plan_detail(
            "session_1",
            detail_ref=record.detail_ref,
        )


@pytest.fixture
def detail_sessions(tmp_path: Path, session_bundle_factory) -> Path:
    sessions = tmp_path / "workspace" / ".boxteam" / "sessions"
    session_bundle_factory(sessions, "session_1")
    session_bundle_factory(sessions, "session_2")
    return sessions


@pytest.fixture
def detail_arguments() -> dict[str, object]:
    return {
        "session_id": "session_1",
        "assembly_id": "assembly-detail",
        "detail_kind": "request_source",
        "retention_class": "request_replay",
        "visibility": "internal",
        "detail": {"text": "请求正文"},
        "source_revision": "producer-revision-1",
    }


@pytest.fixture
def detail_store(detail_sessions: Path) -> ContextPlanDetailStore:
    return ContextPlanDetailStore(detail_sessions, protected_key=b"k" * 32)


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
    assert ref == DetailRef("session_1", "assembly-detail", public_record.detail_id)
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
    saver = RolloutCheckpointSaver(detail_sessions, protected_detail_key=b"k" * 32)
    saver._storage.register_context_plan_detail(public_record)
    restored = saver._storage.get_context_plan_detail("session_1", detail_ref=ref)
    assert restored["detail_ref"] == ref
    assert detail_record_from_mapping(restored) == public_record
    assert saver.read_context_plan_detail(
        "session_1", detail_ref=ref
    ) == detail_store.read(
        session_id="session_1",
        record=public_record,
    )
    with pytest.raises((AttributeError, TypeError)):
        public_record.detail_ref = DetailRef(
            "session_2", ref.assembly_id, ref.detail_id
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("detail_ref", "legacy-id"),
        ("detail_ref", {"session_id": "session_1"}),
        ("detail_ref", DetailRef("session_2", "assembly-detail", "other")),
        ("detail_id", "other"),
        ("assembly_id", "other"),
        ("session_id", "session_2"),
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
        ("redacted_stable_digest", "unexpected"),
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


@pytest.mark.parametrize("value", ["detail-legacy", {"session_id": "session_1"}, None])
def test_locator_only_accepts_typed_identity(value):
    with pytest.raises(TypeError, match="DetailRef"):
        detail_relative_path(value)
    with pytest.raises(TypeError, match="DetailRef"):
        protected_detail_relative_path(value)


@pytest.mark.parametrize("operation", ["read", "remove", "gc"])
def test_owner_failure_cannot_touch_either_session(
    detail_sessions, detail_store, public_record, operation
):
    root = get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
    target = root / public_record.relative_path
    original = target.read_bytes()
    with pytest.raises(ValueError, match="source-mismatch"):
        if operation == "gc":
            detail_store.gc(
                session_id="session_2",
                expired_before=datetime.now(UTC),
                allowed_refs=[public_record.detail_ref],
            )
        else:
            getattr(detail_store, operation)(
                session_id="session_2", record=public_record
            )
    assert target.read_bytes() == original
    second = get_session_path_resolver(detail_sessions).resolve_session_node(
        "session_2"
    )
    assert not (second / "rollout").exists()


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
def test_sensitive_marker_keeps_session_v1_algorithm_for_all_json_types(
    detail_sessions, detail_store, detail_arguments, body, value_type
):
    arguments = {**detail_arguments, "detail": body, "sensitive": True}
    first = detail_store.write(**arguments)
    restarted = ContextPlanDetailStore(detail_sessions, protected_key=b"k" * 32)
    same = restarted.write(**arguments)
    other = restarted.write(**{**arguments, "session_id": "session_2"})
    root = get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
    key = (root / "rollout" / ".context-redaction-key").read_bytes()
    expected = (
        "hmac-sha256:session:v1:"
        + hmac.new(key, canonical_json_bytes(body), hashlib.sha256).hexdigest()
    )
    assert first.redacted_stable_digest == same.redacted_stable_digest == expected
    assert other.redacted_stable_digest != expected
    raw = json.loads((root / first.relative_path).read_bytes())
    assert raw["detail"] == {
        "redacted": True,
        "redacted_stable_digest": expected,
        "value_type": value_type,
        "detail_kind": "request_source",
        "length": len(canonical_json_bytes(body)),
    }
    assert raw["detail_content_hash"] is None
    assert raw["length"] == first.length == len(canonical_json_bytes(body))
    assert raw["detail_ref"] == first.detail_ref.to_dict()
    assert (
        restarted.read(session_id="session_1", record=first, include_sensitive=True)[
            "detail"
        ]
        == body
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "session_2"),
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
def test_protected_aad_binds_typed_owner_and_manifest(
    detail_sessions, detail_store, detail_arguments, field, value
):
    record = detail_store.write(**detail_arguments, sensitive=True)
    root = get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
    blob = (root / protected_detail_relative_path(record.detail_ref)).read_bytes()
    changes = {field: value}
    if field in {"assembly_id", "detail_id"}:
        identity = {**record.detail_ref.to_dict(), field: value}
        changes["relative_path"] = detail_relative_path(
            DetailRef.from_dict(identity)
        ).as_posix()
    different = replace(record, **changes)
    backend = ProtectedDetailBackend(b"k" * 32)
    with pytest.raises(ProtectedDetailError, match="authentication"):
        backend.decrypt(blob, record=different)


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
    root = get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
    target = root / public_record.relative_path
    payload = json.loads(target.read_bytes())
    payload[field] = value
    target.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(DetailUnavailableError):
        detail_store.read(session_id="session_1", record=public_record)


def test_expired_read_fails_and_gc_requires_explicit_typed_allowlist(
    detail_sessions, detail_store, detail_arguments
):
    expired = detail_store.write(**detail_arguments, retention_days=0)
    retained = detail_store.write(**detail_arguments)
    root = get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
    target = root / expired.relative_path
    with pytest.raises(DetailUnavailableError, match="已过期"):
        detail_store.read(session_id="session_1", record=expired)
    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    with pytest.raises(TypeError, match="allowed_refs"):
        detail_store.gc(session_id="session_1", expired_before=cutoff)
    with pytest.raises(TypeError, match="DetailRef"):
        detail_store.gc(
            session_id="session_1",
            expired_before=cutoff,
            allowed_refs=[expired.detail_id],
        )
    with pytest.raises(ValueError, match="时区"):
        detail_store.gc(
            session_id="session_1",
            expired_before=cutoff.replace(tzinfo=None),
            allowed_refs=[expired.detail_ref],
        )
    assert (
        detail_store.gc(session_id="session_1", expired_before=cutoff, allowed_refs=[])
        == ()
    )
    assert target.exists()
    assert detail_store.gc(
        session_id="session_1",
        expired_before=cutoff,
        allowed_refs=[expired.detail_ref, retained.detail_ref, expired.detail_ref],
    ) == (expired.detail_ref,)
    assert not target.exists()
    assert (root / retained.relative_path).is_file()


@pytest.mark.parametrize("missing", ["none", "manifest", "protected", "both"])
def test_gc_retries_tombstoned_exact_files_without_source_sharing(
    detail_sessions, detail_store, detail_arguments, missing
):
    expired = detail_store.write(**detail_arguments, sensitive=True, retention_days=0)
    independent = detail_store.write(
        **{**detail_arguments, "assembly_id": "other-assembly"}, sensitive=True
    )
    root = get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
    manifest = root / expired.relative_path
    protected = root / protected_detail_relative_path(expired.detail_ref)
    if missing in {"manifest", "both"}:
        manifest.unlink()
    if missing in {"protected", "both"}:
        protected.unlink()
    removed = detail_store.gc(
        session_id="session_1",
        expired_before=datetime.now(UTC),
        allowed_refs=[expired.detail_ref],
    )
    assert removed == (() if missing == "both" else (expired.detail_ref,))
    assert not manifest.exists() and not protected.exists()
    assert (
        detail_store.read(
            session_id="session_1", record=independent, include_sensitive=True
        )["detail"]
        == detail_arguments["detail"]
    )


@pytest.mark.parametrize("operation", ["read", "remove", "gc"])
@pytest.mark.parametrize("location", ["manifest", "protected", "parent"])
def test_symlink_preflight_preserves_other_files(
    detail_sessions, detail_store, detail_arguments, tmp_path, operation, location
):
    record = detail_store.write(**detail_arguments, sensitive=True)
    root = get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
    manifest = root / record.relative_path
    protected = root / protected_detail_relative_path(record.detail_ref)
    victim = manifest if location == "manifest" else protected
    if location == "parent":
        victim = protected.parent
    saved = tmp_path / "artifacts" / "original"
    saved.parent.mkdir()
    victim.rename(saved)
    victim.symlink_to(saved, target_is_directory=location == "parent")
    manifest_exists = manifest.exists()
    with pytest.raises(DetailUnavailableError, match="符号链接"):
        if operation == "gc":
            detail_store.gc(
                session_id="session_1",
                expired_before=datetime.now(UTC) + timedelta(days=31),
                allowed_refs=[record.detail_ref],
            )
        else:
            options = {"include_sensitive": True} if operation == "read" else {}
            getattr(detail_store, operation)(
                session_id="session_1", record=record, **options
            )
    assert victim.is_symlink() and saved.exists()
    assert manifest.exists() == manifest_exists


def test_missing_key_is_not_regenerated_during_read(
    detail_sessions, detail_store, detail_arguments
):
    record = detail_store.write(**detail_arguments, sensitive=True)
    root = get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
    key_path = root / "rollout" / ".context-redaction-key"
    key_path.unlink()
    with pytest.raises(DetailUnavailableError, match="key 不存在"):
        detail_store.read(session_id="session_1", record=record, include_sensitive=True)
    assert not key_path.exists()


@pytest.mark.parametrize("failure", ["collision", "symlink", "io"])
def test_publish_failure_preserves_existing_target_and_rolls_back_own_cipher(
    detail_sessions,
    detail_store,
    detail_arguments,
    tmp_path,
    monkeypatch,
    failure,
):
    original_publish = detail_store._files.publish
    attempted = []
    outside = tmp_path / "artifacts" / "existing-body"
    outside.parent.mkdir()
    outside.write_bytes(b"existing-owner-content")

    def fail_public(ref, raw, *, protected=False):
        if not protected:
            attempted.append(ref)
            target = detail_store._files.path(ref, create=True)
            if failure == "collision":
                target.write_bytes(b"existing-owner-content")
            elif failure == "symlink":
                target.symlink_to(outside)
            else:
                raise OSError("injected publication failure")
        return original_publish(ref, raw, protected=protected)

    monkeypatch.setattr(detail_store._files, "publish", fail_public)
    with pytest.raises((DetailUnavailableError, OSError)):
        detail_store.write(**detail_arguments, sensitive=True)
    assert len(attempted) == 1
    root = get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
    public = root / detail_relative_path(attempted[0])
    assert not (root / protected_detail_relative_path(attempted[0])).exists()
    assert not list(root.rglob("*.tmp"))
    if failure != "io":
        assert public.read_bytes() == b"existing-owner-content"
        assert public.is_symlink() == (failure == "symlink")
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
        field: "other-assembly" if field == "assembly_id" else "session_2",
    }
    ref = DetailRef.from_dict(identity)
    changes = {
        field: identity[field],
        "relative_path": detail_relative_path(ref).as_posix(),
    }
    different = replace(record, **changes)
    assert different.detail_id == record.detail_id
    for protected in (False, True):
        raw = detail_store._files.read(record.detail_ref, protected=protected)
        detail_store._files.publish(ref, raw, protected=protected)
    with pytest.raises(DetailUnavailableError, match="source-mismatch"):
        detail_store.read(
            session_id=ref.session_id, record=different, include_sensitive=True
        )
    detail_store.remove(session_id=ref.session_id, record=different)
    assert (
        detail_store.read(
            session_id="session_1", record=record, include_sensitive=True
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
    root = get_session_path_resolver(detail_sessions).resolve_session_node("session_1")
    target = root / record.relative_path
    target.write_bytes(raw)
    with pytest.raises(DetailUnavailableError):
        detail_store.gc(
            session_id="session_1",
            expired_before=datetime.now(UTC),
            allowed_refs=[record.detail_ref],
        )
    assert target.read_bytes() == raw


@pytest.mark.parametrize("protected", [False, True])
def test_missing_files_fail_without_recreating_paths(
    detail_sessions, detail_store, detail_arguments, protected
):
    record = detail_store.write(**detail_arguments, sensitive=True)
    target = detail_store._files.path(record.detail_ref, protected=protected)
    target.unlink()
    target.parent.rmdir()
    with pytest.raises(DetailUnavailableError, match="缺失"):
        detail_store.read(session_id="session_1", record=record, include_sensitive=True)
    assert not target.parent.exists()
