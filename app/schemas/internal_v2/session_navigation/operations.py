"""会话目录异步 mutation 协议 DTO（OpenSpec add-itemized-rollout-context 8.1-G/8.1-H）。

与同步目录读模型（``models.py``）分开：这里只定义 typed 导航 operation 的
批量入队、durable receipt、按 ID 状态查询、revision-pinned snapshot 与
独立 ``navigation`` 事件 channel 的公共协议。

契约要点（design.md §9.0）：

- 每个 intent 的 ``operation_id`` 等于客户端经验证的 ``client_operation_id``；
- ``202`` 只表示 durable acceptance，绝不表示目录已改变；
- 状态闭集为 ``queued|running|committed|rejected|cancelled|dependency_failed``，
  其中 ``dependency_failed`` 只能由 ``queued`` 直接进入；
- 事件与 operation terminal 状态在同一个 catalog 事务提交。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "NavigationEventDTO",
    "NavigationEventsPageDTO",
    "NavigationMutationEnqueueRequest",
    "NavigationMutationEnqueueResultDTO",
    "NavigationMutationIntentDTO",
    "NavigationMutationReceiptDTO",
    "NavigationMutationStatusPageDTO",
    "NavigationSnapshotDTO",
]

# operation_id 形态：软件生成的 ``op_`` + 32 位小写 hex（UUIDv4 位数保留）。
_OPERATION_ID_PATTERN = re.compile(r"op_[0-9a-f]{32}")


def validate_operation_id(value: object) -> None:
    """校验导航 operation/client_operation_id 形态。

    与 canonical session/thread ID 同款纪律：不做清洗、截断或大小写折叠。
    """
    if not isinstance(value, str):
        raise TypeError(f"operation_id 必须是字符串: {value!r}")
    if _OPERATION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"operation_id 形态非法: {value!r}")


# operation 状态闭集（除 dependency_failed 外与设计文档一致）。
NavigationMutationState = Literal[
    "queued",
    "running",
    "committed",
    "rejected",
    "cancelled",
    "dependency_failed",
]
# 终态集合：进入后仅保留 compact tombstone，迟到重放一律返回原 terminal。
NAVIGATION_MUTATION_TERMINAL_STATES = (
    "committed",
    "rejected",
    "cancelled",
    "dependency_failed",
)

NavigationMutationKind = Literal[
    "create_folder",
    "rename_node",
    "move_node",
    "delete_folder",
    "delete_session",
]


class NavigationMutationIntentDTO(BaseModel):
    """批量入队中的一个 typed 导航 intent。

    ``target_node_id`` 与 ``created_by_operation_id`` 二选一：新建 Folder 用
    ``client_ref`` 表达未确认节点，后端在 acceptance 时分配 canonical ID，
    跨批依赖通过 ``created_by_operation_id`` 解析；同批内依赖用
    ``depends_on`` 引用本批其它 intent 的 ``client_operation_id``。
    """

    model_config = ConfigDict(extra="forbid")

    client_operation_id: str
    client_sequence: int = Field(ge=1)
    kind: NavigationMutationKind
    base_catalog_revision: int = Field(ge=0)
    expected_revision: int | None = Field(default=None, ge=1)
    target_node_id: str | None = None
    created_by_operation_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_node_id: str | None = None
    recursive: bool = False
    depends_on: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_intent(self) -> NavigationMutationIntentDTO:
        validate_operation_id(self.client_operation_id)
        for dependency_id in self.depends_on:
            validate_operation_id(dependency_id)
        if self.created_by_operation_id is not None:
            validate_operation_id(self.created_by_operation_id)
        if self.kind == "create_folder":
            if self.name is None:
                raise ValueError("create_folder 缺少 name")
            if self.target_node_id is not None:
                raise ValueError("create_folder 不接受 target_node_id")
            return self
        if self.target_node_id is None:
            raise ValueError(f"{self.kind} 缺少 target_node_id")
        if self.kind == "rename_node":
            if self.name is None:
                raise ValueError("rename_node 缺少 name")
            self._require_expected_revision()
            if self.parent_node_id is not None:
                raise ValueError("rename_node 不接受 parent_node_id")
        elif self.kind == "move_node":
            # move_node 允许 parent_node_id=None（移到根），但必须显式给出。
            self._require_expected_revision()
            if "parent_node_id" not in self.model_fields_set:
                raise ValueError("move_node 必须显式给出 parent_node_id（可为 null）")
        elif self.kind in ("delete_folder", "delete_session"):
            if self.kind == "delete_session" and self.recursive:
                raise ValueError("delete_session 不接受 recursive")
        return self

    def _require_expected_revision(self) -> None:
        """改名/移动必须携带目标 node 的 expected revision 作为执行期 CAS 前置。"""
        if self.expected_revision is None:
            raise ValueError(f"{self.kind} 必须给出 target node 的 expected_revision")


class NavigationMutationEnqueueRequest(BaseModel):
    """批量入队信封：一个短 SQLite 事务内原子接受全部 intent。"""

    model_config = ConfigDict(extra="forbid")

    intents: list[NavigationMutationIntentDTO] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_batch(self) -> NavigationMutationEnqueueRequest:
        seen_ids: set[str] = set()
        batch_ids = {intent.client_operation_id for intent in self.intents}
        for intent in self.intents:
            if intent.client_operation_id in seen_ids:
                raise ValueError(
                    f"同批 client_operation_id 重复: {intent.client_operation_id}"
                )
            seen_ids.add(intent.client_operation_id)
        for intent in self.intents:
            for dependency_id in intent.depends_on:
                if dependency_id == intent.client_operation_id:
                    raise ValueError(
                        f"intent 不能依赖自身: {intent.client_operation_id}"
                    )
                if dependency_id not in batch_ids:
                    raise ValueError(
                        "depends_on 必须引用同一批次的 client_operation_id: "
                        f"intent={intent.client_operation_id}, "
                        f"dependency={dependency_id}"
                    )
        sequences = [intent.client_sequence for intent in self.intents]
        if len(set(sequences)) != len(sequences):
            raise ValueError("同批 client_sequence 必须唯一且表达入队顺序")
        return self


class NavigationMutationReceiptDTO(BaseModel):
    """单个 operation 的 durable receipt（202 返回体与状态查询共用）。

    ``created_node_id`` 只在 create_folder 时给出后端分配的 canonical Folder
    ID；``pending_settlement`` 只对已 committed 的删除类 operation 为真，表示
    逻辑已提交但物理排空尚未完成。
    """

    operation_id: str
    client_sequence: int
    queue_seq: int
    kind: NavigationMutationKind
    state: NavigationMutationState
    created_node_id: str | None = None
    committed_catalog_revision: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    pending_settlement: bool = False
    receipt_revision: int = Field(ge=0)
    updated_at: datetime


class NavigationMutationEnqueueResultDTO(BaseModel):
    """批量入队的 202 durable acceptance 结果（含 client_ref → canonical ID 映射）。"""

    workspace_id: str
    accepted_count: int = Field(ge=0)
    receipts: list[NavigationMutationReceiptDTO] = Field(default_factory=list)
    created_node_ids: dict[str, str] = Field(default_factory=dict)


class NavigationMutationStatusPageDTO(BaseModel):
    """按精确 operation ID 查询的 durable 状态页。

    缺失的 operation_id 出现在 ``unknown_operation_ids``：客户端据此保留
    pending 并按同一 ID 重试，不直接回退。
    """

    workspace_id: str
    catalog_revision: int = Field(ge=0)
    items: list[NavigationMutationReceiptDTO] = Field(default_factory=list)
    unknown_operation_ids: list[str] = Field(default_factory=list)


class NavigationEventDTO(BaseModel):
    """独立 ``navigation`` channel 的单个终态事件。

    ``event_seq`` 单调且与同库 operation terminal 状态同事务提交，作为可恢复
    cursor；``result_state`` 是该 operation 的终态闭集取值。
    """

    event_seq: int = Field(ge=1)
    workspace_id: str
    operation_id: str
    queue_seq: int
    kind: NavigationMutationKind
    result_state: NavigationMutationState
    committed_catalog_revision: int | None = None
    affected_node_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_detail: str | None = None
    created_at: datetime


class NavigationEventsPageDTO(BaseModel):
    """navigation channel 事件页；``next_cursor`` 仅在还有更多事件时给出。"""

    workspace_id: str
    event_seq_watermark: int = Field(ge=0)
    items: list[NavigationEventDTO] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    cursor_gone: bool = False


class NavigationSnapshotDTO(BaseModel):
    """revision-pinned catalog snapshot：只读单事务内取同一 revision 与事件水位。"""

    workspace_id: str
    catalog_revision: int = Field(ge=0)
    event_seq_watermark: int = Field(ge=0)
    generation: int = Field(ge=0)
