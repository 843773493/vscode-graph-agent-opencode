"""外部资源身份与 operation lease 的持久事实。"""

from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import ConfigDict, TypeAdapter

from app.core.child_process_state import ChildProcessState


@dataclass(slots=True)
class ResourceRecord:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    resource_id: str
    kind: str
    lifetime_scope: str
    created_by_turn_id: str | None
    status: str = "running"
    updated_at: str = ""
    process: ChildProcessState | None = None
    mcp_server_id: str | None = None
    cleanup_failed: bool = False


@dataclass(slots=True)
class ResourceLease:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    lease_id: str
    resource_id: str
    turn_stream_id: str
    operation_id: str
    # 冷恢复必须拒绝非法状态，不能将未识别的占用当作已释放。
    status: Literal["active", "dispatching", "released", "settled", "reconcile_required"] = "active"
    updated_at: str = ""
    holder_session_id: str | None = None
    holder_thread_id: str | None = None
    # 期限由实际 owner 执行；账本不能据此假报资源或远端操作已停止。
    expires_at: str | None = None


RESOURCE_RECORD = TypeAdapter(ResourceRecord)
RESOURCE_LEASES = TypeAdapter(list[ResourceLease], config=ConfigDict(hide_input_in_errors=True))

