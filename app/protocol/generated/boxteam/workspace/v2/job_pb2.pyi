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

class JobStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    JOB_STATUS_UNSPECIFIED: _ClassVar[JobStatus]
    JOB_STATUS_ACCEPTED: _ClassVar[JobStatus]
    JOB_STATUS_QUEUED: _ClassVar[JobStatus]
    JOB_STATUS_RUNNING: _ClassVar[JobStatus]
    JOB_STATUS_STREAMING: _ClassVar[JobStatus]
    JOB_STATUS_WAITING_INPUT: _ClassVar[JobStatus]
    JOB_STATUS_PAUSED: _ClassVar[JobStatus]
    JOB_STATUS_INTERRUPT_PENDING: _ClassVar[JobStatus]
    JOB_STATUS_CANCELLING: _ClassVar[JobStatus]
    JOB_STATUS_COMPLETED: _ClassVar[JobStatus]
    JOB_STATUS_SUCCEEDED: _ClassVar[JobStatus]
    JOB_STATUS_FAILED: _ClassVar[JobStatus]
    JOB_STATUS_CANCELLED: _ClassVar[JobStatus]
    JOB_STATUS_TIMED_OUT: _ClassVar[JobStatus]

class RunMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RUN_MODE_UNSPECIFIED: _ClassVar[RunMode]
    RUN_MODE_SINGLE_AGENT: _ClassVar[RunMode]
    RUN_MODE_MULTI_AGENT: _ClassVar[RunMode]
JOB_STATUS_UNSPECIFIED: JobStatus
JOB_STATUS_ACCEPTED: JobStatus
JOB_STATUS_QUEUED: JobStatus
JOB_STATUS_RUNNING: JobStatus
JOB_STATUS_STREAMING: JobStatus
JOB_STATUS_WAITING_INPUT: JobStatus
JOB_STATUS_PAUSED: JobStatus
JOB_STATUS_INTERRUPT_PENDING: JobStatus
JOB_STATUS_CANCELLING: JobStatus
JOB_STATUS_COMPLETED: JobStatus
JOB_STATUS_SUCCEEDED: JobStatus
JOB_STATUS_FAILED: JobStatus
JOB_STATUS_CANCELLED: JobStatus
JOB_STATUS_TIMED_OUT: JobStatus
RUN_MODE_UNSPECIFIED: RunMode
RUN_MODE_SINGLE_AGENT: RunMode
RUN_MODE_MULTI_AGENT: RunMode

class Job(_message.Message):
    __slots__ = ("job_id", "message_id", "session_id", "mode", "status", "entry_agent", "progress", "current_step", "error_message", "metadata", "created_at", "updated_at", "ended_at", "thread_id")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ENTRY_AGENT_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_STEP_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    message_id: str
    session_id: str
    mode: RunMode
    status: JobStatus
    entry_agent: str
    progress: int
    current_step: str
    error_message: str
    metadata: _struct_pb2.Struct
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    ended_at: _timestamp_pb2.Timestamp
    thread_id: str
    def __init__(self, job_id: _Optional[str] = ..., message_id: _Optional[str] = ..., session_id: _Optional[str] = ..., mode: _Optional[_Union[RunMode, str]] = ..., status: _Optional[_Union[JobStatus, str]] = ..., entry_agent: _Optional[str] = ..., progress: _Optional[int] = ..., current_step: _Optional[str] = ..., error_message: _Optional[str] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., ended_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., thread_id: _Optional[str] = ...) -> None: ...

class JobProgress(_message.Message):
    __slots__ = ("job_id", "status", "current_step_id", "progress", "message")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_STEP_ID_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    status: JobStatus
    current_step_id: str
    progress: int
    message: str
    def __init__(self, job_id: _Optional[str] = ..., status: _Optional[_Union[JobStatus, str]] = ..., current_step_id: _Optional[str] = ..., progress: _Optional[int] = ..., message: _Optional[str] = ...) -> None: ...

class ToolApprovalAction(_message.Message):
    __slots__ = ("tool_call_id", "name", "args", "description", "allowed_decisions")
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    ALLOWED_DECISIONS_FIELD_NUMBER: _ClassVar[int]
    tool_call_id: str
    name: str
    args: _struct_pb2.Struct
    description: str
    allowed_decisions: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, tool_call_id: _Optional[str] = ..., name: _Optional[str] = ..., args: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., description: _Optional[str] = ..., allowed_decisions: _Optional[_Iterable[str]] = ...) -> None: ...

class ToolApproval(_message.Message):
    __slots__ = ("approval_id", "checkpoint_id", "job_id", "session_id", "thread_id", "actions")
    APPROVAL_ID_FIELD_NUMBER: _ClassVar[int]
    CHECKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIONS_FIELD_NUMBER: _ClassVar[int]
    approval_id: str
    checkpoint_id: str
    job_id: str
    session_id: str
    thread_id: str
    actions: _containers.RepeatedCompositeFieldContainer[ToolApprovalAction]
    def __init__(self, approval_id: _Optional[str] = ..., checkpoint_id: _Optional[str] = ..., job_id: _Optional[str] = ..., session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., actions: _Optional[_Iterable[_Union[ToolApprovalAction, _Mapping]]] = ...) -> None: ...

class ToolApprovalEditedAction(_message.Message):
    __slots__ = ("name", "args")
    NAME_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    name: str
    args: _struct_pb2.Struct
    def __init__(self, name: _Optional[str] = ..., args: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ToolApprovalDecision(_message.Message):
    __slots__ = ("tool_call_id", "type", "edited_action", "message")
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    EDITED_ACTION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    tool_call_id: str
    type: str
    edited_action: ToolApprovalEditedAction
    message: str
    def __init__(self, tool_call_id: _Optional[str] = ..., type: _Optional[str] = ..., edited_action: _Optional[_Union[ToolApprovalEditedAction, _Mapping]] = ..., message: _Optional[str] = ...) -> None: ...

class ToolApprovalResponse(_message.Message):
    __slots__ = ("approval_id", "decisions")
    APPROVAL_ID_FIELD_NUMBER: _ClassVar[int]
    DECISIONS_FIELD_NUMBER: _ClassVar[int]
    approval_id: str
    decisions: _containers.RepeatedCompositeFieldContainer[ToolApprovalDecision]
    def __init__(self, approval_id: _Optional[str] = ..., decisions: _Optional[_Iterable[_Union[ToolApprovalDecision, _Mapping]]] = ...) -> None: ...

class ToolApprovalSubmitRequest(_message.Message):
    __slots__ = ("command_id", "checkpoint_id", "responses")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    CHECKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    RESPONSES_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    checkpoint_id: str
    responses: _containers.RepeatedCompositeFieldContainer[ToolApprovalResponse]
    def __init__(self, command_id: _Optional[str] = ..., checkpoint_id: _Optional[str] = ..., responses: _Optional[_Iterable[_Union[ToolApprovalResponse, _Mapping]]] = ...) -> None: ...

class ToolApprovalCommand(_message.Message):
    __slots__ = ("command_id", "checkpoint_id", "status")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    CHECKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    checkpoint_id: str
    status: str
    def __init__(self, command_id: _Optional[str] = ..., checkpoint_id: _Optional[str] = ..., status: _Optional[str] = ...) -> None: ...
