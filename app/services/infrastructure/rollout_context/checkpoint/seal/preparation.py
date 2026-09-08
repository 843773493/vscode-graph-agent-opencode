"""最终 middleware 输入的显式注册；已封存请求重试不重读 active view。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields, replace

from langchain_core.messages import BaseMessage

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.request_plan import (
    ContextContribution,
    resolve_contribution_for_ref,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    require_key,
    runtime_draft,
)


def prepare_registered_context(
    owner,
    session_id: str,
    *,
    turn_id: str,
    plan_creation_idempotency_key: str,
    seal_idempotency_key: str,
    request_messages: Sequence[BaseMessage],
    prompt_contributions: Sequence[ContextContribution],
    tool_snapshot: Sequence[Mapping[str, object]],
    provider_version: str,
    target_format: str,
    checkpoint_ns: str,
) -> dict[str, object]:
    require_key(plan_creation_idempotency_key, field="plan_creation_idempotency_key")
    require_key(seal_idempotency_key, field="seal_idempotency_key")
    if not session_id or not turn_id or not provider_version:
        raise ValueError("provider context 缺少 session_id/turn_id/provider_version")
    if target_format not in {"chat_completions", "responses"}:
        raise ValueError(f"不支持的 provider context target_format: {target_format}")
    if not owner.supports_itemized_context(session_id, checkpoint_ns=checkpoint_ns):
        raise RuntimeError("provider dispatch 必须使用可用的 v2 itemized context")
    contributions = tuple(prompt_contributions)
    contribution_ids = set()
    for contribution in contributions:
        if not isinstance(contribution, ContextContribution):
            raise TypeError("prompt_contributions 只能包含 ContextContribution")
        replace(contribution)
        if contribution.contribution_id in contribution_ids:
            raise ValueError("provider context 重复 prompt contribution")
        contribution_ids.add(contribution.contribution_id)
    # 仅持久化指纹；敏感贡献使用既有 manifest token，正文不进入 plan registry。
    input_hash = sha256_jcs(
        {
            "schema": "provider-prepare-input:v1",
            "request_messages": [
                message.model_dump(mode="json") for message in request_messages
            ],
            "prompt_contributions": [
                {
                    field.name: getattr(contribution, field.name)
                    for field in fields(ContextContribution)
                    if field.name != "body"
                }
                for contribution in contributions
            ],
            "tool_snapshot": [dict(tool) for tool in tool_snapshot],
        }
    )
    plan_id = "plan-prepared:" + sha256_jcs(
        {"creation_key": plan_creation_idempotency_key}
    )
    registered = owner._find_runtime_context_plan(session_id, plan_id, checkpoint_ns)
    if registered is None or registered.plan_state == "unsealed":
        owner._storage.ensure_active_view_contains_turn_root(
            session_id, turn_id=turn_id, checkpoint_ns=checkpoint_ns
        )
        owner._storage.ensure_request_tool_result_items(
            session_id,
            turn_id=turn_id,
            messages=request_messages,
            checkpoint_ns=checkpoint_ns,
        )
        for contribution in contributions:
            owner.register_context_contribution(
                session_id,
                contribution,
                checkpoint_ns=checkpoint_ns,
                request_content=contribution.body,
            )
        full_plan = owner.compose_committed_context_plan(
            session_id,
            plan_id=plan_id,
            tool_snapshot=tool_snapshot,
            checkpoint_ns=checkpoint_ns,
        )
        refs = []
        selected_ids = set()
        for ref in full_plan.refs:
            contribution = resolve_contribution_for_ref(ref, full_plan.contributions)
            if (
                contribution is not None
                and contribution.contribution_kind
                not in {"overlay_base", "overlay_delta"}
                and contribution.contribution_id not in contribution_ids
            ):
                continue
            refs.append(ref)
            if contribution is not None:
                selected_ids.add(contribution.contribution_id)
        # 注册最终 filtered plan，而不是之后再删 ref/contribution 的中间计划。
        plan = replace(
            full_plan,
            refs=tuple(refs),
            contributions=tuple(
                item
                for item in full_plan.contributions
                if item.contribution_id in selected_ids
            ),
            plan_creation_idempotency_key=plan_creation_idempotency_key,
        )
        registered = owner.create_context_plan(
            session_id, plan, checkpoint_ns=checkpoint_ns
        )
    execution_id = owner._storage.execution_for_turn(
        session_id, turn_id=turn_id, checkpoint_ns=checkpoint_ns
    )
    snapshot = owner.seal_context_plan(
        session_id,
        runtime_draft(registered),
        seal_idempotency_key=seal_idempotency_key,
        turn_id=turn_id,
        execution_id=execution_id,
        provider_version=provider_version,
        target_format=target_format,
        request_input_hash=input_hash,
        checkpoint_ns=checkpoint_ns,
    )
    plan = snapshot.as_sealed_plan()
    messages, tools, losses = owner.project_context_plan_to_provider(
        session_id, plan, target_format=target_format, checkpoint_ns=checkpoint_ns
    )
    pending = {
        "assembly_id": snapshot.assembly_id,
        "execution_id": snapshot.execution_id,
        "plan": plan,
        "messages": messages,
        "tools": tools,
        "losses": losses,
    }
    with owner._lock:
        queue = owner._prepared_dispatches.setdefault(
            (session_id, checkpoint_ns, turn_id), []
        )
        if not any(item["assembly_id"] == snapshot.assembly_id for item in queue):
            queue.append(pending)
    return pending
