"""同次 prepare 前驱复用与唯一 seal 事务的新鲜校验合同。"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 按当前测试文件隔离正式工作区
)
from tests.integration.backend.sessions.rollout_context.test_context_production_prefix import (
    production_context as production_context,  # noqa: PLC0414 - 复用真实 prepare fixture
)

pytestmark = pytest.mark.parametrize("registry_db", ["protected"], indirect=True)


@pytest.fixture
def predecessor_reads(production_context, monkeypatch):
    saver = production_context[0]
    assemblies = saver._storage.assemblies
    original_id = assemblies._latest_context_assembly_id_connection
    original_read = assemblies._read_context_assembly_connection
    original_compose = saver._compose_committed_context_plan
    identities, reads, compositions = [], [], []

    def latest_id(connection, *args, **kwargs):
        result = original_id(connection, *args, **kwargs)
        identities.append((connection, result, kwargs.get("excluding_assembly_id")))
        return result

    def read(connection, *args, **kwargs):
        reads.append((connection, kwargs["assembly_id"]))
        return original_read(connection, *args, **kwargs)

    def compose(snapshot, *args, **kwargs):
        compositions.append((snapshot.connection, kwargs["predecessor"]))
        return original_compose(snapshot, *args, **kwargs)

    monkeypatch.setattr(assemblies, "_latest_context_assembly_id_connection", latest_id)
    monkeypatch.setattr(assemblies, "_read_context_assembly_connection", read)
    monkeypatch.setattr(saver, "_compose_committed_context_plan", compose)
    return identities, reads, compositions


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
@pytest.mark.parametrize("damage_stage", ["after-guidance", "before-next-attempt"])
def test_prepare_rejects_predecessor_manifest_damage_at_its_fresh_boundary(
    production_context, predecessor_reads, monkeypatch, target_format, damage_stage,
):
    saver, session, _thread, connection, manager, prepare = production_context
    first = prepare("manifest-base", target_format=target_format)
    manager.observe("instruction:R01", "尚未确认的第二版", revision="instructions:2")
    pending = manager.prepare_pending()
    original_register = saver.register_context_contribution
    identities, reads, compositions = predecessor_reads
    for calls in predecessor_reads:
        calls.clear()
    damaged = []
    commits = connection.execute(
        "SELECT commit_id FROM storage_commits WHERE commit_kind='assembly_sealed'",
    ).fetchall()
    jsonl = saver._storage.storage.jsonl_path(session).read_bytes()

    def damage():
        cursor = connection.execute(
            "UPDATE context_assembly_selections SET omission_reason='forged-after-guidance' "
            "WHERE assembly_id=? AND plan_ordinal=0",
            (first["assembly_id"],),
        )
        assert cursor.rowcount == 1
        connection.commit()
        damaged.append(first["assembly_id"])

    def register(*args, **kwargs):
        result = original_register(*args, **kwargs)
        if damage_stage == "after-guidance" and not damaged:
            assert [assembly_id for _connection, assembly_id in reads] == [first["assembly_id"]]
            damage()
        return result

    monkeypatch.setattr(saver, "register_context_contribution", register)
    if damage_stage == "before-next-attempt":
        damage()
    with pytest.raises(RuntimeError, match="context assembly selection manifest"):
        prepare("manifest-damaged", target_format=target_format)
    assert damaged == [first["assembly_id"]]
    if damage_stage == "after-guidance":
        # 同 ID 的外部篡改延迟到 writer 独立完整读取时拒绝，不声称 compose 会重验。
        assert [assembly_id for _connection, assembly_id, _excluded in identities] == [first["assembly_id"]] * 3
        assert [assembly_id for _connection, assembly_id in reads] == [first["assembly_id"]] * 2
        assert len(compositions) == 1
        assert all(read_connection is not compositions[0][0] for read_connection, _assembly_id in reads)
        assert identities[-1][2] is not None
    else:
        assert len(identities) == len(reads) == 1
        assert identities[0][1:] == (first["assembly_id"], None)
        assert compositions == []
    assert manager.prepare_pending() == pending
    assert connection.execute(
        "SELECT latest_visible_committed_revision FROM context_source_control_states",
    ).fetchone() == ("instructions:1",)
    assert connection.execute("SELECT assembly_id FROM context_assemblies").fetchall() == [(first["assembly_id"],)]
    assert connection.execute(
        "SELECT commit_id FROM storage_commits WHERE commit_kind='assembly_sealed'",
    ).fetchall() == commits
    assert connection.execute(
        "SELECT DISTINCT assembly_id FROM resource_activation_assembly_bindings",
    ).fetchall() == [(first["assembly_id"],)]
    assert saver._storage.storage.jsonl_path(session).read_bytes() == jsonl
    assert list(saver._prepared_dispatches.values()) == [[first]]


@pytest.mark.parametrize("target_format", ["chat_completions", "responses"])
def test_prepare_follows_predecessor_advanced_during_contribution_registration(
    production_context, predecessor_reads, monkeypatch, target_format,
):
    saver, _session, _thread, connection, manager, prepare = production_context
    first = prepare("advance-base", target_format=target_format)
    original_register = saver.register_context_contribution
    identities, reads, compositions = predecessor_reads
    for calls in predecessor_reads:
        calls.clear()
    registering, competing = [], []
    messages = (
        AIMessage(
            id="advance-assistant", content="竞争请求新增的读取说明",
            tool_calls=[{"id": "advance-call", "name": "read_file", "args": {"path": "a.txt"}}],
        ),
        ToolMessage(id="advance-result", tool_call_id="advance-call", name="read_file", content="竞争请求新增的文件正文"),
    )

    def register_then_compete(*args, **kwargs):
        result = original_register(*args, **kwargs)
        if not registering:
            registering.append(True)
            competing.append(prepare("advance-winner", request_messages=messages, target_format=target_format))
        return result

    monkeypatch.setattr(saver, "register_context_contribution", register_then_compete)
    continued = prepare("advance-continued", target_format=target_format)
    assert len(competing) == 1
    winner = competing[0]
    assert winner["snapshot"].seal_binding.previous_assembly_id == first["assembly_id"]
    assert continued["snapshot"].seal_binding.previous_assembly_id == winner["assembly_id"]
    assert [(excluded, assembly_id) for _connection, assembly_id, excluded in identities if excluded is not None] == [
        (winner["assembly_id"], first["assembly_id"]),
        (continued["assembly_id"], winner["assembly_id"]),
    ]
    assert [previous.assembly_id for _connection, previous in compositions] == [first["assembly_id"], winner["assembly_id"]]
    # 变化后的 winner 必须在 compose 所属连接内深读，不能只把 ID 填进 binding。
    assert (compositions[-1][0], winner["assembly_id"]) in reads
    first_ids, winner_ids, continued_ids = (
        {entry.ref.ref_id for entry in result["snapshot"].selection if entry.included and entry.ref.ref_type == "canonical_item"}
        for result in (first, winner, continued)
    )
    assert winner_ids > first_ids
    assert continued_ids == winner_ids
    frames = winner["snapshot"].seal_binding.prefix.state.frames
    assert continued["snapshot"].seal_binding.prefix.state.frames[:len(frames)] == frames
    assert [message.content for message in continued["messages"][-2:]] == [message.content for message in messages]
    assert connection.execute(
        "SELECT assembly_id FROM context_assemblies ORDER BY rowid",
    ).fetchall() == [(first["assembly_id"],), (winner["assembly_id"],), (continued["assembly_id"],)]
    assert connection.execute(
        "SELECT count(*) FROM storage_commits WHERE commit_kind='assembly_sealed'",
    ).fetchone() == (3,)
    assert connection.execute(
        "SELECT plan_state, assembly_id FROM context_plans "
        "WHERE plan_creation_idempotency_key='create:advance-continued'",
    ).fetchone() == ("sealed", continued["assembly_id"])
    assert manager.prepare_pending() is None
    assert list(saver._prepared_dispatches.values()) == [[first, winner, continued]]


@pytest.mark.parametrize("missing_stage", ["guidance", "compose"])
def test_prepare_exact_predecessor_identity_comparison_handles_none(
    production_context, predecessor_reads, monkeypatch, missing_stage,
):
    saver, _session, _thread, connection, manager, prepare = production_context
    first = prepare("none-base")
    for calls in predecessor_reads:
        calls.clear()
    identities, reads, compositions = predecessor_reads
    assemblies = saver._storage.assemblies
    original_id = assemblies._latest_context_assembly_id_connection
    queried = []

    def latest_id(*args, **kwargs):
        actual_id = original_id(*args, **kwargs)
        queried.append(actual_id)
        # 本地 sealed owner 不会因普通并发消失；这里只定向注入资格查询的 None
        # 结果，验证两向比较，不把它当作真实 fork/view 竞争的证明。
        missing_index = 1 if missing_stage == "guidance" else 2
        return None if len(queried) == missing_index else actual_id

    monkeypatch.setattr(assemblies, "_latest_context_assembly_id_connection", latest_id)
    if missing_stage == "guidance":
        result = prepare("none-to-id")
        assert result["snapshot"].seal_binding.previous_assembly_id == first["assembly_id"]
        assert (compositions[0][0], first["assembly_id"]) in reads
        assert len(reads) == 2  # compose 新前驱与 writer 独立读取
    else:
        with pytest.raises(RuntimeError, match="context-seal-predecessor-conflict"):
            prepare("id-to-none")
        assert compositions[0][1] is None
        assert all(read_connection is not compositions[0][0] for read_connection, _assembly_id in reads)
        assert connection.execute("SELECT assembly_id FROM context_assemblies").fetchall() == [(first["assembly_id"],)]
        assert list(saver._prepared_dispatches.values()) == [[first]]
    assert queried == [first["assembly_id"]] * 3
    assert identities[-1][2] is not None
    assert manager.prepare_pending() is None
