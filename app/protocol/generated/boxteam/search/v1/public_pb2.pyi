from boxteam.gateway.v1 import public_pb2 as _public_pb2
from boxteam.workspace.v2 import public_pb2 as _public_pb2_1
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FederatedSearchQueryDTO(_message.Message):
    __slots__ = ("category", "operation", "contract_version", "query", "match_mode", "limit")
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    CONTRACT_VERSION_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    MATCH_MODE_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    category: str
    operation: str
    contract_version: int
    query: str
    match_mode: str
    limit: int
    def __init__(self, category: _Optional[str] = ..., operation: _Optional[str] = ..., contract_version: _Optional[int] = ..., query: _Optional[str] = ..., match_mode: _Optional[str] = ..., limit: _Optional[int] = ...) -> None: ...

class FederatedSearchBranchKeyDTO(_message.Message):
    __slots__ = ("owner", "category", "operation")
    OWNER_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    OPERATION_FIELD_NUMBER: _ClassVar[int]
    owner: _public_pb2.GatewaySearchWorkspaceOwnerDTO
    category: str
    operation: str
    def __init__(self, owner: _Optional[_Union[_public_pb2.GatewaySearchWorkspaceOwnerDTO, _Mapping]] = ..., category: _Optional[str] = ..., operation: _Optional[str] = ...) -> None: ...

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
    __slots__ = ("branch", "state", "provider_id", "code", "message", "retryable", "cursor", "allocated_results", "returned_results")
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_ID_FIELD_NUMBER: _ClassVar[int]
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    CURSOR_FIELD_NUMBER: _ClassVar[int]
    ALLOCATED_RESULTS_FIELD_NUMBER: _ClassVar[int]
    RETURNED_RESULTS_FIELD_NUMBER: _ClassVar[int]
    branch: FederatedSearchBranchKeyDTO
    state: str
    provider_id: str
    code: str
    message: str
    retryable: bool
    cursor: str
    allocated_results: int
    returned_results: int
    def __init__(self, branch: _Optional[_Union[FederatedSearchBranchKeyDTO, _Mapping]] = ..., state: _Optional[str] = ..., provider_id: _Optional[str] = ..., code: _Optional[str] = ..., message: _Optional[str] = ..., retryable: _Optional[bool] = ..., cursor: _Optional[str] = ..., allocated_results: _Optional[int] = ..., returned_results: _Optional[int] = ...) -> None: ...

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

class FederatedSearchEventDTO(_message.Message):
    __slots__ = ("sequence", "request_id", "type", "scope", "branch", "page", "summary")
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    sequence: int
    request_id: str
    type: str
    scope: _public_pb2.GatewaySearchScopeResolutionDTO
    branch: FederatedSearchBranchDTO
    page: _public_pb2_1.WorkspaceSearchResultDTO
    summary: FederatedSearchSummaryDTO
    def __init__(self, sequence: _Optional[int] = ..., request_id: _Optional[str] = ..., type: _Optional[str] = ..., scope: _Optional[_Union[_public_pb2.GatewaySearchScopeResolutionDTO, _Mapping]] = ..., branch: _Optional[_Union[FederatedSearchBranchDTO, _Mapping]] = ..., page: _Optional[_Union[_public_pb2_1.WorkspaceSearchResultDTO, _Mapping]] = ..., summary: _Optional[_Union[FederatedSearchSummaryDTO, _Mapping]] = ...) -> None: ...
