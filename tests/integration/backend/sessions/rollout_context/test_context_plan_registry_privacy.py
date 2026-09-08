"""真实 domain/SQLite draft 上的凭据拒绝与无改写合同，不占其它 registry 工作区。"""

from __future__ import annotations

import json
import traceback
from dataclasses import replace

import pytest

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.redaction import SessionHashRedactor
from app.domain.itemized.refs import ToolSetRef
from app.services.infrastructure.rollout_context.assembly.plans.privacy import (
    validate_draft_privacy,
)
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    create_registration,
    read_registration,
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    draft as draft,  # noqa: PLC0414 - 显式导出 pytest fixture
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - request.node.path 使用本文件的独立输出
)


@pytest.fixture
def private_value():
    return "privacy-only-fixture-credential"


@pytest.fixture
def redactor():
    return SessionHashRedactor(b"registry-privacy-fixture-key-00000")


@pytest.fixture
def credential_marker(redactor, private_value):
    return redactor.redact(private_value, redaction_class="credential")


@pytest.fixture
def metadata_plan(draft, redactor):
    def build(metadata, *, protection):
        original = draft.contributions[0]
        digest = redactor.redact(original.body, redaction_class="context")[
            "redacted_stable_digest"
        ]
        contribution = replace(
            original,
            body=None,
            metadata={**original.metadata, **metadata},
            protection=protection,
            content_hash=original.content_hash if protection == "public" else None,
            redacted_stable_digest=None if protection == "public" else digest,
        )
        ref = replace(
            draft.refs[0],
            protection=protection,
            content_hash=contribution.content_hash,
            redacted_stable_digest=contribution.redacted_stable_digest,
        )
        return replace(draft, refs=(ref,), contributions=(contribution,))

    return build


@pytest.fixture
def policy_plan(draft):
    def build(policy):
        original = draft.tool_set_refs[0]
        tool = ToolSetRef.from_tool_snapshot(
            snapshot_id=original.ref_id,
            session_id=draft.session_id,
            plan_id=draft.plan_id,
            source_revision=original.source_revision,
            tools=original.tools,
            tool_policy=policy,
        )
        return replace(draft, tool_set_refs=(tool,))

    return build


@pytest.fixture
def assert_rejected_without_mutation(registry_db, private_value):
    _saver, _session, connection, _accepted = registry_db

    def check(plan):
        metadata = tuple(contribution.metadata for contribution in plan.contributions)
        policies = tuple(ref.tool_policy for ref in plan.tool_set_refs)
        before_values = canonical_json_bytes([metadata, policies])
        before_sql = tuple(connection.iterdump())
        with pytest.raises(ValueError, match="plan-privacy-required") as caught:
            validate_draft_privacy(plan)
        assert canonical_json_bytes([metadata, policies]) == before_values
        assert all(
            source.metadata is original
            for source, original in zip(plan.contributions, metadata, strict=True)
        )
        assert all(
            ref.tool_policy is original
            for ref, original in zip(plan.tool_set_refs, policies, strict=True)
        )
        assert tuple(connection.iterdump()) == before_sql
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert private_value not in "".join(traceback.format_exception(caught.value))

    return check


@pytest.mark.parametrize("protection", ["public", "protected", "redacted"])
@pytest.mark.parametrize("key", ["api_key", "aPi_KeY"])
def test_metadata_credentials_rejected_independent_of_protection(
    metadata_plan, private_value, protection, key, assert_rejected_without_mutation
):
    plan = metadata_plan({key: private_value}, protection=protection)
    assert_rejected_without_mutation(plan)


@pytest.mark.parametrize("surface", ["metadata", "policy"])
@pytest.mark.parametrize(
    "header",
    [
        "Proxy-Authorization",
        "pRoXy-AuThOrIzAtIoN",
        "X-API-Key",
        "x-ApI-kEy",
        "api-key",
        "aPi-KeY",
        "Cookie",
        "cOoKiE",
        "Set-Cookie",
        "sEt-CoOkIe",
    ],
)
def test_http_credentials_in_nested_headers_are_rejected(
    metadata_plan,
    policy_plan,
    private_value,
    surface,
    header,
    assert_rejected_without_mutation,
):
    value = {"providers": [{"request": {"HeAdErS": {header: private_value}}}]}
    plan = (
        metadata_plan(value, protection="protected")
        if surface == "metadata"
        else policy_plan(value)
    )
    assert_rejected_without_mutation(plan)


@pytest.mark.parametrize(
    "header", ["Proxy-Authorization", "X-API-Key", "api-key", "Cookie", "Set-Cookie"]
)
def test_http_credential_markers_preserve_policy_and_hash(
    policy_plan, credential_marker, header, registry_db, private_value
):
    _saver, session, connection, _accepted = registry_db
    policy = {"headers": {header: credential_marker, "Accept": "application/json"}}
    plan = policy_plan(policy)
    before = canonical_json_bytes(plan.to_dict())
    assert validate_draft_privacy(plan) is None
    assert canonical_json_bytes(plan.to_dict()) == before
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        registered = create_registration(connection, plan)
    restored = read_registration(connection, session, registered.draft.plan_id)
    assert restored.draft.tool_set_refs[0].tool_policy == policy
    assert private_value not in "\n".join(connection.iterdump())


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "password",
        "credential",
        "credentials",
    ],
)
def test_existing_domain_credential_rules_remain_authoritative(
    metadata_plan, private_value, key, assert_rejected_without_mutation
):
    plan = metadata_plan({"nested": [{key: private_value}]}, protection="protected")
    assert_rejected_without_mutation(plan)


@pytest.mark.parametrize("surface", ["metadata", "policy"])
@pytest.mark.parametrize("key", ["Authorization", "aUtHoRiZaTiOn"])
@pytest.mark.parametrize("nesting", ["direct", "headers", "array", "tuple"])
def test_authorization_rejected_recursively_without_case_bypass(
    metadata_plan,
    policy_plan,
    private_value,
    surface,
    key,
    nesting,
    assert_rejected_without_mutation,
):
    credential = {key: "Bearer " + private_value}
    value = {
        "direct": credential,
        "headers": {"HeAdErS": credential},
        "array": {"providers": [{"request": {"HEADERS": credential}}]},
        "tuple": {"providers": ({"request": {"headers": credential}},)},
    }[nesting]
    plan = (
        metadata_plan(value, protection="protected")
        if surface == "metadata"
        else policy_plan(value)
    )
    assert_rejected_without_mutation(plan)


@pytest.mark.parametrize("surface", ["metadata", "policy"])
@pytest.mark.parametrize(
    "damage",
    [
        "missing_class",
        "missing_length",
        "missing_digest",
        "boolean_length",
        "bad_digest",
        "extra_body",
    ],
)
def test_partial_or_body_carrying_markers_are_not_safe_provenance(
    metadata_plan,
    policy_plan,
    credential_marker,
    private_value,
    surface,
    damage,
    assert_rejected_without_mutation,
):
    marker = dict(credential_marker)
    if damage.startswith("missing_"):
        marker.pop(
            {
                "missing_class": "redaction_class",
                "missing_length": "content_length",
                "missing_digest": "redacted_stable_digest",
            }[damage]
        )
    elif damage == "boolean_length":
        marker["content_length"] = True
    elif damage == "bad_digest":
        marker["redacted_stable_digest"] = "sha256:jcs:v1:" + "a" * 64
    else:
        marker["body"] = private_value
    plan = (
        metadata_plan({"api_key": marker}, protection="protected")
        if surface == "metadata"
        else policy_plan({"headers": {"Authorization": marker}})
    )
    assert_rejected_without_mutation(plan)


@pytest.mark.parametrize("surface", ["metadata", "policy"])
def test_complete_markers_are_persisted_exactly_without_plaintext(
    metadata_plan, policy_plan, credential_marker, private_value, surface, registry_db
):
    _saver, session, connection, _accepted = registry_db
    value = {
        "nested": [
            {
                "api_key": credential_marker,
                "headers": {"Authorization": credential_marker},
            }
        ]
    }
    plan = (
        metadata_plan(value, protection="protected")
        if surface == "metadata"
        else policy_plan(value)
    )
    before = canonical_json_bytes(plan.to_dict())
    before_hash = plan.plan_hash()
    assert validate_draft_privacy(plan) is None
    assert canonical_json_bytes(plan.to_dict()) == before
    assert plan.plan_hash() == before_hash
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        registered = create_registration(connection, plan)
    recovered = read_registration(connection, session, plan.plan_id)
    assert recovered == registered
    assert validate_draft_privacy(recovered.draft) is None
    restored = (
        recovered.draft.contributions[0].metadata
        if surface == "metadata"
        else recovered.draft.tool_set_refs[0].tool_policy
    )
    assert restored["nested"] == value["nested"]
    assert private_value not in "\n".join(connection.iterdump())


@pytest.mark.parametrize("surface", ["metadata", "policy"])
def test_registry_write_rejects_credentials_before_publication(
    metadata_plan, policy_plan, private_value, surface, registry_db
):
    saver, session, connection, _accepted = registry_db
    plan = (
        metadata_plan({"api_key": private_value}, protection="protected")
        if surface == "metadata"
        else policy_plan({"headers": {"Authorization": "Bearer " + private_value}})
    )
    before_sql = tuple(connection.iterdump())
    before_rollout = (saver._storage.root(session) / "rollout.jsonl").read_bytes()
    with pytest.raises(ValueError, match="plan-privacy-required"), connection:
        connection.execute("BEGIN IMMEDIATE")
        create_registration(connection, plan)
    assert tuple(connection.iterdump()) == before_sql
    assert (
        saver._storage.root(session) / "rollout.jsonl"
    ).read_bytes() == before_rollout
    assert private_value not in "\n".join(connection.iterdump())


@pytest.mark.parametrize("surface", ["metadata", "policy"])
@pytest.mark.parametrize("header", ["draft_json", "creation_json"])
def test_recovery_refuses_credentials_in_current_and_initial_manifests(
    draft, policy_plan, private_value, surface, header, registry_db
):
    _saver, session, connection, _accepted = registry_db
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        registered = create_registration(connection, draft)
    raw = json.loads(canonical_json_bytes(registered.draft.to_dict()))
    if surface == "metadata":
        raw["contributions"][0]["metadata"]["api_key"] = private_value
    else:
        # 保持真实 domain 工具 manifest/hash 一致，只注入不允许持久化的认证配置。
        changed = policy_plan({"headers": {"aUtHoRiZaTiOn": "Bearer " + private_value}})
        raw = replace(registered.draft, tool_set_refs=changed.tool_set_refs).to_dict()
    with connection:
        if header == "draft_json":
            connection.execute(
                "UPDATE context_plans SET draft_json=?, draft_hash=? WHERE session_id=? AND plan_id=?",
                (
                    canonical_json_bytes(raw).decode(),
                    sha256_jcs(raw),
                    session,
                    draft.plan_id,
                ),
            )
        else:
            connection.execute(
                "UPDATE context_plans SET creation_json=? WHERE session_id=? AND plan_id=?",
                (canonical_json_bytes(raw).decode(), session, draft.plan_id),
            )
    before = tuple(connection.iterdump())
    with pytest.raises(ValueError, match="plan-privacy-required") as caught:
        read_registration(connection, session, draft.plan_id)
    assert tuple(connection.iterdump()) == before
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert private_value not in "".join(traceback.format_exception(caught.value))


def test_safe_provenance_and_noncredential_policy_remain_unchanged(
    draft, metadata_plan, registry_db
):
    _saver, session, connection, _accepted = registry_db
    provenance = {
        "source_ordinal": 0,
        "source_ref": "source-contribution",
        "source_revision": "revision-1",
        "origin": {"session_id": session, "plan_id": "plan"},
        "source_overlay_epoch": 0,
        "token_count": 123,
        "headers": {"Accept": "application/json", "X-Request-ID": "provenance-request"},
        "created_at": "2026-09-08T00:00:00+00:00",
        "tags": ["Authorization", "api_key"],
    }
    plan = metadata_plan(provenance, protection="public")
    before = canonical_json_bytes(plan.to_dict())
    assert validate_draft_privacy(plan) is None
    assert canonical_json_bytes(plan.to_dict()) == before
    # 写前调用允许实际正文仍由 registry owner 去除；privacy 不偷偷修改 source。
    body = draft.contributions[0].body
    assert validate_draft_privacy(draft) is None
    assert draft.contributions[0].body is body
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        registered = create_registration(connection, plan)
    assert registered.draft.contributions[0].metadata == provenance


def test_tool_schema_property_names_are_not_misclassified_as_credentials(draft):
    original = draft.tool_set_refs[0]
    tool = ToolSetRef.from_tool_snapshot(
        snapshot_id=original.ref_id,
        session_id=draft.session_id,
        plan_id=draft.plan_id,
        source_revision=original.source_revision,
        tools=(
            {
                "name": "inspect_configuration",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "api_key": {"type": "string"},
                        "password": {"type": "string"},
                        "Authorization": {"type": "string"},
                        "Proxy-Authorization": {"type": "string"},
                        "X-API-Key": {"type": "string"},
                        "api-key": {"type": "string"},
                        "Cookie": {"type": "string"},
                        "Set-Cookie": {"type": "string"},
                    },
                },
            },
        ),
        tool_policy={"parallel_tool_calls": False, "token_budget": 123},
    )
    plan = replace(draft, tool_set_refs=(tool,))
    before = canonical_json_bytes(plan.to_dict())
    assert validate_draft_privacy(plan) is None
    assert canonical_json_bytes(plan.to_dict()) == before


def test_non_plan_input_is_rejected():
    with pytest.raises(TypeError, match="ContextRequestPlan"):
        validate_draft_privacy({"contributions": []})
