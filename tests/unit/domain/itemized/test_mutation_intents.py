"""A1 合同单测：ContextMutationIntent 四分支与 typed source_ordinal 链路。"""

from __future__ import annotations

import pytest

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import PayloadKind, SelectionKind, SemanticKind
from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash
from app.domain.itemized.mutation_intents import (
    AppendCanonicalItemIntent,
    ApplySourceLifecycleDecision,
    MutationIntentOwner,
    RebuildContextEpochIntent,
    SwitchToolSetIntent,
    intent_kind,
)
from app.domain.itemized.plan_hash import context_plan_hash
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.domain.itemized.selection import ContextSelectionEntry
from app.domain.itemized.serde.plan import unsealed_context_plan_from_dict


def _owner() -> MutationIntentOwner:
    return MutationIntentOwner(session_id="ses_" + "a" * 32, thread_id="thr_" + "b" * 32)


class TestAppendCanonicalItemIntent:
    def test_idempotency_key_is_stable_and_identity_derived(self) -> None:
        intent = AppendCanonicalItemIntent(
            owner=_owner(),
            item_id="item-1",
            item_kind="user_message",
            origin_turn_id="turn-1",
        )
        same = AppendCanonicalItemIntent(
            owner=_owner(),
            item_id="item-1",
            item_kind="user_message",
            origin_turn_id="turn-1",
        )
        other = AppendCanonicalItemIntent(
            owner=_owner(),
            item_id="item-2",
            item_kind="user_message",
            origin_turn_id="turn-1",
        )
        assert intent.idempotency_key == same.idempotency_key
        assert intent.idempotency_key != other.idempotency_key

    def test_tool_call_requires_paired_tool_call_id(self) -> None:
        with pytest.raises(ValueError, match="tool_call_id"):
            AppendCanonicalItemIntent(
                owner=_owner(),
                item_id="item-3",
                item_kind="tool_call",
                origin_turn_id="turn-1",
            )

    def test_non_tool_kind_rejects_tool_call_id(self) -> None:
        with pytest.raises(ValueError, match="tool_call_id"):
            AppendCanonicalItemIntent(
                owner=_owner(),
                item_id="item-4",
                item_kind="user_message",
                origin_turn_id="turn-1",
                tool_call_id="call-1",
            )

    def test_failure_outcome_is_reject_atomic(self) -> None:
        intent = AppendCanonicalItemIntent(
            owner=_owner(),
            item_id="item-5",
            item_kind="tool_result",
            origin_turn_id="turn-1",
            tool_call_id="call-1",
        )
        assert intent.failure_outcome == "reject_atomic"


class TestApplySourceLifecycleDecision:
    def test_delta_with_distinct_from_revision_is_valid(self) -> None:
        decision = ApplySourceLifecycleDecision(
            owner=_owner(),
            source_id="src-1",
            source_kind="skill",
            name="alpha",
            decision_kind="delta",
            revision="rev-2",
            from_revision="rev-1",
            content="delta",
            item_id="item-src-delta",
        )
        assert decision.from_revision == "rev-1"
        assert decision.revision == "rev-2"

    def test_delta_same_revision_rejected(self) -> None:
        with pytest.raises(ValueError, match="from_revision"):
            ApplySourceLifecycleDecision(
                owner=_owner(),
                source_id="src-1",
                source_kind="skill",
                name="alpha",
                decision_kind="delta",
                revision="rev-2",
                from_revision="rev-2",
                content="delta",
                item_id="item-src-delta",
            )

    def test_base_rejects_from_revision(self) -> None:
        with pytest.raises(ValueError, match="from_revision"):
            ApplySourceLifecycleDecision(
                owner=_owner(),
                source_id="src-1",
                source_kind="skill",
                name="alpha",
                decision_kind="base",
                revision="rev-1",
                from_revision="rev-0",
                content="base",
                item_id="item-src-base",
            )

    def test_untrack_carries_no_revision(self) -> None:
        with pytest.raises(ValueError, match="untrack"):
            ApplySourceLifecycleDecision(
                owner=_owner(),
                source_id="src-1",
                source_kind="skill",
                name="alpha",
                decision_kind="untrack",
                revision="rev-1",
            )

    def test_idempotency_key_covers_revision(self) -> None:
        base = ApplySourceLifecycleDecision(
            owner=_owner(),
            source_id="src-1",
            source_kind="skill",
            name="alpha",
            decision_kind="base",
            revision="rev-1",
            content="base",
            item_id="item-src-base",
        )
        advanced = ApplySourceLifecycleDecision(
            owner=_owner(),
            source_id="src-1",
            source_kind="skill",
            name="alpha",
            decision_kind="delta",
            revision="rev-2",
            from_revision="rev-1",
            content="delta",
            item_id="item-src-delta",
        )
        assert base.idempotency_key != advanced.idempotency_key
        assert base.failure_outcome == "keep_pending"


class TestSwitchToolSetIntent:
    def test_idempotency_key_and_outcome(self) -> None:
        intent = SwitchToolSetIntent(
            owner=_owner(),
            desired_revision="ts-2",
            tool_set_snapshot_id="snapshot-2",
        )
        assert "ts-2" in intent.idempotency_key
        assert intent.failure_outcome == "keep_applied_toolset"

    def test_empty_desired_revision_rejected(self) -> None:
        with pytest.raises(ValueError, match="desired_revision"):
            SwitchToolSetIntent(
                owner=_owner(),
                desired_revision="",
                tool_set_snapshot_id="snapshot-2",
            )


class TestRebuildContextEpochIntent:
    def test_only_compaction_and_rewind_allowed(self) -> None:
        with pytest.raises(ValueError, match="epoch_reason"):
            RebuildContextEpochIntent(
                owner=_owner(),
                epoch_reason="toolset_changed",  # type: ignore[arg-type]
                view_revision=1,
                control_revision=1,
            )

    def test_idempotency_key_and_outcome(self) -> None:
        intent = RebuildContextEpochIntent(
            owner=_owner(),
            epoch_reason="rewind",
            view_revision=3,
            control_revision=7,
        )
        assert intent.view_revision == 3
        assert intent.idempotency_key.endswith("rewind:3:7")
        assert intent.failure_outcome == "keep_old_view"

    def test_negative_revision_rejected(self) -> None:
        with pytest.raises(ValueError, match="view_revision"):
            RebuildContextEpochIntent(
                owner=_owner(),
                epoch_reason="compaction",
                view_revision=-1,
                control_revision=0,
            )


class TestIntentDispatch:
    def test_intent_kind_discriminates_all_branches(self) -> None:
        intents = [
            AppendCanonicalItemIntent(
                owner=_owner(), item_id="i", item_kind="user_message",
                origin_turn_id="t",
            ),
            ApplySourceLifecycleDecision(
                owner=_owner(), source_id="s", source_kind="skill",
                name="alpha", decision_kind="base", revision="r",
                content="base", item_id="item-src",
            ),
            SwitchToolSetIntent(
                owner=_owner(), desired_revision="ts",
                tool_set_snapshot_id="snap",
            ),
            RebuildContextEpochIntent(
                owner=_owner(), epoch_reason="compaction",
                view_revision=1, control_revision=1,
            ),
        ]
        kinds = {intent_kind(intent) for intent in intents}
        assert kinds == {
            "append_canonical_item",
            "apply_source_lifecycle_decision",
            "switch_tool_set",
            "rebuild_context_epoch",
        }


class TestTypedSourceOrdinal:
    def _ref(self, plan: ContextRequestPlan, contribution_id: str) -> ContextRef:
        body: dict[str, object] = {"text": "a"}
        return ContextRef.request_only_ref(
            contribution_id,
            session_id=plan.session_id, thread_id="thread-1",
            plan_id=plan.plan_id,
            source_revision="rev-1",
            semantic_kind=SemanticKind.EXTENSION.value,
            payload_kind=PayloadKind.STRUCTURED_CONTENT.value,
            content_length=len(canonical_json_bytes(body)),
            content_hash_value=contribution_content_hash("prompt", body),
            source_ref=contribution_id,
        )

    def _contribution(self, source_ordinal: int | None, *, metadata: dict[str, object] | None = None) -> ContextContribution:
        body: dict[str, object] = {"text": "a"}
        return ContextContribution(
            contribution_id="contrib-1",
            source_kind="skill",
            source_revision="rev-1",
            content_hash=contribution_content_hash("prompt", body),
            request_only=True,
            metadata=metadata or {},
            contribution_kind="prompt",
            body=body,
            source_ordinal=source_ordinal,
        )

    def _plan(self, contribution: ContextContribution) -> ContextRequestPlan:
        plan = ContextRequestPlan(
            session_id="ses_" + "a" * 32,
            plan_id="plan-1",
            refs=(),
            contributions=(contribution,),
        )
        return ContextRequestPlan(
            session_id=plan.session_id,
            plan_id=plan.plan_id,
            refs=(self._ref(plan, contribution.contribution_id),),
            contributions=plan.contributions,
        )

    def test_unsealed_plan_requires_registry_assigned_ordinal(self) -> None:
        with pytest.raises(Exception, match="source_ordinal"):
            self._plan(self._contribution(None))

    def test_metadata_ordinal_key_has_no_interpretation(self) -> None:
        """metadata 同名键不再补号：typed 缺失时直接 schema error。"""
        with pytest.raises(Exception, match="source_ordinal"):
            self._plan(
                self._contribution(None, metadata={"source_ordinal": 4}),
            )

    def test_hash_preimage_unchanged_by_metadata_key(self) -> None:
        """typed 值相同且 metadata 带同名键时 hash 不变（键为 inert 数据）。"""
        plan_typed = self._plan(self._contribution(3))
        plan_legacy_in_metadata = self._plan(
            self._contribution(3, metadata={"source_ordinal": 3}),
        )
        assert context_plan_hash(plan_typed) == context_plan_hash(
            plan_legacy_in_metadata,
        )

    def test_draft_manifest_round_trip_preserves_typed_ordinal(self) -> None:
        plan = self._plan(self._contribution(2))
        manifest = plan.to_dict()
        restored = unsealed_context_plan_from_dict(manifest)
        assert restored.contributions[0].source_ordinal == 2
        assert restored.plan_hash() == plan.plan_hash()

    def test_contribution_rejects_bool_ordinal(self) -> None:
        with pytest.raises(Exception, match="source_ordinal"):
            self._contribution(True)  # type: ignore[arg-type]

    def test_sealed_plan_clears_source_ordinal(self) -> None:
        plan = self._plan(self._contribution(2))
        ref = self._ref(plan, "contrib-1")
        entry = ContextSelectionEntry(
            assembly_id="assembly-1",
            plan_ordinal=0,
            ref=ref,
            selection_kind=SelectionKind.REQUEST_ONLY,
            source_revision=ref.source_revision,
            content_length=ref.content_length,
            content_hash=ref.content_hash,
            visibility=ref.visibility,
            protection=ref.protection,
            availability=ref.availability,
            detail_ref=DetailRef(plan.session_id, "assembly-1", "detail-1"),
            contribution_id="contrib-1",
            contribution_ordinal=0,
        )
        sealed = plan.seal_for_assembly("assembly-1", selection=(entry,))
        assert sealed.plan_state == "sealed"
        assert all(item.source_ordinal is None for item in sealed.contributions)
