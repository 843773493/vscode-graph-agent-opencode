import type {
  WebUiSessionSidebarSettings,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "../../types/backend";
import type { ConversationContentView } from "../../types/frontend";

function describeSettingValue(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "数组";
  return typeof value;
}

/**
 * 会话内容视图的完整取值集合，与 `types/frontend.ts` 的
 * `ConversationContentView` 一一对应。`ContentViewSlots` 只把 agent/events/requests
 * 当作检视槽，其余值一律落到会话记录槽；因此未知值会被静默当成「默认视图」，
 * 用户看到的视图与设置里的值不一致，却没有任何提示。
 */
export const CONVERSATION_CONTENT_VIEWS = [
  "default",
  "events",
  "requests",
  "changes",
  "resources",
  "agent",
] as const satisfies readonly ConversationContentView[];

/**
 * 归一 `layout.content_view`：它来自浏览器本地展示偏好。
 * 未知取值会被前端静默展示成「默认视图」，因此这里按仓库既有约定
 * （参见同文件的 `normalizeExpandedPathsByWorkspace`）响亮失败，不静默收敛。
 */
export function normalizeContentView(value: unknown): void {
  if (value === null || value === undefined) {
    return;
  }
  if (
    typeof value !== "string"
    || !(CONVERSATION_CONTENT_VIEWS as readonly string[]).includes(value)
  ) {
    const received =
      typeof value === "string" ? JSON.stringify(value) : describeSettingValue(value);
    throw new Error(
      `UI 设置 content_view 必须是以下之一：${CONVERSATION_CONTENT_VIEWS.join("、")}，实际收到 ${received}`,
    );
  }
}

/**
 * 归一 `workspace_file_tree.expanded_paths_by_workspace`：它来自浏览器本地展示偏好。
 * 非法载荷一旦被当成合法设置传下去，
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
  };
}

export function normalizeWebUiSettings(
  value: WebUiSettingsUpdate,
): WebUiSettings {
  const defaults = createDefaultWebUiSettings();
  const layout = value.layout ?? defaults.layout;
  normalizeContentView(layout.content_view);
  return {
    layout,
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
