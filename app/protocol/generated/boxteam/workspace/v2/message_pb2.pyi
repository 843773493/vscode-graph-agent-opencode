from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class AttachmentRef(_message.Message):
    __slots__ = ("file_id", "name", "content_type", "data_url", "directory_uri")
    FILE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    DATA_URL_FIELD_NUMBER: _ClassVar[int]
    DIRECTORY_URI_FIELD_NUMBER: _ClassVar[int]
    file_id: str
    name: str
    content_type: str
    data_url: str
    directory_uri: str
    def __init__(self, file_id: _Optional[str] = ..., name: _Optional[str] = ..., content_type: _Optional[str] = ..., data_url: _Optional[str] = ..., directory_uri: _Optional[str] = ...) -> None: ...
