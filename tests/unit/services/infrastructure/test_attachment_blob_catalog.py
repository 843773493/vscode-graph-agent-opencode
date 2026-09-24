"""workspace 附件 content-addressed blob store 的分层测试（8.3 附件链）。

覆盖点：日期分桶物理定位、逻辑引用、去重与跨 Session 复用、权限隔离、
缩略图派生、损坏图片诊断、digest 唯一 claim、blob identity conflict、
pin/fence 发布复核、上传与删除竞态、定点恢复与引用感知 GC。

全部使用 tmp_path 隔离，不依赖网络或真实 LLM。
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.core.session_control_store import SessionControlStore
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    compute_blob_identity,
    date_bucket_relative_locator,
    validate_blob_relative_locator,
)
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
    BlobIdentityConflictError,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    ingest_staging_relative_locator,
    resolve_blob_path,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
    parse_logical_file_id,
)

SESSION_A = "ses_8044804392e9434e8f61961ec7604c3b"
SESSION_B = "ses_aed5707da48947108df3da01acc1b1b0"


def _data_url(content_type: str, data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _attach(name: str, content_type: str, data: bytes) -> AttachmentRef:
    return AttachmentRef(
        file_id=f"inline:{name}",
        name=name,
        content_type=content_type,
        data_url=_data_url(content_type, data),
    )


def _blob_files(workspace_root: Path) -> list[Path]:
    return sorted(
        path
        for path in (workspace_root / ".boxteam" / "attachments").rglob("blb_*")
        if path.is_file()
    )


def _store(tmp_path: Path, session_bundle_factory, *session_ids: str) -> AttachmentBlobStore:
    for session_id in session_ids:
        session_bundle_factory(tmp_path / ".boxteam" / "sessions", session_id)
    return AttachmentBlobStore(tmp_path)


# ----------------------------------------------------------------------
# 身份与受检 locator
# ----------------------------------------------------------------------


def test_blob_identity_is_pure_sha256_of_bytes() -> None:
    identity = compute_blob_identity(b"hello")
    assert identity.blob_id == "blb_" + compute_blob_identity(b"hello").blob_id[4:]
    assert identity.digest == "sha256:" + identity.blob_id[4:]
    assert identity.length == 5


def test_date_bucket_locator_has_no_extra_shard() -> None:
    locator = date_bucket_relative_locator(
        "blb_" + "a" * 64, datetime(2026, 3, 4, tzinfo=UTC).date()
    )
    assert locator == "2026/03/04/blb_" + "a" * 64
    validate_blob_relative_locator(locator)


@pytest.mark.parametrize(
    "locator",
    [
        "2026/03/04/blb_" + "A" * 64,
        "2026/03/04/blb_" + "a" * 63,
        "../2026/03/04/blb_" + "a" * 64,
        "/2026/03/04/blb_" + "a" * 64,
        "2026/03/04/blb_" + "a" * 64 + "/x",
        "blb_" + "a" * 64,
    ],
)
def test_validate_blob_relative_locator_rejects_non_canonical(locator: str) -> None:
    with pytest.raises(ValueError):
        validate_blob_relative_locator(locator)


def test_resolve_blob_path_rejects_caller_supplied_escape(tmp_path: Path) -> None:
    """resolver 只接受受检 locator：调用方字符串路径必须失败。"""
    with pytest.raises(ValueError):
        resolve_blob_path(tmp_path / "attachments", "../../etc/passwd")


@pytest.mark.parametrize(
    "file_id",
    [
        "boxteam-session://%s/attachments/../../etc/passwd" % SESSION_A,
        "boxteam-session://%s/attachments/att_1/../x" % SESSION_A,
        "boxteam-session://%s/other/att_1" % SESSION_A,
        "boxteam-session://%s/attachments" % SESSION_A,
        "/etc/passwd",
    ],
)
def test_parse_logical_file_id_rejects_traversal_and_foreign(file_id: str) -> None:
    with pytest.raises(ValueError):
        parse_logical_file_id(SESSION_A, file_id)


def test_parse_logical_file_id_rejects_other_session() -> None:
    with pytest.raises(ValueError, match="不属于指定会话"):
        parse_logical_file_id(SESSION_B, f"boxteam-session://{SESSION_A}/attachments/att_1")


# ----------------------------------------------------------------------
# 写入与读取
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_inline_writes_date_bucketed_blob_and_reads_back(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    payload = b"\x89PNG\r\n\x1a\nimage-data"

    stored = (
        await store.persist_inline(SESSION_A, [_attach("example.png", "image/png", payload)])
    )[0]

    assert stored.data_url is None
    assert stored.file_id.startswith(f"boxteam-session://{SESSION_A}/attachments/att_")
    blobs = _blob_files(tmp_path)
    assert len(blobs) == 1
    identity = compute_blob_identity(payload)
    assert blobs[0].name == identity.blob_id
    assert blobs[0].parent.parent.parent.name == str(datetime.now(UTC).year)

    content = store.read(SESSION_A, stored.file_id)
    assert content.data == payload
    assert content.content_type == "image/png"
    store.close()


@pytest.mark.asyncio
async def test_persist_inline_deduplicates_and_reuses_first_locator(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    attachment = _attach("duplicate.png", "image/png", b"same-image")

    first = (await store.persist_inline(SESSION_A, [attachment]))[0]
    second = (await store.persist_inline(SESSION_A, [attachment]))[0]

    assert first.file_id == second.file_id
    assert len(_blob_files(tmp_path)) == 1
    store.close()


@pytest.mark.asyncio
async def test_same_digest_across_sessions_reuses_single_physical_blob(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A, SESSION_B)
    payload = b"shared-bytes"

    ref_a = (await store.persist_inline(SESSION_A, [_attach("a.bin", "application/pdf", payload)]))[0]
    ref_b = (await store.persist_inline(SESSION_B, [_attach("b.bin", "application/pdf", payload)]))[0]

    assert len(_blob_files(tmp_path)) == 1
    assert ref_a.file_id != ref_b.file_id
    assert store.read(SESSION_A, ref_a.file_id).data == payload
    assert store.read(SESSION_B, ref_b.file_id).data == payload
    store.close()


@pytest.mark.asyncio
async def test_generic_mime_and_name_suffix_are_preserved(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)

    stored = (
        await store.persist_inline(
            SESSION_A,
            [_attach("document.custom", "application/x-custom-document", b"custom")],
        )
    )[0]

    assert store.read(SESSION_A, stored.file_id).data == b"custom"
    assert store.read(SESSION_A, stored.file_id).content_type == "application/x-custom-document"
    store.close()


@pytest.mark.asyncio
async def test_read_rejects_attachment_from_another_session(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A, SESSION_B)
    ref_a = (await store.persist_inline(SESSION_A, [_attach("a.png", "image/png", b"a")]))[0]
    ref_b = (await store.persist_inline(SESSION_B, [_attach("b.png", "image/png", b"b")]))[0]

    with pytest.raises(ValueError, match="不属于指定会话"):
        store.read(SESSION_B, ref_a.file_id)
    assert store.read(SESSION_B, ref_b.file_id).data == b"b"
    store.close()


@pytest.mark.asyncio
async def test_persist_rejects_mismatched_content_type(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    attachment = AttachmentRef(
        file_id="inline:image.png",
        content_type="image/jpeg",
        data_url=_data_url("image/png", b"image"),
    )

    with pytest.raises(ValueError, match="MIME 不一致"):
        await store.persist_inline(SESSION_A, [attachment])
    assert _blob_files(tmp_path) == []
    store.close()


@pytest.mark.asyncio
async def test_relative_path_exposes_logical_ref_not_physical_locator(
    tmp_path, session_bundle_factory
) -> None:
    """模型/canonical content 只拿逻辑引用，绝不暴露 blob 物理 locator。"""
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    stored = (await store.persist_inline(SESSION_A, [_attach("a.png", "image/png", b"a")]))[0]

    relative = store.relative_path(SESSION_A, stored.file_id)
    assert relative == stored.file_id
    assert relative.startswith(f"boxteam-session://{SESSION_A}/attachments/att_")
    assert ".boxteam/attachments" not in relative
    store.close()


# ----------------------------------------------------------------------
# 缩略图
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_thumbnail_generates_bounded_cached_webp(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    buffer = BytesIO()
    Image.new("RGB", (1600, 900), color=(245, 210, 80)).save(buffer, format="PNG")
    stored = (
        await store.persist_inline(SESSION_A, [_attach("large.png", "image/png", buffer.getvalue())])
    )[0]

    thumbnail = store.read_thumbnail(SESSION_A, stored.file_id)

    assert thumbnail.content_type == "image/webp"
    with Image.open(BytesIO(thumbnail.data)) as image:
        assert max(image.size) == 512
    assert store.read_thumbnail(SESSION_A, stored.file_id).data == thumbnail.data
    store.close()


@pytest.mark.asyncio
async def test_read_thumbnail_reports_corrupt_image_as_value_error(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    truncated = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    stored = (
        await store.persist_inline(SESSION_A, [_attach("bad.png", "image/png", truncated)])
    )[0]

    with pytest.raises(ValueError, match="已损坏或不是有效图片"):
        store.read_thumbnail(SESSION_A, stored.file_id)
    assert store.read(SESSION_A, stored.file_id).data == truncated
    store.close()


@pytest.mark.asyncio
async def test_read_thumbnail_rejects_non_image(tmp_path, session_bundle_factory) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    stored = (
        await store.persist_inline(SESSION_A, [_attach("doc.pdf", "application/pdf", b"%PDF-1.7")])
    )[0]

    with pytest.raises(ValueError, match="不是图片"):
        store.read_thumbnail(SESSION_A, stored.file_id)
    store.close()


# ----------------------------------------------------------------------
# digest 唯一 claim 与 identity conflict（catalog 层，确定性）
# ----------------------------------------------------------------------


def _catalog(tmp_path: Path) -> AttachmentBlobCatalog:
    return AttachmentBlobCatalog(
        tmp_path / ".boxteam" / "attachments" / "catalog.sqlite"
    )


def test_mark_hashed_claims_single_locator_per_digest(tmp_path: Path) -> None:
    """并发相同 digest：唯一 claim 冻结首次日期与 locator，不产生第二 locator。"""
    catalog = _catalog(tmp_path)
    identity = compute_blob_identity(b"same")
    ingest_ids = {
        "first": "a7937b64b8caa58f03721bb6bacf5c78",
        "second": "16367aacb67a4a017c8da8ab95682ccb",
    }
    for index, ingest_hex in ingest_ids.items():
        catalog.create_or_get_ingest_record(
            ingest_idempotency_key=f"attkey_{index}",
            ingest_id=f"ing_{ingest_hex}",
            owner_session_id=SESSION_A,
            owner_thread_id="thr_" + "a" * 32,
            pin_lease_id=f"lease_{index}",
            pin_fencing_token=1,
            pin_captured_generation=1,
            preimage_hash="b" * 64,
            staging_relative_locator=ingest_staging_relative_locator(
                f"ing_{ingest_hex}"
            ),
            max_bytes=1024,
        )

    first_record, first_claim = catalog.mark_ingest_hashed(
        ingest_idempotency_key="attkey_first", identity=identity
    )
    _second_record, second_claim = catalog.mark_ingest_hashed(
        ingest_idempotency_key="attkey_second", identity=identity
    )

    assert first_claim.winning_ingest_id == "ing_a7937b64b8caa58f03721bb6bacf5c78"
    assert second_claim.final_relative_locator == first_claim.final_relative_locator
    assert second_claim.winning_ingest_id == "ing_a7937b64b8caa58f03721bb6bacf5c78"
    assert len(catalog.list_non_terminal_claims()) == 1
    catalog.close()


def test_publish_rejects_same_blob_id_with_different_length(tmp_path: Path) -> None:
    """同一 blob-id 不同 length → blob-identity-conflict，不覆盖。"""
    catalog = _catalog(tmp_path)
    payload = b"correct-bytes"
    identity = compute_blob_identity(payload)
    tampered = type(identity)(blob_id=identity.blob_id, digest=identity.digest, length=99)

    catalog.create_or_get_ingest_record(
        ingest_idempotency_key="attkey_x",
        ingest_id="ing_2d711642b726b04401627ca9fbac32f5",
        owner_session_id=SESSION_A,
        owner_thread_id="thr_" + "a" * 32,
        pin_lease_id="lease_x",
        pin_fencing_token=1,
        pin_captured_generation=1,
        preimage_hash="c" * 64,
        staging_relative_locator=ingest_staging_relative_locator("ing_2d711642b726b04401627ca9fbac32f5"),
        max_bytes=1024,
    )
    catalog.publish_blob_and_owner_ref(
        identity=identity,
        relative_locator=date_bucket_relative_locator(identity.blob_id, datetime.now(UTC).date()),
        ingest_idempotency_key="attkey_x",
        attachment_id="att_first",
        owner_session_id=SESSION_A,
        owner_thread_id="thr_" + "a" * 32,
        file_name="a.bin",
        mime_type="application/pdf",
    )

    with pytest.raises(BlobIdentityConflictError):
        catalog.publish_blob_and_owner_ref(
            identity=tampered,
            relative_locator=date_bucket_relative_locator(
                identity.blob_id, datetime.now(UTC).date()
            ),
            ingest_idempotency_key="attkey_x",
            attachment_id="att_second",
            owner_session_id=SESSION_A,
            owner_thread_id="thr_" + "a" * 32,
            file_name="b.bin",
            mime_type="application/pdf",
        )
    catalog.close()


def test_ingest_record_rejects_preimage_conflict(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    kwargs = dict(
        ingest_idempotency_key="attkey_k",
        ingest_id="ing_8254c329a92850f6d539dd376f4816ee",
        owner_session_id=SESSION_A,
        owner_thread_id="thr_" + "a" * 32,
        pin_lease_id="lease_k",
        pin_fencing_token=1,
        pin_captured_generation=1,
        staging_relative_locator=ingest_staging_relative_locator("ing_8254c329a92850f6d539dd376f4816ee"),
        max_bytes=1024,
    )
    catalog.create_or_get_ingest_record(preimage_hash="a" * 64, **kwargs)

    with pytest.raises(RuntimeError, match="preimage 冲突"):
        catalog.create_or_get_ingest_record(preimage_hash="b" * 64, **kwargs)
    catalog.close()


# ----------------------------------------------------------------------
# pin / fence 发布复核 与 上传-删除竞态
# ----------------------------------------------------------------------


def _flip_fence_to_deleting(tmp_path: Path) -> None:
    sessions_root = tmp_path / ".boxteam" / "sessions"
    session_dir = next(
        path
        for path in sessions_root.rglob(SESSION_A)
        if path.is_dir()
    )
    control = SessionControlStore(session_dir / "session-control.sqlite")
    try:
        assert control.cas_fence_transition(1, "deleting") is True
    finally:
        control.close()


@pytest.mark.asyncio
async def test_publish_rechecks_active_session_under_same_gate(
    tmp_path, session_bundle_factory, monkeypatch
) -> None:
    """rename 后若 Session 已进入 deleting，发布必须被拒绝且不提交 owner ref。"""
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    original = store._commit_staged_blob

    def _rename_then_delete(**kwargs):
        original(**kwargs)
        _flip_fence_to_deleting(tmp_path)

    monkeypatch.setattr(store, "_commit_staged_blob", _rename_then_delete)

    with pytest.raises(SessionDeletionPendingError):
        await store.persist_inline(
            SESSION_A, [_attach("race.png", "image/png", b"race-bytes")]
        )

    assert store._catalog.list_owner_refs_for_session(SESSION_A, active_only=True) == ()
    store.close()


@pytest.mark.asyncio
async def test_publish_rejects_when_pin_token_invalidated(
    tmp_path, session_bundle_factory, monkeypatch
) -> None:
    """pin 未生效（token 被接管/失效）时不得发布 owner reference。"""
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    original = store._commit_staged_blob
    captured: dict[str, object] = {}

    def _rename_then_takeover(**kwargs):
        original(**kwargs)
        sessions_root = tmp_path / ".boxteam" / "sessions"
        session_dir = next(path for path in sessions_root.rglob(SESSION_A) if path.is_dir())
        control = SessionControlStore(session_dir / "session-control.sqlite")
        try:
            lease = control.list_non_terminal_leases()[0]
            captured["lease_id"] = lease.lease_id
            control.takeover_lease(
                lease_id=lease.lease_id, expected_fencing_token=lease.fencing_token
            )
        finally:
            control.close()

    monkeypatch.setattr(store, "_commit_staged_blob", _rename_then_takeover)

    with pytest.raises(SessionDeletionPendingError, match="pin 已失效"):
        await store.persist_inline(
            SESSION_A, [_attach("pin.png", "image/png", b"pin-bytes")]
        )

    assert captured["lease_id"] is not None
    assert store._catalog.list_owner_refs_for_session(SESSION_A, active_only=True) == ()
    store.close()


@pytest.mark.asyncio
async def test_persist_rejected_when_session_already_deleting(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    _flip_fence_to_deleting(tmp_path)

    with pytest.raises(SessionDeletionPendingError):
        await store.persist_inline(SESSION_A, [_attach("x.png", "image/png", b"x")])
    assert _blob_files(tmp_path) == []
    assert store._catalog.list_owner_refs_for_session(SESSION_A) == ()
    store.close()


# ----------------------------------------------------------------------
# 定点恢复 / 引用释放 / GC
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_session_references_keeps_other_session_blob(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A, SESSION_B)
    payload = b"shared"
    ref_a = (await store.persist_inline(SESSION_A, [_attach("a.bin", "application/pdf", payload)]))[0]
    ref_b = (await store.persist_inline(SESSION_B, [_attach("b.bin", "application/pdf", payload)]))[0]

    released = store.release_session_references(SESSION_A)

    assert ref_a.file_id.rsplit("/", 1)[-1] in "".join(released)
    with pytest.raises(FileNotFoundError):
        store.read(SESSION_A, ref_a.file_id)
    assert store.read(SESSION_B, ref_b.file_id).data == payload
    # 另有 session 的 active reference，GC 不得回收该正文。
    store.collect_garbage(retention=timedelta(0))
    assert len(_blob_files(tmp_path)) == 1
    store.close()


@pytest.mark.asyncio
async def test_collect_garbage_tombstones_then_deletes_unreferenced_blob(
    tmp_path, session_bundle_factory
) -> None:
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    ref = (await store.persist_inline(SESSION_A, [_attach("a.bin", "application/pdf", b"gone")]))[0]

    store.release_session_references(SESSION_A)
    collected = store.collect_garbage(retention=timedelta(0))

    assert collected
    assert _blob_files(tmp_path) == []
    with pytest.raises(FileNotFoundError):
        store.read(SESSION_A, ref.file_id)
    store.close()


@pytest.mark.asyncio
async def test_recover_session_cleans_orphan_staging_from_record(
    tmp_path, session_bundle_factory
) -> None:
    """无 record 文件不得被吸收；有 record 的 staging 按 record 定点清理。"""
    store = _store(tmp_path, session_bundle_factory, SESSION_A)
    staging_root = tmp_path / ".boxteam" / "attachments" / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    orphan = staging_root / "ing_88f6811ab5d8fc6d3177f9b7609ae0fc"
    orphan.write_bytes(b"orphan-without-record")

    assert store.recover_session(SESSION_A) == ()
    assert orphan.is_file()

    store._catalog.create_or_get_ingest_record(
        ingest_idempotency_key="attkey_orphan",
        ingest_id="ing_88f6811ab5d8fc6d3177f9b7609ae0fc",
        owner_session_id=SESSION_A,
        owner_thread_id="thr_" + "a" * 32,
        pin_lease_id="lease_orphan",
        pin_fencing_token=1,
        pin_captured_generation=1,
        preimage_hash="d" * 64,
        staging_relative_locator=ingest_staging_relative_locator("ing_88f6811ab5d8fc6d3177f9b7609ae0fc"),
        max_bytes=1024,
    )

    actions = store.recover_session(SESSION_A)
    assert "aborted-ingest" in actions
    assert not orphan.exists()
    store.close()
