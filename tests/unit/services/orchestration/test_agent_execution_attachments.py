from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.agents.provider_capabilities import (
    IMAGE_INPUT,
    detect_required_capabilities,
    select_providers_for_capabilities,
)
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
    assert content[1]["type"] == "text"
    assert "本消息包含 1 个附件" in str(content[1]["text"])
    assert "1. test.jpg（图片，image/jpeg）" in str(content[1]["text"])
    assert content[2]["type"] == "text"
    assert "附件 1/1：test.jpg" in str(content[2]["text"])
    assert "image/jpeg" in str(content[2]["text"])
    assert content[3]["type"] == "image_url"
    image_url = content[3]["image_url"]
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
    assert content[1]["type"] == "text"
    assert "本消息包含 1 个附件" in str(content[1]["text"])
    assert content[2]["type"] == "text"
    assert "附件 1/1：test.jpg" in str(content[2]["text"])
    assert content[3] == {
        "type": "image_url",
        "image_url": {"url": data_url},
    }


def test_build_human_content_converts_workspace_video_attachment_to_frames(monkeypatch):
    """视频附件应被转换成按时间顺序排列的 image_url 关键帧块。"""
    if shutil.which("ffmpeg") is None:
        pytest.skip("需要 ffmpeg 才能验证视频抽帧")

    workspace_root = Path.cwd() / "asset" / "default_test_workspace"
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))

    content = build_human_content(
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
    assert "本消息包含 1 个附件" in str(content[1]["text"])
    assert "1. multimodal-test.mp4（视频，video/mp4）" in str(content[1]["text"])
    assert content[2]["type"] == "text"
    assert "附件 1/1：multimodal-test.mp4" in str(content[2]["text"])
    assert "video/mp4" in str(content[2]["text"])
    assert "已抽取为" in str(content[3]["text"])
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


def test_build_human_content_labels_multiple_attachments_in_order():
    """多附件应向模型显式标注序号和文件名，避免最终回复把附件合并理解。"""
    content = build_human_content(
        "请逐个说明附件",
        [
            AttachmentRef(
                file_id="inline:first.jpg",
                name="first.jpg",
                content_type="image/jpeg",
                data_url="data:image/jpeg;base64,/9j/2Q==",
            ),
            AttachmentRef(
                file_id="inline:second.png",
                name="second.png",
                content_type="image/png",
                data_url="data:image/png;base64,iVBORw0KGgo=",
            ),
        ],
    )

    assert isinstance(content, list)
    assert content[1]["type"] == "text"
    assert "本消息包含 2 个附件" in str(content[1]["text"])
    assert "1. first.jpg（图片，image/jpeg）" in str(content[1]["text"])
    assert "2. second.png（图片，image/png）" in str(content[1]["text"])
    label_blocks = [
        block
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and str(block.get("text", "")).startswith("附件 ")
    ]
    assert [block["text"].split("（", 1)[0] for block in label_blocks] == [
        "附件 1/2：first.jpg",
        "附件 2/2：second.png",
    ]


def test_select_providers_for_image_request_uses_image_input_provider_only():
    """图片请求应跳过未声明 image_input 的 provider，避免先产生一串不支持图片的错误。"""
    providers = [
        {"id": "primary", "model": "text-only"},
        {"id": "backup_1", "model": "text-only-2"},
        {"id": "backup_3", "model": "vision", "capabilities": ["image_input"]},
    ]

    selected = select_providers_for_capabilities(providers, {IMAGE_INPUT})

    assert selected == [providers[2]]


def test_select_providers_for_image_request_accepts_legacy_vision_alias():
    """历史 vision 标记仍应按 image_input 处理，避免旧配置突然失效。"""
    providers = [
        {"id": "primary", "model": "text-only"},
        {"id": "backup_3", "model": "vision", "capabilities": ["vision"]},
    ]

    selected = select_providers_for_capabilities(providers, {IMAGE_INPUT})

    assert selected == [providers[1]]


def test_select_providers_for_text_request_keeps_configured_order():
    """普通文本请求仍按 agent 配置顺序进行 fallback。"""
    providers = [
        {"id": "primary", "model": "text-only"},
        {"id": "backup_3", "model": "vision", "capabilities": ["image_input"]},
    ]

    selected = select_providers_for_capabilities(
        providers,
        detect_required_capabilities("请描述项目"),
    )

    assert selected == providers


def test_select_providers_for_image_request_requires_image_input_capability():
    """图片请求没有 image_input provider 时应提前给出可理解配置错误。"""
    with pytest.raises(RuntimeError, match="image_input"):
        select_providers_for_capabilities(
            [{"id": "primary", "model": "text-only"}],
            {IMAGE_INPUT},
        )


def test_detect_required_capabilities_detects_multimodal_content():
    assert detect_required_capabilities(
        [
            {"type": "text", "text": "请描述图片"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
        ]
    ) == {"text_input", "image_input"}
    assert detect_required_capabilities("请描述图片") == {"text_input"}
