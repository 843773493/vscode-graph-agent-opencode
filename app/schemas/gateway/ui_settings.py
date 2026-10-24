from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.resource_uri import parse_resource_uri
from app.gateway.theme.defaults import DEFAULT_THEME_BACKGROUND_OVERLAY


def _file_reference(value: str) -> str:
    # 设置只保存 owner 已返回的地址；原生输入必须先经过普通文件入口。
    resource = parse_resource_uri(value)
    if resource.kind not in {"files", "attachments"}:
        raise ValueError("UI 文件引用必须使用 files 或 attachments 资源 URI")
    return resource.display_uri


class WebUIMainAreaRatiosDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_sessions: float = Field(default=1, gt=0)
    chat: float = Field(default=1, gt=0)
    workspace_preview: float = Field(default=1, gt=0)
    auxiliary: float = Field(default=1, gt=0)


class WebUIWorkspaceBottomPanelSettingsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visible: bool | None = None
    height: int | None = Field(default=None, ge=190, le=520)
    tab: Literal["terminal", "output", "ports", "automation"] | None = None
    terminal_id: str | None = None


class WebUILayoutSettingsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workbench_view: Literal["sessions", "gateway"] | None = None
    agent_sessions_panel_open: bool | None = None
    chat_visible: bool | None = None
    auxiliary_visible: bool | None = None
    panel_visible: bool | None = None
    auxiliary_tab: Literal["changes", "files", "resources", "debug"] | None = None
    auxiliary_tab_order: (
        list[Literal["changes", "files", "resources", "debug"]] | None
    ) = Field(default=None, min_length=4, max_length=4)
    main_area_ratios: WebUIMainAreaRatiosDTO | None = None
    bottom_panel_by_workspace: (
        dict[str, WebUIWorkspaceBottomPanelSettingsDTO] | None
    ) = Field(
        default=None,
        max_length=200,
    )
    workspace_preview_visible: bool | None = None
    workspace_preview_maximized: bool | None = None
    workspace_preview_file_paths: list[str] | None = Field(default=None, max_length=20)
    workspace_preview_active_file_path: str | None = Field(
        default=None,
        max_length=4096,
    )
    customizations_collapsed: bool | None = None
    customizations_height: int | None = Field(default=None, ge=80, le=520)
    panel_height: int | None = Field(default=None, ge=190, le=520)
    content_view: (
        Literal[
            "default",
            "events",
            "requests",
            "changes",
            "resources",
            "agent",
        ]
        | None
    ) = None
    delivery_policy_default: (
        Literal[
            "after_turn",
            "after_tool_result",
            "after_interrupt",
        ]
        | None
    ) = None

    @field_validator("workspace_preview_file_paths")
    @classmethod
    def validate_preview_files(cls, values: list[str] | None) -> list[str] | None:
        return [_file_reference(value) for value in values] if values is not None else None

    @field_validator("workspace_preview_active_file_path")
    @classmethod
    def validate_active_preview_file(cls, value: str | None) -> str | None:
        return _file_reference(value) if value is not None else None

    @field_validator("auxiliary_tab_order")
    @classmethod
    def validate_auxiliary_tab_order(
        cls,
        value: list[str] | None,
    ) -> list[str] | None:
        if value is not None and set(value) != {
            "changes",
            "files",
            "resources",
            "debug",
        }:
            raise ValueError("右侧侧边栏标签顺序必须包含且仅包含全部四个标签")
        return value


class WebUISessionSidebarSettingsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filter_mode: Literal["all", "current", "attachments", "agent", "named"] = "all"
    sort_mode: Literal["created", "updated"] = "updated"
    grouping_mode: Literal["workspace", "time"] = "workspace"
    workspace_group_capped: bool = True
    collapsed_workspace_ids: list[str] = Field(default_factory=list, max_length=1000)
    collapsed_session_ids: list[str] = Field(default_factory=list, max_length=5000)
    expanded_root_tree_ids: list[str] = Field(default_factory=list, max_length=1000)
    collapsed_section_ids: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator(
        "collapsed_workspace_ids",
        "collapsed_session_ids",
        "expanded_root_tree_ids",
        "collapsed_section_ids",
    )
    @classmethod
    def normalize_ids(cls, values: list[str]) -> list[str]:
        if any(len(value) > 4096 for value in values):
            raise ValueError("UI 设置 ID 长度不能超过 4096")
        return sorted(set(values))


class WebUIWorkspaceFileTreeSettingsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expanded_paths_by_workspace: dict[str, list[str]] = Field(
        default_factory=dict,
        max_length=200,
    )

    @field_validator("expanded_paths_by_workspace")
    @classmethod
    def normalize_expanded_paths(
        cls,
        values: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        normalized: dict[str, list[str]] = {}
        for workspace_id, paths in values.items():
            if len(workspace_id) > 4096:
                raise ValueError("文件树工作区 ID 长度不能超过 4096")
            if len(paths) > 1000 or any(len(path) > 4096 for path in paths):
                raise ValueError("单个工作区最多保存 1000 个长度不超过 4096 的路径")
            normalized[workspace_id] = sorted({_file_reference(path) for path in paths})
        return normalized


class WebUIGatewayConsoleSettingsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: Literal["routing", "managed"] = "routing"


class GatewayThemeBackgroundDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["remote", "gateway_asset"]
    url: str | None = None
    asset_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    position: str = Field(default="center", min_length=1, max_length=120)
    size: str = Field(default="cover", min_length=1, max_length=120)
    repeat: Literal["no-repeat", "repeat", "repeat-x", "repeat-y", "space", "round"] = (
        "no-repeat"
    )
    appearance: Literal["immersive", "theme"] = "immersive"
    overlay: str = Field(
        default=DEFAULT_THEME_BACKGROUND_OVERLAY,
        min_length=1,
        max_length=1000,
    )

    @model_validator(mode="after")
    def validate_source(self) -> GatewayThemeBackgroundDTO:
        if self.type == "remote":
            if not self.url or self.asset_id is not None:
                raise ValueError("remote 背景必须只提供 url")
        elif not self.asset_id or self.url is not None:
            raise ValueError("gateway_asset 背景必须只提供 asset_id")
        return self


class ResolvedGatewayThemeDTO(BaseModel):
    id: str
    label: str
    color_scheme: Literal["light", "dark"]
    tokens: dict[str, str]
    background_image_url: str | None = None


class GatewayThemeOptionDTO(BaseModel):
    id: str
    label: str
    extends: Literal["warm", "green", "blue"]
    source: Literal["builtin", "gateway_config"]
    preview_tokens: dict[str, str]
    background_image_url: str | None = None


class GatewayThemeCatalogDTO(BaseModel):
    current_theme_id: str
    items: list[GatewayThemeOptionDTO]
    current_theme: ResolvedGatewayThemeDTO


class GatewayUIAssetDTO(BaseModel):
    asset_id: str
    original_filename: str
    content_type: str
    size: int
    sha256: str
    imported_at: str
    url: str
    referenced_theme_ids: list[str] = Field(default_factory=list)


class GatewayUIAssetListDTO(BaseModel):
    items: list[GatewayUIAssetDTO] = Field(default_factory=list)


class WebUIThemeSettingsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    background: GatewayThemeBackgroundDTO | None = None
    resolved_theme: ResolvedGatewayThemeDTO | None = None


class WebUIThemeSettingsUpdateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    background: GatewayThemeBackgroundDTO | None = None


class WebUISettingsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layout: WebUILayoutSettingsDTO = Field(default_factory=WebUILayoutSettingsDTO)
    session_sidebar: WebUISessionSidebarSettingsDTO = Field(
        default_factory=WebUISessionSidebarSettingsDTO
    )
    workspace_file_tree: WebUIWorkspaceFileTreeSettingsDTO = Field(
        default_factory=WebUIWorkspaceFileTreeSettingsDTO
    )
    gateway_console: WebUIGatewayConsoleSettingsDTO = Field(
        default_factory=WebUIGatewayConsoleSettingsDTO
    )
    theme: WebUIThemeSettingsDTO = Field(default_factory=WebUIThemeSettingsDTO)


class WebUISettingsUpdateDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layout: WebUILayoutSettingsDTO | None = None
    session_sidebar: WebUISessionSidebarSettingsDTO | None = None
    workspace_file_tree: WebUIWorkspaceFileTreeSettingsDTO | None = None
    gateway_console: WebUIGatewayConsoleSettingsDTO | None = None
    theme: WebUIThemeSettingsUpdateDTO | None = None
