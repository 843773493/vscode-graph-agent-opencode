"""per-session session-control.sqlite 的跨 Session 通信 ledger owner。

本模块承载 outbox/inbox 这一条垂直链路的唯一实现（D5，2.4/4.7）：

- ``communication_outbox`` / ``communication_inbox`` 表 DDL、
  target_accepted 覆盖索引、列清单与行投影；
- source 侧 create-or-get outbox（operation 层 PK 幂等 + communication 层
  UNIQUE dedupe，任何 preimage 漂移 fail closed）与前向状态 CAS 迁移；
- target 侧 create-or-get inbox（main binding fresh 校验、admission
  identity 确定性派生）、admission 领取、execution bound 与失败记录；
- kind=reply 的双端因果证明（各自在本库对方表里要求方向相反的行）。

CommunicationLedgerMixin 由 app.core.session_control_store.SessionControlStore
继承装配；本模块只依赖宿主类提供的 database_path、_connection、_ensure_open()
与 _write_transaction()，不感知 thread catalog / creation record / execution
intent / operation lease / owner binding 等其它控制库职责。错误分类沿用
session_control_store 约定：KeyError 目标行缺失、RuntimeError 库被外部改动或
CAS 冲突、ValueError 输入形态非法、TypeError 输入类型错误。
"""

from app.core.session_control_primitives import (
    SHA256_HEX_PATTERN as SHA256_HEX_PATTERN,  # noqa: PLC0414
)

from .inbox_writes import InboxWritesMixin
from .outbox_writes import OutboxWritesMixin
from .reads import (
    ReadsMixin,
    _fetch_inbox_row,  # noqa: F401
    _fetch_outbox_row_by_communication,  # noqa: F401
    _fetch_outbox_row_by_operation,  # noqa: F401
)
from .records import (
    CommunicationInboxRecord,
    CommunicationOutboxRecord,
    _communication_inbox_from_row,  # noqa: F401
    _communication_outbox_from_row,  # noqa: F401
    derive_communication_admission_identity,
)
from .schema import (
    _COMMUNICATION_ID_PATTERN,  # noqa: F401
    _COMMUNICATION_INBOX_COLUMNS,  # noqa: F401
    _COMMUNICATION_KINDS,  # noqa: F401
    _COMMUNICATION_OUTBOX_COLUMNS,  # noqa: F401
    _COMMUNICATION_OUTBOX_TERMINAL_STATES,  # noqa: F401
    _COMMUNICATION_OUTBOX_TRANSITIONS,  # noqa: F401
    COMMUNICATION_INBOX_TABLE_DDL,
    COMMUNICATION_OUTBOX_TABLE_DDL,
    IDX_COMMUNICATION_INBOX_TARGET_ACCEPTED_DDL,
)
from .validation import (
    _OUTBOX_PREIMAGE_FIELDS,  # noqa: F401
    _inbox_preimage_mismatches,  # noqa: F401
    _outbox_preimage_mismatches,  # noqa: F401
    _validate_communication_address,  # noqa: F401
    _validate_communication_kind_and_reply,  # noqa: F401
    _validate_communication_text,  # noqa: F401
)

__all__ = [
    "COMMUNICATION_INBOX_TABLE_DDL",
    "COMMUNICATION_OUTBOX_TABLE_DDL",
    "IDX_COMMUNICATION_INBOX_TARGET_ACCEPTED_DDL",
    "CommunicationInboxRecord",
    "CommunicationLedgerMixin",
    "CommunicationOutboxRecord",
    "derive_communication_admission_identity",
]


class CommunicationLedgerMixin(
    OutboxWritesMixin,
    InboxWritesMixin,
    ReadsMixin,
):
    """SessionControlStore 的跨 Session 通信 ledger 方法族。

    依赖宿主类提供 database_path、_connection、_ensure_open() 与
    _write_transaction()。
    """
