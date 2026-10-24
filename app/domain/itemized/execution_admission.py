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


@dataclass(frozen=True, slots=True)
class ExecutionAdmissionCursor:
    """同一 thread 的准入提交顺序；可不连续，不表示全部 context 的排序。"""

    admission: ExecutionAdmission
    commit_id: int
    commit_kind: Literal["acceptance", "execution_admission"]

    def __post_init__(self) -> None:
        if not isinstance(self.admission, ExecutionAdmission):
            raise TypeError("准入游标必须绑定完整 ExecutionAdmission")
        if not isinstance(self.commit_id, int) or isinstance(self.commit_id, bool) or self.commit_id < 1:
            raise ValueError("准入游标必须引用真实 storage commit")
        expected = "acceptance" if self.admission.execution_origin == "user_turn" else "execution_admission"
        if self.commit_kind != expected:
            raise ValueError("准入游标 commit kind 与 execution origin 不一致")


@dataclass(frozen=True, slots=True)
class InternalExecutionInterrupt:
    """原 owner 接受的内部执行中断；历史复制不赋予目标操作权。"""

    admission: ExecutionAdmission
    interrupt_request_id: str
    reminder_item_id: str
    reason: Literal["user_requested"] = "user_requested"

    def __post_init__(self) -> None:
        if not isinstance(self.admission, ExecutionAdmission) or self.admission.execution_origin != "internal":
            raise ValueError("内部中断必须绑定 Turn-less internal admission")
        for name in ("interrupt_request_id", "reminder_item_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"InternalExecutionInterrupt.{name} 必须是非空身份")
        if self.reason != "user_requested":
            raise ValueError("内部 execution 中断必须保留明确的用户请求原因")


__all__ = [
    "ExecutionAdmission",
    "ExecutionAdmissionCursor",
    "ExecutionBinding",
    "ExecutionOrigin",
    "InternalExecutionIntent",
    "InternalExecutionInterrupt",
    "UserExecutionIdentity",
    "internal_execution_request_preimage",
    "user_turn_admission_payload_hash",
]
