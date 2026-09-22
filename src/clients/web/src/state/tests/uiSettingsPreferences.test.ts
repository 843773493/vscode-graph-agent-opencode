import { describe, expect, test } from "bun:test";
import {
  resolveAgentSessionsPreferences,
  stableUiSettingIds,
  toggleUiSettingId,
} from "../uiSettings/preferences";
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
  theme: { theme_id: "warm", background: null, resolved_theme: null },
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

describe("持久化 id 集合切换", () => {
  test("唯一实现按「存在则移除、不存在则加入」切换且不改动入参", () => {
    const source = ["workspace-a", "workspace-b"];

    // 已存在：移除。
    const removed = toggleUiSettingId(source, "workspace-a");
    expect([...removed].sort()).toEqual(["workspace-b"]);
    // 不存在：加入。
    const added = toggleUiSettingId(source, "workspace-c");
    expect([...added].sort()).toEqual([
      "workspace-a",
      "workspace-b",
      "workspace-c",
    ]);
    // 入参不得被就地修改（调用方依赖不可变语义）。
    expect(source).toEqual(["workspace-a", "workspace-b"]);

    // 与既有归一化组合：切换后经 stableUiSettingIds 去重排序。
    expect(stableUiSettingIds(toggleUiSettingId(source, "workspace-c"))).toEqual([
      "workspace-a",
      "workspace-b",
      "workspace-c",
    ]);
  });
});
