// Gateway JSON 类型适配层。
// 字段结构来自 gateway/v1/public.proto；这里补回 JSON API 的 null 约定和字符串枚举。
import type * as GatewayProtocol from "./protocol_generated/boxteam/gateway/v1/public";

type OptionalFields<T> = T extends readonly (infer Item)[]
  ? OptionalFields<Item>[]
  : T extends object
    ? { [Key in keyof T]: undefined extends T[Key]
      ? OptionalFields<Exclude<T[Key], undefined>> | undefined
      : OptionalFields<T[Key]> }
    : T;

export type ActivateGatewayWorkspaceResultDTO = OptionalFields<GatewayProtocol.ActivateGatewayWorkspaceResultDTO>;
export type AddLocalWorkspaceRequest = OptionalFields<GatewayProtocol.AddLocalWorkspaceRequest>;
export type AddRemoteGatewayRequest = OptionalFields<GatewayProtocol.AddRemoteGatewayRequest>;
export type CreateFederationManagedWorkspaceRequest = OptionalFields<GatewayProtocol.CreateFederationManagedWorkspaceRequest>;
export type CreateGatewayGuestRequest = OptionalFields<GatewayProtocol.CreateGatewayGuestRequest>;
export type CreateGatewayManagedWorkspaceRequest = Omit<
  OptionalFields<GatewayProtocol.CreateGatewayManagedWorkspaceRequest>,
  "name" | "gateway_connection_id"
> & {
  name?: string | null;
  gateway_connection_id?: string | null;
};
export type CreateGatewayUserRequest = OptionalFields<GatewayProtocol.CreateGatewayUserRequest>;
export type AcquireGatewayUserRequest = Omit<OptionalFields<GatewayProtocol.AcquireGatewayUserRequest>, "client_label"> & {
  client_label?: string | null;
};
export type CreatePortForwardRequest = Omit<OptionalFields<GatewayProtocol.CreatePortForwardRequest>, "local_port" | "label"> & {
  local_port?: number | null;
  label?: string | null;
};
export type ChangePortForwardLocalPortRequest = OptionalFields<GatewayProtocol.ChangePortForwardLocalPortRequest>;
export type ChangePortForwardLabelRequest = Omit<OptionalFields<GatewayProtocol.ChangePortForwardLabelRequest>, "label"> & {
  label?: string | null;
};
export type DevelopmentRuntimeRestartDTO = OptionalFields<GatewayProtocol.DevelopmentRuntimeRestartDTO>;
export type GatewayConfigSourceDTO = OptionalFields<GatewayProtocol.GatewayConfigSourceDTO>;
export type GatewayConfigSourcesDTO = OptionalFields<GatewayProtocol.GatewayConfigSourcesDTO>;
export type GatewayDiagnosticLogDTO = OptionalFields<GatewayProtocol.GatewayDiagnosticLogDTO>;
export type GatewayDiagnosticWorkspaceDTO = OptionalFields<GatewayProtocol.GatewayDiagnosticWorkspaceDTO>;
export type GatewayDiagnosticsDTO = OptionalFields<GatewayProtocol.GatewayDiagnosticsDTO>;
export type GatewayDirectoryEntryDTO = OptionalFields<GatewayProtocol.GatewayDirectoryEntryDTO>;
export type GatewayDirectoryListDTO = OptionalFields<GatewayProtocol.GatewayDirectoryListDTO>;
export type GatewayHealthDTO = OptionalFields<GatewayProtocol.GatewayHealthDTO>;
export type GatewayInboundAccessListDTO = OptionalFields<GatewayProtocol.GatewayInboundAccessListDTO>;
export type GatewayInboundPeerDTO = OptionalFields<GatewayProtocol.GatewayInboundPeerDTO>;
export type GatewayInboundWorkspaceDTO = OptionalFields<GatewayProtocol.GatewayInboundWorkspaceDTO>;
export type GatewayManagedWorkspaceDTO = OptionalFields<GatewayProtocol.GatewayManagedWorkspaceDTO>;
export type GatewayManagedWorkspaceListDTO = OptionalFields<GatewayProtocol.GatewayManagedWorkspaceListDTO>;
export type GatewayRemoteConnectionSummaryDTO = Omit<OptionalFields<GatewayProtocol.GatewayRemoteConnectionSummaryDTO>, "ssh_config_host"> & {
  ssh_config_host?: string | null;
};
export type GatewayRuntimeBlockerDTO = OptionalFields<GatewayProtocol.GatewayRuntimeBlockerDTO>;
export type GatewayRuntimeRestartResultDTO = OptionalFields<GatewayProtocol.GatewayRuntimeRestartResultDTO>;
export type GatewayRuntimeStateResultDTO = OptionalFields<GatewayProtocol.GatewayRuntimeStateResultDTO>;
export type GatewayServiceStatusDTO = OptionalFields<GatewayProtocol.GatewayServiceStatusDTO>;
export type GatewayThemeBackgroundDTO = Omit<OptionalFields<GatewayProtocol.GatewayThemeBackgroundDTO>, "type" | "url" | "asset_id" | "repeat" | "appearance"> & {
  type: "remote" | "gateway_asset";
  url?: string | null;
  asset_id?: string | null;
  repeat?: "no-repeat" | "repeat" | "repeat-x" | "repeat-y" | "space" | "round";
  appearance?: "immersive" | "theme";
};
export type GatewayThemeCatalogDTO = Omit<OptionalFields<GatewayProtocol.GatewayThemeCatalogDTO>, "items" | "current_theme"> & {
  items: GatewayThemeOptionDTO[];
  current_theme: ResolvedGatewayThemeDTO;
};
export type GatewayThemeOptionDTO = Omit<OptionalFields<GatewayProtocol.GatewayThemeOptionDTO>, "extends" | "source" | "preview_tokens" | "background_image_url"> & {
  extends: "warm" | "green" | "blue";
  source: "builtin" | "gateway_config";
  preview_tokens: Record<string, string>;
  background_image_url?: string | null;
};
export type GatewayUIAssetDTO = OptionalFields<GatewayProtocol.GatewayUIAssetDTO>;
export type GatewayUIAssetListDTO = Omit<OptionalFields<GatewayProtocol.GatewayUIAssetListDTO>, "items"> & {
  items?: GatewayUIAssetDTO[];
};
export type GatewayUserAccessDTO = OptionalFields<GatewayProtocol.GatewayUserAccessDTO>;
export type GatewayUserDTO = OptionalFields<GatewayProtocol.GatewayUserDTO>;
export type GatewayUserLeaseDTO = OptionalFields<GatewayProtocol.GatewayUserLeaseDTO>;
export type GatewayUserListDTO = OptionalFields<GatewayProtocol.GatewayUserListDTO>;
export type GatewayUserViewStateDTO = OptionalFields<GatewayProtocol.GatewayUserViewStateDTO>;
export type GatewayUserViewStateUpdateRequest = Omit<OptionalFields<GatewayProtocol.GatewayUserViewStateUpdateRequest>, "turn_anchor"> & {
  turn_anchor?: string | null;
};
export type GatewayWorkspaceDTO = Omit<OptionalFields<GatewayProtocol.GatewayWorkspaceDTO>, "parent_workspace_id" | "connection_kind" | "status" | "runtime_action" | "remote"> & {
  parent_workspace_id?: string | null;
  connection_kind: "local" | "remote_gateway";
  status: "ready" | "offline";
  runtime_action: "start_managed_backend" | "safe_restart_managed_backend" | "force_restart_managed_backend" | "reconnect_remote_gateway" | "probe_external_backend";
  remote?: GatewayRemoteConnectionSummaryDTO | null;
};
export type GatewayWorkspaceListDTO = Omit<OptionalFields<GatewayProtocol.GatewayWorkspaceListDTO>, "active_workspace_id" | "items"> & {
  active_workspace_id?: string | null;
  items?: GatewayWorkspaceDTO[];
};
export type GatewayConfigReloadStatusDTO = Omit<OptionalFields<GatewayProtocol.GatewayConfigReloadStatusDTO>, "reason"> & {
  reason?: "invalid_config" | "restart_required" | "apply_failed" | null;
};
export type PortForwardDTO = Omit<OptionalFields<GatewayProtocol.PortForwardDTO>, "remote_host" | "local_host" | "protocol" | "label" | "status" | "error" | "local_url"> & {
  remote_host: "127.0.0.1";
  local_host: "127.0.0.1";
  protocol: "http" | "https" | "tcp";
  label: string | null;
  status: "starting" | "active" | "error" | "stopped";
  error: string | null;
  local_url: string | null;
};
export type PortForwardListDTO = Omit<OptionalFields<GatewayProtocol.PortForwardListDTO>, "items"> & {
  items: PortForwardDTO[];
};
export type ReorderGatewayWorkspacesRequest = OptionalFields<GatewayProtocol.ReorderGatewayWorkspacesRequest>;
export type ResolvedGatewayThemeDTO = Omit<OptionalFields<GatewayProtocol.ResolvedGatewayThemeDTO>, "color_scheme" | "tokens" | "background_image_url"> & {
  color_scheme: "light" | "dark";
  tokens: Record<string, string>;
  background_image_url?: string | null;
};
export type SshConnectionOptionDTO = OptionalFields<GatewayProtocol.SshConnectionOptionDTO>;
export type SshConnectionOptionListDTO = OptionalFields<GatewayProtocol.SshConnectionOptionListDTO>;
export type UpdateGatewayWorkspaceRequest = Omit<OptionalFields<GatewayProtocol.UpdateGatewayWorkspaceRequest>, "name" | "parent_workspace_id"> & {
  name?: string | null;
  parent_workspace_id?: string | null;
};
export type WebUIGatewayConsoleSettingsDTO = OptionalFields<GatewayProtocol.WebUIGatewayConsoleSettingsDTO>;
export type WebUIMainAreaRatiosDTO = OptionalFields<GatewayProtocol.WebUIMainAreaRatiosDTO>;
export type WebUIWorkspaceBottomPanelSettingsDTO = Omit<
  OptionalFields<GatewayProtocol.WebUIWorkspaceBottomPanelSettingsDTO>,
  "tab" | "terminal_id"
> & {
  tab?: "terminal" | "output" | "gateway" | "ports" | "automation";
  terminal_id?: string | null;
};
export type WebUILayoutSettingsDTO = Omit<
  OptionalFields<GatewayProtocol.WebUILayoutSettingsDTO>,
  | "workbench_view"
  | "auxiliary_tab"
  | "auxiliary_tab_order"
  | "main_area_ratios"
  | "bottom_panel_by_workspace"
  | "workspace_preview_file_paths"
  | "workspace_preview_active_file_path"
  | "content_view"
  | "delivery_policy_default"
> & {
  workbench_view?: "sessions" | "gateway" | null;
  auxiliary_tab?: "changes" | "files" | "automation" | "resources" | "debug" | null;
  auxiliary_tab_order?: Array<"changes" | "files" | "automation" | "resources" | "debug"> | null;
  main_area_ratios?: WebUIMainAreaRatiosDTO | null;
  bottom_panel_by_workspace?: Record<string, WebUIWorkspaceBottomPanelSettingsDTO> | null;
  workspace_preview_file_paths?: string[] | null;
  workspace_preview_active_file_path?: string | null;
  content_view?: "default" | "events" | "requests" | "changes" | "resources" | "agent" | null;
  delivery_policy_default?: "after_turn" | "after_tool_result" | "after_interrupt" | null;
};
export type WebUISessionSidebarSettingsDTO = Omit<
  OptionalFields<GatewayProtocol.WebUISessionSidebarSettingsDTO>,
  "filter_mode" | "sort_mode" | "grouping_mode"
> & {
  filter_mode?: "all" | "current" | "attachments" | "agent" | "named";
  sort_mode?: "created" | "updated";
  grouping_mode?: "workspace" | "time";
};
export type WebUISettingsDTO = Omit<OptionalFields<GatewayProtocol.WebUISettingsDTO>, "recent_local_workspace_paths"> & {
  recent_local_workspace_paths: string[];
};
export type WebUISettingsUpdateDTO = Omit<OptionalFields<GatewayProtocol.WebUISettingsUpdateDTO>, "recent_local_workspace_paths"> & {
  recent_local_workspace_paths?: string[] | null;
};
export type WebUIThemeSettingsDTO = Omit<OptionalFields<GatewayProtocol.WebUIThemeSettingsDTO>, "background" | "resolved_theme"> & {
  background?: GatewayThemeBackgroundDTO | null;
  resolved_theme?: ResolvedGatewayThemeDTO | null;
};
export type WebUISettingsUpdate = WebUISettingsUpdateDTO;
export type WebUIThemeSettingsUpdateDTO = Omit<OptionalFields<GatewayProtocol.WebUIThemeSettingsUpdateDTO>, "background"> & {
  background?: GatewayThemeBackgroundDTO | null;
};
export type WebUIWorkspaceFileTreeSettingsDTO = OptionalFields<GatewayProtocol.WebUIWorkspaceFileTreeSettingsDTO>;

export type GatewayResourceDTO = OptionalFields<GatewayProtocol.GatewayResourceDTO>;
export type GatewayResourceListDTO = OptionalFields<GatewayProtocol.GatewayResourceListDTO>;
export type GatewayResourceScopeErrorDTO = OptionalFields<GatewayProtocol.GatewayResourceScopeErrorDTO>;
export type GatewaySessionSearchMatchDTO = Omit<OptionalFields<GatewayProtocol.GatewaySessionSearchMatchDTO>, "node_kind"> & {
  node_kind: "workspace_folder" | "workspace" | "folder" | "session";
};
export type GatewaySessionSearchResultsDTO = OptionalFields<GatewayProtocol.GatewaySessionSearchResultsDTO>;
export type GatewaySessionSearchWorkspaceStatusDTO = OptionalFields<GatewayProtocol.GatewaySessionSearchWorkspaceStatusDTO>;
export type GenerationOutputDTO = OptionalFields<GatewayProtocol.GenerationOutputDTO>;
export type GenerationRunDTO = OptionalFields<GatewayProtocol.GenerationRunDTO>;
export type GenerationRunListDTO = OptionalFields<GatewayProtocol.GenerationRunListDTO>;
export type GeneratorTypeRefDTO = GatewayProtocol.GeneratorTypeRefDTO;
export type GeneratorTriggerDTO = Omit<OptionalFields<GatewayProtocol.GeneratorTriggerDTO>, "type" | "expression" | "interval_seconds"> & {
  type?: "manual" | "cron" | "interval";
  expression?: string | null;
  interval_seconds?: number | null;
  timezone?: string;
};
export type GeneratorPlacementDTO = Omit<OptionalFields<GatewayProtocol.GeneratorPlacementDTO>, "kind" | "session_id" | "folder_id"> & {
  kind: "workspace" | "session" | "session_folder";
  session_id?: string | null;
  folder_id?: string | null;
};
export type GeneratorNamingDTO = Omit<OptionalFields<GatewayProtocol.GeneratorNamingDTO>, "path_template"> & {
  path_template?: string[];
};
export type GeneratorContextSourceDTO = Omit<OptionalFields<GatewayProtocol.GeneratorContextSourceDTO>, "kind" | "workspace_id" | "session_id" | "snapshot_id"> & {
  kind?: "fresh" | "live_session" | "snapshot";
  workspace_id?: string | null;
  session_id?: string | null;
  snapshot_id?: string | null;
};
export type GeneratorSessionStrategyDTO = Omit<OptionalFields<GatewayProtocol.GeneratorSessionStrategyDTO>, "mode" | "target" | "concurrency" | "report_back"> & {
  mode?: "new_per_run" | "continue_existing" | "fork_new_and_report_back";
  target?: SessionLocatorDTO | null;
  concurrency?: "queue";
  report_back?: "none" | "link" | "summary" | "summary_and_link" | "full" | "continue_agent";
};
export type GeneratorDefinitionCreateRequest = Omit<
  OptionalFields<GatewayProtocol.GeneratorDefinitionCreateRequest>,
  "generator_type" | "trigger" | "placement" | "context_source" | "created_from" | "naming" | "session_strategy"
> & {
  generator_type?: GeneratorTypeRefDTO;
  trigger?: GeneratorTriggerDTO;
  placement: GeneratorPlacementDTO;
  context_source?: GeneratorContextSourceDTO;
  created_from?: SessionLocatorDTO | null;
  naming?: GeneratorNamingDTO;
  session_strategy?: GeneratorSessionStrategyDTO;
  config?: Record<string, unknown>;
};
export type GeneratorDefinitionDTO = Omit<
  OptionalFields<GatewayProtocol.GeneratorDefinitionDTO>,
  "generator_type" | "trigger" | "placement" | "context_source" | "created_from" | "naming" | "session_strategy" | "status"
> & {
  generator_type?: GeneratorTypeRefDTO;
  trigger?: GeneratorTriggerDTO;
  placement: GeneratorPlacementDTO;
  context_source?: GeneratorContextSourceDTO;
  created_from?: SessionLocatorDTO | null;
  naming?: GeneratorNamingDTO;
  session_strategy?: GeneratorSessionStrategyDTO;
  status?: "ready" | "paused" | "blocked";
  config?: Record<string, unknown>;
};
export type GeneratorDefinitionListDTO = OptionalFields<GatewayProtocol.GeneratorDefinitionListDTO>;
export type GeneratorDefinitionUpdateRequest = Omit<
  OptionalFields<GatewayProtocol.GeneratorDefinitionUpdateRequest>,
  "trigger" | "placement" | "context_source" | "naming" | "session_strategy"
> & {
  trigger?: GeneratorTriggerDTO | null;
  placement?: GeneratorPlacementDTO | null;
  context_source?: GeneratorContextSourceDTO | null;
  naming?: GeneratorNamingDTO | null;
  session_strategy?: GeneratorSessionStrategyDTO | null;
  config?: Record<string, unknown> | null;
};
export type GeneratorManualRunRequest = OptionalFields<GatewayProtocol.GeneratorManualRunRequest>;
export type GeneratorPlacementPreviewDTO = Omit<OptionalFields<GatewayProtocol.GeneratorPlacementPreviewDTO>, "preview_kind"> & {
  preview_kind?: "logical_physical_path_template";
};
export type GeneratorPlacementPreviewRequest = Omit<
  OptionalFields<GatewayProtocol.GeneratorPlacementPreviewRequest>,
  "naming" | "generated_at" | "placement" | "session_strategy"
> & {
  naming: GeneratorNamingDTO;
  generated_at?: string | null;
  placement?: GeneratorPlacementDTO | null;
  session_strategy?: GeneratorSessionStrategyDTO;
};
export type SessionLocatorDTO = OptionalFields<GatewayProtocol.SessionLocatorDTO>;
export type WorkspaceNavigationNodeDTO = Omit<OptionalFields<GatewayProtocol.WorkspaceNavigationNodeDTO>, "kind" | "parent_node_id" | "workspace_id"> & {
  kind: "workspace_folder" | "workspace_ref";
  parent_node_id?: string | null;
  workspace_id?: string | null;
};
export type WorkspaceFolderCreateRequest = Omit<OptionalFields<GatewayProtocol.WorkspaceFolderCreateRequest>, "parent_node_id" | "position"> & {
  parent_node_id?: string | null;
  position?: number | null;
};
export type WorkspaceNavigationNodeUpdateRequest = Omit<OptionalFields<GatewayProtocol.WorkspaceNavigationNodeUpdateRequest>, "name" | "parent_node_id" | "position"> & {
  name?: string | null;
  parent_node_id?: string | null;
  position?: number | null;
};
export type WorkspaceNavigationPlacementRequest = Omit<OptionalFields<GatewayProtocol.WorkspaceNavigationPlacementRequest>, "parent_node_id" | "mode" | "target_node_id"> & {
  parent_node_id?: string | null;
  mode: "before" | "after" | "last";
  target_node_id?: string | null;
};
export type WorkspaceNavigationTreeDTO = OptionalFields<GatewayProtocol.WorkspaceNavigationTreeDTO>;
