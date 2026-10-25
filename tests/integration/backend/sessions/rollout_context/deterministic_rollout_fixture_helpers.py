"""由真实 Saver 生成确定性 v2 集成 history，绝非真实 Provider 录制。"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.session_thread_context import session_thread_owner_scope
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.support.rollout.fixture_output import commit_fixture_output


def seed_deterministic_v2_rollout(
    workspace: Path, session_bundle_factory, prepare_execution,
) -> tuple[str, dict[str, str]]:
    sessions = workspace / ".boxteam" / "sessions"
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session_id)
    messages = []
    tool_bodies = {}
    with RolloutCheckpointSaver(sessions) as saver:
        owner = saver.forks.source.resolve_main_thread_owner(session_id)
        config = build_checkpoint_config(owner.thread_id)
        for ordinal in range(1, 129):
            turn_id = f"deterministic-turn-{ordinal:04d}"
            user = HumanMessage(
                content=f"确定性集成输入 {ordinal}",
                id=f"deterministic-user-{ordinal:04d}",
                response_metadata={"turn_id": turn_id},
            )
            messages.append(user)
            if ordinal % 8 == 0:
                call_id = f"deterministic-call-{ordinal:04d}"
                repetitions = 12_000 if ordinal in {120, 128} else 32
                body = f"确定性工具正文 {ordinal}: " + (f"body-{ordinal}|" * repetitions)
                tool_bodies[call_id] = body
                messages.extend(
                    [
                        AIMessage(
                            content="",
                            id=f"deterministic-invoke-{ordinal:04d}",
                            tool_calls=[
                                {
                                    "id": call_id,
                                    "name": "read_file",
                                    "args": {"path": f"fixture-{ordinal}.txt"},
                                }
                            ],
                        ),
                        ToolMessage(
                            content=body,
                            id=f"deterministic-result-{ordinal:04d}",
                            name="read_file",
                            tool_call_id=call_id,
                        ),
                    ]
                )
            final_id = f"deterministic-final-{ordinal:04d}"
            messages.append(AIMessage(content=f"确定性集成回答 {ordinal}", id=final_id))
            checkpoint = empty_checkpoint()
            checkpoint["id"] = f"deterministic-checkpoint-{ordinal:04d}"
            checkpoint["channel_values"] = {
                "messages": list(messages),
                "counter": ordinal,
            }
            checkpoint["channel_versions"] = {
                "messages": str(ordinal),
                "counter": str(ordinal),
            }
            accepted = saver.accept_turn(
                session_id, thread_id=owner.thread_id,
                accepted_ingress_id=user.id,
                acceptance_idempotency_key=f"deterministic-acceptance:{user.id}",
                job_id=f"deterministic-job:{turn_id}",
                admission_idempotency_key=f"deterministic-admission:{turn_id}",
                payload=user.content, turn_id=turn_id, root_item_id=f"item-{user.id}",
            )
            admission = saver.execution_admission(
                session_id, thread_id=owner.thread_id,
                execution_id=accepted["initial_execution_id"],
            )
            with (
                session_thread_owner_scope(owner, langgraph_thread_id=owner.thread_id),
                execution_admission_scope(admission, langgraph_thread_id=owner.thread_id),
            ):
                prepare_execution(saver, key=f"deterministic-create:{turn_id}")
                commit_fixture_output(saver, messages=checkpoint["channel_values"]["messages"])
                config = saver.put(
                    config, checkpoint,
                    {"source": "deterministic-integration", "step": ordinal},
                    checkpoint["channel_versions"],
                )
    return session_id, tool_bodies
