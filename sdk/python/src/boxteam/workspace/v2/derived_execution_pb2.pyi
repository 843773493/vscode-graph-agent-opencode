import datetime

from boxteam.workspace.v2 import public_pb2 as _public_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class DerivedExecutionSourceDTO(_message.Message):
    __slots__ = ("item_ids",)
    ITEM_IDS_FIELD_NUMBER: _ClassVar[int]
    item_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, item_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class DerivedExecutionSubmitRequest(_message.Message):
    __slots__ = ("session_id", "thread_id", "idempotency_key", "instruction", "source", "allowed_tool_ids", "model", "timeout_seconds", "max_model_calls", "foreground")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTION_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    ALLOWED_TOOL_IDS_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    MAX_MODEL_CALLS_FIELD_NUMBER: _ClassVar[int]
    FOREGROUND_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    thread_id: str
    idempotency_key: str
    instruction: str
    source: DerivedExecutionSourceDTO
    allowed_tool_ids: _containers.RepeatedScalarFieldContainer[str]
    model: _public_pb2.ModelReferenceDTO
    timeout_seconds: float
    max_model_calls: int
    foreground: bool
    def __init__(self, session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., idempotency_key: _Optional[str] = ..., instruction: _Optional[str] = ..., source: _Optional[_Union[DerivedExecutionSourceDTO, _Mapping]] = ..., allowed_tool_ids: _Optional[_Iterable[str]] = ..., model: _Optional[_Union[_public_pb2.ModelReferenceDTO, _Mapping]] = ..., timeout_seconds: _Optional[float] = ..., max_model_calls: _Optional[int] = ..., foreground: _Optional[bool] = ...) -> None: ...

class DerivedExecutionScopeRequest(_message.Message):
    __slots__ = ("session_id", "thread_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    thread_id: str
    def __init__(self, session_id: _Optional[str] = ..., thread_id: _Optional[str] = ...) -> None: ...

class DerivedItemReferenceDTO(_message.Message):
    __slots__ = ("session_id", "thread_id", "item_id", "content_hash")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    thread_id: str
    item_id: str
    content_hash: str
    def __init__(self, session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., item_id: _Optional[str] = ..., content_hash: _Optional[str] = ...) -> None: ...

class DerivedDispatchReferenceDTO(_message.Message):
    __slots__ = ("assembly_id", "model_call_id", "request_hash", "reference_hash")
    ASSEMBLY_ID_FIELD_NUMBER: _ClassVar[int]
    MODEL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_HASH_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_HASH_FIELD_NUMBER: _ClassVar[int]
    assembly_id: str
    model_call_id: str
    request_hash: str
    reference_hash: str
    def __init__(self, assembly_id: _Optional[str] = ..., model_call_id: _Optional[str] = ..., request_hash: _Optional[str] = ..., reference_hash: _Optional[str] = ...) -> None: ...

class DerivedExecutionResultDTO(_message.Message):
    __slots__ = ("execution_id", "dispatch_ref", "output_refs", "output_ref_count", "output_refs_truncated")
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    DISPATCH_REF_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_REFS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_REF_COUNT_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_REFS_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    execution_id: str
    dispatch_ref: DerivedDispatchReferenceDTO
    output_refs: _containers.RepeatedCompositeFieldContainer[DerivedItemReferenceDTO]
    output_ref_count: int
    output_refs_truncated: bool
    def __init__(self, execution_id: _Optional[str] = ..., dispatch_ref: _Optional[_Union[DerivedDispatchReferenceDTO, _Mapping]] = ..., output_refs: _Optional[_Iterable[_Union[DerivedItemReferenceDTO, _Mapping]]] = ..., output_ref_count: _Optional[int] = ..., output_refs_truncated: _Optional[bool] = ...) -> None: ...

class DerivedExecutionDTO(_message.Message):
    __slots__ = ("task_id", "session_id", "thread_id", "status", "created_at", "started_at", "ended_at", "result", "source_view_id", "source_last_ref", "error")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_LAST_REF_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    session_id: str
    thread_id: str
    status: str
    created_at: _timestamp_pb2.Timestamp
    started_at: _timestamp_pb2.Timestamp
    ended_at: _timestamp_pb2.Timestamp
    result: DerivedExecutionResultDTO
    source_view_id: str
    source_last_ref: DerivedItemReferenceDTO
    error: str
    def __init__(self, task_id: _Optional[str] = ..., session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., status: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., ended_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., result: _Optional[_Union[DerivedExecutionResultDTO, _Mapping]] = ..., source_view_id: _Optional[str] = ..., source_last_ref: _Optional[_Union[DerivedItemReferenceDTO, _Mapping]] = ..., error: _Optional[str] = ...) -> None: ...
