import datetime

from boxteam.gateway.v2 import service_plugin_pb2 as _service_plugin_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GatewayResourceAddressRequest(_message.Message):
    __slots__ = ("uri", "local_gateway_id", "resource_gateway_id")
    URI_FIELD_NUMBER: _ClassVar[int]
    LOCAL_GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    uri: str
    local_gateway_id: str
    resource_gateway_id: str
    def __init__(self, uri: _Optional[str] = ..., local_gateway_id: _Optional[str] = ..., resource_gateway_id: _Optional[str] = ...) -> None: ...

class GatewayResourceAddressDTO(_message.Message):
    __slots__ = ("uri", "gateway_id", "scope", "scope_id", "kind", "file_root_uri", "session_id", "thread_id", "reference_uri")
    URI_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    SCOPE_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    FILE_ROOT_URI_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_URI_FIELD_NUMBER: _ClassVar[int]
    uri: str
    gateway_id: str
    scope: str
    scope_id: str
    kind: str
    file_root_uri: str
    session_id: str
    thread_id: str
    reference_uri: str
    def __init__(self, uri: _Optional[str] = ..., gateway_id: _Optional[str] = ..., scope: _Optional[str] = ..., scope_id: _Optional[str] = ..., kind: _Optional[str] = ..., file_root_uri: _Optional[str] = ..., session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., reference_uri: _Optional[str] = ...) -> None: ...

class AcquireGatewayUserRequest(_message.Message):
    __slots__ = ("client_label",)
    CLIENT_LABEL_FIELD_NUMBER: _ClassVar[int]
    client_label: str
    def __init__(self, client_label: _Optional[str] = ...) -> None: ...

class NativeDirectoryBindRequest(_message.Message):
    __slots__ = ("native_root",)
    NATIVE_ROOT_FIELD_NUMBER: _ClassVar[int]
    native_root: str
    def __init__(self, native_root: _Optional[str] = ...) -> None: ...

class NativeDirectoryBindingDTO(_message.Message):
    __slots__ = ("uri",)
    URI_FIELD_NUMBER: _ClassVar[int]
    uri: str
    def __init__(self, uri: _Optional[str] = ...) -> None: ...

class NativeDirectoryResolveDTO(_message.Message):
    __slots__ = ("uri", "native_root")
    URI_FIELD_NUMBER: _ClassVar[int]
    NATIVE_ROOT_FIELD_NUMBER: _ClassVar[int]
    uri: str
    native_root: str
    def __init__(self, uri: _Optional[str] = ..., native_root: _Optional[str] = ...) -> None: ...

class ActivateGatewayWorkspaceResultDTO(_message.Message):
    __slots__ = ("active_workspace",)
    ACTIVE_WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    active_workspace: GatewaySearchWorkspaceOwnerDTO
    def __init__(self, active_workspace: _Optional[_Union[GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ...) -> None: ...

class AddLocalWorkspaceRequest(_message.Message):
    __slots__ = ("root_path", "name", "backend_url")
    ROOT_PATH_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    BACKEND_URL_FIELD_NUMBER: _ClassVar[int]
    root_path: str
    name: str
    backend_url: str
    def __init__(self, root_path: _Optional[str] = ..., name: _Optional[str] = ..., backend_url: _Optional[str] = ...) -> None: ...

class AddRemoteGatewayRequest(_message.Message):
    __slots__ = ("name", "ssh_config_host", "host", "port", "username", "private_key_path", "remote_gateway_port", "connection_id")
    NAME_FIELD_NUMBER: _ClassVar[int]
    SSH_CONFIG_HOST_FIELD_NUMBER: _ClassVar[int]
    HOST_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    PRIVATE_KEY_PATH_FIELD_NUMBER: _ClassVar[int]
    REMOTE_GATEWAY_PORT_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    name: str
    ssh_config_host: str
    host: str
    port: int
    username: str
    private_key_path: str
    remote_gateway_port: int
    connection_id: str
    def __init__(self, name: _Optional[str] = ..., ssh_config_host: _Optional[str] = ..., host: _Optional[str] = ..., port: _Optional[int] = ..., username: _Optional[str] = ..., private_key_path: _Optional[str] = ..., remote_gateway_port: _Optional[int] = ..., connection_id: _Optional[str] = ...) -> None: ...

class ChangePortForwardLabelRequest(_message.Message):
    __slots__ = ("label",)
    LABEL_FIELD_NUMBER: _ClassVar[int]
    label: str
    def __init__(self, label: _Optional[str] = ...) -> None: ...

class ChangePortForwardLocalPortRequest(_message.Message):
    __slots__ = ("local_port",)
    LOCAL_PORT_FIELD_NUMBER: _ClassVar[int]
    local_port: int
    def __init__(self, local_port: _Optional[int] = ...) -> None: ...

class CreateFederationManagedWorkspaceRequest(_message.Message):
    __slots__ = ("root_path", "name", "create_directory")
    ROOT_PATH_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CREATE_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    root_path: str
    name: str
    create_directory: bool
    def __init__(self, root_path: _Optional[str] = ..., name: _Optional[str] = ..., create_directory: _Optional[bool] = ...) -> None: ...

class CreateGatewayGuestRequest(_message.Message):
    __slots__ = ("tracking",)
    TRACKING_FIELD_NUMBER: _ClassVar[int]
    tracking: _struct_pb2.Struct
    def __init__(self, tracking: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class CreateGatewayManagedWorkspaceRequest(_message.Message):
    __slots__ = ("root_path", "name", "create_directory", "gateway_connection_id")
    ROOT_PATH_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CREATE_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    root_path: str
    name: str
    create_directory: bool
    gateway_connection_id: str
    def __init__(self, root_path: _Optional[str] = ..., name: _Optional[str] = ..., create_directory: _Optional[bool] = ..., gateway_connection_id: _Optional[str] = ...) -> None: ...

class CreateGatewayUserRequest(_message.Message):
    __slots__ = ("display_name", "user_id")
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    display_name: str
    user_id: str
    def __init__(self, display_name: _Optional[str] = ..., user_id: _Optional[str] = ...) -> None: ...

class CreatePortForwardRequest(_message.Message):
    __slots__ = ("remote_port", "local_port", "protocol", "label")
    REMOTE_PORT_FIELD_NUMBER: _ClassVar[int]
    LOCAL_PORT_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    remote_port: int
    local_port: int
    protocol: str
    label: str
    def __init__(self, remote_port: _Optional[int] = ..., local_port: _Optional[int] = ..., protocol: _Optional[str] = ..., label: _Optional[str] = ...) -> None: ...

class DevelopmentRuntimeRestartDTO(_message.Message):
    __slots__ = ("status", "previous_process_id", "helper_process_id", "delay_ms")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_PROCESS_ID_FIELD_NUMBER: _ClassVar[int]
    HELPER_PROCESS_ID_FIELD_NUMBER: _ClassVar[int]
    DELAY_MS_FIELD_NUMBER: _ClassVar[int]
    status: str
    previous_process_id: int
    helper_process_id: int
    delay_ms: int
    def __init__(self, status: _Optional[str] = ..., previous_process_id: _Optional[int] = ..., helper_process_id: _Optional[int] = ..., delay_ms: _Optional[int] = ...) -> None: ...

class FederationProtocolManifestDTO(_message.Message):
    __slots__ = ("protocol_version", "gateway_id", "federation_depth", "capabilities", "config_event_cursor", "config_reload_state", "config_reload_restart_required", "config_reload_candidate_ref")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    FEDERATION_DEPTH_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    CONFIG_EVENT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    CONFIG_RELOAD_STATE_FIELD_NUMBER: _ClassVar[int]
    CONFIG_RELOAD_RESTART_REQUIRED_FIELD_NUMBER: _ClassVar[int]
    CONFIG_RELOAD_CANDIDATE_REF_FIELD_NUMBER: _ClassVar[int]
    protocol_version: int
    gateway_id: str
    federation_depth: int
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    config_event_cursor: int
    config_reload_state: str
    config_reload_restart_required: bool
    config_reload_candidate_ref: str
    def __init__(self, protocol_version: _Optional[int] = ..., gateway_id: _Optional[str] = ..., federation_depth: _Optional[int] = ..., capabilities: _Optional[_Iterable[str]] = ..., config_event_cursor: _Optional[int] = ..., config_reload_state: _Optional[str] = ..., config_reload_restart_required: _Optional[bool] = ..., config_reload_candidate_ref: _Optional[str] = ...) -> None: ...

class FederationWorkspaceDTO(_message.Message):
    __slots__ = ("workspace_id", "name", "managed", "connection_kind", "services", "backend_generation", "affected_workspace_ids", "backend_id")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    MANAGED_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_KIND_FIELD_NUMBER: _ClassVar[int]
    SERVICES_FIELD_NUMBER: _ClassVar[int]
    BACKEND_GENERATION_FIELD_NUMBER: _ClassVar[int]
    AFFECTED_WORKSPACE_IDS_FIELD_NUMBER: _ClassVar[int]
    BACKEND_ID_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    name: str
    managed: bool
    connection_kind: str
    services: _containers.RepeatedScalarFieldContainer[str]
    backend_generation: str
    affected_workspace_ids: _containers.RepeatedScalarFieldContainer[str]
    backend_id: str
    def __init__(self, workspace_id: _Optional[str] = ..., name: _Optional[str] = ..., managed: _Optional[bool] = ..., connection_kind: _Optional[str] = ..., services: _Optional[_Iterable[str]] = ..., backend_generation: _Optional[str] = ..., affected_workspace_ids: _Optional[_Iterable[str]] = ..., backend_id: _Optional[str] = ...) -> None: ...

class FederationWorkspaceListDTO(_message.Message):
    __slots__ = ("protocol_version", "gateway_id", "items", "excluded", "identities_verified")
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    EXCLUDED_FIELD_NUMBER: _ClassVar[int]
    IDENTITIES_VERIFIED_FIELD_NUMBER: _ClassVar[int]
    protocol_version: int
    gateway_id: str
    items: _containers.RepeatedCompositeFieldContainer[FederationWorkspaceDTO]
    excluded: _containers.RepeatedScalarFieldContainer[str]
    identities_verified: bool
    def __init__(self, protocol_version: _Optional[int] = ..., gateway_id: _Optional[str] = ..., items: _Optional[_Iterable[_Union[FederationWorkspaceDTO, _Mapping]]] = ..., excluded: _Optional[_Iterable[str]] = ..., identities_verified: _Optional[bool] = ...) -> None: ...

class GatewayConfigReloadStatusDTO(_message.Message):
    __slots__ = ("available", "healthy", "revision", "restart_required", "reason", "changed_sections", "last_error", "error", "state", "active_revision", "pending_revision", "candidate_id", "candidate_ref", "attempt_id", "apply_id", "layer_digests", "applied_paths", "deferred_paths")
    class LayerDigestsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _struct_pb2.Value
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ...) -> None: ...
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    HEALTHY_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    RESTART_REQUIRED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    CHANGED_SECTIONS_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_REVISION_FIELD_NUMBER: _ClassVar[int]
    PENDING_REVISION_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_ID_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_REF_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    APPLY_ID_FIELD_NUMBER: _ClassVar[int]
    LAYER_DIGESTS_FIELD_NUMBER: _ClassVar[int]
    APPLIED_PATHS_FIELD_NUMBER: _ClassVar[int]
    DEFERRED_PATHS_FIELD_NUMBER: _ClassVar[int]
    available: bool
    healthy: bool
    revision: str
    restart_required: bool
    reason: str
    changed_sections: _containers.RepeatedScalarFieldContainer[str]
    last_error: str
    error: str
    state: str
    active_revision: int
    pending_revision: int
    candidate_id: str
    candidate_ref: str
    attempt_id: str
    apply_id: str
    layer_digests: _containers.MessageMap[str, _struct_pb2.Value]
    applied_paths: _containers.RepeatedScalarFieldContainer[str]
    deferred_paths: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, available: _Optional[bool] = ..., healthy: _Optional[bool] = ..., revision: _Optional[str] = ..., restart_required: _Optional[bool] = ..., reason: _Optional[str] = ..., changed_sections: _Optional[_Iterable[str]] = ..., last_error: _Optional[str] = ..., error: _Optional[str] = ..., state: _Optional[str] = ..., active_revision: _Optional[int] = ..., pending_revision: _Optional[int] = ..., candidate_id: _Optional[str] = ..., candidate_ref: _Optional[str] = ..., attempt_id: _Optional[str] = ..., apply_id: _Optional[str] = ..., layer_digests: _Optional[_Mapping[str, _struct_pb2.Value]] = ..., applied_paths: _Optional[_Iterable[str]] = ..., deferred_paths: _Optional[_Iterable[str]] = ...) -> None: ...

class GatewayConfigPendingHealthProofRequest(_message.Message):
    __slots__ = ("config_domain", "candidate_ref", "candidate_id", "generation", "loaded_source", "active_revision", "pending_revision", "candidate_digest", "effective_digest", "secret_binding_digest", "fencing_token_digest", "health_digest", "gateway_id", "consumer_health_digests")
    class ConsumerHealthDigestsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    CONFIG_DOMAIN_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_REF_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    LOADED_SOURCE_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_REVISION_FIELD_NUMBER: _ClassVar[int]
    PENDING_REVISION_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    SECRET_BINDING_DIGEST_FIELD_NUMBER: _ClassVar[int]
    FENCING_TOKEN_DIGEST_FIELD_NUMBER: _ClassVar[int]
    HEALTH_DIGEST_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    CONSUMER_HEALTH_DIGESTS_FIELD_NUMBER: _ClassVar[int]
    config_domain: str
    candidate_ref: str
    candidate_id: str
    generation: str
    loaded_source: str
    active_revision: int
    pending_revision: int
    candidate_digest: str
    effective_digest: str
    secret_binding_digest: str
    fencing_token_digest: str
    health_digest: str
    gateway_id: str
    consumer_health_digests: _containers.ScalarMap[str, str]
    def __init__(self, config_domain: _Optional[str] = ..., candidate_ref: _Optional[str] = ..., candidate_id: _Optional[str] = ..., generation: _Optional[str] = ..., loaded_source: _Optional[str] = ..., active_revision: _Optional[int] = ..., pending_revision: _Optional[int] = ..., candidate_digest: _Optional[str] = ..., effective_digest: _Optional[str] = ..., secret_binding_digest: _Optional[str] = ..., fencing_token_digest: _Optional[str] = ..., health_digest: _Optional[str] = ..., gateway_id: _Optional[str] = ..., consumer_health_digests: _Optional[_Mapping[str, str]] = ...) -> None: ...

class GatewayConfigPendingDiscardRequest(_message.Message):
    __slots__ = ("expected_active_revision", "expected_active_digest")
    EXPECTED_ACTIVE_REVISION_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_ACTIVE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    expected_active_revision: int
    expected_active_digest: str
    def __init__(self, expected_active_revision: _Optional[int] = ..., expected_active_digest: _Optional[str] = ...) -> None: ...

class GatewayConfigSourceEditRequest(_message.Message):
    __slots__ = ("gateway_id", "source_key", "jsonc", "base_file_digest", "base_layer_revision", "base_layer_digest", "expected_active_revision", "expected_active_digest", "idempotency_key")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_KEY_FIELD_NUMBER: _ClassVar[int]
    JSONC_FIELD_NUMBER: _ClassVar[int]
    BASE_FILE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    BASE_LAYER_REVISION_FIELD_NUMBER: _ClassVar[int]
    BASE_LAYER_DIGEST_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_ACTIVE_REVISION_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_ACTIVE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    source_key: str
    jsonc: str
    base_file_digest: _struct_pb2.Value
    base_layer_revision: _struct_pb2.Value
    base_layer_digest: _struct_pb2.Value
    expected_active_revision: _struct_pb2.Value
    expected_active_digest: _struct_pb2.Value
    idempotency_key: str
    def __init__(self, gateway_id: _Optional[str] = ..., source_key: _Optional[str] = ..., jsonc: _Optional[str] = ..., base_file_digest: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., base_layer_revision: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., base_layer_digest: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., expected_active_revision: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., expected_active_digest: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., idempotency_key: _Optional[str] = ...) -> None: ...

class GatewayConfigSourceLayerViewDTO(_message.Message):
    __slots__ = ("source_key", "layer", "precedence", "writable", "payload")
    SOURCE_KEY_FIELD_NUMBER: _ClassVar[int]
    LAYER_FIELD_NUMBER: _ClassVar[int]
    PRECEDENCE_FIELD_NUMBER: _ClassVar[int]
    WRITABLE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    source_key: str
    layer: str
    precedence: int
    writable: bool
    payload: _struct_pb2.Struct
    def __init__(self, source_key: _Optional[str] = ..., layer: _Optional[str] = ..., precedence: _Optional[int] = ..., writable: _Optional[bool] = ..., payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class GatewayConfigSourceOriginDTO(_message.Message):
    __slots__ = ("path", "source_key", "shadowed_sources")
    PATH_FIELD_NUMBER: _ClassVar[int]
    SOURCE_KEY_FIELD_NUMBER: _ClassVar[int]
    SHADOWED_SOURCES_FIELD_NUMBER: _ClassVar[int]
    path: str
    source_key: str
    shadowed_sources: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, path: _Optional[str] = ..., source_key: _Optional[str] = ..., shadowed_sources: _Optional[_Iterable[str]] = ...) -> None: ...

class GatewayConfigSourceEditDTO(_message.Message):
    __slots__ = ("idempotency_key", "candidate_id", "state", "file_digest", "source_saved", "layer_imported")
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    FILE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    SOURCE_SAVED_FIELD_NUMBER: _ClassVar[int]
    LAYER_IMPORTED_FIELD_NUMBER: _ClassVar[int]
    idempotency_key: str
    candidate_id: str
    state: str
    file_digest: str
    source_saved: bool
    layer_imported: bool
    def __init__(self, idempotency_key: _Optional[str] = ..., candidate_id: _Optional[str] = ..., state: _Optional[str] = ..., file_digest: _Optional[str] = ..., source_saved: _Optional[bool] = ..., layer_imported: _Optional[bool] = ...) -> None: ...

class GatewayConfigSourceDocumentDTO(_message.Message):
    __slots__ = ("gateway_id", "source_key", "layer", "writable", "jsonc", "file_presence", "file_digest", "layer_revision", "layer_digest", "source_generation", "active_revision", "active_digest", "json_schema", "effective", "source_effective", "layers", "origins", "status", "edit")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_KEY_FIELD_NUMBER: _ClassVar[int]
    LAYER_FIELD_NUMBER: _ClassVar[int]
    WRITABLE_FIELD_NUMBER: _ClassVar[int]
    JSONC_FIELD_NUMBER: _ClassVar[int]
    FILE_PRESENCE_FIELD_NUMBER: _ClassVar[int]
    FILE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    LAYER_REVISION_FIELD_NUMBER: _ClassVar[int]
    LAYER_DIGEST_FIELD_NUMBER: _ClassVar[int]
    SOURCE_GENERATION_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_REVISION_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    JSON_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_EFFECTIVE_FIELD_NUMBER: _ClassVar[int]
    LAYERS_FIELD_NUMBER: _ClassVar[int]
    ORIGINS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    EDIT_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    source_key: str
    layer: str
    writable: bool
    jsonc: str
    file_presence: str
    file_digest: _struct_pb2.Value
    layer_revision: _struct_pb2.Value
    layer_digest: _struct_pb2.Value
    source_generation: _struct_pb2.Value
    active_revision: _struct_pb2.Value
    active_digest: _struct_pb2.Value
    json_schema: _struct_pb2.Struct
    effective: _struct_pb2.Struct
    source_effective: _struct_pb2.Struct
    layers: _containers.RepeatedCompositeFieldContainer[GatewayConfigSourceLayerViewDTO]
    origins: _containers.RepeatedCompositeFieldContainer[GatewayConfigSourceOriginDTO]
    status: GatewayConfigReloadStatusDTO
    edit: GatewayConfigSourceEditDTO
    def __init__(self, gateway_id: _Optional[str] = ..., source_key: _Optional[str] = ..., layer: _Optional[str] = ..., writable: _Optional[bool] = ..., jsonc: _Optional[str] = ..., file_presence: _Optional[str] = ..., file_digest: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., layer_revision: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., layer_digest: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., source_generation: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., active_revision: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., active_digest: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., json_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., effective: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., source_effective: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., layers: _Optional[_Iterable[_Union[GatewayConfigSourceLayerViewDTO, _Mapping]]] = ..., origins: _Optional[_Iterable[_Union[GatewayConfigSourceOriginDTO, _Mapping]]] = ..., status: _Optional[_Union[GatewayConfigReloadStatusDTO, _Mapping]] = ..., edit: _Optional[_Union[GatewayConfigSourceEditDTO, _Mapping]] = ...) -> None: ...

class GatewayConfigSourcePreflightDTO(_message.Message):
    __slots__ = ("document", "candidate", "candidate_digest", "changed_paths")
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    CHANGED_PATHS_FIELD_NUMBER: _ClassVar[int]
    document: GatewayConfigSourceDocumentDTO
    candidate: _struct_pb2.Struct
    candidate_digest: str
    changed_paths: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, document: _Optional[_Union[GatewayConfigSourceDocumentDTO, _Mapping]] = ..., candidate: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., candidate_digest: _Optional[str] = ..., changed_paths: _Optional[_Iterable[str]] = ...) -> None: ...

class GatewayConfigEventDTO(_message.Message):
    __slots__ = ("event_seq", "event_id", "config_domain", "candidate_id", "attempt_id", "apply_id", "idempotency_key", "commit_revision", "active_revision", "pending_revision", "source", "result", "activation_scope", "changed_paths", "applied_paths", "deferred_paths", "error", "occurred_at")
    EVENT_SEQ_FIELD_NUMBER: _ClassVar[int]
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIG_DOMAIN_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_ID_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    APPLY_ID_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    COMMIT_REVISION_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_REVISION_FIELD_NUMBER: _ClassVar[int]
    PENDING_REVISION_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ACTIVATION_SCOPE_FIELD_NUMBER: _ClassVar[int]
    CHANGED_PATHS_FIELD_NUMBER: _ClassVar[int]
    APPLIED_PATHS_FIELD_NUMBER: _ClassVar[int]
    DEFERRED_PATHS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    OCCURRED_AT_FIELD_NUMBER: _ClassVar[int]
    event_seq: int
    event_id: str
    config_domain: str
    candidate_id: str
    attempt_id: str
    apply_id: str
    idempotency_key: str
    commit_revision: int
    active_revision: int
    pending_revision: int
    source: str
    result: str
    activation_scope: str
    changed_paths: _containers.RepeatedScalarFieldContainer[str]
    applied_paths: _containers.RepeatedScalarFieldContainer[str]
    deferred_paths: _containers.RepeatedScalarFieldContainer[str]
    error: str
    occurred_at: str
    def __init__(self, event_seq: _Optional[int] = ..., event_id: _Optional[str] = ..., config_domain: _Optional[str] = ..., candidate_id: _Optional[str] = ..., attempt_id: _Optional[str] = ..., apply_id: _Optional[str] = ..., idempotency_key: _Optional[str] = ..., commit_revision: _Optional[int] = ..., active_revision: _Optional[int] = ..., pending_revision: _Optional[int] = ..., source: _Optional[str] = ..., result: _Optional[str] = ..., activation_scope: _Optional[str] = ..., changed_paths: _Optional[_Iterable[str]] = ..., applied_paths: _Optional[_Iterable[str]] = ..., deferred_paths: _Optional[_Iterable[str]] = ..., error: _Optional[str] = ..., occurred_at: _Optional[str] = ...) -> None: ...

class GatewayConfigEventsDTO(_message.Message):
    __slots__ = ("cursor", "events", "has_more")
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    HAS_MORE_FIELD_NUMBER: _ClassVar[int]
    cursor: int
    events: _containers.RepeatedCompositeFieldContainer[GatewayConfigEventDTO]
    has_more: bool
    def __init__(self, cursor: _Optional[int] = ..., events: _Optional[_Iterable[_Union[GatewayConfigEventDTO, _Mapping]]] = ..., has_more: _Optional[bool] = ...) -> None: ...

class GatewayConfigSourceDTO(_message.Message):
    __slots__ = ("layer", "precedence", "loaded", "source_key", "presence", "layer_revision", "layer_digest", "source_generation", "uri")
    LAYER_FIELD_NUMBER: _ClassVar[int]
    PRECEDENCE_FIELD_NUMBER: _ClassVar[int]
    LOADED_FIELD_NUMBER: _ClassVar[int]
    SOURCE_KEY_FIELD_NUMBER: _ClassVar[int]
    PRESENCE_FIELD_NUMBER: _ClassVar[int]
    LAYER_REVISION_FIELD_NUMBER: _ClassVar[int]
    LAYER_DIGEST_FIELD_NUMBER: _ClassVar[int]
    SOURCE_GENERATION_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    layer: str
    precedence: int
    loaded: bool
    source_key: str
    presence: str
    layer_revision: int
    layer_digest: str
    source_generation: int
    uri: str
    def __init__(self, layer: _Optional[str] = ..., precedence: _Optional[int] = ..., loaded: _Optional[bool] = ..., source_key: _Optional[str] = ..., presence: _Optional[str] = ..., layer_revision: _Optional[int] = ..., layer_digest: _Optional[str] = ..., source_generation: _Optional[int] = ..., uri: _Optional[str] = ...) -> None: ...

class GatewayConfigSourcesDTO(_message.Message):
    __slots__ = ("revision", "sources", "schema_uri", "policy_manifest")
    REVISION_FIELD_NUMBER: _ClassVar[int]
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_URI_FIELD_NUMBER: _ClassVar[int]
    POLICY_MANIFEST_FIELD_NUMBER: _ClassVar[int]
    revision: str
    sources: _containers.RepeatedCompositeFieldContainer[GatewayConfigSourceDTO]
    schema_uri: str
    policy_manifest: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    def __init__(self, revision: _Optional[str] = ..., sources: _Optional[_Iterable[_Union[GatewayConfigSourceDTO, _Mapping]]] = ..., schema_uri: _Optional[str] = ..., policy_manifest: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ...) -> None: ...

class GatewayDiagnosticLogDTO(_message.Message):
    __slots__ = ("log_id", "source", "workspace_id", "workspace_name", "service", "label", "status", "tail", "truncated", "line_count", "size_bytes", "updated_at", "error")
    LOG_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_NAME_FIELD_NUMBER: _ClassVar[int]
    SERVICE_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TAIL_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    LINE_COUNT_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    log_id: str
    source: str
    workspace_id: str
    workspace_name: str
    service: str
    label: str
    status: str
    tail: str
    truncated: bool
    line_count: int
    size_bytes: int
    updated_at: str
    error: str
    def __init__(self, log_id: _Optional[str] = ..., source: _Optional[str] = ..., workspace_id: _Optional[str] = ..., workspace_name: _Optional[str] = ..., service: _Optional[str] = ..., label: _Optional[str] = ..., status: _Optional[str] = ..., tail: _Optional[str] = ..., truncated: _Optional[bool] = ..., line_count: _Optional[int] = ..., size_bytes: _Optional[int] = ..., updated_at: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class GatewayDiagnosticWorkspaceDTO(_message.Message):
    __slots__ = ("workspace_id", "name", "connection_kind", "status", "managed", "system_default", "connection_error")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_KIND_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MANAGED_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_DEFAULT_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ERROR_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    name: str
    connection_kind: str
    status: str
    managed: bool
    system_default: bool
    connection_error: str
    def __init__(self, workspace_id: _Optional[str] = ..., name: _Optional[str] = ..., connection_kind: _Optional[str] = ..., status: _Optional[str] = ..., managed: _Optional[bool] = ..., system_default: _Optional[bool] = ..., connection_error: _Optional[str] = ...) -> None: ...

class GatewayDiagnosticLogSinkDTO(_message.Message):
    __slots__ = ("log_id", "owner_id", "role", "source", "device", "inode")
    LOG_ID_FIELD_NUMBER: _ClassVar[int]
    OWNER_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    INODE_FIELD_NUMBER: _ClassVar[int]
    log_id: str
    owner_id: str
    role: str
    source: str
    device: str
    inode: str
    def __init__(self, log_id: _Optional[str] = ..., owner_id: _Optional[str] = ..., role: _Optional[str] = ..., source: _Optional[str] = ..., device: _Optional[str] = ..., inode: _Optional[str] = ...) -> None: ...

class GatewayDiagnosticLoggingStateDTO(_message.Message):
    __slots__ = ("service", "pid", "runtime_instance_id", "applied_revision", "level", "pretty", "log_sink")
    SERVICE_FIELD_NUMBER: _ClassVar[int]
    PID_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    APPLIED_REVISION_FIELD_NUMBER: _ClassVar[int]
    LEVEL_FIELD_NUMBER: _ClassVar[int]
    PRETTY_FIELD_NUMBER: _ClassVar[int]
    LOG_SINK_FIELD_NUMBER: _ClassVar[int]
    service: str
    pid: int
    runtime_instance_id: str
    applied_revision: str
    level: str
    pretty: bool
    log_sink: GatewayDiagnosticLogSinkDTO
    def __init__(self, service: _Optional[str] = ..., pid: _Optional[int] = ..., runtime_instance_id: _Optional[str] = ..., applied_revision: _Optional[str] = ..., level: _Optional[str] = ..., pretty: _Optional[bool] = ..., log_sink: _Optional[_Union[GatewayDiagnosticLogSinkDTO, _Mapping]] = ...) -> None: ...

class GatewayDiagnosticProcessDTO(_message.Message):
    __slots__ = ("service", "workspace_ids", "state", "output_mode", "error")
    SERVICE_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_IDS_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_MODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    service: str
    workspace_ids: _containers.RepeatedScalarFieldContainer[str]
    state: GatewayDiagnosticLoggingStateDTO
    output_mode: str
    error: str
    def __init__(self, service: _Optional[str] = ..., workspace_ids: _Optional[_Iterable[str]] = ..., state: _Optional[_Union[GatewayDiagnosticLoggingStateDTO, _Mapping]] = ..., output_mode: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class TelemetryRuntimeSettings(_message.Message):
    __slots__ = ("enabled", "sample_ratio", "sample_buffer_limit")
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_RATIO_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_BUFFER_LIMIT_FIELD_NUMBER: _ClassVar[int]
    enabled: bool
    sample_ratio: float
    sample_buffer_limit: int
    def __init__(self, enabled: _Optional[bool] = ..., sample_ratio: _Optional[float] = ..., sample_buffer_limit: _Optional[int] = ...) -> None: ...

class StatisticsRuntimeSettings(_message.Message):
    __slots__ = ("enabled", "collection_interval_ms")
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    COLLECTION_INTERVAL_MS_FIELD_NUMBER: _ClassVar[int]
    enabled: bool
    collection_interval_ms: int
    def __init__(self, enabled: _Optional[bool] = ..., collection_interval_ms: _Optional[int] = ...) -> None: ...

class RuntimeRequestSample(_message.Message):
    __slots__ = ("kind", "status_code", "duration_ms", "failed")
    KIND_FIELD_NUMBER: _ClassVar[int]
    STATUS_CODE_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    FAILED_FIELD_NUMBER: _ClassVar[int]
    kind: str
    status_code: int
    duration_ms: float
    failed: bool
    def __init__(self, kind: _Optional[str] = ..., status_code: _Optional[int] = ..., duration_ms: _Optional[float] = ..., failed: _Optional[bool] = ...) -> None: ...

class RuntimeRequestStatistics(_message.Message):
    __slots__ = ("count", "errors", "duration_ms", "proxy_count", "proxy_errors", "proxy_duration_ms")
    COUNT_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    PROXY_COUNT_FIELD_NUMBER: _ClassVar[int]
    PROXY_ERRORS_FIELD_NUMBER: _ClassVar[int]
    PROXY_DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    count: int
    errors: int
    duration_ms: float
    proxy_count: int
    proxy_errors: int
    proxy_duration_ms: float
    def __init__(self, count: _Optional[int] = ..., errors: _Optional[int] = ..., duration_ms: _Optional[float] = ..., proxy_count: _Optional[int] = ..., proxy_errors: _Optional[int] = ..., proxy_duration_ms: _Optional[float] = ...) -> None: ...

class RuntimeProcessResources(_message.Message):
    __slots__ = ("service", "pid", "rss_bytes", "cpu_seconds", "error")
    SERVICE_FIELD_NUMBER: _ClassVar[int]
    PID_FIELD_NUMBER: _ClassVar[int]
    RSS_BYTES_FIELD_NUMBER: _ClassVar[int]
    CPU_SECONDS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    service: str
    pid: int
    rss_bytes: int
    cpu_seconds: float
    error: str
    def __init__(self, service: _Optional[str] = ..., pid: _Optional[int] = ..., rss_bytes: _Optional[int] = ..., cpu_seconds: _Optional[float] = ..., error: _Optional[str] = ...) -> None: ...

class RuntimeObservationStatus(_message.Message):
    __slots__ = ("instance_id", "applied_revision", "telemetry", "statistics", "requests", "samples", "processes", "collection_error", "unsupported_metrics")
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    APPLIED_REVISION_FIELD_NUMBER: _ClassVar[int]
    TELEMETRY_FIELD_NUMBER: _ClassVar[int]
    STATISTICS_FIELD_NUMBER: _ClassVar[int]
    REQUESTS_FIELD_NUMBER: _ClassVar[int]
    SAMPLES_FIELD_NUMBER: _ClassVar[int]
    PROCESSES_FIELD_NUMBER: _ClassVar[int]
    COLLECTION_ERROR_FIELD_NUMBER: _ClassVar[int]
    UNSUPPORTED_METRICS_FIELD_NUMBER: _ClassVar[int]
    instance_id: str
    applied_revision: str
    telemetry: TelemetryRuntimeSettings
    statistics: StatisticsRuntimeSettings
    requests: RuntimeRequestStatistics
    samples: _containers.RepeatedCompositeFieldContainer[RuntimeRequestSample]
    processes: _containers.RepeatedCompositeFieldContainer[RuntimeProcessResources]
    collection_error: str
    unsupported_metrics: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, instance_id: _Optional[str] = ..., applied_revision: _Optional[str] = ..., telemetry: _Optional[_Union[TelemetryRuntimeSettings, _Mapping]] = ..., statistics: _Optional[_Union[StatisticsRuntimeSettings, _Mapping]] = ..., requests: _Optional[_Union[RuntimeRequestStatistics, _Mapping]] = ..., samples: _Optional[_Iterable[_Union[RuntimeRequestSample, _Mapping]]] = ..., processes: _Optional[_Iterable[_Union[RuntimeProcessResources, _Mapping]]] = ..., collection_error: _Optional[str] = ..., unsupported_metrics: _Optional[_Iterable[str]] = ...) -> None: ...

class RuntimeObservationApplyRequest(_message.Message):
    __slots__ = ("instance_id", "revision", "telemetry", "statistics")
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    TELEMETRY_FIELD_NUMBER: _ClassVar[int]
    STATISTICS_FIELD_NUMBER: _ClassVar[int]
    instance_id: str
    revision: str
    telemetry: TelemetryRuntimeSettings
    statistics: StatisticsRuntimeSettings
    def __init__(self, instance_id: _Optional[str] = ..., revision: _Optional[str] = ..., telemetry: _Optional[_Union[TelemetryRuntimeSettings, _Mapping]] = ..., statistics: _Optional[_Union[StatisticsRuntimeSettings, _Mapping]] = ...) -> None: ...

class PluginRuntimeObservationDTO(_message.Message):
    __slots__ = ("instance", "observation", "error")
    INSTANCE_FIELD_NUMBER: _ClassVar[int]
    OBSERVATION_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    instance: _service_plugin_pb2.PluginInstanceView
    observation: RuntimeObservationStatus
    error: str
    def __init__(self, instance: _Optional[_Union[_service_plugin_pb2.PluginInstanceView, _Mapping]] = ..., observation: _Optional[_Union[RuntimeObservationStatus, _Mapping]] = ..., error: _Optional[str] = ...) -> None: ...

class GatewayDiagnosticsDTO(_message.Message):
    __slots__ = ("gateway_id", "gateway_name", "gateway_connection_id", "connection_kind", "status", "checked_at", "selected_workspace_id", "selected_log_id", "workspaces", "logs", "processes", "observation", "plugin_observations")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_NAME_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_KIND_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CHECKED_AT_FIELD_NUMBER: _ClassVar[int]
    SELECTED_WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SELECTED_LOG_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACES_FIELD_NUMBER: _ClassVar[int]
    LOGS_FIELD_NUMBER: _ClassVar[int]
    PROCESSES_FIELD_NUMBER: _ClassVar[int]
    OBSERVATION_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_OBSERVATIONS_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    gateway_name: str
    gateway_connection_id: str
    connection_kind: str
    status: str
    checked_at: str
    selected_workspace_id: str
    selected_log_id: str
    workspaces: _containers.RepeatedCompositeFieldContainer[GatewayDiagnosticWorkspaceDTO]
    logs: _containers.RepeatedCompositeFieldContainer[GatewayDiagnosticLogDTO]
    processes: _containers.RepeatedCompositeFieldContainer[GatewayDiagnosticProcessDTO]
    observation: RuntimeObservationStatus
    plugin_observations: _containers.RepeatedCompositeFieldContainer[PluginRuntimeObservationDTO]
    def __init__(self, gateway_id: _Optional[str] = ..., gateway_name: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., connection_kind: _Optional[str] = ..., status: _Optional[str] = ..., checked_at: _Optional[str] = ..., selected_workspace_id: _Optional[str] = ..., selected_log_id: _Optional[str] = ..., workspaces: _Optional[_Iterable[_Union[GatewayDiagnosticWorkspaceDTO, _Mapping]]] = ..., logs: _Optional[_Iterable[_Union[GatewayDiagnosticLogDTO, _Mapping]]] = ..., processes: _Optional[_Iterable[_Union[GatewayDiagnosticProcessDTO, _Mapping]]] = ..., observation: _Optional[_Union[RuntimeObservationStatus, _Mapping]] = ..., plugin_observations: _Optional[_Iterable[_Union[PluginRuntimeObservationDTO, _Mapping]]] = ...) -> None: ...

class GatewayDiagnosticLogQueryScopeDTO(_message.Message):
    __slots__ = ("source", "workspace_ids")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_IDS_FIELD_NUMBER: _ClassVar[int]
    source: str
    workspace_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, source: _Optional[str] = ..., workspace_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class GatewayDiagnosticLogQueryRequest(_message.Message):
    __slots__ = ("scope", "query", "limit", "cursor", "deadline_at")
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_AT_FIELD_NUMBER: _ClassVar[int]
    scope: GatewayDiagnosticLogQueryScopeDTO
    query: str
    limit: int
    cursor: str
    deadline_at: float
    def __init__(self, scope: _Optional[_Union[GatewayDiagnosticLogQueryScopeDTO, _Mapping]] = ..., query: _Optional[str] = ..., limit: _Optional[int] = ..., cursor: _Optional[str] = ..., deadline_at: _Optional[float] = ...) -> None: ...

class GatewayDiagnosticLogQuerySourceDTO(_message.Message):
    __slots__ = ("log_id", "source", "service", "label", "workspace_ids", "status", "error")
    LOG_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    SERVICE_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_IDS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    log_id: str
    source: str
    service: str
    label: str
    workspace_ids: _containers.RepeatedScalarFieldContainer[str]
    status: str
    error: str
    def __init__(self, log_id: _Optional[str] = ..., source: _Optional[str] = ..., service: _Optional[str] = ..., label: _Optional[str] = ..., workspace_ids: _Optional[_Iterable[str]] = ..., status: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class GatewayDiagnosticLogReferenceDTO(_message.Message):
    __slots__ = ("gateway_id", "log_id", "device", "inode", "line_start", "line_end", "content_version", "start_codepoint", "end_codepoint")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    LOG_ID_FIELD_NUMBER: _ClassVar[int]
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    INODE_FIELD_NUMBER: _ClassVar[int]
    LINE_START_FIELD_NUMBER: _ClassVar[int]
    LINE_END_FIELD_NUMBER: _ClassVar[int]
    CONTENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    START_CODEPOINT_FIELD_NUMBER: _ClassVar[int]
    END_CODEPOINT_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    log_id: str
    device: str
    inode: str
    line_start: int
    line_end: int
    content_version: str
    start_codepoint: int
    end_codepoint: int
    def __init__(self, gateway_id: _Optional[str] = ..., log_id: _Optional[str] = ..., device: _Optional[str] = ..., inode: _Optional[str] = ..., line_start: _Optional[int] = ..., line_end: _Optional[int] = ..., content_version: _Optional[str] = ..., start_codepoint: _Optional[int] = ..., end_codepoint: _Optional[int] = ...) -> None: ...

class GatewayDiagnosticLogMatchDTO(_message.Message):
    __slots__ = ("source", "reference", "preview", "preview_start_codepoint")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_START_CODEPOINT_FIELD_NUMBER: _ClassVar[int]
    source: GatewayDiagnosticLogQuerySourceDTO
    reference: GatewayDiagnosticLogReferenceDTO
    preview: str
    preview_start_codepoint: int
    def __init__(self, source: _Optional[_Union[GatewayDiagnosticLogQuerySourceDTO, _Mapping]] = ..., reference: _Optional[_Union[GatewayDiagnosticLogReferenceDTO, _Mapping]] = ..., preview: _Optional[str] = ..., preview_start_codepoint: _Optional[int] = ...) -> None: ...

class GatewayDiagnosticLogQueryResultDTO(_message.Message):
    __slots__ = ("gateway_id", "scope", "query", "items", "sources", "complete", "cursor", "limited_reason", "scanned_sources", "scanned_lines", "scanned_bytes")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    COMPLETE_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    LIMITED_REASON_FIELD_NUMBER: _ClassVar[int]
    SCANNED_SOURCES_FIELD_NUMBER: _ClassVar[int]
    SCANNED_LINES_FIELD_NUMBER: _ClassVar[int]
    SCANNED_BYTES_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    scope: GatewayDiagnosticLogQueryScopeDTO
    query: str
    items: _containers.RepeatedCompositeFieldContainer[GatewayDiagnosticLogMatchDTO]
    sources: _containers.RepeatedCompositeFieldContainer[GatewayDiagnosticLogQuerySourceDTO]
    complete: bool
    cursor: str
    limited_reason: str
    scanned_sources: int
    scanned_lines: int
    scanned_bytes: int
    def __init__(self, gateway_id: _Optional[str] = ..., scope: _Optional[_Union[GatewayDiagnosticLogQueryScopeDTO, _Mapping]] = ..., query: _Optional[str] = ..., items: _Optional[_Iterable[_Union[GatewayDiagnosticLogMatchDTO, _Mapping]]] = ..., sources: _Optional[_Iterable[_Union[GatewayDiagnosticLogQuerySourceDTO, _Mapping]]] = ..., complete: _Optional[bool] = ..., cursor: _Optional[str] = ..., limited_reason: _Optional[str] = ..., scanned_sources: _Optional[int] = ..., scanned_lines: _Optional[int] = ..., scanned_bytes: _Optional[int] = ...) -> None: ...

class GatewayDiagnosticLogLocateRequest(_message.Message):
    __slots__ = ("scope", "reference")
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    scope: GatewayDiagnosticLogQueryScopeDTO
    reference: GatewayDiagnosticLogReferenceDTO
    def __init__(self, scope: _Optional[_Union[GatewayDiagnosticLogQueryScopeDTO, _Mapping]] = ..., reference: _Optional[_Union[GatewayDiagnosticLogReferenceDTO, _Mapping]] = ...) -> None: ...

class GatewayDiagnosticLogLocationDTO(_message.Message):
    __slots__ = ("source", "reference", "text")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    source: GatewayDiagnosticLogQuerySourceDTO
    reference: GatewayDiagnosticLogReferenceDTO
    text: str
    def __init__(self, source: _Optional[_Union[GatewayDiagnosticLogQuerySourceDTO, _Mapping]] = ..., reference: _Optional[_Union[GatewayDiagnosticLogReferenceDTO, _Mapping]] = ..., text: _Optional[str] = ...) -> None: ...

class GatewayDirectoryEntryDTO(_message.Message):
    __slots__ = ("name", "path")
    NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    name: str
    path: str
    def __init__(self, name: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class GatewayDirectoryListDTO(_message.Message):
    __slots__ = ("path", "parent_path", "home_path", "entries", "truncated", "limit", "name")
    PATH_FIELD_NUMBER: _ClassVar[int]
    PARENT_PATH_FIELD_NUMBER: _ClassVar[int]
    HOME_PATH_FIELD_NUMBER: _ClassVar[int]
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    path: str
    parent_path: str
    home_path: str
    entries: _containers.RepeatedCompositeFieldContainer[GatewayDirectoryEntryDTO]
    truncated: bool
    limit: int
    name: str
    def __init__(self, path: _Optional[str] = ..., parent_path: _Optional[str] = ..., home_path: _Optional[str] = ..., entries: _Optional[_Iterable[_Union[GatewayDirectoryEntryDTO, _Mapping]]] = ..., truncated: _Optional[bool] = ..., limit: _Optional[int] = ..., name: _Optional[str] = ...) -> None: ...

class GatewayHealthDTO(_message.Message):
    __slots__ = ("status", "process_id", "development_restart_available", "gateway_id", "active_workspace")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PROCESS_ID_FIELD_NUMBER: _ClassVar[int]
    DEVELOPMENT_RESTART_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    status: str
    process_id: int
    development_restart_available: bool
    gateway_id: str
    active_workspace: GatewaySearchWorkspaceOwnerDTO
    def __init__(self, status: _Optional[str] = ..., process_id: _Optional[int] = ..., development_restart_available: _Optional[bool] = ..., gateway_id: _Optional[str] = ..., active_workspace: _Optional[_Union[GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ...) -> None: ...

class GatewayInboundAccessListDTO(_message.Message):
    __slots__ = ("gateway_id", "peers", "items")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    PEERS_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    peers: _containers.RepeatedCompositeFieldContainer[GatewayInboundPeerDTO]
    items: _containers.RepeatedCompositeFieldContainer[GatewayInboundWorkspaceDTO]
    def __init__(self, gateway_id: _Optional[str] = ..., peers: _Optional[_Iterable[_Union[GatewayInboundPeerDTO, _Mapping]]] = ..., items: _Optional[_Iterable[_Union[GatewayInboundWorkspaceDTO, _Mapping]]] = ...) -> None: ...

class GatewayInboundPeerDTO(_message.Message):
    __slots__ = ("connection_id", "peer_gateway_id", "credential_expires_at")
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    PEER_GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    CREDENTIAL_EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    connection_id: str
    peer_gateway_id: str
    credential_expires_at: str
    def __init__(self, connection_id: _Optional[str] = ..., peer_gateway_id: _Optional[str] = ..., credential_expires_at: _Optional[str] = ...) -> None: ...

class GatewayInboundWorkspaceDTO(_message.Message):
    __slots__ = ("workspace_id", "name", "status", "managed", "system_default")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MANAGED_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_DEFAULT_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    name: str
    status: str
    managed: bool
    system_default: bool
    def __init__(self, workspace_id: _Optional[str] = ..., name: _Optional[str] = ..., status: _Optional[str] = ..., managed: _Optional[bool] = ..., system_default: _Optional[bool] = ...) -> None: ...

class GatewayManagedWorkspaceDTO(_message.Message):
    __slots__ = ("gateway_id", "workspace_id", "name", "status", "removable", "system_default")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    REMOVABLE_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_DEFAULT_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    workspace_id: str
    name: str
    status: str
    removable: bool
    system_default: bool
    def __init__(self, gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., name: _Optional[str] = ..., status: _Optional[str] = ..., removable: _Optional[bool] = ..., system_default: _Optional[bool] = ...) -> None: ...

class GatewayManagedWorkspaceListDTO(_message.Message):
    __slots__ = ("gateway_connection_id", "gateway_id", "gateway_name", "connection_kind", "items")
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_NAME_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_KIND_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    gateway_connection_id: str
    gateway_id: str
    gateway_name: str
    connection_kind: str
    items: _containers.RepeatedCompositeFieldContainer[GatewayManagedWorkspaceDTO]
    def __init__(self, gateway_connection_id: _Optional[str] = ..., gateway_id: _Optional[str] = ..., gateway_name: _Optional[str] = ..., connection_kind: _Optional[str] = ..., items: _Optional[_Iterable[_Union[GatewayManagedWorkspaceDTO, _Mapping]]] = ...) -> None: ...

class GatewayRemoteConnectionSummaryDTO(_message.Message):
    __slots__ = ("gateway_connection_id", "gateway_id", "name", "host", "port", "username", "ssh_config_host", "remote_gateway_port", "config_event_cursor", "config_reload_state", "restart_required", "candidate_ref")
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    HOST_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    SSH_CONFIG_HOST_FIELD_NUMBER: _ClassVar[int]
    REMOTE_GATEWAY_PORT_FIELD_NUMBER: _ClassVar[int]
    CONFIG_EVENT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    CONFIG_RELOAD_STATE_FIELD_NUMBER: _ClassVar[int]
    RESTART_REQUIRED_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_REF_FIELD_NUMBER: _ClassVar[int]
    gateway_connection_id: str
    gateway_id: str
    name: str
    host: str
    port: int
    username: str
    ssh_config_host: str
    remote_gateway_port: int
    config_event_cursor: int
    config_reload_state: str
    restart_required: bool
    candidate_ref: str
    def __init__(self, gateway_connection_id: _Optional[str] = ..., gateway_id: _Optional[str] = ..., name: _Optional[str] = ..., host: _Optional[str] = ..., port: _Optional[int] = ..., username: _Optional[str] = ..., ssh_config_host: _Optional[str] = ..., remote_gateway_port: _Optional[int] = ..., config_event_cursor: _Optional[int] = ..., config_reload_state: _Optional[str] = ..., restart_required: _Optional[bool] = ..., candidate_ref: _Optional[str] = ...) -> None: ...

class GatewayResourceDTO(_message.Message):
    __slots__ = ("gateway_connection_id", "gateway_name", "workspace_id", "workspace_name", "connection_kind", "session_id", "session_title", "resource", "gateway_id")
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_NAME_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_NAME_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_KIND_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_TITLE_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_connection_id: str
    gateway_name: str
    workspace_id: str
    workspace_name: str
    connection_kind: str
    session_id: str
    session_title: str
    resource: _struct_pb2.Struct
    gateway_id: str
    def __init__(self, gateway_connection_id: _Optional[str] = ..., gateway_name: _Optional[str] = ..., workspace_id: _Optional[str] = ..., workspace_name: _Optional[str] = ..., connection_kind: _Optional[str] = ..., session_id: _Optional[str] = ..., session_title: _Optional[str] = ..., resource: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., gateway_id: _Optional[str] = ...) -> None: ...

class GatewayResourceListDTO(_message.Message):
    __slots__ = ("items", "errors")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[GatewayResourceDTO]
    errors: _containers.RepeatedCompositeFieldContainer[GatewayResourceScopeErrorDTO]
    def __init__(self, items: _Optional[_Iterable[_Union[GatewayResourceDTO, _Mapping]]] = ..., errors: _Optional[_Iterable[_Union[GatewayResourceScopeErrorDTO, _Mapping]]] = ...) -> None: ...

class GatewayResourceScopeErrorDTO(_message.Message):
    __slots__ = ("scope_key", "label", "message")
    SCOPE_KEY_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    scope_key: str
    label: str
    message: str
    def __init__(self, scope_key: _Optional[str] = ..., label: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class GatewayRuntimeDesiredWorkspaceSet(_message.Message):
    __slots__ = ("workspace_ids",)
    WORKSPACE_IDS_FIELD_NUMBER: _ClassVar[int]
    workspace_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, workspace_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class GatewayRuntimeGroupRequest(_message.Message):
    __slots__ = ("expected_backend_generation", "expected_workspace_ids", "desired_workspace_ids")
    EXPECTED_BACKEND_GENERATION_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_WORKSPACE_IDS_FIELD_NUMBER: _ClassVar[int]
    DESIRED_WORKSPACE_IDS_FIELD_NUMBER: _ClassVar[int]
    expected_backend_generation: str
    expected_workspace_ids: _containers.RepeatedScalarFieldContainer[str]
    desired_workspace_ids: GatewayRuntimeDesiredWorkspaceSet
    def __init__(self, expected_backend_generation: _Optional[str] = ..., expected_workspace_ids: _Optional[_Iterable[str]] = ..., desired_workspace_ids: _Optional[_Union[GatewayRuntimeDesiredWorkspaceSet, _Mapping]] = ...) -> None: ...

class GatewayRuntimeBlockerDTO(_message.Message):
    __slots__ = ("kind", "resource_id", "session_id", "status", "detail", "workspace_id", "gateway_id")
    KIND_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    kind: str
    resource_id: str
    session_id: str
    status: str
    detail: str
    workspace_id: str
    gateway_id: str
    def __init__(self, kind: _Optional[str] = ..., resource_id: _Optional[str] = ..., session_id: _Optional[str] = ..., status: _Optional[str] = ..., detail: _Optional[str] = ..., workspace_id: _Optional[str] = ..., gateway_id: _Optional[str] = ...) -> None: ...

class GatewayRuntimeRestartResultDTO(_message.Message):
    __slots__ = ("workspace_id", "status", "forced", "blockers", "workspaces", "backend_generation", "affected_workspace_ids", "gateway_id")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    FORCED_FIELD_NUMBER: _ClassVar[int]
    BLOCKERS_FIELD_NUMBER: _ClassVar[int]
    WORKSPACES_FIELD_NUMBER: _ClassVar[int]
    BACKEND_GENERATION_FIELD_NUMBER: _ClassVar[int]
    AFFECTED_WORKSPACE_IDS_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    status: str
    forced: bool
    blockers: _containers.RepeatedCompositeFieldContainer[GatewayRuntimeBlockerDTO]
    workspaces: GatewayWorkspaceListDTO
    backend_generation: str
    affected_workspace_ids: _containers.RepeatedScalarFieldContainer[str]
    gateway_id: str
    def __init__(self, workspace_id: _Optional[str] = ..., status: _Optional[str] = ..., forced: _Optional[bool] = ..., blockers: _Optional[_Iterable[_Union[GatewayRuntimeBlockerDTO, _Mapping]]] = ..., workspaces: _Optional[_Union[GatewayWorkspaceListDTO, _Mapping]] = ..., backend_generation: _Optional[str] = ..., affected_workspace_ids: _Optional[_Iterable[str]] = ..., gateway_id: _Optional[str] = ...) -> None: ...

class GatewayRuntimeStateResultDTO(_message.Message):
    __slots__ = ("workspace_id", "status", "blockers", "workspaces", "backend_generation", "affected_workspace_ids", "gateway_id")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    BLOCKERS_FIELD_NUMBER: _ClassVar[int]
    WORKSPACES_FIELD_NUMBER: _ClassVar[int]
    BACKEND_GENERATION_FIELD_NUMBER: _ClassVar[int]
    AFFECTED_WORKSPACE_IDS_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    status: str
    blockers: _containers.RepeatedCompositeFieldContainer[GatewayRuntimeBlockerDTO]
    workspaces: GatewayWorkspaceListDTO
    backend_generation: str
    affected_workspace_ids: _containers.RepeatedScalarFieldContainer[str]
    gateway_id: str
    def __init__(self, workspace_id: _Optional[str] = ..., status: _Optional[str] = ..., blockers: _Optional[_Iterable[_Union[GatewayRuntimeBlockerDTO, _Mapping]]] = ..., workspaces: _Optional[_Union[GatewayWorkspaceListDTO, _Mapping]] = ..., backend_generation: _Optional[str] = ..., affected_workspace_ids: _Optional[_Iterable[str]] = ..., gateway_id: _Optional[str] = ...) -> None: ...

class GatewayServiceStatusDTO(_message.Message):
    __slots__ = ("status", "health_path", "local_url", "local_port", "remote_host", "remote_port", "error")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    HEALTH_PATH_FIELD_NUMBER: _ClassVar[int]
    LOCAL_URL_FIELD_NUMBER: _ClassVar[int]
    LOCAL_PORT_FIELD_NUMBER: _ClassVar[int]
    REMOTE_HOST_FIELD_NUMBER: _ClassVar[int]
    REMOTE_PORT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    status: str
    health_path: str
    local_url: str
    local_port: int
    remote_host: str
    remote_port: int
    error: str
    def __init__(self, status: _Optional[str] = ..., health_path: _Optional[str] = ..., local_url: _Optional[str] = ..., local_port: _Optional[int] = ..., remote_host: _Optional[str] = ..., remote_port: _Optional[int] = ..., error: _Optional[str] = ...) -> None: ...

class GatewaySearchWorkspaceOwnerDTO(_message.Message):
    __slots__ = ("gateway_id", "workspace_id")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    workspace_id: str
    def __init__(self, gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ...) -> None: ...

class GatewayWorkspaceDiscoveryHitDTO(_message.Message):
    __slots__ = ("owner", "name", "navigation_node_id", "breadcrumb_node_ids", "breadcrumb_names")
    OWNER_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    NAVIGATION_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    BREADCRUMB_NODE_IDS_FIELD_NUMBER: _ClassVar[int]
    BREADCRUMB_NAMES_FIELD_NUMBER: _ClassVar[int]
    owner: GatewaySearchWorkspaceOwnerDTO
    name: str
    navigation_node_id: str
    breadcrumb_node_ids: _containers.RepeatedScalarFieldContainer[str]
    breadcrumb_names: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, owner: _Optional[_Union[GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ..., name: _Optional[str] = ..., navigation_node_id: _Optional[str] = ..., breadcrumb_node_ids: _Optional[_Iterable[str]] = ..., breadcrumb_names: _Optional[_Iterable[str]] = ...) -> None: ...

class GatewayNavigationFolderDiscoveryHitDTO(_message.Message):
    __slots__ = ("name", "navigation_node_id", "breadcrumb_node_ids", "breadcrumb_names")
    NAME_FIELD_NUMBER: _ClassVar[int]
    NAVIGATION_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    BREADCRUMB_NODE_IDS_FIELD_NUMBER: _ClassVar[int]
    BREADCRUMB_NAMES_FIELD_NUMBER: _ClassVar[int]
    name: str
    navigation_node_id: str
    breadcrumb_node_ids: _containers.RepeatedScalarFieldContainer[str]
    breadcrumb_names: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, name: _Optional[str] = ..., navigation_node_id: _Optional[str] = ..., breadcrumb_node_ids: _Optional[_Iterable[str]] = ..., breadcrumb_names: _Optional[_Iterable[str]] = ...) -> None: ...

class GatewayWorkspaceDiscoveryResultsDTO(_message.Message):
    __slots__ = ("gateway_id", "query", "resolved_at", "source", "items", "cursor", "complete", "folders")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    RESOLVED_AT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    COMPLETE_FIELD_NUMBER: _ClassVar[int]
    FOLDERS_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    query: str
    resolved_at: _timestamp_pb2.Timestamp
    source: str
    items: _containers.RepeatedCompositeFieldContainer[GatewayWorkspaceDiscoveryHitDTO]
    cursor: str
    complete: bool
    folders: _containers.RepeatedCompositeFieldContainer[GatewayNavigationFolderDiscoveryHitDTO]
    def __init__(self, gateway_id: _Optional[str] = ..., query: _Optional[str] = ..., resolved_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., source: _Optional[str] = ..., items: _Optional[_Iterable[_Union[GatewayWorkspaceDiscoveryHitDTO, _Mapping]]] = ..., cursor: _Optional[str] = ..., complete: _Optional[bool] = ..., folders: _Optional[_Iterable[_Union[GatewayNavigationFolderDiscoveryHitDTO, _Mapping]]] = ...) -> None: ...

class GatewaySearchScopeDTO(_message.Message):
    __slots__ = ("version", "gateway_id", "resolved_at", "mode", "owners")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    RESOLVED_AT_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    OWNERS_FIELD_NUMBER: _ClassVar[int]
    version: int
    gateway_id: str
    resolved_at: _timestamp_pb2.Timestamp
    mode: str
    owners: _containers.RepeatedCompositeFieldContainer[GatewaySearchWorkspaceOwnerDTO]
    def __init__(self, version: _Optional[int] = ..., gateway_id: _Optional[str] = ..., resolved_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., mode: _Optional[str] = ..., owners: _Optional[_Iterable[_Union[GatewaySearchWorkspaceOwnerDTO, _Mapping]]] = ...) -> None: ...

class GatewaySearchScopeRequest(_message.Message):
    __slots__ = ("mode", "origin", "owners", "scope")
    MODE_FIELD_NUMBER: _ClassVar[int]
    ORIGIN_FIELD_NUMBER: _ClassVar[int]
    OWNERS_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    mode: str
    origin: GatewaySearchWorkspaceOwnerDTO
    owners: _containers.RepeatedCompositeFieldContainer[GatewaySearchWorkspaceOwnerDTO]
    scope: GatewaySearchScopeDTO
    def __init__(self, mode: _Optional[str] = ..., origin: _Optional[_Union[GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ..., owners: _Optional[_Iterable[_Union[GatewaySearchWorkspaceOwnerDTO, _Mapping]]] = ..., scope: _Optional[_Union[GatewaySearchScopeDTO, _Mapping]] = ...) -> None: ...

class GatewaySearchScopeBranchDTO(_message.Message):
    __slots__ = ("owner", "state", "code", "message")
    OWNER_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    owner: GatewaySearchWorkspaceOwnerDTO
    state: str
    code: str
    message: str
    def __init__(self, owner: _Optional[_Union[GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ..., state: _Optional[str] = ..., code: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class GatewaySearchScopeResolutionDTO(_message.Message):
    __slots__ = ("scope", "branches")
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    BRANCHES_FIELD_NUMBER: _ClassVar[int]
    scope: GatewaySearchScopeDTO
    branches: _containers.RepeatedCompositeFieldContainer[GatewaySearchScopeBranchDTO]
    def __init__(self, scope: _Optional[_Union[GatewaySearchScopeDTO, _Mapping]] = ..., branches: _Optional[_Iterable[_Union[GatewaySearchScopeBranchDTO, _Mapping]]] = ...) -> None: ...

class GatewayThemeBackgroundDTO(_message.Message):
    __slots__ = ("type", "url", "asset_id", "position", "size", "repeat", "appearance", "overlay")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    ASSET_ID_FIELD_NUMBER: _ClassVar[int]
    POSITION_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    REPEAT_FIELD_NUMBER: _ClassVar[int]
    APPEARANCE_FIELD_NUMBER: _ClassVar[int]
    OVERLAY_FIELD_NUMBER: _ClassVar[int]
    type: str
    url: str
    asset_id: str
    position: str
    size: str
    repeat: str
    appearance: str
    overlay: str
    def __init__(self, type: _Optional[str] = ..., url: _Optional[str] = ..., asset_id: _Optional[str] = ..., position: _Optional[str] = ..., size: _Optional[str] = ..., repeat: _Optional[str] = ..., appearance: _Optional[str] = ..., overlay: _Optional[str] = ...) -> None: ...

class GatewayThemeCatalogDTO(_message.Message):
    __slots__ = ("current_theme_id", "items", "current_theme")
    CURRENT_THEME_ID_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_THEME_FIELD_NUMBER: _ClassVar[int]
    current_theme_id: str
    items: _containers.RepeatedCompositeFieldContainer[GatewayThemeOptionDTO]
    current_theme: ResolvedGatewayThemeDTO
    def __init__(self, current_theme_id: _Optional[str] = ..., items: _Optional[_Iterable[_Union[GatewayThemeOptionDTO, _Mapping]]] = ..., current_theme: _Optional[_Union[ResolvedGatewayThemeDTO, _Mapping]] = ...) -> None: ...

class GatewayThemeOptionDTO(_message.Message):
    __slots__ = ("id", "label", "extends", "source", "preview_tokens", "background_image_url")
    ID_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    EXTENDS_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_TOKENS_FIELD_NUMBER: _ClassVar[int]
    BACKGROUND_IMAGE_URL_FIELD_NUMBER: _ClassVar[int]
    id: str
    label: str
    extends: str
    source: str
    preview_tokens: _struct_pb2.Struct
    background_image_url: str
    def __init__(self, id: _Optional[str] = ..., label: _Optional[str] = ..., extends: _Optional[str] = ..., source: _Optional[str] = ..., preview_tokens: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., background_image_url: _Optional[str] = ...) -> None: ...

class GatewayUIAssetDTO(_message.Message):
    __slots__ = ("asset_id", "original_filename", "content_type", "size", "sha256", "imported_at", "url", "referenced_theme_ids")
    ASSET_ID_FIELD_NUMBER: _ClassVar[int]
    ORIGINAL_FILENAME_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    IMPORTED_AT_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    REFERENCED_THEME_IDS_FIELD_NUMBER: _ClassVar[int]
    asset_id: str
    original_filename: str
    content_type: str
    size: int
    sha256: str
    imported_at: str
    url: str
    referenced_theme_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, asset_id: _Optional[str] = ..., original_filename: _Optional[str] = ..., content_type: _Optional[str] = ..., size: _Optional[int] = ..., sha256: _Optional[str] = ..., imported_at: _Optional[str] = ..., url: _Optional[str] = ..., referenced_theme_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class GatewayUIAssetListDTO(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[GatewayUIAssetDTO]
    def __init__(self, items: _Optional[_Iterable[_Union[GatewayUIAssetDTO, _Mapping]]] = ...) -> None: ...

class GatewayUserAccessDTO(_message.Message):
    __slots__ = ("kind", "user_id", "lease_generation", "expires_at", "takeover")
    KIND_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    LEASE_GENERATION_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    TAKEOVER_FIELD_NUMBER: _ClassVar[int]
    kind: str
    user_id: str
    lease_generation: int
    expires_at: str
    takeover: bool
    def __init__(self, kind: _Optional[str] = ..., user_id: _Optional[str] = ..., lease_generation: _Optional[int] = ..., expires_at: _Optional[str] = ..., takeover: _Optional[bool] = ...) -> None: ...

class GatewayUserDTO(_message.Message):
    __slots__ = ("user_id", "display_name", "created_at", "lease")
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    LEASE_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    display_name: str
    created_at: str
    lease: GatewayUserLeaseDTO
    def __init__(self, user_id: _Optional[str] = ..., display_name: _Optional[str] = ..., created_at: _Optional[str] = ..., lease: _Optional[_Union[GatewayUserLeaseDTO, _Mapping]] = ...) -> None: ...

class GatewayUserLeaseDTO(_message.Message):
    __slots__ = ("occupied", "client_label", "heartbeat_at", "expires_at")
    OCCUPIED_FIELD_NUMBER: _ClassVar[int]
    CLIENT_LABEL_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_AT_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    occupied: bool
    client_label: str
    heartbeat_at: str
    expires_at: str
    def __init__(self, occupied: _Optional[bool] = ..., client_label: _Optional[str] = ..., heartbeat_at: _Optional[str] = ..., expires_at: _Optional[str] = ...) -> None: ...

class GatewayUserListDTO(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[GatewayUserDTO]
    def __init__(self, items: _Optional[_Iterable[_Union[GatewayUserDTO, _Mapping]]] = ...) -> None: ...

class GatewayUserViewStateDTO(_message.Message):
    __slots__ = ("user_id", "workspace_id", "session_id", "turn_anchor", "scroll_offset", "follow_latest", "projection_version", "tool_details_expanded", "updated_at", "gateway_id")
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_ANCHOR_FIELD_NUMBER: _ClassVar[int]
    SCROLL_OFFSET_FIELD_NUMBER: _ClassVar[int]
    FOLLOW_LATEST_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_VERSION_FIELD_NUMBER: _ClassVar[int]
    TOOL_DETAILS_EXPANDED_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    workspace_id: str
    session_id: str
    turn_anchor: str
    scroll_offset: float
    follow_latest: bool
    projection_version: int
    tool_details_expanded: bool
    updated_at: str
    gateway_id: str
    def __init__(self, user_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., session_id: _Optional[str] = ..., turn_anchor: _Optional[str] = ..., scroll_offset: _Optional[float] = ..., follow_latest: _Optional[bool] = ..., projection_version: _Optional[int] = ..., tool_details_expanded: _Optional[bool] = ..., updated_at: _Optional[str] = ..., gateway_id: _Optional[str] = ...) -> None: ...

class GatewayUserViewStateUpdateRequest(_message.Message):
    __slots__ = ("turn_anchor", "scroll_offset", "follow_latest", "projection_version", "tool_details_expanded")
    TURN_ANCHOR_FIELD_NUMBER: _ClassVar[int]
    SCROLL_OFFSET_FIELD_NUMBER: _ClassVar[int]
    FOLLOW_LATEST_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_VERSION_FIELD_NUMBER: _ClassVar[int]
    TOOL_DETAILS_EXPANDED_FIELD_NUMBER: _ClassVar[int]
    turn_anchor: str
    scroll_offset: float
    follow_latest: bool
    projection_version: int
    tool_details_expanded: bool
    def __init__(self, turn_anchor: _Optional[str] = ..., scroll_offset: _Optional[float] = ..., follow_latest: _Optional[bool] = ..., projection_version: _Optional[int] = ..., tool_details_expanded: _Optional[bool] = ...) -> None: ...

class GatewayWorkspaceDTO(_message.Message):
    __slots__ = ("workspace_id", "name", "backend_url", "connection_kind", "status", "active", "managed", "removable", "system_default", "runtime_action", "config_reload", "services", "connection_error", "checked_at", "backend_generation", "affected_workspace_ids", "gateway_id", "parent_workspace", "remote_gateways", "backend_id")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    BACKEND_URL_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_KIND_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_FIELD_NUMBER: _ClassVar[int]
    MANAGED_FIELD_NUMBER: _ClassVar[int]
    REMOVABLE_FIELD_NUMBER: _ClassVar[int]
    SYSTEM_DEFAULT_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_ACTION_FIELD_NUMBER: _ClassVar[int]
    CONFIG_RELOAD_FIELD_NUMBER: _ClassVar[int]
    SERVICES_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ERROR_FIELD_NUMBER: _ClassVar[int]
    CHECKED_AT_FIELD_NUMBER: _ClassVar[int]
    BACKEND_GENERATION_FIELD_NUMBER: _ClassVar[int]
    AFFECTED_WORKSPACE_IDS_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    PARENT_WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    REMOTE_GATEWAYS_FIELD_NUMBER: _ClassVar[int]
    BACKEND_ID_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    name: str
    backend_url: str
    connection_kind: str
    status: str
    active: bool
    managed: bool
    removable: bool
    system_default: bool
    runtime_action: str
    config_reload: GatewayConfigReloadStatusDTO
    services: _struct_pb2.Struct
    connection_error: str
    checked_at: str
    backend_generation: str
    affected_workspace_ids: _containers.RepeatedScalarFieldContainer[str]
    gateway_id: str
    parent_workspace: GatewaySearchWorkspaceOwnerDTO
    remote_gateways: _containers.RepeatedCompositeFieldContainer[GatewayRemoteConnectionSummaryDTO]
    backend_id: str
    def __init__(self, workspace_id: _Optional[str] = ..., name: _Optional[str] = ..., backend_url: _Optional[str] = ..., connection_kind: _Optional[str] = ..., status: _Optional[str] = ..., active: _Optional[bool] = ..., managed: _Optional[bool] = ..., removable: _Optional[bool] = ..., system_default: _Optional[bool] = ..., runtime_action: _Optional[str] = ..., config_reload: _Optional[_Union[GatewayConfigReloadStatusDTO, _Mapping]] = ..., services: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., connection_error: _Optional[str] = ..., checked_at: _Optional[str] = ..., backend_generation: _Optional[str] = ..., affected_workspace_ids: _Optional[_Iterable[str]] = ..., gateway_id: _Optional[str] = ..., parent_workspace: _Optional[_Union[GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ..., remote_gateways: _Optional[_Iterable[_Union[GatewayRemoteConnectionSummaryDTO, _Mapping]]] = ..., backend_id: _Optional[str] = ...) -> None: ...

class GatewayWorkspaceListDTO(_message.Message):
    __slots__ = ("items", "active_workspace")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[GatewayWorkspaceDTO]
    active_workspace: GatewaySearchWorkspaceOwnerDTO
    def __init__(self, items: _Optional[_Iterable[_Union[GatewayWorkspaceDTO, _Mapping]]] = ..., active_workspace: _Optional[_Union[GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ...) -> None: ...

class PortForwardDTO(_message.Message):
    __slots__ = ("gateway_id", "forward_id", "workspace_id", "connection_id", "remote_host", "remote_port", "local_host", "local_port", "protocol", "label", "status", "error", "local_url")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    FORWARD_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    REMOTE_HOST_FIELD_NUMBER: _ClassVar[int]
    REMOTE_PORT_FIELD_NUMBER: _ClassVar[int]
    LOCAL_HOST_FIELD_NUMBER: _ClassVar[int]
    LOCAL_PORT_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    LOCAL_URL_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    forward_id: str
    workspace_id: str
    connection_id: str
    remote_host: str
    remote_port: int
    local_host: str
    local_port: int
    protocol: str
    label: str
    status: str
    error: str
    local_url: str
    def __init__(self, gateway_id: _Optional[str] = ..., forward_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., connection_id: _Optional[str] = ..., remote_host: _Optional[str] = ..., remote_port: _Optional[int] = ..., local_host: _Optional[str] = ..., local_port: _Optional[int] = ..., protocol: _Optional[str] = ..., label: _Optional[str] = ..., status: _Optional[str] = ..., error: _Optional[str] = ..., local_url: _Optional[str] = ...) -> None: ...

class PortForwardListDTO(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[PortForwardDTO]
    def __init__(self, items: _Optional[_Iterable[_Union[PortForwardDTO, _Mapping]]] = ...) -> None: ...

class ReorderGatewayWorkspacesRequest(_message.Message):
    __slots__ = ("workspaces",)
    WORKSPACES_FIELD_NUMBER: _ClassVar[int]
    workspaces: _containers.RepeatedCompositeFieldContainer[GatewaySearchWorkspaceOwnerDTO]
    def __init__(self, workspaces: _Optional[_Iterable[_Union[GatewaySearchWorkspaceOwnerDTO, _Mapping]]] = ...) -> None: ...

class ResolvedGatewayThemeDTO(_message.Message):
    __slots__ = ("id", "label", "color_scheme", "tokens", "background_image_url")
    ID_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    COLOR_SCHEME_FIELD_NUMBER: _ClassVar[int]
    TOKENS_FIELD_NUMBER: _ClassVar[int]
    BACKGROUND_IMAGE_URL_FIELD_NUMBER: _ClassVar[int]
    id: str
    label: str
    color_scheme: str
    tokens: _struct_pb2.Struct
    background_image_url: str
    def __init__(self, id: _Optional[str] = ..., label: _Optional[str] = ..., color_scheme: _Optional[str] = ..., tokens: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., background_image_url: _Optional[str] = ...) -> None: ...

class SshConnectionOptionDTO(_message.Message):
    __slots__ = ("connection_id", "source", "label", "host", "port", "username", "ssh_config_host", "initial_path")
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    HOST_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    USERNAME_FIELD_NUMBER: _ClassVar[int]
    SSH_CONFIG_HOST_FIELD_NUMBER: _ClassVar[int]
    INITIAL_PATH_FIELD_NUMBER: _ClassVar[int]
    connection_id: str
    source: str
    label: str
    host: str
    port: int
    username: str
    ssh_config_host: str
    initial_path: str
    def __init__(self, connection_id: _Optional[str] = ..., source: _Optional[str] = ..., label: _Optional[str] = ..., host: _Optional[str] = ..., port: _Optional[int] = ..., username: _Optional[str] = ..., ssh_config_host: _Optional[str] = ..., initial_path: _Optional[str] = ...) -> None: ...

class SshConnectionOptionListDTO(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[SshConnectionOptionDTO]
    def __init__(self, items: _Optional[_Iterable[_Union[SshConnectionOptionDTO, _Mapping]]] = ...) -> None: ...

class UpdateGatewayWorkspaceRequest(_message.Message):
    __slots__ = ("name", "parent_workspace")
    NAME_FIELD_NUMBER: _ClassVar[int]
    PARENT_WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    name: str
    parent_workspace: GatewaySearchWorkspaceOwnerDTO
    def __init__(self, name: _Optional[str] = ..., parent_workspace: _Optional[_Union[GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ...) -> None: ...

class WebUIGatewayConsoleSettingsDTO(_message.Message):
    __slots__ = ("view",)
    VIEW_FIELD_NUMBER: _ClassVar[int]
    view: str
    def __init__(self, view: _Optional[str] = ...) -> None: ...

class WebUILayoutSettingsDTO(_message.Message):
    __slots__ = ("workbench_view", "agent_sessions_panel_open", "chat_visible", "auxiliary_visible", "panel_visible", "auxiliary_tab", "auxiliary_tab_order", "main_area_ratios", "bottom_panel_by_workspace", "workspace_preview_visible", "workspace_preview_maximized", "workspace_preview_file_paths", "workspace_preview_active_file_path", "customizations_collapsed", "customizations_height", "panel_height", "content_view", "delivery_policy_default")
    WORKBENCH_VIEW_FIELD_NUMBER: _ClassVar[int]
    AGENT_SESSIONS_PANEL_OPEN_FIELD_NUMBER: _ClassVar[int]
    CHAT_VISIBLE_FIELD_NUMBER: _ClassVar[int]
    AUXILIARY_VISIBLE_FIELD_NUMBER: _ClassVar[int]
    PANEL_VISIBLE_FIELD_NUMBER: _ClassVar[int]
    AUXILIARY_TAB_FIELD_NUMBER: _ClassVar[int]
    AUXILIARY_TAB_ORDER_FIELD_NUMBER: _ClassVar[int]
    MAIN_AREA_RATIOS_FIELD_NUMBER: _ClassVar[int]
    BOTTOM_PANEL_BY_WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_PREVIEW_VISIBLE_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_PREVIEW_MAXIMIZED_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_PREVIEW_FILE_PATHS_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_PREVIEW_ACTIVE_FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    CUSTOMIZATIONS_COLLAPSED_FIELD_NUMBER: _ClassVar[int]
    CUSTOMIZATIONS_HEIGHT_FIELD_NUMBER: _ClassVar[int]
    PANEL_HEIGHT_FIELD_NUMBER: _ClassVar[int]
    CONTENT_VIEW_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_POLICY_DEFAULT_FIELD_NUMBER: _ClassVar[int]
    workbench_view: str
    agent_sessions_panel_open: bool
    chat_visible: bool
    auxiliary_visible: bool
    panel_visible: bool
    auxiliary_tab: str
    auxiliary_tab_order: _containers.RepeatedScalarFieldContainer[str]
    main_area_ratios: WebUIMainAreaRatiosDTO
    bottom_panel_by_workspace: _struct_pb2.Struct
    workspace_preview_visible: bool
    workspace_preview_maximized: bool
    workspace_preview_file_paths: _containers.RepeatedScalarFieldContainer[str]
    workspace_preview_active_file_path: str
    customizations_collapsed: bool
    customizations_height: int
    panel_height: int
    content_view: str
    delivery_policy_default: str
    def __init__(self, workbench_view: _Optional[str] = ..., agent_sessions_panel_open: _Optional[bool] = ..., chat_visible: _Optional[bool] = ..., auxiliary_visible: _Optional[bool] = ..., panel_visible: _Optional[bool] = ..., auxiliary_tab: _Optional[str] = ..., auxiliary_tab_order: _Optional[_Iterable[str]] = ..., main_area_ratios: _Optional[_Union[WebUIMainAreaRatiosDTO, _Mapping]] = ..., bottom_panel_by_workspace: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., workspace_preview_visible: _Optional[bool] = ..., workspace_preview_maximized: _Optional[bool] = ..., workspace_preview_file_paths: _Optional[_Iterable[str]] = ..., workspace_preview_active_file_path: _Optional[str] = ..., customizations_collapsed: _Optional[bool] = ..., customizations_height: _Optional[int] = ..., panel_height: _Optional[int] = ..., content_view: _Optional[str] = ..., delivery_policy_default: _Optional[str] = ...) -> None: ...

class WebUIMainAreaRatiosDTO(_message.Message):
    __slots__ = ("agent_sessions", "chat", "workspace_preview", "auxiliary")
    AGENT_SESSIONS_FIELD_NUMBER: _ClassVar[int]
    CHAT_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_PREVIEW_FIELD_NUMBER: _ClassVar[int]
    AUXILIARY_FIELD_NUMBER: _ClassVar[int]
    agent_sessions: float
    chat: float
    workspace_preview: float
    auxiliary: float
    def __init__(self, agent_sessions: _Optional[float] = ..., chat: _Optional[float] = ..., workspace_preview: _Optional[float] = ..., auxiliary: _Optional[float] = ...) -> None: ...

class WebUISessionSidebarSettingsDTO(_message.Message):
    __slots__ = ("filter_mode", "sort_mode", "grouping_mode", "workspace_group_capped", "collapsed_workspace_ids", "collapsed_session_ids", "expanded_root_tree_ids", "collapsed_section_ids")
    FILTER_MODE_FIELD_NUMBER: _ClassVar[int]
    SORT_MODE_FIELD_NUMBER: _ClassVar[int]
    GROUPING_MODE_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_GROUP_CAPPED_FIELD_NUMBER: _ClassVar[int]
    COLLAPSED_WORKSPACE_IDS_FIELD_NUMBER: _ClassVar[int]
    COLLAPSED_SESSION_IDS_FIELD_NUMBER: _ClassVar[int]
    EXPANDED_ROOT_TREE_IDS_FIELD_NUMBER: _ClassVar[int]
    COLLAPSED_SECTION_IDS_FIELD_NUMBER: _ClassVar[int]
    filter_mode: str
    sort_mode: str
    grouping_mode: str
    workspace_group_capped: bool
    collapsed_workspace_ids: _containers.RepeatedScalarFieldContainer[str]
    collapsed_session_ids: _containers.RepeatedScalarFieldContainer[str]
    expanded_root_tree_ids: _containers.RepeatedScalarFieldContainer[str]
    collapsed_section_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, filter_mode: _Optional[str] = ..., sort_mode: _Optional[str] = ..., grouping_mode: _Optional[str] = ..., workspace_group_capped: _Optional[bool] = ..., collapsed_workspace_ids: _Optional[_Iterable[str]] = ..., collapsed_session_ids: _Optional[_Iterable[str]] = ..., expanded_root_tree_ids: _Optional[_Iterable[str]] = ..., collapsed_section_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class WebUISettingsDTO(_message.Message):
    __slots__ = ("layout", "session_sidebar", "workspace_file_tree", "gateway_console", "theme")
    LAYOUT_FIELD_NUMBER: _ClassVar[int]
    SESSION_SIDEBAR_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_FILE_TREE_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONSOLE_FIELD_NUMBER: _ClassVar[int]
    THEME_FIELD_NUMBER: _ClassVar[int]
    layout: WebUILayoutSettingsDTO
    session_sidebar: WebUISessionSidebarSettingsDTO
    workspace_file_tree: WebUIWorkspaceFileTreeSettingsDTO
    gateway_console: WebUIGatewayConsoleSettingsDTO
    theme: WebUIThemeSettingsDTO
    def __init__(self, layout: _Optional[_Union[WebUILayoutSettingsDTO, _Mapping]] = ..., session_sidebar: _Optional[_Union[WebUISessionSidebarSettingsDTO, _Mapping]] = ..., workspace_file_tree: _Optional[_Union[WebUIWorkspaceFileTreeSettingsDTO, _Mapping]] = ..., gateway_console: _Optional[_Union[WebUIGatewayConsoleSettingsDTO, _Mapping]] = ..., theme: _Optional[_Union[WebUIThemeSettingsDTO, _Mapping]] = ...) -> None: ...

class WebUISettingsUpdateDTO(_message.Message):
    __slots__ = ("layout", "session_sidebar", "workspace_file_tree", "gateway_console", "theme")
    LAYOUT_FIELD_NUMBER: _ClassVar[int]
    SESSION_SIDEBAR_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_FILE_TREE_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONSOLE_FIELD_NUMBER: _ClassVar[int]
    THEME_FIELD_NUMBER: _ClassVar[int]
    layout: WebUILayoutSettingsDTO
    session_sidebar: WebUISessionSidebarSettingsDTO
    workspace_file_tree: WebUIWorkspaceFileTreeSettingsDTO
    gateway_console: WebUIGatewayConsoleSettingsDTO
    theme: WebUIThemeSettingsUpdateDTO
    def __init__(self, layout: _Optional[_Union[WebUILayoutSettingsDTO, _Mapping]] = ..., session_sidebar: _Optional[_Union[WebUISessionSidebarSettingsDTO, _Mapping]] = ..., workspace_file_tree: _Optional[_Union[WebUIWorkspaceFileTreeSettingsDTO, _Mapping]] = ..., gateway_console: _Optional[_Union[WebUIGatewayConsoleSettingsDTO, _Mapping]] = ..., theme: _Optional[_Union[WebUIThemeSettingsUpdateDTO, _Mapping]] = ...) -> None: ...

class WebUIThemeSettingsDTO(_message.Message):
    __slots__ = ("theme_id", "background", "resolved_theme")
    THEME_ID_FIELD_NUMBER: _ClassVar[int]
    BACKGROUND_FIELD_NUMBER: _ClassVar[int]
    RESOLVED_THEME_FIELD_NUMBER: _ClassVar[int]
    theme_id: str
    background: GatewayThemeBackgroundDTO
    resolved_theme: ResolvedGatewayThemeDTO
    def __init__(self, theme_id: _Optional[str] = ..., background: _Optional[_Union[GatewayThemeBackgroundDTO, _Mapping]] = ..., resolved_theme: _Optional[_Union[ResolvedGatewayThemeDTO, _Mapping]] = ...) -> None: ...

class WebUIThemeSettingsUpdateDTO(_message.Message):
    __slots__ = ("theme_id", "background")
    THEME_ID_FIELD_NUMBER: _ClassVar[int]
    BACKGROUND_FIELD_NUMBER: _ClassVar[int]
    theme_id: str
    background: GatewayThemeBackgroundDTO
    def __init__(self, theme_id: _Optional[str] = ..., background: _Optional[_Union[GatewayThemeBackgroundDTO, _Mapping]] = ...) -> None: ...

class WebUIWorkspaceBottomPanelSettingsDTO(_message.Message):
    __slots__ = ("visible", "height", "tab", "terminal_id")
    VISIBLE_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    TAB_FIELD_NUMBER: _ClassVar[int]
    TERMINAL_ID_FIELD_NUMBER: _ClassVar[int]
    visible: bool
    height: int
    tab: str
    terminal_id: str
    def __init__(self, visible: _Optional[bool] = ..., height: _Optional[int] = ..., tab: _Optional[str] = ..., terminal_id: _Optional[str] = ...) -> None: ...

class WebUIWorkspaceFileTreeSettingsDTO(_message.Message):
    __slots__ = ("expanded_paths_by_workspace",)
    EXPANDED_PATHS_BY_WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    expanded_paths_by_workspace: _struct_pb2.Struct
    def __init__(self, expanded_paths_by_workspace: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class WorkspaceFolderCreateRequest(_message.Message):
    __slots__ = ("name", "parent_node_id", "position")
    NAME_FIELD_NUMBER: _ClassVar[int]
    PARENT_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    POSITION_FIELD_NUMBER: _ClassVar[int]
    name: str
    parent_node_id: str
    position: int
    def __init__(self, name: _Optional[str] = ..., parent_node_id: _Optional[str] = ..., position: _Optional[int] = ...) -> None: ...

class WorkspaceNavigationBreadcrumbDTO(_message.Message):
    __slots__ = ("revision", "items")
    REVISION_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    revision: str
    items: _containers.RepeatedCompositeFieldContainer[WorkspaceNavigationNodeDTO]
    def __init__(self, revision: _Optional[str] = ..., items: _Optional[_Iterable[_Union[WorkspaceNavigationNodeDTO, _Mapping]]] = ...) -> None: ...

class WorkspaceNavigationNodeDTO(_message.Message):
    __slots__ = ("node_id", "kind", "name", "parent_node_id", "position", "workspace")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    PARENT_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    POSITION_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    kind: str
    name: str
    parent_node_id: str
    position: int
    workspace: GatewaySearchWorkspaceOwnerDTO
    def __init__(self, node_id: _Optional[str] = ..., kind: _Optional[str] = ..., name: _Optional[str] = ..., parent_node_id: _Optional[str] = ..., position: _Optional[int] = ..., workspace: _Optional[_Union[GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ...) -> None: ...

class WorkspaceNavigationNodeUpdateRequest(_message.Message):
    __slots__ = ("name", "parent_node_id", "position")
    NAME_FIELD_NUMBER: _ClassVar[int]
    PARENT_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    POSITION_FIELD_NUMBER: _ClassVar[int]
    name: str
    parent_node_id: str
    position: int
    def __init__(self, name: _Optional[str] = ..., parent_node_id: _Optional[str] = ..., position: _Optional[int] = ...) -> None: ...

class WorkspaceNavigationPlacementRequest(_message.Message):
    __slots__ = ("node_id", "parent_node_id", "mode", "target_node_id")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    PARENT_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    TARGET_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    parent_node_id: str
    mode: str
    target_node_id: str
    def __init__(self, node_id: _Optional[str] = ..., parent_node_id: _Optional[str] = ..., mode: _Optional[str] = ..., target_node_id: _Optional[str] = ...) -> None: ...

class WorkspaceNavigationReorderRequest(_message.Message):
    __slots__ = ("parent_node_id", "node_ids")
    PARENT_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    NODE_IDS_FIELD_NUMBER: _ClassVar[int]
    parent_node_id: str
    node_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, parent_node_id: _Optional[str] = ..., node_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class WorkspaceNavigationTreeDTO(_message.Message):
    __slots__ = ("revision", "nodes")
    REVISION_FIELD_NUMBER: _ClassVar[int]
    NODES_FIELD_NUMBER: _ClassVar[int]
    revision: str
    nodes: _containers.RepeatedCompositeFieldContainer[WorkspaceNavigationNodeDTO]
    def __init__(self, revision: _Optional[str] = ..., nodes: _Optional[_Iterable[_Union[WorkspaceNavigationNodeDTO, _Mapping]]] = ...) -> None: ...

class GatewayManagementTargetDTO(_message.Message):
    __slots__ = ("gateway_id", "name", "connection_kind", "gateway_connection_id", "connection_error")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_KIND_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ERROR_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    name: str
    connection_kind: str
    gateway_connection_id: str
    connection_error: str
    def __init__(self, gateway_id: _Optional[str] = ..., name: _Optional[str] = ..., connection_kind: _Optional[str] = ..., gateway_connection_id: _Optional[str] = ..., connection_error: _Optional[str] = ...) -> None: ...

class GatewayManagementDirectoryDTO(_message.Message):
    __slots__ = ("local_gateway_id", "items")
    LOCAL_GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    local_gateway_id: str
    items: _containers.RepeatedCompositeFieldContainer[GatewayManagementTargetDTO]
    def __init__(self, local_gateway_id: _Optional[str] = ..., items: _Optional[_Iterable[_Union[GatewayManagementTargetDTO, _Mapping]]] = ...) -> None: ...
