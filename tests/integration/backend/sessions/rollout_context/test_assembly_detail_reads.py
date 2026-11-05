"""详情查询只恢复命中的真实封存快照，未命中正文按需检测损坏。"""

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from unittest.mock import create_autospec

import pytest

from app.abstractions.session_context import (
    SessionContextMessageSourceProtocol,
    SessionLookupProtocol,
)
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_context import SessionContextReadRequest
from app.services.business.session_context_query_service import (
    SessionContextQueryService,
)
from app.services.business.session_context_resource import (
    context_assembly_identity,
    session_resource_identity,
    session_resource_uri,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_schema4_assembly_manifest import (
    _seal,
)
from tests.integration.backend.sessions.rollout_context.test_schema4_assembly_manifest import (
    assembly_case as assembly_case,  # noqa: PLC0414 - 按当前测试文件隔离真实存储
)


@pytest.fixture
def assembly_detail(assembly_case):
    case = assembly_case
    snapshots = tuple(_seal(case, f"detail-{index}") for index in range(12))
    workspace_id = "00000000-0000-4000-8000-000000000001"
    identity = session_resource_identity(workspace_id, case.session_id)
    uri = session_resource_uri(workspace_id, case.session_id)
    lookup = create_autospec(SessionLookupProtocol, spec_set=True)
    lookup.session_read_admission.return_value = nullcontext()
    lookup.get.return_value = SessionDTO(
        session_id=case.session_id, thread_id=case.thread_id,
        workspace_id=workspace_id, resource_identity=identity, uri=uri, scope="workspace",
        title="详情读取", current_agent_id="default",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    with RolloutCheckpointSaver(case.sessions) as cold:
        service = SessionContextQueryService(
            assembly_source=cold, session_lookup=lookup, workspace_id=workspace_id,
            message_source=create_autospec(SessionContextMessageSourceProtocol, spec_set=True),
        )
        query = SessionContextReadRequest(
            resource_identity=identity, uri=uri, scope="workspace", view="assembly",
            assembly_ref=context_assembly_identity(identity, snapshots[-1].assembly_id),
            max_chars=65_536,
        )
        yield case, snapshots, cold, service, query


@pytest.mark.asyncio
async def test_detail_read_only_restores_selected_assembly(assembly_detail, request):
    case, snapshots, cold, service, query = assembly_detail
    target = snapshots[-1]
    ids = tuple(value.assembly_id for value in snapshots)
    assert cold.list_context_assembly_ids(case.session_id, case.thread_id) == ids
    assert cold.list_context_assembly_ids(case.session_id, case.thread_id, turn_id=case.turn_id) == ids
    assert cold.list_context_assembly_ids(case.session_id, case.thread_id, turn_id="missing") == ()
    expected = cold.read_context_assembly_history(
        case.session_id, case.thread_id, target.assembly_id,
    )
    before = case.jsonl_path.read_bytes()
    started = perf_counter()
    result = await service.read_context(query)
    elapsed = (perf_counter() - started) * 1000
    assert result.items[0].data["assembly_id"] == target.assembly_id
    assert result.items[0].data["selection_count"] == len(target.selection)
    assert result.items[0].data["history_message_count"] == len(expected.messages)
    assert result.items[0].data["history_loss_count"] == len(expected.losses)
    assert len([item for item in result.items if item.kind == "selection"]) == len(target.selection)
    assert case.jsonl_path.read_bytes() == before
    evidence = TestRunContext.from_test_file(Path(request.node.path)).prepare().artifacts_dir
    (evidence / "detail-read.json").write_text(json.dumps({
        "milliseconds": elapsed,
        "result": result.model_dump(mode="json"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"milliseconds": elapsed}))


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["header", "body"])
async def test_detail_defers_unselected_damage_but_rejects_selected_damage(assembly_detail, damage):
    case, snapshots, cold, service, query = assembly_detail
    expected = await service.read_context(query)
    old = snapshots[0]
    with case.connection:
        if damage == "header":
            case.connection.execute("UPDATE context_assemblies SET plan_hash='forged' WHERE assembly_id=?", (old.assembly_id,))
        else:
            case.connection.execute("UPDATE context_assemblies SET snapshot_json='{}' WHERE assembly_id=?", (old.assembly_id,))
    before = case.connection.execute("SELECT * FROM context_assemblies ORDER BY rowid").fetchall()
    jsonl = case.jsonl_path.read_bytes()
    assert (await service.read_context(query)).model_dump() == expected.model_dump()
    broken_query = query.model_copy(update={
        "assembly_ref": context_assembly_identity(query.resource_identity, old.assembly_id),
    })
    error, message = (
        (RuntimeError, "context assembly header") if damage == "header"
        else (ValueError, "assembly content manifest 格式不受支持")
    )
    with pytest.raises(error, match=message):
        await service.read_context(broken_query)
    # 完整列表仍维持恢复全部快照的校验合同。
    with pytest.raises(error, match=message):
        cold.list_context_assemblies(case.session_id, case.thread_id)
    assert case.connection.execute("SELECT * FROM context_assemblies ORDER BY rowid").fetchall() == before
    assert case.jsonl_path.read_bytes() == jsonl
