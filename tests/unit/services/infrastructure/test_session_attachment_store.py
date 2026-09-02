from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from io import BytesIO

import pytest
from PIL import Image

from app.core.session_paths import SessionPathResolver, physical_segment
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.session_attachment_store import SessionAttachmentStore


def _data_url(content_type: str, data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def test_persist_inline_attachment_under_session_directory(
    tmp_path,
    session_bundle_factory,
):
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "session_media")
    store = SessionAttachmentStore(tmp_path)
    payload = b"\x89PNG\r\n\x1a\nimage-data"

    stored = store.persist_inline(
        "session_media",
        [
            AttachmentRef(
                file_id="inline:example.png",
                name="example.png",
                content_type="image/png",
                data_url=_data_url("image/png", payload),
            )
        ],
    )[0]

    assert stored.data_url is None
    assert stored.file_id.startswith("boxteam-session://session_media/attachments/")
    assert store.read("session_media", stored.file_id).data == payload
    assert store.read("session_media", stored.file_id).content_type == "image/png"


def test_persist_inline_attachment_deduplicates_content(
    tmp_path,
    session_bundle_factory,
):
    session_dir = session_bundle_factory(
        tmp_path / ".boxteam" / "sessions",
        "session_media",
    )
    store = SessionAttachmentStore(tmp_path)
    attachment = AttachmentRef(
        file_id="inline:duplicate.png",
        content_type="image/png",
        data_url=_data_url("image/png", b"same-image"),
    )

    first = store.persist_inline("session_media", [attachment])[0]
    second = store.persist_inline("session_media", [attachment])[0]

    assert first.file_id == second.file_id
    attachment_files = list((session_dir / "attachments").iterdir())
    assert len(attachment_files) == 1


def test_read_thumbnail_generates_bounded_cached_webp(
    tmp_path,
    session_bundle_factory,
):
    source_buffer = BytesIO()
    Image.new("RGB", (1600, 900), color=(245, 210, 80)).save(
        source_buffer,
        format="PNG",
    )
    session_dir = session_bundle_factory(
        tmp_path / ".boxteam" / "sessions",
        "session_thumbnail",
    )
    store = SessionAttachmentStore(tmp_path)
    stored = store.persist_inline(
        "session_thumbnail",
        [
            AttachmentRef(
                file_id="inline:large.png",
                name="large.png",
                content_type="image/png",
                data_url=_data_url("image/png", source_buffer.getvalue()),
            )
        ],
    )[0]

    thumbnail = store.read_thumbnail("session_thumbnail", stored.file_id)

    assert thumbnail.content_type == "image/webp"
    with Image.open(BytesIO(thumbnail.data)) as image:
        assert max(image.size) == 512
    derived = list((session_dir / "attachments" / "derived").glob("*.webp"))
    assert len(derived) == 1
    assert store.read_thumbnail("session_thumbnail", stored.file_id).data == thumbnail.data


def test_read_thumbnail_does_not_upscale_small_image(tmp_path, session_bundle_factory):
    source_buffer = BytesIO()
    Image.new("RGB", (120, 80), color=(20, 40, 60)).save(source_buffer, format="PNG")
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "session_small")
    store = SessionAttachmentStore(tmp_path)
    stored = store.persist_inline(
        "session_small",
        [
            AttachmentRef(
                file_id="inline:small.png",
                content_type="image/png",
                data_url=_data_url("image/png", source_buffer.getvalue()),
            )
        ],
    )[0]

    thumbnail = store.read_thumbnail("session_small", stored.file_id)

    with Image.open(BytesIO(thumbnail.data)) as image:
        assert image.size == (120, 80)


def test_persist_inline_accepts_generic_pdf_attachment(tmp_path, session_bundle_factory):
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "session_pdf")
    store = SessionAttachmentStore(tmp_path)
    payload = b"%PDF-1.7\nnot-a-renderer"

    stored = store.persist_inline(
        "session_pdf",
        [
            AttachmentRef(
                file_id="inline:document.pdf",
                name="document.pdf",
                content_type="application/pdf",
                data_url=_data_url("application/pdf", payload),
            )
        ],
    )[0]

    assert store.read("session_pdf", stored.file_id).data == payload
    assert store.read("session_pdf", stored.file_id).content_type == "application/pdf"
    with pytest.raises(ValueError, match="不是图片"):
        store.read_thumbnail("session_pdf", stored.file_id)


def test_persist_inline_accepts_custom_generic_mime_with_name_suffix(
    tmp_path,
    session_bundle_factory,
):
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "session_custom")
    store = SessionAttachmentStore(tmp_path)

    stored = store.persist_inline(
        "session_custom",
        [
            AttachmentRef(
                file_id="inline:document.custom",
                name="document.custom",
                content_type="application/x-custom-document",
                data_url=_data_url("application/x-custom-document", b"custom"),
            )
        ],
    )[0]

    assert stored.file_id.endswith(".custom")
    assert store.read("session_custom", stored.file_id).data == b"custom"


def test_read_rejects_attachment_from_another_session(
    tmp_path,
    session_bundle_factory,
):
    sessions_root = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions_root, "session_a")
    session_bundle_factory(sessions_root, "session_b")
    store = SessionAttachmentStore(tmp_path)
    stored = store.persist_inline(
        "session_a",
        [
            AttachmentRef(
                file_id="inline:image.png",
                content_type="image/png",
                data_url=_data_url("image/png", b"session-a-image"),
            )
        ],
    )[0]

    with pytest.raises(ValueError, match="不属于指定会话"):
        store.read("session_b", stored.file_id)


def test_persist_rejects_mismatched_content_type(tmp_path, session_bundle_factory):
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "session_media")
    store = SessionAttachmentStore(tmp_path)

    with pytest.raises(ValueError, match="MIME 不一致"):
        store.persist_inline(
            "session_media",
            [
                AttachmentRef(
                    file_id="inline:image.png",
                    content_type="image/jpeg",
                    data_url=_data_url("image/png", b"image"),
                )
            ],
        )


def test_startup_migrates_legacy_inline_image_and_runtime_rejects_inline_id(
    tmp_path,
):
    sessions_root = tmp_path / ".boxteam" / "sessions"
    session_id = "session_legacy_12345678"
    session_dir = sessions_root / physical_segment("历史附件", session_id)
    session_dir.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "title": "历史附件",
                "created_at": now,
                "updated_at": now,
            }
        ),
        encoding="utf-8",
    )
    file_id = "inline:legacy:test.jpg"
    data_url = _data_url("image/jpeg", b"legacy-image")
    logs_root = session_dir / "logs" / "llm_requests"
    logs_root.mkdir(parents=True)
    (logs_root / "100.json").write_text(
        json.dumps(
            {
                "request": {
                    "messages": [
                        {
                            "content": [
                                {"type": "text", "text": "附件 1"},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": data_url},
                                },
                            ],
                            "response_metadata": {
                                "attachments": [
                                    {
                                        "file_id": file_id,
                                        "name": "test.jpg",
                                        "content_type": "image/jpeg",
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    pending_path = session_dir / "pending_requests.json"
    pending_path.write_text(
        json.dumps({"attachments": [{"file_id": file_id}]}),
        encoding="utf-8",
    )

    SessionPathResolver(sessions_root).initialize()
    migrated_file_id = json.loads(pending_path.read_text(encoding="utf-8"))[
        "attachments"
    ][0]["file_id"]
    store = SessionAttachmentStore(tmp_path)

    recovered = store.read(session_id, migrated_file_id)

    assert recovered.data == b"legacy-image"
    assert recovered.content_type == "image/jpeg"
    assert migrated_file_id.startswith(
        f"boxteam-session://{session_id}/attachments/"
    )
    with pytest.raises(ValueError, match="必须使用会话逻辑定位符"):
        store.read(session_id, file_id)
