"""旧 protected golden → runtime 显式升级 → 新 typed read 的独立集成证据。"""

from __future__ import annotations

import hashlib
import inspect
import json
import traceback
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.details import (
    upgrade_detail,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.model import (
    SchemaV3DetailCapability,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
    protected_detail_relative_path,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    protected_aad,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.runtime.protected_detail import (
    ProtectedDetailBackend,
    ProtectedDetailError,
)

SESSION_ID = "ses_e6d2707870e54cab8c135193c0802532"

# 冻结旧 writer 的 wire 格式与独立密文向量，不调用当前实现生成 expected blob/AAD。
# 来源：2026-09-07T19:49:03.790Z 源码读取，rollout continuation
# 01a07c06-50e8-73e0-9051-93b47e7529ae_01a07c34-8cda-7d63-af12-1afc6f76e347:13789。
# 旧 protected_detail.py SHA-256:
# 62916f2b5898ae0ec51635887bef470ac59c8e9c96214da45d8cc76db6056955。
# writer 输入使用非生产测试 key p*32、session key s*32、nonce 000102...0b。
# R21 离线用独立 AESGCM 认证原始向量后，仅将 AAD/明文 session_id 迁为 canonical，
# 再用原 key/nonce 固定新密文；正文、摘要、旧 wire 格式不变。
_OLD_AAD = b'{"assembly_id":"old-assembly","content_length":60,"detail_ref":"detail-old","format_version":1,"session_id":"ses_e6d2707870e54cab8c135193c0802532","source_revision":"producer-v1"}'
_OLD_BLOB = bytes.fromhex(
    "626f787465616d2d636f6e746578742d64657461696c2d763100000102030405060708090a0b393eba76"
    "0746af6e44b0d3a47064681fe088ca115bb7945a26faaa8a47cb545cf45c9d0a6d0985e985a54499265a"
    "bd52d25646397d94a016646f78206a3c1d49335c0aa58ac1738a009be22aae92c656ff0be42a1edb55a6"
    "58eb3b1c44046b8631f70205fcc856c0a79e3751276488d132b1d5a5b4293f58a735039d7f030649a566"
    "be8405b26c3534371ac118969579e5bdfc524903c347dcd386ca8571b52faa1bfa9711fa998d83fa7327"
    "b26f567b7925ffa85c004281aee8bf70a91685b3633f39bc05c34a5a9285dfb0967d641e73508cad33df"
    "a5d73b993c667006984212c3f70c1c0f0430ad0bba408a13c13879203d562ff9cd9e3180d224be667285"
    "a0008c12d61527ff6f064f29575d4f6428289d5f6297b0aa6f8636539b36b38a85314bb7ae34f7db44a1"
    "a383ca0890eab2977e31b4be622d1482d8b604acfbcd56fb304d28629730815607a9be920fb279a19105"
    "6532b8b98a00cadc7fdb4fee53e14b6b9d488545574ae2da7e45eb255ec8d8b9d5ed8cd94a8721881a4f"
    "56e0fbc8359ba3eddea914695bbf230c1cc99a84bfda05e5e7040afe9c8454859b7fc167ee021826dceb"
    "410bff7c781fa29ecafa065cea0c52916fd183b69b865cf517f29d521536d1f7024151bb83f20b936126"
    "10f55a8b115a01e041a2ecac786c34903e"
)
_DIGEST = "hmac-sha256:session:v1:569a6c4d3895f5fd60c239576b71e424eddeb8df426065d9eda8e3b22922614c"
_BODY_HASH = (
    "sha256:jcs:v1:48f7a217f13566c30b46df7f8e08c24d55a06fd80777ed04be29d857485998f9"
)
_SECRET = "schema2-protected-fixture"


@pytest.fixture
def upgrade_root(integration_workspace_root_path, request) -> Path:
    node = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    root = Path(integration_workspace_root_path) / "protected-upgrade-cases" / node
    root.mkdir(parents=True)
    return root


@pytest.fixture
def sessions_dir(upgrade_root, session_bundle_factory) -> Path:
    sessions = upgrade_root / ".boxteam" / "sessions"
    session_root = session_bundle_factory(sessions, SESSION_ID)
    rollout = session_root / "rollout"
    rollout.mkdir()
    key = rollout / ".context-redaction-key"
    key.write_bytes(b"s" * 32)
    key.chmod(0o600)
    source = rollout / "context-plan-details-protected" / "old-assembly"
    source.mkdir(parents=True)
    (source / "detail-old.bin").write_bytes(_OLD_BLOB)
    return sessions


@pytest.fixture
def session_root(sessions_dir) -> Path:
    return get_session_path_resolver(sessions_dir).resolve_session_node(
        SESSION_ID
    )


@pytest.fixture
def upgrade_store(sessions_dir) -> ContextPlanDetailStore:
    return ContextPlanDetailStore(sessions_dir, protected_key=b"p" * 32)


@pytest.fixture
def upgrade_arguments() -> dict[str, object]:
    return {
        "legacy_blob": _OLD_BLOB,
        "legacy_aad": _OLD_AAD,
        "expected_digest": _DIGEST,
        "target_ref": DetailRef(SESSION_ID, "new-assembly", "new-detail"),
        "detail_kind": "request_source",
        "retention_class": "request_replay",
        "visibility": "private",
        "required": True,
        "created_at": "2026-09-07T01:00:00+00:00",
        "expires_at": "2036-09-07T01:00:00+00:00",
        "checkpoint_ns": "branch",
    }


@pytest.fixture
def old_plaintext() -> dict[str, object]:
    return {
        "format_version": 1,
        "session_id": SESSION_ID,
        "assembly_id": "old-assembly",
        "detail_ref": "detail-old",
        "source_revision": "producer-v1",
        "content_length": 60,
        "detail": {"secret": _SECRET, "mode": "旧上下文"},
        "detail_content_hash": _BODY_HASH,
        "redacted_stable_digest": _DIGEST,
    }


@pytest.fixture
def legacy_envelope(upgrade_arguments) -> dict[str, object]:
    return {
        "format_version": 1,
        "assembly_id": "old-assembly",
        "content_length": 60,
        "source_revision": "producer-v1",
        "sensitive": True,
        "protection": "protected",
        "detail": {"redacted": True, "redacted_stable_digest": _DIGEST},
        "redacted_stable_digest": _DIGEST,
        "detail_content_hash": None,
        "protected_body": True,
        "created_at": upgrade_arguments["created_at"],
        "gc_after": upgrade_arguments["expires_at"],
    }


@pytest.fixture
def capability_arguments(legacy_envelope, upgrade_arguments):
    return {
        "legacy_envelope": legacy_envelope,
        "legacy_blob": _OLD_BLOB,
        "legacy_session_id": SESSION_ID,
        "legacy_detail_id": "detail-old",
        **{
            key: upgrade_arguments[key]
            for key in (
                "target_ref",
                "detail_kind",
                "retention_class",
                "visibility",
                "required",
                "checkpoint_ns",
                "expected_digest",
            )
        },
    }


@pytest.fixture
def authenticated_corruption():
    """仅在已冻结 framing 上注入被 AES-GCM 正确认证的非法明文。"""

    def encrypt(raw: bytes) -> bytes:
        nonce = bytes(range(12))
        return (
            b"boxteam-context-detail-v1\x00"
            + nonce
            + AESGCM(b"p" * 32).encrypt(nonce, raw, _OLD_AAD)
        )

    return encrypt


def _artifact_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_frozen_old_writer_blob_authenticates_exact_aad(old_plaintext):
    backend = ProtectedDetailBackend(b"p" * 32)
    assert backend.authenticate_schema2_blob(_OLD_BLOB, aad=_OLD_AAD) == old_plaintext
    assert len(_OLD_BLOB) > len(b"boxteam-context-detail-v1\x00") + 12 + 16
    assert _SECRET.encode() not in _OLD_BLOB


def test_prepare_is_memory_only_and_new_record_roundtrips(
    upgrade_store,
    upgrade_arguments,
    session_root,
    sessions_dir,
    old_plaintext,
):
    before = _artifact_bytes(session_root)
    prepared = upgrade_store.prepare_protected_detail_upgrade(**upgrade_arguments)
    assert _artifact_bytes(session_root) == before
    record = prepared.record
    assert record.detail_ref == upgrade_arguments["target_ref"]
    assert record.source_revision == "producer-v1"
    assert record.length == 60
    assert record.expires_at == upgrade_arguments["expires_at"]
    assert record.checkpoint_ns == "branch"
    assert (
        record.relative_path == "rollout/context-plan-details/new-assembly/new-detail"
    )
    assert record.redacted_stable_digest == _DIGEST
    marker = json.loads(prepared.manifest_bytes)
    assert marker["created_at"] == upgrade_arguments["created_at"]
    assert marker["detail"] == {
        "redacted": True,
        "detail_kind": "request_source",
        "value_type": "object",
        "length": 60,
        "redacted_stable_digest": _DIGEST,
    }
    assert marker["detail_content_hash"] is None
    assert record.content_hash == sha256_jcs(marker)
    assert _SECRET not in repr(prepared)
    assert _SECRET.encode() not in prepared.manifest_bytes + prepared.protected_bytes
    # 测试模拟 migration 安装 prepared bytes；生产升级入口本身不负责发布。
    for path, raw in (
        (session_root / record.relative_path, prepared.manifest_bytes),
        (
            session_root / protected_detail_relative_path(record.detail_ref),
            prepared.protected_bytes,
        ),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    restarted = ContextPlanDetailStore(sessions_dir, protected_key=b"p" * 32)
    assert (
        restarted.read(
            session_id=SESSION_ID, record=record, include_sensitive=True
        )["detail"]
        == old_plaintext["detail"]
    )
    old = (
        session_root
        / "rollout/context-plan-details-protected/old-assembly/detail-old.bin"
    )
    assert old.read_bytes() == _OLD_BLOB


@pytest.mark.parametrize(
    "kind",
    [
        "no-encryption-key",
        "wrong-encryption-key",
        "missing-session-key",
        "wrong-session-key",
        "symlink-session-key",
    ],
)
def test_keys_are_required_and_failure_never_creates_or_changes_files(
    sessions_dir,
    session_root,
    upgrade_arguments,
    kind,
    caplog,
):
    key_path = session_root / "rollout" / ".context-redaction-key"
    key = b"p" * 32
    expected = "authentication"
    if kind == "no-encryption-key":
        key, expected = None, "key-required"
    elif kind == "wrong-encryption-key":
        key = b"x" * 32
    elif kind == "missing-session-key":
        key_path.unlink()
        expected = "session-key-required"
    elif kind == "symlink-session-key":
        saved_key = session_root / "saved-test-key"
        key_path.rename(saved_key)
        key_path.symlink_to(saved_key)
        expected = "session-key-required"
    else:
        key_path.write_bytes(b"x" * 32)
        expected = "session-digest"
    before = _artifact_bytes(session_root)
    store = ContextPlanDetailStore(sessions_dir, protected_key=key)
    with pytest.raises(
        DetailUnavailableError, match=f"schema-upgrade-protected-{expected}"
    ) as caught:
        store.prepare_protected_detail_upgrade(**upgrade_arguments)
    assert _artifact_bytes(session_root) == before
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert _SECRET not in caplog.text + "".join(
        traceback.format_exception(caught.value)
    )


@pytest.mark.parametrize(
    "field,value,stage",
    [
        ("format_version", 2, "source-aad"),
        ("format_version", True, "source-aad"),
        ("content_length", True, "source-aad"),
        ("content_length", -1, "source-aad"),
        ("content_length", 61, "authentication"),
        ("detail_ref", "different", "authentication"),
        ("assembly_id", "different", "authentication"),
        ("source_revision", "different", "authentication"),
        ("session_id", "different", "source-owner"),
        ("extra", "unknown", "source-aad"),
    ],
)
def test_mismatched_given_old_aad_is_not_guessed(
    upgrade_store,
    upgrade_arguments,
    session_root,
    field,
    value,
    stage,
):
    aad = json.loads(_OLD_AAD)
    aad[field] = value
    before = _artifact_bytes(session_root)
    with pytest.raises(DetailUnavailableError, match=stage):
        upgrade_store.prepare_protected_detail_upgrade(
            **{**upgrade_arguments, "legacy_aad": canonical_json_bytes(aad)}
        )
    assert _artifact_bytes(session_root) == before


@pytest.mark.parametrize(
    "bad", ["format", "nonce", "tag", "truncated", "noncanonical-aad", "invalid-aad"]
)
def test_corruption_is_rejected_without_plaintext_or_artifacts(
    upgrade_store,
    upgrade_arguments,
    session_root,
    bad,
    caplog,
):
    changed = dict(upgrade_arguments)
    if bad in {"format", "nonce", "tag"}:
        blob = bytearray(_OLD_BLOB)
        index = {
            "format": 0,
            "nonce": len(b"boxteam-context-detail-v1\x00"),
            "tag": -1,
        }[bad]
        blob[index] ^= 1
        changed["legacy_blob"] = bytes(blob)
    elif bad == "truncated":
        changed["legacy_blob"] = _OLD_BLOB[:30]
    elif bad == "noncanonical-aad":
        changed["legacy_aad"] = _OLD_AAD + b"\n"
    else:
        changed["legacy_aad"] = (_SECRET + "\n{").encode()
    before = _artifact_bytes(session_root)
    with pytest.raises(DetailUnavailableError) as caught:
        upgrade_store.prepare_protected_detail_upgrade(**changed)
    assert _artifact_bytes(session_root) == before
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert _SECRET not in caplog.text + "".join(
        traceback.format_exception(caught.value)
    )


@pytest.mark.parametrize(
    "field,value,stage",
    [
        ("detail_ref", "different", "source-manifest"),
        ("content_length", True, "source-manifest"),
        ("detail_content_hash", "wrong", "source-manifest"),
        ("extra", 1, "source-manifest"),
        ("redacted_stable_digest", "wrong", "session-digest"),
        ("redacted_stable_digest", "机密", "session-digest"),
    ],
)
def test_authenticated_but_invalid_plaintext_is_rejected(
    upgrade_store,
    upgrade_arguments,
    session_root,
    old_plaintext,
    authenticated_corruption,
    field,
    value,
    stage,
):
    plaintext = {**old_plaintext, field: value}
    blob = authenticated_corruption(canonical_json_bytes(plaintext))
    before = _artifact_bytes(session_root)
    with pytest.raises(DetailUnavailableError, match=stage):
        upgrade_store.prepare_protected_detail_upgrade(
            **{**upgrade_arguments, "legacy_blob": blob}
        )
    assert _artifact_bytes(session_root) == before


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json-secret",
        b'{"secret":"schema2-protected-fixture","bad":NaN}',
        b'{"secret":"schema2-protected-fixture","bad":"\xff"}',
        b"[]",
    ],
)
def test_authenticated_invalid_json_never_survives_in_error_chain(
    upgrade_store,
    upgrade_arguments,
    authenticated_corruption,
    raw,
    caplog,
):
    blob = authenticated_corruption(raw)
    with pytest.raises(DetailUnavailableError) as caught:
        upgrade_store.prepare_protected_detail_upgrade(
            **{**upgrade_arguments, "legacy_blob": blob}
        )
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert _SECRET not in caplog.text + "".join(
        traceback.format_exception(caught.value)
    )


def test_rehashed_replacement_body_cannot_forge_session_provenance(
    upgrade_store,
    upgrade_arguments,
    old_plaintext,
    authenticated_corruption,
):
    body = {"secret": "schema2-replacement-value", "mode": "旧上下文"}
    assert len(canonical_json_bytes(body)) == 60
    altered = {**old_plaintext, "detail": body, "detail_content_hash": sha256_jcs(body)}
    blob = authenticated_corruption(canonical_json_bytes(altered))
    with pytest.raises(DetailUnavailableError, match="session-digest"):
        upgrade_store.prepare_protected_detail_upgrade(
            **{**upgrade_arguments, "legacy_blob": blob}
        )


@pytest.mark.parametrize(
    "field,value,stage",
    [
        ("expected_digest", "wrong", "session-digest"),
        ("expected_digest", None, "session-digest"),
        ("target_ref", "old-string", "source-owner"),
        (
            "target_ref",
            DetailRef("other-session", "assembly", "detail"),
            "source-owner",
        ),
        ("detail_kind", "", "session-digest"),
        ("retention_class", "", "target-manifest"),
        ("visibility", "secret", "target-manifest"),
        ("required", 1, "target-manifest"),
        ("created_at", None, "target-manifest"),
        ("created_at", "2026-09-07", "target-manifest"),
        ("expires_at", "not-a-date", "target-manifest"),
    ],
)
def test_target_scope_and_metadata_are_explicit(
    upgrade_store,
    upgrade_arguments,
    session_root,
    field,
    value,
    stage,
):
    before = _artifact_bytes(session_root)
    with pytest.raises(DetailUnavailableError, match=stage):
        upgrade_store.prepare_protected_detail_upgrade(
            **{**upgrade_arguments, field: value}
        )
    assert _artifact_bytes(session_root) == before


def test_normal_runtime_has_no_legacy_decrypt_fallback(
    upgrade_store, upgrade_arguments
):
    prepared = upgrade_store.prepare_protected_detail_upgrade(**upgrade_arguments)
    backend = ProtectedDetailBackend(b"p" * 32)
    with pytest.raises(ProtectedDetailError, match="格式非法"):
        backend.decrypt(_OLD_BLOB, record=prepared.record)
    with pytest.raises(ProtectedDetailError, match="authentication"):
        backend.decrypt(
            prepared.protected_bytes,
            record=replace(prepared.record, detail_kind="other"),
        )
    assert (
        protected_aad(prepared.record)["detail_ref"]
        == upgrade_arguments["target_ref"].to_dict()
    )


def test_encryption_failure_has_safe_error_and_no_partial_writes(
    upgrade_store,
    upgrade_arguments,
    session_root,
    monkeypatch,
    caplog,
):
    def fail_encrypt(*args, **kwargs):
        raise RuntimeError(_SECRET)

    monkeypatch.setattr(ProtectedDetailBackend, "encrypt", fail_encrypt)
    before = _artifact_bytes(session_root)
    with pytest.raises(DetailUnavailableError, match="target-encryption") as caught:
        upgrade_store.prepare_protected_detail_upgrade(**upgrade_arguments)
    assert _artifact_bytes(session_root) == before
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert _SECRET not in caplog.text + "".join(
        traceback.format_exception(caught.value)
    )


def test_preparation_preserves_expiry_and_is_retryable(
    upgrade_store, upgrade_arguments
):
    args = {**upgrade_arguments, "expires_at": "2000-01-01T00:00:00+00:00"}
    first = upgrade_store.prepare_protected_detail_upgrade(**args)
    second = upgrade_store.prepare_protected_detail_upgrade(**args)
    assert first.record == second.record
    assert first.manifest_bytes == second.manifest_bytes
    assert first.protected_bytes != second.protected_bytes
    assert first.record.expires_at == args["expires_at"]
    with pytest.raises(DetailUnavailableError, match="已过期"):
        upgrade_store.read(
            session_id=SESSION_ID, record=first.record, include_sensitive=True
        )


@pytest.mark.parametrize("protection", ["public", "redacted", "protected"])
def test_normal_write_uses_same_new_envelope_without_old_ingress(
    upgrade_store,
    session_root,
    protection,
):
    body = {"text": "normal-new-write"}
    record = upgrade_store.write(
        session_id=SESSION_ID,
        assembly_id="normal-assembly",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="internal",
        detail=body,
        sensitive=protection != "public",
        protection=protection,
        source_revision="normal-producer",
    )
    payload = json.loads((session_root / record.relative_path).read_bytes())
    assert payload["format_version"] == 2
    assert payload["detail_ref"] == record.detail_ref.to_dict()
    assert sha256_jcs(payload) == record.content_hash
    if protection == "redacted":
        with pytest.raises(DetailUnavailableError, match="没有 protected body"):
            upgrade_store.read(
                session_id=SESSION_ID, record=record, include_sensitive=True
            )
    else:
        assert (
            upgrade_store.read(
                session_id=SESSION_ID, record=record, include_sensitive=True
            )["detail"]
            == body
        )


def test_new_cipher_manifest_is_verified_before_return(
    upgrade_store,
    upgrade_arguments,
    session_root,
    monkeypatch,
):
    original = ProtectedDetailBackend.encrypt

    def corrupt_manifest(self, *, record, detail, detail_content_hash):
        return original(
            self, record=record, detail=detail, detail_content_hash="corrupt"
        )

    monkeypatch.setattr(ProtectedDetailBackend, "encrypt", corrupt_manifest)
    before = _artifact_bytes(session_root)
    with pytest.raises(DetailUnavailableError, match="target-encryption"):
        upgrade_store.prepare_protected_detail_upgrade(**upgrade_arguments)
    assert _artifact_bytes(session_root) == before


def test_saver_factory_matches_volta_public_protocol(
    sessions_dir,
    upgrade_store,
    upgrade_arguments,
    capability_arguments,
    legacy_envelope,
):
    saver = RolloutCheckpointSaver(sessions_dir, protected_detail_key=b"p" * 32)
    capability = saver._detail_store.schema_v3_detail_capability()
    for name, function in inspect.getmembers(
        SchemaV3DetailCapability, inspect.isfunction
    ):
        if not name.startswith("_"):
            expected = inspect.signature(function).parameters
            actual = inspect.signature(getattr(type(capability), name)).parameters
            assert tuple(expected) == tuple(actual), name
    capability.require_protected_key()
    prepared = capability.prepare_legacy_detail(**capability_arguments)
    direct = upgrade_store.prepare_protected_detail_upgrade(**upgrade_arguments)
    assert prepared.record == direct.record
    assert prepared.manifest_bytes == direct.manifest_bytes
    body = ProtectedDetailBackend(b"p" * 32).decrypt(
        prepared.protected_bytes,
        record=prepared.record,
    )["detail"]
    assert body["secret"] == _SECRET


def test_capability_authenticates_once_and_returns_final_record(
    upgrade_store,
    capability_arguments,
    legacy_envelope,
    monkeypatch,
):
    original = ProtectedDetailBackend.authenticate_schema2_blob
    calls = []

    def count_auth(self, blob, *, aad):
        calls.append(aad)
        return original(self, blob, aad=aad)

    monkeypatch.setattr(ProtectedDetailBackend, "authenticate_schema2_blob", count_auth)
    capability = upgrade_store.schema_v3_detail_capability()
    prepared = capability.prepare_legacy_detail(**capability_arguments)
    assert calls == [_OLD_AAD]
    assert prepared.record.content_hash != sha256_jcs(legacy_envelope)
    assert prepared.record.content_hash == sha256_jcs(
        json.loads(prepared.manifest_bytes)
    )
    assert not hasattr(capability, "legacy_detail_value_type")
    assert not hasattr(capability, "rewrap_legacy_detail")


def test_factory_rejects_missing_protected_key(sessions_dir):
    capability = ContextPlanDetailStore(sessions_dir).schema_v3_detail_capability()
    with pytest.raises(DetailUnavailableError, match="protected-key-required"):
        capability.require_protected_key()


def test_volta_upgrade_detail_consumes_real_capability(
    upgrade_store,
    session_root,
    legacy_envelope,
    old_plaintext,
):
    raw = canonical_json_bytes(legacy_envelope)
    relative = "context-plan-details/old-assembly/detail-old.json"
    path = session_root / "rollout" / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    row = {
        "detail_ref": "detail-old",
        "session_id": SESSION_ID,
        "checkpoint_ns": "branch",
        "assembly_id": "old-assembly",
        "relative_path": "rollout/" + relative,
        "content_hash": sha256_jcs(legacy_envelope),
        "source_revision": "producer-v1",
        "content_length": 60,
        "redacted_stable_digest": _DIGEST,
        "protection": "protected",
        "availability": "available",
        "required": 1,
        "sensitive": 1,
        "status": "available",
        "created_at": legacy_envelope["created_at"],
        "gc_after": legacy_envelope["gc_after"],
    }
    before = _artifact_bytes(session_root)
    upgraded = upgrade_detail(
        row,
        rollout_root=session_root / "rollout",
        session_id=SESSION_ID,
        checkpoint_ns="branch",
        purpose=("request_source", "request_replay", "private"),
        detail_capability=upgrade_store.schema_v3_detail_capability(),
    )
    assert _artifact_bytes(session_root) == before
    assert upgraded.new_raw is not None and upgraded.protected_new_raw is not None
    assert sha256_jcs(json.loads(upgraded.new_raw)) == upgraded.record.content_hash
    body = ProtectedDetailBackend(b"p" * 32).decrypt(
        upgraded.protected_new_raw,
        record=upgraded.record,
    )["detail"]
    assert body == old_plaintext["detail"]
