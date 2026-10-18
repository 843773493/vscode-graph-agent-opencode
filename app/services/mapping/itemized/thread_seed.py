"""冻结 creation record 到 seed/reference 请求的纯映射。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.abstractions.internal_message import PreparedInternalMessage
from app.core.session_control_store import ThreadCreationRecord
from app.domain.itemized.hashing import sha256_jcs
from app.prompting import PromptSection, internal_message_factory

THREAD_SEED_DISPLAY_CONTENT = "已收到委派任务。"


@dataclass(frozen=True, slots=True)
class FrozenThreadSeed:
    reference: dict[str, object]
    message: PreparedInternalMessage
    gateway_id: str | None


def frozen_thread_seed(record: ThreadCreationRecord) -> FrozenThreadSeed:
    if record.state != "published" or record.task_seed is None or record.task_reference is None:
        raise RuntimeError("running child seed 必须来自已发布的冻结 creation record")
    seed = json.loads(record.task_seed)
    reference = json.loads(record.task_reference)
    if not isinstance(seed, dict) or not isinstance(reference, dict):
        raise TypeError("creation seed/reference 必须是 object")
    description = seed.get("description")
    if not isinstance(description, str) or not description:
        raise ValueError("child seed 缺少 description")
    if "gateway_id" not in seed:
        raise ValueError("child seed 缺少冻结 gateway_id；独立工作区必须显式保存 null")
    gateway_id = seed["gateway_id"]
    if gateway_id is not None and (not isinstance(gateway_id, str) or not gateway_id.strip()):
        raise ValueError("child seed gateway_id 必须是非空字符串或显式 null")
    sections = [
        PromptSection("control_context", {**reference, **dict(seed.get("trusted_context", {}))}),
        PromptSection("delegated_task", description),
    ]
    instructions = seed.get("untrusted_instructions")
    if instructions is not None:
        sections.append(PromptSection("untrusted_instructions", instructions))
    return FrozenThreadSeed(
        gateway_id=gateway_id,
        reference={
            "creation_idempotency_key": record.thread_creation_idempotency_key,
            "task_seed_hash": sha256_jcs(seed),
            "reference_binding_hash": sha256_jcs(reference),
        },
        message=internal_message_factory.build(
            kind="delegated_task",
            control="在当前 child thread 中独立完成已冻结的委派任务；结果由后端按持久 parent thread 地址自动回报。",
            sections=tuple(sections),
            display_content=THREAD_SEED_DISPLAY_CONTENT,
        ),
    )
