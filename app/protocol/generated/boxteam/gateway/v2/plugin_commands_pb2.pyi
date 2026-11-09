from google.protobuf import struct_pb2 as _struct_pb2
from boxteam.gateway.v2 import service_plugin_pb2 as _service_plugin_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PluginContributionTarget(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id", "workspace_id")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    workspace_id: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., workspace_id: _Optional[str] = ...) -> None: ...

class PluginCommandPublication(_message.Message):
    __slots__ = ("binding", "revision", "state", "error", "instance_id", "commands", "registrations", "menus", "keybindings")
    BINDING_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    COMMANDS_FIELD_NUMBER: _ClassVar[int]
    REGISTRATIONS_FIELD_NUMBER: _ClassVar[int]
    MENUS_FIELD_NUMBER: _ClassVar[int]
    KEYBINDINGS_FIELD_NUMBER: _ClassVar[int]
    binding: _service_plugin_pb2.PluginBinding
    revision: str
    state: str
    error: str
    instance_id: str
    commands: _containers.RepeatedCompositeFieldContainer[_service_plugin_pb2.PluginCommandContribution]
    registrations: _containers.RepeatedCompositeFieldContainer[PluginCommandRegistration]
    menus: _containers.RepeatedCompositeFieldContainer[_service_plugin_pb2.PluginMenuContribution]
    keybindings: _containers.RepeatedCompositeFieldContainer[_service_plugin_pb2.PluginKeybindingContribution]
    def __init__(self, binding: _Optional[_Union[_service_plugin_pb2.PluginBinding, _Mapping]] = ..., revision: _Optional[str] = ..., state: _Optional[str] = ..., error: _Optional[str] = ..., instance_id: _Optional[str] = ..., commands: _Optional[_Iterable[_Union[_service_plugin_pb2.PluginCommandContribution, _Mapping]]] = ..., registrations: _Optional[_Iterable[_Union[PluginCommandRegistration, _Mapping]]] = ..., menus: _Optional[_Iterable[_Union[_service_plugin_pb2.PluginMenuContribution, _Mapping]]] = ..., keybindings: _Optional[_Iterable[_Union[_service_plugin_pb2.PluginKeybindingContribution, _Mapping]]] = ...) -> None: ...

class PluginCommandSnapshotDTO(_message.Message):
    __slots__ = ("gateway_id", "workspace_id", "publications")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PUBLICATIONS_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    workspace_id: str
    publications: _containers.RepeatedCompositeFieldContainer[PluginCommandPublication]
    def __init__(self, gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., publications: _Optional[_Iterable[_Union[PluginCommandPublication, _Mapping]]] = ...) -> None: ...

class PluginCommandRegistration(_message.Message):
    __slots__ = ("command_id", "handler", "registration_id")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    HANDLER_FIELD_NUMBER: _ClassVar[int]
    REGISTRATION_ID_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    handler: str
    registration_id: str
    def __init__(self, command_id: _Optional[str] = ..., handler: _Optional[str] = ..., registration_id: _Optional[str] = ...) -> None: ...

class PluginCommandRegistrationsDTO(_message.Message):
    __slots__ = ("instance_id", "commands")
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    COMMANDS_FIELD_NUMBER: _ClassVar[int]
    instance_id: str
    commands: _containers.RepeatedCompositeFieldContainer[PluginCommandRegistration]
    def __init__(self, instance_id: _Optional[str] = ..., commands: _Optional[_Iterable[_Union[PluginCommandRegistration, _Mapping]]] = ...) -> None: ...

class PluginCommandInstanceRequest(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id", "package_id", "package_version", "plugin_id", "workspace_id", "instance_id")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    package_id: str
    package_version: str
    plugin_id: str
    workspace_id: str
    instance_id: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., package_id: _Optional[str] = ..., package_version: _Optional[str] = ..., plugin_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., instance_id: _Optional[str] = ...) -> None: ...

class PluginCommandCallRequest(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id", "package_id", "package_version", "plugin_id", "workspace_id", "instance_id", "registration_id", "command_id", "call_id", "arguments", "remaining_ms", "session_id", "thread_id")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    REGISTRATION_ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_FIELD_NUMBER: _ClassVar[int]
    REMAINING_MS_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    package_id: str
    package_version: str
    plugin_id: str
    workspace_id: str
    instance_id: str
    registration_id: str
    command_id: str
    call_id: str
    arguments: _struct_pb2.Struct
    remaining_ms: int
    session_id: str
    thread_id: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., package_id: _Optional[str] = ..., package_version: _Optional[str] = ..., plugin_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., instance_id: _Optional[str] = ..., registration_id: _Optional[str] = ..., command_id: _Optional[str] = ..., call_id: _Optional[str] = ..., arguments: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., remaining_ms: _Optional[int] = ..., session_id: _Optional[str] = ..., thread_id: _Optional[str] = ...) -> None: ...

class PluginCommandCancelRequest(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id", "package_id", "package_version", "plugin_id", "workspace_id", "instance_id", "registration_id", "command_id", "call_id")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    REGISTRATION_ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    package_id: str
    package_version: str
    plugin_id: str
    workspace_id: str
    instance_id: str
    registration_id: str
    command_id: str
    call_id: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., package_id: _Optional[str] = ..., package_version: _Optional[str] = ..., plugin_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., instance_id: _Optional[str] = ..., registration_id: _Optional[str] = ..., command_id: _Optional[str] = ..., call_id: _Optional[str] = ...) -> None: ...

class PluginCommandCancelDTO(_message.Message):
    __slots__ = ("instance_id", "call_id", "accepted")
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    instance_id: str
    call_id: str
    accepted: bool
    def __init__(self, instance_id: _Optional[str] = ..., call_id: _Optional[str] = ..., accepted: _Optional[bool] = ...) -> None: ...

class PluginCommandEventDTO(_message.Message):
    __slots__ = ("instance_id", "registration_id", "command_id", "call_id", "request_id", "sequence", "kind", "completed", "total", "message", "result", "error_code")
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    REGISTRATION_ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    instance_id: str
    registration_id: str
    command_id: str
    call_id: str
    request_id: str
    sequence: int
    kind: str
    completed: float
    total: float
    message: str
    result: _struct_pb2.Value
    error_code: str
    def __init__(self, instance_id: _Optional[str] = ..., registration_id: _Optional[str] = ..., command_id: _Optional[str] = ..., call_id: _Optional[str] = ..., request_id: _Optional[str] = ..., sequence: _Optional[int] = ..., kind: _Optional[str] = ..., completed: _Optional[float] = ..., total: _Optional[float] = ..., message: _Optional[str] = ..., result: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., error_code: _Optional[str] = ...) -> None: ...
