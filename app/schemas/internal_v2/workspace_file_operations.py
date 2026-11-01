"""文件集合操作复用导航意图的身份、顺序、依赖和状态语义。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from app.core.resource_uri import parse_resource_uri, resource_display_uri
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationState,
    validate_operation_id,
)


class WorkspaceDirectoryKeyDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: str
    scope: Literal["workspace", "filesystem"]
    root: str
    path: str

    @model_validator(mode="after")
    def validate_uri_key(self) -> WorkspaceDirectoryKeyDTO:
        parsed = parse_resource_uri(self.path)
        if parsed.kind not in {"files", "attachments"} or parsed.scope not in {"workspace", "gateway"}:
            raise ValueError("目录键必须使用 files 或 attachments 资源 URI")
        root = resource_display_uri(scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
                                    tail_segments=parsed.tail_segments[:1], gateway_authority=parsed.gateway_authority)
        scope = "workspace" if parsed.scope == "workspace" else "filesystem"
        if self.root != root or self.scope != scope:
            raise ValueError("目录键的根或 scope 与 URI 不一致")
        if parsed.scope == "workspace" and parsed.scope_id != self.workspace_id:
            raise ValueError("目录键的工作区 URI 与执行 owner 不一致")
        return self

    @model_serializer(mode="wrap")
    def serialize_key(self, handler: SerializerFunctionWrapHandler):
        # model_copy 不会执行模型校验；输出和落盘仍核原有根、scope及owner配对。
        type(self).model_validate({name: getattr(self, name) for name in type(self).model_fields})
        return handler(self)


def validate_file_uri(value: str) -> str:
    parsed = parse_resource_uri(value)
    if parsed.kind not in {"files", "attachments"}:
        raise ValueError("文件引用必须使用 files 或 attachments 资源 URI")
    return parsed.display_uri


def _serialize_file_uri(value: str):
    # 构造和发布共用原有规范化，不处理文件正文或原生请求输入。
    # 不声明独立返回schema，保留使用字段的minLength等既有公开结构约束。
    return validate_file_uri(value)


WorkspaceFileURI = Annotated[str, AfterValidator(validate_file_uri), PlainSerializer(_serialize_file_uri)]


class WorkspaceDirectoryRevisionDTO(BaseModel):
    directory_key: WorkspaceDirectoryKeyDTO
    directory_revision: int = Field(ge=1)
    exists: bool


class WorkspaceFileNodeDTO(BaseModel):
    name: str
    path: WorkspaceFileURI
    kind: Literal["file", "directory", "symlink", "other"]
    has_children: bool = False
    size: int | None = None
    modified_at: str | None = None


class WorkspaceFileListDTO(BaseModel):
    root_path: WorkspaceFileURI
    path: WorkspaceFileURI
    items: list[WorkspaceFileNodeDTO] = Field(default_factory=list)
    truncated: bool = False
    limit: int = 500
    next_cursor: str | None = None
    directory_key: WorkspaceDirectoryKeyDTO
    directory_revision: int = Field(ge=1)


class WorkspaceFileMutationIntentDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_operation_id: str
    client_sequence: int = Field(ge=1)
    expected_directory_revision: int | None = Field(default=None, ge=1)
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    created_by_operation_id: str | None = None
    relative_child_path: str = ""
    directory_revision_from_operation_id: str | None = None
    client_ref: str | None = Field(default=None, min_length=1, max_length=200)
    directory_key: WorkspaceDirectoryKeyDTO

    @model_validator(mode="after")
    def validate_intent(self) -> WorkspaceFileMutationIntentDTO:
        validate_operation_id(self.client_operation_id)
        for dependency in self.depends_on:
            validate_operation_id(dependency)
            if dependency == self.client_operation_id:
                raise ValueError("文件 intent 不能依赖自身")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("文件 intent 的依赖不能重复")
        for reference in (self.created_by_operation_id, self.directory_revision_from_operation_id):
            if reference is not None:
                validate_operation_id(reference)
                if reference not in self.depends_on:
                    raise ValueError("创建者和目录 revision 来源必须显式声明为依赖")
        if self.created_by_operation_id is None and self.relative_child_path:
            raise ValueError("relative_child_path 必须引用原创建 operation")
        if self.created_by_operation_id is not None and self.directory_revision_from_operation_id != self.created_by_operation_id:
            raise ValueError("创建后继的目录 revision 必须来自原创建 operation")
        if (self.expected_directory_revision is None) == (self.directory_revision_from_operation_id is None):
            raise ValueError("expected_directory_revision 与前序结果 revision 必须二选一")
        return self


class WorkspaceFileOperationItemDTO(BaseModel):
    path: WorkspaceFileURI
    kind: Literal["file", "directory"]
    state: Literal["pending", "published", "conflict", "unverified"]
    error: str | None = None


class WorkspaceFileOperationReceiptDTO(BaseModel):
    operation_id: str
    client_sequence: int
    queue_seq: int
    kind: Literal["create", "paste", "copy", "upload"]
    state: NavigationMutationState
    receipt_revision: int
    effect: Literal["none", "complete", "partial", "unverified"]
    items: list[WorkspaceFileOperationItemDTO] = Field(default_factory=list)
    directories: list[WorkspaceDirectoryRevisionDTO] = Field(default_factory=list)
    snapshots: list[WorkspaceFileListDTO] = Field(default_factory=list)
    created_path: WorkspaceFileURI | None = None
    error_code: str | None = None
    error_detail: str | None = None


class WorkspaceFileOperationStatusDTO(BaseModel):
    operations: list[WorkspaceFileOperationReceiptDTO]
    worker_state: str
    worker_error: str | None = None
