"""来源分组贯穿真实消息、工具、封存、冷读和产品 fork。"""

import json
from typing import Any, cast

import pytest

from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 正式隔离工作区
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414 - 真实 prepare/seal
)
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RuntimeSourceCase,
    _job,
    _send,
    cold_fork_deadlines,
    source_case,
)
from tests.support.api_helpers import create_session

__all__ = ["cold_fork_deadlines", "source_case"]


@pytest.mark.parametrize("registry_db", ["protected"], indirect=True)
@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_grouping_switch_keeps_parent_retry_and_cold_wire(production_context, target_format):
    saver, session, thread, _connection, manager, prepare = production_context
    first = prepare("before-sources", target_format=target_format)
    for identity in ("tail:a", "tail:b"):
        manager.register(ContextSourceDescriptor(
            source_id=identity, source_kind="external", name=identity, description="",
            internal_locator="不读取当前文件", root_placement="tail_only",
        ), tracking_status="tracked")
        manager.observe(identity, identity + " 正文 Ω", revision="v1").require_tracked()
    grouped = prepare("grouped", target_format=target_format)
    frozen = canonical_json_bytes(grouped["snapshot"].to_dict())
    frames = grouped["snapshot"].seal_binding.prefix.state.frames
    assert frames[:-1] == first["snapshot"].seal_binding.prefix.state.frames
    assert [ref.source_identity for ref in frames[-1].item_refs] == ["tail:a", "tail:b"]
    assert [block.source_identity for block in parse_source_blocks(frames[-1].content)] == ["tail:a", "tail:b"]
    for identity in ("tail:a", "tail:b"):
        manager.observe(identity, identity + " 第二版 é", revision="v2").require_tracked()
    separate = prepare("separate", target_format=target_format, source_message_grouping="separate")
    following = separate["snapshot"].seal_binding.prefix.state.frames
    assert following[:-2] == frames
    assert [len(frame.item_refs) for frame in following[-2:]] == [1, 1]
    assert [frame.item_refs[0].source_identity for frame in following[-2:]] == ["tail:a", "tail:b"]
    assert separate["snapshot"].seal_binding.prefix.state.epoch == grouped["snapshot"].seal_binding.prefix.state.epoch
    retry = prepare("grouped", target_format=target_format, source_message_grouping="separate")
    assert canonical_json_bytes(retry["snapshot"].to_dict()) == frozen
    assert retry["messages"] == grouped["messages"]
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        for original in (grouped, separate):
            snapshot = cold.get_context_assembly(session, thread, assembly_id=original["assembly_id"])
            projected = cold.project_context_plan_for_dispatch(
                session, thread, snapshot.as_sealed_plan(), target_format=target_format,
            )
            assert projected.messages == original["messages"]
            assert snapshot.request_hash == original["snapshot"].request_hash
            assert canonical_json_bytes(snapshot.to_dict()) == canonical_json_bytes(original["snapshot"].to_dict())
            if target_format == "responses":
                native = projected.native
                assert native is not None
                source_inputs = [value for value in cast(dict[str, Any], native["request"])["input"]
                                 if "tail:a" in json.dumps(value, ensure_ascii=False)]
                assert len(source_inputs) == (1 if original is grouped else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_case", [{"terminal": True, "browser": True, "managed_gateway": True}], indirect=True)
async def test_product_tools_freeze_mixed_tracking_and_fork(
    source_case: RuntimeSourceCase, cold_fork_deadlines,
):
    case = source_case
    grouping = "aggregate"
    path = case.workspace / ".boxteam/workspace.jsonc"
    config = json.loads(path.read_text())
    config["runtime"]["agent"]["run"]["mode"] = "single_agent"
    config.setdefault("context", {})["source_messages"] = {"grouping": grouping}
    path.write_text(json.dumps(config, ensure_ascii=False))
    assert await case.container.config_service.reload(idempotency_key="grouping-initial")
    session = await create_session(case.client, "混合来源分组")
    saver = case.container.checkpointer
    main = await case.client.get(f"/api/v1/sessions/{session}/main-thread")
    assert main.status_code == 200, main.text
    thread = main.json()["data"]["thread_id"]
    assert isinstance(thread, str)
    previous = None
    for batch, active_grouping in enumerate((grouping, "separate" if grouping == "aggregate" else "aggregate")):
        if batch:
            config["context"]["source_messages"]["grouping"] = active_grouping
            path.write_text(json.dumps(config, ensure_ascii=False))
            assert await case.container.config_service.reload(idempotency_key="grouping-switch")
        names = (f"group-{batch}-tracked", f"group-{batch}-snapshot")
        for name in names:
            skill = case.workspace / ".boxteam/skills" / name / "SKILL.md"
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text(f"---\nname: {name}\ndescription: 分组验收\n---\n{name} 唯一正文 Ω\n")
        calls = await case.plan(session, [{"tool_calls": [
            {"index": index, "id": f"group-{batch}-{index}", "type": "function", "function": {
                "name": "skill_load", "arguments": json.dumps({"name": name, "mode": mode}),
            }} for index, (name, mode) in enumerate(zip(names, ("tracked", "snapshot"), strict=True))
        ]}, {"content": f"batch-{batch} 完成"}])
        accepted = await _send(case, session, f"加载第 {batch} 批来源")
        job_id = accepted["job_id"]
        assert isinstance(job_id, str) and accepted["thread_id"] == thread
        await _job(case, job_id)
        # 外部 Provider wire 是异构 JSON；只在该断言边界使用 Any。
        wire = cast(list[dict[str, Any]], case.request(calls[-1])["messages"])
        call_index, = [index for index, message in enumerate(wire) if any(
            call["id"] == f"group-{batch}-0" for call in message.get("tool_calls", [])
        )]
        assert [message["role"] for message in wire[call_index + 1:call_index + 3]] == ["tool", "tool"]
        assert [message["tool_call_id"] for message in wire[call_index + 1:call_index + 3]] == [f"group-{batch}-0", f"group-{batch}-1"]
        latest = saver.list_context_assemblies(session, thread)[-1]
        assert latest.seal_binding is not None
        frames = latest.seal_binding.prefix.state.frames
        members = [(frame, ref, block) for frame in frames if frame.source_protocol is not None and frame.role == "user"
                   for ref, block in zip(frame.item_refs, parse_source_blocks(frame.content), strict=True)
                   if block.name in names]
        assert [block.name for _, _, block in members] == list(names)
        assert [ref.source_tracking_at_seal for _, ref, _ in members] == ["tracked", "untracked"]
        assert [ref.source_revision for _, ref, _ in members] == [block.revision for _, _, block in members]
        assert len({frame.ordinal for frame, _, _ in members}) == (1 if active_grouping == "aggregate" else 2)
        for _, ref, block in members:
            assert block.body == block.name + " 唯一正文 Ω\n"
            selected, = [entry for entry in latest.selection if entry.included and entry.ref.ref_id == ref.ref_id]
            assert selected.source_revision == ref.source_revision
            occurrence, = [index for index, message in enumerate(wire)
                           if isinstance(message.get("content"), str)
                           and message["content"].startswith('{"protocol":"boxteam-context-sources:')
                           and any(candidate.body == block.body for candidate in parse_source_blocks(message["content"]))]
            assert occurrence > call_index + 2
        if previous is not None:
            assert previous.seal_binding is not None
            assert frames[:len(previous.seal_binding.prefix.state.frames)] == previous.seal_binding.prefix.state.frames
        previous = latest
    assert previous is not None
    frozen = canonical_json_bytes(previous.to_dict())
    point_response = await case.client.get(f"/api/v1/sessions/{session}/fork-source-point", params={"mode": "full_rollout_copy"})
    assert point_response.status_code == 200, point_response.text
    point = point_response.json()["data"]
    key = "grouped-source-full-copy"
    deadline = cold_fork_deadlines.for_operation(point["main_container_uri"], key)
    response = await deadline.wait(case.client.post(f"/api/v1/sessions/{session}/fork-context", json={
        "idempotency_key": key, "source_uri": point["source_uri"], "source_view_id": point["source_view_id"],
        "source_view_revision": point["source_view_revision"], "mode": "full_rollout_copy",
        "target_container_uri": point["main_container_uri"], "creation_kind": "new_session_main", "title": "分组恢复",
    }), "来源分组 full-copy HTTP")
    case.write_actual("grouping-fork-response", {"status_code": response.status_code, "body": response.json()})
    try:
        assert response.status_code == 200, response.text
    finally:
        # HTTP 已结束；失败仍保留原响应并使测试失败，不能让活跃计时器遮蔽错误。
        cold_fork_deadlines.complete(deadline)
    target = response.json()["data"]
    target_session, target_thread = target["session"]["session_id"], target["thread_id"]
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        copied = cold.get_context_assembly(target_session, target_thread, assembly_id=previous.assembly_id)
        assert canonical_json_bytes(copied.to_dict()) == frozen
        assert (copied.session_id, copied.thread_id) == (session, thread)
    await case.plan(target_session, [{"content": "fork 后来源分组可继续"}], thread_id=target_thread)
    accepted = await _send(case, target_session, "继续")
    job_id = accepted["job_id"]
    assert isinstance(job_id, str)
    await _job(case, job_id)
    assert canonical_json_bytes(saver.get_context_assembly(session, thread, assembly_id=previous.assembly_id).to_dict()) == frozen
