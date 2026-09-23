import { describe, expect, test } from "bun:test";
import {
  normalizeContentView,
  normalizeExpandedPathsByWorkspace,
  normalizeWebUiSettings,
  resolveAgentSessionsPreferences,
  stableUiSettingIds,
  toggleUiSettingId,
} from "./preferences";
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


describe("文件树展开态归一化", () => {
  test("合法载荷去重、稳定排序并保持每个工作区独立", () => {
    expect(normalizeExpandedPathsByWorkspace({
      "workspace-b": ["src", "", "src"],
      "workspace-a": ["lib"],
    })).toEqual({
      "workspace-a": ["lib"],
      "workspace-b": ["", "src"],
    });
  });

  test("缺失或 null 归一为空对象而不是抛错", () => {
    expect(normalizeExpandedPathsByWorkspace(undefined)).toEqual({});
    expect(normalizeExpandedPathsByWorkspace(null)).toEqual({});
  });

  test("顶层不是对象时响亮失败", () => {
    expect(() => normalizeExpandedPathsByWorkspace("not-an-array"))
      .toThrow("expanded_paths_by_workspace 必须是「工作区 ID → 路径数组」的对象，实际收到 string");
    expect(() => normalizeExpandedPathsByWorkspace(["src"]))
      .toThrow("实际收到 数组");
    expect(() => normalizeExpandedPathsByWorkspace(42))
      .toThrow("实际收到 number");
  });

  test("单个工作区的值不是数组时响亮失败并指出工作区", () => {
    expect(() => normalizeExpandedPathsByWorkspace({ "ws-a": "src" }))
      .toThrow('expanded_paths_by_workspace["ws-a"] 必须是字符串数组，实际收到 string');
    expect(() => normalizeExpandedPathsByWorkspace({ "ws-a": { nested: ["src"] } }))
      .toThrow('expanded_paths_by_workspace["ws-a"] 必须是字符串数组，实际收到 object');
    expect(() => normalizeExpandedPathsByWorkspace({ "ws-a": null }))
      .toThrow("实际收到 null");
  });

  test("数组内含非字符串时响亮失败并指出元素类型", () => {
    expect(() => normalizeExpandedPathsByWorkspace({ "ws-a": [123] }))
      .toThrow('expanded_paths_by_workspace["ws-a"] 含非字符串元素，实际收到 number');
    expect(() => normalizeExpandedPathsByWorkspace({ "ws-a": [{}] }))
      .toThrow("含非字符串元素，实际收到 object");
    expect(() => normalizeExpandedPathsByWorkspace({ "ws-a": [null] }))
      .toThrow("含非字符串元素，实际收到 null");
    expect(() => normalizeExpandedPathsByWorkspace({ "ws-a": [["deep"]] }))
      .toThrow("含非字符串元素，实际收到 数组");
  });

  test("整个设置归一化会对损坏的展开态响亮失败而非产出虚假默认值", () => {
    expect(() => normalizeWebUiSettings({
      workspace_file_tree: {
        expanded_paths_by_workspace: { "ws-a": [1, 2] },
      },
    })).toThrow("含非字符串元素");
    // 缺失展开态时仍给出空对象，保持既有默认行为。
    expect(normalizeWebUiSettings({}).workspace_file_tree)
      .toEqual({ expanded_paths_by_workspace: {} });
  });
});

describe("会话内容视图归一化", () => {
  test("合法取值一律通过校验", () => {
    const views = ["default", "events", "requests", "changes", "resources", "agent"] as const;
    for (const view of views) {
      expect(() => normalizeContentView(view)).not.toThrow();
    }
  });

  test("缺失或 null 视为未设置，交由调用方沿用当前视图", () => {
    expect(() => normalizeContentView(undefined)).not.toThrow();
    expect(() => normalizeContentView(null)).not.toThrow();
  });

  test("未知取值响亮失败并列出合法取值", () => {
    // 未知视图若不报错，会被 ContentViewSlots 静默当成「默认视图」渲染。
    expect(() => normalizeContentView("timeline")).toThrow(
      'UI 设置 content_view 必须是以下之一：default、events、requests、changes、resources、agent，实际收到 "timeline"',
    );
    expect(() => normalizeContentView("")).toThrow("实际收到 \"\"");
    expect(() => normalizeContentView(42)).toThrow("实际收到 number");
    expect(() => normalizeContentView(["default"])).toThrow("实际收到 数组");
    expect(() => normalizeContentView({ view: "default" })).toThrow("实际收到 object");
  });

  test("整个设置归一化会校验 content_view，未知取值不再静默落成默认视图", () => {
    expect(normalizeWebUiSettings({
      layout: { content_view: "events" },
    }).layout.content_view).toBe("events");
    expect(() => normalizeWebUiSettings({
      layout: { content_view: "timeline" as never },
    })).toThrow("content_view 必须是以下之一");
    // 缺失 content_view 时沿用空 layout，保持既有默认行为。
    expect(normalizeWebUiSettings({}).layout.content_view).toBeUndefined();
  });
});
