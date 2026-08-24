// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface AcquireGatewayUserRequest {
  client_label?: string | null;
}
export interface ActivateGatewayWorkspaceResultDTO {
  active_workspace_id: string;
}
export interface AddLocalWorkspaceRequest {
  /**
   * 本机工作区绝对路径
   */
  root_path: string;
  /**
   * 工作区显示名称
   */
  name?: string | null;
  /**
   * 已有后端 URL；未提供时 Gateway 会为该工作区启动本机后端。
   */
  backend_url?: string | null;
}
export interface AddRemoteGatewayRequest {
  /**
   * 远程 Gateway 显示名称
   */
  name?: string | null;
  /**
   * 复用已连接远程 Gateway 的 SSH 主机和凭据
   */
  connection_workspace_id?: string | null;
  /**
   * 复用用户 ~/.ssh/config 中的 Host 别名
   */
  ssh_config_host?: string | null;
  /**
   * 手动连接的 SSH 主机
   */
  host?: string | null;
  /**
   * 手动连接的 SSH 端口
   */
  port?: number;
  /**
   * 手动连接的 SSH 用户名
   */
  username?: string | null;
  /**
   * 手动连接使用的 SSH 私钥路径
   */
  private_key_path?: string | null;
  /**
   * 远端 Gateway loopback 端口
   */
  remote_gateway_port?: number;
}
export interface ChangePortForwardLabelRequest {
  label?: string | null;
}
export interface ChangePortForwardLocalPortRequest {
  local_port: number;
}
export interface CreateFederationManagedWorkspaceRequest {
  /**
   * 远端 Gateway 主机上的绝对目录
   */
  root_path: string;
  name?: string | null;
  /**
   * 目录不存在时由远端 Gateway 创建目录
   */
  create_directory?: boolean;
}
export interface CreateGatewayGuestRequest {
  tracking?: {
    [k: string]: string | number | boolean;
  };
}
export interface CreateGatewayManagedWorkspaceRequest {
  /**
   * 远端 Gateway 主机上的绝对目录
   */
  root_path: string;
  name?: string | null;
  /**
   * 目录不存在时由远端 Gateway 创建目录
   */
  create_directory?: boolean;
  /**
   * 为空时管理本机 Gateway；否则管理指定远程 Gateway
   */
  gateway_connection_id?: string | null;
}
export interface CreateGatewayUserRequest {
  display_name: string;
  user_id?: string | null;
}
export interface CreatePortForwardRequest {
  remote_port: number;
  local_port?: number | null;
  protocol?: "http" | "https" | "tcp";
  label?: string | null;
}
export interface DevelopmentRuntimeRestartDTO {
  status?: "scheduled";
  previous_process_id: number;
  helper_process_id: number;
  delay_ms: number;
}
export interface FederationProtocolManifestDTO {
  protocol_version: number;
  gateway_id: string;
  federation_depth?: 0;
  capabilities?: string[];
}
export interface FederationWorkspaceDTO {
  workspace_id: string;
  name: string;
  root_path: string;
  managed: boolean;
  connection_kind: "local";
  services?: ("workspace_api" | "terminal_manager" | "browser_manager")[];
}
export interface FederationWorkspaceListDTO {
  protocol_version: number;
  gateway_id: string;
  items?: FederationWorkspaceDTO[];
  excluded?: string[];
}
export interface GatewayConfigReloadStatusDTO {
  available?: boolean;
  healthy?: boolean | null;
  revision?: string | null;
  restart_required?: boolean;
  reason?: ("invalid_config" | "restart_required" | "apply_failed") | null;
  changed_sections?: string[];
  last_error?: string | null;
  error?: string | null;
}
export interface GatewayConfigSourceDTO {
  path: string;
  layer: "inline" | "user" | "user_local" | "sqlite";
  precedence: number;
  loaded: boolean;
}
export interface GatewayConfigSourcesDTO {
  revision: string;
  schema_path: string;
  sources?: GatewayConfigSourceDTO[];
}
export interface GatewayDiagnosticLogDTO {
  log_id: string;
  source: "gateway" | "workspace";
  workspace_id?: string | null;
  workspace_name?: string | null;
  service: string;
  label: string;
  status: "available" | "empty" | "unavailable";
  tail?: string;
  truncated?: boolean;
  line_count?: number;
  size_bytes?: number;
  updated_at?: string | null;
  error?: string | null;
}
export interface GatewayDiagnosticWorkspaceDTO {
  workspace_id: string;
  name: string;
  root_path: string;
  connection_kind: "local" | "remote_gateway";
  status: "ready" | "offline";
  managed: boolean;
  system_default: boolean;
  connection_error?: string | null;
}
export interface GatewayDiagnosticsDTO {
  gateway_id: string;
  gateway_name: string;
  gateway_connection_id?: string | null;
  connection_kind: "local" | "remote_gateway";
  status: "ready" | "degraded" | "offline";
  checked_at: string;
  selected_workspace_id?: string | null;
  selected_log_id?: string | null;
  workspaces?: GatewayDiagnosticWorkspaceDTO[];
  logs?: GatewayDiagnosticLogDTO[];
}
export interface GatewayDirectoryEntryDTO {
  name: string;
  path: string;
}
export interface GatewayDirectoryListDTO {
  path: string;
  parent_path?: string | null;
  home_path: string;
  entries?: GatewayDirectoryEntryDTO[];
  truncated?: boolean;
  limit: number;
}
export interface GatewayHealthDTO {
  status?: "ok";
  active_workspace_id?: string | null;
  process_id: number;
  development_restart_available?: boolean;
}
export interface GatewayInboundAccessListDTO {
  gateway_id: string;
  peers?: GatewayInboundPeerDTO[];
  items?: GatewayInboundWorkspaceDTO[];
}
export interface GatewayInboundPeerDTO {
  connection_id: string;
  peer_gateway_id: string;
  credential_expires_at: string;
}
export interface GatewayInboundWorkspaceDTO {
  workspace_id: string;
  name: string;
  root_path: string;
  status: "ready" | "offline";
  managed: boolean;
  system_default: boolean;
}
export interface GatewayManagedWorkspaceDTO {
  workspace_id: string;
  name: string;
  root_path: string;
  status: "ready" | "offline";
  removable: boolean;
  system_default: boolean;
}
export interface GatewayManagedWorkspaceListDTO {
  gateway_connection_id?: string | null;
  gateway_id: string;
  gateway_name: string;
  connection_kind: "local" | "remote_gateway";
  items?: GatewayManagedWorkspaceDTO[];
}
export interface GatewayRemoteConnectionSummaryDTO {
  gateway_connection_id: string;
  remote_workspace_id: string;
  gateway_id: string;
  name: string;
  host: string;
  port: number;
  username: string;
  ssh_config_host?: string | null;
  remote_gateway_port: number;
}
export interface GatewayRuntimeBlockerDTO {
  kind: "job" | "tool" | "background_task";
  resource_id: string;
  session_id: string;
  status: string;
  detail?: string | null;
}
export interface GatewayRuntimeRestartResultDTO {
  workspace_id: string;
  status: "restarted" | "blocked";
  forced: boolean;
  blockers?: GatewayRuntimeBlockerDTO[];
  workspaces: GatewayWorkspaceListDTO;
}
export interface GatewayWorkspaceListDTO {
  active_workspace_id?: string | null;
  items?: GatewayWorkspaceDTO[];
}
export interface GatewayWorkspaceDTO {
  workspace_id: string;
  parent_workspace_id?: string | null;
  name: string;
  root_path: string;
  backend_url: string;
  connection_kind: "local" | "remote_gateway";
  status: "ready" | "offline";
  active?: boolean;
  managed?: boolean;
  removable?: boolean;
  system_default?: boolean;
  runtime_action:
    | "start_managed_backend"
    | "safe_restart_managed_backend"
    | "force_restart_managed_backend"
    | "reconnect_remote_gateway"
    | "probe_external_backend";
  config_reload?: GatewayConfigReloadStatusDTO;
  remote?: GatewayRemoteConnectionSummaryDTO | null;
  services?: {
    [k: string]: GatewayServiceStatusDTO;
  };
  connection_error?: string | null;
  checked_at: string;
}
export interface GatewayServiceStatusDTO {
  status: "ready" | "offline" | "unavailable";
  health_path: string;
  local_url?: string | null;
  local_port?: number | null;
  remote_host?: string | null;
  remote_port?: number | null;
  error?: string | null;
}
export interface GatewayRuntimeStateResultDTO {
  workspace_id: string;
  status: "started" | "stopped" | "blocked";
  blockers?: GatewayRuntimeBlockerDTO[];
  workspaces: GatewayWorkspaceListDTO;
}
export interface GatewayThemeBackgroundDTO {
  type: "remote" | "gateway_asset";
  url?: string | null;
  asset_id?: string | null;
  position?: string;
  size?: string;
  repeat?: "no-repeat" | "repeat" | "repeat-x" | "repeat-y" | "space" | "round";
  appearance?: "immersive" | "theme";
  overlay?: string;
}
export interface GatewayThemeCatalogDTO {
  current_theme_id: string;
  items: GatewayThemeOptionDTO[];
  current_theme: ResolvedGatewayThemeDTO;
}
export interface GatewayThemeOptionDTO {
  id: string;
  label: string;
  extends: "warm" | "green" | "blue";
  source: "builtin" | "gateway_config";
  preview_tokens: {
    [k: string]: string;
  };
  background_image_url?: string | null;
}
export interface ResolvedGatewayThemeDTO {
  id: string;
  label: string;
  color_scheme: "light" | "dark";
  tokens: {
    [k: string]: string;
  };
  background_image_url?: string | null;
}
export interface GatewayUIAssetDTO {
  asset_id: string;
  original_filename: string;
  content_type: string;
  size: number;
  sha256: string;
  imported_at: string;
  url: string;
  referenced_theme_ids?: string[];
}
export interface GatewayUIAssetListDTO {
  items?: GatewayUIAssetDTO[];
}
export interface GatewayUserAccessDTO {
  kind: "user" | "guest";
  user_id?: string | null;
  lease_generation: number;
  expires_at?: string | null;
  takeover?: boolean;
}
export interface GatewayUserDTO {
  user_id: string;
  display_name: string;
  created_at: string;
  lease?: GatewayUserLeaseDTO;
}
export interface GatewayUserLeaseDTO {
  occupied?: boolean;
  client_label?: string | null;
  heartbeat_at?: string | null;
  expires_at?: string | null;
}
export interface GatewayUserListDTO {
  items?: GatewayUserDTO[];
}
export interface GatewayUserViewStateDTO {
  user_id: string;
  workspace_id: string;
  session_id: string;
  turn_anchor?: string | null;
  scroll_offset: number;
  follow_latest?: boolean;
  projection_version: number;
  tool_details_expanded?: boolean;
  updated_at: string;
}
export interface GatewayUserViewStateUpdateRequest {
  turn_anchor?: string | null;
  scroll_offset?: number;
  follow_latest?: boolean;
  projection_version?: number;
  tool_details_expanded?: boolean;
}
export interface PortForwardDTO {
  forward_id: string;
  workspace_id: string;
  connection_id: string;
  remote_host: "127.0.0.1";
  remote_port: number;
  local_host: "127.0.0.1";
  local_port: number;
  protocol: "http" | "https" | "tcp";
  label: string | null;
  status: "starting" | "active" | "error" | "stopped";
  error: string | null;
  local_url: string | null;
}
export interface PortForwardListDTO {
  items: PortForwardDTO[];
}
export interface ReorderGatewayWorkspacesRequest {
  /**
   * 按目标展示顺序排列的全部 Gateway 工作区 ID
   */
  workspace_ids: string[];
}
export interface SshConnectionOptionDTO {
  connection_id: string;
  source: "boxteam" | "ssh_config";
  label: string;
  host: string;
  port: number;
  username: string;
  workspace_id?: string | null;
  ssh_config_host?: string | null;
  initial_path?: string | null;
}
export interface SshConnectionOptionListDTO {
  items?: SshConnectionOptionDTO[];
}
export interface UpdateGatewayWorkspaceRequest {
  /**
   * 工作区显示名称
   */
  name?: string | null;
  /**
   * 父工作区 ID；显式传入 null 表示移出父工作区
   */
  parent_workspace_id?: string | null;
}
export interface WebUIGatewayConsoleSettingsDTO {
  view?: "routing" | "managed";
}
export interface WebUILayoutSettingsDTO {
  workbench_view?: ("sessions" | "gateway") | null;
  agent_sessions_panel_open?: boolean | null;
  chat_visible?: boolean | null;
  auxiliary_visible?: boolean | null;
  panel_visible?: boolean | null;
  auxiliary_tab?: ("changes" | "files" | "automation" | "resources" | "debug") | null;
  auxiliary_tab_order?:
    | [
        "changes" | "files" | "automation" | "resources" | "debug",
        "changes" | "files" | "automation" | "resources" | "debug",
        "changes" | "files" | "automation" | "resources" | "debug",
        "changes" | "files" | "automation" | "resources" | "debug"
      ]
    | [
        "changes" | "files" | "automation" | "resources" | "debug",
        "changes" | "files" | "automation" | "resources" | "debug",
        "changes" | "files" | "automation" | "resources" | "debug",
        "changes" | "files" | "automation" | "resources" | "debug",
        "changes" | "files" | "automation" | "resources" | "debug"
      ]
    | null;
  main_area_ratios?: WebUIMainAreaRatiosDTO | null;
  bottom_panel_by_workspace?: {
    [k: string]: WebUIWorkspaceBottomPanelSettingsDTO;
  } | null;
  workspace_preview_visible?: boolean | null;
  workspace_preview_maximized?: boolean | null;
  workspace_preview_file_paths?:
    | []
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string]
    | [string, string, string, string, string, string]
    | [string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | null;
  workspace_preview_active_file_path?: string | null;
  customizations_collapsed?: boolean | null;
  customizations_height?: number | null;
  panel_height?: number | null;
  content_view?: ("default" | "events" | "requests" | "changes" | "resources" | "agent") | null;
  delivery_policy_default?: ("after_turn" | "after_tool_result" | "after_interrupt") | null;
}
export interface WebUIMainAreaRatiosDTO {
  agent_sessions?: number;
  chat?: number;
  workspace_preview?: number;
  auxiliary?: number;
}
export interface WebUIWorkspaceBottomPanelSettingsDTO {
  visible?: boolean | null;
  height?: number | null;
  tab?: ("terminal" | "output" | "gateway" | "ports" | "automation") | null;
  terminal_id?: string | null;
}
export interface WebUISessionSidebarSettingsDTO {
  filter_mode?: "all" | "current" | "attachments" | "agent" | "named";
  sort_mode?: "created" | "updated";
  grouping_mode?: "workspace" | "time";
  workspace_group_capped?: boolean;
  /**
   * @maxItems 1000
   */
  collapsed_workspace_ids?: string[];
  /**
   * @maxItems 5000
   */
  collapsed_session_ids?: string[];
  /**
   * @maxItems 1000
   */
  expanded_root_tree_ids?: string[];
  /**
   * @maxItems 1000
   */
  collapsed_section_ids?: string[];
}
export interface WebUISettingsDTO {
  layout?: WebUILayoutSettingsDTO;
  session_sidebar?: WebUISessionSidebarSettingsDTO;
  workspace_file_tree?: WebUIWorkspaceFileTreeSettingsDTO;
  gateway_console?: WebUIGatewayConsoleSettingsDTO;
  theme?: WebUIThemeSettingsDTO;
  recent_local_workspace_paths?: string[];
}
export interface WebUIWorkspaceFileTreeSettingsDTO {
  expanded_paths_by_workspace?: {
    [k: string]: string[];
  };
}
export interface WebUIThemeSettingsDTO {
  theme_id?: string | null;
  background?: GatewayThemeBackgroundDTO | null;
  resolved_theme?: ResolvedGatewayThemeDTO | null;
}
export interface WebUISettingsUpdateDTO {
  layout?: WebUILayoutSettingsDTO | null;
  session_sidebar?: WebUISessionSidebarSettingsDTO | null;
  workspace_file_tree?: WebUIWorkspaceFileTreeSettingsDTO | null;
  gateway_console?: WebUIGatewayConsoleSettingsDTO | null;
  theme?: WebUIThemeSettingsUpdateDTO | null;
  recent_local_workspace_paths?: string[] | null;
}
export interface WebUIThemeSettingsUpdateDTO {
  theme_id?: string | null;
  background?: GatewayThemeBackgroundDTO | null;
}
