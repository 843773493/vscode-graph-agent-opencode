from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from io import BytesIO

import pytest
from PIL import Image

from app.core.session_catalog_migration import SessionCatalogMigrator
from app.core.session_paths import SessionPathResolver, physical_segment
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.message import AttachmentRef
from app.services.infrastructure.session_attachment_store import SessionAttachmentStore


def _data_url(content_type: str, data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def test_persist_inline_attachment_under_session_directory(
    tmp_path,
    session_bundle_factory,
):
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "ses_8044804392e9434e8f61961ec7604c3b")
    store = SessionAttachmentStore(tmp_path)
    payload = b"\x89PNG\r\n\x1a\nimage-data"

    stored = store.persist_inline(
        "ses_8044804392e9434e8f61961ec7604c3b",
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
    assert stored.file_id.startswith("boxteam-session://ses_8044804392e9434e8f61961ec7604c3b/attachments/")
    assert store.read("ses_8044804392e9434e8f61961ec7604c3b", stored.file_id).data == payload
    assert store.read("ses_8044804392e9434e8f61961ec7604c3b", stored.file_id).content_type == "image/png"


def test_persist_inline_attachment_deduplicates_content(
    tmp_path,
    session_bundle_factory,
):
    session_dir = session_bundle_factory(
        tmp_path / ".boxteam" / "sessions",
        "ses_8044804392e9434e8f61961ec7604c3b",
    )
    store = SessionAttachmentStore(tmp_path)
    attachment = AttachmentRef(
        file_id="inline:duplicate.png",
        content_type="image/png",
        data_url=_data_url("image/png", b"same-image"),
    )

    first = store.persist_inline("ses_8044804392e9434e8f61961ec7604c3b", [attachment])[0]
    second = store.persist_inline("ses_8044804392e9434e8f61961ec7604c3b", [attachment])[0]

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
        "ses_51f65446626742a1862c659358c12151",
    )
    store = SessionAttachmentStore(tmp_path)
    stored = store.persist_inline(
        "ses_51f65446626742a1862c659358c12151",
        [
            AttachmentRef(
                file_id="inline:large.png",
                name="large.png",
                content_type="image/png",
                data_url=_data_url("image/png", source_buffer.getvalue()),
            )
        ],
    )[0]

    thumbnail = store.read_thumbnail("ses_51f65446626742a1862c659358c12151", stored.file_id)

    assert thumbnail.content_type == "image/webp"
    with Image.open(BytesIO(thumbnail.data)) as image:
        assert max(image.size) == 512
    derived = list((session_dir / "attachments" / "derived").glob("*.webp"))
    assert len(derived) == 1
    assert store.read_thumbnail("ses_51f65446626742a1862c659358c12151", stored.file_id).data == thumbnail.data


def test_read_thumbnail_does_not_upscale_small_image(tmp_path, session_bundle_factory):
    source_buffer = BytesIO()
    Image.new("RGB", (120, 80), color=(20, 40, 60)).save(source_buffer, format="PNG")
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "ses_ed806bd3191449c685068a106941dd2e")
    store = SessionAttachmentStore(tmp_path)
    stored = store.persist_inline(
        "ses_ed806bd3191449c685068a106941dd2e",
        [
            AttachmentRef(
                file_id="inline:small.png",
                content_type="image/png",
                data_url=_data_url("image/png", source_buffer.getvalue()),
            )
        ],
    )[0]

    thumbnail = store.read_thumbnail("ses_ed806bd3191449c685068a106941dd2e", stored.file_id)

    with Image.open(BytesIO(thumbnail.data)) as image:
        assert image.size == (120, 80)


def test_persist_inline_accepts_generic_pdf_attachment(tmp_path, session_bundle_factory):
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "ses_3fd32bd7260440fc80eba572c1643e05")
    store = SessionAttachmentStore(tmp_path)
    payload = b"%PDF-1.7\nnot-a-renderer"

    stored = store.persist_inline(
        "ses_3fd32bd7260440fc80eba572c1643e05",
        [
            AttachmentRef(
                file_id="inline:document.pdf",
                name="document.pdf",
                content_type="application/pdf",
                data_url=_data_url("application/pdf", payload),
            )
        ],
    )[0]

    assert store.read("ses_3fd32bd7260440fc80eba572c1643e05", stored.file_id).data == payload
    assert store.read("ses_3fd32bd7260440fc80eba572c1643e05", stored.file_id).content_type == "application/pdf"
    with pytest.raises(ValueError, match="不是图片"):
        store.read_thumbnail("ses_3fd32bd7260440fc80eba572c1643e05", stored.file_id)


def test_read_thumbnail_reports_corrupt_image_as_value_error(
    tmp_path,
    session_bundle_factory,
):
    """损坏的图片必须产生领域 ValueError，而不是让 PIL 的 OSError 冒泡成 500。"""
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "ses_95b23364ba2c4d638f0492cf5bcb7e0d")
    store = SessionAttachmentStore(tmp_path)
    truncated = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    stored = store.persist_inline(
        "ses_95b23364ba2c4d638f0492cf5bcb7e0d",
        [
            AttachmentRef(
                file_id="inline:truncated.png",
                name="truncated.png",
                content_type="image/png",
                data_url=_data_url("image/png", truncated),
            )
        ],
    )[0]

    with pytest.raises(ValueError, match="已损坏或不是有效图片"):
        store.read_thumbnail("ses_95b23364ba2c4d638f0492cf5bcb7e0d", stored.file_id)
    # 原始字节仍然可读，便于前端展示并让用户重新上传。
    assert store.read("ses_95b23364ba2c4d638f0492cf5bcb7e0d", stored.file_id).data == truncated


def test_persist_inline_accepts_custom_generic_mime_with_name_suffix(
    tmp_path,
    session_bundle_factory,
):
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "ses_96b0fba134fd47e4857e34fee945a9a8")
    store = SessionAttachmentStore(tmp_path)

    stored = store.persist_inline(
        "ses_96b0fba134fd47e4857e34fee945a9a8",
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
    assert store.read("ses_96b0fba134fd47e4857e34fee945a9a8", stored.file_id).data == b"custom"


def test_read_rejects_attachment_from_another_session(
    tmp_path,
    session_bundle_factory,
):
    sessions_root = tmp_path / ".boxteam" / "sessions"
    session_bundle_factory(sessions_root, "ses_aed5707da48947108df3da01acc1b1b0")
    session_bundle_factory(sessions_root, "ses_ea102cf1fb1a40d482bdc155df780f85")
    store = SessionAttachmentStore(tmp_path)
    stored = store.persist_inline(
        "ses_aed5707da48947108df3da01acc1b1b0",
        [
            AttachmentRef(
                file_id="inline:image.png",
                content_type="image/png",
                data_url=_data_url("image/png", b"session-a-image"),
            )
        ],
    )[0]

    with pytest.raises(ValueError, match="不属于指定会话"):
        store.read("ses_ea102cf1fb1a40d482bdc155df780f85", stored.file_id)


def test_persist_rejects_mismatched_content_type(tmp_path, session_bundle_factory):
    session_bundle_factory(tmp_path / ".boxteam" / "sessions", "ses_8044804392e9434e8f61961ec7604c3b")
    store = SessionAttachmentStore(tmp_path)

    with pytest.raises(ValueError, match="MIME 不一致"):
        store.persist_inline(
            "ses_8044804392e9434e8f61961ec7604c3b",
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
    session_id = "ses_4a2c165f3f3345448447f8e9fe15a9ea"
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

    # 旧形态 inline 迁移由上面的 legacy initialize 完成；随后必须经
    # SessionCatalogMigrator 建立唯一 SQLite authority，工厂 resolver 才能
    # 解析该会话。
    workspace_id = load_or_create_workspace_id(tmp_path)
    asyncio.run(
        SessionCatalogMigrator(
            workspace_id=workspace_id,
            sessions_root=sessions_root,
            database_path=tmp_path
            / ".boxteam"
            / "navigation"
            / "session-catalog.sqlite",
            maintenance_root=tmp_path / "maintenance",
        ).migrate()
    )

    store = SessionAttachmentStore(tmp_path)

    recovered = store.read(session_id, migrated_file_id)

    assert recovered.data == b"legacy-image"
    assert recovered.content_type == "image/jpeg"
    assert migrated_file_id.startswith(
        f"boxteam-session://{session_id}/attachments/"
    )
    with pytest.raises(ValueError, match="必须使用会话逻辑定位符"):
        store.read(session_id, file_id)
