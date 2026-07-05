from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import httpx
import pytest

from tests.e2e.utils import last_assistant_message, wait_for_job_done


pytestmark = pytest.mark.skipif(
    os.environ.get("CCTQ_API_KEY") is None,
    reason="需要 CCTQ_API_KEY 才能运行真实图片输入模型 e2e",
)


@pytest.fixture(scope="module")
def e2e_config_path() -> str:
    return str(Path.cwd() / "configs" / "tests" / "cctq_vision.jsonc")


def _assert_vision_answer_mentions_shapes(text: str) -> None:
    normalized = text.lower()
    assert ("红" in normalized or "red" in normalized) and (
        "圆" in normalized or "circle" in normalized
    ), f"回复未识别红色圆形: {text}"
    assert ("蓝" in normalized or "blue" in normalized) and (
        "三角" in normalized or "triangle" in normalized
    ), f"回复未识别蓝色三角形: {text}"
    assert ("绿" in normalized or "green" in normalized) and (
        "方" in normalized or "square" in normalized
    ), f"回复未识别绿色方块: {text}"


def _assert_video_answer_mentions_timeline(text: str) -> None:
    normalized = text.lower()
    assert ("红" in normalized or "red" in normalized) and (
        "red start" in normalized
    ), f"回复未识别红色开头: {text}"
    assert ("绿" in normalized or "green" in normalized) and (
        "green middle" in normalized
    ), f"回复未识别绿色中段: {text}"
    assert ("蓝" in normalized or "blue" in normalized) and (
        "blue end" in normalized
    ), f"回复未识别蓝色结尾: {text}"


@pytest.mark.asyncio
async def test_cctq_vision_model_receives_image_attachment(
    client: httpx.AsyncClient,
    is_debug: bool,
) -> None:
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "CCTQ Vision Attachment Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": (
                    "请判断图片中是否有红色圆形、蓝色三角形、绿色方块。"
                    "请分别说明每个图形是否存在。"
                ),
                "attachments": [
                    {
                        "file_id": "assets/test.jpg",
                        "name": "test.jpg",
                        "content_type": "image/jpeg",
                    }
                ],
            },
            "run": {
                "mode": "single_agent",
                "agent_id": "default",
            },
        },
    )
    assert message_response.status_code == 200, message_response.text
    job_id = message_response.json()["data"]["job_id"]

    await wait_for_job_done(
        client,
        job_id,
        max_attempts=100000 if is_debug else 120,
    )

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    result = last_assistant_message(messages_response.json()["data"]["items"])
    _assert_vision_answer_mentions_shapes(result)

    state_response = await client.get(f"/api/v1/sessions/{session_id}/agent-state/messages")
    assert state_response.status_code == 200
    jsonl = state_response.json()["data"]["jsonl"]
    records = [json.loads(line) for line in jsonl.splitlines() if line.strip()]
    user_record = records[0]
    assert user_record["role"] == "user"
    content = user_record["content"]
    assert isinstance(content, list)
    assert any(
        isinstance(block, dict) and block.get("type") == "image_url"
        for block in content
    ), user_record
    assert user_record["response_metadata"]["attachments"][0]["file_id"] == "assets/test.jpg"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="视频附件抽帧需要 ffmpeg")
@pytest.mark.asyncio
async def test_cctq_video_attachment_input_is_sampled_to_frames(
    client: httpx.AsyncClient,
    is_debug: bool,
) -> None:
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "CCTQ Video Attachment Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {
                "content": "请按时间顺序说明视频里的颜色和英文文字。",
                "attachments": [
                    {
                        "file_id": "assets/multimodal-test.mp4",
                        "name": "multimodal-test.mp4",
                        "content_type": "video/mp4",
                    }
                ],
            },
            "run": {
                "mode": "single_agent",
                "agent_id": "default",
            },
        },
    )
    assert message_response.status_code == 200, message_response.text
    job_id = message_response.json()["data"]["job_id"]

    await wait_for_job_done(
        client,
        job_id,
        max_attempts=100000 if is_debug else 120,
    )

    messages_response = await client.get(f"/api/v1/sessions/{session_id}/messages")
    assert messages_response.status_code == 200
    result = last_assistant_message(messages_response.json()["data"]["items"])
    _assert_video_answer_mentions_timeline(result)

    state_response = await client.get(f"/api/v1/sessions/{session_id}/agent-state/messages")
    assert state_response.status_code == 200
    jsonl = state_response.json()["data"]["jsonl"]
    records = [json.loads(line) for line in jsonl.splitlines() if line.strip()]
    user_record = records[0]
    assert user_record["role"] == "user"
    content = user_record["content"]
    assert isinstance(content, list)
    image_blocks = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]
    assert len(image_blocks) >= 3
    assert (
        user_record["response_metadata"]["attachments"][0]["file_id"]
        == "assets/multimodal-test.mp4"
    )
    assert user_record["response_metadata"]["attachments"][0]["content_type"] == "video/mp4"
