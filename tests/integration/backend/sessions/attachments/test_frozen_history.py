"""产品生成的附件输入在目录修改、回收与重启后仍能精确重建。"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import commentjson
import httpx
import pytest
from PIL import Image

from app.agents.providers.wire_finalize import ChatWireProfile, finalize_provider_wire
from app.core.path_utils import get_session_path_resolver
from app.core.resource_uri import (
    file_uri_child,
    parse_attachment_uri,
    parse_resource_uri,
)
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
)
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.integration.backend.sessions.rollout_context.test_execution_history_storage import (
    HistoryTarget,
)
from tests.integration.backend.sessions.rollout_context.test_execution_history_storage import (
    history_target as history_target,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS, acquire_gateway_guest
from tests.support.sealed_request_audit import audit_sealed_requests


@pytest.fixture
def integration_workspace_config_path(integration_workspace_config_path: str) -> str:
    """只在本测试复制的配置中声明图片能力，不依赖当前模型目录。"""
    path = Path(integration_workspace_config_path)
    config = commentjson.loads(path.read_text(encoding="utf-8"))
    primary = next(provider for provider in config["llm"]["providers"] if provider["id"] == "primary")
    primary["api_mode"]["model_info"]["supports_vision"] = True
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def plan(target: HistoryTarget, session: str, thread: str, replies: list[dict[str, object]]) -> list[str]:
    config = commentjson.loads((target.workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))
    provider = next(provider for provider in config["llm"]["providers"] if provider["id"] == "primary")
    interactions = [build_cassette(
        asset_id="attachment-frozen-history", model=provider["model"],
        url=provider["endpoint"] + "/chat/completions",
        frames=chat_completion_frames(
            response_id=f"chatcmpl-attachment-history-{index}", model=provider["model"],
            deltas=({"role": "assistant", **reply},),
            finish_reason="tool_calls" if "tool_calls" in reply else "stop",
        ),
    ).raw["interactions"][0] for index, reply in enumerate(replies)]
    return target.replay.control.plan_model_calls(session, thread, interactions)


def tool(name: str, arguments: dict[str, object], identity: str) -> dict[str, object]:
    return {"tool_calls": [{"index": 0, "id": identity, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
    }}]}


@pytest.mark.asyncio
async def test_attachment_request_rebuild_survives_derived_edit_gc_and_restart(history_target: HistoryTarget) -> None:
    target = history_target
    assert target.backend is not None
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{target.gateway_port}", timeout=60,
        headers={**LOCAL_TOKEN_HEADERS, "X-Workspace-ID": target.backend.workspace_id},
    ) as client:
        await acquire_gateway_guest(client)
        health = await client.get("/api/gateway/health")
        assert health.status_code == 200, health.text
        gateway_id = health.json()["data"]["gateway_id"]
        session = await create_session(client, "附件冻结输入回收验收")
        main = await client.get(f"/api/v1/sessions/{session}/main-thread")
        assert main.status_code == 200, main.text
        thread = main.json()["data"]["thread_id"]
        image = BytesIO()
        Image.new("RGB", (800, 400), (35, 125, 210)).save(image, format="PNG")
        original = image.getvalue()
        calls = plan(target, session, thread, [{"content": "预览输入已冻结。"}])
        uploaded = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "查看图片并保留原件。", "attachments": [{
                "file_id": "inline:input", "name": "input.png", "content_type": "image/png",
                "data_url": "data:image/png;base64," + base64.b64encode(original).decode(),
            }]}, "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert uploaded.status_code == 200, uploaded.text
        await wait_for_job_done(client, uploaded.json()["data"]["job_id"])
        history = await client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 10})
        assert history.status_code == 200, history.text
        ref, = history.json()["data"]["items"][0]["user_messages"][0]["attachments"]
        derived = file_uri_child(ref["directory_uri"], "derived", "result.txt")
        assert parse_resource_uri(derived).gateway_authority == gateway_id
        change_uri = parse_resource_uri(derived, local_gateway_id=gateway_id).display_uri
        frozen_text = "已冻结的派生输入 Ω\n"
        calls += plan(target, session, thread, [
            tool("write_file", {"file_path": derived, "content": frozen_text, "expected_version": "absent"}, "write-derived"),
            tool("read_file", {"path": derived}, "read-derived"),
            tool("read_file", {"path": ref["file_id"]}, "read-original"),
            {"content": "原件和派生输入均已消费。"},
        ])
        accepted = await client.post(f"/api/v1/sessions/{session}/messages", json={
            "message": {"content": "生成派生结果，然后读取结果与图片原件。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert accepted.status_code == 200, accepted.text
        await wait_for_job_done(client, accepted.json()["data"]["job_id"])
        changes = await client.get(f"/api/v1/sessions/{session}/changesets/all")
        assert changes.status_code == 200, changes.text
        change, = changes.json()["data"]["files"]
        assert (change["file_path"], change["after_preview"]) == (change_uri, frozen_text)
        reviewed = await client.post(f"/api/v1/sessions/{session}/changesets/all/review", json={
            "file_path": derived, "reviewed": True,
        })
        assert reviewed.status_code == 200 and reviewed.json()["data"]["file_path"] == change_uri, reviewed.text
        current = await client.get("/api/v1/workspace/files/content", params={"path": derived})
        assert current.status_code == 200, current.text
        assert current.json()["data"]["content"] == frozen_text
        changed_text = "当前文件已改变，不能用它重建旧请求。\n"
        saved = await client.put("/api/v1/workspace/files/content", params={"path": derived}, json={
            "content": changed_text, "expected_revision": current.json()["data"]["revision"],
        })
        assert saved.status_code == 200 and saved.json()["data"]["content"] == changed_text, saved.text
        resolver = get_session_path_resolver(target.workspace / ".boxteam/sessions")
        index = resolver.resolve_thread_node(session, thread) / "rollout/index.sqlite"
        canonical = index.with_name("rollout.jsonl").read_bytes()
        captures = {call: json.loads((target.replay.artifacts / (call + ".json")).read_text()) for call in calls}
        last_wire = canonical_json_bytes(captures[calls[-1]]["request"])
        assert frozen_text.strip().encode() in last_wire and changed_text.strip().encode() not in last_wire
        assert base64.b64encode(original) in last_wire
        assert b"data:image/webp;base64," in last_wire
        profile = ChatWireProfile(
            provider_id="primary", custom_llm_provider="openai", model="e2e-stub-model",
            reasoning_content_replay=True, image_input_replay=True,
        )
        frozen = {}
        with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as connection:
            assemblies = dict(connection.execute("SELECT model_call_id,assembly_id FROM model_calls"))
        with closing(AttachmentBlobStore(target.workspace)) as store:
            saver = RolloutCheckpointSaver(target.workspace / ".boxteam/sessions", attachment_store=store)
            for call in calls:
                snapshot = saver.get_context_assembly(session, thread, assembly_id=assemblies[call])
                frozen[call] = snapshot.to_dict()

        await asyncio.to_thread(target.restart)
        with closing(AttachmentBlobStore(target.workspace)) as store:
            aid = parse_attachment_uri(ref["file_id"], require_file=True).attachment_id
            directory = store._catalog.get_directory(aid)
            assert directory is not None and directory.gc_after is not None
            root = resolve_attachment_directory(store._attachments_root, directory.relative_locator)
            assert (root / "original/content").read_bytes() == original
            assert (root / "preview/512.webp").is_file()
            with Image.open(root / "preview/512.webp") as preview:
                assert preview.size == (512, 256)
            assert (root / "derived/result.txt").read_text() == changed_text
            # 使用原 owner 的持久期限和外置时钟；不手填关联、canonical 或执行状态。
            expires = datetime.fromisoformat(directory.gc_after) + timedelta(seconds=1)
        with closing(AttachmentBlobStore(target.workspace, clock=lambda: expires)) as store:
            collected = await store.collect_garbage(leases=ExternalResourceLeaseLedger(
                state_path=target.workspace / ".boxteam/resources.json",
            ))
            assert collected == (aid,)
            assert not root.exists()

        await asyncio.to_thread(target.restart)
        unavailable = await client.get(f"/api/v1/sessions/{session}/attachments/content", params={"file_id": ref["file_id"]})
        assert unavailable.status_code == 404, unavailable.text
        loaded = await client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 10})
        assert loaded.status_code == 200, loaded.text
        historical_ref, = loaded.json()["data"]["items"][0]["user_messages"][0]["attachments"]
        assert historical_ref["file_id"] == ref["file_id"] and historical_ref["availability"] == "unavailable"
        changes = await client.get(f"/api/v1/sessions/{session}/changesets/all")
        assert changes.status_code == 200, changes.text
        change, = changes.json()["data"]["files"]
        assert (change["file_path"], change["after_preview"], change["reviewed"]) == (change_uri, frozen_text, True)
        with closing(AttachmentBlobStore(target.workspace)) as store:
            saver = RolloutCheckpointSaver(target.workspace / ".boxteam/sessions", attachment_store=store)
            for call in calls:
                snapshot = saver.get_context_assembly(session, thread, assembly_id=assemblies[call])
                assert snapshot.to_dict() == frozen[call]
                assert snapshot.provider_version == profile.identity()
                messages, tools, losses = saver.project_context_plan_to_provider(
                    session, thread, snapshot.as_sealed_plan(), target_format="chat_completions",
                    include_runtime_notices=True, include_summaries=True,
                )
                assert not losses
                params = {key: value for key, value in snapshot.request_hash_preimage.items() if key not in {"messages", "tools"}}
                rebuilt = normalize_wire_request(finalize_provider_wire(
                    messages=messages, tools=tools, native_request=None, profile=profile, params=params,
                ))
                assert rebuilt == normalize_wire_request(captures[call]["request"])
                assert rebuilt == snapshot.request_hash_preimage
                snapshot.validate_hashes()
        assert index.with_name("rollout.jsonl").read_bytes() == canonical
        evidence = audit_sealed_requests(index, (resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"), captures)
        (target.replay.artifacts / "attachment-gc-rebuild.json").write_text(json.dumps(evidence, indent=2) + "\n")
