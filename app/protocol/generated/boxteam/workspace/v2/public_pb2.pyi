import datetime

from boxteam.gateway.v2 import service_plugin_pb2 as _service_plugin_pb2
from boxteam.workspace.v2 import message_pb2 as _message_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class APIResponse(_message.Message):
    __slots__ = ("code", "message", "data", "request_id")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    code: int
    message: str
    data: _struct_pb2.Struct
    request_id: str
    def __init__(self, code: _Optional[int] = ..., message: _Optional[str] = ..., data: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., request_id: _Optional[str] = ...) -> None: ...

class AgentDTO(_message.Message):
    __slots__ = ("agent_id", "name", "description", "model", "tools", "capabilities", "providers", "workspace_default")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    TOOLS_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    PROVIDERS_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_DEFAULT_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    name: str
    description: str
    model: str
    tools: _containers.RepeatedScalarFieldContainer[str]
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    providers: _containers.RepeatedCompositeFieldContainer[AgentProviderDTO]
    workspace_default: bool
    def __init__(self, agent_id: _Optional[str] = ..., name: _Optional[str] = ..., description: _Optional[str] = ..., model: _Optional[str] = ..., tools: _Optional[_Iterable[str]] = ..., capabilities: _Optional[_Iterable[str]] = ..., providers: _Optional[_Iterable[_Union[AgentProviderDTO, _Mapping]]] = ..., workspace_default: _Optional[bool] = ...) -> None: ...

class AgentProviderDTO(_message.Message):
    __slots__ = ("provider_id", "model", "custom_llm_provider", "workspace_default", "available", "configuration_error")
    PROVIDER_ID_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    CUSTOM_LLM_PROVIDER_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_DEFAULT_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_ERROR_FIELD_NUMBER: _ClassVar[int]
    provider_id: str
    model: str
    custom_llm_provider: str
    workspace_default: bool
    available: bool
    configuration_error: str
    def __init__(self, provider_id: _Optional[str] = ..., model: _Optional[str] = ..., custom_llm_provider: _Optional[str] = ..., workspace_default: _Optional[bool] = ..., available: _Optional[bool] = ..., configuration_error: _Optional[str] = ...) -> None: ...

class AgentStateMessagesDTO(_message.Message):
    __slots__ = ("session_id", "message_count", "jsonl")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_COUNT_FIELD_NUMBER: _ClassVar[int]
    JSONL_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    message_count: int
    jsonl: str
    def __init__(self, session_id: _Optional[str] = ..., message_count: _Optional[int] = ..., jsonl: _Optional[str] = ...) -> None: ...

class ArtifactDTO(_message.Message):
    __slots__ = ("artifact_id", "job_id", "type", "name", "path", "metadata")
    ARTIFACT_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    artifact_id: str
    job_id: str
    type: str
    name: str
    path: str
    metadata: _struct_pb2.Struct
    def __init__(self, artifact_id: _Optional[str] = ..., job_id: _Optional[str] = ..., type: _Optional[str] = ..., name: _Optional[str] = ..., path: _Optional[str] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ConfigDTO(_message.Message):
    __slots__ = ("default_model", "default_orchestration", "max_concurrent_agents", "allow_shell_tools", "ignored_paths", "auto_summarize", "metadata")
    DEFAULT_MODEL_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_ORCHESTRATION_FIELD_NUMBER: _ClassVar[int]
    MAX_CONCURRENT_AGENTS_FIELD_NUMBER: _ClassVar[int]
    ALLOW_SHELL_TOOLS_FIELD_NUMBER: _ClassVar[int]
    IGNORED_PATHS_FIELD_NUMBER: _ClassVar[int]
    AUTO_SUMMARIZE_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    default_model: str
    default_orchestration: str
    max_concurrent_agents: int
    allow_shell_tools: bool
    ignored_paths: _containers.RepeatedScalarFieldContainer[str]
    auto_summarize: bool
    metadata: _struct_pb2.Struct
    def __init__(self, default_model: _Optional[str] = ..., default_orchestration: _Optional[str] = ..., max_concurrent_agents: _Optional[int] = ..., allow_shell_tools: _Optional[bool] = ..., ignored_paths: _Optional[_Iterable[str]] = ..., auto_summarize: _Optional[bool] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ConfigReloadStatusDTO(_message.Message):
    __slots__ = ("healthy", "revision", "restart_required", "reason", "changed_sections", "last_success_at", "last_attempt_at", "last_error", "mcp_cleanup", "state", "active_revision", "pending_revision", "candidate_id", "candidate_ref", "attempt_id", "apply_id", "layer_digests", "applied_paths", "deferred_paths")
    class LayerDigestsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _struct_pb2.Value
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ...) -> None: ...
    HEALTHY_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    RESTART_REQUIRED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    CHANGED_SECTIONS_FIELD_NUMBER: _ClassVar[int]
    LAST_SUCCESS_AT_FIELD_NUMBER: _ClassVar[int]
    LAST_ATTEMPT_AT_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_FIELD_NUMBER: _ClassVar[int]
    MCP_CLEANUP_FIELD_NUMBER: _ClassVar[int]
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
    healthy: bool
    revision: str
    restart_required: bool
    reason: str
    changed_sections: _containers.RepeatedScalarFieldContainer[str]
    last_success_at: str
    last_attempt_at: str
    last_error: str
    mcp_cleanup: _containers.RepeatedCompositeFieldContainer[McpCleanupDTO]
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
    def __init__(self, healthy: _Optional[bool] = ..., revision: _Optional[str] = ..., restart_required: _Optional[bool] = ..., reason: _Optional[str] = ..., changed_sections: _Optional[_Iterable[str]] = ..., last_success_at: _Optional[str] = ..., last_attempt_at: _Optional[str] = ..., last_error: _Optional[str] = ..., mcp_cleanup: _Optional[_Iterable[_Union[McpCleanupDTO, _Mapping]]] = ..., state: _Optional[str] = ..., active_revision: _Optional[int] = ..., pending_revision: _Optional[int] = ..., candidate_id: _Optional[str] = ..., candidate_ref: _Optional[str] = ..., attempt_id: _Optional[str] = ..., apply_id: _Optional[str] = ..., layer_digests: _Optional[_Mapping[str, _struct_pb2.Value]] = ..., applied_paths: _Optional[_Iterable[str]] = ..., deferred_paths: _Optional[_Iterable[str]] = ...) -> None: ...

class ConfigSourceEditRequest(_message.Message):
    __slots__ = ("gateway_id", "workspace_id", "source_key", "jsonc", "base_file_digest", "base_layer_revision", "base_layer_digest", "expected_active_revision", "expected_active_digest", "idempotency_key")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_KEY_FIELD_NUMBER: _ClassVar[int]
    JSONC_FIELD_NUMBER: _ClassVar[int]
    BASE_FILE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    BASE_LAYER_REVISION_FIELD_NUMBER: _ClassVar[int]
    BASE_LAYER_DIGEST_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_ACTIVE_REVISION_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_ACTIVE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    workspace_id: str
    source_key: str
    jsonc: str
    base_file_digest: _struct_pb2.Value
    base_layer_revision: _struct_pb2.Value
    base_layer_digest: _struct_pb2.Value
    expected_active_revision: _struct_pb2.Value
    expected_active_digest: _struct_pb2.Value
    idempotency_key: str
    def __init__(self, gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., source_key: _Optional[str] = ..., jsonc: _Optional[str] = ..., base_file_digest: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., base_layer_revision: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., base_layer_digest: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., expected_active_revision: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., expected_active_digest: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., idempotency_key: _Optional[str] = ...) -> None: ...

class ConfigSourceLayerViewDTO(_message.Message):
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

class ConfigSourceOriginDTO(_message.Message):
    __slots__ = ("path", "source_key", "shadowed_sources")
    PATH_FIELD_NUMBER: _ClassVar[int]
    SOURCE_KEY_FIELD_NUMBER: _ClassVar[int]
    SHADOWED_SOURCES_FIELD_NUMBER: _ClassVar[int]
    path: str
    source_key: str
    shadowed_sources: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, path: _Optional[str] = ..., source_key: _Optional[str] = ..., shadowed_sources: _Optional[_Iterable[str]] = ...) -> None: ...

class ConfigSourceEditDTO(_message.Message):
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

class ConfigSourceDocumentDTO(_message.Message):
    __slots__ = ("gateway_id", "workspace_id", "source_key", "layer", "writable", "jsonc", "file_presence", "file_digest", "layer_revision", "layer_digest", "source_generation", "active_revision", "active_digest", "json_schema", "effective", "source_effective", "layers", "origins", "status", "edit")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
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
    workspace_id: str
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
    layers: _containers.RepeatedCompositeFieldContainer[ConfigSourceLayerViewDTO]
    origins: _containers.RepeatedCompositeFieldContainer[ConfigSourceOriginDTO]
    status: ConfigReloadStatusDTO
    edit: ConfigSourceEditDTO
    def __init__(self, gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., source_key: _Optional[str] = ..., layer: _Optional[str] = ..., writable: _Optional[bool] = ..., jsonc: _Optional[str] = ..., file_presence: _Optional[str] = ..., file_digest: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., layer_revision: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., layer_digest: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., source_generation: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., active_revision: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., active_digest: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., json_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., effective: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., source_effective: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., layers: _Optional[_Iterable[_Union[ConfigSourceLayerViewDTO, _Mapping]]] = ..., origins: _Optional[_Iterable[_Union[ConfigSourceOriginDTO, _Mapping]]] = ..., status: _Optional[_Union[ConfigReloadStatusDTO, _Mapping]] = ..., edit: _Optional[_Union[ConfigSourceEditDTO, _Mapping]] = ...) -> None: ...

class ConfigSourcePreflightDTO(_message.Message):
    __slots__ = ("document", "candidate", "candidate_digest", "changed_paths")
    DOCUMENT_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    CHANGED_PATHS_FIELD_NUMBER: _ClassVar[int]
    document: ConfigSourceDocumentDTO
    candidate: _struct_pb2.Struct
    candidate_digest: str
    changed_paths: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, document: _Optional[_Union[ConfigSourceDocumentDTO, _Mapping]] = ..., candidate: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., candidate_digest: _Optional[str] = ..., changed_paths: _Optional[_Iterable[str]] = ...) -> None: ...

class ConfigEventDTO(_message.Message):
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

class McpCleanupDTO(_message.Message):
    __slots__ = ("server_id", "connection_ref", "phase", "release_verified", "error_type")
    SERVER_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_REF_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    RELEASE_VERIFIED_FIELD_NUMBER: _ClassVar[int]
    ERROR_TYPE_FIELD_NUMBER: _ClassVar[int]
    server_id: str
    connection_ref: str
    phase: str
    release_verified: bool
    error_type: str
    def __init__(self, server_id: _Optional[str] = ..., connection_ref: _Optional[str] = ..., phase: _Optional[str] = ..., release_verified: _Optional[bool] = ..., error_type: _Optional[str] = ...) -> None: ...

class ConfigSourceDTO(_message.Message):
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

class ConfigSourcesDTO(_message.Message):
    __slots__ = ("revision", "sources", "runtime_overrides", "schema_uri")
    REVISION_FIELD_NUMBER: _ClassVar[int]
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_OVERRIDES_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_URI_FIELD_NUMBER: _ClassVar[int]
    revision: str
    sources: _containers.RepeatedCompositeFieldContainer[ConfigSourceDTO]
    runtime_overrides: _containers.RepeatedScalarFieldContainer[str]
    schema_uri: str
    def __init__(self, revision: _Optional[str] = ..., sources: _Optional[_Iterable[_Union[ConfigSourceDTO, _Mapping]]] = ..., runtime_overrides: _Optional[_Iterable[str]] = ..., schema_uri: _Optional[str] = ...) -> None: ...

class ConfigUpdateRequest(_message.Message):
    __slots__ = ("default_model", "default_orchestration", "max_concurrent_agents", "allow_shell_tools", "ignored_paths", "auto_summarize")
    DEFAULT_MODEL_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_ORCHESTRATION_FIELD_NUMBER: _ClassVar[int]
    MAX_CONCURRENT_AGENTS_FIELD_NUMBER: _ClassVar[int]
    ALLOW_SHELL_TOOLS_FIELD_NUMBER: _ClassVar[int]
    IGNORED_PATHS_FIELD_NUMBER: _ClassVar[int]
    AUTO_SUMMARIZE_FIELD_NUMBER: _ClassVar[int]
    default_model: str
    default_orchestration: str
    max_concurrent_agents: int
    allow_shell_tools: bool
    ignored_paths: _containers.RepeatedScalarFieldContainer[str]
    auto_summarize: bool
    def __init__(self, default_model: _Optional[str] = ..., default_orchestration: _Optional[str] = ..., max_concurrent_agents: _Optional[int] = ..., allow_shell_tools: _Optional[bool] = ..., ignored_paths: _Optional[_Iterable[str]] = ..., auto_summarize: _Optional[bool] = ...) -> None: ...

class ChildThreadListDTO(_message.Message):
    __slots__ = ("parent_session_id", "items", "total", "teams")
    PARENT_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    TEAMS_FIELD_NUMBER: _ClassVar[int]
    parent_session_id: str
    items: _containers.RepeatedCompositeFieldContainer[ChildThreadSummaryDTO]
    total: int
    teams: _containers.RepeatedCompositeFieldContainer[TeamBoardDTO]
    def __init__(self, parent_session_id: _Optional[str] = ..., items: _Optional[_Iterable[_Union[ChildThreadSummaryDTO, _Mapping]]] = ..., total: _Optional[int] = ..., teams: _Optional[_Iterable[_Union[TeamBoardDTO, _Mapping]]] = ...) -> None: ...

class SessionCatalogNodeDTO(_message.Message):
    __slots__ = ("node_id", "kind", "name", "parent_node_id", "session_id", "folder_id", "has_children", "node_revision", "state", "created_at", "updated_at", "session")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    PARENT_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    FOLDER_ID_FIELD_NUMBER: _ClassVar[int]
    HAS_CHILDREN_FIELD_NUMBER: _ClassVar[int]
    NODE_REVISION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    SESSION_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    kind: str
    name: str
    parent_node_id: str
    session_id: str
    folder_id: str
    has_children: bool
    node_revision: int
    state: str
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    session: SessionDTO
    def __init__(self, node_id: _Optional[str] = ..., kind: _Optional[str] = ..., name: _Optional[str] = ..., parent_node_id: _Optional[str] = ..., session_id: _Optional[str] = ..., folder_id: _Optional[str] = ..., has_children: _Optional[bool] = ..., node_revision: _Optional[int] = ..., state: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., session: _Optional[_Union[SessionDTO, _Mapping]] = ...) -> None: ...

class SessionCatalogPageDTO(_message.Message):
    __slots__ = ("revision", "catalog_revision", "parent_node_id", "items", "cursor", "total", "consistency_warning")
    REVISION_FIELD_NUMBER: _ClassVar[int]
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    PARENT_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    CONSISTENCY_WARNING_FIELD_NUMBER: _ClassVar[int]
    revision: str
    catalog_revision: int
    parent_node_id: str
    items: _containers.RepeatedCompositeFieldContainer[SessionCatalogNodeDTO]
    cursor: str
    total: int
    consistency_warning: str
    def __init__(self, revision: _Optional[str] = ..., catalog_revision: _Optional[int] = ..., parent_node_id: _Optional[str] = ..., items: _Optional[_Iterable[_Union[SessionCatalogNodeDTO, _Mapping]]] = ..., cursor: _Optional[str] = ..., total: _Optional[int] = ..., consistency_warning: _Optional[str] = ...) -> None: ...

class SessionCatalogBreadcrumbDTO(_message.Message):
    __slots__ = ("revision", "catalog_revision", "items")
    REVISION_FIELD_NUMBER: _ClassVar[int]
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    revision: str
    catalog_revision: int
    items: _containers.RepeatedCompositeFieldContainer[SessionCatalogNodeDTO]
    def __init__(self, revision: _Optional[str] = ..., catalog_revision: _Optional[int] = ..., items: _Optional[_Iterable[_Union[SessionCatalogNodeDTO, _Mapping]]] = ...) -> None: ...

class SessionCatalogSearchResultDTO(_message.Message):
    __slots__ = ("node", "breadcrumb", "relative_path")
    NODE_FIELD_NUMBER: _ClassVar[int]
    BREADCRUMB_FIELD_NUMBER: _ClassVar[int]
    RELATIVE_PATH_FIELD_NUMBER: _ClassVar[int]
    node: SessionCatalogNodeDTO
    breadcrumb: _containers.RepeatedCompositeFieldContainer[SessionCatalogNodeDTO]
    relative_path: str
    def __init__(self, node: _Optional[_Union[SessionCatalogNodeDTO, _Mapping]] = ..., breadcrumb: _Optional[_Iterable[_Union[SessionCatalogNodeDTO, _Mapping]]] = ..., relative_path: _Optional[str] = ...) -> None: ...

class SessionCatalogSearchResultsDTO(_message.Message):
    __slots__ = ("revision", "catalog_revision", "items", "cursor", "total")
    REVISION_FIELD_NUMBER: _ClassVar[int]
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    revision: str
    catalog_revision: int
    items: _containers.RepeatedCompositeFieldContainer[SessionCatalogSearchResultDTO]
    cursor: str
    total: int
    def __init__(self, revision: _Optional[str] = ..., catalog_revision: _Optional[int] = ..., items: _Optional[_Iterable[_Union[SessionCatalogSearchResultDTO, _Mapping]]] = ..., cursor: _Optional[str] = ..., total: _Optional[int] = ...) -> None: ...

class SessionCatalogExportDTO(_message.Message):
    __slots__ = ("revision", "catalog_revision", "items")
    REVISION_FIELD_NUMBER: _ClassVar[int]
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    revision: str
    catalog_revision: int
    items: _containers.RepeatedCompositeFieldContainer[SessionCatalogNodeDTO]
    def __init__(self, revision: _Optional[str] = ..., catalog_revision: _Optional[int] = ..., items: _Optional[_Iterable[_Union[SessionCatalogNodeDTO, _Mapping]]] = ...) -> None: ...

class SearchProviderCapabilityDTO(_message.Message):
    __slots__ = ("provider_id", "category", "operations", "contract_version", "supported_options", "position_kind", "max_results", "cache_policy", "match_modes")
    PROVIDER_ID_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    OPERATIONS_FIELD_NUMBER: _ClassVar[int]
    CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    SUPPORTED_OPTIONS_FIELD_NUMBER: _ClassVar[int]
    POSITION_KIND_FIELD_NUMBER: _ClassVar[int]
    MAX_RESULTS_FIELD_NUMBER: _ClassVar[int]
    CACHE_POLICY_FIELD_NUMBER: _ClassVar[int]
    MATCH_MODES_FIELD_NUMBER: _ClassVar[int]
    provider_id: str
    category: str
    operations: _containers.RepeatedScalarFieldContainer[str]
    contract_version: int
    supported_options: _containers.RepeatedScalarFieldContainer[str]
    position_kind: str
    max_results: int
    cache_policy: str
    match_modes: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, provider_id: _Optional[str] = ..., category: _Optional[str] = ..., operations: _Optional[_Iterable[str]] = ..., contract_version: _Optional[int] = ..., supported_options: _Optional[_Iterable[str]] = ..., position_kind: _Optional[str] = ..., max_results: _Optional[int] = ..., cache_policy: _Optional[str] = ..., match_modes: _Optional[_Iterable[str]] = ...) -> None: ...

class WorkspaceSearchCapabilitiesDTO(_message.Message):
    __slots__ = ("workspace_id", "providers")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PROVIDERS_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    providers: _containers.RepeatedCompositeFieldContainer[SearchProviderCapabilityDTO]
    def __init__(self, workspace_id: _Optional[str] = ..., providers: _Optional[_Iterable[_Union[SearchProviderCapabilityDTO, _Mapping]]] = ...) -> None: ...

class WorkspaceFileSearchTargetDTO(_message.Message):
    __slots__ = ("uri", "search_options", "max_bytes")
    URI_FIELD_NUMBER: _ClassVar[int]
    SEARCH_OPTIONS_FIELD_NUMBER: _ClassVar[int]
    MAX_BYTES_FIELD_NUMBER: _ClassVar[int]
    uri: str
    search_options: ResourceNavigationSearchOptions
    max_bytes: int
    def __init__(self, uri: _Optional[str] = ..., search_options: _Optional[_Union[ResourceNavigationSearchOptions, _Mapping]] = ..., max_bytes: _Optional[int] = ...) -> None: ...

class WorkspaceContextSearchOptionsDTO(_message.Message):
    __slots__ = ("sources", "case_sensitive", "max_chars", "revision")
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    CASE_SENSITIVE_FIELD_NUMBER: _ClassVar[int]
    MAX_CHARS_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    sources: _containers.RepeatedScalarFieldContainer[str]
    case_sensitive: bool
    max_chars: int
    revision: str
    def __init__(self, sources: _Optional[_Iterable[str]] = ..., case_sensitive: _Optional[bool] = ..., max_chars: _Optional[int] = ..., revision: _Optional[str] = ...) -> None: ...

class WorkspaceSearchRequest(_message.Message):
    __slots__ = ("workspace_id", "category", "operation", "contract_version", "query", "match_mode", "limit", "cursor", "deadline_at", "file", "context")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    MATCH_MODE_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_AT_FIELD_NUMBER: _ClassVar[int]
    FILE_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    category: str
    operation: str
    contract_version: int
    query: str
    match_mode: str
    limit: int
    cursor: str
    deadline_at: float
    file: WorkspaceFileSearchTargetDTO
    context: WorkspaceContextSearchOptionsDTO
    def __init__(self, workspace_id: _Optional[str] = ..., category: _Optional[str] = ..., operation: _Optional[str] = ..., contract_version: _Optional[int] = ..., query: _Optional[str] = ..., match_mode: _Optional[str] = ..., limit: _Optional[int] = ..., cursor: _Optional[str] = ..., deadline_at: _Optional[float] = ..., file: _Optional[_Union[WorkspaceFileSearchTargetDTO, _Mapping]] = ..., context: _Optional[_Union[WorkspaceContextSearchOptionsDTO, _Mapping]] = ...) -> None: ...

class WorkspaceSearchResultDTO(_message.Message):
    __slots__ = ("provider_id", "workspace_id", "category", "operation", "contract_version", "state", "limited_reason", "catalog", "threads", "messages", "llm_requests", "files", "context", "traces")
    PROVIDER_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    LIMITED_REASON_FIELD_NUMBER: _ClassVar[int]
    CATALOG_FIELD_NUMBER: _ClassVar[int]
    THREADS_FIELD_NUMBER: _ClassVar[int]
    MESSAGES_FIELD_NUMBER: _ClassVar[int]
    LLM_REQUESTS_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    TRACES_FIELD_NUMBER: _ClassVar[int]
    provider_id: str
    workspace_id: str
    category: str
    operation: str
    contract_version: int
    state: str
    limited_reason: str
    catalog: SessionCatalogSearchResultsDTO
    threads: SessionThreadSearchResultsDTO
    messages: WorkspaceMessageSearchResultsDTO
    llm_requests: WorkspaceLLMRequestLogSearchResultsDTO
    files: ResourceNavigationResultDTO
    context: SessionContextSearchResultDTO
    traces: WorkspaceTraceSearchResultsDTO
    def __init__(self, provider_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., category: _Optional[str] = ..., operation: _Optional[str] = ..., contract_version: _Optional[int] = ..., state: _Optional[str] = ..., limited_reason: _Optional[str] = ..., catalog: _Optional[_Union[SessionCatalogSearchResultsDTO, _Mapping]] = ..., threads: _Optional[_Union[SessionThreadSearchResultsDTO, _Mapping]] = ..., messages: _Optional[_Union[WorkspaceMessageSearchResultsDTO, _Mapping]] = ..., llm_requests: _Optional[_Union[WorkspaceLLMRequestLogSearchResultsDTO, _Mapping]] = ..., files: _Optional[_Union[ResourceNavigationResultDTO, _Mapping]] = ..., context: _Optional[_Union[SessionContextSearchResultDTO, _Mapping]] = ..., traces: _Optional[_Union[WorkspaceTraceSearchResultsDTO, _Mapping]] = ...) -> None: ...

class SessionThreadSearchResultDTO(_message.Message):
    __slots__ = ("workspace_id", "session_id", "thread_id", "thread_uri", "kind", "title", "session_title", "created_at")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_URI_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    SESSION_TITLE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    session_id: str
    thread_id: str
    thread_uri: str
    kind: str
    title: str
    session_title: str
    created_at: _timestamp_pb2.Timestamp
    def __init__(self, workspace_id: _Optional[str] = ..., session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., thread_uri: _Optional[str] = ..., kind: _Optional[str] = ..., title: _Optional[str] = ..., session_title: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class SessionThreadSearchResultsDTO(_message.Message):
    __slots__ = ("catalog_revision", "items", "cursor", "complete", "limited_reason", "scanned_sessions", "scanned_threads")
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    COMPLETE_FIELD_NUMBER: _ClassVar[int]
    LIMITED_REASON_FIELD_NUMBER: _ClassVar[int]
    SCANNED_SESSIONS_FIELD_NUMBER: _ClassVar[int]
    SCANNED_THREADS_FIELD_NUMBER: _ClassVar[int]
    catalog_revision: int
    items: _containers.RepeatedCompositeFieldContainer[SessionThreadSearchResultDTO]
    cursor: str
    complete: bool
    limited_reason: str
    scanned_sessions: int
    scanned_threads: int
    def __init__(self, catalog_revision: _Optional[int] = ..., items: _Optional[_Iterable[_Union[SessionThreadSearchResultDTO, _Mapping]]] = ..., cursor: _Optional[str] = ..., complete: _Optional[bool] = ..., limited_reason: _Optional[str] = ..., scanned_sessions: _Optional[int] = ..., scanned_threads: _Optional[int] = ...) -> None: ...

class ChildThreadSummaryDTO(_message.Message):
    __slots__ = ("thread_id", "created_at", "delegation_id", "role", "subagent_type", "title", "collaboration_state", "admission_state", "status", "runtime", "report_execution_id", "report_status")
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    DELEGATION_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    SUBAGENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    COLLABORATION_STATE_FIELD_NUMBER: _ClassVar[int]
    ADMISSION_STATE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_FIELD_NUMBER: _ClassVar[int]
    REPORT_EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    REPORT_STATUS_FIELD_NUMBER: _ClassVar[int]
    thread_id: str
    created_at: _timestamp_pb2.Timestamp
    delegation_id: str
    role: str
    subagent_type: str
    title: str
    collaboration_state: str
    admission_state: str
    status: str
    runtime: ChildThreadRuntimeDTO
    report_execution_id: str
    report_status: str
    def __init__(self, thread_id: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., delegation_id: _Optional[str] = ..., role: _Optional[str] = ..., subagent_type: _Optional[str] = ..., title: _Optional[str] = ..., collaboration_state: _Optional[str] = ..., admission_state: _Optional[str] = ..., status: _Optional[str] = ..., runtime: _Optional[_Union[ChildThreadRuntimeDTO, _Mapping]] = ..., report_execution_id: _Optional[str] = ..., report_status: _Optional[str] = ...) -> None: ...

class ChildThreadBlockerDTO(_message.Message):
    __slots__ = ("kind", "reason")
    KIND_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    kind: str
    reason: str
    def __init__(self, kind: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class ChildThreadRuntimeDTO(_message.Message):
    __slots__ = ("residency", "cold_eligible", "execution_state", "last_activity_at", "idle_deadline", "blockers")
    RESIDENCY_FIELD_NUMBER: _ClassVar[int]
    COLD_ELIGIBLE_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_STATE_FIELD_NUMBER: _ClassVar[int]
    LAST_ACTIVITY_AT_FIELD_NUMBER: _ClassVar[int]
    IDLE_DEADLINE_FIELD_NUMBER: _ClassVar[int]
    BLOCKERS_FIELD_NUMBER: _ClassVar[int]
    residency: str
    cold_eligible: bool
    execution_state: str
    last_activity_at: _timestamp_pb2.Timestamp
    idle_deadline: _timestamp_pb2.Timestamp
    blockers: _containers.RepeatedCompositeFieldContainer[ChildThreadBlockerDTO]
    def __init__(self, residency: _Optional[str] = ..., cold_eligible: _Optional[bool] = ..., execution_state: _Optional[str] = ..., last_activity_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., idle_deadline: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., blockers: _Optional[_Iterable[_Union[ChildThreadBlockerDTO, _Mapping]]] = ...) -> None: ...

class CursorPage(_message.Message):
    __slots__ = ("items", "next_cursor", "has_more")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    HAS_MORE_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    next_cursor: str
    has_more: bool
    def __init__(self, items: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., next_cursor: _Optional[str] = ..., has_more: _Optional[bool] = ...) -> None: ...

class DeleteSessionResultDTO(_message.Message):
    __slots__ = ("session_id", "status")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    status: str
    def __init__(self, session_id: _Optional[str] = ..., status: _Optional[str] = ...) -> None: ...

class EntityRef(_message.Message):
    __slots__ = ("id", "name")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class FileTreeShortcutDTO(_message.Message):
    __slots__ = ("path", "label", "source")
    PATH_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    path: str
    label: str
    source: str
    def __init__(self, path: _Optional[str] = ..., label: _Optional[str] = ..., source: _Optional[str] = ...) -> None: ...

class FileTreeShortcutRequest(_message.Message):
    __slots__ = ("path", "label")
    PATH_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    path: str
    label: str
    def __init__(self, path: _Optional[str] = ..., label: _Optional[str] = ...) -> None: ...

class GoalJobAccountingDTO(_message.Message):
    __slots__ = ("tokens", "elapsed_seconds", "time_closed")
    TOKENS_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_SECONDS_FIELD_NUMBER: _ClassVar[int]
    TIME_CLOSED_FIELD_NUMBER: _ClassVar[int]
    tokens: int
    elapsed_seconds: int
    time_closed: bool
    def __init__(self, tokens: _Optional[int] = ..., elapsed_seconds: _Optional[int] = ..., time_closed: _Optional[bool] = ...) -> None: ...

class JobControlRequest(_message.Message):
    __slots__ = ("scope", "action", "agent_id", "step_id", "message", "params")
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    STEP_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    PARAMS_FIELD_NUMBER: _ClassVar[int]
    scope: str
    action: str
    agent_id: str
    step_id: str
    message: str
    params: _struct_pb2.Struct
    def __init__(self, scope: _Optional[str] = ..., action: _Optional[str] = ..., agent_id: _Optional[str] = ..., step_id: _Optional[str] = ..., message: _Optional[str] = ..., params: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class JobControlResponseDTO(_message.Message):
    __slots__ = ("job_id", "status", "control_state", "session_id", "thread_id")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CONTROL_STATE_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    status: str
    control_state: str
    session_id: str
    thread_id: str
    def __init__(self, job_id: _Optional[str] = ..., status: _Optional[str] = ..., control_state: _Optional[str] = ..., session_id: _Optional[str] = ..., thread_id: _Optional[str] = ...) -> None: ...

class JobDTO(_message.Message):
    __slots__ = ("created_at", "updated_at", "job_id", "message_id", "session_id", "mode", "status", "entry_agent", "progress", "current_step", "error_message", "metadata", "ended_at", "thread_id")
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ENTRY_AGENT_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_STEP_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    job_id: str
    message_id: str
    session_id: str
    mode: str
    status: str
    entry_agent: str
    progress: int
    current_step: str
    error_message: str
    metadata: _struct_pb2.Struct
    ended_at: _timestamp_pb2.Timestamp
    thread_id: str
    def __init__(self, created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., job_id: _Optional[str] = ..., message_id: _Optional[str] = ..., session_id: _Optional[str] = ..., mode: _Optional[str] = ..., status: _Optional[str] = ..., entry_agent: _Optional[str] = ..., progress: _Optional[int] = ..., current_step: _Optional[str] = ..., error_message: _Optional[str] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., ended_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., thread_id: _Optional[str] = ...) -> None: ...

class JobDispatchSnapshotDTO(_message.Message):
    __slots__ = ("session_id", "job_id", "job_status", "active_job_id", "blocked_by_job_id", "queued_jobs_ahead", "queued_job_count", "pending_job_count", "delivery_policy", "enqueue_sequence", "queue_snapshot_version", "thread_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_STATUS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCKED_BY_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    QUEUED_JOBS_AHEAD_FIELD_NUMBER: _ClassVar[int]
    QUEUED_JOB_COUNT_FIELD_NUMBER: _ClassVar[int]
    PENDING_JOB_COUNT_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_POLICY_FIELD_NUMBER: _ClassVar[int]
    ENQUEUE_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    QUEUE_SNAPSHOT_VERSION_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    job_id: str
    job_status: str
    active_job_id: str
    blocked_by_job_id: str
    queued_jobs_ahead: int
    queued_job_count: int
    pending_job_count: int
    delivery_policy: str
    enqueue_sequence: int
    queue_snapshot_version: int
    thread_id: str
    def __init__(self, session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., job_status: _Optional[str] = ..., active_job_id: _Optional[str] = ..., blocked_by_job_id: _Optional[str] = ..., queued_jobs_ahead: _Optional[int] = ..., queued_job_count: _Optional[int] = ..., pending_job_count: _Optional[int] = ..., delivery_policy: _Optional[str] = ..., enqueue_sequence: _Optional[int] = ..., queue_snapshot_version: _Optional[int] = ..., thread_id: _Optional[str] = ...) -> None: ...

class JobProgressDTO(_message.Message):
    __slots__ = ("job_id", "status", "current_step_id", "progress", "message")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_STEP_ID_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    status: str
    current_step_id: str
    progress: int
    message: str
    def __init__(self, job_id: _Optional[str] = ..., status: _Optional[str] = ..., current_step_id: _Optional[str] = ..., progress: _Optional[int] = ..., message: _Optional[str] = ...) -> None: ...

class JobStatusChangedExecutionEventDTO(_message.Message):
    __slots__ = ("event_id", "session_id", "job_id", "time", "type", "payload")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    session_id: str
    job_id: str
    time: _timestamp_pb2.Timestamp
    type: str
    payload: JobProgressDTO
    def __init__(self, event_id: _Optional[str] = ..., session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., type: _Optional[str] = ..., payload: _Optional[_Union[JobProgressDTO, _Mapping]] = ...) -> None: ...

class JobStepProgressDTO(_message.Message):
    __slots__ = ("agent_id", "message", "phase")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    message: str
    phase: str
    def __init__(self, agent_id: _Optional[str] = ..., message: _Optional[str] = ..., phase: _Optional[str] = ...) -> None: ...

class JobStepUpdatedExecutionEventDTO(_message.Message):
    __slots__ = ("event_id", "session_id", "job_id", "time", "type", "payload")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    session_id: str
    job_id: str
    time: _timestamp_pb2.Timestamp
    type: str
    payload: JobStepProgressDTO
    def __init__(self, event_id: _Optional[str] = ..., session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., type: _Optional[str] = ..., payload: _Optional[_Union[JobStepProgressDTO, _Mapping]] = ...) -> None: ...

class JobUpdatedExecutionEventDTO(_message.Message):
    __slots__ = ("event_id", "session_id", "job_id", "time", "type", "payload")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    session_id: str
    job_id: str
    time: _timestamp_pb2.Timestamp
    type: str
    payload: JobProgressDTO
    def __init__(self, event_id: _Optional[str] = ..., session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., type: _Optional[str] = ..., payload: _Optional[_Union[JobProgressDTO, _Mapping]] = ...) -> None: ...

class LLMRequestLogRecordDTO(_message.Message):
    __slots__ = ("session_id", "job_id", "timestamp", "file_name", "request", "response", "upstream")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    FILE_NAME_FIELD_NUMBER: _ClassVar[int]
    REQUEST_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_FIELD_NUMBER: _ClassVar[int]
    UPSTREAM_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    job_id: str
    timestamp: int
    file_name: str
    request: _struct_pb2.Struct
    response: _struct_pb2.Struct
    upstream: _struct_pb2.Struct
    def __init__(self, session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., timestamp: _Optional[int] = ..., file_name: _Optional[str] = ..., request: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., response: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., upstream: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class LLMRequestLogReferenceDTO(_message.Message):
    __slots__ = ("session_id", "file_name", "content_version", "start_codepoint", "end_codepoint")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    FILE_NAME_FIELD_NUMBER: _ClassVar[int]
    CONTENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    START_CODEPOINT_FIELD_NUMBER: _ClassVar[int]
    END_CODEPOINT_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    file_name: str
    content_version: str
    start_codepoint: int
    end_codepoint: int
    def __init__(self, session_id: _Optional[str] = ..., file_name: _Optional[str] = ..., content_version: _Optional[str] = ..., start_codepoint: _Optional[int] = ..., end_codepoint: _Optional[int] = ...) -> None: ...

class LLMRequestLogMatchDTO(_message.Message):
    __slots__ = ("reference", "timestamp", "preview", "preview_start_codepoint")
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_START_CODEPOINT_FIELD_NUMBER: _ClassVar[int]
    reference: LLMRequestLogReferenceDTO
    timestamp: int
    preview: str
    preview_start_codepoint: int
    def __init__(self, reference: _Optional[_Union[LLMRequestLogReferenceDTO, _Mapping]] = ..., timestamp: _Optional[int] = ..., preview: _Optional[str] = ..., preview_start_codepoint: _Optional[int] = ...) -> None: ...

class LLMRequestLogLocationDTO(_message.Message):
    __slots__ = ("reference", "record", "text")
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    RECORD_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    reference: LLMRequestLogReferenceDTO
    record: LLMRequestLogRecordDTO
    text: str
    def __init__(self, reference: _Optional[_Union[LLMRequestLogReferenceDTO, _Mapping]] = ..., record: _Optional[_Union[LLMRequestLogRecordDTO, _Mapping]] = ..., text: _Optional[str] = ...) -> None: ...

class WorkspaceLLMRequestLogMatchDTO(_message.Message):
    __slots__ = ("session_uri", "session_title", "match")
    SESSION_URI_FIELD_NUMBER: _ClassVar[int]
    SESSION_TITLE_FIELD_NUMBER: _ClassVar[int]
    MATCH_FIELD_NUMBER: _ClassVar[int]
    session_uri: str
    session_title: str
    match: LLMRequestLogMatchDTO
    def __init__(self, session_uri: _Optional[str] = ..., session_title: _Optional[str] = ..., match: _Optional[_Union[LLMRequestLogMatchDTO, _Mapping]] = ...) -> None: ...

class WorkspaceLLMRequestLogSearchResultsDTO(_message.Message):
    __slots__ = ("catalog_revision", "items", "cursor", "complete", "limited_reason", "scanned_nodes", "scanned_records", "scanned_bytes")
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    COMPLETE_FIELD_NUMBER: _ClassVar[int]
    LIMITED_REASON_FIELD_NUMBER: _ClassVar[int]
    SCANNED_NODES_FIELD_NUMBER: _ClassVar[int]
    SCANNED_RECORDS_FIELD_NUMBER: _ClassVar[int]
    SCANNED_BYTES_FIELD_NUMBER: _ClassVar[int]
    catalog_revision: int
    items: _containers.RepeatedCompositeFieldContainer[WorkspaceLLMRequestLogMatchDTO]
    cursor: str
    complete: bool
    limited_reason: str
    scanned_nodes: int
    scanned_records: int
    scanned_bytes: int
    def __init__(self, catalog_revision: _Optional[int] = ..., items: _Optional[_Iterable[_Union[WorkspaceLLMRequestLogMatchDTO, _Mapping]]] = ..., cursor: _Optional[str] = ..., complete: _Optional[bool] = ..., limited_reason: _Optional[str] = ..., scanned_nodes: _Optional[int] = ..., scanned_records: _Optional[int] = ..., scanned_bytes: _Optional[int] = ...) -> None: ...

class TraceReferenceDTO(_message.Message):
    __slots__ = ("session_id", "thread_id", "event_id", "line_start", "line_end", "content_version", "start_codepoint", "end_codepoint")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    LINE_START_FIELD_NUMBER: _ClassVar[int]
    LINE_END_FIELD_NUMBER: _ClassVar[int]
    CONTENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    START_CODEPOINT_FIELD_NUMBER: _ClassVar[int]
    END_CODEPOINT_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    thread_id: str
    event_id: str
    line_start: int
    line_end: int
    content_version: str
    start_codepoint: int
    end_codepoint: int
    def __init__(self, session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., event_id: _Optional[str] = ..., line_start: _Optional[int] = ..., line_end: _Optional[int] = ..., content_version: _Optional[str] = ..., start_codepoint: _Optional[int] = ..., end_codepoint: _Optional[int] = ...) -> None: ...

class TraceMatchDTO(_message.Message):
    __slots__ = ("reference", "event_type", "timestamp", "preview", "preview_start_codepoint")
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    EVENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_START_CODEPOINT_FIELD_NUMBER: _ClassVar[int]
    reference: TraceReferenceDTO
    event_type: str
    timestamp: _timestamp_pb2.Timestamp
    preview: str
    preview_start_codepoint: int
    def __init__(self, reference: _Optional[_Union[TraceReferenceDTO, _Mapping]] = ..., event_type: _Optional[str] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., preview: _Optional[str] = ..., preview_start_codepoint: _Optional[int] = ...) -> None: ...

class TraceLocationDTO(_message.Message):
    __slots__ = ("reference", "record", "text")
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    RECORD_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    reference: TraceReferenceDTO
    record: TraceEventDTO
    text: str
    def __init__(self, reference: _Optional[_Union[TraceReferenceDTO, _Mapping]] = ..., record: _Optional[_Union[TraceEventDTO, _Mapping]] = ..., text: _Optional[str] = ...) -> None: ...

class WorkspaceTraceMatchDTO(_message.Message):
    __slots__ = ("session_uri", "session_title", "match")
    SESSION_URI_FIELD_NUMBER: _ClassVar[int]
    SESSION_TITLE_FIELD_NUMBER: _ClassVar[int]
    MATCH_FIELD_NUMBER: _ClassVar[int]
    session_uri: str
    session_title: str
    match: TraceMatchDTO
    def __init__(self, session_uri: _Optional[str] = ..., session_title: _Optional[str] = ..., match: _Optional[_Union[TraceMatchDTO, _Mapping]] = ...) -> None: ...

class WorkspaceTraceSearchResultsDTO(_message.Message):
    __slots__ = ("catalog_revision", "items", "cursor", "complete", "limited_reason", "scanned_nodes", "scanned_records", "scanned_bytes")
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    COMPLETE_FIELD_NUMBER: _ClassVar[int]
    LIMITED_REASON_FIELD_NUMBER: _ClassVar[int]
    SCANNED_NODES_FIELD_NUMBER: _ClassVar[int]
    SCANNED_RECORDS_FIELD_NUMBER: _ClassVar[int]
    SCANNED_BYTES_FIELD_NUMBER: _ClassVar[int]
    catalog_revision: int
    items: _containers.RepeatedCompositeFieldContainer[WorkspaceTraceMatchDTO]
    cursor: str
    complete: bool
    limited_reason: str
    scanned_nodes: int
    scanned_records: int
    scanned_bytes: int
    def __init__(self, catalog_revision: _Optional[int] = ..., items: _Optional[_Iterable[_Union[WorkspaceTraceMatchDTO, _Mapping]]] = ..., cursor: _Optional[str] = ..., complete: _Optional[bool] = ..., limited_reason: _Optional[str] = ..., scanned_nodes: _Optional[int] = ..., scanned_records: _Optional[int] = ..., scanned_bytes: _Optional[int] = ...) -> None: ...

class McpServiceReferenceDTO(_message.Message):
    __slots__ = ("gateway_id", "workspace_id", "package_id", "package_version", "plugin_id", "service_id")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    SERVICE_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    workspace_id: str
    package_id: str
    package_version: str
    plugin_id: str
    service_id: str
    def __init__(self, gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., package_id: _Optional[str] = ..., package_version: _Optional[str] = ..., plugin_id: _Optional[str] = ..., service_id: _Optional[str] = ...) -> None: ...

class McpServerDTO(_message.Message):
    __slots__ = ("server_id", "transport", "enabled", "status", "tools", "connection_ref", "unavailable_reason", "service")
    SERVER_ID_FIELD_NUMBER: _ClassVar[int]
    TRANSPORT_FIELD_NUMBER: _ClassVar[int]
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TOOLS_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_REF_FIELD_NUMBER: _ClassVar[int]
    UNAVAILABLE_REASON_FIELD_NUMBER: _ClassVar[int]
    SERVICE_FIELD_NUMBER: _ClassVar[int]
    server_id: str
    transport: str
    enabled: bool
    status: str
    tools: _containers.RepeatedCompositeFieldContainer[McpToolDTO]
    connection_ref: str
    unavailable_reason: str
    service: McpServiceReferenceDTO
    def __init__(self, server_id: _Optional[str] = ..., transport: _Optional[str] = ..., enabled: _Optional[bool] = ..., status: _Optional[str] = ..., tools: _Optional[_Iterable[_Union[McpToolDTO, _Mapping]]] = ..., connection_ref: _Optional[str] = ..., unavailable_reason: _Optional[str] = ..., service: _Optional[_Union[McpServiceReferenceDTO, _Mapping]] = ...) -> None: ...

class McpToolDTO(_message.Message):
    __slots__ = ("tool_id", "server_id", "remote_name", "description")
    TOOL_ID_FIELD_NUMBER: _ClassVar[int]
    SERVER_ID_FIELD_NUMBER: _ClassVar[int]
    REMOTE_NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    tool_id: str
    server_id: str
    remote_name: str
    description: str
    def __init__(self, tool_id: _Optional[str] = ..., server_id: _Optional[str] = ..., remote_name: _Optional[str] = ..., description: _Optional[str] = ...) -> None: ...

class McpConnectionOperationDTO(_message.Message):
    __slots__ = ("operation_id", "connection_id", "server_id", "action", "status", "stage", "created_at", "deadline", "updated_at", "error_code")
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    SERVER_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    operation_id: str
    connection_id: str
    server_id: str
    action: str
    status: str
    stage: str
    created_at: float
    deadline: float
    updated_at: float
    error_code: str
    def __init__(self, operation_id: _Optional[str] = ..., connection_id: _Optional[str] = ..., server_id: _Optional[str] = ..., action: _Optional[str] = ..., status: _Optional[str] = ..., stage: _Optional[str] = ..., created_at: _Optional[float] = ..., deadline: _Optional[float] = ..., updated_at: _Optional[float] = ..., error_code: _Optional[str] = ...) -> None: ...

class McpConnectionRequestDTO(_message.Message):
    __slots__ = ("operation_id", "server_id", "action")
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    SERVER_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    operation_id: str
    server_id: str
    action: str
    def __init__(self, operation_id: _Optional[str] = ..., server_id: _Optional[str] = ..., action: _Optional[str] = ...) -> None: ...

class McpAuthorizationDTO(_message.Message):
    __slots__ = ("authorization_url",)
    AUTHORIZATION_URL_FIELD_NUMBER: _ClassVar[int]
    authorization_url: str
    def __init__(self, authorization_url: _Optional[str] = ...) -> None: ...

class McpAuthorizationCallbackDTO(_message.Message):
    __slots__ = ("code", "state")
    CODE_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    code: str
    state: str
    def __init__(self, code: _Optional[str] = ..., state: _Optional[str] = ...) -> None: ...

class McpWorkspaceTargetDTO(_message.Message):
    __slots__ = ("kind", "gateway_id", "workspace_id")
    KIND_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    kind: str
    gateway_id: str
    workspace_id: str
    def __init__(self, kind: _Optional[str] = ..., gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ...) -> None: ...

class McpPrivateHeaderRefDTO(_message.Message):
    __slots__ = ("kind", "workspace_id", "server_id", "header_name", "endpoint", "version")
    KIND_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SERVER_ID_FIELD_NUMBER: _ClassVar[int]
    HEADER_NAME_FIELD_NUMBER: _ClassVar[int]
    ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    kind: str
    workspace_id: str
    server_id: str
    header_name: str
    endpoint: str
    version: str
    def __init__(self, kind: _Optional[str] = ..., workspace_id: _Optional[str] = ..., server_id: _Optional[str] = ..., header_name: _Optional[str] = ..., endpoint: _Optional[str] = ..., version: _Optional[str] = ...) -> None: ...

class McpPrivateHeaderWriteRequestDTO(_message.Message):
    __slots__ = ("target", "server_id", "header_name", "endpoint", "value")
    TARGET_FIELD_NUMBER: _ClassVar[int]
    SERVER_ID_FIELD_NUMBER: _ClassVar[int]
    HEADER_NAME_FIELD_NUMBER: _ClassVar[int]
    ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    target: McpWorkspaceTargetDTO
    server_id: str
    header_name: str
    endpoint: str
    value: str
    def __init__(self, target: _Optional[_Union[McpWorkspaceTargetDTO, _Mapping]] = ..., server_id: _Optional[str] = ..., header_name: _Optional[str] = ..., endpoint: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class McpPrivateHeaderWriteResponseDTO(_message.Message):
    __slots__ = ("code", "message", "data", "request_id", "target", "protocol_version")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    code: int
    message: str
    data: McpPrivateHeaderRefDTO
    request_id: str
    target: McpWorkspaceTargetDTO
    protocol_version: int
    def __init__(self, code: _Optional[int] = ..., message: _Optional[str] = ..., data: _Optional[_Union[McpPrivateHeaderRefDTO, _Mapping]] = ..., request_id: _Optional[str] = ..., target: _Optional[_Union[McpWorkspaceTargetDTO, _Mapping]] = ..., protocol_version: _Optional[int] = ...) -> None: ...

class McpServerListResponseDTO(_message.Message):
    __slots__ = ("code", "message", "data", "request_id", "target", "protocol_version", "catalog_revision", "catalog_generation")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    CATALOG_GENERATION_FIELD_NUMBER: _ClassVar[int]
    code: int
    message: str
    data: _containers.RepeatedCompositeFieldContainer[McpServerDTO]
    request_id: str
    target: McpWorkspaceTargetDTO
    protocol_version: int
    catalog_revision: str
    catalog_generation: int
    def __init__(self, code: _Optional[int] = ..., message: _Optional[str] = ..., data: _Optional[_Iterable[_Union[McpServerDTO, _Mapping]]] = ..., request_id: _Optional[str] = ..., target: _Optional[_Union[McpWorkspaceTargetDTO, _Mapping]] = ..., protocol_version: _Optional[int] = ..., catalog_revision: _Optional[str] = ..., catalog_generation: _Optional[int] = ...) -> None: ...

class McpConnectionOperationListResponseDTO(_message.Message):
    __slots__ = ("code", "message", "data", "request_id", "target", "protocol_version")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    code: int
    message: str
    data: _containers.RepeatedCompositeFieldContainer[McpConnectionOperationDTO]
    request_id: str
    target: McpWorkspaceTargetDTO
    protocol_version: int
    def __init__(self, code: _Optional[int] = ..., message: _Optional[str] = ..., data: _Optional[_Iterable[_Union[McpConnectionOperationDTO, _Mapping]]] = ..., request_id: _Optional[str] = ..., target: _Optional[_Union[McpWorkspaceTargetDTO, _Mapping]] = ..., protocol_version: _Optional[int] = ...) -> None: ...

class McpConnectionOperationResponseDTO(_message.Message):
    __slots__ = ("code", "message", "data", "request_id", "target", "protocol_version")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    code: int
    message: str
    data: McpConnectionOperationDTO
    request_id: str
    target: McpWorkspaceTargetDTO
    protocol_version: int
    def __init__(self, code: _Optional[int] = ..., message: _Optional[str] = ..., data: _Optional[_Union[McpConnectionOperationDTO, _Mapping]] = ..., request_id: _Optional[str] = ..., target: _Optional[_Union[McpWorkspaceTargetDTO, _Mapping]] = ..., protocol_version: _Optional[int] = ...) -> None: ...

class McpAuthorizationResponseDTO(_message.Message):
    __slots__ = ("code", "message", "data", "request_id", "target", "protocol_version")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_VERSION_FIELD_NUMBER: _ClassVar[int]
    code: int
    message: str
    data: McpAuthorizationDTO
    request_id: str
    target: McpWorkspaceTargetDTO
    protocol_version: int
    def __init__(self, code: _Optional[int] = ..., message: _Optional[str] = ..., data: _Optional[_Union[McpAuthorizationDTO, _Mapping]] = ..., request_id: _Optional[str] = ..., target: _Optional[_Union[McpWorkspaceTargetDTO, _Mapping]] = ..., protocol_version: _Optional[int] = ...) -> None: ...

class MessageCreateRequest(_message.Message):
    __slots__ = ("role", "content", "attachments", "metadata")
    ROLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    ATTACHMENTS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    role: str
    content: str
    attachments: _containers.RepeatedCompositeFieldContainer[_message_pb2.AttachmentRef]
    metadata: _struct_pb2.Struct
    def __init__(self, role: _Optional[str] = ..., content: _Optional[str] = ..., attachments: _Optional[_Iterable[_Union[_message_pb2.AttachmentRef, _Mapping]]] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class MessageDTO(_message.Message):
    __slots__ = ("created_at", "updated_at", "message_id", "session_id", "role", "content", "attachments", "metadata", "thread_id")
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    ATTACHMENTS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    message_id: str
    session_id: str
    role: str
    content: str
    attachments: _containers.RepeatedCompositeFieldContainer[_message_pb2.AttachmentRef]
    metadata: _struct_pb2.Struct
    thread_id: str
    def __init__(self, created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., message_id: _Optional[str] = ..., session_id: _Optional[str] = ..., role: _Optional[str] = ..., content: _Optional[str] = ..., attachments: _Optional[_Iterable[_Union[_message_pb2.AttachmentRef, _Mapping]]] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., thread_id: _Optional[str] = ...) -> None: ...

class MessageReplayAccepted(_message.Message):
    __slots__ = ("message_id", "job_id", "status", "dispatch", "session_id", "action", "replaced_message_id", "removed_message_count", "workspace_changes_reverted", "notice", "thread_id", "turn_id")
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DISPATCH_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    REPLACED_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    REMOVED_MESSAGE_COUNT_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_CHANGES_REVERTED_FIELD_NUMBER: _ClassVar[int]
    NOTICE_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    message_id: str
    job_id: str
    status: str
    dispatch: JobDispatchSnapshotDTO
    session_id: str
    action: str
    replaced_message_id: str
    removed_message_count: int
    workspace_changes_reverted: bool
    notice: str
    thread_id: str
    turn_id: str
    def __init__(self, message_id: _Optional[str] = ..., job_id: _Optional[str] = ..., status: _Optional[str] = ..., dispatch: _Optional[_Union[JobDispatchSnapshotDTO, _Mapping]] = ..., session_id: _Optional[str] = ..., action: _Optional[str] = ..., replaced_message_id: _Optional[str] = ..., removed_message_count: _Optional[int] = ..., workspace_changes_reverted: _Optional[bool] = ..., notice: _Optional[str] = ..., thread_id: _Optional[str] = ..., turn_id: _Optional[str] = ...) -> None: ...

class MessageReplayRequest(_message.Message):
    __slots__ = ("action", "content", "acknowledge_context_only")
    ACTION_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    ACKNOWLEDGE_CONTEXT_ONLY_FIELD_NUMBER: _ClassVar[int]
    action: str
    content: str
    acknowledge_context_only: bool
    def __init__(self, action: _Optional[str] = ..., content: _Optional[str] = ..., acknowledge_context_only: _Optional[bool] = ...) -> None: ...

class MessageRunAccepted(_message.Message):
    __slots__ = ("message_id", "job_id", "status", "dispatch", "thread_id", "turn_id")
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DISPATCH_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    message_id: str
    job_id: str
    status: str
    dispatch: JobDispatchSnapshotDTO
    thread_id: str
    turn_id: str
    def __init__(self, message_id: _Optional[str] = ..., job_id: _Optional[str] = ..., status: _Optional[str] = ..., dispatch: _Optional[_Union[JobDispatchSnapshotDTO, _Mapping]] = ..., thread_id: _Optional[str] = ..., turn_id: _Optional[str] = ...) -> None: ...

class MessageRunRequest(_message.Message):
    __slots__ = ("message", "run")
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RUN_FIELD_NUMBER: _ClassVar[int]
    message: MessageCreateRequest
    run: RunOptions
    def __init__(self, message: _Optional[_Union[MessageCreateRequest, _Mapping]] = ..., run: _Optional[_Union[RunOptions, _Mapping]] = ...) -> None: ...

class ExtensionCatalogBindingAuditDTO(_message.Message):
    __slots__ = ("binding_id", "binding_hash", "catalog_revision", "generation", "provider_binding_identity", "target_id", "target_schema_hash")
    BINDING_ID_FIELD_NUMBER: _ClassVar[int]
    BINDING_HASH_FIELD_NUMBER: _ClassVar[int]
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_BINDING_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    TARGET_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_SCHEMA_HASH_FIELD_NUMBER: _ClassVar[int]
    binding_id: str
    binding_hash: str
    catalog_revision: str
    generation: int
    provider_binding_identity: str
    target_id: str
    target_schema_hash: str
    def __init__(self, binding_id: _Optional[str] = ..., binding_hash: _Optional[str] = ..., catalog_revision: _Optional[str] = ..., generation: _Optional[int] = ..., provider_binding_identity: _Optional[str] = ..., target_id: _Optional[str] = ..., target_schema_hash: _Optional[str] = ...) -> None: ...

class NodeDebugActionRecordDTO(_message.Message):
    __slots__ = ("action_id", "session_id", "action", "message", "actor", "tool_name", "tool_call_id", "result", "created_at", "thread_id", "extension_catalog_binding")
    ACTION_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ACTOR_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    EXTENSION_CATALOG_BINDING_FIELD_NUMBER: _ClassVar[int]
    action_id: str
    session_id: str
    action: str
    message: str
    actor: str
    tool_name: str
    tool_call_id: str
    result: str
    created_at: _timestamp_pb2.Timestamp
    thread_id: str
    extension_catalog_binding: ExtensionCatalogBindingAuditDTO
    def __init__(self, action_id: _Optional[str] = ..., session_id: _Optional[str] = ..., action: _Optional[str] = ..., message: _Optional[str] = ..., actor: _Optional[str] = ..., tool_name: _Optional[str] = ..., tool_call_id: _Optional[str] = ..., result: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., thread_id: _Optional[str] = ..., extension_catalog_binding: _Optional[_Union[ExtensionCatalogBindingAuditDTO, _Mapping]] = ...) -> None: ...

class NodeDebugNoActionParams(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class NodeDebugSetBreakpointParams(_message.Message):
    __slots__ = ("path", "line", "column", "condition", "hit_condition", "log_message")
    PATH_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    COLUMN_FIELD_NUMBER: _ClassVar[int]
    CONDITION_FIELD_NUMBER: _ClassVar[int]
    HIT_CONDITION_FIELD_NUMBER: _ClassVar[int]
    LOG_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    path: str
    line: int
    column: int
    condition: str
    hit_condition: int
    log_message: str
    def __init__(self, path: _Optional[str] = ..., line: _Optional[int] = ..., column: _Optional[int] = ..., condition: _Optional[str] = ..., hit_condition: _Optional[int] = ..., log_message: _Optional[str] = ...) -> None: ...

class NodeDebugUpdateBreakpointParams(_message.Message):
    __slots__ = ("breakpoint_id", "path", "line", "column", "condition", "hit_condition", "log_message")
    BREAKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    COLUMN_FIELD_NUMBER: _ClassVar[int]
    CONDITION_FIELD_NUMBER: _ClassVar[int]
    HIT_CONDITION_FIELD_NUMBER: _ClassVar[int]
    LOG_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    breakpoint_id: str
    path: str
    line: int
    column: int
    condition: str
    hit_condition: int
    log_message: str
    def __init__(self, breakpoint_id: _Optional[str] = ..., path: _Optional[str] = ..., line: _Optional[int] = ..., column: _Optional[int] = ..., condition: _Optional[str] = ..., hit_condition: _Optional[int] = ..., log_message: _Optional[str] = ...) -> None: ...

class NodeDebugClearBreakpointParams(_message.Message):
    __slots__ = ("breakpoint_id",)
    BREAKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    breakpoint_id: str
    def __init__(self, breakpoint_id: _Optional[str] = ...) -> None: ...

class NodeDebugEvaluateParams(_message.Message):
    __slots__ = ("expression", "call_frame_id")
    EXPRESSION_FIELD_NUMBER: _ClassVar[int]
    CALL_FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    expression: str
    call_frame_id: str
    def __init__(self, expression: _Optional[str] = ..., call_frame_id: _Optional[str] = ...) -> None: ...

class NodeDebugActionRequest(_message.Message):
    __slots__ = ("session_id", "pause", "step_over", "step_into", "step_out", "set_breakpoint", "update_breakpoint", "clear_breakpoint", "evaluate", "stop", "thread_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CONTINUE_FIELD_NUMBER: _ClassVar[int]
    PAUSE_FIELD_NUMBER: _ClassVar[int]
    STEP_OVER_FIELD_NUMBER: _ClassVar[int]
    STEP_INTO_FIELD_NUMBER: _ClassVar[int]
    STEP_OUT_FIELD_NUMBER: _ClassVar[int]
    SET_BREAKPOINT_FIELD_NUMBER: _ClassVar[int]
    UPDATE_BREAKPOINT_FIELD_NUMBER: _ClassVar[int]
    CLEAR_BREAKPOINT_FIELD_NUMBER: _ClassVar[int]
    EVALUATE_FIELD_NUMBER: _ClassVar[int]
    STOP_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    pause: NodeDebugNoActionParams
    step_over: NodeDebugNoActionParams
    step_into: NodeDebugNoActionParams
    step_out: NodeDebugNoActionParams
    set_breakpoint: NodeDebugSetBreakpointParams
    update_breakpoint: NodeDebugUpdateBreakpointParams
    clear_breakpoint: NodeDebugClearBreakpointParams
    evaluate: NodeDebugEvaluateParams
    stop: NodeDebugNoActionParams
    thread_id: str
    def __init__(self, session_id: _Optional[str] = ..., pause: _Optional[_Union[NodeDebugNoActionParams, _Mapping]] = ..., step_over: _Optional[_Union[NodeDebugNoActionParams, _Mapping]] = ..., step_into: _Optional[_Union[NodeDebugNoActionParams, _Mapping]] = ..., step_out: _Optional[_Union[NodeDebugNoActionParams, _Mapping]] = ..., set_breakpoint: _Optional[_Union[NodeDebugSetBreakpointParams, _Mapping]] = ..., update_breakpoint: _Optional[_Union[NodeDebugUpdateBreakpointParams, _Mapping]] = ..., clear_breakpoint: _Optional[_Union[NodeDebugClearBreakpointParams, _Mapping]] = ..., evaluate: _Optional[_Union[NodeDebugEvaluateParams, _Mapping]] = ..., stop: _Optional[_Union[NodeDebugNoActionParams, _Mapping]] = ..., thread_id: _Optional[str] = ..., **kwargs) -> None: ...

class NodeDebugBreakpointDTO(_message.Message):
    __slots__ = ("breakpoint_id", "path", "line", "column", "condition", "hit_condition", "log_message", "verified", "actual_line", "original_line", "source_line", "previous_line", "next_line", "source_digest", "relocation_status", "relocation_message", "created_at")
    BREAKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    COLUMN_FIELD_NUMBER: _ClassVar[int]
    CONDITION_FIELD_NUMBER: _ClassVar[int]
    HIT_CONDITION_FIELD_NUMBER: _ClassVar[int]
    LOG_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    VERIFIED_FIELD_NUMBER: _ClassVar[int]
    ACTUAL_LINE_FIELD_NUMBER: _ClassVar[int]
    ORIGINAL_LINE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_LINE_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_LINE_FIELD_NUMBER: _ClassVar[int]
    NEXT_LINE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    RELOCATION_STATUS_FIELD_NUMBER: _ClassVar[int]
    RELOCATION_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    breakpoint_id: str
    path: str
    line: int
    column: int
    condition: str
    hit_condition: int
    log_message: str
    verified: bool
    actual_line: int
    original_line: int
    source_line: str
    previous_line: str
    next_line: str
    source_digest: str
    relocation_status: str
    relocation_message: str
    created_at: _timestamp_pb2.Timestamp
    def __init__(self, breakpoint_id: _Optional[str] = ..., path: _Optional[str] = ..., line: _Optional[int] = ..., column: _Optional[int] = ..., condition: _Optional[str] = ..., hit_condition: _Optional[int] = ..., log_message: _Optional[str] = ..., verified: _Optional[bool] = ..., actual_line: _Optional[int] = ..., original_line: _Optional[int] = ..., source_line: _Optional[str] = ..., previous_line: _Optional[str] = ..., next_line: _Optional[str] = ..., source_digest: _Optional[str] = ..., relocation_status: _Optional[str] = ..., relocation_message: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class NodeDebugBreakpointRequest(_message.Message):
    __slots__ = ("path", "line", "column", "condition", "hit_condition", "log_message")
    PATH_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    COLUMN_FIELD_NUMBER: _ClassVar[int]
    CONDITION_FIELD_NUMBER: _ClassVar[int]
    HIT_CONDITION_FIELD_NUMBER: _ClassVar[int]
    LOG_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    path: str
    line: int
    column: int
    condition: str
    hit_condition: int
    log_message: str
    def __init__(self, path: _Optional[str] = ..., line: _Optional[int] = ..., column: _Optional[int] = ..., condition: _Optional[str] = ..., hit_condition: _Optional[int] = ..., log_message: _Optional[str] = ...) -> None: ...

class NodeDebugCapabilitiesDTO(_message.Message):
    __slots__ = ("enabled", "default_adapter", "supported_adapters", "launch_profiles")
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_ADAPTER_FIELD_NUMBER: _ClassVar[int]
    SUPPORTED_ADAPTERS_FIELD_NUMBER: _ClassVar[int]
    LAUNCH_PROFILES_FIELD_NUMBER: _ClassVar[int]
    enabled: bool
    default_adapter: str
    supported_adapters: _containers.RepeatedScalarFieldContainer[str]
    launch_profiles: _containers.RepeatedCompositeFieldContainer[NodeDebugLaunchProfileDTO]
    def __init__(self, enabled: _Optional[bool] = ..., default_adapter: _Optional[str] = ..., supported_adapters: _Optional[_Iterable[str]] = ..., launch_profiles: _Optional[_Iterable[_Union[NodeDebugLaunchProfileDTO, _Mapping]]] = ...) -> None: ...

class NodeDebugDebuggerContributionDTO(_message.Message):
    __slots__ = ("registration_id", "type", "label", "configuration_schema", "service_id", "dap_version", "capabilities", "gateway_id", "workspace_id", "package_id", "plugin_id", "package_version", "package_digest", "instance_id", "contract_version", "declaration_revision")
    REGISTRATION_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_SCHEMA_FIELD_NUMBER: _ClassVar[int]
    SERVICE_ID_FIELD_NUMBER: _ClassVar[int]
    DAP_VERSION_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    DECLARATION_REVISION_FIELD_NUMBER: _ClassVar[int]
    registration_id: str
    type: str
    label: str
    configuration_schema: _struct_pb2.Struct
    service_id: str
    dap_version: int
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    gateway_id: str
    workspace_id: str
    package_id: str
    plugin_id: str
    package_version: str
    package_digest: str
    instance_id: str
    contract_version: int
    declaration_revision: str
    def __init__(self, registration_id: _Optional[str] = ..., type: _Optional[str] = ..., label: _Optional[str] = ..., configuration_schema: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., service_id: _Optional[str] = ..., dap_version: _Optional[int] = ..., capabilities: _Optional[_Iterable[str]] = ..., gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., package_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., package_version: _Optional[str] = ..., package_digest: _Optional[str] = ..., instance_id: _Optional[str] = ..., contract_version: _Optional[int] = ..., declaration_revision: _Optional[str] = ...) -> None: ...

class NodeDebugDebuggerContributionsDTO(_message.Message):
    __slots__ = ("gateway_id", "workspace_id", "debuggers")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    DEBUGGERS_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    workspace_id: str
    debuggers: _containers.RepeatedCompositeFieldContainer[NodeDebugDebuggerContributionDTO]
    def __init__(self, gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., debuggers: _Optional[_Iterable[_Union[NodeDebugDebuggerContributionDTO, _Mapping]]] = ...) -> None: ...

class NodeDebugBindingSnapshotDTO(_message.Message):
    __slots__ = ("registration_id", "type", "gateway_id", "workspace_id", "package_id", "plugin_id", "package_version", "package_digest", "instance_id", "service_id", "contract_version", "declaration_revision", "dap_version")
    REGISTRATION_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_ID_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_VERSION_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
    SERVICE_ID_FIELD_NUMBER: _ClassVar[int]
    CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    DECLARATION_REVISION_FIELD_NUMBER: _ClassVar[int]
    DAP_VERSION_FIELD_NUMBER: _ClassVar[int]
    registration_id: str
    type: str
    gateway_id: str
    workspace_id: str
    package_id: str
    plugin_id: str
    package_version: str
    package_digest: str
    instance_id: str
    service_id: str
    contract_version: int
    declaration_revision: str
    dap_version: int
    def __init__(self, registration_id: _Optional[str] = ..., type: _Optional[str] = ..., gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., package_id: _Optional[str] = ..., plugin_id: _Optional[str] = ..., package_version: _Optional[str] = ..., package_digest: _Optional[str] = ..., instance_id: _Optional[str] = ..., service_id: _Optional[str] = ..., contract_version: _Optional[int] = ..., declaration_revision: _Optional[str] = ..., dap_version: _Optional[int] = ...) -> None: ...

class NodeDebugConfigurationActivateRequest(_message.Message):
    __slots__ = ("session_id", "thread_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    thread_id: str
    def __init__(self, session_id: _Optional[str] = ..., thread_id: _Optional[str] = ...) -> None: ...

class NodeDebugConfigurationBreakpointDTO(_message.Message):
    __slots__ = ("breakpoint_id", "path", "line", "column", "condition", "hit_condition", "log_message", "original_line", "source_line", "previous_line", "next_line", "source_digest", "relocation_status", "relocation_message", "created_at")
    BREAKPOINT_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    COLUMN_FIELD_NUMBER: _ClassVar[int]
    CONDITION_FIELD_NUMBER: _ClassVar[int]
    HIT_CONDITION_FIELD_NUMBER: _ClassVar[int]
    LOG_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ORIGINAL_LINE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_LINE_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_LINE_FIELD_NUMBER: _ClassVar[int]
    NEXT_LINE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_DIGEST_FIELD_NUMBER: _ClassVar[int]
    RELOCATION_STATUS_FIELD_NUMBER: _ClassVar[int]
    RELOCATION_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    breakpoint_id: str
    path: str
    line: int
    column: int
    condition: str
    hit_condition: int
    log_message: str
    original_line: int
    source_line: str
    previous_line: str
    next_line: str
    source_digest: str
    relocation_status: str
    relocation_message: str
    created_at: _timestamp_pb2.Timestamp
    def __init__(self, breakpoint_id: _Optional[str] = ..., path: _Optional[str] = ..., line: _Optional[int] = ..., column: _Optional[int] = ..., condition: _Optional[str] = ..., hit_condition: _Optional[int] = ..., log_message: _Optional[str] = ..., original_line: _Optional[int] = ..., source_line: _Optional[str] = ..., previous_line: _Optional[str] = ..., next_line: _Optional[str] = ..., source_digest: _Optional[str] = ..., relocation_status: _Optional[str] = ..., relocation_message: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class NodeDebugConfigurationCopyRequest(_message.Message):
    __slots__ = ("source_session_id", "target_session_id", "name", "activate", "source_thread_id", "target_thread_id")
    SOURCE_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    ACTIVATE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    source_session_id: str
    target_session_id: str
    name: str
    activate: bool
    source_thread_id: str
    target_thread_id: str
    def __init__(self, source_session_id: _Optional[str] = ..., target_session_id: _Optional[str] = ..., name: _Optional[str] = ..., activate: _Optional[bool] = ..., source_thread_id: _Optional[str] = ..., target_thread_id: _Optional[str] = ...) -> None: ...

class NodeDebugConfigurationCreateRequest(_message.Message):
    __slots__ = ("session_id", "name", "script_path", "working_directory", "launch_profile_name", "args", "breakpoints", "activate", "thread_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    SCRIPT_PATH_FIELD_NUMBER: _ClassVar[int]
    WORKING_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    LAUNCH_PROFILE_NAME_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    BREAKPOINTS_FIELD_NUMBER: _ClassVar[int]
    ACTIVATE_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    name: str
    script_path: str
    working_directory: str
    launch_profile_name: str
    args: _containers.RepeatedScalarFieldContainer[str]
    breakpoints: _containers.RepeatedCompositeFieldContainer[NodeDebugBreakpointRequest]
    activate: bool
    thread_id: str
    def __init__(self, session_id: _Optional[str] = ..., name: _Optional[str] = ..., script_path: _Optional[str] = ..., working_directory: _Optional[str] = ..., launch_profile_name: _Optional[str] = ..., args: _Optional[_Iterable[str]] = ..., breakpoints: _Optional[_Iterable[_Union[NodeDebugBreakpointRequest, _Mapping]]] = ..., activate: _Optional[bool] = ..., thread_id: _Optional[str] = ...) -> None: ...

class NodeDebugConfigurationDTO(_message.Message):
    __slots__ = ("schema_version", "configuration_id", "name", "revision", "script_path", "working_directory", "launch_profile_name", "args", "breakpoints", "created_at", "updated_at")
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    SCRIPT_PATH_FIELD_NUMBER: _ClassVar[int]
    WORKING_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    LAUNCH_PROFILE_NAME_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    BREAKPOINTS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    schema_version: int
    configuration_id: str
    name: str
    revision: int
    script_path: str
    working_directory: str
    launch_profile_name: str
    args: _containers.RepeatedScalarFieldContainer[str]
    breakpoints: _containers.RepeatedCompositeFieldContainer[NodeDebugConfigurationBreakpointDTO]
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, schema_version: _Optional[int] = ..., configuration_id: _Optional[str] = ..., name: _Optional[str] = ..., revision: _Optional[int] = ..., script_path: _Optional[str] = ..., working_directory: _Optional[str] = ..., launch_profile_name: _Optional[str] = ..., args: _Optional[_Iterable[str]] = ..., breakpoints: _Optional[_Iterable[_Union[NodeDebugConfigurationBreakpointDTO, _Mapping]]] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class NodeDebugConfigurationImportRequest(_message.Message):
    __slots__ = ("session_id", "configuration", "activate", "thread_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_FIELD_NUMBER: _ClassVar[int]
    ACTIVATE_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    configuration: NodeDebugConfigurationDTO
    activate: bool
    thread_id: str
    def __init__(self, session_id: _Optional[str] = ..., configuration: _Optional[_Union[NodeDebugConfigurationDTO, _Mapping]] = ..., activate: _Optional[bool] = ..., thread_id: _Optional[str] = ...) -> None: ...

class NodeDebugConfigurationSummaryDTO(_message.Message):
    __slots__ = ("configuration_id", "name", "script_path", "launch_profile_name", "breakpoint_count", "revision", "updated_at")
    CONFIGURATION_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    SCRIPT_PATH_FIELD_NUMBER: _ClassVar[int]
    LAUNCH_PROFILE_NAME_FIELD_NUMBER: _ClassVar[int]
    BREAKPOINT_COUNT_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    configuration_id: str
    name: str
    script_path: str
    launch_profile_name: str
    breakpoint_count: int
    revision: int
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, configuration_id: _Optional[str] = ..., name: _Optional[str] = ..., script_path: _Optional[str] = ..., launch_profile_name: _Optional[str] = ..., breakpoint_count: _Optional[int] = ..., revision: _Optional[int] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class NodeDebugConfigurationUpdateRequest(_message.Message):
    __slots__ = ("session_id", "name", "script_path", "working_directory", "launch_profile_name", "args", "breakpoints", "thread_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    SCRIPT_PATH_FIELD_NUMBER: _ClassVar[int]
    WORKING_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    LAUNCH_PROFILE_NAME_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    BREAKPOINTS_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    name: str
    script_path: str
    working_directory: str
    launch_profile_name: str
    args: _containers.RepeatedScalarFieldContainer[str]
    breakpoints: _containers.RepeatedCompositeFieldContainer[NodeDebugBreakpointRequest]
    thread_id: str
    def __init__(self, session_id: _Optional[str] = ..., name: _Optional[str] = ..., script_path: _Optional[str] = ..., working_directory: _Optional[str] = ..., launch_profile_name: _Optional[str] = ..., args: _Optional[_Iterable[str]] = ..., breakpoints: _Optional[_Iterable[_Union[NodeDebugBreakpointRequest, _Mapping]]] = ..., thread_id: _Optional[str] = ...) -> None: ...

class NodeDebugEvaluationDTO(_message.Message):
    __slots__ = ("expression", "value", "type", "description", "error", "evaluated_at")
    EXPRESSION_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    EVALUATED_AT_FIELD_NUMBER: _ClassVar[int]
    expression: str
    value: str
    type: str
    description: str
    error: str
    evaluated_at: _timestamp_pb2.Timestamp
    def __init__(self, expression: _Optional[str] = ..., value: _Optional[str] = ..., type: _Optional[str] = ..., description: _Optional[str] = ..., error: _Optional[str] = ..., evaluated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class NodeDebugLaunchProfileDTO(_message.Message):
    __slots__ = ("name", "adapter", "runtime", "supported", "program", "working_directory", "args")
    NAME_FIELD_NUMBER: _ClassVar[int]
    ADAPTER_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_FIELD_NUMBER: _ClassVar[int]
    SUPPORTED_FIELD_NUMBER: _ClassVar[int]
    PROGRAM_FIELD_NUMBER: _ClassVar[int]
    WORKING_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    name: str
    adapter: str
    runtime: str
    supported: bool
    program: str
    working_directory: str
    args: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, name: _Optional[str] = ..., adapter: _Optional[str] = ..., runtime: _Optional[str] = ..., supported: _Optional[bool] = ..., program: _Optional[str] = ..., working_directory: _Optional[str] = ..., args: _Optional[_Iterable[str]] = ...) -> None: ...

class NodeDebugSessionManifestDTO(_message.Message):
    __slots__ = ("schema_version", "session_id", "active_configuration_id", "actions", "updated_at", "thread_id", "configuration_ids")
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_CONFIGURATION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIONS_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_IDS_FIELD_NUMBER: _ClassVar[int]
    schema_version: int
    session_id: str
    active_configuration_id: str
    actions: _containers.RepeatedCompositeFieldContainer[NodeDebugActionRecordDTO]
    updated_at: _timestamp_pb2.Timestamp
    thread_id: str
    configuration_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, schema_version: _Optional[int] = ..., session_id: _Optional[str] = ..., active_configuration_id: _Optional[str] = ..., actions: _Optional[_Iterable[_Union[NodeDebugActionRecordDTO, _Mapping]]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., thread_id: _Optional[str] = ..., configuration_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class NodeDebugStackFrameDTO(_message.Message):
    __slots__ = ("call_frame_id", "function_name", "url", "path", "line", "column", "scope_names", "variables")
    CALL_FRAME_ID_FIELD_NUMBER: _ClassVar[int]
    FUNCTION_NAME_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    COLUMN_FIELD_NUMBER: _ClassVar[int]
    SCOPE_NAMES_FIELD_NUMBER: _ClassVar[int]
    VARIABLES_FIELD_NUMBER: _ClassVar[int]
    call_frame_id: str
    function_name: str
    url: str
    path: str
    line: int
    column: int
    scope_names: _containers.RepeatedScalarFieldContainer[str]
    variables: _containers.RepeatedCompositeFieldContainer[NodeDebugVariableDTO]
    def __init__(self, call_frame_id: _Optional[str] = ..., function_name: _Optional[str] = ..., url: _Optional[str] = ..., path: _Optional[str] = ..., line: _Optional[int] = ..., column: _Optional[int] = ..., scope_names: _Optional[_Iterable[str]] = ..., variables: _Optional[_Iterable[_Union[NodeDebugVariableDTO, _Mapping]]] = ...) -> None: ...

class NodeDebugStartRequest(_message.Message):
    __slots__ = ("session_id", "configuration_id", "path", "working_directory", "launch_profile_name", "args", "breakpoints", "thread_id", "debugger_registration_id", "defer_configuration_done")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_ID_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    WORKING_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    LAUNCH_PROFILE_NAME_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    BREAKPOINTS_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    DEBUGGER_REGISTRATION_ID_FIELD_NUMBER: _ClassVar[int]
    DEFER_CONFIGURATION_DONE_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    configuration_id: str
    path: str
    working_directory: str
    launch_profile_name: str
    args: _containers.RepeatedScalarFieldContainer[str]
    breakpoints: _containers.RepeatedCompositeFieldContainer[NodeDebugBreakpointRequest]
    thread_id: str
    debugger_registration_id: str
    defer_configuration_done: bool
    def __init__(self, session_id: _Optional[str] = ..., configuration_id: _Optional[str] = ..., path: _Optional[str] = ..., working_directory: _Optional[str] = ..., launch_profile_name: _Optional[str] = ..., args: _Optional[_Iterable[str]] = ..., breakpoints: _Optional[_Iterable[_Union[NodeDebugBreakpointRequest, _Mapping]]] = ..., thread_id: _Optional[str] = ..., debugger_registration_id: _Optional[str] = ..., defer_configuration_done: _Optional[bool] = ...) -> None: ...

class NodeDebugStateDTO(_message.Message):
    __slots__ = ("session_id", "status", "active_configuration_id", "active_configuration_name", "configurations", "script_path", "working_directory", "launch_profile_name", "args", "pid", "paused_reason", "error_message", "call_stack", "last_stopped_frame", "breakpoints", "output", "last_evaluation", "evaluations", "actions", "configuration_revision", "requires_restart", "source_changed_paths", "thread_id", "run_id", "debugger_binding")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_CONFIGURATION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_CONFIGURATION_NAME_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATIONS_FIELD_NUMBER: _ClassVar[int]
    SCRIPT_PATH_FIELD_NUMBER: _ClassVar[int]
    WORKING_DIRECTORY_FIELD_NUMBER: _ClassVar[int]
    LAUNCH_PROFILE_NAME_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    PID_FIELD_NUMBER: _ClassVar[int]
    PAUSED_REASON_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    CALL_STACK_FIELD_NUMBER: _ClassVar[int]
    LAST_STOPPED_FRAME_FIELD_NUMBER: _ClassVar[int]
    BREAKPOINTS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    LAST_EVALUATION_FIELD_NUMBER: _ClassVar[int]
    EVALUATIONS_FIELD_NUMBER: _ClassVar[int]
    ACTIONS_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_REVISION_FIELD_NUMBER: _ClassVar[int]
    REQUIRES_RESTART_FIELD_NUMBER: _ClassVar[int]
    SOURCE_CHANGED_PATHS_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    DEBUGGER_BINDING_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    status: str
    active_configuration_id: str
    active_configuration_name: str
    configurations: _containers.RepeatedCompositeFieldContainer[NodeDebugConfigurationSummaryDTO]
    script_path: str
    working_directory: str
    launch_profile_name: str
    args: _containers.RepeatedScalarFieldContainer[str]
    pid: int
    paused_reason: str
    error_message: str
    call_stack: _containers.RepeatedCompositeFieldContainer[NodeDebugStackFrameDTO]
    last_stopped_frame: NodeDebugStackFrameDTO
    breakpoints: _containers.RepeatedCompositeFieldContainer[NodeDebugBreakpointDTO]
    output: _containers.RepeatedScalarFieldContainer[str]
    last_evaluation: NodeDebugEvaluationDTO
    evaluations: _containers.RepeatedCompositeFieldContainer[NodeDebugEvaluationDTO]
    actions: _containers.RepeatedCompositeFieldContainer[NodeDebugActionRecordDTO]
    configuration_revision: int
    requires_restart: bool
    source_changed_paths: _containers.RepeatedScalarFieldContainer[str]
    thread_id: str
    run_id: str
    debugger_binding: NodeDebugBindingSnapshotDTO
    def __init__(self, session_id: _Optional[str] = ..., status: _Optional[str] = ..., active_configuration_id: _Optional[str] = ..., active_configuration_name: _Optional[str] = ..., configurations: _Optional[_Iterable[_Union[NodeDebugConfigurationSummaryDTO, _Mapping]]] = ..., script_path: _Optional[str] = ..., working_directory: _Optional[str] = ..., launch_profile_name: _Optional[str] = ..., args: _Optional[_Iterable[str]] = ..., pid: _Optional[int] = ..., paused_reason: _Optional[str] = ..., error_message: _Optional[str] = ..., call_stack: _Optional[_Iterable[_Union[NodeDebugStackFrameDTO, _Mapping]]] = ..., last_stopped_frame: _Optional[_Union[NodeDebugStackFrameDTO, _Mapping]] = ..., breakpoints: _Optional[_Iterable[_Union[NodeDebugBreakpointDTO, _Mapping]]] = ..., output: _Optional[_Iterable[str]] = ..., last_evaluation: _Optional[_Union[NodeDebugEvaluationDTO, _Mapping]] = ..., evaluations: _Optional[_Iterable[_Union[NodeDebugEvaluationDTO, _Mapping]]] = ..., actions: _Optional[_Iterable[_Union[NodeDebugActionRecordDTO, _Mapping]]] = ..., configuration_revision: _Optional[int] = ..., requires_restart: _Optional[bool] = ..., source_changed_paths: _Optional[_Iterable[str]] = ..., thread_id: _Optional[str] = ..., run_id: _Optional[str] = ..., debugger_binding: _Optional[_Union[NodeDebugBindingSnapshotDTO, _Mapping]] = ...) -> None: ...

class NodeDebugVariableDTO(_message.Message):
    __slots__ = ("name", "value", "type", "object_id", "scope")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    OBJECT_ID_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: str
    type: str
    object_id: str
    scope: str
    def __init__(self, name: _Optional[str] = ..., value: _Optional[str] = ..., type: _Optional[str] = ..., object_id: _Optional[str] = ..., scope: _Optional[str] = ...) -> None: ...

class PendingRequestDTO(_message.Message):
    __slots__ = ("job_id", "message_id", "session_id", "content", "attachments", "delivery_policy", "enqueue_sequence", "position", "status", "waiting_reason", "last_boundary", "agent_id", "message_created_at", "message_metadata", "created_at", "updated_at", "snapshot_version", "gateway_id", "thread_id", "turn_id", "execution_binding")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    ATTACHMENTS_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_POLICY_FIELD_NUMBER: _ClassVar[int]
    ENQUEUE_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    POSITION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    WAITING_REASON_FIELD_NUMBER: _ClassVar[int]
    LAST_BOUNDARY_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_METADATA_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_VERSION_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_BINDING_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    message_id: str
    session_id: str
    content: str
    attachments: _containers.RepeatedCompositeFieldContainer[_message_pb2.AttachmentRef]
    delivery_policy: str
    enqueue_sequence: int
    position: int
    status: str
    waiting_reason: str
    last_boundary: str
    agent_id: str
    message_created_at: str
    message_metadata: _struct_pb2.Struct
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    snapshot_version: int
    gateway_id: str
    thread_id: str
    turn_id: str
    execution_binding: _struct_pb2.Struct
    def __init__(self, job_id: _Optional[str] = ..., message_id: _Optional[str] = ..., session_id: _Optional[str] = ..., content: _Optional[str] = ..., attachments: _Optional[_Iterable[_Union[_message_pb2.AttachmentRef, _Mapping]]] = ..., delivery_policy: _Optional[str] = ..., enqueue_sequence: _Optional[int] = ..., position: _Optional[int] = ..., status: _Optional[str] = ..., waiting_reason: _Optional[str] = ..., last_boundary: _Optional[str] = ..., agent_id: _Optional[str] = ..., message_created_at: _Optional[str] = ..., message_metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., snapshot_version: _Optional[int] = ..., gateway_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., turn_id: _Optional[str] = ..., execution_binding: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class PendingRequestListDTO(_message.Message):
    __slots__ = ("session_id", "active_job_id", "requests", "snapshot_version", "thread_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    REQUESTS_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_VERSION_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    active_job_id: str
    requests: _containers.RepeatedCompositeFieldContainer[PendingRequestDTO]
    snapshot_version: int
    thread_id: str
    def __init__(self, session_id: _Optional[str] = ..., active_job_id: _Optional[str] = ..., requests: _Optional[_Iterable[_Union[PendingRequestDTO, _Mapping]]] = ..., snapshot_version: _Optional[int] = ..., thread_id: _Optional[str] = ...) -> None: ...

class PendingRequestPolicyUpdateRequest(_message.Message):
    __slots__ = ("delivery_policy", "expected_snapshot_version")
    DELIVERY_POLICY_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_SNAPSHOT_VERSION_FIELD_NUMBER: _ClassVar[int]
    delivery_policy: str
    expected_snapshot_version: int
    def __init__(self, delivery_policy: _Optional[str] = ..., expected_snapshot_version: _Optional[int] = ...) -> None: ...

class PendingRequestSummaryDTO(_message.Message):
    __slots__ = ("job_id", "message_id", "enqueue_sequence", "delivery_policy", "status", "updated_at", "thread_id")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    ENQUEUE_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_POLICY_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    message_id: str
    enqueue_sequence: int
    delivery_policy: str
    status: str
    updated_at: _timestamp_pb2.Timestamp
    thread_id: str
    def __init__(self, job_id: _Optional[str] = ..., message_id: _Optional[str] = ..., enqueue_sequence: _Optional[int] = ..., delivery_policy: _Optional[str] = ..., status: _Optional[str] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., thread_id: _Optional[str] = ...) -> None: ...

class PendingRequestSummaryListDTO(_message.Message):
    __slots__ = ("session_id", "active_job_id", "requests", "request_count", "snapshot_version", "truncated", "thread_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    REQUESTS_FIELD_NUMBER: _ClassVar[int]
    REQUEST_COUNT_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_VERSION_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    active_job_id: str
    requests: _containers.RepeatedCompositeFieldContainer[PendingRequestSummaryDTO]
    request_count: int
    snapshot_version: int
    truncated: bool
    thread_id: str
    def __init__(self, session_id: _Optional[str] = ..., active_job_id: _Optional[str] = ..., requests: _Optional[_Iterable[_Union[PendingRequestSummaryDTO, _Mapping]]] = ..., request_count: _Optional[int] = ..., snapshot_version: _Optional[int] = ..., truncated: _Optional[bool] = ..., thread_id: _Optional[str] = ...) -> None: ...

class PermissionRequestDTO(_message.Message):
    __slots__ = ("id", "session_id", "permission", "patterns", "metadata", "always", "tool")
    ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PERMISSION_FIELD_NUMBER: _ClassVar[int]
    PATTERNS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    ALWAYS_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    id: str
    session_id: str
    permission: str
    patterns: _containers.RepeatedScalarFieldContainer[str]
    metadata: _struct_pb2.Struct
    always: _containers.RepeatedScalarFieldContainer[str]
    tool: _struct_pb2.Struct
    def __init__(self, id: _Optional[str] = ..., session_id: _Optional[str] = ..., permission: _Optional[str] = ..., patterns: _Optional[_Iterable[str]] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., always: _Optional[_Iterable[str]] = ..., tool: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class QuestionInfoDTO(_message.Message):
    __slots__ = ("question", "header", "options", "multiple", "question_key", "header_key", "custom")
    QUESTION_FIELD_NUMBER: _ClassVar[int]
    HEADER_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    MULTIPLE_FIELD_NUMBER: _ClassVar[int]
    QUESTION_KEY_FIELD_NUMBER: _ClassVar[int]
    HEADER_KEY_FIELD_NUMBER: _ClassVar[int]
    CUSTOM_FIELD_NUMBER: _ClassVar[int]
    question: str
    header: str
    options: _containers.RepeatedCompositeFieldContainer[QuestionOptionDTO]
    multiple: bool
    question_key: str
    header_key: str
    custom: bool
    def __init__(self, question: _Optional[str] = ..., header: _Optional[str] = ..., options: _Optional[_Iterable[_Union[QuestionOptionDTO, _Mapping]]] = ..., multiple: _Optional[bool] = ..., question_key: _Optional[str] = ..., header_key: _Optional[str] = ..., custom: _Optional[bool] = ...) -> None: ...

class QuestionOptionDTO(_message.Message):
    __slots__ = ("label", "description", "label_key", "description_key", "mode")
    LABEL_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    LABEL_KEY_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_KEY_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    label: str
    description: str
    label_key: str
    description_key: str
    mode: str
    def __init__(self, label: _Optional[str] = ..., description: _Optional[str] = ..., label_key: _Optional[str] = ..., description_key: _Optional[str] = ..., mode: _Optional[str] = ...) -> None: ...

class QuestionRequestDTO(_message.Message):
    __slots__ = ("id", "session_id", "questions", "blocking", "tool")
    ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    QUESTIONS_FIELD_NUMBER: _ClassVar[int]
    BLOCKING_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    id: str
    session_id: str
    questions: _containers.RepeatedCompositeFieldContainer[QuestionInfoDTO]
    blocking: bool
    tool: _struct_pb2.Struct
    def __init__(self, id: _Optional[str] = ..., session_id: _Optional[str] = ..., questions: _Optional[_Iterable[_Union[QuestionInfoDTO, _Mapping]]] = ..., blocking: _Optional[bool] = ..., tool: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class RunOptions(_message.Message):
    __slots__ = ("mode", "agent_id", "response_mode", "max_steps", "timeout_seconds", "context", "delivery_policy")
    MODE_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_MODE_FIELD_NUMBER: _ClassVar[int]
    ASYNC_FIELD_NUMBER: _ClassVar[int]
    MAX_STEPS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_POLICY_FIELD_NUMBER: _ClassVar[int]
    mode: str
    agent_id: str
    response_mode: str
    max_steps: int
    timeout_seconds: int
    context: _struct_pb2.Struct
    delivery_policy: str
    def __init__(self, mode: _Optional[str] = ..., agent_id: _Optional[str] = ..., response_mode: _Optional[str] = ..., max_steps: _Optional[int] = ..., timeout_seconds: _Optional[int] = ..., context: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., delivery_policy: _Optional[str] = ..., **kwargs) -> None: ...

class RuntimeDrainBlockerDTO(_message.Message):
    __slots__ = ("kind", "resource_id", "session_id", "status", "detail")
    KIND_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    kind: str
    resource_id: str
    session_id: str
    status: str
    detail: str
    def __init__(self, kind: _Optional[str] = ..., resource_id: _Optional[str] = ..., session_id: _Optional[str] = ..., status: _Optional[str] = ..., detail: _Optional[str] = ...) -> None: ...

class RuntimeDrainResultDTO(_message.Message):
    __slots__ = ("lifecycle_state", "accepting_jobs", "blockers", "interrupted_resources")
    LIFECYCLE_STATE_FIELD_NUMBER: _ClassVar[int]
    ACCEPTING_JOBS_FIELD_NUMBER: _ClassVar[int]
    BLOCKERS_FIELD_NUMBER: _ClassVar[int]
    INTERRUPTED_RESOURCES_FIELD_NUMBER: _ClassVar[int]
    lifecycle_state: str
    accepting_jobs: bool
    blockers: _containers.RepeatedCompositeFieldContainer[RuntimeDrainBlockerDTO]
    interrupted_resources: int
    def __init__(self, lifecycle_state: _Optional[str] = ..., accepting_jobs: _Optional[bool] = ..., blockers: _Optional[_Iterable[_Union[RuntimeDrainBlockerDTO, _Mapping]]] = ..., interrupted_resources: _Optional[int] = ...) -> None: ...

class RuntimeInfoDTO(_message.Message):
    __slots__ = ("pid", "uptime_seconds", "workspace_id", "active_jobs", "lifecycle_state", "accepting_jobs", "blockers", "loaded_agents")
    PID_FIELD_NUMBER: _ClassVar[int]
    UPTIME_SECONDS_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_JOBS_FIELD_NUMBER: _ClassVar[int]
    LIFECYCLE_STATE_FIELD_NUMBER: _ClassVar[int]
    ACCEPTING_JOBS_FIELD_NUMBER: _ClassVar[int]
    BLOCKERS_FIELD_NUMBER: _ClassVar[int]
    LOADED_AGENTS_FIELD_NUMBER: _ClassVar[int]
    pid: int
    uptime_seconds: int
    workspace_id: str
    active_jobs: int
    lifecycle_state: str
    accepting_jobs: bool
    blockers: _containers.RepeatedCompositeFieldContainer[RuntimeDrainBlockerDTO]
    loaded_agents: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, pid: _Optional[int] = ..., uptime_seconds: _Optional[int] = ..., workspace_id: _Optional[str] = ..., active_jobs: _Optional[int] = ..., lifecycle_state: _Optional[str] = ..., accepting_jobs: _Optional[bool] = ..., blockers: _Optional[_Iterable[_Union[RuntimeDrainBlockerDTO, _Mapping]]] = ..., loaded_agents: _Optional[_Iterable[str]] = ...) -> None: ...

class RuntimeShutdownDTO(_message.Message):
    __slots__ = ("status", "delay_seconds")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DELAY_SECONDS_FIELD_NUMBER: _ClassVar[int]
    status: str
    delay_seconds: int
    def __init__(self, status: _Optional[str] = ..., delay_seconds: _Optional[int] = ...) -> None: ...

class RuntimeShutdownResultDTO(_message.Message):
    __slots__ = ("status", "delay_seconds")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DELAY_SECONDS_FIELD_NUMBER: _ClassVar[int]
    status: str
    delay_seconds: int
    def __init__(self, status: _Optional[str] = ..., delay_seconds: _Optional[int] = ...) -> None: ...

class RuntimeStatusDTO(_message.Message):
    __slots__ = ("pid", "uptime_seconds", "workspace_id", "active_jobs", "lifecycle_state", "accepting_jobs", "blockers", "loaded_agents")
    PID_FIELD_NUMBER: _ClassVar[int]
    UPTIME_SECONDS_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_JOBS_FIELD_NUMBER: _ClassVar[int]
    LIFECYCLE_STATE_FIELD_NUMBER: _ClassVar[int]
    ACCEPTING_JOBS_FIELD_NUMBER: _ClassVar[int]
    BLOCKERS_FIELD_NUMBER: _ClassVar[int]
    LOADED_AGENTS_FIELD_NUMBER: _ClassVar[int]
    pid: int
    uptime_seconds: int
    workspace_id: str
    active_jobs: int
    lifecycle_state: str
    accepting_jobs: bool
    blockers: _containers.RepeatedCompositeFieldContainer[RuntimeDrainBlockerDTO]
    loaded_agents: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, pid: _Optional[int] = ..., uptime_seconds: _Optional[int] = ..., workspace_id: _Optional[str] = ..., active_jobs: _Optional[int] = ..., lifecycle_state: _Optional[str] = ..., accepting_jobs: _Optional[bool] = ..., blockers: _Optional[_Iterable[_Union[RuntimeDrainBlockerDTO, _Mapping]]] = ..., loaded_agents: _Optional[_Iterable[str]] = ...) -> None: ...

class SessionChangesSummaryDTO(_message.Message):
    __slots__ = ("files", "additions", "deletions")
    FILES_FIELD_NUMBER: _ClassVar[int]
    ADDITIONS_FIELD_NUMBER: _ClassVar[int]
    DELETIONS_FIELD_NUMBER: _ClassVar[int]
    files: int
    additions: int
    deletions: int
    def __init__(self, files: _Optional[int] = ..., additions: _Optional[int] = ..., deletions: _Optional[int] = ...) -> None: ...

class SessionChangesetDTO(_message.Message):
    __slots__ = ("session_id", "changeset_id", "label", "description", "change_kind", "turn_id", "status", "summary", "files", "generated_at")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CHANGESET_ID_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    CHANGE_KIND_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    GENERATED_AT_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    changeset_id: str
    label: str
    description: str
    change_kind: str
    turn_id: str
    status: str
    summary: SessionChangesSummaryDTO
    files: _containers.RepeatedCompositeFieldContainer[SessionFileChangeDTO]
    generated_at: _timestamp_pb2.Timestamp
    def __init__(self, session_id: _Optional[str] = ..., changeset_id: _Optional[str] = ..., label: _Optional[str] = ..., description: _Optional[str] = ..., change_kind: _Optional[str] = ..., turn_id: _Optional[str] = ..., status: _Optional[str] = ..., summary: _Optional[_Union[SessionChangesSummaryDTO, _Mapping]] = ..., files: _Optional[_Iterable[_Union[SessionFileChangeDTO, _Mapping]]] = ..., generated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class SessionChangesetListDTO(_message.Message):
    __slots__ = ("session_id", "items")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    items: _containers.RepeatedCompositeFieldContainer[SessionChangesetListItemDTO]
    def __init__(self, session_id: _Optional[str] = ..., items: _Optional[_Iterable[_Union[SessionChangesetListItemDTO, _Mapping]]] = ...) -> None: ...

class SessionChangesetListItemDTO(_message.Message):
    __slots__ = ("changeset_id", "label", "description", "change_kind", "is_default", "turn_id", "summary")
    CHANGESET_ID_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    CHANGE_KIND_FIELD_NUMBER: _ClassVar[int]
    IS_DEFAULT_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    changeset_id: str
    label: str
    description: str
    change_kind: str
    is_default: bool
    turn_id: str
    summary: SessionChangesSummaryDTO
    def __init__(self, changeset_id: _Optional[str] = ..., label: _Optional[str] = ..., description: _Optional[str] = ..., change_kind: _Optional[str] = ..., is_default: _Optional[bool] = ..., turn_id: _Optional[str] = ..., summary: _Optional[_Union[SessionChangesSummaryDTO, _Mapping]] = ...) -> None: ...

class SessionCompactResultDTO(_message.Message):
    __slots__ = ("session_id", "status", "message", "before_message_count", "effective_message_count_before", "effective_message_count_after", "summarized_message_count", "retained_message_count", "summary", "strategy", "compacted_at")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    BEFORE_MESSAGE_COUNT_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_MESSAGE_COUNT_BEFORE_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_MESSAGE_COUNT_AFTER_FIELD_NUMBER: _ClassVar[int]
    SUMMARIZED_MESSAGE_COUNT_FIELD_NUMBER: _ClassVar[int]
    RETAINED_MESSAGE_COUNT_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    STRATEGY_FIELD_NUMBER: _ClassVar[int]
    COMPACTED_AT_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    status: str
    message: str
    before_message_count: int
    effective_message_count_before: int
    effective_message_count_after: int
    summarized_message_count: int
    retained_message_count: int
    summary: str
    strategy: str
    compacted_at: _timestamp_pb2.Timestamp
    def __init__(self, session_id: _Optional[str] = ..., status: _Optional[str] = ..., message: _Optional[str] = ..., before_message_count: _Optional[int] = ..., effective_message_count_before: _Optional[int] = ..., effective_message_count_after: _Optional[int] = ..., summarized_message_count: _Optional[int] = ..., retained_message_count: _Optional[int] = ..., summary: _Optional[str] = ..., strategy: _Optional[str] = ..., compacted_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class SessionCompletedExecutionEventDTO(_message.Message):
    __slots__ = ("event_id", "session_id", "job_id", "time", "type", "payload")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    session_id: str
    job_id: str
    time: _timestamp_pb2.Timestamp
    type: str
    payload: JobProgressDTO
    def __init__(self, event_id: _Optional[str] = ..., session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., type: _Optional[str] = ..., payload: _Optional[_Union[JobProgressDTO, _Mapping]] = ...) -> None: ...

class SessionContextProvenanceRefDTO(_message.Message):
    __slots__ = ("kind", "token")
    KIND_FIELD_NUMBER: _ClassVar[int]
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    kind: str
    token: str
    def __init__(self, kind: _Optional[str] = ..., token: _Optional[str] = ...) -> None: ...

class SessionContextResourceDTO(_message.Message):
    __slots__ = ("display_uri", "resource_kind", "owner_scope", "facet", "revision", "availability", "provenance_ref")
    DISPLAY_URI_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_KIND_FIELD_NUMBER: _ClassVar[int]
    OWNER_SCOPE_FIELD_NUMBER: _ClassVar[int]
    FACET_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    AVAILABILITY_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_REF_FIELD_NUMBER: _ClassVar[int]
    display_uri: str
    resource_kind: str
    owner_scope: str
    facet: str
    revision: str
    availability: str
    provenance_ref: SessionContextProvenanceRefDTO
    def __init__(self, display_uri: _Optional[str] = ..., resource_kind: _Optional[str] = ..., owner_scope: _Optional[str] = ..., facet: _Optional[str] = ..., revision: _Optional[str] = ..., availability: _Optional[str] = ..., provenance_ref: _Optional[_Union[SessionContextProvenanceRefDTO, _Mapping]] = ...) -> None: ...

class SessionContextItemDTO(_message.Message):
    __slots__ = ("kind", "role", "record_index", "text", "reasoning", "tool_summary", "tool_calls", "tool_results", "data", "raw_record", "truncated", "resource_identity", "uri", "scope", "assembly_ref", "resource_refs")
    KIND_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    RECORD_INDEX_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    REASONING_FIELD_NUMBER: _ClassVar[int]
    TOOL_SUMMARY_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLS_FIELD_NUMBER: _ClassVar[int]
    TOOL_RESULTS_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    RAW_RECORD_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    ASSEMBLY_REF_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_REFS_FIELD_NUMBER: _ClassVar[int]
    kind: str
    role: str
    record_index: int
    text: str
    reasoning: str
    tool_summary: _containers.RepeatedScalarFieldContainer[str]
    tool_calls: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    tool_results: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Struct]
    data: _struct_pb2.Struct
    raw_record: _struct_pb2.Struct
    truncated: bool
    resource_identity: str
    uri: str
    scope: str
    assembly_ref: str
    resource_refs: _containers.RepeatedCompositeFieldContainer[SessionContextResourceDTO]
    def __init__(self, kind: _Optional[str] = ..., role: _Optional[str] = ..., record_index: _Optional[int] = ..., text: _Optional[str] = ..., reasoning: _Optional[str] = ..., tool_summary: _Optional[_Iterable[str]] = ..., tool_calls: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., tool_results: _Optional[_Iterable[_Union[_struct_pb2.Struct, _Mapping]]] = ..., data: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., raw_record: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., truncated: _Optional[bool] = ..., resource_identity: _Optional[str] = ..., uri: _Optional[str] = ..., scope: _Optional[str] = ..., assembly_ref: _Optional[str] = ..., resource_refs: _Optional[_Iterable[_Union[SessionContextResourceDTO, _Mapping]]] = ...) -> None: ...

class SessionContextPartialErrorDTO(_message.Message):
    __slots__ = ("error", "resource_identity")
    ERROR_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    error: str
    resource_identity: str
    def __init__(self, error: _Optional[str] = ..., resource_identity: _Optional[str] = ...) -> None: ...

class SessionContextReadRequest(_message.Message):
    __slots__ = ("view", "include", "recent_rounds", "include_initial_goal", "cursor", "limit", "max_chars", "revision", "resource_identity", "uri", "scope", "record_index", "assembly_ref")
    VIEW_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_FIELD_NUMBER: _ClassVar[int]
    RECENT_ROUNDS_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_INITIAL_GOAL_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    MAX_CHARS_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    RECORD_INDEX_FIELD_NUMBER: _ClassVar[int]
    ASSEMBLY_REF_FIELD_NUMBER: _ClassVar[int]
    view: str
    include: _containers.RepeatedScalarFieldContainer[str]
    recent_rounds: int
    include_initial_goal: bool
    cursor: str
    limit: int
    max_chars: int
    revision: str
    resource_identity: str
    uri: str
    scope: str
    record_index: int
    assembly_ref: str
    def __init__(self, view: _Optional[str] = ..., include: _Optional[_Iterable[str]] = ..., recent_rounds: _Optional[int] = ..., include_initial_goal: _Optional[bool] = ..., cursor: _Optional[str] = ..., limit: _Optional[int] = ..., max_chars: _Optional[int] = ..., revision: _Optional[str] = ..., resource_identity: _Optional[str] = ..., uri: _Optional[str] = ..., scope: _Optional[str] = ..., record_index: _Optional[int] = ..., assembly_ref: _Optional[str] = ...) -> None: ...

class SessionContextReadResultDTO(_message.Message):
    __slots__ = ("view", "revision", "compacted", "compaction_cutoff", "raw_message_count", "effective_record_count", "returned_chars", "truncated", "has_more", "next_cursor", "items", "partial_errors", "omitted_partial_error_count", "resource_identity", "uri", "scope", "record_index", "assembly_ref")
    VIEW_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    COMPACTED_FIELD_NUMBER: _ClassVar[int]
    COMPACTION_CUTOFF_FIELD_NUMBER: _ClassVar[int]
    RAW_MESSAGE_COUNT_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_RECORD_COUNT_FIELD_NUMBER: _ClassVar[int]
    RETURNED_CHARS_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    HAS_MORE_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    PARTIAL_ERRORS_FIELD_NUMBER: _ClassVar[int]
    OMITTED_PARTIAL_ERROR_COUNT_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    RECORD_INDEX_FIELD_NUMBER: _ClassVar[int]
    ASSEMBLY_REF_FIELD_NUMBER: _ClassVar[int]
    view: str
    revision: str
    compacted: bool
    compaction_cutoff: int
    raw_message_count: int
    effective_record_count: int
    returned_chars: int
    truncated: bool
    has_more: bool
    next_cursor: str
    items: _containers.RepeatedCompositeFieldContainer[SessionContextItemDTO]
    partial_errors: _containers.RepeatedCompositeFieldContainer[SessionContextPartialErrorDTO]
    omitted_partial_error_count: int
    resource_identity: str
    uri: str
    scope: str
    record_index: int
    assembly_ref: str
    def __init__(self, view: _Optional[str] = ..., revision: _Optional[str] = ..., compacted: _Optional[bool] = ..., compaction_cutoff: _Optional[int] = ..., raw_message_count: _Optional[int] = ..., effective_record_count: _Optional[int] = ..., returned_chars: _Optional[int] = ..., truncated: _Optional[bool] = ..., has_more: _Optional[bool] = ..., next_cursor: _Optional[str] = ..., items: _Optional[_Iterable[_Union[SessionContextItemDTO, _Mapping]]] = ..., partial_errors: _Optional[_Iterable[_Union[SessionContextPartialErrorDTO, _Mapping]]] = ..., omitted_partial_error_count: _Optional[int] = ..., resource_identity: _Optional[str] = ..., uri: _Optional[str] = ..., scope: _Optional[str] = ..., record_index: _Optional[int] = ..., assembly_ref: _Optional[str] = ...) -> None: ...

class SessionContextSearchMatchDTO(_message.Message):
    __slots__ = ("preview", "source", "revision", "record_index", "match_start", "match_end", "resource_identity", "uri", "scope")
    PREVIEW_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    RECORD_INDEX_FIELD_NUMBER: _ClassVar[int]
    MATCH_START_FIELD_NUMBER: _ClassVar[int]
    MATCH_END_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    preview: str
    source: str
    revision: str
    record_index: int
    match_start: int
    match_end: int
    resource_identity: str
    uri: str
    scope: str
    def __init__(self, preview: _Optional[str] = ..., source: _Optional[str] = ..., revision: _Optional[str] = ..., record_index: _Optional[int] = ..., match_start: _Optional[int] = ..., match_end: _Optional[int] = ..., resource_identity: _Optional[str] = ..., uri: _Optional[str] = ..., scope: _Optional[str] = ...) -> None: ...

class SessionContextSearchRequest(_message.Message):
    __slots__ = ("query", "sources", "match_mode", "case_sensitive", "max_results", "max_chars", "cursor", "revision", "resource_identity", "uri", "scope")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    MATCH_MODE_FIELD_NUMBER: _ClassVar[int]
    CASE_SENSITIVE_FIELD_NUMBER: _ClassVar[int]
    MAX_RESULTS_FIELD_NUMBER: _ClassVar[int]
    MAX_CHARS_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    query: str
    sources: _containers.RepeatedScalarFieldContainer[str]
    match_mode: str
    case_sensitive: bool
    max_results: int
    max_chars: int
    cursor: str
    revision: str
    resource_identity: str
    uri: str
    scope: str
    def __init__(self, query: _Optional[str] = ..., sources: _Optional[_Iterable[str]] = ..., match_mode: _Optional[str] = ..., case_sensitive: _Optional[bool] = ..., max_results: _Optional[int] = ..., max_chars: _Optional[int] = ..., cursor: _Optional[str] = ..., revision: _Optional[str] = ..., resource_identity: _Optional[str] = ..., uri: _Optional[str] = ..., scope: _Optional[str] = ...) -> None: ...

class SessionContextSearchResultDTO(_message.Message):
    __slots__ = ("query", "match_mode", "revision", "returned_chars", "truncated", "has_more", "next_cursor", "total_matches", "matches", "partial_errors", "omitted_partial_error_count", "resource_identity", "uri", "scope")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    MATCH_MODE_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    RETURNED_CHARS_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    HAS_MORE_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    TOTAL_MATCHES_FIELD_NUMBER: _ClassVar[int]
    MATCHES_FIELD_NUMBER: _ClassVar[int]
    PARTIAL_ERRORS_FIELD_NUMBER: _ClassVar[int]
    OMITTED_PARTIAL_ERROR_COUNT_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    query: str
    match_mode: str
    revision: str
    returned_chars: int
    truncated: bool
    has_more: bool
    next_cursor: str
    total_matches: int
    matches: _containers.RepeatedCompositeFieldContainer[SessionContextSearchMatchDTO]
    partial_errors: _containers.RepeatedCompositeFieldContainer[SessionContextPartialErrorDTO]
    omitted_partial_error_count: int
    resource_identity: str
    uri: str
    scope: str
    def __init__(self, query: _Optional[str] = ..., match_mode: _Optional[str] = ..., revision: _Optional[str] = ..., returned_chars: _Optional[int] = ..., truncated: _Optional[bool] = ..., has_more: _Optional[bool] = ..., next_cursor: _Optional[str] = ..., total_matches: _Optional[int] = ..., matches: _Optional[_Iterable[_Union[SessionContextSearchMatchDTO, _Mapping]]] = ..., partial_errors: _Optional[_Iterable[_Union[SessionContextPartialErrorDTO, _Mapping]]] = ..., omitted_partial_error_count: _Optional[int] = ..., resource_identity: _Optional[str] = ..., uri: _Optional[str] = ..., scope: _Optional[str] = ...) -> None: ...

class SessionControlResultDTO(_message.Message):
    __slots__ = ("session_id", "action", "status")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    action: str
    status: str
    def __init__(self, session_id: _Optional[str] = ..., action: _Optional[str] = ..., status: _Optional[str] = ...) -> None: ...

class SessionCreateRequest(_message.Message):
    __slots__ = ("title", "agent_id", "folder_id")
    TITLE_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    FOLDER_ID_FIELD_NUMBER: _ClassVar[int]
    title: str
    agent_id: str
    folder_id: str
    def __init__(self, title: _Optional[str] = ..., agent_id: _Optional[str] = ..., folder_id: _Optional[str] = ...) -> None: ...

class SessionDTO(_message.Message):
    __slots__ = ("created_at", "updated_at", "session_id", "workspace_id", "title", "title_source", "current_agent_id", "current_provider_id", "parent_session_id", "context_source_session_id", "kind", "generation_origin", "resource_identity", "uri", "scope", "thread_id")
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    TITLE_SOURCE_FIELD_NUMBER: _ClassVar[int]
    CURRENT_AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    CURRENT_PROVIDER_ID_FIELD_NUMBER: _ClassVar[int]
    PARENT_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_SOURCE_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    GENERATION_ORIGIN_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    session_id: str
    workspace_id: str
    title: str
    title_source: str
    current_agent_id: str
    current_provider_id: str
    parent_session_id: str
    context_source_session_id: str
    kind: str
    generation_origin: SessionGenerationOriginDTO
    resource_identity: str
    uri: str
    scope: str
    thread_id: str
    def __init__(self, created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., session_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., title: _Optional[str] = ..., title_source: _Optional[str] = ..., current_agent_id: _Optional[str] = ..., current_provider_id: _Optional[str] = ..., parent_session_id: _Optional[str] = ..., context_source_session_id: _Optional[str] = ..., kind: _Optional[str] = ..., generation_origin: _Optional[_Union[SessionGenerationOriginDTO, _Mapping]] = ..., resource_identity: _Optional[str] = ..., uri: _Optional[str] = ..., scope: _Optional[str] = ..., thread_id: _Optional[str] = ...) -> None: ...

class SessionErrorExecutionEventDTO(_message.Message):
    __slots__ = ("event_id", "session_id", "job_id", "time", "type", "payload")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    session_id: str
    job_id: str
    time: _timestamp_pb2.Timestamp
    type: str
    payload: SessionErrorPayloadDTO
    def __init__(self, event_id: _Optional[str] = ..., session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., type: _Optional[str] = ..., payload: _Optional[_Union[SessionErrorPayloadDTO, _Mapping]] = ...) -> None: ...

class SessionErrorPayloadDTO(_message.Message):
    __slots__ = ("error",)
    ERROR_FIELD_NUMBER: _ClassVar[int]
    error: str
    def __init__(self, error: _Optional[str] = ...) -> None: ...

class SessionExecutionEventBaseDTO(_message.Message):
    __slots__ = ("event_id", "session_id", "job_id", "time")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    session_id: str
    job_id: str
    time: _timestamp_pb2.Timestamp
    def __init__(self, event_id: _Optional[str] = ..., session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class SessionExecutionSnapshotDTO(_message.Message):
    __slots__ = ("created_at", "updated_at", "session", "message", "job", "steps", "status", "active_step_status", "last_event_id")
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    SESSION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    JOB_FIELD_NUMBER: _ClassVar[int]
    STEPS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_STEP_STATUS_FIELD_NUMBER: _ClassVar[int]
    LAST_EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    session: SessionDTO
    message: MessageDTO
    job: JobDTO
    steps: _containers.RepeatedCompositeFieldContainer[StepDTO]
    status: str
    active_step_status: str
    last_event_id: str
    def __init__(self, created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., session: _Optional[_Union[SessionDTO, _Mapping]] = ..., message: _Optional[_Union[MessageDTO, _Mapping]] = ..., job: _Optional[_Union[JobDTO, _Mapping]] = ..., steps: _Optional[_Iterable[_Union[StepDTO, _Mapping]]] = ..., status: _Optional[str] = ..., active_step_status: _Optional[str] = ..., last_event_id: _Optional[str] = ...) -> None: ...

class SessionExecutionSseDTO(_message.Message):
    __slots__ = ("event", "raw_type", "raw_payload")
    EVENT_FIELD_NUMBER: _ClassVar[int]
    RAW_TYPE_FIELD_NUMBER: _ClassVar[int]
    RAW_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    event: _struct_pb2.Struct
    raw_type: str
    raw_payload: _struct_pb2.Struct
    def __init__(self, event: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., raw_type: _Optional[str] = ..., raw_payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SessionFileChangeDTO(_message.Message):
    __slots__ = ("file_path", "kind", "additions", "deletions", "reviewed", "latest_edit_id", "tool_call_ids", "execution_ids", "turn_ids", "diff_text", "before_preview", "after_preview")
    FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    ADDITIONS_FIELD_NUMBER: _ClassVar[int]
    DELETIONS_FIELD_NUMBER: _ClassVar[int]
    REVIEWED_FIELD_NUMBER: _ClassVar[int]
    LATEST_EDIT_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_IDS_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_IDS_FIELD_NUMBER: _ClassVar[int]
    TURN_IDS_FIELD_NUMBER: _ClassVar[int]
    DIFF_TEXT_FIELD_NUMBER: _ClassVar[int]
    BEFORE_PREVIEW_FIELD_NUMBER: _ClassVar[int]
    AFTER_PREVIEW_FIELD_NUMBER: _ClassVar[int]
    file_path: str
    kind: str
    additions: int
    deletions: int
    reviewed: bool
    latest_edit_id: str
    tool_call_ids: _containers.RepeatedScalarFieldContainer[str]
    execution_ids: _containers.RepeatedScalarFieldContainer[str]
    turn_ids: _containers.RepeatedScalarFieldContainer[str]
    diff_text: str
    before_preview: str
    after_preview: str
    def __init__(self, file_path: _Optional[str] = ..., kind: _Optional[str] = ..., additions: _Optional[int] = ..., deletions: _Optional[int] = ..., reviewed: _Optional[bool] = ..., latest_edit_id: _Optional[str] = ..., tool_call_ids: _Optional[_Iterable[str]] = ..., execution_ids: _Optional[_Iterable[str]] = ..., turn_ids: _Optional[_Iterable[str]] = ..., diff_text: _Optional[str] = ..., before_preview: _Optional[str] = ..., after_preview: _Optional[str] = ...) -> None: ...

class SessionFileReviewRequest(_message.Message):
    __slots__ = ("file_path", "reviewed")
    FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    REVIEWED_FIELD_NUMBER: _ClassVar[int]
    file_path: str
    reviewed: bool
    def __init__(self, file_path: _Optional[str] = ..., reviewed: _Optional[bool] = ...) -> None: ...

class SessionFileReviewResultDTO(_message.Message):
    __slots__ = ("session_id", "file_path", "reviewed")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    FILE_PATH_FIELD_NUMBER: _ClassVar[int]
    REVIEWED_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    file_path: str
    reviewed: bool
    def __init__(self, session_id: _Optional[str] = ..., file_path: _Optional[str] = ..., reviewed: _Optional[bool] = ...) -> None: ...

class SessionFileTreeSettingsDTO(_message.Message):
    __slots__ = ("session_id", "session_shortcuts", "workspace_shortcuts", "default_shortcuts", "effective_shortcuts")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_SHORTCUTS_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_SHORTCUTS_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_SHORTCUTS_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_SHORTCUTS_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    session_shortcuts: _containers.RepeatedCompositeFieldContainer[FileTreeShortcutDTO]
    workspace_shortcuts: _containers.RepeatedCompositeFieldContainer[FileTreeShortcutDTO]
    default_shortcuts: _containers.RepeatedCompositeFieldContainer[FileTreeShortcutDTO]
    effective_shortcuts: _containers.RepeatedCompositeFieldContainer[FileTreeShortcutDTO]
    def __init__(self, session_id: _Optional[str] = ..., session_shortcuts: _Optional[_Iterable[_Union[FileTreeShortcutDTO, _Mapping]]] = ..., workspace_shortcuts: _Optional[_Iterable[_Union[FileTreeShortcutDTO, _Mapping]]] = ..., default_shortcuts: _Optional[_Iterable[_Union[FileTreeShortcutDTO, _Mapping]]] = ..., effective_shortcuts: _Optional[_Iterable[_Union[FileTreeShortcutDTO, _Mapping]]] = ...) -> None: ...

class SessionForkRequest(_message.Message):
    __slots__ = ("mode", "anchor_mode", "source_uri", "idempotency_key", "source_view_id", "source_view_revision", "anchor_item_id", "target_container_uri", "creation_kind", "title", "relationship", "resources", "goal")
    MODE_FIELD_NUMBER: _ClassVar[int]
    ANCHOR_MODE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_URI_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_REVISION_FIELD_NUMBER: _ClassVar[int]
    ANCHOR_ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_CONTAINER_URI_FIELD_NUMBER: _ClassVar[int]
    CREATION_KIND_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    RELATIONSHIP_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_FIELD_NUMBER: _ClassVar[int]
    GOAL_FIELD_NUMBER: _ClassVar[int]
    mode: str
    anchor_mode: str
    source_uri: str
    idempotency_key: str
    source_view_id: str
    source_view_revision: int
    anchor_item_id: str
    target_container_uri: str
    creation_kind: str
    title: str
    relationship: str
    resources: _containers.RepeatedCompositeFieldContainer[SessionForkResourceChoice]
    goal: SessionForkGoalSelection
    def __init__(self, mode: _Optional[str] = ..., anchor_mode: _Optional[str] = ..., source_uri: _Optional[str] = ..., idempotency_key: _Optional[str] = ..., source_view_id: _Optional[str] = ..., source_view_revision: _Optional[int] = ..., anchor_item_id: _Optional[str] = ..., target_container_uri: _Optional[str] = ..., creation_kind: _Optional[str] = ..., title: _Optional[str] = ..., relationship: _Optional[str] = ..., resources: _Optional[_Iterable[_Union[SessionForkResourceChoice, _Mapping]]] = ..., goal: _Optional[_Union[SessionForkGoalSelection, _Mapping]] = ...) -> None: ...

class SessionForkGoalSelection(_message.Message):
    __slots__ = ("token_budget",)
    TOKEN_BUDGET_FIELD_NUMBER: _ClassVar[int]
    token_budget: int
    def __init__(self, token_budget: _Optional[int] = ...) -> None: ...

class SessionForkResourceChoice(_message.Message):
    __slots__ = ("resource_uri", "action", "owner_reference")
    RESOURCE_URI_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    OWNER_REFERENCE_FIELD_NUMBER: _ClassVar[int]
    resource_uri: str
    action: str
    owner_reference: SessionForkOwnerReference
    def __init__(self, resource_uri: _Optional[str] = ..., action: _Optional[str] = ..., owner_reference: _Optional[_Union[SessionForkOwnerReference, _Mapping]] = ...) -> None: ...

class SessionForkOwnerReference(_message.Message):
    __slots__ = ("kind", "resource_id", "workspace_id", "expected_revision")
    KIND_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_REVISION_FIELD_NUMBER: _ClassVar[int]
    kind: str
    resource_id: str
    workspace_id: str
    expected_revision: int
    def __init__(self, kind: _Optional[str] = ..., resource_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., expected_revision: _Optional[int] = ...) -> None: ...

class SessionForkResourceResult(_message.Message):
    __slots__ = ("resource_uri", "action", "availability", "target_uri", "reason", "owner_reference", "owner_resource", "operation_state", "committed_revision")
    RESOURCE_URI_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    AVAILABILITY_FIELD_NUMBER: _ClassVar[int]
    TARGET_URI_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    OWNER_REFERENCE_FIELD_NUMBER: _ClassVar[int]
    OWNER_RESOURCE_FIELD_NUMBER: _ClassVar[int]
    OPERATION_STATE_FIELD_NUMBER: _ClassVar[int]
    COMMITTED_REVISION_FIELD_NUMBER: _ClassVar[int]
    resource_uri: str
    action: str
    availability: str
    target_uri: str
    reason: str
    owner_reference: SessionForkOwnerReference
    owner_resource: WorkspaceResourceDTO
    operation_state: str
    committed_revision: int
    def __init__(self, resource_uri: _Optional[str] = ..., action: _Optional[str] = ..., availability: _Optional[str] = ..., target_uri: _Optional[str] = ..., reason: _Optional[str] = ..., owner_reference: _Optional[_Union[SessionForkOwnerReference, _Mapping]] = ..., owner_resource: _Optional[_Union[WorkspaceResourceDTO, _Mapping]] = ..., operation_state: _Optional[str] = ..., committed_revision: _Optional[int] = ...) -> None: ...

class SessionForkExportReleaseDTO(_message.Message):
    __slots__ = ("state", "error")
    STATE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    state: str
    error: str
    def __init__(self, state: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class SessionForkResultDTO(_message.Message):
    __slots__ = ("session", "thread_id", "thread_uri", "creation_kind", "source_uri", "source_view_id", "source_view_revision", "anchor_item_id", "mode", "resources", "goal_id", "export_release", "target_gateway_id")
    SESSION_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_URI_FIELD_NUMBER: _ClassVar[int]
    CREATION_KIND_FIELD_NUMBER: _ClassVar[int]
    SOURCE_URI_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_REVISION_FIELD_NUMBER: _ClassVar[int]
    ANCHOR_ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_FIELD_NUMBER: _ClassVar[int]
    GOAL_ID_FIELD_NUMBER: _ClassVar[int]
    EXPORT_RELEASE_FIELD_NUMBER: _ClassVar[int]
    TARGET_GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    session: SessionDTO
    thread_id: str
    thread_uri: str
    creation_kind: str
    source_uri: str
    source_view_id: str
    source_view_revision: int
    anchor_item_id: str
    mode: str
    resources: _containers.RepeatedCompositeFieldContainer[SessionForkResourceResult]
    goal_id: str
    export_release: SessionForkExportReleaseDTO
    target_gateway_id: str
    def __init__(self, session: _Optional[_Union[SessionDTO, _Mapping]] = ..., thread_id: _Optional[str] = ..., thread_uri: _Optional[str] = ..., creation_kind: _Optional[str] = ..., source_uri: _Optional[str] = ..., source_view_id: _Optional[str] = ..., source_view_revision: _Optional[int] = ..., anchor_item_id: _Optional[str] = ..., mode: _Optional[str] = ..., resources: _Optional[_Iterable[_Union[SessionForkResourceResult, _Mapping]]] = ..., goal_id: _Optional[str] = ..., export_release: _Optional[_Union[SessionForkExportReleaseDTO, _Mapping]] = ..., target_gateway_id: _Optional[str] = ...) -> None: ...

class SessionForkResourceSourceDTO(_message.Message):
    __slots__ = ("resource_uri", "name", "content_type", "availability", "required_for_context")
    RESOURCE_URI_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    AVAILABILITY_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_FOR_CONTEXT_FIELD_NUMBER: _ClassVar[int]
    resource_uri: str
    name: str
    content_type: str
    availability: str
    required_for_context: bool
    def __init__(self, resource_uri: _Optional[str] = ..., name: _Optional[str] = ..., content_type: _Optional[str] = ..., availability: _Optional[str] = ..., required_for_context: _Optional[bool] = ...) -> None: ...

class SessionForkSourcePointDTO(_message.Message):
    __slots__ = ("source_uri", "source_view_id", "source_view_revision", "anchor_item_id", "child_container_uri", "main_container_uri", "title", "goal_available", "resources", "resource_mode", "resource_anchor_mode", "live_resources")
    SOURCE_URI_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_REVISION_FIELD_NUMBER: _ClassVar[int]
    ANCHOR_ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    CHILD_CONTAINER_URI_FIELD_NUMBER: _ClassVar[int]
    MAIN_CONTAINER_URI_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    GOAL_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_MODE_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_ANCHOR_MODE_FIELD_NUMBER: _ClassVar[int]
    LIVE_RESOURCES_FIELD_NUMBER: _ClassVar[int]
    source_uri: str
    source_view_id: str
    source_view_revision: int
    anchor_item_id: str
    child_container_uri: str
    main_container_uri: str
    title: str
    goal_available: bool
    resources: _containers.RepeatedCompositeFieldContainer[SessionForkResourceSourceDTO]
    resource_mode: str
    resource_anchor_mode: str
    live_resources: _containers.RepeatedCompositeFieldContainer[WorkspaceResourceDTO]
    def __init__(self, source_uri: _Optional[str] = ..., source_view_id: _Optional[str] = ..., source_view_revision: _Optional[int] = ..., anchor_item_id: _Optional[str] = ..., child_container_uri: _Optional[str] = ..., main_container_uri: _Optional[str] = ..., title: _Optional[str] = ..., goal_available: _Optional[bool] = ..., resources: _Optional[_Iterable[_Union[SessionForkResourceSourceDTO, _Mapping]]] = ..., resource_mode: _Optional[str] = ..., resource_anchor_mode: _Optional[str] = ..., live_resources: _Optional[_Iterable[_Union[WorkspaceResourceDTO, _Mapping]]] = ...) -> None: ...

class SessionGenerationOriginDTO(_message.Message):
    __slots__ = ("generator_id", "run_id", "idempotency_key", "generator_type_id", "generator_type_version")
    GENERATOR_ID_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    GENERATOR_TYPE_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATOR_TYPE_VERSION_FIELD_NUMBER: _ClassVar[int]
    generator_id: str
    run_id: str
    idempotency_key: str
    generator_type_id: str
    generator_type_version: str
    def __init__(self, generator_id: _Optional[str] = ..., run_id: _Optional[str] = ..., idempotency_key: _Optional[str] = ..., generator_type_id: _Optional[str] = ..., generator_type_version: _Optional[str] = ...) -> None: ...

class SessionGoalClearResultDTO(_message.Message):
    __slots__ = ("session_id", "cleared", "thread_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CLEARED_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    cleared: bool
    thread_id: str
    def __init__(self, session_id: _Optional[str] = ..., cleared: _Optional[bool] = ..., thread_id: _Optional[str] = ...) -> None: ...

class SessionSkillUntrackRequest(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class SessionSkillUntrackErrorDTO(_message.Message):
    __slots__ = ("code", "message")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    def __init__(self, code: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class SessionSkillUntrackResultDTO(_message.Message):
    __slots__ = ("session_id", "thread_id", "name", "mode", "status", "display_uri", "revision", "content_hash", "append_status", "tracked", "queued", "error")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_URI_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    APPEND_STATUS_FIELD_NUMBER: _ClassVar[int]
    TRACKED_FIELD_NUMBER: _ClassVar[int]
    QUEUED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    thread_id: str
    name: str
    mode: str
    status: str
    display_uri: str
    revision: str
    content_hash: str
    append_status: str
    tracked: bool
    queued: bool
    error: SessionSkillUntrackErrorDTO
    def __init__(self, session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., name: _Optional[str] = ..., mode: _Optional[str] = ..., status: _Optional[str] = ..., display_uri: _Optional[str] = ..., revision: _Optional[str] = ..., content_hash: _Optional[str] = ..., append_status: _Optional[str] = ..., tracked: _Optional[bool] = ..., queued: _Optional[bool] = ..., error: _Optional[_Union[SessionSkillUntrackErrorDTO, _Mapping]] = ...) -> None: ...

class SessionGoalDTO(_message.Message):
    __slots__ = ("goal_id", "session_id", "objective", "status", "token_budget", "tokens_used", "time_used_seconds", "revision", "created_at", "updated_at", "last_accounted_job_id", "last_continued_job_id", "accounted_jobs", "thread_id")
    GOAL_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    OBJECTIVE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TOKEN_BUDGET_FIELD_NUMBER: _ClassVar[int]
    TOKENS_USED_FIELD_NUMBER: _ClassVar[int]
    TIME_USED_SECONDS_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    LAST_ACCOUNTED_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    LAST_CONTINUED_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    ACCOUNTED_JOBS_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    goal_id: str
    session_id: str
    objective: str
    status: str
    token_budget: int
    tokens_used: int
    time_used_seconds: int
    revision: int
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    last_accounted_job_id: str
    last_continued_job_id: str
    accounted_jobs: _struct_pb2.Struct
    thread_id: str
    def __init__(self, goal_id: _Optional[str] = ..., session_id: _Optional[str] = ..., objective: _Optional[str] = ..., status: _Optional[str] = ..., token_budget: _Optional[int] = ..., tokens_used: _Optional[int] = ..., time_used_seconds: _Optional[int] = ..., revision: _Optional[int] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., last_accounted_job_id: _Optional[str] = ..., last_continued_job_id: _Optional[str] = ..., accounted_jobs: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., thread_id: _Optional[str] = ...) -> None: ...

class SessionGoalSetRequest(_message.Message):
    __slots__ = ("objective", "status", "token_budget", "replace")
    OBJECTIVE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TOKEN_BUDGET_FIELD_NUMBER: _ClassVar[int]
    REPLACE_FIELD_NUMBER: _ClassVar[int]
    objective: str
    status: str
    token_budget: int
    replace: bool
    def __init__(self, objective: _Optional[str] = ..., status: _Optional[str] = ..., token_budget: _Optional[int] = ..., replace: _Optional[bool] = ...) -> None: ...

class SessionInformationErrorDTO(_message.Message):
    __slots__ = ("event_id", "job_id", "type", "message", "timestamp", "message_truncated")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    job_id: str
    type: str
    message: str
    timestamp: _timestamp_pb2.Timestamp
    message_truncated: bool
    def __init__(self, event_id: _Optional[str] = ..., job_id: _Optional[str] = ..., type: _Optional[str] = ..., message: _Optional[str] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., message_truncated: _Optional[bool] = ...) -> None: ...

class SessionInformationExecutionDTO(_message.Message):
    __slots__ = ("job_id", "status", "current_tool", "last_error", "last_error_truncated")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_TOOL_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    status: str
    current_tool: str
    last_error: str
    last_error_truncated: bool
    def __init__(self, job_id: _Optional[str] = ..., status: _Optional[str] = ..., current_tool: _Optional[str] = ..., last_error: _Optional[str] = ..., last_error_truncated: _Optional[bool] = ...) -> None: ...

class SessionInformationResourceDTO(_message.Message):
    __slots__ = ("resource_id", "kind", "name", "status", "updated_at", "ended_at", "name_truncated")
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    NAME_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    resource_id: str
    kind: str
    name: str
    status: str
    updated_at: _timestamp_pb2.Timestamp
    ended_at: _timestamp_pb2.Timestamp
    name_truncated: bool
    def __init__(self, resource_id: _Optional[str] = ..., kind: _Optional[str] = ..., name: _Optional[str] = ..., status: _Optional[str] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., ended_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., name_truncated: _Optional[bool] = ...) -> None: ...

class SessionInformationSessionDTO(_message.Message):
    __slots__ = ("session_id", "workspace_id", "title", "current_agent_id", "current_provider_id", "parent_session_id", "context_source_session_id", "kind", "created_at", "updated_at", "title_truncated")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CURRENT_AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    CURRENT_PROVIDER_ID_FIELD_NUMBER: _ClassVar[int]
    PARENT_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_SOURCE_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    TITLE_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    workspace_id: str
    title: str
    current_agent_id: str
    current_provider_id: str
    parent_session_id: str
    context_source_session_id: str
    kind: str
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    title_truncated: bool
    def __init__(self, session_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., title: _Optional[str] = ..., current_agent_id: _Optional[str] = ..., current_provider_id: _Optional[str] = ..., parent_session_id: _Optional[str] = ..., context_source_session_id: _Optional[str] = ..., kind: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., title_truncated: _Optional[bool] = ...) -> None: ...

class SessionInformationResourceSummaryDTO(_message.Message):
    __slots__ = ("active", "recent_closed", "active_count", "recent_closed_count", "active_truncated", "recent_closed_truncated", "historical_omitted")
    ACTIVE_FIELD_NUMBER: _ClassVar[int]
    RECENT_CLOSED_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_COUNT_FIELD_NUMBER: _ClassVar[int]
    RECENT_CLOSED_COUNT_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    RECENT_CLOSED_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    HISTORICAL_OMITTED_FIELD_NUMBER: _ClassVar[int]
    active: _containers.RepeatedCompositeFieldContainer[SessionInformationResourceDTO]
    recent_closed: _containers.RepeatedCompositeFieldContainer[SessionInformationResourceDTO]
    active_count: int
    recent_closed_count: int
    active_truncated: bool
    recent_closed_truncated: bool
    historical_omitted: bool
    def __init__(self, active: _Optional[_Iterable[_Union[SessionInformationResourceDTO, _Mapping]]] = ..., recent_closed: _Optional[_Iterable[_Union[SessionInformationResourceDTO, _Mapping]]] = ..., active_count: _Optional[int] = ..., recent_closed_count: _Optional[int] = ..., active_truncated: _Optional[bool] = ..., recent_closed_truncated: _Optional[bool] = ..., historical_omitted: _Optional[bool] = ...) -> None: ...

class SessionInformationRelationsDTO(_message.Message):
    __slots__ = ("child_count", "child_ids", "child_ids_truncated")
    CHILD_COUNT_FIELD_NUMBER: _ClassVar[int]
    CHILD_IDS_FIELD_NUMBER: _ClassVar[int]
    CHILD_IDS_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    child_count: int
    child_ids: _containers.RepeatedScalarFieldContainer[str]
    child_ids_truncated: bool
    def __init__(self, child_count: _Optional[int] = ..., child_ids: _Optional[_Iterable[str]] = ..., child_ids_truncated: _Optional[bool] = ...) -> None: ...

class SessionInformationSnapshotDTO(_message.Message):
    __slots__ = ("kind", "schema_version", "generated_at", "session", "workspace", "execution", "trace", "relations", "resources", "recent_errors")
    KIND_FIELD_NUMBER: _ClassVar[int]
    SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    GENERATED_AT_FIELD_NUMBER: _ClassVar[int]
    SESSION_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    RELATIONS_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_FIELD_NUMBER: _ClassVar[int]
    RECENT_ERRORS_FIELD_NUMBER: _ClassVar[int]
    kind: str
    schema_version: int
    generated_at: _timestamp_pb2.Timestamp
    session: SessionInformationSessionDTO
    workspace: SessionInformationWorkspaceDTO
    execution: SessionInformationExecutionDTO
    trace: SessionInformationTraceDTO
    relations: SessionInformationRelationsDTO
    resources: SessionInformationResourceSummaryDTO
    recent_errors: _containers.RepeatedCompositeFieldContainer[SessionInformationErrorDTO]
    def __init__(self, kind: _Optional[str] = ..., schema_version: _Optional[int] = ..., generated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., session: _Optional[_Union[SessionInformationSessionDTO, _Mapping]] = ..., workspace: _Optional[_Union[SessionInformationWorkspaceDTO, _Mapping]] = ..., execution: _Optional[_Union[SessionInformationExecutionDTO, _Mapping]] = ..., trace: _Optional[_Union[SessionInformationTraceDTO, _Mapping]] = ..., relations: _Optional[_Union[SessionInformationRelationsDTO, _Mapping]] = ..., resources: _Optional[_Union[SessionInformationResourceSummaryDTO, _Mapping]] = ..., recent_errors: _Optional[_Iterable[_Union[SessionInformationErrorDTO, _Mapping]]] = ...) -> None: ...

class SessionInformationTraceDTO(_message.Message):
    __slots__ = ("observed_event_count", "last_event_id", "last_event_type", "last_event_at", "truncated")
    OBSERVED_EVENT_COUNT_FIELD_NUMBER: _ClassVar[int]
    LAST_EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    LAST_EVENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    LAST_EVENT_AT_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    observed_event_count: int
    last_event_id: str
    last_event_type: str
    last_event_at: _timestamp_pb2.Timestamp
    truncated: bool
    def __init__(self, observed_event_count: _Optional[int] = ..., last_event_id: _Optional[str] = ..., last_event_type: _Optional[str] = ..., last_event_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., truncated: _Optional[bool] = ...) -> None: ...

class SessionInformationWorkspaceDTO(_message.Message):
    __slots__ = ("workspace_id", "name")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    name: str
    def __init__(self, workspace_id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class SessionInterruptResultDTO(_message.Message):
    __slots__ = ("session_id", "job_id", "status", "phase", "tool_name", "interrupted_at", "interrupt_request_id", "thread_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    INTERRUPTED_AT_FIELD_NUMBER: _ClassVar[int]
    INTERRUPT_REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    job_id: str
    status: str
    phase: str
    tool_name: str
    interrupted_at: _timestamp_pb2.Timestamp
    interrupt_request_id: str
    thread_id: str
    def __init__(self, session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., status: _Optional[str] = ..., phase: _Optional[str] = ..., tool_name: _Optional[str] = ..., interrupted_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., interrupt_request_id: _Optional[str] = ..., thread_id: _Optional[str] = ...) -> None: ...

class SessionListResultDTO(_message.Message):
    __slots__ = ("items", "total", "cursor")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[SessionDTO]
    total: int
    cursor: str
    def __init__(self, items: _Optional[_Iterable[_Union[SessionDTO, _Mapping]]] = ..., total: _Optional[int] = ..., cursor: _Optional[str] = ...) -> None: ...

class GlobalThreadAddress(_message.Message):
    __slots__ = ("gateway_id", "workspace_id", "session_id", "thread_id")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    workspace_id: str
    session_id: str
    thread_id: str
    def __init__(self, gateway_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., session_id: _Optional[str] = ..., thread_id: _Optional[str] = ...) -> None: ...

class CommunicationPayload(_message.Message):
    __slots__ = ("content", "kind", "reply_to_communication_id", "delivery_policy")
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    REPLY_TO_COMMUNICATION_ID_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_POLICY_FIELD_NUMBER: _ClassVar[int]
    content: str
    kind: str
    reply_to_communication_id: str
    delivery_policy: str
    def __init__(self, content: _Optional[str] = ..., kind: _Optional[str] = ..., reply_to_communication_id: _Optional[str] = ..., delivery_policy: _Optional[str] = ...) -> None: ...

class CommunicationDiscoveryRequest(_message.Message):
    __slots__ = ("source", "session_id", "workspace_id", "gateway_id")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    source: GlobalThreadAddress
    session_id: str
    workspace_id: str
    gateway_id: str
    def __init__(self, source: _Optional[_Union[GlobalThreadAddress, _Mapping]] = ..., session_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., gateway_id: _Optional[str] = ...) -> None: ...

class CommunicationAcceptanceRequest(_message.Message):
    __slots__ = ("communication_id", "source", "target", "payload", "request_id")
    COMMUNICATION_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    communication_id: str
    source: GlobalThreadAddress
    target: GlobalThreadAddress
    payload: CommunicationPayload
    request_id: str
    def __init__(self, communication_id: _Optional[str] = ..., source: _Optional[_Union[GlobalThreadAddress, _Mapping]] = ..., target: _Optional[_Union[GlobalThreadAddress, _Mapping]] = ..., payload: _Optional[_Union[CommunicationPayload, _Mapping]] = ..., request_id: _Optional[str] = ...) -> None: ...

class CommunicationReceipt(_message.Message):
    __slots__ = ("communication_id", "source", "target", "payload_hash", "admission_id", "state", "accepted_at")
    COMMUNICATION_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_HASH_FIELD_NUMBER: _ClassVar[int]
    ADMISSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_AT_FIELD_NUMBER: _ClassVar[int]
    communication_id: str
    source: GlobalThreadAddress
    target: GlobalThreadAddress
    payload_hash: str
    admission_id: str
    state: str
    accepted_at: str
    def __init__(self, communication_id: _Optional[str] = ..., source: _Optional[_Union[GlobalThreadAddress, _Mapping]] = ..., target: _Optional[_Union[GlobalThreadAddress, _Mapping]] = ..., payload_hash: _Optional[str] = ..., admission_id: _Optional[str] = ..., state: _Optional[str] = ..., accepted_at: _Optional[str] = ...) -> None: ...

class CommunicationObservationRequest(_message.Message):
    __slots__ = ("observation_id", "source_call_id", "page_identity", "source", "target", "operation", "query", "page_ordinal", "expires_at", "terminal_replay")
    OBSERVATION_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    PAGE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    PAGE_ORDINAL_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    TERMINAL_REPLAY_FIELD_NUMBER: _ClassVar[int]
    observation_id: str
    source_call_id: str
    page_identity: str
    source: GlobalThreadAddress
    target: GlobalThreadAddress
    operation: str
    query: _struct_pb2.Struct
    page_ordinal: int
    expires_at: str
    terminal_replay: bool
    def __init__(self, observation_id: _Optional[str] = ..., source_call_id: _Optional[str] = ..., page_identity: _Optional[str] = ..., source: _Optional[_Union[GlobalThreadAddress, _Mapping]] = ..., target: _Optional[_Union[GlobalThreadAddress, _Mapping]] = ..., operation: _Optional[str] = ..., query: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., page_ordinal: _Optional[int] = ..., expires_at: _Optional[str] = ..., terminal_replay: _Optional[bool] = ...) -> None: ...

class ContextGrantTarget(_message.Message):
    __slots__ = ("gateway_id", "resource_identity", "uri", "scope")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    resource_identity: str
    uri: str
    scope: str
    def __init__(self, gateway_id: _Optional[str] = ..., resource_identity: _Optional[str] = ..., uri: _Optional[str] = ..., scope: _Optional[str] = ...) -> None: ...

class ContextQueryRequest(_message.Message):
    __slots__ = ("source_gateway_id", "resource_target", "read", "search")
    SOURCE_GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_TARGET_FIELD_NUMBER: _ClassVar[int]
    READ_FIELD_NUMBER: _ClassVar[int]
    SEARCH_FIELD_NUMBER: _ClassVar[int]
    source_gateway_id: str
    resource_target: ContextGrantTarget
    read: SessionContextReadRequest
    search: SessionContextSearchRequest
    def __init__(self, source_gateway_id: _Optional[str] = ..., resource_target: _Optional[_Union[ContextGrantTarget, _Mapping]] = ..., read: _Optional[_Union[SessionContextReadRequest, _Mapping]] = ..., search: _Optional[_Union[SessionContextSearchRequest, _Mapping]] = ...) -> None: ...

class CommunicationGatewayRequest(_message.Message):
    __slots__ = ("operation", "target", "resource_target", "fork_target", "navigation_target", "mutation_target", "body")
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_TARGET_FIELD_NUMBER: _ClassVar[int]
    FORK_TARGET_FIELD_NUMBER: _ClassVar[int]
    NAVIGATION_TARGET_FIELD_NUMBER: _ClassVar[int]
    MUTATION_TARGET_FIELD_NUMBER: _ClassVar[int]
    BODY_FIELD_NUMBER: _ClassVar[int]
    operation: str
    target: GlobalThreadAddress
    resource_target: ContextGrantTarget
    fork_target: ForkGrantTarget
    navigation_target: ResourceNavigationGrantTarget
    mutation_target: TextMutationGrantTarget
    body: _struct_pb2.Struct
    def __init__(self, operation: _Optional[str] = ..., target: _Optional[_Union[GlobalThreadAddress, _Mapping]] = ..., resource_target: _Optional[_Union[ContextGrantTarget, _Mapping]] = ..., fork_target: _Optional[_Union[ForkGrantTarget, _Mapping]] = ..., navigation_target: _Optional[_Union[ResourceNavigationGrantTarget, _Mapping]] = ..., mutation_target: _Optional[_Union[TextMutationGrantTarget, _Mapping]] = ..., body: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class ForkGrantTarget(_message.Message):
    __slots__ = ("gateway_id", "uri", "fork_role")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    FORK_ROLE_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    uri: str
    fork_role: str
    def __init__(self, gateway_id: _Optional[str] = ..., uri: _Optional[str] = ..., fork_role: _Optional[str] = ...) -> None: ...

class ResourceNavigationGrantTarget(_message.Message):
    __slots__ = ("gateway_id", "uri")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    uri: str
    def __init__(self, gateway_id: _Optional[str] = ..., uri: _Optional[str] = ...) -> None: ...

class ResourceNavigationRelayRequest(_message.Message):
    __slots__ = ("source_gateway_id", "navigation_target", "navigation")
    SOURCE_GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    NAVIGATION_TARGET_FIELD_NUMBER: _ClassVar[int]
    NAVIGATION_FIELD_NUMBER: _ClassVar[int]
    source_gateway_id: str
    navigation_target: ResourceNavigationGrantTarget
    navigation: ResourceNavigationRequest
    def __init__(self, source_gateway_id: _Optional[str] = ..., navigation_target: _Optional[_Union[ResourceNavigationGrantTarget, _Mapping]] = ..., navigation: _Optional[_Union[ResourceNavigationRequest, _Mapping]] = ...) -> None: ...

class ForkCapturedPoint(_message.Message):
    __slots__ = ("source_view_id", "source_view_revision", "anchor_item_id")
    SOURCE_VIEW_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_REVISION_FIELD_NUMBER: _ClassVar[int]
    ANCHOR_ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    source_view_id: str
    source_view_revision: int
    anchor_item_id: str
    def __init__(self, source_view_id: _Optional[str] = ..., source_view_revision: _Optional[int] = ..., anchor_item_id: _Optional[str] = ...) -> None: ...

class ForkSourceCaptureDTO(_message.Message):
    __slots__ = ("copy_operation_id", "creation_key", "source_session_id", "source_thread_id", "target_session_id", "target_thread_id", "snapshot_id", "fork_id", "materialization_id", "request_json", "preimage_hash", "state", "snapshot_sha256", "marker_sha256", "abort_reason", "source_point")
    COPY_OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    CREATION_KEY_FIELD_NUMBER: _ClassVar[int]
    SOURCE_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    FORK_ID_FIELD_NUMBER: _ClassVar[int]
    MATERIALIZATION_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_JSON_FIELD_NUMBER: _ClassVar[int]
    PREIMAGE_HASH_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_SHA256_FIELD_NUMBER: _ClassVar[int]
    MARKER_SHA256_FIELD_NUMBER: _ClassVar[int]
    ABORT_REASON_FIELD_NUMBER: _ClassVar[int]
    SOURCE_POINT_FIELD_NUMBER: _ClassVar[int]
    copy_operation_id: str
    creation_key: str
    source_session_id: str
    source_thread_id: str
    target_session_id: str
    target_thread_id: str
    snapshot_id: str
    fork_id: str
    materialization_id: str
    request_json: str
    preimage_hash: str
    state: str
    snapshot_sha256: str
    marker_sha256: str
    abort_reason: str
    source_point: ForkCapturedPoint
    def __init__(self, copy_operation_id: _Optional[str] = ..., creation_key: _Optional[str] = ..., source_session_id: _Optional[str] = ..., source_thread_id: _Optional[str] = ..., target_session_id: _Optional[str] = ..., target_thread_id: _Optional[str] = ..., snapshot_id: _Optional[str] = ..., fork_id: _Optional[str] = ..., materialization_id: _Optional[str] = ..., request_json: _Optional[str] = ..., preimage_hash: _Optional[str] = ..., state: _Optional[str] = ..., snapshot_sha256: _Optional[str] = ..., marker_sha256: _Optional[str] = ..., abort_reason: _Optional[str] = ..., source_point: _Optional[_Union[ForkCapturedPoint, _Mapping]] = ...) -> None: ...

class SessionForkTransferRequest(_message.Message):
    __slots__ = ("source_gateway_id", "fork_target", "action", "request", "capture", "cursor", "file_name", "offset", "generation_origin", "release_state")
    SOURCE_GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    FORK_TARGET_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    REQUEST_FIELD_NUMBER: _ClassVar[int]
    CAPTURE_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    FILE_NAME_FIELD_NUMBER: _ClassVar[int]
    OFFSET_FIELD_NUMBER: _ClassVar[int]
    GENERATION_ORIGIN_FIELD_NUMBER: _ClassVar[int]
    RELEASE_STATE_FIELD_NUMBER: _ClassVar[int]
    source_gateway_id: str
    fork_target: ForkGrantTarget
    action: str
    request: SessionForkRequest
    capture: ForkSourceCaptureDTO
    cursor: int
    file_name: str
    offset: int
    generation_origin: SessionGenerationOriginDTO
    release_state: str
    def __init__(self, source_gateway_id: _Optional[str] = ..., fork_target: _Optional[_Union[ForkGrantTarget, _Mapping]] = ..., action: _Optional[str] = ..., request: _Optional[_Union[SessionForkRequest, _Mapping]] = ..., capture: _Optional[_Union[ForkSourceCaptureDTO, _Mapping]] = ..., cursor: _Optional[int] = ..., file_name: _Optional[str] = ..., offset: _Optional[int] = ..., generation_origin: _Optional[_Union[SessionGenerationOriginDTO, _Mapping]] = ..., release_state: _Optional[str] = ...) -> None: ...

class ForkTransferFileDTO(_message.Message):
    __slots__ = ("name", "size_bytes", "content_base64")
    NAME_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    CONTENT_BASE64_FIELD_NUMBER: _ClassVar[int]
    name: str
    size_bytes: int
    content_base64: str
    def __init__(self, name: _Optional[str] = ..., size_bytes: _Optional[int] = ..., content_base64: _Optional[str] = ...) -> None: ...

class SessionForkTransferResultDTO(_message.Message):
    __slots__ = ("capture", "intent", "files", "next_cursor", "chunk_base64", "chunk_sha256", "offset", "result")
    CAPTURE_FIELD_NUMBER: _ClassVar[int]
    INTENT_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    CHUNK_BASE64_FIELD_NUMBER: _ClassVar[int]
    CHUNK_SHA256_FIELD_NUMBER: _ClassVar[int]
    OFFSET_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    capture: ForkSourceCaptureDTO
    intent: _struct_pb2.Struct
    files: _containers.RepeatedCompositeFieldContainer[ForkTransferFileDTO]
    next_cursor: int
    chunk_base64: str
    chunk_sha256: str
    offset: int
    result: SessionForkResultDTO
    def __init__(self, capture: _Optional[_Union[ForkSourceCaptureDTO, _Mapping]] = ..., intent: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., files: _Optional[_Iterable[_Union[ForkTransferFileDTO, _Mapping]]] = ..., next_cursor: _Optional[int] = ..., chunk_base64: _Optional[str] = ..., chunk_sha256: _Optional[str] = ..., offset: _Optional[int] = ..., result: _Optional[_Union[SessionForkResultDTO, _Mapping]] = ...) -> None: ...

class SessionNetworkWaitDTO(_message.Message):
    __slots__ = ("id", "session_id", "message", "restored", "created_at", "restored_at")
    ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RESTORED_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    RESTORED_AT_FIELD_NUMBER: _ClassVar[int]
    id: str
    session_id: str
    message: str
    restored: bool
    created_at: _timestamp_pb2.Timestamp
    restored_at: _timestamp_pb2.Timestamp
    def __init__(self, id: _Optional[str] = ..., session_id: _Optional[str] = ..., message: _Optional[str] = ..., restored: _Optional[bool] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., restored_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class SessionObservationStateDTO(_message.Message):
    __slots__ = ("session_id", "active_job_id", "is_streaming", "is_idle")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    IS_STREAMING_FIELD_NUMBER: _ClassVar[int]
    IS_IDLE_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    active_job_id: str
    is_streaming: bool
    is_idle: bool
    def __init__(self, session_id: _Optional[str] = ..., active_job_id: _Optional[str] = ..., is_streaming: _Optional[bool] = ..., is_idle: _Optional[bool] = ...) -> None: ...

class SessionResourceControlRequest(_message.Message):
    __slots__ = ("action", "params")
    ACTION_FIELD_NUMBER: _ClassVar[int]
    PARAMS_FIELD_NUMBER: _ClassVar[int]
    action: str
    params: _struct_pb2.Struct
    def __init__(self, action: _Optional[str] = ..., params: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SessionResourceControlResultDTO(_message.Message):
    __slots__ = ("session_id", "resource_id", "kind", "action", "status", "resource")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    resource_id: str
    kind: str
    action: str
    status: str
    resource: SessionResourceDTO
    def __init__(self, session_id: _Optional[str] = ..., resource_id: _Optional[str] = ..., kind: _Optional[str] = ..., action: _Optional[str] = ..., status: _Optional[str] = ..., resource: _Optional[_Union[SessionResourceDTO, _Mapping]] = ...) -> None: ...

class SessionResourceDTO(_message.Message):
    __slots__ = ("resource_id", "session_id", "kind", "name", "status", "created_at", "updated_at", "started_at", "ended_at", "available_actions", "metadata")
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_ACTIONS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    resource_id: str
    session_id: str
    kind: str
    name: str
    status: str
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    started_at: _timestamp_pb2.Timestamp
    ended_at: _timestamp_pb2.Timestamp
    available_actions: _containers.RepeatedScalarFieldContainer[str]
    metadata: _struct_pb2.Struct
    def __init__(self, resource_id: _Optional[str] = ..., session_id: _Optional[str] = ..., kind: _Optional[str] = ..., name: _Optional[str] = ..., status: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., ended_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., available_actions: _Optional[_Iterable[str]] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SessionResourceListDTO(_message.Message):
    __slots__ = ("session_id", "items")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    items: _containers.RepeatedCompositeFieldContainer[SessionResourceDTO]
    def __init__(self, session_id: _Optional[str] = ..., items: _Optional[_Iterable[_Union[SessionResourceDTO, _Mapping]]] = ...) -> None: ...

class ResourceThreadRef(_message.Message):
    __slots__ = ("session_id", "thread_id")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    thread_id: str
    def __init__(self, session_id: _Optional[str] = ..., thread_id: _Optional[str] = ...) -> None: ...

class ResourceNavigationRequest(_message.Message):
    __slots__ = ("uri", "operation", "query", "limit", "max_bytes", "cursor", "timeout_ms", "scan_limit", "max_response_bytes", "read_format", "line_offset", "max_lines", "search_mode", "file_glob", "max_snippet_bytes", "search_options")
    URI_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    MAX_BYTES_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    SCAN_LIMIT_FIELD_NUMBER: _ClassVar[int]
    MAX_RESPONSE_BYTES_FIELD_NUMBER: _ClassVar[int]
    READ_FORMAT_FIELD_NUMBER: _ClassVar[int]
    LINE_OFFSET_FIELD_NUMBER: _ClassVar[int]
    MAX_LINES_FIELD_NUMBER: _ClassVar[int]
    SEARCH_MODE_FIELD_NUMBER: _ClassVar[int]
    FILE_GLOB_FIELD_NUMBER: _ClassVar[int]
    MAX_SNIPPET_BYTES_FIELD_NUMBER: _ClassVar[int]
    SEARCH_OPTIONS_FIELD_NUMBER: _ClassVar[int]
    uri: str
    operation: str
    query: str
    limit: int
    max_bytes: int
    cursor: str
    timeout_ms: int
    scan_limit: int
    max_response_bytes: int
    read_format: str
    line_offset: int
    max_lines: int
    search_mode: str
    file_glob: str
    max_snippet_bytes: int
    search_options: ResourceNavigationSearchOptions
    def __init__(self, uri: _Optional[str] = ..., operation: _Optional[str] = ..., query: _Optional[str] = ..., limit: _Optional[int] = ..., max_bytes: _Optional[int] = ..., cursor: _Optional[str] = ..., timeout_ms: _Optional[int] = ..., scan_limit: _Optional[int] = ..., max_response_bytes: _Optional[int] = ..., read_format: _Optional[str] = ..., line_offset: _Optional[int] = ..., max_lines: _Optional[int] = ..., search_mode: _Optional[str] = ..., file_glob: _Optional[str] = ..., max_snippet_bytes: _Optional[int] = ..., search_options: _Optional[_Union[ResourceNavigationSearchOptions, _Mapping]] = ...) -> None: ...

class ResourceNavigationSearchOptions(_message.Message):
    __slots__ = ("case_mode", "is_regex", "whole_word", "word_separators", "is_multiline", "all_matches", "include_globs", "exclude_globs", "ignore_mode", "context_lines", "excluded_resource_uris", "files_only", "excluded_subtree_uris")
    CASE_MODE_FIELD_NUMBER: _ClassVar[int]
    IS_REGEX_FIELD_NUMBER: _ClassVar[int]
    WHOLE_WORD_FIELD_NUMBER: _ClassVar[int]
    WORD_SEPARATORS_FIELD_NUMBER: _ClassVar[int]
    IS_MULTILINE_FIELD_NUMBER: _ClassVar[int]
    ALL_MATCHES_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_GLOBS_FIELD_NUMBER: _ClassVar[int]
    EXCLUDE_GLOBS_FIELD_NUMBER: _ClassVar[int]
    IGNORE_MODE_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_LINES_FIELD_NUMBER: _ClassVar[int]
    EXCLUDED_RESOURCE_URIS_FIELD_NUMBER: _ClassVar[int]
    FILES_ONLY_FIELD_NUMBER: _ClassVar[int]
    EXCLUDED_SUBTREE_URIS_FIELD_NUMBER: _ClassVar[int]
    case_mode: str
    is_regex: bool
    whole_word: bool
    word_separators: str
    is_multiline: bool
    all_matches: bool
    include_globs: _containers.RepeatedScalarFieldContainer[str]
    exclude_globs: _containers.RepeatedScalarFieldContainer[str]
    ignore_mode: str
    context_lines: int
    excluded_resource_uris: _containers.RepeatedScalarFieldContainer[str]
    files_only: bool
    excluded_subtree_uris: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, case_mode: _Optional[str] = ..., is_regex: _Optional[bool] = ..., whole_word: _Optional[bool] = ..., word_separators: _Optional[str] = ..., is_multiline: _Optional[bool] = ..., all_matches: _Optional[bool] = ..., include_globs: _Optional[_Iterable[str]] = ..., exclude_globs: _Optional[_Iterable[str]] = ..., ignore_mode: _Optional[str] = ..., context_lines: _Optional[int] = ..., excluded_resource_uris: _Optional[_Iterable[str]] = ..., files_only: _Optional[bool] = ..., excluded_subtree_uris: _Optional[_Iterable[str]] = ...) -> None: ...

class ResourceNavigationNodeDTO(_message.Message):
    __slots__ = ("uri", "resource_shape", "operations", "child_path", "stat", "canonical_uri")
    URI_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_SHAPE_FIELD_NUMBER: _ClassVar[int]
    OPERATIONS_FIELD_NUMBER: _ClassVar[int]
    CHILD_PATH_FIELD_NUMBER: _ClassVar[int]
    STAT_FIELD_NUMBER: _ClassVar[int]
    CANONICAL_URI_FIELD_NUMBER: _ClassVar[int]
    uri: str
    resource_shape: str
    operations: _containers.RepeatedScalarFieldContainer[str]
    child_path: str
    stat: ResourceNavigationStatDTO
    canonical_uri: str
    def __init__(self, uri: _Optional[str] = ..., resource_shape: _Optional[str] = ..., operations: _Optional[_Iterable[str]] = ..., child_path: _Optional[str] = ..., stat: _Optional[_Union[ResourceNavigationStatDTO, _Mapping]] = ..., canonical_uri: _Optional[str] = ...) -> None: ...

class ResourceNavigationStatDTO(_message.Message):
    __slots__ = ("size", "modified_at_ms", "changed_at_ms")
    SIZE_FIELD_NUMBER: _ClassVar[int]
    MODIFIED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    CHANGED_AT_MS_FIELD_NUMBER: _ClassVar[int]
    size: int
    modified_at_ms: float
    changed_at_ms: float
    def __init__(self, size: _Optional[int] = ..., modified_at_ms: _Optional[float] = ..., changed_at_ms: _Optional[float] = ...) -> None: ...

class ResourceNavigationResultDTO(_message.Message):
    __slots__ = ("node", "entries", "content", "truncated", "next_cursor", "truncation_reason", "view", "content_encoding", "media_type", "mime_type", "line_offset", "matches", "content_hash", "contexts")
    NODE_FIELD_NUMBER: _ClassVar[int]
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    TRUNCATION_REASON_FIELD_NUMBER: _ClassVar[int]
    VIEW_FIELD_NUMBER: _ClassVar[int]
    CONTENT_ENCODING_FIELD_NUMBER: _ClassVar[int]
    MEDIA_TYPE_FIELD_NUMBER: _ClassVar[int]
    MIME_TYPE_FIELD_NUMBER: _ClassVar[int]
    LINE_OFFSET_FIELD_NUMBER: _ClassVar[int]
    MATCHES_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    CONTEXTS_FIELD_NUMBER: _ClassVar[int]
    node: ResourceNavigationNodeDTO
    entries: _containers.RepeatedCompositeFieldContainer[ResourceNavigationNodeDTO]
    content: str
    truncated: bool
    next_cursor: str
    truncation_reason: str
    view: str
    content_encoding: str
    media_type: str
    mime_type: str
    line_offset: int
    matches: _containers.RepeatedCompositeFieldContainer[ResourceNavigationMatchDTO]
    content_hash: str
    contexts: _containers.RepeatedCompositeFieldContainer[ResourceNavigationContextDTO]
    def __init__(self, node: _Optional[_Union[ResourceNavigationNodeDTO, _Mapping]] = ..., entries: _Optional[_Iterable[_Union[ResourceNavigationNodeDTO, _Mapping]]] = ..., content: _Optional[str] = ..., truncated: _Optional[bool] = ..., next_cursor: _Optional[str] = ..., truncation_reason: _Optional[str] = ..., view: _Optional[str] = ..., content_encoding: _Optional[str] = ..., media_type: _Optional[str] = ..., mime_type: _Optional[str] = ..., line_offset: _Optional[int] = ..., matches: _Optional[_Iterable[_Union[ResourceNavigationMatchDTO, _Mapping]]] = ..., content_hash: _Optional[str] = ..., contexts: _Optional[_Iterable[_Union[ResourceNavigationContextDTO, _Mapping]]] = ...) -> None: ...

class ResourceNavigationContextDTO(_message.Message):
    __slots__ = ("uri", "line", "text", "truncated")
    URI_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    uri: str
    line: int
    text: str
    truncated: bool
    def __init__(self, uri: _Optional[str] = ..., line: _Optional[int] = ..., text: _Optional[str] = ..., truncated: _Optional[bool] = ...) -> None: ...

class ResourceNavigationMatchDTO(_message.Message):
    __slots__ = ("uri", "line", "column", "text", "snippet_start_column", "truncated", "end_line", "end_column", "utf16_column", "utf16_end_column", "snippet_start_utf16_column")
    URI_FIELD_NUMBER: _ClassVar[int]
    LINE_FIELD_NUMBER: _ClassVar[int]
    COLUMN_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    SNIPPET_START_COLUMN_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    END_LINE_FIELD_NUMBER: _ClassVar[int]
    END_COLUMN_FIELD_NUMBER: _ClassVar[int]
    UTF16_COLUMN_FIELD_NUMBER: _ClassVar[int]
    UTF16_END_COLUMN_FIELD_NUMBER: _ClassVar[int]
    SNIPPET_START_UTF16_COLUMN_FIELD_NUMBER: _ClassVar[int]
    uri: str
    line: int
    column: int
    text: str
    snippet_start_column: int
    truncated: bool
    end_line: int
    end_column: int
    utf16_column: int
    utf16_end_column: int
    snippet_start_utf16_column: int
    def __init__(self, uri: _Optional[str] = ..., line: _Optional[int] = ..., column: _Optional[int] = ..., text: _Optional[str] = ..., snippet_start_column: _Optional[int] = ..., truncated: _Optional[bool] = ..., end_line: _Optional[int] = ..., end_column: _Optional[int] = ..., utf16_column: _Optional[int] = ..., utf16_end_column: _Optional[int] = ..., snippet_start_utf16_column: _Optional[int] = ...) -> None: ...

class WorkspaceResourceDTO(_message.Message):
    __slots__ = ("resource_id", "workspace_id", "kind", "retention_scope", "source_thread", "thread_associations", "owner_revision", "status", "reconciliation_status", "unattached_since", "reclaim_at", "available_actions", "metadata", "created_at", "updated_at", "error")
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    RETENTION_SCOPE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_THREAD_FIELD_NUMBER: _ClassVar[int]
    THREAD_ASSOCIATIONS_FIELD_NUMBER: _ClassVar[int]
    OWNER_REVISION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    RECONCILIATION_STATUS_FIELD_NUMBER: _ClassVar[int]
    UNATTACHED_SINCE_FIELD_NUMBER: _ClassVar[int]
    RECLAIM_AT_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_ACTIONS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    resource_id: str
    workspace_id: str
    kind: str
    retention_scope: str
    source_thread: ResourceThreadRef
    thread_associations: _containers.RepeatedCompositeFieldContainer[ResourceThreadRef]
    owner_revision: int
    status: str
    reconciliation_status: str
    unattached_since: str
    reclaim_at: str
    available_actions: _containers.RepeatedScalarFieldContainer[str]
    metadata: _struct_pb2.Struct
    created_at: str
    updated_at: str
    error: str
    def __init__(self, resource_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., kind: _Optional[str] = ..., retention_scope: _Optional[str] = ..., source_thread: _Optional[_Union[ResourceThreadRef, _Mapping]] = ..., thread_associations: _Optional[_Iterable[_Union[ResourceThreadRef, _Mapping]]] = ..., owner_revision: _Optional[int] = ..., status: _Optional[str] = ..., reconciliation_status: _Optional[str] = ..., unattached_since: _Optional[str] = ..., reclaim_at: _Optional[str] = ..., available_actions: _Optional[_Iterable[str]] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., created_at: _Optional[str] = ..., updated_at: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class WorkspaceResourceListDTO(_message.Message):
    __slots__ = ("workspace_id", "items")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    items: _containers.RepeatedCompositeFieldContainer[WorkspaceResourceDTO]
    def __init__(self, workspace_id: _Optional[str] = ..., items: _Optional[_Iterable[_Union[WorkspaceResourceDTO, _Mapping]]] = ...) -> None: ...

class TerminalResourceCreateRequest(_message.Message):
    __slots__ = ("session_id", "thread_id", "retention_scope", "title", "cwd_relative", "cols", "rows")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    RETENTION_SCOPE_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CWD_RELATIVE_FIELD_NUMBER: _ClassVar[int]
    COLS_FIELD_NUMBER: _ClassVar[int]
    ROWS_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    thread_id: str
    retention_scope: str
    title: str
    cwd_relative: str
    cols: int
    rows: int
    def __init__(self, session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., retention_scope: _Optional[str] = ..., title: _Optional[str] = ..., cwd_relative: _Optional[str] = ..., cols: _Optional[int] = ..., rows: _Optional[int] = ...) -> None: ...

class WorkspaceResourceCommandRequest(_message.Message):
    __slots__ = ("expected_revision", "thread")
    EXPECTED_REVISION_FIELD_NUMBER: _ClassVar[int]
    THREAD_FIELD_NUMBER: _ClassVar[int]
    expected_revision: int
    thread: ResourceThreadRef
    def __init__(self, expected_revision: _Optional[int] = ..., thread: _Optional[_Union[ResourceThreadRef, _Mapping]] = ...) -> None: ...

class ResourceTransportGrantDTO(_message.Message):
    __slots__ = ("resource_id", "owner_revision", "grant_token", "expires_at", "resource", "service")
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    OWNER_REVISION_FIELD_NUMBER: _ClassVar[int]
    GRANT_TOKEN_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_FIELD_NUMBER: _ClassVar[int]
    SERVICE_FIELD_NUMBER: _ClassVar[int]
    resource_id: str
    owner_revision: int
    grant_token: str
    expires_at: str
    resource: WorkspaceResourceDTO
    service: _service_plugin_pb2.PluginServiceReference
    def __init__(self, resource_id: _Optional[str] = ..., owner_revision: _Optional[int] = ..., grant_token: _Optional[str] = ..., expires_at: _Optional[str] = ..., resource: _Optional[_Union[WorkspaceResourceDTO, _Mapping]] = ..., service: _Optional[_Union[_service_plugin_pb2.PluginServiceReference, _Mapping]] = ...) -> None: ...

class BrowserResourceCreateRequest(_message.Message):
    __slots__ = ("session_id", "thread_id", "retention_scope", "title", "url", "viewport", "device_profile", "device_orientation")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    RETENTION_SCOPE_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    VIEWPORT_FIELD_NUMBER: _ClassVar[int]
    DEVICE_PROFILE_FIELD_NUMBER: _ClassVar[int]
    DEVICE_ORIENTATION_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    thread_id: str
    retention_scope: str
    title: str
    url: str
    viewport: _struct_pb2.Struct
    device_profile: str
    device_orientation: str
    def __init__(self, session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., retention_scope: _Optional[str] = ..., title: _Optional[str] = ..., url: _Optional[str] = ..., viewport: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., device_profile: _Optional[str] = ..., device_orientation: _Optional[str] = ...) -> None: ...

class BrowserResourceOperationRequest(_message.Message):
    __slots__ = ("expected_revision", "thread", "action", "payload")
    EXPECTED_REVISION_FIELD_NUMBER: _ClassVar[int]
    THREAD_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    expected_revision: int
    thread: ResourceThreadRef
    action: str
    payload: _struct_pb2.Struct
    def __init__(self, expected_revision: _Optional[int] = ..., thread: _Optional[_Union[ResourceThreadRef, _Mapping]] = ..., action: _Optional[str] = ..., payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SessionStatusChangedExecutionEventDTO(_message.Message):
    __slots__ = ("event_id", "session_id", "job_id", "time", "type", "payload")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    session_id: str
    job_id: str
    time: _timestamp_pb2.Timestamp
    type: str
    payload: _struct_pb2.Struct
    def __init__(self, event_id: _Optional[str] = ..., session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., type: _Optional[str] = ..., payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class SessionStatusDTO(_message.Message):
    __slots__ = ("session_id", "status", "message", "active_job_id", "waiting")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    WAITING_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    status: str
    message: str
    active_job_id: str
    waiting: SessionNetworkWaitDTO
    def __init__(self, session_id: _Optional[str] = ..., status: _Optional[str] = ..., message: _Optional[str] = ..., active_job_id: _Optional[str] = ..., waiting: _Optional[_Union[SessionNetworkWaitDTO, _Mapping]] = ...) -> None: ...

class SessionTurnBootstrapDTO(_message.Message):
    __slots__ = ("session", "latest_turn", "active_job_id", "active_jobs", "active_job_count", "active_jobs_truncated", "projection_state", "older_cursor", "event_cursor", "projection_epoch", "thread_id")
    SESSION_FIELD_NUMBER: _ClassVar[int]
    LATEST_TURN_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_JOBS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_JOB_COUNT_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_JOBS_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_STATE_FIELD_NUMBER: _ClassVar[int]
    OLDER_CURSOR_FIELD_NUMBER: _ClassVar[int]
    EVENT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_EPOCH_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    session: SessionDTO
    latest_turn: TurnSummaryDTO
    active_job_id: str
    active_jobs: _containers.RepeatedCompositeFieldContainer[TurnJobSummaryDTO]
    active_job_count: int
    active_jobs_truncated: bool
    projection_state: str
    older_cursor: str
    event_cursor: str
    projection_epoch: int
    thread_id: str
    def __init__(self, session: _Optional[_Union[SessionDTO, _Mapping]] = ..., latest_turn: _Optional[_Union[TurnSummaryDTO, _Mapping]] = ..., active_job_id: _Optional[str] = ..., active_jobs: _Optional[_Iterable[_Union[TurnJobSummaryDTO, _Mapping]]] = ..., active_job_count: _Optional[int] = ..., active_jobs_truncated: _Optional[bool] = ..., projection_state: _Optional[str] = ..., older_cursor: _Optional[str] = ..., event_cursor: _Optional[str] = ..., projection_epoch: _Optional[int] = ..., thread_id: _Optional[str] = ...) -> None: ...

class SessionUpdateRequest(_message.Message):
    __slots__ = ("agent_id", "provider_id")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_ID_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    provider_id: str
    def __init__(self, agent_id: _Optional[str] = ..., provider_id: _Optional[str] = ...) -> None: ...

class StaleTurnCursorErrorDTO(_message.Message):
    __slots__ = ("code", "session_id", "cursor_epoch", "current_epoch", "message")
    CODE_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CURSOR_EPOCH_FIELD_NUMBER: _ClassVar[int]
    CURRENT_EPOCH_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: str
    session_id: str
    cursor_epoch: int
    current_epoch: int
    message: str
    def __init__(self, code: _Optional[str] = ..., session_id: _Optional[str] = ..., cursor_epoch: _Optional[int] = ..., current_epoch: _Optional[int] = ..., message: _Optional[str] = ...) -> None: ...

class StaleTurnReferenceErrorDTO(_message.Message):
    __slots__ = ("code", "session_id", "turn_ids", "message")
    CODE_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_IDS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: str
    session_id: str
    turn_ids: _containers.RepeatedScalarFieldContainer[str]
    message: str
    def __init__(self, code: _Optional[str] = ..., session_id: _Optional[str] = ..., turn_ids: _Optional[_Iterable[str]] = ..., message: _Optional[str] = ...) -> None: ...

class StepDTO(_message.Message):
    __slots__ = ("created_at", "updated_at", "step_id", "job_id", "parent_step_id", "agent_id", "step_type", "status", "input_payload", "output_payload", "started_at", "ended_at")
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    STEP_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    PARENT_STEP_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    STEP_TYPE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    INPUT_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    step_id: str
    job_id: str
    parent_step_id: str
    agent_id: str
    step_type: str
    status: str
    input_payload: _struct_pb2.Struct
    output_payload: _struct_pb2.Struct
    started_at: _timestamp_pb2.Timestamp
    ended_at: _timestamp_pb2.Timestamp
    def __init__(self, created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., step_id: _Optional[str] = ..., job_id: _Optional[str] = ..., parent_step_id: _Optional[str] = ..., agent_id: _Optional[str] = ..., step_type: _Optional[str] = ..., status: _Optional[str] = ..., input_payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., output_payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., ended_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class TeamBoardDTO(_message.Message):
    __slots__ = ("created_at", "updated_at", "team_id", "name", "session_id", "coordinator_thread_id", "version", "members", "tasks", "recent_events")
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    COORDINATOR_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    MEMBERS_FIELD_NUMBER: _ClassVar[int]
    TASKS_FIELD_NUMBER: _ClassVar[int]
    RECENT_EVENTS_FIELD_NUMBER: _ClassVar[int]
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    team_id: str
    name: str
    session_id: str
    coordinator_thread_id: str
    version: int
    members: _containers.RepeatedCompositeFieldContainer[TeamMemberDTO]
    tasks: _containers.RepeatedCompositeFieldContainer[TeamTaskDTO]
    recent_events: _containers.RepeatedCompositeFieldContainer[TeamEventDTO]
    def __init__(self, created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., team_id: _Optional[str] = ..., name: _Optional[str] = ..., session_id: _Optional[str] = ..., coordinator_thread_id: _Optional[str] = ..., version: _Optional[int] = ..., members: _Optional[_Iterable[_Union[TeamMemberDTO, _Mapping]]] = ..., tasks: _Optional[_Iterable[_Union[TeamTaskDTO, _Mapping]]] = ..., recent_events: _Optional[_Iterable[_Union[TeamEventDTO, _Mapping]]] = ...) -> None: ...

class TeamEventDTO(_message.Message):
    __slots__ = ("event_id", "team_id", "type", "actor_thread_id", "session_id", "created_at", "payload")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    TEAM_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    ACTOR_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    team_id: str
    type: str
    actor_thread_id: str
    session_id: str
    created_at: _timestamp_pb2.Timestamp
    payload: _struct_pb2.Struct
    def __init__(self, event_id: _Optional[str] = ..., team_id: _Optional[str] = ..., type: _Optional[str] = ..., actor_thread_id: _Optional[str] = ..., session_id: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class TeamListDTO(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[TeamBoardDTO]
    def __init__(self, items: _Optional[_Iterable[_Union[TeamBoardDTO, _Mapping]]] = ...) -> None: ...

class TeamMemberDTO(_message.Message):
    __slots__ = ("thread_id", "delegation_id", "role", "source", "work_mode", "instructions", "status", "joined_at", "updated_at")
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    DELEGATION_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    WORK_MODE_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTIONS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    JOINED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    thread_id: str
    delegation_id: str
    role: str
    source: str
    work_mode: str
    instructions: str
    status: str
    joined_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, thread_id: _Optional[str] = ..., delegation_id: _Optional[str] = ..., role: _Optional[str] = ..., source: _Optional[str] = ..., work_mode: _Optional[str] = ..., instructions: _Optional[str] = ..., status: _Optional[str] = ..., joined_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class TeamMemberOperationDTO(_message.Message):
    __slots__ = ("board", "member", "child_thread_id", "child_delegation_id", "child_admission_state")
    BOARD_FIELD_NUMBER: _ClassVar[int]
    MEMBER_FIELD_NUMBER: _ClassVar[int]
    CHILD_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    CHILD_DELEGATION_ID_FIELD_NUMBER: _ClassVar[int]
    CHILD_ADMISSION_STATE_FIELD_NUMBER: _ClassVar[int]
    board: TeamBoardDTO
    member: TeamMemberDTO
    child_thread_id: str
    child_delegation_id: str
    child_admission_state: str
    def __init__(self, board: _Optional[_Union[TeamBoardDTO, _Mapping]] = ..., member: _Optional[_Union[TeamMemberDTO, _Mapping]] = ..., child_thread_id: _Optional[str] = ..., child_delegation_id: _Optional[str] = ..., child_admission_state: _Optional[str] = ...) -> None: ...

class TeamTaskDTO(_message.Message):
    __slots__ = ("created_at", "updated_at", "task_id", "title", "description", "phase", "cycle", "assignee_thread_id", "status", "depends_on_task_ids", "assigned_job_id", "summary", "error", "updated_by_thread_id")
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    CYCLE_FIELD_NUMBER: _ClassVar[int]
    ASSIGNEE_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DEPENDS_ON_TASK_IDS_FIELD_NUMBER: _ClassVar[int]
    ASSIGNED_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    UPDATED_BY_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    task_id: str
    title: str
    description: str
    phase: str
    cycle: int
    assignee_thread_id: str
    status: str
    depends_on_task_ids: _containers.RepeatedScalarFieldContainer[str]
    assigned_job_id: str
    summary: str
    error: str
    updated_by_thread_id: str
    def __init__(self, created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., task_id: _Optional[str] = ..., title: _Optional[str] = ..., description: _Optional[str] = ..., phase: _Optional[str] = ..., cycle: _Optional[int] = ..., assignee_thread_id: _Optional[str] = ..., status: _Optional[str] = ..., depends_on_task_ids: _Optional[_Iterable[str]] = ..., assigned_job_id: _Optional[str] = ..., summary: _Optional[str] = ..., error: _Optional[str] = ..., updated_by_thread_id: _Optional[str] = ...) -> None: ...

class TeamTaskOperationDTO(_message.Message):
    __slots__ = ("board", "task", "dispatched_job_id")
    BOARD_FIELD_NUMBER: _ClassVar[int]
    TASK_FIELD_NUMBER: _ClassVar[int]
    DISPATCHED_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    board: TeamBoardDTO
    task: TeamTaskDTO
    dispatched_job_id: str
    def __init__(self, board: _Optional[_Union[TeamBoardDTO, _Mapping]] = ..., task: _Optional[_Union[TeamTaskDTO, _Mapping]] = ..., dispatched_job_id: _Optional[str] = ...) -> None: ...

class TimestampedDTO(_message.Message):
    __slots__ = ("created_at", "updated_at")
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ToolDTO(_message.Message):
    __slots__ = ("tool_id", "name", "origin", "description", "parameters", "category", "group_id", "group_name", "kind", "execution_enabled", "model_visible", "test_supported")
    TOOL_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    ORIGIN_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    GROUP_ID_FIELD_NUMBER: _ClassVar[int]
    GROUP_NAME_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_ENABLED_FIELD_NUMBER: _ClassVar[int]
    MODEL_VISIBLE_FIELD_NUMBER: _ClassVar[int]
    TEST_SUPPORTED_FIELD_NUMBER: _ClassVar[int]
    tool_id: str
    name: str
    origin: str
    description: str
    parameters: _struct_pb2.Struct
    category: str
    group_id: str
    group_name: str
    kind: str
    execution_enabled: bool
    model_visible: bool
    test_supported: bool
    def __init__(self, tool_id: _Optional[str] = ..., name: _Optional[str] = ..., origin: _Optional[str] = ..., description: _Optional[str] = ..., parameters: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., category: _Optional[str] = ..., group_id: _Optional[str] = ..., group_name: _Optional[str] = ..., kind: _Optional[str] = ..., execution_enabled: _Optional[bool] = ..., model_visible: _Optional[bool] = ..., test_supported: _Optional[bool] = ...) -> None: ...

class ToolSelectionChange(_message.Message):
    __slots__ = ("tool_id", "execution_enabled", "model_visible")
    TOOL_ID_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_ENABLED_FIELD_NUMBER: _ClassVar[int]
    MODEL_VISIBLE_FIELD_NUMBER: _ClassVar[int]
    tool_id: str
    execution_enabled: bool
    model_visible: bool
    def __init__(self, tool_id: _Optional[str] = ..., execution_enabled: _Optional[bool] = ..., model_visible: _Optional[bool] = ...) -> None: ...

class ToolSelectionPatchRequest(_message.Message):
    __slots__ = ("agent_id", "changes")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    CHANGES_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    changes: _containers.RepeatedCompositeFieldContainer[ToolSelectionChange]
    def __init__(self, agent_id: _Optional[str] = ..., changes: _Optional[_Iterable[_Union[ToolSelectionChange, _Mapping]]] = ...) -> None: ...

class ToolTestAttemptDTO(_message.Message):
    __slots__ = ("attempt_id", "case_id", "provider_id", "model", "status", "passed", "tool_called", "execution_succeeded", "duration_ms", "detail", "model_calls", "reasoning_only_calls", "transient_retries", "error")
    ATTEMPT_ID_FIELD_NUMBER: _ClassVar[int]
    CASE_ID_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_ID_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PASSED_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLED_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_SUCCEEDED_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    MODEL_CALLS_FIELD_NUMBER: _ClassVar[int]
    REASONING_ONLY_CALLS_FIELD_NUMBER: _ClassVar[int]
    TRANSIENT_RETRIES_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    attempt_id: str
    case_id: str
    provider_id: str
    model: str
    status: str
    passed: bool
    tool_called: bool
    execution_succeeded: bool
    duration_ms: int
    detail: str
    model_calls: int
    reasoning_only_calls: int
    transient_retries: int
    error: str
    def __init__(self, attempt_id: _Optional[str] = ..., case_id: _Optional[str] = ..., provider_id: _Optional[str] = ..., model: _Optional[str] = ..., status: _Optional[str] = ..., passed: _Optional[bool] = ..., tool_called: _Optional[bool] = ..., execution_succeeded: _Optional[bool] = ..., duration_ms: _Optional[int] = ..., detail: _Optional[str] = ..., model_calls: _Optional[int] = ..., reasoning_only_calls: _Optional[int] = ..., transient_retries: _Optional[int] = ..., error: _Optional[str] = ...) -> None: ...

class ToolTestProviderResultDTO(_message.Message):
    __slots__ = ("provider_id", "model", "status", "completed", "total", "passed", "failed", "model_calls", "reasoning_only_calls", "transient_retries", "success_rate")
    PROVIDER_ID_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    PASSED_FIELD_NUMBER: _ClassVar[int]
    FAILED_FIELD_NUMBER: _ClassVar[int]
    MODEL_CALLS_FIELD_NUMBER: _ClassVar[int]
    REASONING_ONLY_CALLS_FIELD_NUMBER: _ClassVar[int]
    TRANSIENT_RETRIES_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_RATE_FIELD_NUMBER: _ClassVar[int]
    provider_id: str
    model: str
    status: str
    completed: int
    total: int
    passed: int
    failed: int
    model_calls: int
    reasoning_only_calls: int
    transient_retries: int
    success_rate: float
    def __init__(self, provider_id: _Optional[str] = ..., model: _Optional[str] = ..., status: _Optional[str] = ..., completed: _Optional[int] = ..., total: _Optional[int] = ..., passed: _Optional[int] = ..., failed: _Optional[int] = ..., model_calls: _Optional[int] = ..., reasoning_only_calls: _Optional[int] = ..., transient_retries: _Optional[int] = ..., success_rate: _Optional[float] = ...) -> None: ...

class ToolTestRunDTO(_message.Message):
    __slots__ = ("run_id", "tool_name", "status", "progress", "created_at", "updated_at", "repetitions", "providers", "attempts", "error")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    REPETITIONS_FIELD_NUMBER: _ClassVar[int]
    PROVIDERS_FIELD_NUMBER: _ClassVar[int]
    ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    tool_name: str
    status: str
    progress: int
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    repetitions: int
    providers: _containers.RepeatedCompositeFieldContainer[ToolTestProviderResultDTO]
    attempts: _containers.RepeatedCompositeFieldContainer[ToolTestAttemptDTO]
    error: str
    def __init__(self, run_id: _Optional[str] = ..., tool_name: _Optional[str] = ..., status: _Optional[str] = ..., progress: _Optional[int] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., repetitions: _Optional[int] = ..., providers: _Optional[_Iterable[_Union[ToolTestProviderResultDTO, _Mapping]]] = ..., attempts: _Optional[_Iterable[_Union[ToolTestAttemptDTO, _Mapping]]] = ..., error: _Optional[str] = ...) -> None: ...

class ToolTestRunListDTO(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[ToolTestRunDTO]
    def __init__(self, items: _Optional[_Iterable[_Union[ToolTestRunDTO, _Mapping]]] = ...) -> None: ...

class ToolTestStartRequest(_message.Message):
    __slots__ = ("agent_id", "provider_ids", "repetitions")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_IDS_FIELD_NUMBER: _ClassVar[int]
    REPETITIONS_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    provider_ids: _containers.RepeatedScalarFieldContainer[str]
    repetitions: int
    def __init__(self, agent_id: _Optional[str] = ..., provider_ids: _Optional[_Iterable[str]] = ..., repetitions: _Optional[int] = ...) -> None: ...

class TraceEventDTO(_message.Message):
    __slots__ = ("event_id", "part_id", "session_id", "job_id", "type", "phase", "title", "content", "status", "tool_name", "skill_names", "step_id", "timestamp", "raw", "thread_id")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    PART_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    SKILL_NAMES_FIELD_NUMBER: _ClassVar[int]
    STEP_ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    RAW_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    part_id: str
    session_id: str
    job_id: str
    type: str
    phase: str
    title: str
    content: str
    status: str
    tool_name: str
    skill_names: _containers.RepeatedScalarFieldContainer[str]
    step_id: str
    timestamp: _timestamp_pb2.Timestamp
    raw: _struct_pb2.Struct
    thread_id: str
    def __init__(self, event_id: _Optional[str] = ..., part_id: _Optional[str] = ..., session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., type: _Optional[str] = ..., phase: _Optional[str] = ..., title: _Optional[str] = ..., content: _Optional[str] = ..., status: _Optional[str] = ..., tool_name: _Optional[str] = ..., skill_names: _Optional[_Iterable[str]] = ..., step_id: _Optional[str] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., raw: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., thread_id: _Optional[str] = ...) -> None: ...

class TraceObservedExecutionEventDTO(_message.Message):
    __slots__ = ("event_id", "session_id", "job_id", "time", "type", "payload")
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    event_id: str
    session_id: str
    job_id: str
    time: _timestamp_pb2.Timestamp
    type: str
    payload: TraceObservedPayloadDTO
    def __init__(self, event_id: _Optional[str] = ..., session_id: _Optional[str] = ..., job_id: _Optional[str] = ..., time: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., type: _Optional[str] = ..., payload: _Optional[_Union[TraceObservedPayloadDTO, _Mapping]] = ...) -> None: ...

class TraceObservedPayloadDTO(_message.Message):
    __slots__ = ("raw_type",)
    RAW_TYPE_FIELD_NUMBER: _ClassVar[int]
    raw_type: str
    def __init__(self, raw_type: _Optional[str] = ...) -> None: ...

class TurnActivityStatsDTO(_message.Message):
    __slots__ = ("duration_ms", "item_count", "first_item_sequence", "last_item_sequence")
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    ITEM_COUNT_FIELD_NUMBER: _ClassVar[int]
    FIRST_ITEM_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    LAST_ITEM_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    duration_ms: int
    item_count: int
    first_item_sequence: int
    last_item_sequence: int
    def __init__(self, duration_ms: _Optional[int] = ..., item_count: _Optional[int] = ..., first_item_sequence: _Optional[int] = ..., last_item_sequence: _Optional[int] = ...) -> None: ...

class TurnAttachmentDTO(_message.Message):
    __slots__ = ("file_id", "name", "content_type", "availability", "directory_uri")
    FILE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    AVAILABILITY_FIELD_NUMBER: _ClassVar[int]
    DIRECTORY_URI_FIELD_NUMBER: _ClassVar[int]
    file_id: str
    name: str
    content_type: str
    availability: str
    directory_uri: str
    def __init__(self, file_id: _Optional[str] = ..., name: _Optional[str] = ..., content_type: _Optional[str] = ..., availability: _Optional[str] = ..., directory_uri: _Optional[str] = ...) -> None: ...

class TurnBaseDTO(_message.Message):
    __slots__ = ("turn_id", "job_id", "session_id", "ordinal", "revision", "status", "created_at", "updated_at", "completed_at", "source_view_id", "source_view_revision")
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ORDINAL_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_REVISION_FIELD_NUMBER: _ClassVar[int]
    turn_id: str
    job_id: str
    session_id: str
    ordinal: int
    revision: int
    status: str
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    completed_at: _timestamp_pb2.Timestamp
    source_view_id: str
    source_view_revision: int
    def __init__(self, turn_id: _Optional[str] = ..., job_id: _Optional[str] = ..., session_id: _Optional[str] = ..., ordinal: _Optional[int] = ..., revision: _Optional[int] = ..., status: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., completed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., source_view_id: _Optional[str] = ..., source_view_revision: _Optional[int] = ...) -> None: ...

class TurnCursorDTO(_message.Message):
    __slots__ = ("version", "session_id", "projection_epoch", "anchor_turn_id", "include_anchor", "direction", "stage")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_EPOCH_FIELD_NUMBER: _ClassVar[int]
    ANCHOR_TURN_ID_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_ANCHOR_FIELD_NUMBER: _ClassVar[int]
    DIRECTION_FIELD_NUMBER: _ClassVar[int]
    STAGE_FIELD_NUMBER: _ClassVar[int]
    version: int
    session_id: str
    projection_epoch: int
    anchor_turn_id: str
    include_anchor: bool
    direction: str
    stage: int
    def __init__(self, version: _Optional[int] = ..., session_id: _Optional[str] = ..., projection_epoch: _Optional[int] = ..., anchor_turn_id: _Optional[str] = ..., include_anchor: _Optional[bool] = ..., direction: _Optional[str] = ..., stage: _Optional[int] = ...) -> None: ...

class TurnDetailBatchDTO(_message.Message):
    __slots__ = ("items", "projection_epoch", "next_cursor", "has_more")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_EPOCH_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    HAS_MORE_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[TurnDetailDTO]
    projection_epoch: int
    next_cursor: str
    has_more: bool
    def __init__(self, items: _Optional[_Iterable[_Union[TurnDetailDTO, _Mapping]]] = ..., projection_epoch: _Optional[int] = ..., next_cursor: _Optional[str] = ..., has_more: _Optional[bool] = ...) -> None: ...

class TurnDetailBatchRequest(_message.Message):
    __slots__ = ("turn_ids", "include")
    TURN_IDS_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_FIELD_NUMBER: _ClassVar[int]
    turn_ids: _containers.RepeatedScalarFieldContainer[str]
    include: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, turn_ids: _Optional[_Iterable[str]] = ..., include: _Optional[_Iterable[str]] = ...) -> None: ...

class TurnDetailCursorDTO(_message.Message):
    __slots__ = ("version", "session_id", "projection_epoch", "turn_id", "event_index", "include_hash")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_EPOCH_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    EVENT_INDEX_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_HASH_FIELD_NUMBER: _ClassVar[int]
    version: int
    session_id: str
    projection_epoch: int
    turn_id: str
    event_index: int
    include_hash: str
    def __init__(self, version: _Optional[int] = ..., session_id: _Optional[str] = ..., projection_epoch: _Optional[int] = ..., turn_id: _Optional[str] = ..., event_index: _Optional[int] = ..., include_hash: _Optional[str] = ...) -> None: ...

class TurnDetailDTO(_message.Message):
    __slots__ = ("turn_id", "job_id", "session_id", "ordinal", "revision", "status", "created_at", "updated_at", "completed_at", "items_view", "source_message_ids", "merged_job_ids", "user_messages", "response_preview", "preview_truncated", "assistant_text", "thinking_blocks", "tool_summary", "response_parts", "final_response", "items", "detail_truncated", "detail_next_cursor", "activity_stats", "root_logical_item_ordinal", "source_view_id", "source_view_revision", "tool_summary_truncated")
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ORDINAL_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    ITEMS_VIEW_FIELD_NUMBER: _ClassVar[int]
    SOURCE_MESSAGE_IDS_FIELD_NUMBER: _ClassVar[int]
    MERGED_JOB_IDS_FIELD_NUMBER: _ClassVar[int]
    USER_MESSAGES_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_PREVIEW_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    ASSISTANT_TEXT_FIELD_NUMBER: _ClassVar[int]
    THINKING_BLOCKS_FIELD_NUMBER: _ClassVar[int]
    TOOL_SUMMARY_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_PARTS_FIELD_NUMBER: _ClassVar[int]
    FINAL_RESPONSE_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    DETAIL_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    DETAIL_NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_STATS_FIELD_NUMBER: _ClassVar[int]
    ROOT_LOGICAL_ITEM_ORDINAL_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_REVISION_FIELD_NUMBER: _ClassVar[int]
    TOOL_SUMMARY_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    turn_id: str
    job_id: str
    session_id: str
    ordinal: int
    revision: int
    status: str
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    completed_at: _timestamp_pb2.Timestamp
    items_view: str
    source_message_ids: _containers.RepeatedScalarFieldContainer[str]
    merged_job_ids: _containers.RepeatedScalarFieldContainer[str]
    user_messages: _containers.RepeatedCompositeFieldContainer[TurnUserMessageDTO]
    response_preview: str
    preview_truncated: bool
    assistant_text: _containers.RepeatedScalarFieldContainer[str]
    thinking_blocks: _containers.RepeatedCompositeFieldContainer[TurnThinkingBlockDTO]
    tool_summary: _containers.RepeatedCompositeFieldContainer[TurnToolSummaryDTO]
    response_parts: _containers.RepeatedCompositeFieldContainer[TurnResponsePartDTO]
    final_response: str
    items: _containers.RepeatedCompositeFieldContainer[TraceEventDTO]
    detail_truncated: bool
    detail_next_cursor: str
    activity_stats: TurnActivityStatsDTO
    root_logical_item_ordinal: int
    source_view_id: str
    source_view_revision: int
    tool_summary_truncated: bool
    def __init__(self, turn_id: _Optional[str] = ..., job_id: _Optional[str] = ..., session_id: _Optional[str] = ..., ordinal: _Optional[int] = ..., revision: _Optional[int] = ..., status: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., completed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., items_view: _Optional[str] = ..., source_message_ids: _Optional[_Iterable[str]] = ..., merged_job_ids: _Optional[_Iterable[str]] = ..., user_messages: _Optional[_Iterable[_Union[TurnUserMessageDTO, _Mapping]]] = ..., response_preview: _Optional[str] = ..., preview_truncated: _Optional[bool] = ..., assistant_text: _Optional[_Iterable[str]] = ..., thinking_blocks: _Optional[_Iterable[_Union[TurnThinkingBlockDTO, _Mapping]]] = ..., tool_summary: _Optional[_Iterable[_Union[TurnToolSummaryDTO, _Mapping]]] = ..., response_parts: _Optional[_Iterable[_Union[TurnResponsePartDTO, _Mapping]]] = ..., final_response: _Optional[str] = ..., items: _Optional[_Iterable[_Union[TraceEventDTO, _Mapping]]] = ..., detail_truncated: _Optional[bool] = ..., detail_next_cursor: _Optional[str] = ..., activity_stats: _Optional[_Union[TurnActivityStatsDTO, _Mapping]] = ..., root_logical_item_ordinal: _Optional[int] = ..., source_view_id: _Optional[str] = ..., source_view_revision: _Optional[int] = ..., tool_summary_truncated: _Optional[bool] = ...) -> None: ...

class TurnMessageSearchRequest(_message.Message):
    __slots__ = ("query", "limit", "cursor", "category")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    query: str
    limit: int
    cursor: str
    category: str
    def __init__(self, query: _Optional[str] = ..., limit: _Optional[int] = ..., cursor: _Optional[str] = ..., category: _Optional[str] = ...) -> None: ...

class TurnMessageReferenceDTO(_message.Message):
    __slots__ = ("item_id", "content_hash", "start_codepoint", "end_codepoint", "category")
    ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    START_CODEPOINT_FIELD_NUMBER: _ClassVar[int]
    END_CODEPOINT_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    item_id: str
    content_hash: str
    start_codepoint: int
    end_codepoint: int
    category: str
    def __init__(self, item_id: _Optional[str] = ..., content_hash: _Optional[str] = ..., start_codepoint: _Optional[int] = ..., end_codepoint: _Optional[int] = ..., category: _Optional[str] = ...) -> None: ...

class TurnMessageMatchDTO(_message.Message):
    __slots__ = ("turn_id", "message_id", "role", "reference", "preview", "preview_start_codepoint", "tool_call_id", "tool_name")
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_START_CODEPOINT_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    turn_id: str
    message_id: str
    role: str
    reference: TurnMessageReferenceDTO
    preview: str
    preview_start_codepoint: int
    tool_call_id: str
    tool_name: str
    def __init__(self, turn_id: _Optional[str] = ..., message_id: _Optional[str] = ..., role: _Optional[str] = ..., reference: _Optional[_Union[TurnMessageReferenceDTO, _Mapping]] = ..., preview: _Optional[str] = ..., preview_start_codepoint: _Optional[int] = ..., tool_call_id: _Optional[str] = ..., tool_name: _Optional[str] = ...) -> None: ...

class TurnMessageLocationDTO(_message.Message):
    __slots__ = ("turn_id", "message_id", "role", "reference", "content", "tool_call_id", "tool_name")
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    turn_id: str
    message_id: str
    role: str
    reference: TurnMessageReferenceDTO
    content: str
    tool_call_id: str
    tool_name: str
    def __init__(self, turn_id: _Optional[str] = ..., message_id: _Optional[str] = ..., role: _Optional[str] = ..., reference: _Optional[_Union[TurnMessageReferenceDTO, _Mapping]] = ..., content: _Optional[str] = ..., tool_call_id: _Optional[str] = ..., tool_name: _Optional[str] = ...) -> None: ...

class TurnMessageSearchResultsDTO(_message.Message):
    __slots__ = ("session_id", "thread_id", "items", "cursor", "complete", "limited_reason", "scanned_items", "scanned_bytes")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    COMPLETE_FIELD_NUMBER: _ClassVar[int]
    LIMITED_REASON_FIELD_NUMBER: _ClassVar[int]
    SCANNED_ITEMS_FIELD_NUMBER: _ClassVar[int]
    SCANNED_BYTES_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    thread_id: str
    items: _containers.RepeatedCompositeFieldContainer[TurnMessageMatchDTO]
    cursor: str
    complete: bool
    limited_reason: str
    scanned_items: int
    scanned_bytes: int
    def __init__(self, session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., items: _Optional[_Iterable[_Union[TurnMessageMatchDTO, _Mapping]]] = ..., cursor: _Optional[str] = ..., complete: _Optional[bool] = ..., limited_reason: _Optional[str] = ..., scanned_items: _Optional[int] = ..., scanned_bytes: _Optional[int] = ...) -> None: ...

class WorkspaceMessageMatchDTO(_message.Message):
    __slots__ = ("thread", "match")
    THREAD_FIELD_NUMBER: _ClassVar[int]
    MATCH_FIELD_NUMBER: _ClassVar[int]
    thread: SessionThreadSearchResultDTO
    match: TurnMessageMatchDTO
    def __init__(self, thread: _Optional[_Union[SessionThreadSearchResultDTO, _Mapping]] = ..., match: _Optional[_Union[TurnMessageMatchDTO, _Mapping]] = ...) -> None: ...

class WorkspaceMessageSearchResultsDTO(_message.Message):
    __slots__ = ("catalog_revision", "items", "cursor", "complete", "limited_reason", "scanned_sessions", "scanned_threads", "scanned_items", "scanned_bytes")
    CATALOG_REVISION_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    COMPLETE_FIELD_NUMBER: _ClassVar[int]
    LIMITED_REASON_FIELD_NUMBER: _ClassVar[int]
    SCANNED_SESSIONS_FIELD_NUMBER: _ClassVar[int]
    SCANNED_THREADS_FIELD_NUMBER: _ClassVar[int]
    SCANNED_ITEMS_FIELD_NUMBER: _ClassVar[int]
    SCANNED_BYTES_FIELD_NUMBER: _ClassVar[int]
    catalog_revision: int
    items: _containers.RepeatedCompositeFieldContainer[WorkspaceMessageMatchDTO]
    cursor: str
    complete: bool
    limited_reason: str
    scanned_sessions: int
    scanned_threads: int
    scanned_items: int
    scanned_bytes: int
    def __init__(self, catalog_revision: _Optional[int] = ..., items: _Optional[_Iterable[_Union[WorkspaceMessageMatchDTO, _Mapping]]] = ..., cursor: _Optional[str] = ..., complete: _Optional[bool] = ..., limited_reason: _Optional[str] = ..., scanned_sessions: _Optional[int] = ..., scanned_threads: _Optional[int] = ..., scanned_items: _Optional[int] = ..., scanned_bytes: _Optional[int] = ...) -> None: ...

class TurnHistoryLoadRequest(_message.Message):
    __slots__ = ("direction", "cursor", "anchor_turn_id", "turn_ids", "turns", "before_turns", "after_turns", "include", "execution_id", "tool_call_ids", "anchor_message")
    DIRECTION_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    ANCHOR_TURN_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_IDS_FIELD_NUMBER: _ClassVar[int]
    TURNS_FIELD_NUMBER: _ClassVar[int]
    BEFORE_TURNS_FIELD_NUMBER: _ClassVar[int]
    AFTER_TURNS_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_IDS_FIELD_NUMBER: _ClassVar[int]
    ANCHOR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    direction: str
    cursor: str
    anchor_turn_id: str
    turn_ids: _containers.RepeatedScalarFieldContainer[str]
    turns: int
    before_turns: int
    after_turns: int
    include: _containers.RepeatedScalarFieldContainer[str]
    execution_id: str
    tool_call_ids: _containers.RepeatedScalarFieldContainer[str]
    anchor_message: TurnMessageReferenceDTO
    def __init__(self, direction: _Optional[str] = ..., cursor: _Optional[str] = ..., anchor_turn_id: _Optional[str] = ..., turn_ids: _Optional[_Iterable[str]] = ..., turns: _Optional[int] = ..., before_turns: _Optional[int] = ..., after_turns: _Optional[int] = ..., include: _Optional[_Iterable[str]] = ..., execution_id: _Optional[str] = ..., tool_call_ids: _Optional[_Iterable[str]] = ..., anchor_message: _Optional[_Union[TurnMessageReferenceDTO, _Mapping]] = ...) -> None: ...

class TurnHistoryPageDTO(_message.Message):
    __slots__ = ("items", "next_cursor", "has_more", "before_cursor", "after_cursor", "has_before", "has_after", "projection_epoch", "summaries", "internal_executions", "thread_id", "source_view_id", "source_view_revision", "located_message")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    HAS_MORE_FIELD_NUMBER: _ClassVar[int]
    BEFORE_CURSOR_FIELD_NUMBER: _ClassVar[int]
    AFTER_CURSOR_FIELD_NUMBER: _ClassVar[int]
    HAS_BEFORE_FIELD_NUMBER: _ClassVar[int]
    HAS_AFTER_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_EPOCH_FIELD_NUMBER: _ClassVar[int]
    SUMMARIES_FIELD_NUMBER: _ClassVar[int]
    INTERNAL_EXECUTIONS_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_REVISION_FIELD_NUMBER: _ClassVar[int]
    LOCATED_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[TurnDetailDTO]
    next_cursor: str
    has_more: bool
    before_cursor: str
    after_cursor: str
    has_before: bool
    has_after: bool
    projection_epoch: int
    summaries: _containers.RepeatedCompositeFieldContainer[TurnSummaryDTO]
    internal_executions: _containers.RepeatedCompositeFieldContainer[InternalExecutionHistoryDTO]
    thread_id: str
    source_view_id: str
    source_view_revision: int
    located_message: TurnMessageLocationDTO
    def __init__(self, items: _Optional[_Iterable[_Union[TurnDetailDTO, _Mapping]]] = ..., next_cursor: _Optional[str] = ..., has_more: _Optional[bool] = ..., before_cursor: _Optional[str] = ..., after_cursor: _Optional[str] = ..., has_before: _Optional[bool] = ..., has_after: _Optional[bool] = ..., projection_epoch: _Optional[int] = ..., summaries: _Optional[_Iterable[_Union[TurnSummaryDTO, _Mapping]]] = ..., internal_executions: _Optional[_Iterable[_Union[InternalExecutionHistoryDTO, _Mapping]]] = ..., thread_id: _Optional[str] = ..., source_view_id: _Optional[str] = ..., source_view_revision: _Optional[int] = ..., located_message: _Optional[_Union[TurnMessageLocationDTO, _Mapping]] = ...) -> None: ...

class InternalExecutionHistoryDTO(_message.Message):
    __slots__ = ("execution_id", "job_id", "status", "created_at", "display_parts", "response_parts", "session_id", "thread_id", "updated_at")
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_PARTS_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_PARTS_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    execution_id: str
    job_id: str
    status: str
    created_at: _timestamp_pb2.Timestamp
    display_parts: _containers.RepeatedCompositeFieldContainer[InternalExecutionDisplayPartDTO]
    response_parts: _containers.RepeatedCompositeFieldContainer[TurnResponsePartDTO]
    session_id: str
    thread_id: str
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, execution_id: _Optional[str] = ..., job_id: _Optional[str] = ..., status: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., display_parts: _Optional[_Iterable[_Union[InternalExecutionDisplayPartDTO, _Mapping]]] = ..., response_parts: _Optional[_Iterable[_Union[TurnResponsePartDTO, _Mapping]]] = ..., session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class InternalExecutionDisplayPartDTO(_message.Message):
    __slots__ = ("kind", "text", "item_id", "item_sequence", "provenance", "created_at", "logical_item_ordinal")
    KIND_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    ITEM_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    LOGICAL_ITEM_ORDINAL_FIELD_NUMBER: _ClassVar[int]
    kind: str
    text: str
    item_id: str
    item_sequence: int
    provenance: InternalDisplayProvenanceDTO
    created_at: _timestamp_pb2.Timestamp
    logical_item_ordinal: int
    def __init__(self, kind: _Optional[str] = ..., text: _Optional[str] = ..., item_id: _Optional[str] = ..., item_sequence: _Optional[int] = ..., provenance: _Optional[_Union[InternalDisplayProvenanceDTO, _Mapping]] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., logical_item_ordinal: _Optional[int] = ...) -> None: ...

class InternalDisplayProvenanceDTO(_message.Message):
    __slots__ = ("source_session_id", "source_thread_id", "delegation_id", "source_execution_id", "source_item_id")
    SOURCE_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    DELEGATION_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    source_session_id: str
    source_thread_id: str
    delegation_id: str
    source_execution_id: str
    source_item_id: str
    def __init__(self, source_session_id: _Optional[str] = ..., source_thread_id: _Optional[str] = ..., delegation_id: _Optional[str] = ..., source_execution_id: _Optional[str] = ..., source_item_id: _Optional[str] = ...) -> None: ...

class TurnJobSummaryDTO(_message.Message):
    __slots__ = ("job_id", "message_id", "status", "updated_at")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    message_id: str
    status: str
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, job_id: _Optional[str] = ..., message_id: _Optional[str] = ..., status: _Optional[str] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class TurnPageDTO(_message.Message):
    __slots__ = ("items", "next_cursor", "has_more", "projection_epoch")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    HAS_MORE_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_EPOCH_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[TurnSummaryDTO]
    next_cursor: str
    has_more: bool
    projection_epoch: int
    def __init__(self, items: _Optional[_Iterable[_Union[TurnSummaryDTO, _Mapping]]] = ..., next_cursor: _Optional[str] = ..., has_more: _Optional[bool] = ..., projection_epoch: _Optional[int] = ...) -> None: ...

class TurnProjectionCorruptedErrorDTO(_message.Message):
    __slots__ = ("code", "session_id", "message")
    CODE_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: str
    session_id: str
    message: str
    def __init__(self, code: _Optional[str] = ..., session_id: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class TurnResponsePartDTO(_message.Message):
    __slots__ = ("part_id", "kind", "projection", "status", "source", "text", "carrier_type", "tool_call_id", "tool_name", "arguments", "result", "truncated", "final", "outcome_unknown", "completion_reason", "partial")
    PART_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    PROJECTION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    CARRIER_TYPE_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    FINAL_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_UNKNOWN_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_REASON_FIELD_NUMBER: _ClassVar[int]
    PARTIAL_FIELD_NUMBER: _ClassVar[int]
    part_id: str
    kind: str
    projection: str
    status: str
    source: TurnResponseSourceDTO
    text: str
    carrier_type: str
    tool_call_id: str
    tool_name: str
    arguments: str
    result: str
    truncated: bool
    final: bool
    outcome_unknown: bool
    completion_reason: str
    partial: bool
    def __init__(self, part_id: _Optional[str] = ..., kind: _Optional[str] = ..., projection: _Optional[str] = ..., status: _Optional[str] = ..., source: _Optional[_Union[TurnResponseSourceDTO, _Mapping]] = ..., text: _Optional[str] = ..., carrier_type: _Optional[str] = ..., tool_call_id: _Optional[str] = ..., tool_name: _Optional[str] = ..., arguments: _Optional[str] = ..., result: _Optional[str] = ..., truncated: _Optional[bool] = ..., final: _Optional[bool] = ..., outcome_unknown: _Optional[bool] = ..., completion_reason: _Optional[str] = ..., partial: _Optional[bool] = ...) -> None: ...

class TurnResponseSourceDTO(_message.Message):
    __slots__ = ("message_sequence", "assistant_message_sequence", "content_block_index", "item_index", "call_index", "result_message_sequence", "item_id", "item_sequence", "part_ordinal", "created_at", "elapsed_ms", "logical_item_ordinal")
    MESSAGE_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    ASSISTANT_MESSAGE_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_BLOCK_INDEX_FIELD_NUMBER: _ClassVar[int]
    ITEM_INDEX_FIELD_NUMBER: _ClassVar[int]
    CALL_INDEX_FIELD_NUMBER: _ClassVar[int]
    RESULT_MESSAGE_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    ITEM_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    PART_ORDINAL_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_MS_FIELD_NUMBER: _ClassVar[int]
    LOGICAL_ITEM_ORDINAL_FIELD_NUMBER: _ClassVar[int]
    message_sequence: int
    assistant_message_sequence: int
    content_block_index: int
    item_index: int
    call_index: int
    result_message_sequence: int
    item_id: str
    item_sequence: int
    part_ordinal: int
    created_at: _timestamp_pb2.Timestamp
    elapsed_ms: int
    logical_item_ordinal: int
    def __init__(self, message_sequence: _Optional[int] = ..., assistant_message_sequence: _Optional[int] = ..., content_block_index: _Optional[int] = ..., item_index: _Optional[int] = ..., call_index: _Optional[int] = ..., result_message_sequence: _Optional[int] = ..., item_id: _Optional[str] = ..., item_sequence: _Optional[int] = ..., part_ordinal: _Optional[int] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., elapsed_ms: _Optional[int] = ..., logical_item_ordinal: _Optional[int] = ...) -> None: ...

class TurnSummaryDTO(_message.Message):
    __slots__ = ("turn_id", "job_id", "session_id", "ordinal", "revision", "status", "created_at", "updated_at", "completed_at", "items_view", "source_message_ids", "source_message_count", "merged_job_ids", "merged_job_count", "sources_truncated", "user_messages", "user_message_count", "user_messages_truncated", "response_preview", "preview_truncated", "item_count", "thinking_blocks", "tool_summary", "response_parts", "activity_stats", "root_logical_item_ordinal", "source_view_id", "source_view_revision", "tool_summary_truncated")
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ORDINAL_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    COMPLETED_AT_FIELD_NUMBER: _ClassVar[int]
    ITEMS_VIEW_FIELD_NUMBER: _ClassVar[int]
    SOURCE_MESSAGE_IDS_FIELD_NUMBER: _ClassVar[int]
    SOURCE_MESSAGE_COUNT_FIELD_NUMBER: _ClassVar[int]
    MERGED_JOB_IDS_FIELD_NUMBER: _ClassVar[int]
    MERGED_JOB_COUNT_FIELD_NUMBER: _ClassVar[int]
    SOURCES_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    USER_MESSAGES_FIELD_NUMBER: _ClassVar[int]
    USER_MESSAGE_COUNT_FIELD_NUMBER: _ClassVar[int]
    USER_MESSAGES_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_PREVIEW_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    ITEM_COUNT_FIELD_NUMBER: _ClassVar[int]
    THINKING_BLOCKS_FIELD_NUMBER: _ClassVar[int]
    TOOL_SUMMARY_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_PARTS_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_STATS_FIELD_NUMBER: _ClassVar[int]
    ROOT_LOGICAL_ITEM_ORDINAL_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_VIEW_REVISION_FIELD_NUMBER: _ClassVar[int]
    TOOL_SUMMARY_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    turn_id: str
    job_id: str
    session_id: str
    ordinal: int
    revision: int
    status: str
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    completed_at: _timestamp_pb2.Timestamp
    items_view: str
    source_message_ids: _containers.RepeatedScalarFieldContainer[str]
    source_message_count: int
    merged_job_ids: _containers.RepeatedScalarFieldContainer[str]
    merged_job_count: int
    sources_truncated: bool
    user_messages: _containers.RepeatedCompositeFieldContainer[TurnUserMessageSummaryDTO]
    user_message_count: int
    user_messages_truncated: bool
    response_preview: str
    preview_truncated: bool
    item_count: int
    thinking_blocks: _containers.RepeatedCompositeFieldContainer[TurnThinkingBlockDTO]
    tool_summary: _containers.RepeatedCompositeFieldContainer[TurnToolSummaryDTO]
    response_parts: _containers.RepeatedCompositeFieldContainer[TurnResponsePartDTO]
    activity_stats: TurnActivityStatsDTO
    root_logical_item_ordinal: int
    source_view_id: str
    source_view_revision: int
    tool_summary_truncated: bool
    def __init__(self, turn_id: _Optional[str] = ..., job_id: _Optional[str] = ..., session_id: _Optional[str] = ..., ordinal: _Optional[int] = ..., revision: _Optional[int] = ..., status: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., completed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., items_view: _Optional[str] = ..., source_message_ids: _Optional[_Iterable[str]] = ..., source_message_count: _Optional[int] = ..., merged_job_ids: _Optional[_Iterable[str]] = ..., merged_job_count: _Optional[int] = ..., sources_truncated: _Optional[bool] = ..., user_messages: _Optional[_Iterable[_Union[TurnUserMessageSummaryDTO, _Mapping]]] = ..., user_message_count: _Optional[int] = ..., user_messages_truncated: _Optional[bool] = ..., response_preview: _Optional[str] = ..., preview_truncated: _Optional[bool] = ..., item_count: _Optional[int] = ..., thinking_blocks: _Optional[_Iterable[_Union[TurnThinkingBlockDTO, _Mapping]]] = ..., tool_summary: _Optional[_Iterable[_Union[TurnToolSummaryDTO, _Mapping]]] = ..., response_parts: _Optional[_Iterable[_Union[TurnResponsePartDTO, _Mapping]]] = ..., activity_stats: _Optional[_Union[TurnActivityStatsDTO, _Mapping]] = ..., root_logical_item_ordinal: _Optional[int] = ..., source_view_id: _Optional[str] = ..., source_view_revision: _Optional[int] = ..., tool_summary_truncated: _Optional[bool] = ...) -> None: ...

class TurnThinkingBlockDTO(_message.Message):
    __slots__ = ("kind", "text")
    KIND_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    kind: str
    text: str
    def __init__(self, kind: _Optional[str] = ..., text: _Optional[str] = ...) -> None: ...

class TurnToolSummaryDTO(_message.Message):
    __slots__ = ("tool_name", "status", "tool_call_id")
    TOOL_NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    tool_name: str
    status: str
    tool_call_id: str
    def __init__(self, tool_name: _Optional[str] = ..., status: _Optional[str] = ..., tool_call_id: _Optional[str] = ...) -> None: ...

class TurnUserMessageDTO(_message.Message):
    __slots__ = ("message_id", "content", "content_truncated", "attachments", "metadata", "created_at")
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    ATTACHMENTS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    message_id: str
    content: str
    content_truncated: bool
    attachments: _containers.RepeatedCompositeFieldContainer[TurnAttachmentDTO]
    metadata: _struct_pb2.Struct
    created_at: _timestamp_pb2.Timestamp
    def __init__(self, message_id: _Optional[str] = ..., content: _Optional[str] = ..., content_truncated: _Optional[bool] = ..., attachments: _Optional[_Iterable[_Union[TurnAttachmentDTO, _Mapping]]] = ..., metadata: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class TurnUserMessageSummaryDTO(_message.Message):
    __slots__ = ("message_id", "preview", "content_truncated", "attachment_count", "created_at")
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    PREVIEW_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    ATTACHMENT_COUNT_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    message_id: str
    preview: str
    content_truncated: bool
    attachment_count: int
    created_at: _timestamp_pb2.Timestamp
    def __init__(self, message_id: _Optional[str] = ..., preview: _Optional[str] = ..., content_truncated: _Optional[bool] = ..., attachment_count: _Optional[int] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class UiSnapshotResultDTO(_message.Message):
    __slots__ = ("status", "snapshot_id", "html_url", "metadata_url")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    HTML_URL_FIELD_NUMBER: _ClassVar[int]
    METADATA_URL_FIELD_NUMBER: _ClassVar[int]
    status: str
    snapshot_id: str
    html_url: str
    metadata_url: str
    def __init__(self, status: _Optional[str] = ..., snapshot_id: _Optional[str] = ..., html_url: _Optional[str] = ..., metadata_url: _Optional[str] = ...) -> None: ...

class WorkspaceContextDTO(_message.Message):
    __slots__ = ("workspace_id", "project_type", "languages", "git", "index_status", "config")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECT_TYPE_FIELD_NUMBER: _ClassVar[int]
    LANGUAGES_FIELD_NUMBER: _ClassVar[int]
    GIT_FIELD_NUMBER: _ClassVar[int]
    INDEX_STATUS_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    project_type: str
    languages: _containers.RepeatedScalarFieldContainer[str]
    git: _struct_pb2.Struct
    index_status: _struct_pb2.Struct
    config: _struct_pb2.Struct
    def __init__(self, workspace_id: _Optional[str] = ..., project_type: _Optional[str] = ..., languages: _Optional[_Iterable[str]] = ..., git: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., index_status: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class WorkspaceDTO(_message.Message):
    __slots__ = ("workspace_id", "name", "project_type", "git", "runtime")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    PROJECT_TYPE_FIELD_NUMBER: _ClassVar[int]
    GIT_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    name: str
    project_type: str
    git: _struct_pb2.Struct
    runtime: _struct_pb2.Struct
    def __init__(self, workspace_id: _Optional[str] = ..., name: _Optional[str] = ..., project_type: _Optional[str] = ..., git: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., runtime: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class WorkspaceDefaultAgentUpdateRequest(_message.Message):
    __slots__ = ("agent_id",)
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    def __init__(self, agent_id: _Optional[str] = ...) -> None: ...

class WorkspaceDefaultProviderUpdateRequest(_message.Message):
    __slots__ = ("provider_id",)
    PROVIDER_ID_FIELD_NUMBER: _ClassVar[int]
    provider_id: str
    def __init__(self, provider_id: _Optional[str] = ...) -> None: ...

class WorkspaceFileChangeBatchDTO(_message.Message):
    __slots__ = ("changes", "overflow", "directories", "event_id", "invalidated_roots", "snapshots")
    CHANGES_FIELD_NUMBER: _ClassVar[int]
    OVERFLOW_FIELD_NUMBER: _ClassVar[int]
    DIRECTORIES_FIELD_NUMBER: _ClassVar[int]
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    INVALIDATED_ROOTS_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOTS_FIELD_NUMBER: _ClassVar[int]
    changes: _containers.RepeatedCompositeFieldContainer[WorkspaceFileChangeDTO]
    overflow: bool
    directories: _containers.RepeatedCompositeFieldContainer[WorkspaceDirectoryRevisionDTO]
    event_id: int
    invalidated_roots: _containers.RepeatedScalarFieldContainer[str]
    snapshots: _containers.RepeatedCompositeFieldContainer[WorkspaceFileListDTO]
    def __init__(self, changes: _Optional[_Iterable[_Union[WorkspaceFileChangeDTO, _Mapping]]] = ..., overflow: _Optional[bool] = ..., directories: _Optional[_Iterable[_Union[WorkspaceDirectoryRevisionDTO, _Mapping]]] = ..., event_id: _Optional[int] = ..., invalidated_roots: _Optional[_Iterable[str]] = ..., snapshots: _Optional[_Iterable[_Union[WorkspaceFileListDTO, _Mapping]]] = ...) -> None: ...

class WorkspaceFileChangeDTO(_message.Message):
    __slots__ = ("kind", "path")
    KIND_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    kind: str
    path: str
    def __init__(self, kind: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class WorkspaceFileContentDTO(_message.Message):
    __slots__ = ("root_path", "path", "name", "content", "language", "size", "modified_at", "revision")
    ROOT_PATH_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    MODIFIED_AT_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    root_path: str
    path: str
    name: str
    content: str
    language: str
    size: int
    modified_at: str
    revision: str
    def __init__(self, root_path: _Optional[str] = ..., path: _Optional[str] = ..., name: _Optional[str] = ..., content: _Optional[str] = ..., language: _Optional[str] = ..., size: _Optional[int] = ..., modified_at: _Optional[str] = ..., revision: _Optional[str] = ...) -> None: ...

class WorkspaceFileRootsDTO(_message.Message):
    __slots__ = ("workspace_uri", "filesystem_uri", "filesystem_label")
    WORKSPACE_URI_FIELD_NUMBER: _ClassVar[int]
    FILESYSTEM_URI_FIELD_NUMBER: _ClassVar[int]
    FILESYSTEM_LABEL_FIELD_NUMBER: _ClassVar[int]
    workspace_uri: str
    filesystem_uri: str
    filesystem_label: str
    def __init__(self, workspace_uri: _Optional[str] = ..., filesystem_uri: _Optional[str] = ..., filesystem_label: _Optional[str] = ...) -> None: ...

class WorkspaceFileReferencesRequest(_message.Message):
    __slots__ = ("paths",)
    PATHS_FIELD_NUMBER: _ClassVar[int]
    paths: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, paths: _Optional[_Iterable[str]] = ...) -> None: ...

class WorkspaceFileReferencesDTO(_message.Message):
    __slots__ = ("uris",)
    URIS_FIELD_NUMBER: _ClassVar[int]
    uris: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, uris: _Optional[_Iterable[str]] = ...) -> None: ...

class WorkspaceFileCopyRequest(_message.Message):
    __slots__ = ("source_path", "source_scope", "intent")
    SOURCE_PATH_FIELD_NUMBER: _ClassVar[int]
    SOURCE_SCOPE_FIELD_NUMBER: _ClassVar[int]
    INTENT_FIELD_NUMBER: _ClassVar[int]
    source_path: str
    source_scope: str
    intent: WorkspaceFileMutationIntentDTO
    def __init__(self, source_path: _Optional[str] = ..., source_scope: _Optional[str] = ..., intent: _Optional[_Union[WorkspaceFileMutationIntentDTO, _Mapping]] = ...) -> None: ...

class WorkspaceFileCreateRequest(_message.Message):
    __slots__ = ("name", "kind", "intent")
    NAME_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    INTENT_FIELD_NUMBER: _ClassVar[int]
    name: str
    kind: str
    intent: WorkspaceFileMutationIntentDTO
    def __init__(self, name: _Optional[str] = ..., kind: _Optional[str] = ..., intent: _Optional[_Union[WorkspaceFileMutationIntentDTO, _Mapping]] = ...) -> None: ...

class WorkspaceFileListDTO(_message.Message):
    __slots__ = ("root_path", "path", "items", "truncated", "limit", "next_cursor", "directory_key", "directory_revision")
    ROOT_PATH_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    NEXT_CURSOR_FIELD_NUMBER: _ClassVar[int]
    DIRECTORY_KEY_FIELD_NUMBER: _ClassVar[int]
    DIRECTORY_REVISION_FIELD_NUMBER: _ClassVar[int]
    root_path: str
    path: str
    items: _containers.RepeatedCompositeFieldContainer[WorkspaceFileNodeDTO]
    truncated: bool
    limit: int
    next_cursor: str
    directory_key: WorkspaceDirectoryKeyDTO
    directory_revision: int
    def __init__(self, root_path: _Optional[str] = ..., path: _Optional[str] = ..., items: _Optional[_Iterable[_Union[WorkspaceFileNodeDTO, _Mapping]]] = ..., truncated: _Optional[bool] = ..., limit: _Optional[int] = ..., next_cursor: _Optional[str] = ..., directory_key: _Optional[_Union[WorkspaceDirectoryKeyDTO, _Mapping]] = ..., directory_revision: _Optional[int] = ...) -> None: ...

class WorkspaceFileNodeDTO(_message.Message):
    __slots__ = ("name", "path", "kind", "has_children", "size", "modified_at")
    NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    HAS_CHILDREN_FIELD_NUMBER: _ClassVar[int]
    SIZE_FIELD_NUMBER: _ClassVar[int]
    MODIFIED_AT_FIELD_NUMBER: _ClassVar[int]
    name: str
    path: str
    kind: str
    has_children: bool
    size: int
    modified_at: str
    def __init__(self, name: _Optional[str] = ..., path: _Optional[str] = ..., kind: _Optional[str] = ..., has_children: _Optional[bool] = ..., size: _Optional[int] = ..., modified_at: _Optional[str] = ...) -> None: ...

class WorkspaceFilePasteRequest(_message.Message):
    __slots__ = ("source_paths", "intent")
    SOURCE_PATHS_FIELD_NUMBER: _ClassVar[int]
    INTENT_FIELD_NUMBER: _ClassVar[int]
    source_paths: _containers.RepeatedScalarFieldContainer[str]
    intent: WorkspaceFileMutationIntentDTO
    def __init__(self, source_paths: _Optional[_Iterable[str]] = ..., intent: _Optional[_Union[WorkspaceFileMutationIntentDTO, _Mapping]] = ...) -> None: ...

class WorkspaceDirectoryKeyDTO(_message.Message):
    __slots__ = ("workspace_id", "scope", "root", "path")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    ROOT_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    scope: str
    root: str
    path: str
    def __init__(self, workspace_id: _Optional[str] = ..., scope: _Optional[str] = ..., root: _Optional[str] = ..., path: _Optional[str] = ...) -> None: ...

class WorkspaceDirectoryRevisionDTO(_message.Message):
    __slots__ = ("directory_key", "directory_revision", "exists")
    DIRECTORY_KEY_FIELD_NUMBER: _ClassVar[int]
    DIRECTORY_REVISION_FIELD_NUMBER: _ClassVar[int]
    EXISTS_FIELD_NUMBER: _ClassVar[int]
    directory_key: WorkspaceDirectoryKeyDTO
    directory_revision: int
    exists: bool
    def __init__(self, directory_key: _Optional[_Union[WorkspaceDirectoryKeyDTO, _Mapping]] = ..., directory_revision: _Optional[int] = ..., exists: _Optional[bool] = ...) -> None: ...

class WorkspaceFileMutationIntentDTO(_message.Message):
    __slots__ = ("client_operation_id", "client_sequence", "expected_directory_revision", "depends_on", "created_by_operation_id", "relative_child_path", "directory_revision_from_operation_id", "client_ref", "directory_key")
    CLIENT_OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_DIRECTORY_REVISION_FIELD_NUMBER: _ClassVar[int]
    DEPENDS_ON_FIELD_NUMBER: _ClassVar[int]
    CREATED_BY_OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    RELATIVE_CHILD_PATH_FIELD_NUMBER: _ClassVar[int]
    DIRECTORY_REVISION_FROM_OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_REF_FIELD_NUMBER: _ClassVar[int]
    DIRECTORY_KEY_FIELD_NUMBER: _ClassVar[int]
    client_operation_id: str
    client_sequence: int
    expected_directory_revision: int
    depends_on: _containers.RepeatedScalarFieldContainer[str]
    created_by_operation_id: str
    relative_child_path: str
    directory_revision_from_operation_id: str
    client_ref: str
    directory_key: WorkspaceDirectoryKeyDTO
    def __init__(self, client_operation_id: _Optional[str] = ..., client_sequence: _Optional[int] = ..., expected_directory_revision: _Optional[int] = ..., depends_on: _Optional[_Iterable[str]] = ..., created_by_operation_id: _Optional[str] = ..., relative_child_path: _Optional[str] = ..., directory_revision_from_operation_id: _Optional[str] = ..., client_ref: _Optional[str] = ..., directory_key: _Optional[_Union[WorkspaceDirectoryKeyDTO, _Mapping]] = ...) -> None: ...

class WorkspaceFileOperationItemDTO(_message.Message):
    __slots__ = ("path", "kind", "state", "error")
    PATH_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    path: str
    kind: str
    state: str
    error: str
    def __init__(self, path: _Optional[str] = ..., kind: _Optional[str] = ..., state: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class WorkspaceFileOperationReceiptDTO(_message.Message):
    __slots__ = ("operation_id", "client_sequence", "queue_seq", "kind", "state", "receipt_revision", "effect", "items", "directories", "created_path", "error_code", "error_detail", "snapshots")
    OPERATION_ID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    QUEUE_SEQ_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    RECEIPT_REVISION_FIELD_NUMBER: _ClassVar[int]
    EFFECT_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    DIRECTORIES_FIELD_NUMBER: _ClassVar[int]
    CREATED_PATH_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_DETAIL_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOTS_FIELD_NUMBER: _ClassVar[int]
    operation_id: str
    client_sequence: int
    queue_seq: int
    kind: str
    state: str
    receipt_revision: int
    effect: str
    items: _containers.RepeatedCompositeFieldContainer[WorkspaceFileOperationItemDTO]
    directories: _containers.RepeatedCompositeFieldContainer[WorkspaceDirectoryRevisionDTO]
    created_path: str
    error_code: str
    error_detail: str
    snapshots: _containers.RepeatedCompositeFieldContainer[WorkspaceFileListDTO]
    def __init__(self, operation_id: _Optional[str] = ..., client_sequence: _Optional[int] = ..., queue_seq: _Optional[int] = ..., kind: _Optional[str] = ..., state: _Optional[str] = ..., receipt_revision: _Optional[int] = ..., effect: _Optional[str] = ..., items: _Optional[_Iterable[_Union[WorkspaceFileOperationItemDTO, _Mapping]]] = ..., directories: _Optional[_Iterable[_Union[WorkspaceDirectoryRevisionDTO, _Mapping]]] = ..., created_path: _Optional[str] = ..., error_code: _Optional[str] = ..., error_detail: _Optional[str] = ..., snapshots: _Optional[_Iterable[_Union[WorkspaceFileListDTO, _Mapping]]] = ...) -> None: ...

class WorkspaceFileOperationStatusDTO(_message.Message):
    __slots__ = ("operations", "worker_state", "worker_error")
    OPERATIONS_FIELD_NUMBER: _ClassVar[int]
    WORKER_STATE_FIELD_NUMBER: _ClassVar[int]
    WORKER_ERROR_FIELD_NUMBER: _ClassVar[int]
    operations: _containers.RepeatedCompositeFieldContainer[WorkspaceFileOperationReceiptDTO]
    worker_state: str
    worker_error: str
    def __init__(self, operations: _Optional[_Iterable[_Union[WorkspaceFileOperationReceiptDTO, _Mapping]]] = ..., worker_state: _Optional[str] = ..., worker_error: _Optional[str] = ...) -> None: ...

class WorkspaceFileRevealDTO(_message.Message):
    __slots__ = ("path",)
    PATH_FIELD_NUMBER: _ClassVar[int]
    path: str
    def __init__(self, path: _Optional[str] = ...) -> None: ...

class WorkspaceFileUpdateRequest(_message.Message):
    __slots__ = ("content", "expected_revision")
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_REVISION_FIELD_NUMBER: _ClassVar[int]
    content: str
    expected_revision: str
    def __init__(self, content: _Optional[str] = ..., expected_revision: _Optional[str] = ...) -> None: ...

class WorkspaceFileWatchRequest(_message.Message):
    __slots__ = ("paths", "after_event_id")
    PATHS_FIELD_NUMBER: _ClassVar[int]
    AFTER_EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    paths: _containers.RepeatedScalarFieldContainer[str]
    after_event_id: int
    def __init__(self, paths: _Optional[_Iterable[str]] = ..., after_event_id: _Optional[int] = ...) -> None: ...

class WorkspaceObservationSubscription(_message.Message):
    __slots__ = ("subscription_id", "kind", "session_id", "activity_after", "trace_after", "paths", "files_after", "thread_id")
    SUBSCRIPTION_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTIVITY_AFTER_FIELD_NUMBER: _ClassVar[int]
    TRACE_AFTER_FIELD_NUMBER: _ClassVar[int]
    PATHS_FIELD_NUMBER: _ClassVar[int]
    FILES_AFTER_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    subscription_id: str
    kind: str
    session_id: str
    activity_after: int
    trace_after: str
    paths: _containers.RepeatedScalarFieldContainer[str]
    files_after: int
    thread_id: str
    def __init__(self, subscription_id: _Optional[str] = ..., kind: _Optional[str] = ..., session_id: _Optional[str] = ..., activity_after: _Optional[int] = ..., trace_after: _Optional[str] = ..., paths: _Optional[_Iterable[str]] = ..., files_after: _Optional[int] = ..., thread_id: _Optional[str] = ...) -> None: ...

class WorkspaceObservationRequest(_message.Message):
    __slots__ = ("subscriptions",)
    SUBSCRIPTIONS_FIELD_NUMBER: _ClassVar[int]
    subscriptions: _containers.RepeatedCompositeFieldContainer[WorkspaceObservationSubscription]
    def __init__(self, subscriptions: _Optional[_Iterable[_Union[WorkspaceObservationSubscription, _Mapping]]] = ...) -> None: ...

class WorkspaceObservationFrameDTO(_message.Message):
    __slots__ = ("subscription_id", "state", "frame", "status", "detail_json", "cursor")
    SUBSCRIPTION_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    FRAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DETAIL_JSON_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    subscription_id: str
    state: str
    frame: str
    status: int
    detail_json: str
    cursor: str
    def __init__(self, subscription_id: _Optional[str] = ..., state: _Optional[str] = ..., frame: _Optional[str] = ..., status: _Optional[int] = ..., detail_json: _Optional[str] = ..., cursor: _Optional[str] = ...) -> None: ...

class WorkspaceIndexRebuildDTO(_message.Message):
    __slots__ = ("status", "job_id")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    status: str
    job_id: str
    def __init__(self, status: _Optional[str] = ..., job_id: _Optional[str] = ...) -> None: ...

class WorkspaceIndexStatusDTO(_message.Message):
    __slots__ = ("status", "indexed_files", "last_updated")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    INDEXED_FILES_FIELD_NUMBER: _ClassVar[int]
    LAST_UPDATED_FIELD_NUMBER: _ClassVar[int]
    status: str
    indexed_files: int
    last_updated: str
    def __init__(self, status: _Optional[str] = ..., indexed_files: _Optional[int] = ..., last_updated: _Optional[str] = ...) -> None: ...

class TextMutationGrantTarget(_message.Message):
    __slots__ = ("gateway_id", "uri", "mutation_kind")
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    MUTATION_KIND_FIELD_NUMBER: _ClassVar[int]
    gateway_id: str
    uri: str
    mutation_kind: str
    def __init__(self, gateway_id: _Optional[str] = ..., uri: _Optional[str] = ..., mutation_kind: _Optional[str] = ...) -> None: ...

class CreateText(_message.Message):
    __slots__ = ("target", "content", "expected_version")
    TARGET_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    target: str
    content: str
    expected_version: str
    def __init__(self, target: _Optional[str] = ..., content: _Optional[str] = ..., expected_version: _Optional[str] = ...) -> None: ...

class EditText(_message.Message):
    __slots__ = ("target", "old_string", "new_string", "expected_version", "replace_all")
    TARGET_FIELD_NUMBER: _ClassVar[int]
    OLD_STRING_FIELD_NUMBER: _ClassVar[int]
    NEW_STRING_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    REPLACE_ALL_FIELD_NUMBER: _ClassVar[int]
    target: str
    old_string: str
    new_string: str
    expected_version: str
    replace_all: bool
    def __init__(self, target: _Optional[str] = ..., old_string: _Optional[str] = ..., new_string: _Optional[str] = ..., expected_version: _Optional[str] = ..., replace_all: _Optional[bool] = ...) -> None: ...

class PatchText(_message.Message):
    __slots__ = ("target", "input", "expected_versions", "explanation")
    class ExpectedVersionsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    TARGET_FIELD_NUMBER: _ClassVar[int]
    INPUT_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSIONS_FIELD_NUMBER: _ClassVar[int]
    EXPLANATION_FIELD_NUMBER: _ClassVar[int]
    target: str
    input: str
    expected_versions: _containers.ScalarMap[str, str]
    explanation: str
    def __init__(self, target: _Optional[str] = ..., input: _Optional[str] = ..., expected_versions: _Optional[_Mapping[str, str]] = ..., explanation: _Optional[str] = ...) -> None: ...

class TextMutationRequest(_message.Message):
    __slots__ = ("create", "edit", "patch", "timeout_ms", "max_response_bytes")
    CREATE_FIELD_NUMBER: _ClassVar[int]
    EDIT_FIELD_NUMBER: _ClassVar[int]
    PATCH_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    MAX_RESPONSE_BYTES_FIELD_NUMBER: _ClassVar[int]
    create: CreateText
    edit: EditText
    patch: PatchText
    timeout_ms: int
    max_response_bytes: int
    def __init__(self, create: _Optional[_Union[CreateText, _Mapping]] = ..., edit: _Optional[_Union[EditText, _Mapping]] = ..., patch: _Optional[_Union[PatchText, _Mapping]] = ..., timeout_ms: _Optional[int] = ..., max_response_bytes: _Optional[int] = ...) -> None: ...

class TextMutationRelayRequest(_message.Message):
    __slots__ = ("source_gateway_id", "source_workspace_id", "mutation_target", "mutation")
    SOURCE_GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    MUTATION_TARGET_FIELD_NUMBER: _ClassVar[int]
    MUTATION_FIELD_NUMBER: _ClassVar[int]
    source_gateway_id: str
    source_workspace_id: str
    mutation_target: TextMutationGrantTarget
    mutation: TextMutationRequest
    def __init__(self, source_gateway_id: _Optional[str] = ..., source_workspace_id: _Optional[str] = ..., mutation_target: _Optional[_Union[TextMutationGrantTarget, _Mapping]] = ..., mutation: _Optional[_Union[TextMutationRequest, _Mapping]] = ...) -> None: ...

class TextMutationFileDTO(_message.Message):
    __slots__ = ("path", "operation", "state", "expected_version", "intended_version", "exists", "content_hash", "content_observation_error")
    PATH_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    INTENDED_VERSION_FIELD_NUMBER: _ClassVar[int]
    EXISTS_FIELD_NUMBER: _ClassVar[int]
    CONTENT_HASH_FIELD_NUMBER: _ClassVar[int]
    CONTENT_OBSERVATION_ERROR_FIELD_NUMBER: _ClassVar[int]
    path: str
    operation: str
    state: str
    expected_version: str
    intended_version: str
    exists: bool
    content_hash: str
    content_observation_error: str
    def __init__(self, path: _Optional[str] = ..., operation: _Optional[str] = ..., state: _Optional[str] = ..., expected_version: _Optional[str] = ..., intended_version: _Optional[str] = ..., exists: _Optional[bool] = ..., content_hash: _Optional[str] = ..., content_observation_error: _Optional[str] = ...) -> None: ...

class TextMutationReceiptDTO(_message.Message):
    __slots__ = ("status", "effect", "target", "journal_id", "files", "retryable", "explanation", "fuzz", "owner", "code", "error_type", "error", "recovery", "lock_release_error", "journal_error")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    EFFECT_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    JOURNAL_ID_FIELD_NUMBER: _ClassVar[int]
    FILES_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    EXPLANATION_FIELD_NUMBER: _ClassVar[int]
    FUZZ_FIELD_NUMBER: _ClassVar[int]
    OWNER_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_TYPE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    RECOVERY_FIELD_NUMBER: _ClassVar[int]
    LOCK_RELEASE_ERROR_FIELD_NUMBER: _ClassVar[int]
    JOURNAL_ERROR_FIELD_NUMBER: _ClassVar[int]
    status: str
    effect: str
    target: str
    journal_id: str
    files: _containers.RepeatedCompositeFieldContainer[TextMutationFileDTO]
    retryable: bool
    explanation: str
    fuzz: int
    owner: str
    code: str
    error_type: str
    error: str
    recovery: str
    lock_release_error: str
    journal_error: str
    def __init__(self, status: _Optional[str] = ..., effect: _Optional[str] = ..., target: _Optional[str] = ..., journal_id: _Optional[str] = ..., files: _Optional[_Iterable[_Union[TextMutationFileDTO, _Mapping]]] = ..., retryable: _Optional[bool] = ..., explanation: _Optional[str] = ..., fuzz: _Optional[int] = ..., owner: _Optional[str] = ..., code: _Optional[str] = ..., error_type: _Optional[str] = ..., error: _Optional[str] = ..., recovery: _Optional[str] = ..., lock_release_error: _Optional[str] = ..., journal_error: _Optional[str] = ...) -> None: ...

class TextMutationSnapshotDTO(_message.Message):
    __slots__ = ("uri", "before_exists", "before_content", "after_exists", "after_content", "after_error")
    URI_FIELD_NUMBER: _ClassVar[int]
    BEFORE_EXISTS_FIELD_NUMBER: _ClassVar[int]
    BEFORE_CONTENT_FIELD_NUMBER: _ClassVar[int]
    AFTER_EXISTS_FIELD_NUMBER: _ClassVar[int]
    AFTER_CONTENT_FIELD_NUMBER: _ClassVar[int]
    AFTER_ERROR_FIELD_NUMBER: _ClassVar[int]
    uri: str
    before_exists: bool
    before_content: str
    after_exists: bool
    after_content: str
    after_error: str
    def __init__(self, uri: _Optional[str] = ..., before_exists: _Optional[bool] = ..., before_content: _Optional[str] = ..., after_exists: _Optional[bool] = ..., after_content: _Optional[str] = ..., after_error: _Optional[str] = ...) -> None: ...

class TextMutationTransferDTO(_message.Message):
    __slots__ = ("receipt", "snapshots")
    RECEIPT_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOTS_FIELD_NUMBER: _ClassVar[int]
    receipt: TextMutationReceiptDTO
    snapshots: _containers.RepeatedCompositeFieldContainer[TextMutationSnapshotDTO]
    def __init__(self, receipt: _Optional[_Union[TextMutationReceiptDTO, _Mapping]] = ..., snapshots: _Optional[_Iterable[_Union[TextMutationSnapshotDTO, _Mapping]]] = ...) -> None: ...

class TaskTriggerDTO(_message.Message):
    __slots__ = ("version", "type_id", "interval_seconds", "expression", "timezone", "source_uri")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    TYPE_ID_FIELD_NUMBER: _ClassVar[int]
    INTERVAL_SECONDS_FIELD_NUMBER: _ClassVar[int]
    EXPRESSION_FIELD_NUMBER: _ClassVar[int]
    TIMEZONE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_URI_FIELD_NUMBER: _ClassVar[int]
    version: str
    type_id: str
    interval_seconds: int
    expression: str
    timezone: str
    source_uri: str
    def __init__(self, version: _Optional[str] = ..., type_id: _Optional[str] = ..., interval_seconds: _Optional[int] = ..., expression: _Optional[str] = ..., timezone: _Optional[str] = ..., source_uri: _Optional[str] = ...) -> None: ...

class TaskScriptCheckDTO(_message.Message):
    __slots__ = ("command", "timeout_seconds", "output_limit_bytes", "memory_limit_mb")
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_LIMIT_BYTES_FIELD_NUMBER: _ClassVar[int]
    MEMORY_LIMIT_MB_FIELD_NUMBER: _ClassVar[int]
    command: _containers.RepeatedScalarFieldContainer[str]
    timeout_seconds: float
    output_limit_bytes: int
    memory_limit_mb: int
    def __init__(self, command: _Optional[_Iterable[str]] = ..., timeout_seconds: _Optional[float] = ..., output_limit_bytes: _Optional[int] = ..., memory_limit_mb: _Optional[int] = ...) -> None: ...

class TaskConditionDTO(_message.Message):
    __slots__ = ("version", "type_id", "script", "rule", "model")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    TYPE_ID_FIELD_NUMBER: _ClassVar[int]
    SCRIPT_FIELD_NUMBER: _ClassVar[int]
    RULE_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    version: str
    type_id: str
    script: TaskScriptCheckDTO
    rule: TaskRuleCheckDTO
    model: TaskModelCheckDTO
    def __init__(self, version: _Optional[str] = ..., type_id: _Optional[str] = ..., script: _Optional[_Union[TaskScriptCheckDTO, _Mapping]] = ..., rule: _Optional[_Union[TaskRuleCheckDTO, _Mapping]] = ..., model: _Optional[_Union[TaskModelCheckDTO, _Mapping]] = ...) -> None: ...

class TaskModelCheckDTO(_message.Message):
    __slots__ = ("thread_uri", "instruction")
    THREAD_URI_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTION_FIELD_NUMBER: _ClassVar[int]
    thread_uri: str
    instruction: str
    def __init__(self, thread_uri: _Optional[str] = ..., instruction: _Optional[str] = ...) -> None: ...

class TaskRuleCheckDTO(_message.Message):
    __slots__ = ("pointer", "expected")
    POINTER_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_FIELD_NUMBER: _ClassVar[int]
    pointer: str
    expected: _struct_pb2.Value
    def __init__(self, pointer: _Optional[str] = ..., expected: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ...) -> None: ...

class TaskContextDTO(_message.Message):
    __slots__ = ("version", "type_id", "content", "mode")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    TYPE_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    version: str
    type_id: str
    content: str
    mode: str
    def __init__(self, version: _Optional[str] = ..., type_id: _Optional[str] = ..., content: _Optional[str] = ..., mode: _Optional[str] = ...) -> None: ...

class TaskActionDTO(_message.Message):
    __slots__ = ("version", "type_id", "target_uri", "delivery_mode", "delivery_policy", "generation")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    TYPE_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_URI_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_MODE_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_POLICY_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    version: str
    type_id: str
    target_uri: str
    delivery_mode: str
    delivery_policy: str
    generation: SessionGenerationActionConfigDTO
    def __init__(self, version: _Optional[str] = ..., type_id: _Optional[str] = ..., target_uri: _Optional[str] = ..., delivery_mode: _Optional[str] = ..., delivery_policy: _Optional[str] = ..., generation: _Optional[_Union[SessionGenerationActionConfigDTO, _Mapping]] = ...) -> None: ...

class TaskPoliciesDTO(_message.Message):
    __slots__ = ("completion", "misfire", "match_mode")
    COMPLETION_FIELD_NUMBER: _ClassVar[int]
    MISFIRE_FIELD_NUMBER: _ClassVar[int]
    MATCH_MODE_FIELD_NUMBER: _ClassVar[int]
    completion: str
    misfire: str
    match_mode: str
    def __init__(self, completion: _Optional[str] = ..., misfire: _Optional[str] = ..., match_mode: _Optional[str] = ...) -> None: ...

class PersistentTaskConfigurationDTO(_message.Message):
    __slots__ = ("name", "trigger", "condition", "context", "action", "policies")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TRIGGER_FIELD_NUMBER: _ClassVar[int]
    CONDITION_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    POLICIES_FIELD_NUMBER: _ClassVar[int]
    name: str
    trigger: TaskTriggerDTO
    condition: TaskConditionDTO
    context: TaskContextDTO
    action: TaskActionDTO
    policies: TaskPoliciesDTO
    def __init__(self, name: _Optional[str] = ..., trigger: _Optional[_Union[TaskTriggerDTO, _Mapping]] = ..., condition: _Optional[_Union[TaskConditionDTO, _Mapping]] = ..., context: _Optional[_Union[TaskContextDTO, _Mapping]] = ..., action: _Optional[_Union[TaskActionDTO, _Mapping]] = ..., policies: _Optional[_Union[TaskPoliciesDTO, _Mapping]] = ...) -> None: ...

class PersistentTaskDTO(_message.Message):
    __slots__ = ("task_id", "workspace_id", "revision", "definition_revision", "configuration", "lifecycle", "phase", "reason", "recovering", "created_at", "updated_at", "next_run_at", "application_type", "application_version", "goal")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    DEFINITION_REVISION_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_FIELD_NUMBER: _ClassVar[int]
    LIFECYCLE_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    RECOVERING_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    NEXT_RUN_AT_FIELD_NUMBER: _ClassVar[int]
    APPLICATION_TYPE_FIELD_NUMBER: _ClassVar[int]
    APPLICATION_VERSION_FIELD_NUMBER: _ClassVar[int]
    GOAL_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    workspace_id: str
    revision: int
    definition_revision: int
    configuration: PersistentTaskConfigurationDTO
    lifecycle: str
    phase: str
    reason: str
    recovering: bool
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    next_run_at: _timestamp_pb2.Timestamp
    application_type: str
    application_version: str
    goal: SessionGoalDTO
    def __init__(self, task_id: _Optional[str] = ..., workspace_id: _Optional[str] = ..., revision: _Optional[int] = ..., definition_revision: _Optional[int] = ..., configuration: _Optional[_Union[PersistentTaskConfigurationDTO, _Mapping]] = ..., lifecycle: _Optional[str] = ..., phase: _Optional[str] = ..., reason: _Optional[str] = ..., recovering: _Optional[bool] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., next_run_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., application_type: _Optional[str] = ..., application_version: _Optional[str] = ..., goal: _Optional[_Union[SessionGoalDTO, _Mapping]] = ...) -> None: ...

class TaskDecisionDTO(_message.Message):
    __slots__ = ("decision", "reason", "result")
    DECISION_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    decision: str
    reason: str
    result: _struct_pb2.Struct
    def __init__(self, decision: _Optional[str] = ..., reason: _Optional[str] = ..., result: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class TaskDeliveryDTO(_message.Message):
    __slots__ = ("state", "source_id", "item_id", "source_revision", "job_id", "execution_id")
    STATE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_REVISION_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    state: str
    source_id: str
    item_id: str
    source_revision: str
    job_id: str
    execution_id: str
    def __init__(self, state: _Optional[str] = ..., source_id: _Optional[str] = ..., item_id: _Optional[str] = ..., source_revision: _Optional[str] = ..., job_id: _Optional[str] = ..., execution_id: _Optional[str] = ...) -> None: ...

class TaskEvaluationDTO(_message.Message):
    __slots__ = ("thread_uri", "source_item_id", "job_id", "execution_id")
    THREAD_URI_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    thread_uri: str
    source_item_id: str
    job_id: str
    execution_id: str
    def __init__(self, thread_uri: _Optional[str] = ..., source_item_id: _Optional[str] = ..., job_id: _Optional[str] = ..., execution_id: _Optional[str] = ...) -> None: ...

class PersistentTaskRunDTO(_message.Message):
    __slots__ = ("run_id", "task_id", "task_revision", "definition_revision", "trigger_key", "check_only", "phase", "decision", "delivery", "error", "created_at", "updated_at", "request_id", "trigger_type", "scheduled_for", "trigger_payload", "evaluation", "generation")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_REVISION_FIELD_NUMBER: _ClassVar[int]
    DEFINITION_REVISION_FIELD_NUMBER: _ClassVar[int]
    TRIGGER_KEY_FIELD_NUMBER: _ClassVar[int]
    CHECK_ONLY_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    DECISION_FIELD_NUMBER: _ClassVar[int]
    DELIVERY_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TRIGGER_TYPE_FIELD_NUMBER: _ClassVar[int]
    SCHEDULED_FOR_FIELD_NUMBER: _ClassVar[int]
    TRIGGER_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    EVALUATION_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    task_id: str
    task_revision: int
    definition_revision: int
    trigger_key: str
    check_only: bool
    phase: str
    decision: TaskDecisionDTO
    delivery: TaskDeliveryDTO
    error: str
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    request_id: str
    trigger_type: str
    scheduled_for: _timestamp_pb2.Timestamp
    trigger_payload: _struct_pb2.Struct
    evaluation: TaskEvaluationDTO
    generation: SessionGenerationExecuteResultDTO
    def __init__(self, run_id: _Optional[str] = ..., task_id: _Optional[str] = ..., task_revision: _Optional[int] = ..., definition_revision: _Optional[int] = ..., trigger_key: _Optional[str] = ..., check_only: _Optional[bool] = ..., phase: _Optional[str] = ..., decision: _Optional[_Union[TaskDecisionDTO, _Mapping]] = ..., delivery: _Optional[_Union[TaskDeliveryDTO, _Mapping]] = ..., error: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., request_id: _Optional[str] = ..., trigger_type: _Optional[str] = ..., scheduled_for: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., trigger_payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., evaluation: _Optional[_Union[TaskEvaluationDTO, _Mapping]] = ..., generation: _Optional[_Union[SessionGenerationExecuteResultDTO, _Mapping]] = ...) -> None: ...

class TaskThreadTargetDTO(_message.Message):
    __slots__ = ("session_id", "thread_id", "uri")
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    URI_FIELD_NUMBER: _ClassVar[int]
    session_id: str
    thread_id: str
    uri: str
    def __init__(self, session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., uri: _Optional[str] = ...) -> None: ...

class TaskInputConsumptionDTO(_message.Message):
    __slots__ = ("execution_id", "model_call_id", "assembly_id", "request_hash")
    EXECUTION_ID_FIELD_NUMBER: _ClassVar[int]
    MODEL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    ASSEMBLY_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_HASH_FIELD_NUMBER: _ClassVar[int]
    execution_id: str
    model_call_id: str
    assembly_id: str
    request_hash: str
    def __init__(self, execution_id: _Optional[str] = ..., model_call_id: _Optional[str] = ..., assembly_id: _Optional[str] = ..., request_hash: _Optional[str] = ...) -> None: ...

class PersistentTaskRunDetailDTO(_message.Message):
    __slots__ = ("run", "configuration", "check_input", "work_target", "evaluation_target", "action_content", "execution_outcome", "consumption")
    RUN_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_FIELD_NUMBER: _ClassVar[int]
    CHECK_INPUT_FIELD_NUMBER: _ClassVar[int]
    WORK_TARGET_FIELD_NUMBER: _ClassVar[int]
    EVALUATION_TARGET_FIELD_NUMBER: _ClassVar[int]
    ACTION_CONTENT_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_OUTCOME_FIELD_NUMBER: _ClassVar[int]
    CONSUMPTION_FIELD_NUMBER: _ClassVar[int]
    run: PersistentTaskRunDTO
    configuration: PersistentTaskConfigurationDTO
    check_input: _struct_pb2.Struct
    work_target: TaskThreadTargetDTO
    evaluation_target: TaskThreadTargetDTO
    action_content: str
    execution_outcome: str
    consumption: TaskInputConsumptionDTO
    def __init__(self, run: _Optional[_Union[PersistentTaskRunDTO, _Mapping]] = ..., configuration: _Optional[_Union[PersistentTaskConfigurationDTO, _Mapping]] = ..., check_input: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., work_target: _Optional[_Union[TaskThreadTargetDTO, _Mapping]] = ..., evaluation_target: _Optional[_Union[TaskThreadTargetDTO, _Mapping]] = ..., action_content: _Optional[str] = ..., execution_outcome: _Optional[str] = ..., consumption: _Optional[_Union[TaskInputConsumptionDTO, _Mapping]] = ...) -> None: ...

class PersistentTaskCreateRequest(_message.Message):
    __slots__ = ("idempotency_key", "configuration", "enabled")
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_FIELD_NUMBER: _ClassVar[int]
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    idempotency_key: str
    configuration: PersistentTaskConfigurationDTO
    enabled: bool
    def __init__(self, idempotency_key: _Optional[str] = ..., configuration: _Optional[_Union[PersistentTaskConfigurationDTO, _Mapping]] = ..., enabled: _Optional[bool] = ...) -> None: ...

class PersistentTaskUpdateRequest(_message.Message):
    __slots__ = ("expected_revision", "configuration")
    EXPECTED_REVISION_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_FIELD_NUMBER: _ClassVar[int]
    expected_revision: int
    configuration: PersistentTaskConfigurationDTO
    def __init__(self, expected_revision: _Optional[int] = ..., configuration: _Optional[_Union[PersistentTaskConfigurationDTO, _Mapping]] = ...) -> None: ...

class PersistentTaskControlRequest(_message.Message):
    __slots__ = ("expected_revision", "action")
    EXPECTED_REVISION_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    expected_revision: int
    action: str
    def __init__(self, expected_revision: _Optional[int] = ..., action: _Optional[str] = ...) -> None: ...

class PersistentTaskCheckRequest(_message.Message):
    __slots__ = ("idempotency_key", "expected_revision", "check_only")
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_REVISION_FIELD_NUMBER: _ClassVar[int]
    CHECK_ONLY_FIELD_NUMBER: _ClassVar[int]
    idempotency_key: str
    expected_revision: int
    check_only: bool
    def __init__(self, idempotency_key: _Optional[str] = ..., expected_revision: _Optional[int] = ..., check_only: _Optional[bool] = ...) -> None: ...

class PersistentTaskNotificationRequest(_message.Message):
    __slots__ = ("idempotency_key", "payload")
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    idempotency_key: str
    payload: _struct_pb2.Struct
    def __init__(self, idempotency_key: _Optional[str] = ..., payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class TaskRuntimeStatusDTO(_message.Message):
    __slots__ = ("state", "recovering_tasks", "error")
    STATE_FIELD_NUMBER: _ClassVar[int]
    RECOVERING_TASKS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    state: str
    recovering_tasks: int
    error: str
    def __init__(self, state: _Optional[str] = ..., recovering_tasks: _Optional[int] = ..., error: _Optional[str] = ...) -> None: ...

class PersistentTaskListDTO(_message.Message):
    __slots__ = ("items", "runtime")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    RUNTIME_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[PersistentTaskDTO]
    runtime: TaskRuntimeStatusDTO
    def __init__(self, items: _Optional[_Iterable[_Union[PersistentTaskDTO, _Mapping]]] = ..., runtime: _Optional[_Union[TaskRuntimeStatusDTO, _Mapping]] = ...) -> None: ...

class PersistentTaskRunListDTO(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[PersistentTaskRunDTO]
    def __init__(self, items: _Optional[_Iterable[_Union[PersistentTaskRunDTO, _Mapping]]] = ...) -> None: ...

class SessionGenerationPreviewRequest(_message.Message):
    __slots__ = ("name", "title_template", "path_template", "session_title", "generated_at", "placement_kind", "placement_node_id", "session_strategy", "target_session_id")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TITLE_TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    PATH_TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    SESSION_TITLE_FIELD_NUMBER: _ClassVar[int]
    GENERATED_AT_FIELD_NUMBER: _ClassVar[int]
    PLACEMENT_KIND_FIELD_NUMBER: _ClassVar[int]
    PLACEMENT_NODE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_STRATEGY_FIELD_NUMBER: _ClassVar[int]
    TARGET_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    name: str
    title_template: str
    path_template: _containers.RepeatedScalarFieldContainer[str]
    session_title: str
    generated_at: _timestamp_pb2.Timestamp
    placement_kind: str
    placement_node_id: str
    session_strategy: str
    target_session_id: str
    def __init__(self, name: _Optional[str] = ..., title_template: _Optional[str] = ..., path_template: _Optional[_Iterable[str]] = ..., session_title: _Optional[str] = ..., generated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., placement_kind: _Optional[str] = ..., placement_node_id: _Optional[str] = ..., session_strategy: _Optional[str] = ..., target_session_id: _Optional[str] = ...) -> None: ...

class SessionGenerationPreviewDTO(_message.Message):
    __slots__ = ("workspace_id", "title", "path_segments", "navigation_path", "physical_location", "target_session_id")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    PATH_SEGMENTS_FIELD_NUMBER: _ClassVar[int]
    NAVIGATION_PATH_FIELD_NUMBER: _ClassVar[int]
    PHYSICAL_LOCATION_FIELD_NUMBER: _ClassVar[int]
    TARGET_SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    title: str
    path_segments: _containers.RepeatedScalarFieldContainer[str]
    navigation_path: _containers.RepeatedScalarFieldContainer[str]
    physical_location: str
    target_session_id: str
    def __init__(self, workspace_id: _Optional[str] = ..., title: _Optional[str] = ..., path_segments: _Optional[_Iterable[str]] = ..., navigation_path: _Optional[_Iterable[str]] = ..., physical_location: _Optional[str] = ..., target_session_id: _Optional[str] = ...) -> None: ...

class SessionGenerationGeneratorTypeDTO(_message.Message):
    __slots__ = ("type_id", "version")
    TYPE_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    type_id: str
    version: str
    def __init__(self, type_id: _Optional[str] = ..., version: _Optional[str] = ...) -> None: ...

class SessionGenerationPlacementDTO(_message.Message):
    __slots__ = ("kind", "workspace_id", "session_id", "folder_id")
    KIND_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    FOLDER_ID_FIELD_NUMBER: _ClassVar[int]
    kind: str
    workspace_id: str
    session_id: str
    folder_id: str
    def __init__(self, kind: _Optional[str] = ..., workspace_id: _Optional[str] = ..., session_id: _Optional[str] = ..., folder_id: _Optional[str] = ...) -> None: ...

class SessionGenerationContextSourceDTO(_message.Message):
    __slots__ = ("kind", "workspace_id", "session_id", "thread_id", "snapshot_id")
    KIND_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    kind: str
    workspace_id: str
    session_id: str
    thread_id: str
    snapshot_id: str
    def __init__(self, kind: _Optional[str] = ..., workspace_id: _Optional[str] = ..., session_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., snapshot_id: _Optional[str] = ...) -> None: ...

class SessionGenerationTargetDTO(_message.Message):
    __slots__ = ("workspace_id", "session_id", "thread_id")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    session_id: str
    thread_id: str
    def __init__(self, workspace_id: _Optional[str] = ..., session_id: _Optional[str] = ..., thread_id: _Optional[str] = ...) -> None: ...

class SessionGenerationStrategyDTO(_message.Message):
    __slots__ = ("mode", "target", "concurrency", "report_back")
    MODE_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    CONCURRENCY_FIELD_NUMBER: _ClassVar[int]
    REPORT_BACK_FIELD_NUMBER: _ClassVar[int]
    mode: str
    target: SessionGenerationTargetDTO
    concurrency: str
    report_back: str
    def __init__(self, mode: _Optional[str] = ..., target: _Optional[_Union[SessionGenerationTargetDTO, _Mapping]] = ..., concurrency: _Optional[str] = ..., report_back: _Optional[str] = ...) -> None: ...

class SessionGenerationValidateRequest(_message.Message):
    __slots__ = ("generator_type", "name", "config", "placement", "context_source", "session_strategy", "execution_workspace_id", "title_template", "path_template")
    GENERATOR_TYPE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    PLACEMENT_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_SOURCE_FIELD_NUMBER: _ClassVar[int]
    SESSION_STRATEGY_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    PATH_TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    generator_type: SessionGenerationGeneratorTypeDTO
    name: str
    config: _struct_pb2.Struct
    placement: SessionGenerationPlacementDTO
    context_source: SessionGenerationContextSourceDTO
    session_strategy: SessionGenerationStrategyDTO
    execution_workspace_id: str
    title_template: str
    path_template: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, generator_type: _Optional[_Union[SessionGenerationGeneratorTypeDTO, _Mapping]] = ..., name: _Optional[str] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., placement: _Optional[_Union[SessionGenerationPlacementDTO, _Mapping]] = ..., context_source: _Optional[_Union[SessionGenerationContextSourceDTO, _Mapping]] = ..., session_strategy: _Optional[_Union[SessionGenerationStrategyDTO, _Mapping]] = ..., execution_workspace_id: _Optional[str] = ..., title_template: _Optional[str] = ..., path_template: _Optional[_Iterable[str]] = ...) -> None: ...

class SessionGenerationValidationDTO(_message.Message):
    __slots__ = ("workspace_id", "provider_available", "reason")
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    workspace_id: str
    provider_available: bool
    reason: str
    def __init__(self, workspace_id: _Optional[str] = ..., provider_available: _Optional[bool] = ..., reason: _Optional[str] = ...) -> None: ...

class SessionGenerationUIPolicyDTO(_message.Message):
    __slots__ = ("on_run_started", "on_run_completed")
    ON_RUN_STARTED_FIELD_NUMBER: _ClassVar[int]
    ON_RUN_COMPLETED_FIELD_NUMBER: _ClassVar[int]
    on_run_started: str
    on_run_completed: str
    def __init__(self, on_run_started: _Optional[str] = ..., on_run_completed: _Optional[str] = ...) -> None: ...

class SessionGenerationActionConfigDTO(_message.Message):
    __slots__ = ("generator_type", "config", "placement", "context_source", "session_strategy", "title_template", "path_template", "created_from_uri", "gateway_id", "ui_policy", "mount_missing", "delete_outputs")
    GENERATOR_TYPE_FIELD_NUMBER: _ClassVar[int]
    CONFIG_FIELD_NUMBER: _ClassVar[int]
    PLACEMENT_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_SOURCE_FIELD_NUMBER: _ClassVar[int]
    SESSION_STRATEGY_FIELD_NUMBER: _ClassVar[int]
    TITLE_TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    PATH_TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    CREATED_FROM_URI_FIELD_NUMBER: _ClassVar[int]
    GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    UI_POLICY_FIELD_NUMBER: _ClassVar[int]
    MOUNT_MISSING_FIELD_NUMBER: _ClassVar[int]
    DELETE_OUTPUTS_FIELD_NUMBER: _ClassVar[int]
    generator_type: SessionGenerationGeneratorTypeDTO
    config: _struct_pb2.Struct
    placement: SessionGenerationPlacementDTO
    context_source: SessionGenerationContextSourceDTO
    session_strategy: SessionGenerationStrategyDTO
    title_template: str
    path_template: _containers.RepeatedScalarFieldContainer[str]
    created_from_uri: str
    gateway_id: str
    ui_policy: SessionGenerationUIPolicyDTO
    mount_missing: str
    delete_outputs: str
    def __init__(self, generator_type: _Optional[_Union[SessionGenerationGeneratorTypeDTO, _Mapping]] = ..., config: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ..., placement: _Optional[_Union[SessionGenerationPlacementDTO, _Mapping]] = ..., context_source: _Optional[_Union[SessionGenerationContextSourceDTO, _Mapping]] = ..., session_strategy: _Optional[_Union[SessionGenerationStrategyDTO, _Mapping]] = ..., title_template: _Optional[str] = ..., path_template: _Optional[_Iterable[str]] = ..., created_from_uri: _Optional[str] = ..., gateway_id: _Optional[str] = ..., ui_policy: _Optional[_Union[SessionGenerationUIPolicyDTO, _Mapping]] = ..., mount_missing: _Optional[str] = ..., delete_outputs: _Optional[str] = ...) -> None: ...

class SessionGenerationOutputDTO(_message.Message):
    __slots__ = ("kind", "workspace_id", "session_id", "title", "navigation_path")
    KIND_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    SESSION_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    NAVIGATION_PATH_FIELD_NUMBER: _ClassVar[int]
    kind: str
    workspace_id: str
    session_id: str
    title: str
    navigation_path: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, kind: _Optional[str] = ..., workspace_id: _Optional[str] = ..., session_id: _Optional[str] = ..., title: _Optional[str] = ..., navigation_path: _Optional[_Iterable[str]] = ...) -> None: ...

class SessionGenerationExecuteResultDTO(_message.Message):
    __slots__ = ("run_id", "status", "outputs", "message_id", "job_id", "report_back_job_id", "error")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    OUTPUTS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    REPORT_BACK_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    status: str
    outputs: _containers.RepeatedCompositeFieldContainer[SessionGenerationOutputDTO]
    message_id: str
    job_id: str
    report_back_job_id: str
    error: str
    def __init__(self, run_id: _Optional[str] = ..., status: _Optional[str] = ..., outputs: _Optional[_Iterable[_Union[SessionGenerationOutputDTO, _Mapping]]] = ..., message_id: _Optional[str] = ..., job_id: _Optional[str] = ..., report_back_job_id: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class LegacyGenerationRun(_message.Message):
    __slots__ = ("run_id", "generator_id", "idempotency_key", "status", "trigger_type", "scheduled_for", "outputs", "execution_workspace_id", "message_id", "job_id", "report_back_job_id", "error", "started_at", "ended_at")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    GENERATOR_ID_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TRIGGER_TYPE_FIELD_NUMBER: _ClassVar[int]
    SCHEDULED_FOR_FIELD_NUMBER: _ClassVar[int]
    OUTPUTS_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_WORKSPACE_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    REPORT_BACK_JOB_ID_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    generator_id: str
    idempotency_key: str
    status: str
    trigger_type: str
    scheduled_for: _timestamp_pb2.Timestamp
    outputs: _containers.RepeatedCompositeFieldContainer[SessionGenerationOutputDTO]
    execution_workspace_id: str
    message_id: str
    job_id: str
    report_back_job_id: str
    error: str
    started_at: _timestamp_pb2.Timestamp
    ended_at: _timestamp_pb2.Timestamp
    def __init__(self, run_id: _Optional[str] = ..., generator_id: _Optional[str] = ..., idempotency_key: _Optional[str] = ..., status: _Optional[str] = ..., trigger_type: _Optional[str] = ..., scheduled_for: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., outputs: _Optional[_Iterable[_Union[SessionGenerationOutputDTO, _Mapping]]] = ..., execution_workspace_id: _Optional[str] = ..., message_id: _Optional[str] = ..., job_id: _Optional[str] = ..., report_back_job_id: _Optional[str] = ..., error: _Optional[str] = ..., started_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., ended_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class SessionGeneratorImportRequest(_message.Message):
    __slots__ = ("task_id", "source_hash", "configuration", "revision", "lifecycle", "reason", "created_at", "updated_at", "next_run_at", "workspace_ids", "runs")
    class WorkspaceIdsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_HASH_FIELD_NUMBER: _ClassVar[int]
    CONFIGURATION_FIELD_NUMBER: _ClassVar[int]
    REVISION_FIELD_NUMBER: _ClassVar[int]
    LIFECYCLE_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    NEXT_RUN_AT_FIELD_NUMBER: _ClassVar[int]
    WORKSPACE_IDS_FIELD_NUMBER: _ClassVar[int]
    RUNS_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    source_hash: str
    configuration: PersistentTaskConfigurationDTO
    revision: int
    lifecycle: str
    reason: str
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    next_run_at: _timestamp_pb2.Timestamp
    workspace_ids: _containers.ScalarMap[str, str]
    runs: _containers.RepeatedCompositeFieldContainer[LegacyGenerationRun]
    def __init__(self, task_id: _Optional[str] = ..., source_hash: _Optional[str] = ..., configuration: _Optional[_Union[PersistentTaskConfigurationDTO, _Mapping]] = ..., revision: _Optional[int] = ..., lifecycle: _Optional[str] = ..., reason: _Optional[str] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., next_run_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., workspace_ids: _Optional[_Mapping[str, str]] = ..., runs: _Optional[_Iterable[_Union[LegacyGenerationRun, _Mapping]]] = ...) -> None: ...
