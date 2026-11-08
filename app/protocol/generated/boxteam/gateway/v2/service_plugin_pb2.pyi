from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PluginExtensionRequirement(_message.Message):
    __slots__ = ("extension_point", "version")
    EXTENSION_POINT_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    extension_point: str
    version: int
    def __init__(self, extension_point: _Optional[str] = ..., version: _Optional[int] = ...) -> None: ...

class PluginServiceContribution(_message.Message):
    __slots__ = ("service_id", "service_type", "protocol")
    SERVICE_ID_FIELD_NUMBER: _ClassVar[int]
    SERVICE_TYPE_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    service_id: str
    service_type: str
    protocol: str
    def __init__(self, service_id: _Optional[str] = ..., service_type: _Optional[str] = ..., protocol: _Optional[str] = ...) -> None: ...

class PluginSkillContribution(_message.Message):
    __slots__ = ("id", "path", "tool_bindings")
    ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    TOOL_BINDINGS_FIELD_NUMBER: _ClassVar[int]
    id: str
    path: str
    tool_bindings: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    def __init__(self, id: _Optional[str] = ..., path: _Optional[str] = ..., tool_bindings: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ...) -> None: ...

class PluginContributions(_message.Message):
    __slots__ = ("commands", "menus", "keybindings", "configuration", "services", "debuggers", "skills")
    COMMANDS_FIELD_NUMBER: _ClassVar[int]
    MENUS_FIELD_NUMBER: _ClassVar[int]
    KEYBINDINGS_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_FIELD_NUMBER: _ClassVar[int]
    SERVICES_FIELD_NUMBER: _ClassVar[int]
    DEBUGGERS_FIELD_NUMBER: _ClassVar[int]
    SKILLS_FIELD_NUMBER: _ClassVar[int]
    commands: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    menus: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    keybindings: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    configuration: _struct_pb2.Struct
    services: _containers.RepeatedCompositeFieldContainer[PluginServiceContribution]
    debuggers: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    skills: _containers.RepeatedCompositeFieldContainer[PluginSkillContribution]
    def __init__(self, commands: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., menus: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., keybindings: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., configuration: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., services: _Optional[_Iterable[_Union[PluginServiceContribution, _Mapping]]] = ..., debuggers: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., skills: _Optional[_Iterable[_Union[PluginSkillContribution, _Mapping]]] = ...) -> None: ...

class ServicePluginManifest(_message.Message):
    __slots__ = ("id", "name", "instance_scope", "requires", "contributes", "runtime", "entrypoint", "management_protocol_version")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_SCOPE_FIELD_NUMBER: _ClassVar[int]
    REQUIRES_FIELD_NUMBER: _ClassVar[int]
    CONTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_FIELD_NUMBER: _ClassVar[int]
    ENTRYPOINT_FIELD_NUMBER: _ClassVar[int]
    MANAGEMENT_PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    instance_scope: str
    requires: _containers.RepeatedCompositeFieldContainer[PluginExtensionRequirement]
    contributes: PluginContributions
    runtime: str
    entrypoint: _containers.RepeatedScalarFieldContainer[str]
    management_protocol_version: int
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., instance_scope: _Optional[str] = ..., requires: _Optional[_Iterable[_Union[PluginExtensionRequirement, _Mapping]]] = ..., contributes: _Optional[_Union[PluginContributions, _Mapping]] = ..., runtime: _Optional[str] = ..., entrypoint: _Optional[_Iterable[str]] = ..., management_protocol_version: _Optional[int] = ...) -> None: ...

class PluginPackageManifest(_message.Message):
    __slots__ = ("manifest_version", "package_id", "version", "name", "description", "extension_contract_version", "plugins")
    MANIFEST_VERSION_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    EXTENSION_CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    PLUGINS_FIELD_NUMBER: _ClassVar[int]
    manifest_version: int
    package_id: str
    version: str
    name: str
    description: str
    extension_contract_version: int
    plugins: _containers.RepeatedCompositeFieldContainer[ServicePluginManifest]
    def __init__(self, manifest_version: _Optional[int] = ..., package_id: _Optional[str] = ..., version: _Optional[str] = ..., name: _Optional[str] = ..., description: _Optional[str] = ..., extension_contract_version: _Optional[int] = ..., plugins: _Optional[_Iterable[_Union[ServicePluginManifest, _Mapping]]] = ...) -> None: ...

class PluginBinding(_message.Message):
    __slots__ = ("package_id", "plugin_id", "package_version", "workspace_id", "enabled")
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    package_id: str
    plugin_id: str
    package_version: str
    workspace_id: str
    enabled: bool
    def __init__(self, package_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., package_version: _Optional[str] = ..., workspace_id: _Optional[str] = ..., enabled: _Optional[bool] = ...) -> None: ...

class PluginInstanceStatus(_message.Message):
    __slots__ = ("package_id", "package_version", "plugin_id", "instance_scope", "workspace_id", "instance_id", "management_protocol_version", "extension_contract_version", "status", "process_id", "log_sink", "capabilities", "services", "gateway_id")
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_SCOPE_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    MANAGEMENT_PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    EXTENSION_CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PROCESS_ID_FIELD_NUMBER: _ClassVar[int]
    LOG_SINK_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    SERVICES_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    package_id: str
    package_version: str
    plugin_id: str
    instance_scope: str
    workspace_id: str
    instance_id: str
    management_protocol_version: int
    extension_contract_version: int
    status: str
    process_id: int
    log_sink: _struct_pb2.Struct
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    services: _containers.RepeatedCompositeFieldContainer[PluginServiceContribution]
    gateway_id: str
    def __init__(self, package_id: _Optional[str] = ..., package_version: _Optional[str] = ..., plugin_id: _Optional[str] = ..., instance_scope: _Optional[str] = ..., workspace_id: _Optional[str] = ..., instance_id: _Optional[str] = ..., management_protocol_version: _Optional[int] = ..., extension_contract_version: _Optional[int] = ..., status: _Optional[str] = ..., process_id: _Optional[int] = ..., log_sink: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., capabilities: _Optional[_Iterable[str]] = ..., services: _Optional[_Iterable[_Union[PluginServiceContribution, _Mapping]]] = ..., gateway_id: _Optional[str] = ...) -> None: ...

class PluginInstanceView(_message.Message):
    __slots__ = ("gateway_id", "package_id", "package_version", "plugin_id", "instance_scope", "workspace_id", "instance_id", "management_protocol_version", "extension_contract_version", "status", "process_id", "capabilities", "services")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_SCOPE_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    MANAGEMENT_PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    EXTENSION_CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PROCESS_ID_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    SERVICES_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    package_id: str
    package_version: str
    plugin_id: str
    instance_scope: str
    workspace_id: str
    instance_id: str
    management_protocol_version: int
    extension_contract_version: int
    status: str
    process_id: int
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    services: _containers.RepeatedCompositeFieldContainer[PluginServiceContribution]
    def __init__(self, gateway_id: _Optional[str] = ..., package_id: _Optional[str] = ..., package_version: _Optional[str] = ..., plugin_id: _Optional[str] = ..., instance_scope: _Optional[str] = ..., workspace_id: _Optional[str] = ..., instance_id: _Optional[str] = ..., management_protocol_version: _Optional[int] = ..., extension_contract_version: _Optional[int] = ..., status: _Optional[str] = ..., process_id: _Optional[int] = ..., capabilities: _Optional[_Iterable[str]] = ..., services: _Optional[_Iterable[_Union[PluginServiceContribution, _Mapping]]] = ...) -> None: ...

class InstalledPluginPackageDTO(_message.Message):
    __slots__ = ("manifest", "digest")
    MANIFEST_FIELD_NUMBER: _ClassVar[int]
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    manifest: PluginPackageManifest
    digest: str
    def __init__(self, manifest: _Optional[_Union[PluginPackageManifest, _Mapping]] = ..., digest: _Optional[str] = ...) -> None: ...

class PluginSourceStatusDTO(_message.Message):
    __slots__ = ("configured", "message")
    CONFIGURED_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    configured: bool
    message: str
    def __init__(self, configured: _Optional[bool] = ..., message: _Optional[str] = ...) -> None: ...

class PluginInventoryResultDTO(_message.Message):
    __slots__ = ("packages", "sources")
    PACKAGES_FIELD_NUMBER: _ClassVar[int]
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    packages: _containers.RepeatedCompositeFieldContainer[InstalledPluginPackageDTO]
    sources: PluginSourceStatusDTO
    def __init__(self, packages: _Optional[_Iterable[_Union[InstalledPluginPackageDTO, _Mapping]]] = ..., sources: _Optional[_Union[PluginSourceStatusDTO, _Mapping]] = ...) -> None: ...

class PluginSlotStatusDTO(_message.Message):
    __slots__ = ("binding", "revision", "state", "error", "declarative", "instance", "gateway_id", "instance_scope")
    BINDING_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    DECLARATIVE_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_SCOPE_FIELD_NUMBER: _ClassVar[int]
    binding: PluginBinding
    revision: str
    state: str
    error: str
    declarative: bool
    instance: PluginInstanceView
    gateway_id: str
    instance_scope: str
    def __init__(self, binding: _Optional[_Union[PluginBinding, _Mapping]] = ..., revision: _Optional[str] = ..., state: _Optional[str] = ..., error: _Optional[str] = ..., declarative: _Optional[bool] = ..., instance: _Optional[_Union[PluginInstanceView, _Mapping]] = ..., gateway_id: _Optional[str] = ..., instance_scope: _Optional[str] = ...) -> None: ...

class PluginRuntimeStatusDTO(_message.Message):
    __slots__ = ("plugins", "configured_bindings", "workspace_configurations")
    PLUGINS_FIELD_NUMBER: _ClassVar[int]
    CONFIGURED_BINDINGS_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_CONFIGURATIONS_FIELD_NUMBER: _ClassVar[int]
    plugins: _containers.RepeatedCompositeFieldContainer[PluginSlotStatusDTO]
    configured_bindings: _containers.RepeatedCompositeFieldContainer[PluginBinding]
    workspace_configurations: _containers.RepeatedCompositeFieldContainer[WorkspacePluginConfigurationDTO]
    def __init__(self, plugins: _Optional[_Iterable[_Union[PluginSlotStatusDTO, _Mapping]]] = ..., configured_bindings: _Optional[_Iterable[_Union[PluginBinding, _Mapping]]] = ..., workspace_configurations: _Optional[_Iterable[_Union[WorkspacePluginConfigurationDTO, _Mapping]]] = ...) -> None: ...

class WorkspacePluginConfigurationDTO(_message.Message):
    __slots__ = ("workspace_id", "state", "revision", "error")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    state: str
    revision: str
    error: str
    def __init__(self, workspace_id: _Optional[str] = ..., state: _Optional[str] = ..., revision: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class PluginUninstallResultDTO(_message.Message):
    __slots__ = ("package_id", "package_version", "uninstalled")
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    UNINSTALLED_FIELD_NUMBER: _ClassVar[int]
    package_id: str
    package_version: str
    uninstalled: bool
    def __init__(self, package_id: _Optional[str] = ..., package_version: _Optional[str] = ..., uninstalled: _Optional[bool] = ...) -> None: ...

class PluginTarget(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ...) -> None: ...

class InstallPluginRequest(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id", "path", "development_directory", "expected_digest")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    DEVELOPMENT_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_DIGEST_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    path: str
    development_directory: bool
    expected_digest: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., path: _Optional[str] = ..., development_directory: _Optional[bool] = ..., expected_digest: _Optional[str] = ...) -> None: ...

class PluginVersionRequest(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id", "package_id", "package_version")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    package_id: str
    package_version: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., package_id: _Optional[str] = ..., package_version: _Optional[str] = ...) -> None: ...

class RestartPluginRequest(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id", "package_id", "package_version", "plugin_id", "revision", "workspace_id")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    package_id: str
    package_version: str
    plugin_id: str
    revision: str
    workspace_id: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., package_id: _Optional[str] = ..., package_version: _Optional[str] = ..., plugin_id: _Optional[str] = ..., revision: _Optional[str] = ..., workspace_id: _Optional[str] = ...) -> None: ...
