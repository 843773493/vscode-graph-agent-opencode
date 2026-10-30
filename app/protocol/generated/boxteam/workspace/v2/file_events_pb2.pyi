from boxteam.workspace.v2 import public_pb2 as _public_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class WorkspaceFileChange(_message.Message):
    __slots__ = ("kind", "path")
    KIND_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    kind: str
    path: str
    def __init__(self, kind: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class WorkspaceFileChangeBatch(_message.Message):
    __slots__ = ("changes", "overflow", "directories", "event_id", "invalidated_roots", "snapshots")
    CHANGES_FIELD_NUMBER: _ClassVar[int]
    OVERFLOW_FIELD_NUMBER: _ClassVar[int]
    DIRECTORIES_FIELD_NUMBER: _ClassVar[int]
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    INVALIDATED_ROOTS_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOTS_FIELD_NUMBER: _ClassVar[int]
    changes: _containers.RepeatedCompositeFieldContainer[WorkspaceFileChange]
    overflow: bool
    directories: _containers.RepeatedCompositeFieldContainer[_public_pb2.WorkspaceDirectoryRevisionDTO]
    event_id: int
    invalidated_roots: _containers.RepeatedScalarFieldContainer[str]
    snapshots: _containers.RepeatedCompositeFieldContainer[_public_pb2.WorkspaceFileListDTO]
    def __init__(self, changes: _Optional[_Iterable[_Union[WorkspaceFileChange, _Mapping]]] = ..., overflow: _Optional[bool] = ..., directories: _Optional[_Iterable[_Union[_public_pb2.WorkspaceDirectoryRevisionDTO, _Mapping]]] = ..., event_id: _Optional[int] = ..., invalidated_roots: _Optional[_Iterable[str]] = ..., snapshots: _Optional[_Iterable[_Union[_public_pb2.WorkspaceFileListDTO, _Mapping]]] = ...) -> None: ...

class SseError(_message.Message):
    __slots__ = ("message",)
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    message: str
    def __init__(self, message: _Optional[str] = ...) -> None: ...
