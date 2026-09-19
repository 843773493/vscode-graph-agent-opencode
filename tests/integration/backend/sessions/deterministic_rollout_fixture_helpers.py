"""由真实 Saver 生成确定性 v2 集成 history，绝非真实 Provider 录制。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)


def seed_deterministic_v2_rollout(
    workspace: Path, session_bundle_factory
) -> tuple[str, dict[str, str]]:
    sessions = workspace / ".boxteam" / "sessions"
    session_id = f"ses_{uuid4().hex}"
    session_bundle_factory(sessions, session_id)
    messages = []
    tool_bodies = {}
    config = build_checkpoint_config(session_id)
    with RolloutCheckpointSaver(sessions) as saver:
        for ordinal in range(1, 129):
            turn_id = f"deterministic-turn-{ordinal:04d}"
            messages.append(
                HumanMessage(
                    content=f"确定性集成输入 {ordinal}",
                    id=f"deterministic-user-{ordinal:04d}",
                    response_metadata={"turn_id": turn_id},
                )
            )
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
            config = saver.put(
                config,
                checkpoint,
                {"source": "deterministic-integration", "step": ordinal},
                checkpoint["channel_versions"],
            )
            saver.finalize_turn(
                session_id=session_id, turn_id=turn_id, final_message_id=final_id
            )
    return session_id, tool_bodies
