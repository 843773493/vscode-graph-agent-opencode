from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GatewaySkillSourceEntryDTO(_message.Message):
    __slots__ = ("name", "entry_identity", "source_id", "display_uri", "revision", "byte_length", "content", "available", "error_code", "layer", "priority")
    NAME_FIELD_NUMBER: _ClassVar[int]
    ENTRY_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_URI_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    BYTE_LENGTH_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    LAYER_FIELD_NUMBER: _ClassVar[int]
    PRIORITY_FIELD_NUMBER: _ClassVar[int]
    name: str
    entry_identity: str
    source_id: str
    display_uri: str
    revision: str
    byte_length: int
    content: str
    available: bool
    error_code: str
    layer: str
    priority: int
    def __init__(self, name: _Optional[str] = ..., entry_identity: _Optional[str] = ..., source_id: _Optional[str] = ..., display_uri: _Optional[str] = ..., revision: _Optional[str] = ..., byte_length: _Optional[int] = ..., content: _Optional[str] = ..., available: _Optional[bool] = ..., error_code: _Optional[str] = ..., layer: _Optional[str] = ..., priority: _Optional[int] = ...) -> None: ...

class GatewaySkillSourcesDTO(_message.Message):
    __slots__ = ("gateway_id", "workspace_id", "revision", "entries", "available", "error_code")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    workspace_id: str
    revision: str
    entries: _containers.RepeatedCompositeFieldContainer[GatewaySkillSourceEntryDTO]
    available: bool
    error_code: str
    def __init__(self, gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., revision: _Optional[str] = ..., entries: _Optional[_Iterable[_Union[GatewaySkillSourceEntryDTO, _Mapping]]] = ..., available: _Optional[bool] = ..., error_code: _Optional[str] = ...) -> None: ...
