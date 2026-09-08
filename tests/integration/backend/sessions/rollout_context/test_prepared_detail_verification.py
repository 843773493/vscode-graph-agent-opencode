"""迁移重试 capability 对既有 staged typed bytes 的独立认证证据。"""

from __future__ import annotations

import json
import secrets
import traceback
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
    detail_relative_path,
    protected_detail_relative_path,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    parse_detail_payload,
    protected_aad,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.runtime.protected_detail import (
    ProtectedDetailBackend,
)

# 显式注入同一可信旧 writer golden 的 fixtures；request.path 仍是本文件，
# 不复用旧测试或 V 的正式工作区，不重新生成旧 blob 作为期望值。
from tests.integration.backend.sessions.rollout_context.test_protected_detail_upgrade import (
    capability_arguments as capability_arguments,  # noqa: PLC0414 - 显式导出 pytest fixture
)
from tests.integration.backend.sessions.rollout_context.test_protected_detail_upgrade import (
    legacy_envelope as legacy_envelope,  # noqa: PLC0414 - 显式导出 pytest fixture
)
from tests.integration.backend.sessions.rollout_context.test_protected_detail_upgrade import (
    old_plaintext as old_plaintext,  # noqa: PLC0414 - 显式导出 pytest fixture
)
from tests.integration.backend.sessions.rollout_context.test_protected_detail_upgrade import (
    session_root as session_root,  # noqa: PLC0414 - 显式导出 pytest fixture
)
from tests.integration.backend.sessions.rollout_context.test_protected_detail_upgrade import (
    sessions_dir as sessions_dir,  # noqa: PLC0414 - 显式导出 pytest fixture
)
from tests.integration.backend.sessions.rollout_context.test_protected_detail_upgrade import (
    upgrade_arguments as upgrade_arguments,  # noqa: PLC0414 - 显式导出 pytest fixture
)
from tests.integration.backend.sessions.rollout_context.test_protected_detail_upgrade import (
    upgrade_root as upgrade_root,  # noqa: PLC0414 - 显式导出 pytest fixture
)
from tests.integration.backend.sessions.rollout_context.test_protected_detail_upgrade import (
    upgrade_store as upgrade_store,  # noqa: PLC0414 - 显式导出 pytest fixture
)


@pytest.fixture
def capability(upgrade_store):
    return upgrade_store.schema_v3_detail_capability()


@pytest.fixture
def prepared(capability, capability_arguments):
    return capability.prepare_legacy_detail(**capability_arguments)


@pytest.fixture
def source_artifacts(session_root):
    def snapshot():
        return {
            path.relative_to(session_root).as_posix(): path.read_bytes()
            for path in session_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    return snapshot


@pytest.fixture
def authenticated_typed_blob():
    """独立 AES-GCM 构造已认证的非法 envelope，不伪造被测 decoder 结果。"""

    def encrypt(record, raw):
        nonce = secrets.token_bytes(12)
        return (
            b"boxteam-context-detail-v2\x00"
            + nonce
            + AESGCM(b"p" * 32).encrypt(
                nonce, raw, canonical_json_bytes(protected_aad(record))
            )
        )

    return encrypt


def test_repeated_verification_authenticates_without_regeneration(
    capability, prepared, source_artifacts, monkeypatch
):
    original_decrypt = ProtectedDetailBackend.decrypt
    authenticated = []

    def decrypt(self, blob, *, record):
        authenticated.append(blob)
        return original_decrypt(self, blob, record=record)

    def forbidden(*args, **kwargs):
        pytest.fail("staged 验证不能重新加密或读取旧 blob")

    monkeypatch.setattr(ProtectedDetailBackend, "decrypt", decrypt)
    monkeypatch.setattr(ProtectedDetailBackend, "encrypt", forbidden)
    monkeypatch.setattr(ProtectedDetailBackend, "authenticate_schema2_blob", forbidden)
    before = source_artifacts()
    for _ in range(2):
        assert (
            capability.verify_prepared_detail(
                record=prepared.record,
                manifest_bytes=prepared.manifest_bytes,
                protected_bytes=prepared.protected_bytes,
            )
            is None
        )
    assert authenticated == [prepared.protected_bytes, prepared.protected_bytes]
    assert source_artifacts() == before


def test_each_random_preparation_can_be_verified_and_reused(
    capability, prepared, capability_arguments, source_artifacts
):
    before = source_artifacts()
    second = capability.prepare_legacy_detail(**capability_arguments)
    assert second.record == prepared.record
    assert second.manifest_bytes == prepared.manifest_bytes
    assert second.protected_bytes != prepared.protected_bytes
    for artifact in (prepared, second):
        assert (
            capability.verify_prepared_detail(
                record=artifact.record,
                manifest_bytes=artifact.manifest_bytes,
                protected_bytes=artifact.protected_bytes,
            )
            is None
        )
    assert source_artifacts() == before


@pytest.mark.parametrize(
    "shape",
    [
        "record_dict",
        "record_str",
        "manifest_str",
        "manifest_bytearray",
        "cipher_str",
        "cipher_bytearray",
    ],
)
def test_verifier_rejects_untyped_or_mutable_inputs(capability, prepared, shape):
    arguments = {
        "record": prepared.record,
        "manifest_bytes": prepared.manifest_bytes,
        "protected_bytes": prepared.protected_bytes,
    }
    if shape.startswith("record"):
        arguments["record"] = (
            prepared.record.detail_ref.to_dict()
            if shape == "record_dict"
            else "new-detail"
        )
    elif shape.startswith("manifest"):
        arguments["manifest_bytes"] = (
            prepared.manifest_bytes.decode()
            if shape == "manifest_str"
            else bytearray(prepared.manifest_bytes)
        )
    else:
        arguments["protected_bytes"] = (
            prepared.protected_bytes.hex()
            if shape == "cipher_str"
            else bytearray(prepared.protected_bytes)
        )
    with pytest.raises(DetailUnavailableError, match="prepared-manifest"):
        capability.verify_prepared_detail(**arguments)


def test_historical_expiry_is_authenticated_without_replay_permission(
    capability, capability_arguments, source_artifacts
):
    arguments = dict(capability_arguments)
    arguments["legacy_envelope"] = {
        **arguments["legacy_envelope"],
        "gc_after": "2000-01-01T00:00:00+00:00",
    }
    before = source_artifacts()
    artifact = capability.prepare_legacy_detail(**arguments)
    assert artifact.record.expires_at == "2000-01-01T00:00:00+00:00"
    assert (
        capability.verify_prepared_detail(
            record=artifact.record,
            manifest_bytes=artifact.manifest_bytes,
            protected_bytes=artifact.protected_bytes,
        )
        is None
    )
    assert source_artifacts() == before


@pytest.mark.parametrize("key", [None, b"x" * 32])
def test_existing_protected_key_is_required(
    sessions_dir, prepared, key, source_artifacts
):
    capability = ContextPlanDetailStore(
        sessions_dir, protected_key=key
    ).schema_v3_detail_capability()
    before = source_artifacts()
    with pytest.raises(DetailUnavailableError, match="schema-upgrade-protected"):
        capability.verify_prepared_detail(
            record=prepared.record,
            manifest_bytes=prepared.manifest_bytes,
            protected_bytes=prepared.protected_bytes,
        )
    assert source_artifacts() == before


@pytest.mark.parametrize("damage", ["missing", "different", "short", "symlink"])
def test_session_key_is_authenticated_and_never_created(
    capability, prepared, session_root, source_artifacts, damage
):
    path = session_root / "rollout" / ".context-redaction-key"
    if damage in {"missing", "symlink"}:
        path.unlink()
        if damage == "symlink":
            other = path.with_name("diagnostic-key")
            other.write_bytes(b"s" * 32)
            path.symlink_to(other)
    else:
        path.write_bytes(b"x" * (32 if damage == "different" else 3))
    before = source_artifacts()
    with pytest.raises(DetailUnavailableError, match="schema-upgrade-protected"):
        capability.verify_prepared_detail(
            record=prepared.record,
            manifest_bytes=prepared.manifest_bytes,
            protected_bytes=prepared.protected_bytes,
        )
    assert source_artifacts() == before
    if damage == "missing":
        assert not path.exists()
    if damage == "symlink":
        assert path.is_symlink()


@pytest.mark.parametrize("owner", ["session", "assembly", "detail"])
def test_foreign_typed_owner_cannot_verify_staged_bytes(capability, prepared, owner):
    ref = prepared.record.detail_ref
    target = DetailRef(
        "foreign" if owner == "session" else ref.session_id,
        "foreign" if owner == "assembly" else ref.assembly_id,
        "foreign" if owner == "detail" else ref.detail_id,
    )
    record = replace(
        prepared.record,
        session_id=target.session_id,
        assembly_id=target.assembly_id,
        detail_id=target.detail_id,
        relative_path=detail_relative_path(target).as_posix(),
    )
    with pytest.raises(DetailUnavailableError, match="prepared-manifest"):
        capability.verify_prepared_detail(
            record=record,
            manifest_bytes=prepared.manifest_bytes,
            protected_bytes=prepared.protected_bytes,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"detail_kind": "assembly_snapshot"},
        {"retention_class": "assembly_audit"},
        {"visibility": "internal"},
        {"source_revision": "foreign-revision"},
        {"length": 61},
        {"required": False},
        {"checkpoint_ns": "foreign-ns"},
        {"expires_at": "2037-01-01T00:00:00+00:00"},
        {"content_hash": "sha256:jcs:v1:" + "0" * 64},
        {"redacted_stable_digest": "hmac-sha256:session:v1:" + "0" * 64},
        {"status": "unavailable", "availability": "unavailable"},
        {"protection": "redacted"},
    ],
)
def test_record_manifest_mismatch_is_rejected(capability, prepared, changes):
    with pytest.raises(DetailUnavailableError, match="prepared-manifest"):
        capability.verify_prepared_detail(
            record=replace(prepared.record, **changes),
            manifest_bytes=prepared.manifest_bytes,
            protected_bytes=prepared.protected_bytes,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("required", False),
        ("visibility", "internal"),
        ("retention_class", "assembly_audit"),
        ("checkpoint_ns", "foreign"),
        ("expires_at", "2037-01-01T00:00:00+00:00"),
    ],
)
def test_rehashed_manifest_still_requires_original_typed_aad(
    capability, prepared, field, value
):
    manifest = json.loads(prepared.manifest_bytes)
    manifest[field] = value
    raw = canonical_json_bytes(manifest)
    _, record = parse_detail_payload(raw, detail_ref=prepared.record.detail_ref)
    with pytest.raises(DetailUnavailableError, match="prepared-authentication"):
        capability.verify_prepared_detail(
            record=record,
            manifest_bytes=raw,
            protected_bytes=prepared.protected_bytes,
        )


@pytest.mark.parametrize("damage", ["empty", "truncated", "bit_flip", "legacy"])
def test_invalid_or_legacy_ciphertext_is_rejected(
    capability, prepared, capability_arguments, damage
):
    raw = prepared.protected_bytes
    damaged = {
        "empty": b"",
        "truncated": raw[:30],
        "bit_flip": raw[:-1] + bytes([raw[-1] ^ 1]),
        "legacy": capability_arguments["legacy_blob"],
    }[damage]
    with pytest.raises(DetailUnavailableError, match="prepared-authentication"):
        capability.verify_prepared_detail(
            record=prepared.record,
            manifest_bytes=prepared.manifest_bytes,
            protected_bytes=damaged,
        )


@pytest.mark.parametrize(
    "damage", ["hash", "digest", "non_ascii_digest", "body", "length", "extra"]
)
def test_authenticated_plaintext_still_requires_semantic_integrity(
    capability, prepared, authenticated_typed_blob, damage, old_plaintext
):
    payload = dict(
        ProtectedDetailBackend(b"p" * 32).decrypt(
            prepared.protected_bytes,
            record=prepared.record,
        )
    )
    if damage == "hash":
        payload["detail_content_hash"] = "sha256:jcs:v1:" + "0" * 64
    elif damage in {"digest", "non_ascii_digest"}:
        payload["redacted_stable_digest"] = (
            "不同摘要" if damage == "non_ascii_digest" else "wrong"
        )
    elif damage == "body":
        payload["detail"]["secret"] = "x" * len(old_plaintext["detail"]["secret"])
        payload["detail_content_hash"] = sha256_jcs(payload["detail"])
    elif damage == "length":
        payload["detail"] = "short"
        payload["detail_content_hash"] = sha256_jcs(payload["detail"])
    else:
        payload["unauthorized"] = "extra field"
    raw = authenticated_typed_blob(prepared.record, canonical_json_bytes(payload))
    with pytest.raises(DetailUnavailableError, match="schema-upgrade-protected"):
        capability.verify_prepared_detail(
            record=prepared.record,
            manifest_bytes=prepared.manifest_bytes,
            protected_bytes=raw,
        )


def test_authenticated_ciphertext_cannot_lie_about_marker_type(capability, prepared):
    backend = ProtectedDetailBackend(b"p" * 32)
    payload = backend.decrypt(prepared.protected_bytes, record=prepared.record)
    manifest = json.loads(prepared.manifest_bytes)
    manifest["detail"]["value_type"] = "array"
    raw = canonical_json_bytes(manifest)
    _, record = parse_detail_payload(raw, detail_ref=prepared.record.detail_ref)
    cipher = backend.encrypt(
        record=record,
        detail=payload["detail"],
        detail_content_hash=payload["detail_content_hash"],
    )
    with pytest.raises(DetailUnavailableError, match="prepared-integrity"):
        capability.verify_prepared_detail(
            record=record, manifest_bytes=raw, protected_bytes=cipher
        )


@pytest.mark.parametrize(
    "boundary", ["manifest_decoder", "cipher_decoder", "crypto_error"]
)
def test_failure_does_not_expose_plaintext_or_exception_chain(
    capability, prepared, authenticated_typed_blob, old_plaintext, monkeypatch, boundary
):
    secret = old_plaintext["detail"]["secret"]
    malformed = b'{"secret":' + secret.encode()
    manifest = malformed if boundary == "manifest_decoder" else prepared.manifest_bytes
    cipher = (
        authenticated_typed_blob(prepared.record, malformed)
        if boundary == "cipher_decoder"
        else prepared.protected_bytes
    )
    if boundary == "crypto_error":

        def fail(*args, **kwargs):
            raise ValueError(secret)

        monkeypatch.setattr(ProtectedDetailBackend, "decrypt", fail)
    with pytest.raises(
        DetailUnavailableError, match="schema-upgrade-protected"
    ) as caught:
        capability.verify_prepared_detail(
            record=prepared.record,
            manifest_bytes=manifest,
            protected_bytes=cipher,
        )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("body", [None, True, 1.25, "私有文本", [], {}, [1, "a"]])
def test_typed_json_markers_verify_using_real_store_output(
    capability, upgrade_store, session_root, body
):
    record = upgrade_store.write(
        session_id="upgrade-session",
        assembly_id="typed-assembly",
        detail_kind="request_source",
        retention_class="request_replay",
        visibility="private",
        detail=body,
        sensitive=True,
        protection="protected",
        source_revision="typed-source",
    )
    assert (
        capability.verify_prepared_detail(
            record=record,
            manifest_bytes=(session_root / record.relative_path).read_bytes(),
            protected_bytes=(
                session_root / protected_detail_relative_path(record.detail_ref)
            ).read_bytes(),
        )
        is None
    )
