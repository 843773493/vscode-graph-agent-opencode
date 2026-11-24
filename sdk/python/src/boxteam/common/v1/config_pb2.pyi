from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class ConfigNodeInspectionDTO(_message.Message):
    __slots__ = ("node_id", "parent_id", "source_key", "source_uri", "definition_path", "reference")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    PARENT_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_KEY_FIELD_NUMBER: _ClassVar[int]
    SOURCE_URI_FIELD_NUMBER: _ClassVar[int]
    DEFINITION_PATH_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    parent_id: str
    source_key: str
    source_uri: str
    definition_path: str
    reference: str
    def __init__(self, node_id: _Optional[str] = ..., parent_id: _Optional[str] = ..., source_key: _Optional[str] = ..., source_uri: _Optional[str] = ..., definition_path: _Optional[str] = ..., reference: _Optional[str] = ...) -> None: ...
