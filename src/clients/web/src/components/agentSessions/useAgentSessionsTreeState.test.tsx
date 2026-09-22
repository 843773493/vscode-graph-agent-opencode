import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { WebUiSessionSidebarSettings } from "../../types/backend";
import type { AgentSessionsPreferences } from "../../state/uiSettings/preferences";
import { useAgentSessionsTreeState } from "./useAgentSessionsTreeState";

function preferences(
  overrides: Partial<AgentSessionsPreferences> = {},
): AgentSessionsPreferences {
  return {
    filterMode: "all",
    sortMode: "updated",
    groupingMode: "workspace",
    workspaceGroupCapped: true,
    collapsedWorkspaceIds: [],
    collapsedSessionIds: [],
    expandedRootTreeIds: [],
    collapsedSectionIds: [],
    ...overrides,
  };
}

interface MountedHook {
  hook: ReturnType<typeof useAgentSessionsTreeState>;
  writes: Array<Partial<WebUiSessionSidebarSettings>>;
}

function mountHook(
  current: AgentSessionsPreferences,
): MountedHook {
  const writes: Array<Partial<WebUiSessionSidebarSettings>> = [];
  let hook: ReturnType<typeof useAgentSessionsTreeState> | undefined;
  const settings: WebUiSessionSidebarSettings = {
    filter_mode: current.filterMode,
    sort_mode: current.sortMode,
    grouping_mode: current.groupingMode,
    workspace_group_capped: current.workspaceGroupCapped,
    collapsed_workspace_ids: [...current.collapsedWorkspaceIds],
    collapsed_session_ids: [...current.collapsedSessionIds],
    expanded_root_tree_ids: [...current.expandedRootTreeIds],
    collapsed_section_ids: [...current.collapsedSectionIds],
  };

  function Probe(): React.ReactNode {
    hook = useAgentSessionsTreeState({
      preferences: current,
      onPreferencesChange: (updater) => {
        writes.push(updater(settings));
      },
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  act(() => {
    renderer = create(<Probe />);
  });
  renderer!.unmount();
  return { hook: hook!, writes };
}

describe("会话树折叠状态", () => {
  test("切换会话折叠后再经唯一归一实现去重排序", () => {
    const mounted = mountHook(preferences({
      collapsedSessionIds: ["session-z", "session-a", "session-z"],
    }));

    // 未折叠的 session-b：切换后加入，并经 stableUiSettingIds 去重 + 排序。
    act(() => mounted.hook.toggleSession("session-b"));
    expect(mounted.writes[0]?.collapsed_session_ids).toEqual([
      "session-a",
      "session-b",
      "session-z",
    ]);

    // 已折叠的 session-a：切换后移除，归一结果同样稳定有序。
    act(() => mounted.hook.toggleSession("session-a"));
    expect(mounted.writes[1]?.collapsed_session_ids).toEqual([
      "session-z",
    ]);
  });

  test("工作区与根列表折叠走同一套唯一切换实现", () => {
    const mounted = mountHook(preferences());

    act(() => mounted.hook.toggleWorkspace("workspace-b"));
    expect(mounted.writes[0]?.collapsed_workspace_ids).toEqual(["workspace-b"]);

    act(() => mounted.hook.toggleRootList("workspace:workspace-a"));
    expect(mounted.writes[1]?.expanded_root_tree_ids).toEqual([
      "workspace:workspace-a",
    ]);

    // expandWorkspace 是「显式展开」，必须从折叠集合中移除且不新增其它 id。
    act(() => mounted.hook.expandWorkspace("workspace-b"));
    expect(mounted.writes[2]?.collapsed_workspace_ids).toEqual([]);
  });
});
