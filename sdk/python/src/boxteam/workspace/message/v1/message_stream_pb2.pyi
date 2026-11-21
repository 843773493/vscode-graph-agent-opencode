import datetime

from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class BlockDeltaOperation(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    BLOCK_DELTA_OPERATION_UNSPECIFIED: _ClassVar[BlockDeltaOperation]
    BLOCK_DELTA_OPERATION_APPEND: _ClassVar[BlockDeltaOperation]
    BLOCK_DELTA_OPERATION_ITEM_UPSERT: _ClassVar[BlockDeltaOperation]
    BLOCK_DELTA_OPERATION_ITEM_PATCH: _ClassVar[BlockDeltaOperation]
    BLOCK_DELTA_OPERATION_REDACTED: _ClassVar[BlockDeltaOperation]

class StreamStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STREAM_STATUS_UNSPECIFIED: _ClassVar[StreamStatus]
    STREAM_STATUS_OPEN: _ClassVar[StreamStatus]
    STREAM_STATUS_INTERRUPTING: _ClassVar[StreamStatus]
    STREAM_STATUS_COMPLETED: _ClassVar[StreamStatus]
    STREAM_STATUS_INTERRUPTED: _ClassVar[StreamStatus]
    STREAM_STATUS_FAILED: _ClassVar[StreamStatus]

class ModelCallOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MODEL_CALL_OUTCOME_UNSPECIFIED: _ClassVar[ModelCallOutcome]
    MODEL_CALL_OUTCOME_ACCEPTED: _ClassVar[ModelCallOutcome]
    MODEL_CALL_OUTCOME_VALIDATION_FAILED: _ClassVar[ModelCallOutcome]
    MODEL_CALL_OUTCOME_UPSTREAM_ERROR: _ClassVar[ModelCallOutcome]
    MODEL_CALL_OUTCOME_EXECUTION_LOST: _ClassVar[ModelCallOutcome]
    MODEL_CALL_OUTCOME_USER_INTERRUPT: _ClassVar[ModelCallOutcome]

class ToolExecutionStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TOOL_EXECUTION_STATUS_UNSPECIFIED: _ClassVar[ToolExecutionStatus]
    TOOL_EXECUTION_STATUS_RUNNING: _ClassVar[ToolExecutionStatus]
    TOOL_EXECUTION_STATUS_COMPLETED: _ClassVar[ToolExecutionStatus]
    TOOL_EXECUTION_STATUS_FAILED: _ClassVar[ToolExecutionStatus]

class ToolExecutionOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TOOL_EXECUTION_OUTCOME_UNSPECIFIED: _ClassVar[ToolExecutionOutcome]
    TOOL_EXECUTION_OUTCOME_SUCCESS: _ClassVar[ToolExecutionOutcome]
    TOOL_EXECUTION_OUTCOME_PROVIDER_ERROR: _ClassVar[ToolExecutionOutcome]
    TOOL_EXECUTION_OUTCOME_EXECUTION_LOST: _ClassVar[ToolExecutionOutcome]
    TOOL_EXECUTION_OUTCOME_OUTCOME_UNKNOWN: _ClassVar[ToolExecutionOutcome]

class ActivityStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ACTIVITY_STATUS_UNSPECIFIED: _ClassVar[ActivityStatus]
    ACTIVITY_STATUS_RUNNING: _ClassVar[ActivityStatus]
    ACTIVITY_STATUS_WAITING: _ClassVar[ActivityStatus]
    ACTIVITY_STATUS_STOPPING: _ClassVar[ActivityStatus]
    ACTIVITY_STATUS_COMPLETED: _ClassVar[ActivityStatus]
    ACTIVITY_STATUS_FAILED: _ClassVar[ActivityStatus]
    ACTIVITY_STATUS_UNKNOWN: _ClassVar[ActivityStatus]

class ActivityOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ACTIVITY_OUTCOME_UNSPECIFIED: _ClassVar[ActivityOutcome]
    ACTIVITY_OUTCOME_SUCCESS: _ClassVar[ActivityOutcome]
    ACTIVITY_OUTCOME_USER_INTERRUPT: _ClassVar[ActivityOutcome]
    ACTIVITY_OUTCOME_PROVIDER_ERROR: _ClassVar[ActivityOutcome]
    ACTIVITY_OUTCOME_EXECUTION_LOST: _ClassVar[ActivityOutcome]
    ACTIVITY_OUTCOME_OUTCOME_UNKNOWN: _ClassVar[ActivityOutcome]
BLOCK_DELTA_OPERATION_UNSPECIFIED: BlockDeltaOperation
BLOCK_DELTA_OPERATION_APPEND: BlockDeltaOperation
BLOCK_DELTA_OPERATION_ITEM_UPSERT: BlockDeltaOperation
BLOCK_DELTA_OPERATION_ITEM_PATCH: BlockDeltaOperation
BLOCK_DELTA_OPERATION_REDACTED: BlockDeltaOperation
STREAM_STATUS_UNSPECIFIED: StreamStatus
STREAM_STATUS_OPEN: StreamStatus
STREAM_STATUS_INTERRUPTING: StreamStatus
STREAM_STATUS_COMPLETED: StreamStatus
STREAM_STATUS_INTERRUPTED: StreamStatus
STREAM_STATUS_FAILED: StreamStatus
MODEL_CALL_OUTCOME_UNSPECIFIED: ModelCallOutcome
MODEL_CALL_OUTCOME_ACCEPTED: ModelCallOutcome
MODEL_CALL_OUTCOME_VALIDATION_FAILED: ModelCallOutcome
MODEL_CALL_OUTCOME_UPSTREAM_ERROR: ModelCallOutcome
MODEL_CALL_OUTCOME_EXECUTION_LOST: ModelCallOutcome
MODEL_CALL_OUTCOME_USER_INTERRUPT: ModelCallOutcome
TOOL_EXECUTION_STATUS_UNSPECIFIED: ToolExecutionStatus
TOOL_EXECUTION_STATUS_RUNNING: ToolExecutionStatus
TOOL_EXECUTION_STATUS_COMPLETED: ToolExecutionStatus
TOOL_EXECUTION_STATUS_FAILED: ToolExecutionStatus
TOOL_EXECUTION_OUTCOME_UNSPECIFIED: ToolExecutionOutcome
TOOL_EXECUTION_OUTCOME_SUCCESS: ToolExecutionOutcome
TOOL_EXECUTION_OUTCOME_PROVIDER_ERROR: ToolExecutionOutcome
TOOL_EXECUTION_OUTCOME_EXECUTION_LOST: ToolExecutionOutcome
TOOL_EXECUTION_OUTCOME_OUTCOME_UNKNOWN: ToolExecutionOutcome
ACTIVITY_STATUS_UNSPECIFIED: ActivityStatus
ACTIVITY_STATUS_RUNNING: ActivityStatus
ACTIVITY_STATUS_WAITING: ActivityStatus
ACTIVITY_STATUS_STOPPING: ActivityStatus
ACTIVITY_STATUS_COMPLETED: ActivityStatus
ACTIVITY_STATUS_FAILED: ActivityStatus
ACTIVITY_STATUS_UNKNOWN: ActivityStatus
ACTIVITY_OUTCOME_UNSPECIFIED: ActivityOutcome
ACTIVITY_OUTCOME_SUCCESS: ActivityOutcome
ACTIVITY_OUTCOME_USER_INTERRUPT: ActivityOutcome
ACTIVITY_OUTCOME_PROVIDER_ERROR: ActivityOutcome
ACTIVITY_OUTCOME_EXECUTION_LOST: ActivityOutcome
ACTIVITY_OUTCOME_OUTCOME_UNKNOWN: ActivityOutcome

class StreamOpened(_message.Message):
    __slots__ = ("status",)
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: StreamStatus
    def __init__(self, status: _Optional[_Union[StreamStatus, str]] = ...) -> None: ...

class ModelStarted(_message.Message):
    __slots__ = ("model_call_id", "attempt", "model")
    MODEL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    model_call_id: str
    attempt: int
    model: str
    def __init__(self, model_call_id: _Optional[str] = ..., attempt: _Optional[int] = ..., model: _Optional[str] = ...) -> None: ...

class ModelCompleted(_message.Message):
    __slots__ = ("model_call_id", "attempt", "outcome", "reason")
    MODEL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    model_call_id: str
    attempt: int
    outcome: ModelCallOutcome
    reason: str
    def __init__(self, model_call_id: _Optional[str] = ..., attempt: _Optional[int] = ..., outcome: _Optional[_Union[ModelCallOutcome, str]] = ..., reason: _Optional[str] = ...) -> None: ...

class ModelRetrying(_message.Message):
    __slots__ = ("model_call_id", "attempt", "reason")
    MODEL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    model_call_id: str
    attempt: int
    reason: str
    def __init__(self, model_call_id: _Optional[str] = ..., attempt: _Optional[int] = ..., reason: _Optional[str] = ...) -> None: ...

class ModelFailed(_message.Message):
    __slots__ = ("model_call_id", "attempt", "outcome", "error_code", "message", "retryable")
    MODEL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    model_call_id: str
    attempt: int
    outcome: ModelCallOutcome
    error_code: str
    message: str
    retryable: bool
    def __init__(self, model_call_id: _Optional[str] = ..., attempt: _Optional[int] = ..., outcome: _Optional[_Union[ModelCallOutcome, str]] = ..., error_code: _Optional[str] = ..., message: _Optional[str] = ..., retryable: _Optional[bool] = ...) -> None: ...

class BlockStarted(_message.Message):
    __slots__ = ("block_id", "block_index", "carrier_type", "projection")
    BLOCK_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCK_INDEX_FIELD_NUMBER: _ClassVar[int]
    CARRIER_TYPE_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_FIELD_NUMBER: _ClassVar[int]
    block_id: str
    block_index: int
    carrier_type: str
    projection: str
    def __init__(self, block_id: _Optional[str] = ..., block_index: _Optional[int] = ..., carrier_type: _Optional[str] = ..., projection: _Optional[str] = ...) -> None: ...

class BlockDelta(_message.Message):
    __slots__ = ("block_id", "block_index", "local_seq", "carrier_type", "operation", "text", "item", "redacted", "projection")
    BLOCK_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCK_INDEX_FIELD_NUMBER: _ClassVar[int]
    LOCAL_SEQ_FIELD_NUMBER: _ClassVar[int]
    CARRIER_TYPE_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    ITEM_FIELD_NUMBER: _ClassVar[int]
    REDACTED_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_FIELD_NUMBER: _ClassVar[int]
    block_id: str
    block_index: int
    local_seq: int
    carrier_type: str
    operation: BlockDeltaOperation
    text: str
    item: _struct_pb2.Struct
    redacted: bool
    projection: str
    def __init__(self, block_id: _Optional[str] = ..., block_index: _Optional[int] = ..., local_seq: _Optional[int] = ..., carrier_type: _Optional[str] = ..., operation: _Optional[_Union[BlockDeltaOperation, str]] = ..., text: _Optional[str] = ..., item: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., redacted: _Optional[bool] = ..., projection: _Optional[str] = ...) -> None: ...

class BlockCompleted(_message.Message):
    __slots__ = ("block_id", "block_index", "carrier_type", "status", "completion_reason", "partial")
    BLOCK_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCK_INDEX_FIELD_NUMBER: _ClassVar[int]
    CARRIER_TYPE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_REASON_FIELD_NUMBER: _ClassVar[int]
    PARTIAL_FIELD_NUMBER: _ClassVar[int]
    block_id: str
    block_index: int
    carrier_type: str
    status: str
    completion_reason: str
    partial: bool
    def __init__(self, block_id: _Optional[str] = ..., block_index: _Optional[int] = ..., carrier_type: _Optional[str] = ..., status: _Optional[str] = ..., completion_reason: _Optional[str] = ..., partial: _Optional[bool] = ...) -> None: ...

class ToolCall(_message.Message):
    __slots__ = ("tool_call_id", "tool_name", "arguments", "status", "arguments_complete", "completion_reason", "started_seq", "last_event_seq", "completed_seq", "started_at", "updated_at", "completed_at", "tool_invocation_id", "tool_attempt_id")
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_COMPLETE_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_REASON_FIELD_NUMBER: _ClassVar[int]
    STARTED_SEQ_FIELD_NUMBER: _ClassVar[int]
    LAST_EVENT_SEQ_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_SEQ_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    TOOL_INVOCATION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    tool_call_id: str
    tool_name: str
    arguments: _struct_pb2.Struct
    status: str
    arguments_complete: bool
    completion_reason: str
    started_seq: int
    last_event_seq: int
    completed_seq: int
    started_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    completed_at: _timestamp_pb2.Timestamp
    tool_invocation_id: str
    tool_attempt_id: str
    def __init__(self, tool_call_id: _Optional[str] = ..., tool_name: _Optional[str] = ..., arguments: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., status: _Optional[str] = ..., arguments_complete: _Optional[bool] = ..., completion_reason: _Optional[str] = ..., started_seq: _Optional[int] = ..., last_event_seq: _Optional[int] = ..., completed_seq: _Optional[int] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., completed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., tool_invocation_id: _Optional[str] = ..., tool_attempt_id: _Optional[str] = ...) -> None: ...

class ToolCallDelta(_message.Message):
    __slots__ = ("tool_call_id", "tool_name", "arguments_delta", "arguments", "arguments_complete", "tool_invocation_id", "tool_attempt_id")
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_DELTA_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_COMPLETE_FIELD_NUMBER: _ClassVar[int]
    TOOL_INVOCATION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    tool_call_id: str
    tool_name: str
    arguments_delta: str
    arguments: _struct_pb2.Struct
    arguments_complete: bool
    tool_invocation_id: str
    tool_attempt_id: str
    def __init__(self, tool_call_id: _Optional[str] = ..., tool_name: _Optional[str] = ..., arguments_delta: _Optional[str] = ..., arguments: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., arguments_complete: _Optional[bool] = ..., tool_invocation_id: _Optional[str] = ..., tool_attempt_id: _Optional[str] = ...) -> None: ...

class ToolCallCompleted(_message.Message):
    __slots__ = ("tool_call_id", "tool_name", "status", "completion_reason", "arguments_complete", "tool_invocation_id", "tool_attempt_id")
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_REASON_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_COMPLETE_FIELD_NUMBER: _ClassVar[int]
    TOOL_INVOCATION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    tool_call_id: str
    tool_name: str
    status: str
    completion_reason: str
    arguments_complete: bool
    tool_invocation_id: str
    tool_attempt_id: str
    def __init__(self, tool_call_id: _Optional[str] = ..., tool_name: _Optional[str] = ..., status: _Optional[str] = ..., completion_reason: _Optional[str] = ..., arguments_complete: _Optional[bool] = ..., tool_invocation_id: _Optional[str] = ..., tool_attempt_id: _Optional[str] = ...) -> None: ...

class ToolStarted(_message.Message):
    __slots__ = ("tool_execution_id", "tool_call_id", "tool_name", "tool_invocation_id", "tool_attempt_id")
    TOOL_EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    TOOL_INVOCATION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    tool_execution_id: str
    tool_call_id: str
    tool_name: str
    tool_invocation_id: str
    tool_attempt_id: str
    def __init__(self, tool_execution_id: _Optional[str] = ..., tool_call_id: _Optional[str] = ..., tool_name: _Optional[str] = ..., tool_invocation_id: _Optional[str] = ..., tool_attempt_id: _Optional[str] = ...) -> None: ...

class ToolCompleted(_message.Message):
    __slots__ = ("tool_execution_id", "tool_call_id", "tool_name", "status", "result", "error", "outcome", "completion_reason", "tool_invocation_id", "tool_attempt_id")
    TOOL_EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_REASON_FIELD_NUMBER: _ClassVar[int]
    TOOL_INVOCATION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    tool_execution_id: str
    tool_call_id: str
    tool_name: str
    status: ToolExecutionStatus
    result: str
    error: str
    outcome: ToolExecutionOutcome
    completion_reason: str
    tool_invocation_id: str
    tool_attempt_id: str
    def __init__(self, tool_execution_id: _Optional[str] = ..., tool_call_id: _Optional[str] = ..., tool_name: _Optional[str] = ..., status: _Optional[_Union[ToolExecutionStatus, str]] = ..., result: _Optional[str] = ..., error: _Optional[str] = ..., outcome: _Optional[_Union[ToolExecutionOutcome, str]] = ..., completion_reason: _Optional[str] = ..., tool_invocation_id: _Optional[str] = ..., tool_attempt_id: _Optional[str] = ...) -> None: ...

class InterruptRequested(_message.Message):
    __slots__ = ("interrupt_request_id", "reason")
    INTERRUPT_REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    interrupt_request_id: str
    reason: str
    def __init__(self, interrupt_request_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class InterruptRejected(_message.Message):
    __slots__ = ("interrupt_request_id", "reason")
    INTERRUPT_REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    interrupt_request_id: str
    reason: str
    def __init__(self, interrupt_request_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class StreamCompleted(_message.Message):
    __slots__ = ("status",)
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: StreamStatus
    def __init__(self, status: _Optional[_Union[StreamStatus, str]] = ...) -> None: ...

class StreamInterrupted(_message.Message):
    __slots__ = ("interrupt_request_id", "status")
    INTERRUPT_REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    interrupt_request_id: str
    status: StreamStatus
    def __init__(self, interrupt_request_id: _Optional[str] = ..., status: _Optional[_Union[StreamStatus, str]] = ...) -> None: ...

class StreamFailure(_message.Message):
    __slots__ = ("code", "message", "after_interrupt_requested", "resumable")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    AFTER_INTERRUPT_REQUESTED_FIELD_NUMBER: _ClassVar[int]
    RESUMABLE_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    after_interrupt_requested: bool
    resumable: bool
    def __init__(self, code: _Optional[str] = ..., message: _Optional[str] = ..., after_interrupt_requested: _Optional[bool] = ..., resumable: _Optional[bool] = ...) -> None: ...

class MessageBlockSnapshot(_message.Message):
    __slots__ = ("block_id", "block_index", "carrier_type", "status", "text", "items", "redacted", "projection", "model_call_id", "completion_reason", "partial", "started_seq", "last_event_seq", "completed_seq", "started_at", "updated_at", "completed_at")
    BLOCK_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCK_INDEX_FIELD_NUMBER: _ClassVar[int]
    CARRIER_TYPE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    REDACTED_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_FIELD_NUMBER: _ClassVar[int]
    MODEL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_REASON_FIELD_NUMBER: _ClassVar[int]
    PARTIAL_FIELD_NUMBER: _ClassVar[int]
    STARTED_SEQ_FIELD_NUMBER: _ClassVar[int]
    LAST_EVENT_SEQ_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_SEQ_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    block_id: str
    block_index: int
    carrier_type: str
    status: str
    text: str
    items: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    redacted: bool
    projection: str
    model_call_id: str
    completion_reason: str
    partial: bool
    started_seq: int
    last_event_seq: int
    completed_seq: int
    started_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    completed_at: _timestamp_pb2.Timestamp
    def __init__(self, block_id: _Optional[str] = ..., block_index: _Optional[int] = ..., carrier_type: _Optional[str] = ..., status: _Optional[str] = ..., text: _Optional[str] = ..., items: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., redacted: _Optional[bool] = ..., projection: _Optional[str] = ..., model_call_id: _Optional[str] = ..., completion_reason: _Optional[str] = ..., partial: _Optional[bool] = ..., started_seq: _Optional[int] = ..., last_event_seq: _Optional[int] = ..., completed_seq: _Optional[int] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., completed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ToolExecutionSnapshot(_message.Message):
    __slots__ = ("tool_execution_id", "tool_call_id", "tool_name", "status", "result", "error", "outcome", "completion_reason", "started_seq", "last_event_seq", "completed_seq", "started_at", "updated_at", "completed_at", "tool_invocation_id", "tool_attempt_id")
    TOOL_EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_REASON_FIELD_NUMBER: _ClassVar[int]
    STARTED_SEQ_FIELD_NUMBER: _ClassVar[int]
    LAST_EVENT_SEQ_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_SEQ_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    TOOL_INVOCATION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    tool_execution_id: str
    tool_call_id: str
    tool_name: str
    status: ToolExecutionStatus
    result: str
    error: str
    outcome: ToolExecutionOutcome
    completion_reason: str
    started_seq: int
    last_event_seq: int
    completed_seq: int
    started_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    completed_at: _timestamp_pb2.Timestamp
    tool_invocation_id: str
    tool_attempt_id: str
    def __init__(self, tool_execution_id: _Optional[str] = ..., tool_call_id: _Optional[str] = ..., tool_name: _Optional[str] = ..., status: _Optional[_Union[ToolExecutionStatus, str]] = ..., result: _Optional[str] = ..., error: _Optional[str] = ..., outcome: _Optional[_Union[ToolExecutionOutcome, str]] = ..., completion_reason: _Optional[str] = ..., started_seq: _Optional[int] = ..., last_event_seq: _Optional[int] = ..., completed_seq: _Optional[int] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., completed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., tool_invocation_id: _Optional[str] = ..., tool_attempt_id: _Optional[str] = ...) -> None: ...

class ModelCallSnapshot(_message.Message):
    __slots__ = ("model_call_id", "attempt", "status", "outcome", "completion_reason", "retryable", "model", "started_seq", "last_event_seq", "completed_seq", "started_at", "updated_at", "completed_at")
    MODEL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_REASON_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    STARTED_SEQ_FIELD_NUMBER: _ClassVar[int]
    LAST_EVENT_SEQ_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_SEQ_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    model_call_id: str
    attempt: int
    status: str
    outcome: str
    completion_reason: str
    retryable: bool
    model: str
    started_seq: int
    last_event_seq: int
    completed_seq: int
    started_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    completed_at: _timestamp_pb2.Timestamp
    def __init__(self, model_call_id: _Optional[str] = ..., attempt: _Optional[int] = ..., status: _Optional[str] = ..., outcome: _Optional[str] = ..., completion_reason: _Optional[str] = ..., retryable: _Optional[bool] = ..., model: _Optional[str] = ..., started_seq: _Optional[int] = ..., last_event_seq: _Optional[int] = ..., completed_seq: _Optional[int] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., completed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class Activity(_message.Message):
    __slots__ = ("activity_id", "kind", "parent_activity_id", "scope_ref", "status", "outcome", "summary", "cancellable", "resumable", "side_effect_policy", "resource_refs", "detail", "detail_ref", "updated_at", "detail_available", "detail_error", "started_seq", "last_event_seq", "completed_seq", "started_at", "completed_at")
    ACTIVITY_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    PARENT_ACTIVITY_ID_FIELD_NUMBER: _ClassVar[int]
    SCOPE_REF_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    CANCELLABLE_FIELD_NUMBER: _ClassVar[int]
    RESUMABLE_FIELD_NUMBER: _ClassVar[int]
    SIDE_EFFECT_POLICY_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_REFS_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    DETAIL_REF_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    DETAIL_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    DETAIL_ERROR_FIELD_NUMBER: _ClassVar[int]
    STARTED_SEQ_FIELD_NUMBER: _ClassVar[int]
    LAST_EVENT_SEQ_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_SEQ_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    activity_id: str
    kind: str
    parent_activity_id: str
    scope_ref: str
    status: ActivityStatus
    outcome: ActivityOutcome
    summary: str
    cancellable: bool
    resumable: bool
    side_effect_policy: str
    resource_refs: _containers.RepeatedScalarFieldContainer[str]
    detail: _struct_pb2.Struct
    detail_ref: str
    updated_at: _timestamp_pb2.Timestamp
    detail_available: bool
    detail_error: str
    started_seq: int
    last_event_seq: int
    completed_seq: int
    started_at: _timestamp_pb2.Timestamp
    completed_at: _timestamp_pb2.Timestamp
    def __init__(self, activity_id: _Optional[str] = ..., kind: _Optional[str] = ..., parent_activity_id: _Optional[str] = ..., scope_ref: _Optional[str] = ..., status: _Optional[_Union[ActivityStatus, str]] = ..., outcome: _Optional[_Union[ActivityOutcome, str]] = ..., summary: _Optional[str] = ..., cancellable: _Optional[bool] = ..., resumable: _Optional[bool] = ..., side_effect_policy: _Optional[str] = ..., resource_refs: _Optional[_Iterable[str]] = ..., detail: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., detail_ref: _Optional[str] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., detail_available: _Optional[bool] = ..., detail_error: _Optional[str] = ..., started_seq: _Optional[int] = ..., last_event_seq: _Optional[int] = ..., completed_seq: _Optional[int] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., completed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ResourceRef(_message.Message):
    __slots__ = ("resource_id", "lease_id", "operation_id", "status")
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    resource_id: str
    lease_id: str
    operation_id: str
    status: str
    def __init__(self, resource_id: _Optional[str] = ..., lease_id: _Optional[str] = ..., operation_id: _Optional[str] = ..., status: _Optional[str] = ...) -> None: ...

class ActiveState(_message.Message):
    __slots__ = ("kind", "phase", "entity_id", "carrier_type", "block_id", "tool_call_id", "tool_execution_id", "activity_id", "activity_kind", "status", "last_kind", "last_phase", "reason", "detail_ref", "tool_invocation_id", "tool_attempt_id")
    KIND_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    ENTITY_ID_FIELD_NUMBER: _ClassVar[int]
    CARRIER_TYPE_FIELD_NUMBER: _ClassVar[int]
    BLOCK_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_KIND_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    LAST_KIND_FIELD_NUMBER: _ClassVar[int]
    LAST_PHASE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    DETAIL_REF_FIELD_NUMBER: _ClassVar[int]
    TOOL_INVOCATION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    kind: str
    phase: str
    entity_id: str
    carrier_type: str
    block_id: str
    tool_call_id: str
    tool_execution_id: str
    activity_id: str
    activity_kind: str
    status: str
    last_kind: str
    last_phase: str
    reason: str
    detail_ref: str
    tool_invocation_id: str
    tool_attempt_id: str
    def __init__(self, kind: _Optional[str] = ..., phase: _Optional[str] = ..., entity_id: _Optional[str] = ..., carrier_type: _Optional[str] = ..., block_id: _Optional[str] = ..., tool_call_id: _Optional[str] = ..., tool_execution_id: _Optional[str] = ..., activity_id: _Optional[str] = ..., activity_kind: _Optional[str] = ..., status: _Optional[str] = ..., last_kind: _Optional[str] = ..., last_phase: _Optional[str] = ..., reason: _Optional[str] = ..., detail_ref: _Optional[str] = ..., tool_invocation_id: _Optional[str] = ..., tool_attempt_id: _Optional[str] = ...) -> None: ...

class InterruptState(_message.Message):
    __slots__ = ("request_id", "status", "reason", "fact_confirmed")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    FACT_CONFIRMED_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    status: str
    reason: str
    fact_confirmed: bool
    def __init__(self, request_id: _Optional[str] = ..., status: _Optional[str] = ..., reason: _Optional[str] = ..., fact_confirmed: _Optional[bool] = ...) -> None: ...

class RecoveryState(_message.Message):
    __slots__ = ("status", "code", "message", "resumable")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RESUMABLE_FIELD_NUMBER: _ClassVar[int]
    status: str
    code: str
    message: str
    resumable: bool
    def __init__(self, status: _Optional[str] = ..., code: _Optional[str] = ..., message: _Optional[str] = ..., resumable: _Optional[bool] = ...) -> None: ...

class StreamSnapshot(_message.Message):
    __slots__ = ("snapshot_seq", "stream_status", "agent_loop_status", "current_model_call_id", "current_attempt", "blocks", "tool_executions", "interrupt_request_id", "interrupt_status", "failure", "resumable", "tool_calls", "model_calls", "activities", "resource_refs", "active_state", "interrupt_state", "recovery", "workspace_id")
    SNAPSHOT_SEQ_FIELD_NUMBER: _ClassVar[int]
    STREAM_STATUS_FIELD_NUMBER: _ClassVar[int]
    AGENT_LOOP_STATUS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_MODEL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    CURRENT_ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    BLOCKS_FIELD_NUMBER: _ClassVar[int]
    TOOL_EXECUTIONS_FIELD_NUMBER: _ClassVar[int]
    INTERRUPT_REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    INTERRUPT_STATUS_FIELD_NUMBER: _ClassVar[int]
    FAILURE_FIELD_NUMBER: _ClassVar[int]
    RESUMABLE_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLS_FIELD_NUMBER: _ClassVar[int]
    MODEL_CALLS_FIELD_NUMBER: _ClassVar[int]
    ACTIVITIES_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_REFS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_STATE_FIELD_NUMBER: _ClassVar[int]
    INTERRUPT_STATE_FIELD_NUMBER: _ClassVar[int]
    RECOVERY_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    snapshot_seq: int
    stream_status: StreamStatus
    agent_loop_status: str
    current_model_call_id: str
    current_attempt: int
    blocks: _containers.RepeatedCompositeFieldContainer[MessageBlockSnapshot]
    tool_executions: _containers.RepeatedCompositeFieldContainer[ToolExecutionSnapshot]
    interrupt_request_id: str
    interrupt_status: str
    failure: StreamFailure
    resumable: bool
    tool_calls: _containers.RepeatedCompositeFieldContainer[ToolCall]
    model_calls: _containers.RepeatedCompositeFieldContainer[ModelCallSnapshot]
    activities: _containers.RepeatedCompositeFieldContainer[Activity]
    resource_refs: _containers.RepeatedCompositeFieldContainer[ResourceRef]
    active_state: ActiveState
    interrupt_state: InterruptState
    recovery: RecoveryState
    workspace_id: str
    def __init__(self, snapshot_seq: _Optional[int] = ..., stream_status: _Optional[_Union[StreamStatus, str]] = ..., agent_loop_status: _Optional[str] = ..., current_model_call_id: _Optional[str] = ..., current_attempt: _Optional[int] = ..., blocks: _Optional[_Iterable[_Union[MessageBlockSnapshot, _Mapping]]] = ..., tool_executions: _Optional[_Iterable[_Union[ToolExecutionSnapshot, _Mapping]]] = ..., interrupt_request_id: _Optional[str] = ..., interrupt_status: _Optional[str] = ..., failure: _Optional[_Union[StreamFailure, _Mapping]] = ..., resumable: _Optional[bool] = ..., tool_calls: _Optional[_Iterable[_Union[ToolCall, _Mapping]]] = ..., model_calls: _Optional[_Iterable[_Union[ModelCallSnapshot, _Mapping]]] = ..., activities: _Optional[_Iterable[_Union[Activity, _Mapping]]] = ..., resource_refs: _Optional[_Iterable[_Union[ResourceRef, _Mapping]]] = ..., active_state: _Optional[_Union[ActiveState, _Mapping]] = ..., interrupt_state: _Optional[_Union[InterruptState, _Mapping]] = ..., recovery: _Optional[_Union[RecoveryState, _Mapping]] = ..., workspace_id: _Optional[str] = ...) -> None: ...

class MessageStreamEvent(_message.Message):
    __slots__ = ("event_id", "session_id", "turn_id", "turn_stream_id", "event_seq", "emitted_at", "type", "model_call_id", "block_id", "tool_execution_id", "job_id", "workspace_id", "tool_call_id", "tool_invocation_id", "tool_attempt_id", "thread_id", "stream_opened", "model_started", "model_completed", "model_retrying", "model_failed", "block_started", "block_delta", "block_completed", "tool_call", "tool_call_delta", "tool_call_completed", "tool_started", "tool_completed", "interrupt_requested", "interrupt_rejected", "stream_completed", "stream_interrupted", "stream_failed", "stream_snapshot", "activity")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    EVENT_SEQ_FIELD_NUMBER: _ClassVar[int]
    EMITTED_AT_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    MODEL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCK_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_INVOCATION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    STREAM_OPENED_FIELD_NUMBER: _ClassVar[int]
    MODEL_STARTED_FIELD_NUMBER: _ClassVar[int]
    MODEL_COMPLETED_FIELD_NUMBER: _ClassVar[int]
    MODEL_RETRYING_FIELD_NUMBER: _ClassVar[int]
    MODEL_FAILED_FIELD_NUMBER: _ClassVar[int]
    BLOCK_STARTED_FIELD_NUMBER: _ClassVar[int]
    BLOCK_DELTA_FIELD_NUMBER: _ClassVar[int]
    BLOCK_COMPLETED_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_DELTA_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_COMPLETED_FIELD_NUMBER: _ClassVar[int]
    TOOL_STARTED_FIELD_NUMBER: _ClassVar[int]
    TOOL_COMPLETED_FIELD_NUMBER: _ClassVar[int]
    INTERRUPT_REQUESTED_FIELD_NUMBER: _ClassVar[int]
    INTERRUPT_REJECTED_FIELD_NUMBER: _ClassVar[int]
    STREAM_COMPLETED_FIELD_NUMBER: _ClassVar[int]
    STREAM_INTERRUPTED_FIELD_NUMBER: _ClassVar[int]
    STREAM_FAILED_FIELD_NUMBER: _ClassVar[int]
    STREAM_SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    session_id: str
    turn_id: str
    turn_stream_id: str
    event_seq: int
    emitted_at: _timestamp_pb2.Timestamp
    type: str
    model_call_id: str
    block_id: str
    tool_execution_id: str
    job_id: str
    workspace_id: str
    tool_call_id: str
    tool_invocation_id: str
    tool_attempt_id: str
    thread_id: str
    stream_opened: StreamOpened
    model_started: ModelStarted
    model_completed: ModelCompleted
    model_retrying: ModelRetrying
    model_failed: ModelFailed
    block_started: BlockStarted
    block_delta: BlockDelta
    block_completed: BlockCompleted
    tool_call: ToolCall
    tool_call_delta: ToolCallDelta
    tool_call_completed: ToolCallCompleted
    tool_started: ToolStarted
    tool_completed: ToolCompleted
    interrupt_requested: InterruptRequested
    interrupt_rejected: InterruptRejected
    stream_completed: StreamCompleted
    stream_interrupted: StreamInterrupted
    stream_failed: StreamFailure
    stream_snapshot: StreamSnapshot
    activity: Activity
    def __init__(self, event_id: _Optional[str] = ..., session_id: _Optional[str] = ..., turn_id: _Optional[str] = ..., turn_stream_id: _Optional[str] = ..., event_seq: _Optional[int] = ..., emitted_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., type: _Optional[str] = ..., model_call_id: _Optional[str] = ..., block_id: _Optional[str] = ..., tool_execution_id: _Optional[str] = ..., job_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., tool_call_id: _Optional[str] = ..., tool_invocation_id: _Optional[str] = ..., tool_attempt_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., stream_opened: _Optional[_Union[StreamOpened, _Mapping]] = ..., model_started: _Optional[_Union[ModelStarted, _Mapping]] = ..., model_completed: _Optional[_Union[ModelCompleted, _Mapping]] = ..., model_retrying: _Optional[_Union[ModelRetrying, _Mapping]] = ..., model_failed: _Optional[_Union[ModelFailed, _Mapping]] = ..., block_started: _Optional[_Union[BlockStarted, _Mapping]] = ..., block_delta: _Optional[_Union[BlockDelta, _Mapping]] = ..., block_completed: _Optional[_Union[BlockCompleted, _Mapping]] = ..., tool_call: _Optional[_Union[ToolCall, _Mapping]] = ..., tool_call_delta: _Optional[_Union[ToolCallDelta, _Mapping]] = ..., tool_call_completed: _Optional[_Union[ToolCallCompleted, _Mapping]] = ..., tool_started: _Optional[_Union[ToolStarted, _Mapping]] = ..., tool_completed: _Optional[_Union[ToolCompleted, _Mapping]] = ..., interrupt_requested: _Optional[_Union[InterruptRequested, _Mapping]] = ..., interrupt_rejected: _Optional[_Union[InterruptRejected, _Mapping]] = ..., stream_completed: _Optional[_Union[StreamCompleted, _Mapping]] = ..., stream_interrupted: _Optional[_Union[StreamInterrupted, _Mapping]] = ..., stream_failed: _Optional[_Union[StreamFailure, _Mapping]] = ..., stream_snapshot: _Optional[_Union[StreamSnapshot, _Mapping]] = ..., activity: _Optional[_Union[Activity, _Mapping]] = ...) -> None: ...
