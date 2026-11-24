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
class CompactionExecutionReference:
    """摘要任务中一次块／合并调用，只引用原 owner 冻结的请求计划。"""

    task_id: str
    candidate_id: str
    plan_id: str
    stage: Literal["chunk", "merge"]
    ordinal: int
    attempt: int

    def __post_init__(self) -> None:
        for name in ("task_id", "candidate_id", "plan_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"CompactionExecutionReference.{name} 必须是非空字符串")
        if self.stage not in {"chunk", "merge"}:
            raise ValueError("摘要派生请求必须明确为 chunk 或 merge")
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("摘要派生请求 ordinal 必须是非负整数")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("摘要派生请求 attempt 必须是正整数")


@dataclass(frozen=True, slots=True)
class InternalExecutionIntent:
    """Turn-less 请求身份；通知、初始 seed 与摘要派生输入明确区分。"""

    message_id: str
    execution_id: str
    admission_idempotency_key: str
    input_reference_json: str | None = None
    compaction_reference: CompactionExecutionReference | None = None

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
        if self.compaction_reference is not None:
            if not isinstance(self.compaction_reference, CompactionExecutionReference):
                raise TypeError("摘要派生输入必须是 CompactionExecutionReference")
            if self.input_reference_json is not None:
                raise ValueError("摘要派生输入不得同时绑定 seed/reference")
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
    """所有 internal 输入共用的唯一 canonical request preimage。"""
    kind = request_input.get("kind")
    if kind == "runtime_notice":
        expected = {"kind", "payload"}
    elif kind in {"seed_reference", "compaction_reference"}:
        expected = {"kind", "reference"}
    else:
        raise ValueError(f"未知 internal execution input kind: {kind!r}")
    if set(request_input) != expected:
        raise ValueError("internal execution input 缺失字段或包含未知字段")
    if kind == "compaction_reference":
        reference = request_input["reference"]
        if not isinstance(reference, Mapping):
            raise TypeError("摘要派生输入 reference 必须是结构化引用")
        # 冷恢复与写入沿同一类型检查，不从当前任务或文件补造历史输入。
        CompactionExecutionReference(**reference)
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
    "CompactionExecutionReference",
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


@dataclass(frozen=True, slots=True)
class ExecutionTerminalResult:
    """原 convergence 提交的只读结果；commit_id 也是消息镜像的稳定身份。"""

    commit_id: int
    outcome: str
    failure: dict[str, object] | None = None
    after_interrupt_requested: bool = False
