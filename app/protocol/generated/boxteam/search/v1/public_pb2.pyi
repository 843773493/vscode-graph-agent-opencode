from boxteam.gateway.v1 import public_pb2 as _public_pb2
from boxteam.workspace.v2 import public_pb2 as _public_pb2_1
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FederatedFileSearchTargetDTO(_message.Message):
    __slots__ = ("owner", "target")
    OWNER_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    owner: _public_pb2.GatewaySearchWorkspaceOwnerDTO
    target: _public_pb2_1.WorkspaceFileSearchTargetDTO
    def __init__(self, owner: _Optional[_Union[_public_pb2.GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ..., target: _Optional[_Union[_public_pb2_1.WorkspaceFileSearchTargetDTO, _Mapping]] = ...) -> None: ...

class FederatedSearchQueryDTO(_message.Message):
    __slots__ = ("category", "operation", "contract_version", "query", "match_mode", "limit", "file_targets", "allow_catalog_mirror", "context")
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    MATCH_MODE_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    FILE_TARGETS_FIELD_NUMBER: _ClassVar[int]
    ALLOW_CATALOG_MIRROR_FIELD_NUMBER: _ClassVar[int]
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    category: str
    operation: str
    contract_version: int
    query: str
    match_mode: str
    limit: int
    file_targets: _containers.RepeatedCompositeFieldContainer[FederatedFileSearchTargetDTO]
    allow_catalog_mirror: bool
    context: _public_pb2_1.WorkspaceContextSearchOptionsDTO
    def __init__(self, category: _Optional[str] = ..., operation: _Optional[str] = ..., contract_version: _Optional[int] = ..., query: _Optional[str] = ..., match_mode: _Optional[str] = ..., limit: _Optional[int] = ..., file_targets: _Optional[_Iterable[_Union[FederatedFileSearchTargetDTO, _Mapping]]] = ..., allow_catalog_mirror: _Optional[bool] = ..., context: _Optional[_Union[_public_pb2_1.WorkspaceContextSearchOptionsDTO, _Mapping]] = ...) -> None: ...

class FederatedSearchBranchKeyDTO(_message.Message):
    __slots__ = ("owner", "category", "operation", "resource_uri")
    OWNER_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_URI_FIELD_NUMBER: _ClassVar[int]
    owner: _public_pb2.GatewaySearchWorkspaceOwnerDTO
    category: str
    operation: str
    resource_uri: str
    def __init__(self, owner: _Optional[_Union[_public_pb2.GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ..., category: _Optional[str] = ..., operation: _Optional[str] = ..., resource_uri: _Optional[str] = ...) -> None: ...

class FederatedSearchContinuationDTO(_message.Message):
    __slots__ = ("branch", "cursor")
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    branch: FederatedSearchBranchKeyDTO
    cursor: str
    def __init__(self, branch: _Optional[_Union[FederatedSearchBranchKeyDTO, _Mapping]] = ..., cursor: _Optional[str] = ...) -> None: ...

class FederatedSearchRequest(_message.Message):
    __slots__ = ("scope", "queries", "continuations", "max_results", "deadline_at")
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    QUERIES_FIELD_NUMBER: _ClassVar[int]
    CONTINUATIONS_FIELD_NUMBER: _ClassVar[int]
    MAX_RESULTS_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_AT_FIELD_NUMBER: _ClassVar[int]
    scope: _public_pb2.GatewaySearchScopeRequest
    queries: _containers.RepeatedCompositeFieldContainer[FederatedSearchQueryDTO]
    continuations: _containers.RepeatedCompositeFieldContainer[FederatedSearchContinuationDTO]
    max_results: int
    deadline_at: float
    def __init__(self, scope: _Optional[_Union[_public_pb2.GatewaySearchScopeRequest, _Mapping]] = ..., queries: _Optional[_Iterable[_Union[FederatedSearchQueryDTO, _Mapping]]] = ..., continuations: _Optional[_Iterable[_Union[FederatedSearchContinuationDTO, _Mapping]]] = ..., max_results: _Optional[int] = ..., deadline_at: _Optional[float] = ...) -> None: ...

class FederatedSearchBranchDTO(_message.Message):
    __slots__ = ("branch", "state", "provider_id", "code", "message", "retryable", "cursor", "allocated_results", "returned_results", "error_owner", "reason", "http_status")
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_ID_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    ALLOCATED_RESULTS_FIELD_NUMBER: _ClassVar[int]
    RETURNED_RESULTS_FIELD_NUMBER: _ClassVar[int]
    ERROR_OWNER_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    HTTP_STATUS_FIELD_NUMBER: _ClassVar[int]
    branch: FederatedSearchBranchKeyDTO
    state: str
    provider_id: str
    code: str
    message: str
    retryable: bool
    cursor: str
    allocated_results: int
    returned_results: int
    error_owner: str
    reason: str
    http_status: int
    def __init__(self, branch: _Optional[_Union[FederatedSearchBranchKeyDTO, _Mapping]] = ..., state: _Optional[str] = ..., provider_id: _Optional[str] = ..., code: _Optional[str] = ..., message: _Optional[str] = ..., retryable: _Optional[bool] = ..., cursor: _Optional[str] = ..., allocated_results: _Optional[int] = ..., returned_results: _Optional[int] = ..., error_owner: _Optional[str] = ..., reason: _Optional[str] = ..., http_status: _Optional[int] = ...) -> None: ...

class FederatedSearchSummaryDTO(_message.Message):
    __slots__ = ("state", "order", "branches", "returned_results")
    STATE_FIELD_NUMBER: _ClassVar[int]
    ORDER_FIELD_NUMBER: _ClassVar[int]
    BRANCHES_FIELD_NUMBER: _ClassVar[int]
    RETURNED_RESULTS_FIELD_NUMBER: _ClassVar[int]
    state: str
    order: str
    branches: _containers.RepeatedCompositeFieldContainer[FederatedSearchBranchDTO]
    returned_results: int
    def __init__(self, state: _Optional[str] = ..., order: _Optional[str] = ..., branches: _Optional[_Iterable[_Union[FederatedSearchBranchDTO, _Mapping]]] = ..., returned_results: _Optional[int] = ...) -> None: ...

class FederatedSearchSourceDTO(_message.Message):
    __slots__ = ("kind", "updated_at", "stale", "unavailable_reason")
    KIND_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    STALE_FIELD_NUMBER: _ClassVar[int]
    UNAVAILABLE_REASON_FIELD_NUMBER: _ClassVar[int]
    kind: str
    updated_at: str
    stale: bool
    unavailable_reason: str
    def __init__(self, kind: _Optional[str] = ..., updated_at: _Optional[str] = ..., stale: _Optional[bool] = ..., unavailable_reason: _Optional[str] = ...) -> None: ...

class FederatedSearchEventDTO(_message.Message):
    __slots__ = ("sequence", "request_id", "type", "scope", "branch", "page", "summary", "source")
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    sequence: int
    request_id: str
    type: str
    scope: _public_pb2.GatewaySearchScopeResolutionDTO
    branch: FederatedSearchBranchDTO
    page: _public_pb2_1.WorkspaceSearchResultDTO
    summary: FederatedSearchSummaryDTO
    source: FederatedSearchSourceDTO
    def __init__(self, sequence: _Optional[int] = ..., request_id: _Optional[str] = ..., type: _Optional[str] = ..., scope: _Optional[_Union[_public_pb2.GatewaySearchScopeResolutionDTO, _Mapping]] = ..., branch: _Optional[_Union[FederatedSearchBranchDTO, _Mapping]] = ..., page: _Optional[_Union[_public_pb2_1.WorkspaceSearchResultDTO, _Mapping]] = ..., summary: _Optional[_Union[FederatedSearchSummaryDTO, _Mapping]] = ..., source: _Optional[_Union[FederatedSearchSourceDTO, _Mapping]] = ...) -> None: ...

class GatewayContextSearchRequest(_message.Message):
    __slots__ = ("source_gateway_id", "resource_target", "resource_search")
    SOURCE_GATEWAY_ID_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_TARGET_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_SEARCH_FIELD_NUMBER: _ClassVar[int]
    source_gateway_id: str
    resource_target: _public_pb2_1.ContextGrantTarget
    resource_search: FederatedSearchRequest
    def __init__(self, source_gateway_id: _Optional[str] = ..., resource_target: _Optional[_Union[_public_pb2_1.ContextGrantTarget, _Mapping]] = ..., resource_search: _Optional[_Union[FederatedSearchRequest, _Mapping]] = ...) -> None: ...

class GatewayContextSearchResultDTO(_message.Message):
    __slots__ = ("events",)
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    events: _containers.RepeatedCompositeFieldContainer[FederatedSearchEventDTO]
    def __init__(self, events: _Optional[_Iterable[_Union[FederatedSearchEventDTO, _Mapping]]] = ...) -> None: ...
