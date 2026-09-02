from __future__ import annotations

import base64
import shutil
from io import BytesIO
from pathlib import Path

import pytest

from app.agents.provider_capabilities import (
    detect_required_capabilities,
)
from app.schemas.internal_v2.message import AttachmentRef
from app.services.business.user_content_builder import UserContentBuilder


def _image_bytes(format_name: str, size: tuple[int, int] = (80, 60)) -> bytes:
    buffer = BytesIO()
    from PIL import Image

    Image.new("RGB", size, color=(220, 160, 40)).save(buffer, format=format_name)
    return buffer.getvalue()


def _build_user_content(
    message: str,
    attachments: list[AttachmentRef],
    *,
    workspace_root: Path | None = None,
) -> str | list[dict[str, object]]:
    return UserContentBuilder(workspace_root=workspace_root).build(
        message,
        attachments,
    ).content


def test_user_content_builder_converts_workspace_image_attachment(tmp_path, monkeypatch):
    """图片附件应被转换成 OpenAI-compatible image_url content block。"""
    image_path = tmp_path / "assets" / "test.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(_image_bytes("JPEG", (1600, 900)))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    content = _build_user_content(
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
    assert content[1]["type"] == "text"
    assert "path=\"assets/test.jpg\"" in str(content[1]["text"])
    assert "preview_status=\"available\"" in str(content[1]["text"])
    assert content[1]["metadata"]["kind"] == "attachment_manifest"
    assert content[2]["type"] == "image_url"
    assert content[2]["metadata"]["kind"] == "attachment_preview"
    image_url = content[2]["image_url"]
    assert isinstance(image_url, dict)
    preview_url = image_url["url"]
    assert preview_url.startswith("data:image/webp;base64,")
    preview_bytes = base64.b64decode(preview_url.split(",", 1)[1])
    from PIL import Image

    with Image.open(BytesIO(preview_bytes)) as preview:
        assert max(preview.size) == 512
    assert len(preview_bytes) < len(image_path.read_bytes())


def test_user_content_builder_rejects_unpersisted_inline_image():
    """Agent 运行时只接受已经持久化的附件定位符。"""
    data_url = "data:image/jpeg;base64,/9j/2Q=="

    with pytest.raises(ValueError, match="必须先持久化"):
        _build_user_content(
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


def test_user_content_builder_converts_workspace_video_attachment_to_frames(monkeypatch):
    """视频附件应被转换成按时间顺序排列的 image_url 关键帧块。"""
    if shutil.which("ffmpeg") is None:
        pytest.skip("需要 ffmpeg 才能验证视频抽帧")

    workspace_root = (
        Path.cwd()
        / "tests"
        / "fixtures"
        / "workspaces"
        / "default_test_workspace"
    )
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))

    content = _build_user_content(
        "请描述视频",
        [
            AttachmentRef(
                file_id="assets/multimodal-test.mp4",
                name="multimodal-test.mp4",
                content_type="video/mp4",
            )
        ],
    )

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "请描述视频"}
    assert content[1]["type"] == "text"
    assert "path=\"assets/multimodal-test.mp4\"" in str(content[1]["text"])
    assert content[1]["metadata"]["kind"] == "attachment_manifest"
    image_blocks = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]
    assert len(image_blocks) >= 3
    assert all(
        isinstance(block["image_url"], dict)
        and str(block["image_url"]["url"]).startswith("data:image/jpeg;base64,")
        for block in image_blocks
    )


def test_user_content_builder_labels_multiple_attachments_in_order(tmp_path):
    """多附件应向模型显式标注序号和文件名，避免最终回复把附件合并理解。"""
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "first.jpg").write_bytes(_image_bytes("JPEG"))
    (assets / "second.png").write_bytes(_image_bytes("PNG"))
    content = _build_user_content(
        "请逐个说明附件",
        [
            AttachmentRef(
                file_id="assets/first.jpg",
                name="first.jpg",
                content_type="image/jpeg",
            ),
            AttachmentRef(
                file_id="assets/second.png",
                name="second.png",
                content_type="image/png",
            ),
        ],
        workspace_root=tmp_path,
    )

    assert isinstance(content, list)
    assert content[1]["type"] == "text"
    manifest_blocks = [
        block
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and block.get("metadata", {}).get("kind") == "attachment_manifest"
    ]
    assert [block["metadata"]["name"] for block in manifest_blocks] == [
        "first.jpg",
        "second.png",
    ]
    assert [block["metadata"]["path"] for block in manifest_blocks] == [
        "assets/first.jpg",
        "assets/second.png",
    ]


def test_detect_required_capabilities_detects_multimodal_content():
    assert detect_required_capabilities(
        [
            {"type": "text", "text": "请描述图片"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
        ]
    ) == {"text_input", "image_input"}
    assert detect_required_capabilities("请描述图片") == {"text_input"}


def test_generated_attachment_preview_is_optional_for_capability_routing():
    assert detect_required_capabilities(
        [
            {"type": "text", "text": "附件路径"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/webp;base64,preview"},
                "metadata": {
                    "origin": "generated",
                    "kind": "attachment_preview",
                    "schema_version": 1,
                    "file_id": "boxteam-session://session/attachments/a.png",
                },
            },
        ]
    ) == {"text_input"}


def test_user_content_builder_keeps_generic_file_as_manifest_only(tmp_path):
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.7")

    content = _build_user_content(
        "请分析报告",
        [
            AttachmentRef(
                file_id="report.pdf",
                name="报告.pdf",
                content_type="application/pdf",
            )
        ],
        workspace_root=tmp_path,
    )

    assert isinstance(content, list)
    assert len(content) == 2
    assert content[1]["metadata"]["kind"] == "attachment_manifest"
    assert content[1]["metadata"]["content_type"] == "application/pdf"
    assert not any(block.get("type") == "image_url" for block in content)


def test_user_content_builder_reports_preview_failure_without_losing_path(tmp_path):
    image_path = tmp_path / "broken.png"
    image_path.write_bytes(b"not-an-image")

    content = _build_user_content(
        "请检查附件",
        [
            AttachmentRef(
                file_id="broken.png",
                name='危险"文件.png',
                content_type="image/png",
            )
        ],
        workspace_root=tmp_path,
    )

    assert isinstance(content, list)
    manifest = content[1]
    assert manifest["metadata"]["kind"] == "attachment_manifest"
    assert manifest["metadata"]["preview_status"] == "failed"
    assert 'name="危险&quot;文件.png"' in str(manifest["text"])
    assert not any(block.get("type") == "image_url" for block in content)
