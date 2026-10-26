"""真实 Provider 的 request→sealed assembly→checkpoint→冷历史闭环。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
from collections.abc import AsyncIterator, Callable, Generator
from contextlib import closing
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
from app.domain.itemized.epoch.prefix_epoch import (
    stable_prefix_bytes,
    stable_prefix_digest,
)
from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    contribution_content_hash,
    sha256_jcs,
)
from app.schemas.internal_v2.session_context import SessionContextReadResultDTO
from app.services.business.resource_derivations.agents import WORKSPACE_AGENTS_SOURCE_ID
from app.services.business.session_context_resource import context_assembly_identity
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
)
from app.services.infrastructure.rollout_context.storage.catalog.item_reader import (
    RolloutCanonicalReadMixin,
)
from app.services.infrastructure.tool_selection_store import ToolSelectionStore
from app.services.mapping.itemized.carrier_dedup import superseded_item_ids
from app.services.mapping.itemized.content_blocks import (
    direct_content_blocks,
    reasoning_content_rows,
)
from tests.support.paths import output_root_for_test
from tests.support.ports import e2e_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    BackendProcess,
    close_backend_process,
    start_backend_process,
    terminate_process,
)
from tests.support.sealed_request_audit import logged_provider_wire

_AGENTS_VERSIONS = (
    ("CIL_PROVIDER_AGENTS_A\n审计回执：CIL_PROVENANCE_TURN_1_OK\n"
     "回复规则：只输出上下文中最后出现的 CIL_PROVENANCE_TURN_<数字>_OK 完整标记，不添加其它内容，不调用工具。\n"),
    ("CIL_PROVIDER_AGENTS_B\r\n保留末尾空白  \n审计回执：CIL_PROVENANCE_TURN_2_OK\n"
     "回复规则：只输出上下文中最后出现的 CIL_PROVENANCE_TURN_<数字>_OK 完整标记，不添加其它内容，不调用工具。\n"),
    ("CIL_PROVIDER_AGENTS_C 🧭\n保留末尾空白  \n审计回执：CIL_PROVENANCE_TURN_3_OK\n"
     "回复规则：只输出上下文中最后出现的 CIL_PROVENANCE_TURN_<数字>_OK 完整标记，不添加其它内容，不调用工具。\n"),
)


@pytest.fixture(scope="module")
def provenance_artifacts(request: pytest.FixtureRequest) -> Path:
    root = output_root_for_test(Path(request.node.fspath), test_layer="e2e", project_root=Path.cwd())
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    return artifacts


@pytest.fixture(scope="module")
def e2e_backend_composition(request: pytest.FixtureRequest) -> tuple[str, dict[str, str]]:
    """真实工作区的 Skill 回查必须指向同一隔离 Gateway。"""
    port = e2e_port_block_for_file(Path(request.node.fspath)).port(1)
    return "app.main:app", {"BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{port}"}


@pytest.fixture
def provenance_backend_start(
    e2e_workspace_root_path: str, e2e_backend_port: int,
    e2e_backend_composition: tuple[str, dict[str, str]],
) -> Generator[Callable[[str], subprocess.Popen[str]], None, None]:
    """所有重启进程归同一 fixture 持有，中途断言失败也必须回收。"""
    processes: list[BackendProcess] = []

    def start(log_name: str) -> subprocess.Popen[str]:
        asgi_app, environment = e2e_backend_composition
        process = start_backend_process(
            workspace_root=e2e_workspace_root_path, port=e2e_backend_port,
            log_name=log_name, asgi_app=asgi_app, env_overrides=environment,
            refresh_user_configuration=False,
        )
        processes.append(process)
        return process.process

    try:
        yield start
    finally:
        for process in reversed(processes):
            close_backend_process(process)


@pytest.fixture(scope="module")
def provenance_gateway(
    request: pytest.FixtureRequest,
    e2e_backend_process: subprocess.Popen[str],
    e2e_workspace_root_path: str,
    client_base_url: str,
    provenance_artifacts: Path,
) -> Generator[GatewayProcess, None, None]:
    gateway = start_gateway_process(
        workspace_root=Path(e2e_workspace_root_path), default_backend_url=client_base_url,
        port=e2e_port_block_for_file(Path(request.node.fspath)).port(1),
        artifacts_dir=provenance_artifacts,
    )
    try:
        yield gateway
    finally:
        close_gateway_process(gateway)


@pytest.fixture
async def client(provenance_gateway: GatewayProcess) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{provenance_gateway.port}", headers=LOCAL_TOKEN_HEADERS,
        timeout=30,
    ) as connection:
        await acquire_gateway_guest(connection)
        yield connection


def _save(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _response(response: httpx.Response, path: Path) -> dict:
    path.write_text(response.text, encoding="utf-8")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert response.headers["X-Request-ID"] == payload["request_id"]
    return payload


async def _terminal_job(client: httpx.AsyncClient, job_id: str, path: Path) -> dict:
    """保留真实终态后再审计，失败调用也不能丢失 provenance。"""
    for poll in range(180):
        response = await client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()["data"]
        if job["status"] in {"completed", "succeeded", "failed", "cancelled", "timed_out"}:
            _save(path, job)
            return job
        if poll == 179:
            _save(path, job)
            break
        await asyncio.sleep(1)
    pytest.fail(f"Job {job_id} 超时未完成；最后状态已保存")


async def _audit_log_api(
    client: httpx.AsyncClient, session_node: Path, session_id: str, artifacts: Path, *, phase: str,
) -> list[dict]:
    """经真实 Gateway 消费完整日志，逐字段核对磁盘里的同一次调用。"""
    request_id = str(uuid4())
    payload = _response(
        await client.get(f"/api/v1/sessions/{session_id}/llm-request-logs", headers={"X-Request-ID": request_id}),
        artifacts / f"{phase}-llm-request-logs.json",
    )
    assert payload["request_id"] == request_id, "Gateway 与工作区后端必须透传同一个 request_id"
    records = payload["data"]
    paths = sorted((session_node / "logs" / "llm_requests").glob("*.json"), key=lambda path: int(path.stem))
    assert len(records) == len(paths)
    for record, path in zip(records, paths, strict=True):
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert record["file_name"] == path.name
        assert "file_path" not in record, "普通日志响应不得暴露本机存储路径"
        assert record["session_id"] == session_id
        for field in ("session_id", "job_id", "timestamp", "request", "response", "upstream"):
            assert record[field] == raw[field], f"日志 API 丢失或改变 {field}: {path.name}"
    return records


async def _trace_events(
    client: httpx.AsyncClient, session_id: str, artifacts: Path, ordinal: int,
) -> list[dict]:
    """诊断页从尾部向前读取，合并后仍按原始发生顺序审计。"""
    events: list[dict] = []
    cursors: set[str] = set()
    params: dict[str, str | int] = {"limit": 200}
    while True:
        page = _response(
            await client.get(f"/api/v1/sessions/{session_id}/traces", params=params),
            artifacts / f"turn-{ordinal}-trace-page-{len(cursors) + 1}.json",
        )["data"]
        events[0:0] = page["items"]
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
        assert isinstance(cursor, str) and cursor and cursor not in cursors
        cursors.add(cursor)
        params["cursor"] = cursor
    assert len({event["event_id"] for event in events}) == len(events), "trace 分页重复事件"
    return events


def _audit_calls(
    session_node: Path, thread_node: Path, *, session_id: str, thread_id: str, job_id: str,
    desired_revision: str, artifacts: Path, ordinal: int, succeeded: bool,
) -> list[tuple[ContextAssemblySnapshot, dict]]:
    logs = [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((session_node / "logs" / "llm_requests").glob("*.json"))]
    logs = [log for log in logs if log["job_id"] == job_id]
    _save(artifacts / f"turn-{ordinal}-provider-logs.json", logs)
    assert len(logs) == 1, "无工具的 Turn 应只有一次模型节点调用，路由可尝试多个候选"
    log = logs[0]
    attempts = log["upstream"]["attempts"]
    assert attempts, "真实模型节点必须记录全部已发送尝试"
    if succeeded:
        assert log["response"]["error"] is None
        assert attempts[-1]["error"] is None and attempts[-1]["response"] is not None
        assert {message["response_metadata"]["model_call_id"] for message in log["response"]["result"]} == {
            attempts[-1]["sealed_dispatch"]["callback_model_call_id"]
        }
    else:
        assert log["response"]["error"] and not log["response"]["result"]
    audited = [
        _audit_attempt(
            log, attempt, thread_node, session_id=session_id, thread_id=thread_id,
            desired_revision=desired_revision, artifacts=artifacts,
            ordinal=f"{ordinal}-attempt-{index}",
        )
        for index, attempt in enumerate(attempts, 1)
    ]
    if succeeded:
        # fallback 候选共用一次图节点；checkpoint 必须指向最终 applied assembly。
        assert audited[-1][1]["checkpoint"]["source_assembly_id"] == audited[-1][0].assembly_id
    return audited


def _audit_attempt(
    log: dict, attempt: dict, thread_node: Path, *, session_id: str, thread_id: str,
    desired_revision: str, artifacts: Path, ordinal: str,
) -> tuple[ContextAssemblySnapshot, dict]:
    succeeded = attempt["error"] is None
    if succeeded:
        assert attempt["response"] is not None
        if "input" in attempt["request"]:
            assert attempt["response"]["status"] == "completed", "incomplete terminal 不满足真实成功验收门"
        else:
            choices = attempt["response"]["choices"]
            # Chat 流日志保存 message 摘要；完成事实另核 model_call/Job 与精确正文。
            assert len(choices) == 1 and choices[0]["message"]["role"] == "assistant"
    else:
        assert isinstance(attempt["error"], str) and attempt["error"]
    reference = attempt["sealed_dispatch"]
    assert reference["session_id"] == session_id
    assert reference["thread_id"] == thread_id
    assert log["session_id"] == session_id
    job_id = log["job_id"]

    index = thread_node / "rollout" / "index.sqlite"
    with closing(sqlite3.connect(f"file:{index}?mode=ro", uri=True)) as connection:
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        row = cursor.execute(
            "SELECT * FROM context_assemblies WHERE assembly_id=? AND session_id=? AND thread_id=?",
            (reference["assembly_id"], session_id, thread_id),
        ).fetchone()
        assert row is not None
        snapshot = ContextAssemblySnapshot.from_dict(load_snapshot(connection, row["snapshot_json"]))
        _save(artifacts / f"turn-{ordinal}-sealed-assembly.json", snapshot.to_dict())
        snapshot.validate_hashes()
        expected_reference = SealedContextDispatchRef.from_snapshot(
            snapshot, checkpoint_ns=reference["checkpoint_ns"], attempt_key=reference["attempt_key"],
            callback_model_call_id=reference["callback_model_call_id"],
        )
        assert reference == json.loads(json.dumps(asdict(expected_reference)))
        actual_wire = logged_provider_wire(attempt, target_format=snapshot.target_format)
        _save(artifacts / f"turn-{ordinal}-actual-wire.json", actual_wire)
        assert actual_wire == snapshot.request_hash_preimage
        assert row["plan_hash"] == reference["plan_hash"]
        assert row["request_hash"] == reference["request_hash"]
        model_call = cursor.execute(
            "SELECT * FROM model_calls WHERE model_call_id=?",
            (reference["sealed_model_call_id"],),
        ).fetchone()
        assert model_call is not None
        assert (model_call["session_id"], model_call["thread_id"], model_call["execution_id"], model_call["assembly_id"]) == (
            session_id, thread_id, reference["execution_id"], reference["assembly_id"],
        )
        expected_outcome = "completed" if succeeded else "failed"
        assert (model_call["dispatch_state"], model_call["outcome"]) == (expected_outcome, expected_outcome)
        binding = snapshot.seal_binding
        assert binding is not None
        assert binding.selection_revision == desired_revision
        assert len(snapshot.tool_set_refs) == 1
        tool_ref = snapshot.tool_set_refs[0]
        assert binding.applied_toolset_revision == tool_ref.source_revision
        tools = cursor.execute(
            "SELECT source_revision, content_hash, tools_json, tool_policy_json FROM tool_set_snapshots "
            "WHERE assembly_id=? AND tool_set_snapshot_id=?",
            (snapshot.assembly_id, tool_ref.ref_id),
        ).fetchone()
        assert tools is not None
        assert tools["source_revision"] == binding.applied_toolset_revision
        assert tools["content_hash"] == tool_ref.content_hash
        assert json.loads(tools["tools_json"]) == list(tool_ref.tools)
        assert json.loads(tools["tool_policy_json"]) == tool_ref.tool_policy
        selection = [tuple(selected) for selected in cursor.execute(
            "SELECT plan_ordinal, ref_type, ref_id, ref_thread_id, included, content_hash, source_revision "
            "FROM context_assembly_selections WHERE assembly_id=? ORDER BY plan_ordinal",
            (snapshot.assembly_id,),
        )]
        assert selection == [
            (entry.plan_ordinal, entry.ref.ref_type, entry.ref.ref_id, entry.ref.thread_id,
             int(entry.included), entry.content_hash, entry.source_revision)
            for entry in snapshot.selection
        ]
        history_message_ids = []
        for entry in snapshot.selection:
            if entry.included and entry.ref.ref_type == "canonical_item":
                identity = cursor.execute(
                    "SELECT json_extract(projection_manifest_json, '$.projection_identity.projection_message_id') "
                    "FROM item_catalog WHERE item_id=?", (entry.ref.ref_id,),
                ).fetchone()
                assert identity is not None and isinstance(identity[0], str)
                if identity[0] not in history_message_ids:
                    history_message_ids.append(identity[0])
        checkpoint = cursor.execute(
            "SELECT p.checkpoint_id, p.view_id, p.checkpoint_json, p.metadata_json, s.source_assembly_id "
            "FROM checkpoints p JOIN checkpoint_context_source_snapshots s USING(checkpoint_id) "
            "WHERE p.checkpoint_ns=? AND s.session_id=? AND s.thread_id=? "
            "ORDER BY p.commit_id DESC, p.rowid DESC LIMIT 1",
            (reference["checkpoint_ns"], session_id, thread_id),
        ).fetchone()
        assert checkpoint is not None
        channel = cursor.execute(
            "SELECT storage_kind, context_view_id FROM checkpoint_channels "
            "WHERE checkpoint_id=? AND channel_name='messages'", (checkpoint["checkpoint_id"],),
        ).fetchone()
        assert channel is not None and channel["storage_kind"] == "rollout_view"
        output_rows = cursor.execute(
            "SELECT item_sequence, jsonl_offset, jsonl_length FROM item_catalog WHERE execution_id=? "
            "AND semantic_kind IN ('assistant_output', 'reasoning') "
            "AND json_extract(projection_manifest_json, '$.projection_identity.model_call_id')=? "
            "ORDER BY item_sequence",
            (reference["execution_id"], reference["callback_model_call_id"]),
        ).fetchall()
        with index.with_name("rollout.jsonl").open("rb") as stream:
            outputs = [RolloutCanonicalReadMixin._read_v2_item_at(
                connection, stream, sequence=row[0], offset=row[1], length=row[2],
            ) for row in output_rows]
        # 原始 stream/shadow 仍留在 canonical；checkpoint/history 按已提交的替代身份投影。
        superseded = superseded_item_ids(outputs)
        output_ids = [item.item_id for item in outputs if item.item_id not in superseded]
        if succeeded:
            assert output_ids, "同一次 callback 的真实输出必须进入 canonical checkpoint"
        checkpoint_items = {item[0] for item in cursor.execute(
            "SELECT item_id FROM context_view_items WHERE view_id=? AND visible=1",
            (channel["context_view_id"],),
        )}
        assert set(output_ids) <= checkpoint_items
        assert {entry.ref.ref_id for entry in snapshot.selection
                if entry.included and entry.ref.ref_type == "canonical_item"} <= checkpoint_items

    prefix = binding.prefix
    prefix_bytes = stable_prefix_bytes(prefix.state.frames)
    digest = stable_prefix_digest(prefix_bytes)
    assert (digest.prefix_hash, digest.prefix_byte_length) == (reference["prefix_hash"], reference["prefix_byte_length"])
    assert reference["frame_hashes"] == [frame.frame_hash() for frame in prefix.state.frames]
    receipt = {
        "job_id": job_id, "dispatch_ref": reference, "checkpoint": dict(checkpoint),
        "messages_channel": dict(channel), "output_item_ids": output_ids, "selection": selection,
        "raw_output_item_ids": [item.item_id for item in outputs],
        "superseded_output_item_ids": sorted(superseded),
        "selected_history_message_ids": history_message_ids,
        "desired_selection_revision": desired_revision,
        "applied_toolset_revision": binding.applied_toolset_revision,
        "provider_succeeded": succeeded, "upstream_error": attempt["error"],
        "upstream_model": attempt["model"], "checkpoint_scope": "post_model_node",
        "handler_error": log["response"]["error"], "model_call": dict(model_call),
    }
    _save(artifacts / f"turn-{ordinal}-provenance-receipt.json", receipt)
    return snapshot, receipt


async def _assembly_history(
    client: httpx.AsyncClient, session: dict, snapshot: ContextAssemblySnapshot, artifacts: Path,
    *, phase: str, selected_message_ids: list[str],
) -> list[dict]:
    request = {key: session[key] for key in ("resource_identity", "uri", "scope")}
    request.update({
        "view": "assembly", "assembly_ref": context_assembly_identity(session["resource_identity"], snapshot.assembly_id),
        "include": ["visible_text", "reasoning", "tool_calls", "tool_results", "system", "raw_record"],
        "limit": 200, "max_chars": 65536,
    })
    items = await _context_items(client, request, artifacts, f"{phase}-{snapshot.assembly_id}-history")
    selected = [item["data"] for item in items if item["kind"] == "selection"]
    assert selected == [entry.to_dict() for entry in snapshot.selection]
    messages = [item for item in items if item["kind"] == "message"]
    # assembly history 只投影 canonical selection；request-only prefix frame 不物化为历史消息。
    assert [item["data"]["message_id"] for item in messages] == selected_message_ids
    request.update({"view": "records"})
    request.pop("assembly_ref")
    records = await _context_items(client, request, artifacts, f"{phase}-{snapshot.assembly_id}-canonical-records")
    canonical = {item["raw_record"]["response_metadata"]["message_id"]: item for item in records}
    assert len(canonical) == len(records), "records 分页重复消息"
    for message in messages:
        expected = canonical[message["data"]["message_id"]]
        for field in ("role", "text", "reasoning", "tool_calls", "tool_results"):
            assert message[field] == expected[field]
        actual_raw, expected_raw = message["raw_record"], expected["raw_record"]
        assert actual_raw["type"] == expected_raw["type"]
        assert actual_raw["id"] == expected_raw["response_metadata"]["message_id"]
        assert actual_raw["response_metadata"].get("model_call_id") == expected_raw["response_metadata"].get("model_call_id")
        # records 使用展示块，assembly 恢复冻结的 Provider carrier；逐块核原文、坐标和保护状态。
        actual_blocks = direct_content_blocks(actual_raw["content"])
        expected_blocks = direct_content_blocks(expected_raw["content"])
        assert len(actual_blocks) == len(expected_blocks)
        assert [block["text"] for block in actual_blocks if block["type"] == "text"] == [
            block["text"] for block in expected_blocks if block["type"] == "text"
        ]
        assert [
            (row.content_block_index, row.item_index, row.reasoning_text or "", row.summary_text or "",
             row.encrypted, row.signature_present)
            for row in reasoning_content_rows(actual_raw["content"])
        ] == [
            (row.content_block_index, row.item_index, row.reasoning_text or "", row.summary_text or "",
             row.encrypted, row.signature_present)
            for row in reasoning_content_rows(expected_raw["content"])
        ]
        for part in expected_raw["response_metadata"].get("content_part_refs", []):
            assert actual_blocks[part["index"]]["type"] == part["type"]
            assert expected_blocks[part["index"]]["id"] == part["id"]
    return messages


async def _context_items(
    client: httpx.AsyncClient, request: dict, artifacts: Path, name: str,
) -> list[dict]:
    """消费公开游标和 JSON item 分片，不能把有预算的单页当完整历史。"""
    items: list[dict] = []
    cursors: set[str] = set()
    chunk = ""
    revision = None
    request = dict(request)
    while True:
        page = _response(
            await client.post("/api/v1/context/read", json=request),
            artifacts / f"{name}-page-{len(cursors) + 1}.json",
        )["data"]
        assert not page["partial_errors"] and not page["omitted_partial_error_count"]
        assert page["returned_chars"] == len(SessionContextReadResultDTO.model_validate(page).model_dump_json())
        assert page["returned_chars"] <= request["max_chars"]
        assert len(page["items"]) <= request["limit"]
        for field in ("resource_identity", "uri", "scope", "view"):
            assert page[field] == request[field]
        assert page["assembly_ref"] == request.get("assembly_ref")
        if revision is None:
            revision = page["revision"]
        assert page["revision"] == revision, "同一分页不能混入不同历史版本"
        for item in page["items"]:
            if item["kind"].endswith("_chunk"):
                assert item["data"]["chunk_start"] == len(chunk)
                chunk += item["text"]
                if not item["truncated"]:
                    items.append(json.loads(chunk))
                    chunk = ""
            else:
                assert not chunk and not item["truncated"]
                items.append(item)
        if not page["has_more"]:
            assert page["next_cursor"] is None and not chunk and not page["truncated"]
            return items
        cursor = page["next_cursor"]
        assert isinstance(cursor, str) and cursor and cursor not in cursors
        cursors.add(cursor)
        request["cursor"] = cursor


def _checkpoint_state(thread_node: Path) -> dict:
    with closing(sqlite3.connect(f"file:{thread_node / 'rollout' / 'index.sqlite'}?mode=ro", uri=True)) as connection:
        # 比较实际正文和全部引用字段；只比较已存hash会漏掉正文被改写但hash未更新。
        tables = {
            "checkpoints": ("checkpoints", "checkpoint_id"),
            "channels": ("checkpoint_channels", "checkpoint_id, channel_name"),
            "state_values": ("checkpoint_state_values", "value_id"),
            "pending_writes": ("pending_writes", "checkpoint_id, task_id, task_path, write_index"),
            "assemblies": ("context_assemblies", "assembly_id"),
            "assembly_content": ("assembly_content_nodes", "session_id, thread_id, node_id"),
            "views": ("context_views", "view_id"),
            "view_ranges": ("context_view_ranges", "view_id, range_index"),
            "view_jumps": ("context_view_jumps", "view_id, jump_level"),
            "membership_blocks": ("context_membership_blocks", "block_id"),
            "membership_items": ("context_membership_items", "block_id, member_index"),
            "view_blocks": ("context_view_item_blocks", "view_id, block_ordinal"),
            "source_snapshots": ("checkpoint_context_source_snapshots", "checkpoint_id"),
            "source_states": ("checkpoint_context_source_states", "checkpoint_id, source_id"),
        }
        state = {
            name: [tuple({"blob_hex": value.hex()} if isinstance(value, bytes) else value for value in row)
                   for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}")]
            for name, (table, order) in tables.items()
        }
        cursor = connection.cursor()
        cursor.row_factory = sqlite3.Row
        values = {row["value_id"]: row for row in cursor.execute("SELECT * FROM checkpoint_state_values")}
        for value in values.values():
            assert len(value["value_blob"]) == value["value_length"], "状态版本正文长度不一致"
            assert hashlib.sha256(value["value_blob"]).hexdigest() == value["value_hash"], "状态版本正文hash不一致"
        references: dict[int, int] = {}
        for channel in cursor.execute(
            "SELECT channel.*, checkpoint.checkpoint_ns FROM checkpoint_channels channel "
            "JOIN checkpoints checkpoint USING(checkpoint_id) "
            "WHERE channel.storage_kind='sqlite_value' AND channel.value_state='present'"
        ):
            value = values.get(channel["value_id"])
            assert value is not None, "checkpoint引用的状态版本正文缺失"
            assert (channel["value_session_id"], channel["value_thread_id"], channel["checkpoint_ns"],
                    channel["channel_name"], channel["channel_version"]) == (
                value["session_id"], value["thread_id"], value["checkpoint_ns"],
                value["channel_name"], value["channel_version"],
            ), "checkpoint引用了其它owner/namespace/channel/version的正文"
            references[value["value_id"]] = references.get(value["value_id"], 0) + 1
        reused = [{
            "value_id": value_id, "session_id": values[value_id]["session_id"],
            "thread_id": values[value_id]["thread_id"], "checkpoint_ns": values[value_id]["checkpoint_ns"],
            "channel": values[value_id]["channel_name"], "version": values[value_id]["channel_version"],
            "references": count, "body_bytes": values[value_id]["value_length"],
        } for value_id, count in sorted(references.items()) if count > 1]
        assert any(row["body_bytes"] > 0 for row in reused), "真实多步执行未证明非空状态版本复用"
        message_states = dict(connection.execute(
            "SELECT value_state,count(*) FROM checkpoint_channels WHERE channel_name='messages' GROUP BY value_state"
        ))
        assert message_states.get("snapshot", 0) > 0 and message_states.get("delta", 0) > 0
        state["version_reuse"] = {"shared_values": reused, "messages": message_states}
        return state


def _audit_agents_versions(
    audited: list[tuple[ContextAssemblySnapshot, dict]], *, ordinal: int, artifacts: Path,
) -> None:
    """真实请求只追加精确 A→B→C delta，路由重投影仍保留原 A root。"""
    revisions = [sha256_jcs({"content": content}) for content in _AGENTS_VERSIONS]
    deltas = (
        (f"--- AGENTS.md@{revisions[0]}\n+++ AGENTS.md@{revisions[1]}\n"
         "@@ -1,3 +1,4 @@\n-CIL_PROVIDER_AGENTS_A\n-审计回执：CIL_PROVENANCE_TURN_1_OK\n"
         "+CIL_PROVIDER_AGENTS_B\r\n+保留末尾空白  \n+审计回执：CIL_PROVENANCE_TURN_2_OK\n"
         " 回复规则：只输出上下文中最后出现的 CIL_PROVENANCE_TURN_<数字>_OK 完整标记，不添加其它内容，不调用工具。\n"),
        (f"--- AGENTS.md@{revisions[1]}\n+++ AGENTS.md@{revisions[2]}\n"
         "@@ -1,4 +1,4 @@\n-CIL_PROVIDER_AGENTS_B\r\n+CIL_PROVIDER_AGENTS_C 🧭\n 保留末尾空白  \n"
         "-审计回执：CIL_PROVENANCE_TURN_2_OK\n+审计回执：CIL_PROVENANCE_TURN_3_OK\n"
         " 回复规则：只输出上下文中最后出现的 CIL_PROVENANCE_TURN_<数字>_OK 完整标记，不添加其它内容，不调用工具。\n"),
    )
    applied = []
    frames_by_assembly = {}
    for snapshot, _ in audited:
        binding = snapshot.seal_binding
        assert binding is not None and binding.root is not None
        root_source, = [source for source in binding.root.sources
                        if source.source_identity == WORKSPACE_AGENTS_SOURCE_ID]
        assert root_source.revision == revisions[0]
        assert root_source.content_hash == contribution_content_hash("root_source", _AGENTS_VERSIONS[0])
        assert _AGENTS_VERSIONS[0] in binding.root.content
        members = [(frame, ref, block) for frame in binding.prefix.state.frames
                   if frame.role == "user" and frame.source_protocol is not None
                   for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True)
                   if ref.source_identity == WORKSPACE_AGENTS_SOURCE_ID]
        assert [(ref.source_revision, frame.role, block.body) for frame, ref, block in members] == [
            (revisions[index + 1], "user", deltas[index]) for index in range(ordinal - 1)
        ]
        frames_by_assembly[snapshot.assembly_id] = [frame.to_dict() for frame, _ref, _block in members]
        for revision in binding.source_revisions:
            if revision.source_id == WORKSPACE_AGENTS_SOURCE_ID:
                assert revision.revision == revisions[ordinal - 1]
                assert revision.previous_revision == (revisions[ordinal - 2] if ordinal > 1 else None)
                applied.append(asdict(revision))
    assert applied, "该轮真实 seal 未应用对应 AGENTS revision"
    _save(artifacts / f"turn-{ordinal}-agents-source-audit.json", {
        "source_id": WORKSPACE_AGENTS_SOURCE_ID, "revision": revisions[ordinal - 1],
        "applied": applied, "frames_by_assembly": frames_by_assembly,
    })


@pytest.mark.asyncio
async def test_real_provider_provenance_survives_cold_history(
    client: httpx.AsyncClient, e2e_workspace_root_path: str, e2e_backend_process: subprocess.Popen[str],
    provenance_backend_start: Callable[[str], subprocess.Popen[str]], provenance_artifacts: Path,
) -> None:
    artifacts = provenance_artifacts
    workspace = Path(e2e_workspace_root_path)
    agents_path = workspace / "AGENTS.md"
    agents_path.write_bytes(_AGENTS_VERSIONS[0].encode())
    active_backend = e2e_backend_process
    for endpoint in ("/api/gateway/health", "/api/gateway/workspaces", "/api/v1/workspace"):
        _response(await client.get(endpoint), artifacts / f"{endpoint.rsplit('/', 1)[1]}-initialization.json")
    session = _response(await client.post("/api/v1/sessions", json={"title": "真实 Provider provenance"}), artifacts / "created-session.json")["data"]
    session_id, thread_id = session["session_id"], session["thread_id"]
    # 只选择隔离 Session 已配置的真实 Responses Provider，不改全局配置或增加请求覆盖。
    session = _response(await client.patch(f"/api/v1/sessions/{session_id}", json={"provider_id": "backup_3"}), artifacts / "selected-provider.json")["data"]
    resolver = get_session_path_resolver(workspace / ".boxteam" / "sessions")
    session_node = resolver.resolve_session_node(session_id)
    thread_node = resolver.resolve_thread_node(session_id, thread_id)
    desired = ToolSelectionStore(boxteam_root=workspace / ".boxteam").snapshot("default")
    snapshots = []
    receipts = []
    response_mismatches = []
    failed_job = None
    for ordinal, source_body in enumerate(_AGENTS_VERSIONS, 1):
        if ordinal > 1:
            # 停机期间只改真实来源文件；历史、checkpoint 与 CSM 状态全部由产品恢复。
            terminate_process(active_backend)
            agents_path.write_bytes(source_body.encode())
            active_backend = provenance_backend_start(f"provenance-source-{ordinal}-backend")
        marker = f"CIL_PROVENANCE_TURN_{ordinal}_OK"
        prompt = f"不要调用任何工具。只回复这一行且不添加其它内容：{marker}"
        sent = _response(await client.post(f"/api/v1/sessions/{session_id}/messages", json={
            "message": {"content": prompt}, "run": {"mode": "single_agent", "agent_id": "default"},
        }), artifacts / f"turn-{ordinal}-entry.json")["data"]
        job = await _terminal_job(client, sent["job_id"], artifacts / f"turn-{ordinal}-job.json")
        succeeded = job["status"] in {"completed", "succeeded"}
        traces = await _trace_events(client, session_id, artifacts, ordinal)
        audited = _audit_calls(session_node, thread_node, session_id=session_id, thread_id=thread_id,
            job_id=sent["job_id"], desired_revision=desired.revision, artifacts=artifacts,
            ordinal=ordinal, succeeded=succeeded)
        _audit_agents_versions(audited, ordinal=ordinal, artifacts=artifacts)
        model_events = [event for event in traces
                        if event["job_id"] == sent["job_id"] and event["type"] == "llm_request"]
        assert [event["raw"]["payload"]["model"] for event in model_events] == [
            receipt["upstream_model"] for _, receipt in audited
        ], "每个真实候选请求必须有且只有一条同序模型 trace"
        for snapshot, receipt in audited:
            snapshots.append(snapshot)
            receipts.append(receipt)
            await _assembly_history(client, session, snapshot, artifacts, phase="live",
                                    selected_message_ids=receipt["selected_history_message_ids"])
        snapshot = audited[-1][0]
        output_item_ids = {item_id for _, receipt in audited for item_id in receipt["output_item_ids"]}
        await _audit_log_api(client, session_node, session_id, artifacts, phase=f"turn-{ordinal}-live")
        history = _response(await client.post(f"/api/v1/sessions/{session_id}/history", json={
            "turn_ids": [snapshot.turn_id], "include": ["user", "text", "final_response", "metadata"],
        }), artifacts / f"turn-{ordinal}-history.json")["data"]
        assert len(history["items"]) == 1
        turn = history["items"][0]
        assert (turn["turn_id"], turn["job_id"]) == (snapshot.turn_id, sent["job_id"])
        assert turn["user_messages"][0]["content"] == prompt
        projected_ids = {part["source"]["item_id"] for part in turn["response_parts"]}
        assert output_item_ids == projected_ids, "历史必须精确投影有效输出，不能重复 stream/shadow"
        if succeeded:
            # 先收齐来源链和冷恢复证据，最终仍以精确 marker 判定整场通过。
            if turn["final_response"].strip() != marker:
                response_mismatches.append({"turn": ordinal, "expected": marker, "actual": turn["final_response"]})
        else:
            if not output_item_ids:
                assert not projected_ids and not turn["final_response"], "失败调用不得伪造 assistant 输出"
            failed_job = job
            break

    first = snapshots[0].seal_binding
    assert first is not None
    assert first.prefix.state.epoch.reason.value == "initial"
    for previous, current in pairwise(snapshots):
        old, new = previous.seal_binding, current.seal_binding
        assert old is not None and new is not None
        assert new.previous_assembly_id == previous.assembly_id
        if old.prefix.state.provider_profile == new.prefix.state.provider_profile:
            assert old.prefix.state.epoch == new.prefix.state.epoch
            assert stable_prefix_bytes(new.prefix.state.frames).startswith(stable_prefix_bytes(old.prefix.state.frames))
        else:
            assert new.prefix.state.epoch.reason.value == "provider_changed"
            assert new.prefix.state.epoch.ordinal == old.prefix.state.epoch.ordinal + 1
            assert old.root is not None and new.root is not None
            assert (new.root.content, new.root.sources) == (old.root.content, old.root.sources)
    assert ToolSelectionStore(boxteam_root=workspace / ".boxteam").snapshot("default").revision == desired.revision
    before = _checkpoint_state(thread_node)
    _save(artifacts / "before-cold-checkpoint-state.json", before)
    live_history = _response(await client.post(f"/api/v1/sessions/{session_id}/history", json={
        "direction": "head", "turns": len(_AGENTS_VERSIONS), "include": ["user", "text", "final_response", "metadata"],
    }), artifacts / "live-history.json")["data"]
    live_logs = await _audit_log_api(client, session_node, session_id, artifacts, phase="before-cold")
    terminate_process(active_backend)
    agents_path.write_bytes(b"CIL_PROVIDER_AGENTS_CURRENT_FILE_IS_NOT_HISTORY\n")
    provenance_backend_start("provenance-cold-backend")
    cold_history = _response(await client.post(f"/api/v1/sessions/{session_id}/history", json={
        "direction": "head", "turns": len(_AGENTS_VERSIONS), "include": ["user", "text", "final_response", "metadata"],
    }), artifacts / "cold-history.json")["data"]
    assert cold_history["items"] == live_history["items"]
    assert await _audit_log_api(client, session_node, session_id, artifacts, phase="cold") == live_logs
    with closing(sqlite3.connect(f"file:{thread_node / 'rollout' / 'index.sqlite'}?mode=ro", uri=True)) as connection:
        for snapshot in snapshots:
            manifest, = connection.execute(
                "SELECT snapshot_json FROM context_assemblies WHERE assembly_id=?", (snapshot.assembly_id,),
            ).fetchone()
            assert canonical_json_bytes(load_snapshot(connection, manifest)) == canonical_json_bytes(snapshot.to_dict()), (
                "来源变化或重启改写了旧 assembly"
            )
    for snapshot, receipt in zip(snapshots, receipts, strict=True):
        await _assembly_history(client, session, snapshot, artifacts, phase="cold",
                                selected_message_ids=receipt["selected_history_message_ids"])
    after = _checkpoint_state(thread_node)
    _save(artifacts / "after-cold-checkpoint-state.json", after)
    assert after == before, "冷 history/assembly projection 不得创建或改写 checkpoint/seal"
    _save(artifacts / "provenance-audit.json", {
        "status": "failed_call" if failed_job is not None else "failed_response" if response_mismatches else "passed",
        "session_id": session_id, "thread_id": thread_id, "calls": receipts,
        "failed_job": failed_job,
        "response_mismatches": response_mismatches,
    })
    assert failed_job is None, f"真实调用失败；完整失败审计与冷恢复收据已保存: {failed_job}"
    assert not response_mismatches, f"真实 Provider 未遵守精确回复要求: {response_mismatches}"
