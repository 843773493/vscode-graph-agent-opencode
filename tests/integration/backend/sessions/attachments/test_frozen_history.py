"""产品生成的附件输入在目录修改、回收与重启后仍能精确重建。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
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
from app.schemas.internal_v2.session import SessionForkResultDTO
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
    _fork,
)
from tests.integration.backend.sessions.rollout_context.test_execution_history_storage import (
    history_target as history_target,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.fork_deadline import ForkDeadlineRegistry
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS, acquire_gateway_guest
from tests.support.sealed_request_audit import audit_sealed_requests
from tests.support.session_navigation.http_operations import submit_navigation_operation


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
    cassettes = [build_cassette(
        asset_id="attachment-frozen-history", model=provider["model"],
        url=provider["endpoint"] + "/chat/completions",
        frames=chat_completion_frames(
            response_id=f"chatcmpl-attachment-history-{index}", model=provider["model"],
            deltas=({"role": "assistant", **reply},),
            finish_reason="tool_calls" if "tool_calls" in reply else "stop",
        ),
    ) for index, reply in enumerate(replies)]
    interactions = []
    for cassette in cassettes:
        records = cassette.raw["interactions"]
        assert isinstance(records, list) and len(records) == 1
        interactions.append(records[0])
    return target.replay.control.plan_model_calls(session, thread, interactions)


def tool(name: str, arguments: dict[str, object], identity: str) -> dict[str, object]:
    return {"tool_calls": [{"index": 0, "id": identity, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
    }}]}


def assert_frozen_requests(
    saver: RolloutCheckpointSaver, session: str, thread: str, *,
    assemblies: Mapping[str, str], frozen: Mapping[str, dict[str, object]],
    captures: Mapping[str, dict[str, object]], profile: ChatWireProfile,
) -> None:
    """源与副本使用同一精确重建断言，不从 live 目录补历史输入。"""
    for call, assembly in assemblies.items():
        snapshot = saver.get_context_assembly(session, thread, assembly_id=assembly)
        assert snapshot.to_dict() == frozen[call]
        assert snapshot.provider_version == profile.identity()
        messages, tools, losses = saver.project_context_plan_to_provider(
            session, thread, snapshot.as_sealed_plan(), target_format="chat_completions",
            include_runtime_notices=True, include_summaries=True,
        )
        assert not losses
        preimage = snapshot.request_hash_preimage
        assert isinstance(preimage, dict)
        params = {key: value for key, value in preimage.items() if key not in {"messages", "tools"}}
        rebuilt = normalize_wire_request(finalize_provider_wire(
            messages=messages, tools=tools, native_request=None, profile=profile, params=params,
        ))
        assert rebuilt == normalize_wire_request(captures[call]["request"]) == snapshot.request_hash_preimage
        snapshot.validate_hashes()


@pytest.mark.asyncio
async def test_attachment_request_rebuild_survives_derived_edit_release_and_restart(
    history_target: HistoryTarget, fork_deadlines: ForkDeadlineRegistry,
) -> None:
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
        captures = {call: json.loads((target.replay.artifacts / (call + ".json")).read_text(encoding="utf-8")) for call in calls}
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

        point = await client.get(f"/api/v1/sessions/{session}/fork-source-point", params={"mode": "full_rollout_copy"})
        assert point.status_code == 200, point.text
        forked = SessionForkResultDTO.model_validate(await _fork(
            client, session, point.json()["data"], fork_deadlines,
            mode="full_rollout_copy", key="attachment-frozen-history-fork",
        ))
        fork_session, fork_thread = forked.session.session_id, forked.thread_id
        assert fork_session != session and fork_thread != thread
        fork_index = resolver.resolve_thread_node(fork_session, fork_thread) / "rollout/index.sqlite"
        assert fork_index.with_name("rollout.jsonl").read_bytes() == canonical
        # 副本的当前归属与历史执行身份分开；不能靠改写原 assembly 获得可读副本。
        with closing(sqlite3.connect(fork_index.as_uri() + "?mode=ro", uri=True)) as connection:
            assert connection.execute("SELECT session_id,thread_id FROM database_meta").fetchone() == (fork_session, fork_thread)
            imported_calls = connection.execute(
                "SELECT session_id,thread_id,model_call_id,assembly_id FROM model_calls ORDER BY model_call_id",
            ).fetchall()
            assert {(row[0], row[1]) for row in imported_calls} == {(session, thread)}
            assert {row[2]: row[3] for row in imported_calls} == assemblies

        # 未建图的冷 fork 由原发布事实保活；正式执行后才由 runtime 取得并释放关联。
        warm_call, = plan(target, fork_session, fork_thread, [{"content": "副本已消费共享目录。"}])
        warmed = await client.post(f"/api/v1/sessions/{fork_session}/messages", json={
            "message": {"content": "使用复制的历史完成一次执行。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert warmed.status_code == 200, warmed.text
        await wait_for_job_done(client, warmed.json()["data"]["job_id"])
        warm_capture = json.loads((target.replay.artifacts / (warm_call + ".json")).read_text(encoding="utf-8"))
        assert (warm_capture["identity"]["session_id"], warm_capture["identity"]["thread_id"]) == (fork_session, fork_thread)
        fork_canonical = fork_index.with_name("rollout.jsonl").read_bytes()
        assert fork_canonical.startswith(canonical) and fork_canonical != canonical
        with closing(sqlite3.connect(fork_index.as_uri() + "?mode=ro", uri=True)) as connection:
            fork_assemblies = dict(connection.execute("SELECT model_call_id,assembly_id FROM model_calls"))
        with closing(AttachmentBlobStore(target.workspace)) as store:
            saver = RolloutCheckpointSaver(target.workspace / ".boxteam/sessions", attachment_store=store)
            fork_frozen = {**frozen, warm_call: saver.get_context_assembly(
                fork_session, fork_thread, assembly_id=fork_assemblies[warm_call],
            ).to_dict()}
        fork_captures = {**captures, warm_call: warm_capture}

        await asyncio.to_thread(target.restart)
        with closing(AttachmentBlobStore(target.workspace)) as store:
            aid = parse_attachment_uri(ref["file_id"], require_file=True).attachment_id
            assert aid is not None
            directory = store._catalog.get_directory(aid)
            assert directory is not None and directory.release_pending == 0
            root = resolve_attachment_directory(store._attachments_root, directory.relative_locator)
            assert (root / "original/content").read_bytes() == original
            assert (root / "preview/512.webp").is_file()
            with Image.open(root / "preview/512.webp") as preview:
                assert preview.size == (512, 256)
            assert (root / "derived/result.txt").read_text(encoding="utf-8") == changed_text
        with closing(AttachmentBlobStore(target.workspace, leases=ExternalResourceLeaseLedger(
                state_path=target.workspace / ".boxteam/resources.json",
            ))) as store:
            assert await store.process_pending_directories() == ()
            assert (root / "derived/result.txt").read_text(encoding="utf-8") == changed_text

        await asyncio.to_thread(target.restart)
        available = await client.get(f"/api/v1/sessions/{session}/attachments/content", params={"file_id": ref["file_id"]})
        assert available.status_code == 200 and available.content == original, available.text
        loaded = await client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 10})
        assert loaded.status_code == 200, loaded.text
        historical_ref, = loaded.json()["data"]["items"][0]["user_messages"][0]["attachments"]
        assert historical_ref["file_id"] == ref["file_id"] and historical_ref["availability"] == "available"
        changes = await client.get(f"/api/v1/sessions/{session}/changesets/all")
        assert changes.status_code == 200, changes.text
        change, = changes.json()["data"]["files"]
        assert (change["file_path"], change["after_preview"], change["reviewed"]) == (change_uri, frozen_text, True)
        with closing(AttachmentBlobStore(target.workspace)) as store:
            saver = RolloutCheckpointSaver(target.workspace / ".boxteam/sessions", attachment_store=store)
            assert_frozen_requests(saver, session, thread, assemblies=assemblies, frozen=frozen, captures=captures, profile=profile)
        assert index.with_name("rollout.jsonl").read_bytes() == canonical
        evidence = audit_sealed_requests(index, (resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json"), captures)
        deleted = await submit_navigation_operation(
            client, "delete_session", target_node_id=session, wait_for_settlement=True,
        )
        assert (deleted["state"], deleted["settlement"]) == ("committed", "settled"), deleted
        assert not index.exists() and (root / "derived/result.txt").read_text(encoding="utf-8") == changed_text
        await asyncio.to_thread(target.restart)
        loaded_fork = await client.post(f"/api/v1/sessions/{fork_session}/history", json={"direction": "head", "turns": 10})
        assert loaded_fork.status_code == 200, loaded_fork.text
        fork_items = loaded_fork.json()["data"]["items"]
        assert [item["turn_id"] for item in fork_items[:-1]] == [item["turn_id"] for item in loaded.json()["data"]["items"]]
        assert fork_items[-1]["final_response"] == "副本已消费共享目录。"
        fork_ref, = fork_items[0]["user_messages"][0]["attachments"]
        assert fork_ref["file_id"] == ref["file_id"] and fork_ref["availability"] == "available"
        assert fork_index.with_name("rollout.jsonl").read_bytes() == fork_canonical
        with closing(AttachmentBlobStore(target.workspace)) as store:
            saver = RolloutCheckpointSaver(target.workspace / ".boxteam/sessions", attachment_store=store)
            assert_frozen_requests(saver, fork_session, fork_thread, assemblies=fork_assemblies, frozen=fork_frozen,
                                   captures=fork_captures, profile=profile)
        removal, continuation = plan(target, fork_session, fork_thread, [
            tool("apply_patch", {
                "target": ref["directory_uri"],
                "input": "*** Begin Patch\n*** Delete File: derived/result.txt\n*** End Patch",
                "expected_versions": {"derived/result.txt": "sha256:" + hashlib.sha256(changed_text.encode()).hexdigest()},
                "explanation": "显式删除处理结果，恢复附件初始基线",
            }, "remove-derived"),
            {"content": "副本在来源删除后继续，并显式清理处理结果。"},
        ])
        accepted = await client.post(f"/api/v1/sessions/{fork_session}/messages", json={
            "message": {"content": "继续使用副本历史，显式删除已完成的处理结果。"},
            "run": {"mode": "single_agent", "agent_id": "default"},
        })
        assert accepted.status_code == 200, accepted.text
        await wait_for_job_done(client, accepted.json()["data"]["job_id"])
        continued = json.loads((target.replay.artifacts / (continuation + ".json")).read_text(encoding="utf-8"))
        assert (continued["identity"]["session_id"], continued["identity"]["thread_id"]) == (fork_session, fork_thread)
        continued_wire = canonical_json_bytes(continued["request"])
        assert frozen_text.strip().encode() in continued_wire and changed_text.strip().encode() not in continued_wire
        assert fork_index.with_name("rollout.jsonl").read_bytes().startswith(fork_canonical)
        with closing(sqlite3.connect(fork_index.as_uri() + "?mode=ro", uri=True)) as connection:
            assert connection.execute(
                "SELECT session_id,thread_id,model_call_id,assembly_id FROM model_calls "
                "WHERE session_id=? AND thread_id=? ORDER BY model_call_id", (session, thread),
            ).fetchall() == imported_calls
            assert {row[0] for row in connection.execute(
                "SELECT model_call_id FROM model_calls WHERE session_id=? AND thread_id=?", (fork_session, fork_thread),
            )} == {warm_call, removal, continuation}
        removed = json.loads((target.replay.artifacts / (removal + ".json")).read_text(encoding="utf-8"))
        fork_evidence = audit_sealed_requests(
            fork_index, (resolver.resolve_session_node(fork_session) / "logs/llm_requests").glob("*.json"),
            {warm_call: warm_capture, removal: removed, continuation: continued},
        )
        await asyncio.to_thread(target.restart)
        assert not root.exists()
        with closing(AttachmentBlobStore(target.workspace)) as store:
            assert store._catalog.get_directory(aid).availability == "released"
            saver = RolloutCheckpointSaver(target.workspace / ".boxteam/sessions", attachment_store=store)
            assert_frozen_requests(saver, fork_session, fork_thread, assemblies=fork_assemblies, frozen=fork_frozen,
                                   captures=fork_captures, profile=profile)
        rebuilt = await client.get("/api/v1/workspace/files/download", params={"path": ref["file_id"]})
        assert rebuilt.status_code == 200 and rebuilt.content == original, rebuilt.text
        assert (root / "original/content").read_bytes() == original
        assert not (root / "derived/result.txt").exists()
        assert fork_index.with_name("rollout.jsonl").read_bytes().startswith(fork_canonical)
        (target.replay.artifacts / "attachment-working-copy-rebuild.json").write_text(json.dumps({
            "source": evidence, "fork": fork_evidence,
            "historical_owner": [session, thread], "imported_calls": imported_calls,
            "scope": "真实 Gateway、产品上传/工具/fork/删除、修改结果保留、回到基线释放/重建与进程重启；模型为 model_stream，不代 Web、Windows或全部8.3",
        }, ensure_ascii=False, indent=2) + "\n")
