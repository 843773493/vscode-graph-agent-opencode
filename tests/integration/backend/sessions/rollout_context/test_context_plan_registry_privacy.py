"""访问分类与 exact 内部 manifest 的真实 SQLite 持久化合同。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.identity.refs import ToolSetRef
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    create_registration,
    read_registration,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    draft as draft,  # noqa: PLC0414 - 显式导出 pytest fixture
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 使用本文件的正式输出路径
)


@pytest.mark.parametrize("protection", ["public", "protected", "redacted"])
def test_access_classification_keeps_exact_body_hash(draft, registry_db, protection):
    _saver, session, connection, _accepted = registry_db
    original = draft.contributions[0]
    annotation = {"api_key": "internal-business-value", "headers": {"Authorization": "business-value"}}
    contribution = replace(
        original, body=None, protection=protection,
        extensions={**original.extensions, "boxteam.test.annotation": {"extension_version": "v1", "value": annotation, "protection": protection}},
    )
    ref = replace(draft.refs[0], protection=protection)
    plan = replace(draft, refs=(ref,), contributions=(contribution,))
    before = canonical_json_bytes(plan.to_dict())
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        registered = create_registration(connection, plan)
    restored = read_registration(connection, session, plan.thread_id, plan.plan_id)
    assert restored == registered
    assert canonical_json_bytes(plan.to_dict()) == before
    assert restored.draft.contributions[0].body is None
    assert restored.draft.contributions[0].content_hash == original.content_hash
    assert restored.draft.contributions[0].extensions["boxteam.test.annotation"]["value"] == annotation


def test_tool_policy_persists_exact_internal_values(draft, registry_db):
    _saver, session, connection, _accepted = registry_db
    original = draft.tool_set_refs[0]
    policy = {
        "headers": {"Authorization": "internal-policy-value"},
        "parallel_tool_calls": False,
        "wire_tool_order": ["read_file"],
    }
    tool = ToolSetRef.from_tool_snapshot(
        snapshot_id=original.ref_id, session_id=draft.session_id,
        thread_id=draft.thread_id, plan_id=draft.plan_id,
        source_revision=original.source_revision, tools=original.tools, tool_policy=policy,
    )
    plan = replace(draft, tool_set_refs=(tool,))
    before_hash = plan.plan_hash()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        registered = create_registration(connection, plan)
    restored = read_registration(connection, session, plan.thread_id, plan.plan_id)
    assert restored == registered
    assert restored.draft.tool_set_refs[0].tool_policy == policy
    assert plan.plan_hash() == before_hash


def test_tool_schema_property_names_remain_business_semantics(draft):
    original = draft.tool_set_refs[0]
    tool = ToolSetRef.from_tool_snapshot(
        snapshot_id=original.ref_id, session_id=draft.session_id,
        thread_id=draft.thread_id, plan_id=draft.plan_id,
        source_revision=original.source_revision,
        tools=({"name": "inspect_configuration", "parameters": {
            "type": "object", "properties": {"api_key": {"type": "string"}, "password": {"type": "string"}},
        }},),
        tool_policy={"parallel_tool_calls": False},
    )
    plan = replace(draft, tool_set_refs=(tool,))
    assert plan.tool_set_refs[0].tools == tool.tools
    assert plan.plan_hash() != draft.plan_hash()
