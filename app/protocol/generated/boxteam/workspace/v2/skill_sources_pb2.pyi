from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

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
