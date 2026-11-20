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

class PluginToolBinding(_message.Message):
    __slots__ = ("tool_id", "contract_version", "schema_hash", "service_id")
    TOOL_ID_FIELD_NUMBER: _ClassVar[int]
    CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_HASH_FIELD_NUMBER: _ClassVar[int]
    SERVICE_ID_FIELD_NUMBER: _ClassVar[int]
    tool_id: str
    contract_version: int
    schema_hash: str
    service_id: str
    def __init__(self, tool_id: _Optional[str] = ..., contract_version: _Optional[int] = ..., schema_hash: _Optional[str] = ..., service_id: _Optional[str] = ...) -> None: ...

class PluginSkillContribution(_message.Message):
    __slots__ = ("id", "path", "tool_bindings")
    ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    TOOL_BINDINGS_FIELD_NUMBER: _ClassVar[int]
    id: str
    path: str
    tool_bindings: _containers.RepeatedCompositeFieldContainer[PluginToolBinding]
    def __init__(self, id: _Optional[str] = ..., path: _Optional[str] = ..., tool_bindings: _Optional[_Iterable[_Union[PluginToolBinding, _Mapping]]] = ...) -> None: ...

class PluginCommandContribution(_message.Message):
    __slots__ = ("id", "title", "category", "handler", "arguments_schema", "result_schema", "required_capabilities", "enablement")
    ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    HANDLER_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    RESULT_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    ENABLEMENT_FIELD_NUMBER: _ClassVar[int]
    id: str
    title: str
    category: str
    handler: str
    arguments_schema: _struct_pb2.Struct
    result_schema: _struct_pb2.Struct
    required_capabilities: _containers.RepeatedScalarFieldContainer[str]
    enablement: str
    def __init__(self, id: _Optional[str] = ..., title: _Optional[str] = ..., category: _Optional[str] = ..., handler: _Optional[str] = ..., arguments_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., result_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., required_capabilities: _Optional[_Iterable[str]] = ..., enablement: _Optional[str] = ...) -> None: ...

class PluginMenuContribution(_message.Message):
    __slots__ = ("id", "location", "command_id", "group", "order", "when")
    ID_FIELD_NUMBER: _ClassVar[int]
    LOCATION_FIELD_NUMBER: _ClassVar[int]
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    GROUP_FIELD_NUMBER: _ClassVar[int]
    ORDER_FIELD_NUMBER: _ClassVar[int]
    WHEN_FIELD_NUMBER: _ClassVar[int]
    id: str
    location: str
    command_id: str
    group: str
    order: int
    when: str
    def __init__(self, id: _Optional[str] = ..., location: _Optional[str] = ..., command_id: _Optional[str] = ..., group: _Optional[str] = ..., order: _Optional[int] = ..., when: _Optional[str] = ...) -> None: ...

class PluginKeybindingContribution(_message.Message):
    __slots__ = ("id", "command_id", "key", "mac", "linux", "win", "when")
    ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    KEY_FIELD_NUMBER: _ClassVar[int]
    MAC_FIELD_NUMBER: _ClassVar[int]
    LINUX_FIELD_NUMBER: _ClassVar[int]
    WIN_FIELD_NUMBER: _ClassVar[int]
    WHEN_FIELD_NUMBER: _ClassVar[int]
    id: str
    command_id: str
    key: str
    mac: str
    linux: str
    win: str
    when: str
    def __init__(self, id: _Optional[str] = ..., command_id: _Optional[str] = ..., key: _Optional[str] = ..., mac: _Optional[str] = ..., linux: _Optional[str] = ..., win: _Optional[str] = ..., when: _Optional[str] = ...) -> None: ...

class PluginDebuggerContribution(_message.Message):
    __slots__ = ("type", "label", "configuration_schema", "service_id", "dap_version", "capabilities")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    SERVICE_ID_FIELD_NUMBER: _ClassVar[int]
    DAP_VERSION_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    type: str
    label: str
    configuration_schema: _struct_pb2.Struct
    service_id: str
    dap_version: int
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, type: _Optional[str] = ..., label: _Optional[str] = ..., configuration_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., service_id: _Optional[str] = ..., dap_version: _Optional[int] = ..., capabilities: _Optional[_Iterable[str]] = ...) -> None: ...

class PluginContributions(_message.Message):
    __slots__ = ("commands", "menus", "keybindings", "configuration", "services", "debuggers", "skills")
    COMMANDS_FIELD_NUMBER: _ClassVar[int]
    MENUS_FIELD_NUMBER: _ClassVar[int]
    KEYBINDINGS_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_FIELD_NUMBER: _ClassVar[int]
    SERVICES_FIELD_NUMBER: _ClassVar[int]
    DEBUGGERS_FIELD_NUMBER: _ClassVar[int]
    SKILLS_FIELD_NUMBER: _ClassVar[int]
    commands: _containers.RepeatedCompositeFieldContainer[PluginCommandContribution]
    menus: _containers.RepeatedCompositeFieldContainer[PluginMenuContribution]
    keybindings: _containers.RepeatedCompositeFieldContainer[PluginKeybindingContribution]
    configuration: _struct_pb2.Struct
    services: _containers.RepeatedCompositeFieldContainer[PluginServiceContribution]
    debuggers: _containers.RepeatedCompositeFieldContainer[PluginDebuggerContribution]
    skills: _containers.RepeatedCompositeFieldContainer[PluginSkillContribution]
    def __init__(self, commands: _Optional[_Iterable[_Union[PluginCommandContribution, _Mapping]]] = ..., menus: _Optional[_Iterable[_Union[PluginMenuContribution, _Mapping]]] = ..., keybindings: _Optional[_Iterable[_Union[PluginKeybindingContribution, _Mapping]]] = ..., configuration: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., services: _Optional[_Iterable[_Union[PluginServiceContribution, _Mapping]]] = ..., debuggers: _Optional[_Iterable[_Union[PluginDebuggerContribution, _Mapping]]] = ..., skills: _Optional[_Iterable[_Union[PluginSkillContribution, _Mapping]]] = ...) -> None: ...

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
    __slots__ = ("manifest_version", "package_id", "version", "name", "description", "extension_contract_version", "plugins", "target")
    MANIFEST_VERSION_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    EXTENSION_CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    PLUGINS_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    manifest_version: int
    package_id: str
    version: str
    name: str
    description: str
    extension_contract_version: int
    plugins: _containers.RepeatedCompositeFieldContainer[ServicePluginManifest]
    target: PluginPackageTarget
    def __init__(self, manifest_version: _Optional[int] = ..., package_id: _Optional[str] = ..., version: _Optional[str] = ..., name: _Optional[str] = ..., description: _Optional[str] = ..., extension_contract_version: _Optional[int] = ..., plugins: _Optional[_Iterable[_Union[ServicePluginManifest, _Mapping]]] = ..., target: _Optional[_Union[PluginPackageTarget, _Mapping]] = ...) -> None: ...

class PluginRuntimeTarget(_message.Message):
    __slots__ = ("version", "abi")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    ABI_FIELD_NUMBER: _ClassVar[int]
    version: str
    abi: str
    def __init__(self, version: _Optional[str] = ..., abi: _Optional[str] = ...) -> None: ...

class PluginPackageTarget(_message.Message):
    __slots__ = ("os", "architecture", "system_baseline", "node", "python")
    OS_FIELD_NUMBER: _ClassVar[int]
    ARCHITECTURE_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_BASELINE_FIELD_NUMBER: _ClassVar[int]
    NODE_FIELD_NUMBER: _ClassVar[int]
    PYTHON_FIELD_NUMBER: _ClassVar[int]
    os: str
    architecture: str
    system_baseline: str
    node: PluginRuntimeTarget
    python: PluginRuntimeTarget
    def __init__(self, os: _Optional[str] = ..., architecture: _Optional[str] = ..., system_baseline: _Optional[str] = ..., node: _Optional[_Union[PluginRuntimeTarget, _Mapping]] = ..., python: _Optional[_Union[PluginRuntimeTarget, _Mapping]] = ...) -> None: ...

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

class PluginShutdownDTO(_message.Message):
    __slots__ = ("instance_id", "process_id", "accepted")
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    PROCESS_ID_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    instance_id: str
    process_id: int
    accepted: bool
    def __init__(self, instance_id: _Optional[str] = ..., process_id: _Optional[int] = ..., accepted: _Optional[bool] = ...) -> None: ...

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
    __slots__ = ("configured", "message", "entries")
    CONFIGURED_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    configured: bool
    message: str
    entries: _containers.RepeatedCompositeFieldContainer[PluginSourceConfig]
    def __init__(self, configured: _Optional[bool] = ..., message: _Optional[str] = ..., entries: _Optional[_Iterable[_Union[PluginSourceConfig, _Mapping]]] = ...) -> None: ...

class PluginSourceConfig(_message.Message):
    __slots__ = ("source_id", "protocol_version", "base_url", "enabled")
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    BASE_URL_FIELD_NUMBER: _ClassVar[int]
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    source_id: str
    protocol_version: int
    base_url: str
    enabled: bool
    def __init__(self, source_id: _Optional[str] = ..., protocol_version: _Optional[int] = ..., base_url: _Optional[str] = ..., enabled: _Optional[bool] = ...) -> None: ...

class PluginSourceArtifact(_message.Message):
    __slots__ = ("package_id", "package_version", "target", "digest", "url", "filename")
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    FILENAME_FIELD_NUMBER: _ClassVar[int]
    package_id: str
    package_version: str
    target: PluginPackageTarget
    digest: str
    url: str
    filename: str
    def __init__(self, package_id: _Optional[str] = ..., package_version: _Optional[str] = ..., target: _Optional[_Union[PluginPackageTarget, _Mapping]] = ..., digest: _Optional[str] = ..., url: _Optional[str] = ..., filename: _Optional[str] = ...) -> None: ...

class PluginSourceCatalogRequest(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id", "source_id", "query", "cursor")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    source_id: str
    query: str
    cursor: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., source_id: _Optional[str] = ..., query: _Optional[str] = ..., cursor: _Optional[str] = ...) -> None: ...

class PluginSourceCatalogDTO(_message.Message):
    __slots__ = ("gateway_id", "source_id", "protocol_version", "catalog_revision", "items", "next_cursor")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    source_id: str
    protocol_version: int
    catalog_revision: str
    items: _containers.RepeatedCompositeFieldContainer[PluginSourceArtifact]
    next_cursor: str
    def __init__(self, gateway_id: _Optional[str] = ..., source_id: _Optional[str] = ..., protocol_version: _Optional[int] = ..., catalog_revision: _Optional[str] = ..., items: _Optional[_Iterable[_Union[PluginSourceArtifact, _Mapping]]] = ..., next_cursor: _Optional[str] = ...) -> None: ...

class FrozenPluginInstallArtifact(_message.Message):
    __slots__ = ("source_kind", "package_id", "package_version", "target", "digest", "source_id", "catalog_revision", "path", "filename")
    SOURCE_KIND_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    FILENAME_FIELD_NUMBER: _ClassVar[int]
    source_kind: str
    package_id: str
    package_version: str
    target: PluginPackageTarget
    digest: str
    source_id: str
    catalog_revision: str
    path: str
    filename: str
    def __init__(self, source_kind: _Optional[str] = ..., package_id: _Optional[str] = ..., package_version: _Optional[str] = ..., target: _Optional[_Union[PluginPackageTarget, _Mapping]] = ..., digest: _Optional[str] = ..., source_id: _Optional[str] = ..., catalog_revision: _Optional[str] = ..., path: _Optional[str] = ..., filename: _Optional[str] = ...) -> None: ...

class PluginInstallOperationRequest(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id", "operation_id")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    operation_id: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., operation_id: _Optional[str] = ...) -> None: ...

class PluginInstallOperationDTO(_message.Message):
    __slots__ = ("gateway_id", "operation_id", "status", "artifact", "cancel_requested", "error")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    CANCEL_REQUESTED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    operation_id: str
    status: str
    artifact: FrozenPluginInstallArtifact
    cancel_requested: bool
    error: str
    def __init__(self, gateway_id: _Optional[str] = ..., operation_id: _Optional[str] = ..., status: _Optional[str] = ..., artifact: _Optional[_Union[FrozenPluginInstallArtifact, _Mapping]] = ..., cancel_requested: _Optional[bool] = ..., error: _Optional[str] = ...) -> None: ...

class PluginInventoryResultDTO(_message.Message):
    __slots__ = ("packages", "sources")
    PACKAGES_FIELD_NUMBER: _ClassVar[int]
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    packages: _containers.RepeatedCompositeFieldContainer[InstalledPluginPackageDTO]
    sources: PluginSourceStatusDTO
    def __init__(self, packages: _Optional[_Iterable[_Union[InstalledPluginPackageDTO, _Mapping]]] = ..., sources: _Optional[_Union[PluginSourceStatusDTO, _Mapping]] = ...) -> None: ...

class PluginSlotStatusDTO(_message.Message):
    __slots__ = ("binding", "revision", "state", "error", "declarative", "instance", "gateway_id", "instance_scope", "log_id")
    BINDING_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    DECLARATIVE_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_SCOPE_FIELD_NUMBER: _ClassVar[int]
    LOG_ID_FIELD_NUMBER: _ClassVar[int]
    binding: PluginBinding
    revision: str
    state: str
    error: str
    declarative: bool
    instance: PluginInstanceView
    gateway_id: str
    instance_scope: str
    log_id: str
    def __init__(self, binding: _Optional[_Union[PluginBinding, _Mapping]] = ..., revision: _Optional[str] = ..., state: _Optional[str] = ..., error: _Optional[str] = ..., declarative: _Optional[bool] = ..., instance: _Optional[_Union[PluginInstanceView, _Mapping]] = ..., gateway_id: _Optional[str] = ..., instance_scope: _Optional[str] = ..., log_id: _Optional[str] = ...) -> None: ...

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

class PluginConfigurationSchemasRequest(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id", "workspace_id")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    workspace_id: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., workspace_id: _Optional[str] = ...) -> None: ...

class PluginConfigurationSchemaDTO(_message.Message):
    __slots__ = ("package_id", "package_version", "plugin_id", "name", "instance_scope", "configuration_schema")
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_SCOPE_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    package_id: str
    package_version: str
    plugin_id: str
    name: str
    instance_scope: str
    configuration_schema: _struct_pb2.Struct
    def __init__(self, package_id: _Optional[str] = ..., package_version: _Optional[str] = ..., plugin_id: _Optional[str] = ..., name: _Optional[str] = ..., instance_scope: _Optional[str] = ..., configuration_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class PluginConfigurationSchemasDTO(_message.Message):
    __slots__ = ("gateway_id", "workspace_id", "plugins")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGINS_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    workspace_id: str
    plugins: _containers.RepeatedCompositeFieldContainer[PluginConfigurationSchemaDTO]
    def __init__(self, gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., plugins: _Optional[_Iterable[_Union[PluginConfigurationSchemaDTO, _Mapping]]] = ...) -> None: ...

class PluginPrivateInputReference(_message.Message):
    __slots__ = ("kind", "gateway_id", "workspace_id", "package_id", "plugin_id", "parameter_pointer", "version")
    KIND_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    PARAMETER_POINTER_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    kind: str
    gateway_id: str
    workspace_id: str
    package_id: str
    plugin_id: str
    parameter_pointer: str
    version: str
    def __init__(self, kind: _Optional[str] = ..., gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., package_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., parameter_pointer: _Optional[str] = ..., version: _Optional[str] = ...) -> None: ...

class PluginPrivateInputWriteRequest(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id", "workspace_id", "package_id", "plugin_id", "parameter_pointer", "value", "package_version", "source_key")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    PARAMETER_POINTER_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    SOURCE_KEY_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    workspace_id: str
    package_id: str
    plugin_id: str
    parameter_pointer: str
    value: _struct_pb2.Value
    package_version: str
    source_key: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., package_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., parameter_pointer: _Optional[str] = ..., value: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., package_version: _Optional[str] = ..., source_key: _Optional[str] = ...) -> None: ...

class InstallPluginRequest(_message.Message):
    __slots__ = ("gateway_id", "gateway_connection_id", "path", "development_directory", "expected_digest", "operation_id", "source_id", "artifact", "catalog_revision")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    DEVELOPMENT_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_DIGEST_FIELD_NUMBER: _ClassVar[int]
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    ARTIFACT_FIELD_NUMBER: _ClassVar[int]
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_connection_id: str
    path: str
    development_directory: bool
    expected_digest: str
    operation_id: str
    source_id: str
    artifact: PluginSourceArtifact
    catalog_revision: str
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., path: _Optional[str] = ..., development_directory: _Optional[bool] = ..., expected_digest: _Optional[str] = ..., operation_id: _Optional[str] = ..., source_id: _Optional[str] = ..., artifact: _Optional[_Union[PluginSourceArtifact, _Mapping]] = ..., catalog_revision: _Optional[str] = ...) -> None: ...

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

class PluginServiceReference(_message.Message):
    __slots__ = ("gateway_id", "package_id", "package_version", "plugin_id", "instance_id", "service_id", "workspace_id")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    SERVICE_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    package_id: str
    package_version: str
    plugin_id: str
    instance_id: str
    service_id: str
    workspace_id: str
    def __init__(self, gateway_id: _Optional[str] = ..., package_id: _Optional[str] = ..., package_version: _Optional[str] = ..., plugin_id: _Optional[str] = ..., instance_id: _Optional[str] = ..., service_id: _Optional[str] = ..., workspace_id: _Optional[str] = ...) -> None: ...
