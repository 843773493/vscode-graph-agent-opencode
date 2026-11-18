"""真实 Saver 封存、幂等重读和冷读取共享模型来源回放规则。"""

from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.agents.providers.wire_finalize import (
    ResponsesWireProfile,
    finalize_provider_wire,
)
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.model_request_context import SummaryCandidateScope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.model_source import ModelSource
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)


@pytest.mark.parametrize("source_state", ["same", "other-model", "missing"])
@pytest.mark.parametrize("summary", [False, True])
def test_private_reasoning_seal_retry_and_cold_projection(source_seal_case, source_state: str, summary: bool) -> None:
    case = source_seal_case
    saver, owner = case.saver, case.owner
    source = ModelSource(
        gateway_id="gateway-a", workspace_id="workspace-a", provider_id="service",
        model_id="main", protocol="responses", endpoint="https://example.com/v1",
        model="upstream-model", authentication="env:MODEL_KEY",
    )
    profile = ResponsesWireProfile(
        "service", "openai", "upstream-model", include=("reasoning.encrypted_content",),
        reasoning_items_summary_replay=True, reasoning_items_encrypted_replay=True,
        model_source=replace(source, model_id="fallback") if source_state == "other-model" else source,
    )
    admission = saver.execution_admission(
        owner.session_id, thread_id=owner.thread_id, execution_id=case.accepted["initial_execution_id"],
    )
    manager = case.manager()
    with (
        session_thread_owner_scope(MutationIntentOwner(owner.session_id, owner.thread_id), langgraph_thread_id=owner.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=owner.thread_id),
    ):
        codec = saver._storage.storage._codec()
        root, = saver._storage.storage.read_items(owner.session_id, owner.thread_id)
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "source-checkpoint"
        checkpoint["channel_values"] = {"messages": [
            codec.from_dict(codec.project_message((root,))),
            AIMessage(id="response-a", content=[
                {"type": "reasoning_items", "reasoning_items": [{
                    "type": "reasoning", "encrypted_content": "private-sealed-content",
                    "summary": [{"type": "summary_text", "text": "保留摘要"}],
                }]},
                {"type": "text", "text": "保留回答"},
            ], response_metadata={"model_source": source.to_dict()} if source_state != "missing" else {"source_provider": "service"}),
        ]}
        checkpoint["channel_versions"] = {"messages": "1"}
        checkpoint["updated_channels"] = ["messages"]
        saver.put(build_checkpoint_config(owner.thread_id), checkpoint, {"source": "unit"}, {"messages": "1"})
        arguments = {
            "turn_id": admission.turn_id, "execution_id": admission.execution_id,
            "plan_creation_idempotency_key": "model-source:create", "seal_idempotency_key": "model-source:seal",
            "context_source_manager": manager,
            "tool_set_supplier": lambda: ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=()),
            "provider_version": profile.identity(), "wire_profile": profile,
            "wire_params": {"model": "upstream-model", "stream": True}, "target_format": "responses",
            **case.activation_contract(saver, plan_creation_idempotency_key="model-source:create"),
        }
        if summary:
            messages = checkpoint["channel_values"]["messages"]
            arguments["request_messages"] = messages
            arguments["summary_candidate"] = SummaryCandidateScope(
                0, "initial", False, True, canonical_messages=tuple(messages),
            )
        prepared = saver.prepare_context_for_provider(owner.session_id, owner.thread_id, **arguments)
        retry = saver.prepare_context_for_provider(owner.session_id, owner.thread_id, **arguments)
        assert retry["snapshot"] == prepared["snapshot"]
        assert retry["native_projection"] == prepared["native_projection"]
    body = prepared["snapshot"].request_hash_preimage
    assert ("private-sealed-content" in repr(body)) is (source_state == "same")
    assert "保留摘要" in repr(body) and "保留回答" in repr(body)
    if source_state != "same":
        reason = "missing_model_source" if source_state == "missing" else "different_model_source"
        assert any(reason in loss for loss in prepared["losses"])
    with RolloutCheckpointSaver(saver._storage.storage.sessions_dir) as cold:
        snapshot = cold.get_context_assembly(owner.session_id, owner.thread_id, assembly_id=prepared["assembly_id"])
        projection = cold.project_context_plan_for_dispatch(
            owner.session_id, owner.thread_id, snapshot.as_sealed_plan(), target_format="responses", wire_profile=profile,
        )
        assert normalize_wire_request(finalize_provider_wire(
            messages=projection.messages, tools=projection.tools, native_request=projection.native["request"],
            profile=profile, params=arguments["wire_params"],
        )) == body
        assert snapshot.request_hash == prepared["snapshot"].request_hash
