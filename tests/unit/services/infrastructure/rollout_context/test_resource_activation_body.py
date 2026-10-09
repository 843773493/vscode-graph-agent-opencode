"""ResourceActivation payload 的 protected detail body owner 合同。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationContractError,
    ResourceActivationSnapshotRef,
    ResourceMemberBinding,
    ResourceProvenanceRef,
    SourceLineageRef,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.rollout_context.activation.body import (
    BODY_UNAVAILABLE_CODE,
    PAYLOAD_DETAIL_KIND,
    PAYLOAD_RETENTION_CLASS,
    ResourceActivationBodyStore,
)
from app.services.infrastructure.rollout_context.activation.common import (
    HASH_MISMATCH_CODE,
    ResourceActivationStoreError,
)
from app.services.infrastructure.rollout_context.activation.lineage import (
    ActivationLineageBodyStore,
)
from app.services.infrastructure.rollout_context.activation.store import (
    ResourceActivationStore,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_record_from_mapping,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from app.services.orchestration.resource_activation import (
    ResourceActivationCoordinator,
    ResourceActivationPolicySnapshot,
)
from app.services.orchestration.resource_activation.contracts import (
    resource_activation_policy_from_config,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.support.workspaces import prepare_default_test_workspace

SESSION_ID = "ses_019c0fd1773c7b198a60f53aeeee17eb"
SNAPSHOT_ID = "turn:body-owner-test"
POLICY = ResourceActivationPolicySnapshot.from_dict({"default_boundary": "turn", "overrides": {}})
PROTECTED_KEY = bytes(range(32))


@pytest.fixture(scope="module")
def body_workspace(request: pytest.FixtureRequest) -> Path:
    run_context = TestRunContext.from_test_file(Path(request.node.path))
    return prepare_default_test_workspace(
        workspace_root=run_context.workspace_root,
        template_root=run_context.project_root
        / "tests/fixtures/workspaces/custom_tool_test_workspace",
    )


@pytest.fixture
def activation_body_store(body_workspace: Path):
    sessions_root = body_workspace / ".boxteam" / "sessions"
    bundle = seed_catalog_session_bundle(sessions_root, SESSION_ID)
    storage = RolloutContextStore(sessions_root, message_codec=LangChainMessageCodec())
    storage.storage.initialize(SESSION_ID, validate_jsonl_items=False)
    detail_store = ContextPlanDetailStore(sessions_root)
    return (
        storage,
        ResourceActivationBodyStore(storage, detail_store),
        bundle.main_thread_id,
    )


def _snapshot_with_detail(detail_ref: DetailRef, payload: object, thread_id: str):
    content_hash = sha256_jcs(payload)
    content_length = len(canonical_json_bytes(payload))
    lineage = SourceLineageRef(
        lineage_id="lineage:skill:body-owner",
        derivation_version="resource-derivation:v1",
        sources=(("source-1", "source-revision-1"),),
    )
    binding = ResourceProvenanceRef(
        resource_id="skill:body-owner",
        display_uri="boxteam://workspace/test/resources/skills/body-owner/SKILL.md",
        resource_kind="skills",
        owner_scope="session",
        facet="activation",
        revision="resource-revision-1",
        availability="available",
        content_length=content_length,
        content_hash=content_hash,
        source_lineage_ref=lineage,
        source_lineage_digest=lineage.digest,
        activation_ordinal=0,
        effective_boundary="turn",
        captured_registry_generation=1,
        detail_ref=detail_ref,
    )
    snapshot = ResourceActivationSnapshotRef(
        activation_snapshot_id=SNAPSHOT_ID,
        snapshot_kind="execution",
        activation_policy_revision=POLICY.revision,
        activation_policy_hash=POLICY.hash,
        activation_policy=POLICY,
        registry_generation=1,
        owner_session_id=SESSION_ID,
        owner_thread_id=thread_id,
        execution_id="execution-body-owner",
        turn_id="turn-1",
        captured_at="2026-10-04T00:00:00+00:00",
        bindings=(binding,),
    )
    return snapshot, binding


def test_payload_is_required_protected_and_uses_canonical_hash_and_length(
    activation_body_store,
) -> None:
    storage, body_store, thread_id = activation_body_store
    payload = {"name": "demo", "content": ["first", "second"]}

    detail_ref = body_store.write_resource_body(
        owner_session_id=SESSION_ID,
        owner_thread_id=thread_id,
        activation_snapshot_id=SNAPSHOT_ID,
        checkpoint_ns="",
        payload=payload,
        resource_id="skill:body-owner",
        activation_ordinal=0,
    )

    record = detail_record_from_mapping(
        storage.assemblies.get_context_plan_detail(SESSION_ID, thread_id, detail_ref=detail_ref)
    )
    assert record.detail_kind == PAYLOAD_DETAIL_KIND
    assert record.retention_class == PAYLOAD_RETENTION_CLASS
    assert record.required is True
    assert record.sensitive is True
    assert record.protection == "protected"
    assert record.source_revision == sha256_jcs(payload)
    assert record.length == len(canonical_json_bytes(payload))
    record.detail_ref.require_owner(SESSION_ID, thread_id, SNAPSHOT_ID)

    manifest = detail_record_from_mapping(
        storage.assemblies.get_context_plan_detail(
            SESSION_ID,
            thread_id,
            detail_ref=record.detail_ref,
            checkpoint_ns="",
        )
    )
    snapshot, binding = _snapshot_with_detail(record.detail_ref, payload, thread_id)
    assert manifest == record
    assert body_store.read_payload(snapshot=snapshot, binding=binding) == payload


def test_payload_read_rejects_resource_ordinal_and_hash_conflicts(
    activation_body_store,
) -> None:
    storage, body_store, thread_id = activation_body_store
    payload = {"name": "demo"}
    detail_ref = body_store.write_resource_body(
        owner_session_id=SESSION_ID,
        owner_thread_id=thread_id,
        activation_snapshot_id=SNAPSHOT_ID,
        checkpoint_ns="",
        payload=payload,
        resource_id="skill:body-owner",
        activation_ordinal=0,
    )
    record = detail_record_from_mapping(
        storage.assemblies.get_context_plan_detail(SESSION_ID, thread_id, detail_ref=detail_ref)
    )
    snapshot, binding = _snapshot_with_detail(record.detail_ref, payload, thread_id)

    for changed in (
        replace(binding, resource_id="skill:other"),
        replace(binding, activation_ordinal=1),
        replace(binding, content_hash=sha256_jcs({"name": "changed"})),
        replace(binding, content_length=binding.content_length + 1),
    ):
        with pytest.raises(ResourceActivationStoreError) as error:
            body_store.read_payload(snapshot=snapshot, binding=changed)
        assert error.value.code == HASH_MISMATCH_CODE


def test_payload_read_rejects_wrong_owner_and_retention_tombstone(
    activation_body_store,
) -> None:
    storage, body_store, thread_id = activation_body_store
    payload = {"name": "demo"}
    detail_ref = body_store.write_resource_body(
        owner_session_id=SESSION_ID,
        owner_thread_id=thread_id,
        activation_snapshot_id=SNAPSHOT_ID,
        checkpoint_ns="",
        payload=payload,
        resource_id="skill:body-owner",
        activation_ordinal=0,
    )
    record = detail_record_from_mapping(
        storage.assemblies.get_context_plan_detail(SESSION_ID, thread_id, detail_ref=detail_ref)
    )
    snapshot, binding = _snapshot_with_detail(record.detail_ref, payload, thread_id)
    wrong_owner_binding = replace(
        binding,
        detail_ref=DetailRef(
            "ses_019c0fd1773c7b198a60f53aeeee17ef",
            thread_id,
            SNAPSHOT_ID,
            record.detail_ref.detail_id,
        ),
    )
    # 非法 owner 在领域构造边界就被拒绝，不能先造一个不合法 snapshot。
    with pytest.raises(ResourceActivationContractError) as owner_error:
        replace(snapshot, bindings=(wrong_owner_binding,))
    assert owner_error.value.code == "resource-activation-schema-invalid"
    with pytest.raises(ResourceActivationStoreError) as binding_error:
        body_store.read_payload(snapshot=snapshot, binding=wrong_owner_binding)
    assert binding_error.value.code == HASH_MISMATCH_CODE

    storage.assemblies.mark_context_plan_details_unavailable(
        SESSION_ID,
        thread_id,
        detail_refs=(record.detail_ref,),
    )
    with pytest.raises(ResourceActivationStoreError) as error:
        body_store.read_payload(snapshot=snapshot, binding=binding)
    assert error.value.code == BODY_UNAVAILABLE_CODE


def test_exact_sensitive_payload_persists_without_key_dependencies(body_workspace: Path) -> None:
    sessions_root = body_workspace / ".boxteam" / "sessions"
    bundle = seed_catalog_session_bundle(sessions_root, SESSION_ID)
    storage = RolloutContextStore(sessions_root, message_codec=LangChainMessageCodec())
    storage.storage.initialize(SESSION_ID, validate_jsonl_items=False)
    body_store = ResourceActivationBodyStore(storage, ContextPlanDetailStore(sessions_root))
    payload = {"secret": "exact activation body"}
    ref = body_store.write_resource_body(
        owner_session_id=SESSION_ID, owner_thread_id=bundle.main_thread_id,
        activation_snapshot_id=SNAPSHOT_ID, checkpoint_ns="",
        payload=payload, resource_id="skill:body-owner", activation_ordinal=0,
    )
    record = detail_record_from_mapping(storage.assemblies.get_context_plan_detail(
        SESSION_ID, bundle.main_thread_id, detail_ref=ref,
    ))
    snapshot, binding = _snapshot_with_detail(record.detail_ref, payload, bundle.main_thread_id)
    restarted = ResourceActivationBodyStore(storage, ContextPlanDetailStore(sessions_root))
    assert restarted.read_payload(snapshot=snapshot, binding=binding) == payload


@pytest.mark.asyncio
async def test_coordinator_freezes_through_protected_body_port(
    activation_body_store,
) -> None:
    _storage, body_store, thread_id = activation_body_store
    payload = {"content": "published immutable skill"}
    registry = ResourceRegistry()
    registry.publish(
        ResourceSnapshot(
            resource_id="skill:body-owner",
            resource_kind="skills",
            facet="activation",
            display_uri="boxteam://workspace/test/resources/skills/body-owner/SKILL.md",
            revision=sha256_jcs(payload),
            payload=payload,
            source_lineage=(("source-1", "source-revision-1"),),
            generation=1,
        )
    )
    saved: list[ResourceActivationSnapshotRef] = []

    class SnapshotSaver:
        def frozen_resource_activation_for_admission(self, admission, *, checkpoint_ns=""):
            return None

        def find_resource_activation_snapshot(
            self, session_id, *, thread_id, activation_snapshot_id, checkpoint_ns=""
        ):
            return None

        async def save_resource_activation_snapshot(
            self, snapshot: ResourceActivationSnapshotRef
        ) -> None:
            saved.append(snapshot)

    coordinator = ResourceActivationCoordinator(
        registry=registry,
        saver=SnapshotSaver(),
        body_store=body_store,
    )
    policy = resource_activation_policy_from_config(
        {
            "context": {
                "resource_activation": {"default_boundary": "turn", "overrides": {}}
            }
        }
    )
    frozen = await coordinator.freeze_execution(
        admission=ExecutionAdmission(
            session_id=SESSION_ID, thread_id=thread_id,
            execution_id="execution-body-port", job_id="job-body-port",
            admission_idempotency_key="admission-body-port",
            execution_origin="user_turn", turn_id="turn-body-port",
        ),
        policy=policy,
    )
    assert saved == [frozen]
    assert frozen.bindings[0].content_hash == sha256_jcs(payload)
    assert (
        body_store.read_payload(snapshot=frozen, binding=frozen.bindings[0]) == payload
    )


def test_same_turn_resume_payload_reuse_verifies_complete_protected_members(
    activation_body_store,
) -> None:
    storage, body_store, thread_id = activation_body_store
    accepted = storage.executions.accept_turn(
        SESSION_ID, thread_id=thread_id, turn_id="turn-members",
        accepted_ingress_id="evt-members", acceptance_idempotency_key="accept-members",
        job_id="job-members", admission_idempotency_key="admit-members", payload="验证成员",
        root_item_id="item-members-user", initial_execution_id="execution-members-source",
    )
    member = ResourceMemberBinding(
        member_id="custom:demo", member_kind="custom", schema_hash="sha256:" + "a" * 64,
        server_id=None, revision="catalog-revision-1",
    )
    payload = {"members": [member.to_dict()], "guidance": "catalog guidance"}
    source_id = "execution:members-source"
    detail_ref = body_store.write_resource_body(
        owner_session_id=SESSION_ID, owner_thread_id=thread_id,
        activation_snapshot_id=source_id, resource_id="skill:body-owner",
        activation_ordinal=0, payload=payload, checkpoint_ns="",
    )
    base, binding = _snapshot_with_detail(detail_ref, payload, thread_id)
    binding = replace(binding, members=(member,))
    first = replace(
        base, activation_snapshot_id=source_id, execution_id=accepted["execution_id"],
        turn_id="turn-members", bindings=(binding,),
    )
    store = ResourceActivationStore(
        storage, ActivationLineageBodyStore(storage, body_store._detail_store),
    )
    store.save_snapshot(first)
    storage.executions.mark_execution_lost(
        SESSION_ID, thread_id=thread_id, turn_id=first.turn_id,
        execution_id=first.execution_id, reason="activation-test-resume",
    )
    resumed = storage.executions.resume_turn(
        SESSION_ID, thread_id=thread_id, job_id="job-members-resumed", turn_id=first.turn_id,
    )
    retry = replace(
        first, activation_snapshot_id="execution:members-resumed",
        execution_id=resumed["execution_id"],
    )
    store.save_snapshot(retry)
    assert retry.bindings == first.bindings
    assert body_store.read_payload(snapshot=retry, binding=binding) == payload

    for changed in (
        replace(member, revision="catalog-revision-2"),
        replace(member, schema_hash="sha256:" + "b" * 64),
    ):
        altered_binding = replace(binding, members=(changed,))
        altered = replace(retry, bindings=(altered_binding,))
        assert altered.bindings_hash == retry.bindings_hash
        assert altered.activation_provenance_hash == retry.activation_provenance_hash
        with pytest.raises(ResourceActivationStoreError) as mismatch:
            body_store.read_payload(snapshot=altered, binding=altered_binding)
        assert mismatch.value.code == HASH_MISMATCH_CODE
    assert store.read_snapshot(
        SESSION_ID, thread_id=thread_id, activation_snapshot_id=retry.activation_snapshot_id,
    ).to_dict() == retry.to_dict()
