from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from app.agents.tools.skill_loading import create_skill_load_tool
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceDescriptor,
    ContextSourceManager,
    ContextSourceTrackingStateConflict,
    SkillCatalogActivationSnapshot,
    SkillCatalogBinding,
    _revision,
)


def _manager():
    manager = ContextSourceManager()
    manager.register(
        ContextSourceDescriptor(
            source_id="skill:debugging",
            source_kind="skill",
            name="debugging",
            description="调试工作流",
            internal_locator="/.boxteam/skills/debugging/SKILL.md",
            resource_uri="boxteam://workspace/skill/debugging",
        )
    )
    return manager


def _commit_model_call_pending(manager: ContextSourceManager):
    batch = manager.prepare_pending()
    assert batch is not None
    manager.commit_model_call_pending(batch)
    return batch.deltas


def _install_snapshot(
    manager: ContextSourceManager,
    name: str,
    body: str,
    *,
    display_uri: str | None = None,
) -> SkillCatalogBinding:
    """安装与手工 activation revision 一致的冻结 snapshot(测试辅助)。"""
    binding = SkillCatalogBinding(
        name=name,
        resource_id=f"skill-entry:test:{name}:activation",
        entry_identity=f"skill-entry:test:{name}",
        display_uri=display_uri or f"boxteam://workspace/skill/{name}",
        activation_revision=_revision(body),
        body=body,
    )
    manager.install_skill_activation_snapshot(
        SkillCatalogActivationSnapshot(
            catalog_revision="sha256:test-catalog",
            entries={name: binding},
        )
    )
    return binding


def test_snapshot_load_reads_once_and_exposes_only_metadata():
    manager = _manager()
    # 冻结 snapshot 是唯一 content port:load 路径零 I/O,不读任何来源。
    _install_snapshot(manager, "debugging", "# Debugging\n\nUse the debugger.\n")

    first = manager.load_skill("debugging")
    second = manager.load_skill("debugging")
    deltas = _commit_model_call_pending(manager)

    assert first.mode == "snapshot"
    assert first.queued is True
    assert second.queued is True
    assert first.revision == second.revision == deltas[0].revision
    assert deltas[0].kind == "activation"
    assert deltas[0].wire_role == "user"
    assert manager.metadata() == (
        {"name": "debugging", "description": "调试工作流"},
    )
    assert not hasattr(first, "internal_locator")


def test_tracked_changes_are_coalesced_from_last_latest_visible_committed_revision():
    manager = _manager()
    manager.activate_skill_content("debugging", "v1\n")
    _install_snapshot(manager, "debugging", "v1\n")
    manager.load_skill("debugging", mode="snapshot")
    _commit_model_call_pending(manager)
    manager.load_skill("debugging", mode="tracked")

    assert manager.observe("skill:debugging", "v2\n") is True
    assert manager.observe("skill:debugging", "v3\n") is True
    deltas = _commit_model_call_pending(manager)

    assert len(deltas) == 1
    assert deltas[0].kind == "delta"
    assert "v1" in deltas[0].content
    assert "v3" in deltas[0].content
    assert "v2" not in deltas[0].content
    assert manager.observe("skill:debugging", "v3\n") is False


def test_untrack_stops_observation_without_removing_existing_context():
    manager = _manager()
    manager.activate_skill_content("debugging", "v1\n")
    _install_snapshot(manager, "debugging", "v1\n")
    manager.load_skill("debugging", mode="tracked")
    activation = _commit_model_call_pending(manager)
    receipt = manager.load_skill("debugging", mode="untrack")

    assert receipt.tracked is False
    assert manager.observe("skill:debugging", "v2\n") is False
    assert manager.prepare_pending() is None
    assert activation[0].content == "v1\n"


def test_restore_after_rewind_reappends_full_current_revision():
    manager = _manager()
    manager.activate_skill_content("debugging", "v1\n")
    _install_snapshot(manager, "debugging", "v1\n")
    manager.load_skill("debugging", mode="tracked")
    _commit_model_call_pending(manager)
    manager.observe("skill:debugging", "v2\n")
    _commit_model_call_pending(manager)

    assert manager.restore_active_revision("skill:debugging", "sha256:rewound") is True
    (delta,) = _commit_model_call_pending(manager)
    assert delta.kind == "rebuild"
    assert delta.content == "v2\n"


_SKILL_LOAD_RESULT_KEYS = {
    "status",
    "name",
    "mode",
    "display_uri",
    "revision",
    "content_hash",
    "append_status",
    "tracked",
    "queued",
    "source_rebound",
    "error",
}


def _assert_no_locator_or_body(payload: dict, locator: str, body: str) -> None:
    """路径隐藏合同:任何字段值都不得携带 locator 或正文。"""
    for key, value in payload.items():
        serialized = value if isinstance(value, str) else json.dumps(value)
        assert locator not in serialized, f"{key} 泄露 locator"
        assert body not in serialized, f"{key} 泄露正文"
        assert ".boxteam" not in serialized, f"{key} 泄露物理路径"


def test_skill_load_tool_resolves_only_from_frozen_snapshot():
    manager = _manager()
    _install_snapshot(manager, "debugging", "正文\n")
    skill_load = create_skill_load_tool(manager)
    first = json.loads(skill_load.invoke({"name": "debugging"}))
    second = json.loads(skill_load.invoke({"name": "debugging"}))

    assert first["name"] == second["name"] == "debugging"
    assert first["revision"] == second["revision"]
    assert "path" not in first
    assert "internal_locator" not in first
    # 固定 schema 与脱敏合同。
    assert set(first) == _SKILL_LOAD_RESULT_KEYS
    assert first["status"] == "loaded"
    assert first["append_status"] == "appended"
    assert first["tracked"] is False
    assert first["error"] is None
    assert first["revision"].startswith("sha256:")
    assert first["content_hash"].startswith("sha256:")
    for payload in (first, second):
        _assert_no_locator_or_body(payload, "/.boxteam/skills/debugging/SKILL.md", "正文")


def test_skill_load_tool_rejects_unknown_arguments():
    manager = _manager()
    skill_load = create_skill_load_tool(manager)

    with pytest.raises(ValidationError, match="unexpected"):
        skill_load.args_schema.model_validate(
            {"name": "debugging", "unexpected": True}
        )


def test_skill_load_receipt_exposes_safe_display_uri_and_hash():
    manager = ContextSourceManager()
    manager.register(
        ContextSourceDescriptor(
            source_id="skill:report",
            source_kind="skill",
            name="report",
            description="周报工作流",
            internal_locator="/.boxteam/skills/report/SKILL.md",
            resource_uri="boxteam://workspace/skill/report",
        )
    )
    binding = _install_snapshot(manager, "report", "# 周报\n")
    receipt = manager.load_skill("report")
    assert receipt.display_uri == binding.display_uri
    assert receipt.revision == binding.activation_revision
    assert receipt.content_hash == binding.content_hash
    assert receipt.status == "loaded"
    assert receipt.append_status == "appended"
    assert not hasattr(receipt, "internal_locator")
    assert "/.boxteam" not in repr(receipt)


def test_skill_load_repeat_snapshot_after_commit_is_already_active():
    manager = _manager()
    _install_snapshot(manager, "debugging", "v1\n")
    first = manager.load_skill("debugging")
    assert first.status == "loaded"
    assert first.append_status == "appended"
    _commit_model_call_pending(manager)
    second = manager.load_skill("debugging")
    assert second.status == "already_active"
    assert second.append_status == "already_active"
    assert second.queued is False
    assert second.revision == first.revision
    # 同 revision 重复 snapshot 不得追加第二个 item。
    assert manager.prepare_pending() is None


def test_skill_load_tracked_mode_registers_tracking_and_appends():
    manager = _manager()
    binding = _install_snapshot(manager, "debugging", "v1\n")
    receipt = manager.load_skill("debugging", mode="tracked")
    assert receipt.tracked is True
    assert receipt.status == "loaded"
    assert receipt.append_status == "appended"
    assert receipt.display_uri == binding.display_uri


def test_skill_load_untrack_not_tracked_is_deterministic():
    manager = _manager()
    skill_load = create_skill_load_tool(manager)
    result = json.loads(skill_load.invoke({"name": "debugging", "mode": "untrack"}))
    assert result["status"] == "not_tracked"
    assert result["tracked"] is False
    assert result["append_status"] == "none"
    assert result["error"] is None
    assert result["revision"] is None
    assert manager.prepare_pending() is None  # 不伪造 source item


def test_skill_load_untrack_stops_tracking_without_reading_source():
    manager = _manager()
    _install_snapshot(manager, "debugging", "v1\n")
    manager.load_skill("debugging", mode="tracked")
    skill_load = create_skill_load_tool(manager)
    result = json.loads(skill_load.invoke({"name": "debugging", "mode": "untrack"}))
    assert result["status"] == "loaded"
    assert result["tracked"] is False
    # untrack 不重新解析 entry、不读取 source。
    assert manager.observe("skill:debugging", "v2\n") is False


def test_skill_load_unknown_name_returns_explicit_error():
    manager = _manager()
    skill_load = create_skill_load_tool(manager)
    result = json.loads(skill_load.invoke({"name": "missing"}))
    assert result["status"] == "error"
    assert result["error"]["code"] == "skill-not-found"
    assert "missing" in result["error"]["message"]
    assert set(result) == _SKILL_LOAD_RESULT_KEYS


def _sha(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_pending_delta_carries_content_hash_and_merged_provenance():
    """多个 pending observation 合并为唯一 delta:from 基准、载荷 hash、
    provenance 与提交后闭合全部显式(D1 构造合同)。"""
    manager = _manager()
    manager.activate_skill_content("debugging", "v1\n")
    _install_snapshot(manager, "debugging", "v1\n")
    manager.load_skill("debugging", mode="tracked")
    _commit_model_call_pending(manager)

    assert manager.observe("skill:debugging", "v2\n") is True
    assert manager.observe("skill:debugging", "v3\n") is True
    batch = manager.prepare_pending()
    assert batch is not None
    (delta,) = batch.deltas
    assert delta.previous_revision == _sha("v1\n")
    assert delta.revision == _sha("v3\n")
    assert delta.content_hash == _sha(delta.content)
    assert delta.observation_provenance == (_sha("v2\n"), _sha("v3\n"))

    manager.commit_model_call_pending(batch)
    assert manager.observe("skill:debugging", "v4\n") is True
    batch2 = manager.prepare_pending()
    assert batch2 is not None
    (delta2,) = batch2.deltas
    assert delta2.previous_revision == _sha("v3\n")
    assert delta2.observation_provenance == (_sha("v4\n"),)


def test_same_normalized_name_register_overrides_index_and_untrack_stops_old():
    """同名高优先级 entry 覆盖 catalog 名称索引但不自动改绑 active
    registration;untrack 按 active tracked registration 定位原 registration。"""
    manager = _manager()
    _install_snapshot(manager, "debugging", "v1\n")
    manager.load_skill("debugging", mode="tracked")
    _commit_model_call_pending(manager)

    manager.register(
        ContextSourceDescriptor(
            source_id="skill:debugging-next",
            source_kind="skill",
            name=" debugging ",  # normalized 后同名
            description="新优先级 entry",
            internal_locator="/.boxteam/skills/debugging-next/SKILL.md",
        )
    )
    assert manager._source_ids_by_name["debugging"] == "skill:debugging-next"

    receipt = manager.load_skill("debugging", mode="untrack")
    assert receipt.status == "loaded"
    assert receipt.tracked is False
    # 停止的是原 active registration(skill:debugging),不是新 catalog entry。
    assert manager.observe("skill:debugging", "v2\n") is False

    again = manager.load_skill("debugging", mode="untrack")
    assert again.status == "not_tracked"


def test_load_tracked_with_changed_effective_entry_rebinds_and_freezes_old():
    """6.2-B：effective entry identity 已变时显式 tracked 触发 rebind。

    冻结旧 registration、为当前 effective entry 追加完整 activation、建立新
    tracking，并返回独立成功态 rebound + source_rebound=true；旧 item 不变。
    """
    manager = _manager()
    _install_snapshot(manager, "debugging", "v1\n")
    manager.load_skill("debugging", mode="tracked")
    (old_activation,) = _commit_model_call_pending(manager)
    assert old_activation.content == "v1\n"
    assert manager._source_ids_by_name["debugging"] == "skill:debugging"

    # 同名高优先级 effective entry 出现：不同 source identity / display URI。
    manager.register(
        ContextSourceDescriptor(
            source_id="skill:debugging-next",
            source_kind="skill",
            name="debugging",
            description="新优先级 entry",
            internal_locator="/.boxteam/skills/debugging-next/SKILL.md",
            resource_uri="boxteam://workspace/skill/debugging-next",
        )
    )
    binding = _install_snapshot(
        manager,
        "debugging",
        "v2\n",
        display_uri="boxteam://workspace/skill/debugging-next",
    )
    assert manager._source_ids_by_name["debugging"] == "skill:debugging-next"

    receipt = manager.load_skill("debugging", mode="tracked")

    assert receipt.status == "rebound"
    assert receipt.source_rebound is True
    assert receipt.append_status == "appended"
    assert receipt.tracked is True
    assert receipt.display_uri == binding.display_uri
    # 旧 registration 冻结为 untracked：不再消费 revision。
    assert manager.observe("skill:debugging", "v3\n") is False
    # 新 effective entry 追加完整 activation（非 delta），正文逐字为 v2。
    batch = manager.prepare_pending()
    assert batch is not None
    (rebound_delta,) = batch.deltas
    assert rebound_delta.source_id == "skill:debugging-next"
    assert rebound_delta.kind == "activation"
    assert rebound_delta.content == "v2\n"
    # 旧 context item 逐字节保持不变。
    assert old_activation.content == "v1\n"
    assert old_activation.revision != rebound_delta.revision


def test_load_tracked_same_effective_entry_is_idempotent_already_active():
    """6.2-B：effective entry 未变时幂等复用，不追加第二个 item。"""
    manager = _manager()
    _install_snapshot(manager, "debugging", "v1\n")
    manager.load_skill("debugging", mode="tracked")
    _commit_model_call_pending(manager)

    again = manager.load_skill("debugging", mode="tracked")

    assert again.status == "already_active"
    assert again.source_rebound is False
    assert again.append_status == "already_active"
    assert manager.prepare_pending() is None


def test_load_tracked_rebind_with_entry_already_visible_reports_both_dimensions():
    """6.2-B 组合边界：rebind 成功、但新 entry 已在可见台账。

    两个正交事实维度必须同时如实承载：``status`` 只描述"本次是否追加了
    context item"（新 entry 已可见时无第二个 item 追加，返回 ``already_active``），
    ``source_rebound`` 只描述"是否发生了 rebind"（已发生，为 True）。此时 rebind
    仍须完成：旧 registration 冻结、新 registration 建立。
    """
    manager = _manager()
    _install_snapshot(manager, "debugging", "v1\n")
    manager.load_skill("debugging", mode="tracked")
    _commit_model_call_pending(manager)

    # 新 effective entry 出现。
    manager.register(
        ContextSourceDescriptor(
            source_id="skill:debugging-next",
            source_kind="skill",
            name="debugging",
            description="新优先级 entry",
            internal_locator="/.boxteam/skills/debugging-next/SKILL.md",
            resource_uri="boxteam://workspace/skill/debugging-next",
        )
    )
    binding = _install_snapshot(
        manager,
        "debugging",
        "v2\n",
        display_uri="boxteam://workspace/skill/debugging-next",
    )
    # 新 entry 的当前 revision 已在可见台账：source owner 已交付并提交其正文，
    # 但此时该 entry 仍未建立 tracked registration。
    manager.activate_skill_content("debugging", "v2\n")
    _commit_model_call_pending(manager)
    # rebind 前：新 entry 已注册但尚未 tracked。
    assert manager._sources["skill:debugging-next"].tracked is False

    receipt = manager.load_skill("debugging", mode="tracked")

    # 维度一：本次未追加第二个 item。
    assert receipt.status == "already_active"
    assert receipt.append_status == "already_active"
    assert receipt.queued is False
    # 维度二：rebind 确已发生。
    assert receipt.source_rebound is True
    assert receipt.tracked is True
    assert receipt.display_uri == binding.display_uri
    # 旧 registration 冻结为 untracked：不再消费 revision。
    assert manager.observe("skill:debugging", "v3\n") is False
    # 新 registration 由本次 rebind 建立且为 tracked（此前为 untracked）。
    assert manager._sources["skill:debugging-next"].tracked is True
    # 无第二个 item 追加。
    assert manager.prepare_pending() is None


def test_load_tracked_duplicate_same_effective_entry_fails_closed():
    """反例守卫：同名同 effective entry 的重复 registration 不得被误判为 rebind。

    两个 source_id 解析到同一 resolved source identity（同一 display URI）时，
    effective entry 并未改变；必须 fail closed，绝不冻结旧 registration 或重复
    追加 context item。
    """
    manager = _manager()
    _install_snapshot(manager, "debugging", "v1\n")
    manager.load_skill("debugging", mode="tracked")
    _commit_model_call_pending(manager)

    # 同一 effective entry 的别名 registration（同 display URI，不同 source_id）。
    manager.register(
        ContextSourceDescriptor(
            source_id="skill:debugging-alias",
            source_kind="skill",
            name="debugging",
            description="同 entry 别名",
            internal_locator="/.boxteam/skills/debugging/SKILL.md",
            resource_uri="boxteam://workspace/skill/debugging",
        )
    )
    assert manager._source_ids_by_name["debugging"] == "skill:debugging-alias"

    with pytest.raises(ContextSourceTrackingStateConflict):
        manager.load_skill("debugging", mode="tracked")
    # 原 registration 仍 tracked：未冻结、未静默改绑。
    assert manager._sources["skill:debugging"].tracked is True
    # 未产生第二个 item 的 rebind activation（pending 只可能是别名自身的首帧）。
    pending = manager.prepare_pending()
    if pending is not None:
        assert [delta.source_id for delta in pending.deltas] == [
            "skill:debugging-alias"
        ]


def test_load_tracked_with_multiple_active_registrations_fails_closed():
    """多个 active registration 无法裁决唯一 effective entry，仍 fail closed。"""
    manager = ContextSourceManager()
    for source_id in ("skill:debugging", "skill:debugging-dup"):
        manager.register(
            ContextSourceDescriptor(
                source_id=source_id,
                source_kind="skill",
                name="debugging",
                description="重复 registration",
                internal_locator=f"/.boxteam/skills/{source_id}/SKILL.md",
            ),
            tracking_status="tracked",
        )
    manager.register(
        ContextSourceDescriptor(
            source_id="skill:debugging-next",
            source_kind="skill",
            name="debugging",
            description="当前 effective entry",
            internal_locator="/.boxteam/skills/debugging-next/SKILL.md",
        )
    )
    _install_snapshot(manager, "debugging", "v1\n")

    with pytest.raises(ContextSourceTrackingStateConflict) as exc_info:
        manager.load_skill("debugging", mode="tracked")
    assert exc_info.value.code == "tracking-state-conflict"


def test_register_tracked_source_consumes_event_observations():
    # 受信来源（AGENTS）以 tracked 身份登记：首帧走 activation，重复注册不降级。
    manager = ContextSourceManager()
    descriptor = ContextSourceDescriptor(
        source_id="agents:workspace",
        source_kind="workspace_agents",
        name="AGENTS.md",
        description="工作区指令",
        internal_locator="boxteam://workspace/agents",
        resource_uri="boxteam://workspace/agents",
    )
    manager.register(descriptor, tracking_status="tracked")

    assert manager.observe("agents:workspace", "# 指令\n") is True
    batch = manager.prepare_pending()
    assert batch is not None
    delta = batch.deltas[0]
    assert delta.source_kind == "workspace_agents"
    assert delta.kind == "activation"
    manager.commit_model_call_pending(batch)

    manager.register(descriptor)
    status, latest = manager.source_observation_state("agents:workspace")
    assert status == "tracked"
    assert latest is not None
