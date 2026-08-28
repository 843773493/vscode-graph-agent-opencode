import type {
  WebUiSessionSidebarSettings,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "../../types/backend";

export function createDefaultWebUiSettings(): WebUiSettings {
  return {
    layout: {},
    session_sidebar: {
      filter_mode: "all",
      sort_mode: "updated",
      grouping_mode: "workspace",
      workspace_group_capped: true,
      collapsed_workspace_ids: [],
      collapsed_session_ids: [],
      expanded_root_tree_ids: [],
      collapsed_section_ids: [],
    },
    workspace_file_tree: { expanded_paths_by_workspace: {} },
    gateway_console: { view: "routing" },
    theme: { theme_id: "warm", background: null, resolved_theme: null },
    recent_local_workspace_paths: [],
  };
}

export function normalizeWebUiSettings(
  value: WebUiSettingsUpdate,
): WebUiSettings {
  const defaults = createDefaultWebUiSettings();
  return {
    layout: value.layout ?? defaults.layout,
    session_sidebar: {
      ...defaults.session_sidebar,
      ...value.session_sidebar,
    },
    workspace_file_tree: {
      ...defaults.workspace_file_tree,
      ...value.workspace_file_tree,
    },
    gateway_console: {
      ...defaults.gateway_console,
      ...value.gateway_console,
    },
    theme: {
      ...defaults.theme,
      ...value.theme,
    },
    recent_local_workspace_paths: Array.isArray(value.recent_local_workspace_paths)
      ? value.recent_local_workspace_paths
      : [],
  };
}

export interface AgentSessionsPreferences {
  filterMode: WebUiSessionSidebarSettings["filter_mode"];
  sortMode: WebUiSessionSidebarSettings["sort_mode"];
  groupingMode: WebUiSessionSidebarSettings["grouping_mode"];
  workspaceGroupCapped: boolean;
  collapsedWorkspaceIds: string[];
  collapsedSessionIds: string[];
  expandedRootTreeIds: string[];
  collapsedSectionIds: string[];
}

function stableUniqueStrings(values: string[]): string[] {
  return [...new Set(values)].sort((left, right) => left.localeCompare(right));
}

export function resolveAgentSessionsPreferences(
  settings: WebUiSettings,
): AgentSessionsPreferences {
  const sidebar = settings.session_sidebar;
  return {
    filterMode: sidebar.filter_mode,
    sortMode: sidebar.sort_mode,
    groupingMode: sidebar.grouping_mode,
    workspaceGroupCapped: sidebar.workspace_group_capped,
    collapsedWorkspaceIds: stableUniqueStrings(sidebar.collapsed_workspace_ids),
    collapsedSessionIds: stableUniqueStrings(sidebar.collapsed_session_ids),
    expandedRootTreeIds: stableUniqueStrings(sidebar.expanded_root_tree_ids),
    collapsedSectionIds: stableUniqueStrings(sidebar.collapsed_section_ids),
  };
}

export function stableUiSettingIds(values: Iterable<string>): string[] {
  return stableUniqueStrings([...values]);
}
