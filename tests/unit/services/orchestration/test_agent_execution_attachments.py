from __future__ import annotations

from app.schemas.public_v2.message import AttachmentRef
from app.services.infrastructure.attachment_content_service import build_human_content


def test_build_human_content_converts_workspace_image_attachment(tmp_path, monkeypatch):
    """图片附件应被转换成 OpenAI-compatible image_url content block。"""
    image_path = tmp_path / "assets" / "test.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    content = build_human_content(
        "请描述图片",
        [
            AttachmentRef(
                file_id="assets/test.jpg",
                name="test.jpg",
                content_type="image/jpeg",
            )
        ],
    )

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "请描述图片"}
    assert content[1]["type"] == "image_url"
    image_url = content[1]["image_url"]
    assert isinstance(image_url, dict)
    assert image_url["url"].startswith("data:image/jpeg;base64,")


def test_build_human_content_keeps_inline_image_data_url():
    """浏览器上传的内联图片应直接传给模型，不再回查工作区路径。"""
    data_url = "data:image/jpeg;base64,/9j/2Q=="

    content = build_human_content(
        "请描述图片",
        [
            AttachmentRef(
                file_id="inline:test.jpg",
                name="test.jpg",
                content_type="image/jpeg",
                data_url=data_url,
            )
        ],
    )

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "请描述图片"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": data_url},
    }
