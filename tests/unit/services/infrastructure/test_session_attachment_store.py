from __future__ import annotations

import base64
import json

import pytest

from app.schemas.public_v2.message import AttachmentRef
from app.services.infrastructure.session_attachment_store import SessionAttachmentStore


def _data_url(content_type: str, data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def test_persist_inline_attachment_under_session_directory(tmp_path):
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
    assert stored.file_id.startswith(
        ".boxteam/sessions/session_media/attachments/"
    )
    assert store.read("session_media", stored.file_id).data == payload
    assert store.read("session_media", stored.file_id).content_type == "image/png"


def test_persist_inline_attachment_deduplicates_content(tmp_path):
    store = SessionAttachmentStore(tmp_path)
    attachment = AttachmentRef(
        file_id="inline:duplicate.png",
        content_type="image/png",
        data_url=_data_url("image/png", b"same-image"),
    )

    first = store.persist_inline("session_media", [attachment])[0]
    second = store.persist_inline("session_media", [attachment])[0]

    assert first.file_id == second.file_id
    attachment_files = list(
        (tmp_path / ".boxteam" / "sessions" / "session_media" / "attachments").iterdir()
    )
    assert len(attachment_files) == 1


def test_read_rejects_attachment_from_another_session(tmp_path):
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


def test_persist_rejects_mismatched_content_type(tmp_path):
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


def test_read_recovers_legacy_inline_image_from_llm_request_log(tmp_path):
    store = SessionAttachmentStore(tmp_path)
    file_id = "inline:legacy:test.jpg"
    data_url = _data_url("image/jpeg", b"legacy-image")
    logs_root = (
        tmp_path
        / ".boxteam"
        / "sessions"
        / "session_legacy"
        / "logs"
        / "llm_requests"
    )
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

    recovered = store.read("session_legacy", file_id)

    assert recovered.data == b"legacy-image"
    assert recovered.content_type == "image/jpeg"
    cache_files = list(
        (tmp_path / ".boxteam" / "sessions" / "session_legacy" / "attachments").glob(
            "legacy-*"
        )
    )
    assert len(cache_files) == 1
