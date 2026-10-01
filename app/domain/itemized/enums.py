"""itemized v2 闭合枚举。"""

from enum import StrEnum


class CanonicalItemStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


class PayloadKind(StrEnum):
    TEXT = "text"
    STRUCTURED_CONTENT = "structured_content"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SUMMARY = "summary"
    ATTACHMENT_REF = "attachment_ref"
    OPAQUE = "opaque"
    EXTENSION = "extension"


class SemanticKind(StrEnum):
    USER_INPUT = "user_input"
    ASSISTANT_OUTPUT = "assistant_output"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    RUNTIME_NOTICE = "runtime_notice"
    COMPACTION_SUMMARY = "compaction_summary"
    ATTACHMENT = "attachment"
    EXTENSION = "extension"


class TurnScope(StrEnum):
    TURN_ROOT = "turn_root"
    TURN_MEMBER = "turn_member"
    AMBIENT = "ambient"
    PENDING_NEXT_TURN = "pending_next_turn"


class TurnStatus(StrEnum):
    OPEN = "open"
    ACTIVE = "active"
    COMPLETED = "completed"
    COMPLETED_EMPTY = "completed_empty"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ControlOutcome(StrEnum):
    COMPLETED = "completed"
    COMPLETED_EMPTY = "completed_empty"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    EXECUTION_LOST = "execution_lost"
    UNKNOWN = "unknown"


class CommitKind(StrEnum):
    ACCEPTANCE = "acceptance"
    ASSEMBLY_SEALED = "assembly_sealed"
    ITEM_CONVERGENCE = "item_convergence"
    TERMINAL_CONVERGENCE = "terminal_convergence"


class CommitMode(StrEnum):
    ITEM_BEARING = "item_bearing"
    METADATA_ONLY = "metadata_only"


class SelectionKind(StrEnum):
    CANONICAL_HISTORY = "canonical_history"
    REQUEST_ONLY = "request_only"
    OVERLAY_BASE = "overlay_base"
    OVERLAY_DELTA = "overlay_delta"
    TOOL_SET = "tool_set"


class BaseDeltaRole(StrEnum):
    NONE = "none"
    BASE = "base"
    DELTA = "delta"


class DetailProtection(StrEnum):
    PUBLIC = "public"
    REDACTED = "redacted"
    PROTECTED = "protected"


class DetailAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    FORBIDDEN = "forbidden"
    EXPIRED = "expired"
