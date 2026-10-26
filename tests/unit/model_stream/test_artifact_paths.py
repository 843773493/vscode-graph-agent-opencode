"""回放请求产物与临时文件均保留目标平台的原生长路径能力。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.core.atomic_fs import native_io_path
from app.testing.model_stream import (
    ModelCallIdentity,
    ModelStreamHTTPTransport,
    StreamScenario,
    build_cassette,
    chat_completion_frames,
    replay_model_call,
)


@pytest.mark.parametrize("path_chars", [258, 320])
async def test_replay_long_path(tmp_path: Path, path_chars: int) -> None:
    identity = ModelCallIdentity(
        "ses_00000000400070008000000000000001",
        "thr_00000000400070008000000000000001",
        "00000000-4000-7000-8000-000000000001",
    )
    # 第一例正文在 MAX_PATH 内而临时文件越界，第二例正文和临时文件均越界。
    name = identity.model_call_id + ".json"
    target_chars = max(path_chars, len(str(tmp_path.resolve())) + len(name) + 3)
    artifact_root = tmp_path / ("x" * (target_chars - len(str(tmp_path.resolve())) - len(name) - 2))
    path = artifact_root / name
    assert len(str(path.resolve())) == target_chars
    cassette = build_cassette(
        asset_id="native-artifact-path", model="path-model", identity=identity,
        url="https://provider.example/v1/chat/completions",
        frames=chat_completion_frames(
            response_id="path-response", model="path-model", deltas=({"role": "assistant", "content": "路径可用"},),
            finish_reason="stop",
        ),
    )
    transport = ModelStreamHTTPTransport(
        scenario=StreamScenario("native-artifact-path", Path("<memory>"), cassette, None, {}),
        mode="replay", replay_policy="call_identity", artifact_root=artifact_root,
    )
    request = {"model": "path-model", "stream": True, "messages": [{"role": "user", "content": "输入"}]}
    async with httpx.AsyncClient(transport=transport) as client:
        with replay_model_call(identity):
            response = await client.post("https://provider.example/v1/chat/completions", json=request)
    assert response.status_code == 200 and "路径可用".encode() in response.content
    assert json.loads(native_io_path(path).read_bytes()) == {
        "identity": identity.to_dict(), "interaction_index": 0,
        "scenario_id": "native-artifact-path", "request": request,
    }
    assert [p.name for p in native_io_path(artifact_root).iterdir()] == [name]
    assert transport.hit_counts == {0: 1}
