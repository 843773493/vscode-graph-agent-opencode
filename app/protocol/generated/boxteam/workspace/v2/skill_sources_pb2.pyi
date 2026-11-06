from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SkillSourcesRefreshDTO(_message.Message):
    __slots__ = ("workspace_id", "revision", "available")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    revision: str
    available: bool
    def __init__(self, workspace_id: _Optional[str] = ..., revision: _Optional[str] = ..., available: _Optional[bool] = ...) -> None: ...

class SkillSourceMetadataDTO(_message.Message):
    __slots__ = ("name", "display_uri", "available", "error_code", "priority")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_URI_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    PRIORITY_FIELD_NUMBER: _ClassVar[int]
    name: str
    display_uri: str
    available: bool
    error_code: str
    priority: int
    def __init__(self, name: _Optional[str] = ..., display_uri: _Optional[str] = ..., available: _Optional[bool] = ..., error_code: _Optional[str] = ..., priority: _Optional[int] = ...) -> None: ...

class SkillSourcesMetadataDTO(_message.Message):
    __slots__ = ("workspace_id", "entries")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    entries: _containers.RepeatedCompositeFieldContainer[SkillSourceMetadataDTO]
    def __init__(self, workspace_id: _Optional[str] = ..., entries: _Optional[_Iterable[_Union[SkillSourceMetadataDTO, _Mapping]]] = ...) -> None: ...
