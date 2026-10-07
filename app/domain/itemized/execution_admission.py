"""一次 Agent execution 的显式准入身份。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias

from app.domain.itemized.hashing import sha256_jcs

ExecutionOrigin = Literal["user_turn", "internal"]


@dataclass(frozen=True, slots=True)
class UserExecutionIdentity:
    """普通用户 Job 在 admission 前冻结的独立 Job、Turn 与准入身份。"""

    job_id: str
    turn_id: str
    admission_idempotency_key: str

    def __post_init__(self) -> None:
        for name in ("job_id", "turn_id", "admission_idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"UserExecutionIdentity.{name} 必须是非空字符串")
        if len({self.job_id, self.turn_id, self.admission_idempotency_key}) != 3:
            raise ValueError("Job、Turn 与 admission identity 必须彼此独立")


@dataclass(frozen=True, slots=True)
class InternalExecutionIntent:
    """Turn-less 请求身份；初始输入只引用 creation owner 冻结的 seed/reference。"""

    message_id: str
    execution_id: str
    admission_idempotency_key: str
    input_reference_json: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "message_id",
            "execution_id",
            "admission_idempotency_key",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"InternalExecutionIntent.{name} 必须是非空字符串"
                )
        if (
            len({self.message_id, self.execution_id, self.admission_idempotency_key})
            != 3
        ):
            raise ValueError("message、execution 与 admission identity 必须彼此独立")
        if self.input_reference_json is not None:
            reference = json.loads(self.input_reference_json)
            if not isinstance(reference, dict) or set(reference) != {
                "creation_idempotency_key", "task_seed_hash", "reference_binding_hash",
            }:
                raise ValueError("初始 execution 必须引用完整冻结 seed/reference identity")
            if any(not isinstance(value, str) or not value for value in reference.values()):
                raise ValueError("初始 execution reference 字段必须是非空字符串")

    @property
    def notice_item_id(self) -> str:
        return f"item-{self.message_id}"


ExecutionBinding: TypeAlias = UserExecutionIdentity | InternalExecutionIntent


def internal_execution_request_preimage(
    *,
    session_id: str,
    thread_id: str,
    execution_id: str,
    job_id: str,
    admission_idempotency_key: str,
    request_input: Mapping[str, object],
) -> dict[str, object]:
    """notice 与 seed/reference 共用的唯一 canonical request preimage。"""
    kind = request_input.get("kind")
    if kind == "runtime_notice":
        expected = {"kind", "payload"}
    elif kind == "seed_reference":
        expected = {"kind", "reference"}
    else:
        raise ValueError(f"未知 internal execution input kind: {kind!r}")
    if set(request_input) != expected:
        raise ValueError("internal execution input 缺失字段或包含未知字段")
    preimage = {
        "session_id": session_id,
        "thread_id": thread_id,
        "execution_id": execution_id,
        "job_id": job_id,
        "admission_idempotency_key": admission_idempotency_key,
        "input": dict(request_input),
    }
    sha256_jcs(preimage)
    return preimage


def user_turn_admission_payload_hash(
    *,
    session_id: str,
    thread_id: str,
    turn_id: str,
    execution_id: str,
    job_id: str,
    accepted_ingress_id: str,
    admission_idempotency_key: str,
    payload_hash: str,
) -> str:
    """计算绑定 acceptance 与 owner 的 user-turn admission preimage hash。"""
    return sha256_jcs(
        {
            "session_id": session_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "execution_id": execution_id,
            "job_id": job_id,
            "accepted_ingress_id": accepted_ingress_id,
            "admission_idempotency_key": admission_idempotency_key,
            "payload_hash": payload_hash,
        }
    )


@dataclass(frozen=True, slots=True)
class ExecutionAdmission:
    """绑定执行到精确 SessionThread、Job 与可选真实 Turn。"""

    session_id: str
    thread_id: str
    execution_id: str
    job_id: str
    admission_idempotency_key: str
    execution_origin: ExecutionOrigin
    turn_id: str | None

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "thread_id",
            "execution_id",
            "job_id",
            "admission_idempotency_key",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ExecutionAdmission.{name} 必须是非空字符串")
        if self.execution_origin == "user_turn":
            if not isinstance(self.turn_id, str) or not self.turn_id.strip():
                raise ValueError("user_turn execution admission 必须绑定真实 turn_id")
        elif self.execution_origin == "internal":
            if self.turn_id is not None:
                raise ValueError("internal execution admission 的 turn_id 必须为 None")
        else:
            raise ValueError(
                f"未知 execution_origin: {self.execution_origin!r}"
            )


__all__ = [
    "ExecutionAdmission",
    "ExecutionBinding",
    "ExecutionOrigin",
    "InternalExecutionIntent",
    "UserExecutionIdentity",
    "internal_execution_request_preimage",
    "user_turn_admission_payload_hash",
]
