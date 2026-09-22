import type {
  WebUiSessionSidebarSettings,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "../../types/backend";

function describeSettingValue(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "数组";
  return typeof value;
}

/**
 * 归一 `workspace_file_tree.expanded_paths_by_workspace`：它来自 Gateway 用户
 * profile，是文件树展开态的唯一权威来源。非法载荷一旦被当成合法设置传下去，
 * 会让文件树在 `new Set(expandedPaths)` 处整块崩溃，因此这里按仓库既有约定
 * （参见 `api/workspaceFilesystem.ts` 的 items 校验）响亮失败，不静默收敛成空对象。
 */
export function normalizeExpandedPathsByWorkspace(
  value: unknown,
): Record<string, string[]> {
  if (value === null || value === undefined) {
    return {};
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new Error(
      `UI 设置 expanded_paths_by_workspace 必须是「工作区 ID → 路径数组」的对象，实际收到 ${describeSettingValue(value)}`,
    );
  }
  const normalized: Record<string, string[]> = {};
  for (const [workspaceId, paths] of Object.entries(value)) {
    if (!Array.isArray(paths)) {
      throw new Error(
        `UI 设置 expanded_paths_by_workspace["${workspaceId}"] 必须是字符串数组，实际收到 ${describeSettingValue(paths)}`,
      );
    }
    for (const path of paths) {
      if (typeof path !== "string") {
        throw new Error(
          `UI 设置 expanded_paths_by_workspace["${workspaceId}"] 含非字符串元素，实际收到 ${describeSettingValue(path)}`,
        );
      }
    }
    normalized[workspaceId] = [...new Set(paths)].sort();
  }
  return normalized;
}

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
      expanded_paths_by_workspace: normalizeExpandedPathsByWorkspace(
        value.workspace_file_tree?.expanded_paths_by_workspace,
      ),
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

function mergeReturnedSection<T extends object>(
  current: T,
  returned: T,
  patch: Partial<T> | null | undefined,
): T {
  if (patch === null || patch === undefined) {
    return current;
  }
  const merged = { ...current };
  for (const key of Object.keys(patch) as Array<keyof T>) {
    if (patch[key] !== undefined) {
      merged[key] = returned[key];
    }
  }
  return merged;
}

/**
 * 游客设置不写入 Gateway profile，因此要把接口返回的本次补丁合并回页面内存。
 * 返回值仍以 Gateway 的归一化结果为准，未参与本次更新的设置继续沿用当前页面状态。
 */
export function mergeGuestWebUiSettings(
  current: WebUiSettings,
  returned: WebUiSettings,
  patch: WebUiSettingsUpdate,
): WebUiSettings {
  const layout = mergeReturnedSection(current.layout, returned.layout, patch.layout);
  const theme = patch.theme === null || patch.theme === undefined
    ? current.theme
    : {
        ...mergeReturnedSection(current.theme, returned.theme, patch.theme),
        resolved_theme: returned.theme.resolved_theme,
      };
  return {
    ...current,
    layout,
    session_sidebar: mergeReturnedSection(
      current.session_sidebar,
      returned.session_sidebar,
      patch.session_sidebar,
    ),
    workspace_file_tree: mergeReturnedSection(
      current.workspace_file_tree,
      returned.workspace_file_tree,
      patch.workspace_file_tree,
    ),
    gateway_console: mergeReturnedSection(
      current.gateway_console,
      returned.gateway_console,
      patch.gateway_console,
    ),
    theme,
    recent_local_workspace_paths:
      patch.recent_local_workspace_paths === null
      || patch.recent_local_workspace_paths === undefined
        ? current.recent_local_workspace_paths
        : returned.recent_local_workspace_paths,
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

/** 持久化 id 集合的唯一切换实现：存在则移除，不存在则加入。 */
export function toggleUiSettingId(
  values: Iterable<string>,
  value: string,
): Set<string> {
  const next = new Set(values);
  if (next.has(value)) {
    next.delete(value);
  } else {
    next.add(value);
  }
  return next;
}
