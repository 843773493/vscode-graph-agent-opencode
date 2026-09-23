import { test } from "bun:test";
import {
  createDefaultWebUiSettings,
  mergeGuestWebUiSettings,
} from "./preferences";
import type { WebUiSettings, WebUiSettingsUpdate } from "../../types/backend";

const current: WebUiSettings = {
  ...createDefaultWebUiSettings(),
  layout: {
    main_area_ratios: {
      agent_sessions: 1.2,
      chat: 1.8,
      workspace_preview: 0.7,
      auxiliary: 0.9,
    },
    auxiliary_visible: true,
    bottom_panel_by_workspace: {
      "workspace-a": {
        visible: true,
        height: 340,
        tab: "output",
        terminal_id: null,
      },
    },
  },
  session_sidebar: {
    ...createDefaultWebUiSettings().session_sidebar,
    filter_mode: "attachments",
  },
  workspace_file_tree: {
    expanded_paths_by_workspace: {
      "workspace-a": ["", "src"],
    },
  },
  recent_local_workspace_paths: ["/workspace-a"],
};

const returned: WebUiSettings = {
  ...createDefaultWebUiSettings(),
  layout: {
    main_area_ratios: {
      agent_sessions: 1.4,
      chat: 2.1,
      workspace_preview: 0.6,
      auxiliary: 0.8,
    },
  },
  session_sidebar: {
    ...createDefaultWebUiSettings().session_sidebar,
    sort_mode: "created",
  },
  recent_local_workspace_paths: ["/workspace-b"],
};

const patch: WebUiSettingsUpdate = {
  layout: { main_area_ratios: returned.layout.main_area_ratios },
  session_sidebar: { sort_mode: "created" },
  recent_local_workspace_paths: ["/workspace-b"],
};

test("游客设置更新保留未参与本次补丁的页面内存状态", () => {
  const merged = mergeGuestWebUiSettings(current, returned, patch);

  if (JSON.stringify(merged.layout.main_area_ratios) !== JSON.stringify(returned.layout.main_area_ratios)) {
    throw new Error("游客布局比例没有应用本次 Gateway 返回的归一化值");
  }
  if (merged.layout.auxiliary_visible !== true) {
    throw new Error("游客布局更新不应清除未参与更新的布局设置");
  }
  if (merged.layout.bottom_panel_by_workspace?.["workspace-a"]?.height !== 340) {
    throw new Error("游客布局更新不应清除其他工作区的底部面板状态");
  }
  if (merged.session_sidebar.filter_mode !== "attachments") {
    throw new Error("游客侧栏更新不应清除未参与更新的侧栏偏好");
  }
  if (merged.session_sidebar.sort_mode !== "created") {
    throw new Error("游客侧栏更新没有应用本次返回的偏好");
  }
  if (merged.workspace_file_tree.expanded_paths_by_workspace["workspace-a"]?.join(",") !== ",src") {
    throw new Error("游客布局更新不应清除文件树展开状态");
  }
  if (merged.recent_local_workspace_paths.join(",") !== "/workspace-b") {
    throw new Error("游客最近工作区路径没有应用本次返回值");
  }
});
