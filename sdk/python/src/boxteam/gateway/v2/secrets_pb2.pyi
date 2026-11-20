from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SecretMetadataDTO(_message.Message):
    __slots__ = ("id", "name", "revision", "created_at", "updated_at")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    revision: int
    created_at: str
    updated_at: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., revision: _Optional[int] = ..., created_at: _Optional[str] = ..., updated_at: _Optional[str] = ...) -> None: ...

class GatewaySecretsDTO(_message.Message):
    __slots__ = ("gateway_id", "items")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    items: _containers.RepeatedCompositeFieldContainer[SecretMetadataDTO]
    def __init__(self, gateway_id: _Optional[str] = ..., items: _Optional[_Iterable[_Union[SecretMetadataDTO, _Mapping]]] = ...) -> None: ...

class GatewaySecretDTO(_message.Message):
    __slots__ = ("gateway_id", "item")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    ITEM_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    item: SecretMetadataDTO
    def __init__(self, gateway_id: _Optional[str] = ..., item: _Optional[_Union[SecretMetadataDTO, _Mapping]] = ...) -> None: ...

class CreateSecretRequest(_message.Message):
    __slots__ = ("name", "value", "id")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: str
    id: str
    def __init__(self, name: _Optional[str] = ..., value: _Optional[str] = ..., id: _Optional[str] = ...) -> None: ...

class UpdateSecretRequest(_message.Message):
    __slots__ = ("expected_revision", "name", "value")
    EXPECTED_REVISION_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    expected_revision: int
    name: str
    value: str
    def __init__(self, expected_revision: _Optional[int] = ..., name: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
