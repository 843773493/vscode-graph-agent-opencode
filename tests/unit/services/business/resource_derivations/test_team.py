"""团队loader的冻结可见性、独立facet及输入向量合同。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from app.services.business.resource_derivations.team import (
    TEAM_EVENT_SOURCE_ID,
    TEAM_LEDGER_SOURCE_ID,
    TeamFacetLoader,
    team_guidance_content,
)
from app.services.infrastructure.resource_platform.derivation.types import SemanticInput


@pytest.fixture
def frozen_ledger():
    return {
        "session_id": "session", "ledger_revision": 7, "event_hash": "event-7",
        "event": {"type": "task.changed"},
        "boards": [{"team_id": "team", "coordinator_thread_id": "coordinator", "name": "冻结任务板", "tasks": []}],
        "members": [{
            "delegation_id": "delegation", "child_thread_id": "child", "role": "reviewer",
            "state": "published", "admission_state": "bound", "title": "展示名称",
            "task_seed": json.dumps({"trusted_context": {"team_id": "team"}}),
        }],
    }


@pytest.fixture
def accepted_inputs_factory():
    def build(value):
        return {
            TEAM_LEDGER_SOURCE_ID: SemanticInput(identity=TEAM_LEDGER_SOURCE_ID,
                revision=str(value["ledger_revision"]), generation=0, content=json.dumps(value)),
            TEAM_EVENT_SOURCE_ID: SemanticInput(identity=TEAM_EVENT_SOURCE_ID,
                revision=value["event_hash"], generation=0, content=value["event_hash"]),
        }
    return build


def test_visible_membership_and_tasks_keep_distinct_business_fields(frozen_ledger, accepted_inputs_factory):
    inputs = accepted_inputs_factory(frozen_ledger)
    membership = json.loads(team_guidance_content(TeamFacetLoader("session", "child", "membership")(inputs).payload))
    tasks = json.loads(team_guidance_content(TeamFacetLoader("session", "child", "tasks")(inputs).payload))
    assert membership == {"session_id": "session", "thread_id": "child", "membership": [{
        "team_id": "team", "coordinator_thread_id": "coordinator", "members": [{
            "delegation_id": "delegation", "child_thread_id": "child", "role": "reviewer",
            "state": "published", "admission_state": "bound",
        }],
    }]}
    assert tasks == {"session_id": "session", "thread_id": "child", "tasks": frozen_ledger["boards"]}


def test_ignored_fields_and_membership_task_changes_are_independent(frozen_ledger, accepted_inputs_factory):
    loaders = {facet: TeamFacetLoader("session", "coordinator", facet) for facet in ("membership", "tasks")}
    original = {facet: loader(accepted_inputs_factory(frozen_ledger)).payload for facet, loader in loaders.items()}
    ignored = deepcopy(frozen_ledger)
    ignored["ledger_revision"], ignored["event_hash"] = 8, "event-8"
    ignored["event"] = {"type": "member.display_name_changed"}
    ignored["members"][0]["title"] = "新的展示名称"
    hidden = deepcopy(ignored["members"][0])
    hidden.update(delegation_id="hidden", child_thread_id=None, state="registering")
    ignored["members"].append(hidden)
    assert {facet: loader(accepted_inputs_factory(ignored)).payload for facet, loader in loaders.items()} == original
    changed_member = deepcopy(ignored)
    changed_member["members"][0]["role"] = "implementer"
    membership = loaders["membership"](accepted_inputs_factory(changed_member)).payload
    assert membership != original["membership"]
    assert loaders["tasks"](accepted_inputs_factory(changed_member)).payload == original["tasks"]
    changed_task = deepcopy(changed_member)
    changed_task["boards"][0]["tasks"] = [{"task_id": "task", "title": "新增任务"}]
    assert loaders["membership"](accepted_inputs_factory(changed_task)).payload == membership
    assert loaders["tasks"](accepted_inputs_factory(changed_task)).payload != original["tasks"]


@pytest.mark.parametrize("facet", ["membership", "tasks"])
def test_unrelated_thread_cannot_see_team(frozen_ledger, accepted_inputs_factory, facet):
    result = TeamFacetLoader("session", "unrelated", facet)(accepted_inputs_factory(frozen_ledger))
    assert result.available and result.payload == {"content": ""}


@pytest.mark.parametrize("damage", ["nontext", "json", "shape", "owner", "revision", "event"])
def test_invalid_accepted_value_is_explicitly_unavailable(frozen_ledger, accepted_inputs_factory, damage):
    inputs = accepted_inputs_factory(frozen_ledger)
    if damage == "event":
        inputs[TEAM_EVENT_SOURCE_ID] = replace(inputs[TEAM_EVENT_SOURCE_ID], revision="another-event")
    else:
        content = inputs[TEAM_LEDGER_SOURCE_ID].content
        if damage == "nontext":
            content = None
        elif damage == "json":
            content = "{"
        elif damage == "shape":
            content = "[]"
        elif damage == "owner":
            content = json.dumps({**frozen_ledger, "session_id": "another-session"})
        else:
            content = json.dumps({**frozen_ledger, "ledger_revision": 8})
        inputs[TEAM_LEDGER_SOURCE_ID] = replace(inputs[TEAM_LEDGER_SOURCE_ID], content=content)
    result = TeamFacetLoader("session", "coordinator", "membership")(inputs)
    assert result.available is False and result.payload is None
    assert result.error


def test_unknown_facet_is_a_program_error():
    with pytest.raises(ValueError, match="未知团队 facet"):
        TeamFacetLoader("session", "coordinator", "unknown")
