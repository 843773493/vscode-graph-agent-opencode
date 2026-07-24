import { resolveAgentSessionsPreferences, stableUiSettingIds } from "../uiSettings/preferences";
import type { WebUiSettings } from "../../types/backend";

const settings: WebUiSettings = {
  layout: {},
  session_sidebar: {
    filter_mode: "attachments",
    sort_mode: "created",
    grouping_mode: "time",
    workspace_group_capped: false,
    collapsed_workspace_ids: ["workspace-b", "workspace-a", "workspace-b"],
    collapsed_session_ids: ["session-b"],
    expanded_root_tree_ids: ["workspace:workspace-a"],
    collapsed_section_ids: ["time:older"],
  },
  workspace_file_tree: { expanded_paths_by_workspace: {} },
  gateway_console: { view: "managed" },
  recent_local_workspace_paths: [],
};

const preferences = resolveAgentSessionsPreferences(settings);
if (preferences.collapsedWorkspaceIds.join(",") !== "workspace-a,workspace-b") {
  throw new Error("工作区折叠 ID 未去重并稳定排序");
}
if (
  preferences.filterMode !== "attachments"
  || preferences.sortMode !== "created"
  || preferences.groupingMode !== "time"
  || preferences.workspaceGroupCapped
) {
  throw new Error("会话侧栏偏好解析错误");
}
if (stableUiSettingIds(["b", "a", "b"]).join(",") !== "a,b") {
  throw new Error("UI 设置 ID 归一化错误");
}
