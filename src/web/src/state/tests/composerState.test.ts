import { describe, expect, test } from "bun:test";
import type { AppState } from "../../types/frontend";
import {
  reuseComposerStateSnapshot,
  selectComposerState,
} from "../composerState";

function state(): AppState {
  return {
    apiPort: 8014,
    gatewayWorkspaces: [],
    activeGatewayWorkspaceId: "workspace",
    workspaceSwitching: false,
    uiSettings: {
      layout: {},
      session_sidebar: {
        filter_mode: "all",
        sort_mode: "updated",
        grouping_mode: "workspace",
        workspace_group_capped: false,
        collapsed_workspace_ids: [],
        collapsed_session_ids: [],
        expanded_root_tree_ids: [],
        collapsed_section_ids: [],
      },
      workspace_file_tree: { expanded_paths_by_workspace: {} },
      gateway_console: { view: "routing" },
      recent_local_workspace_paths: [],
    },
    agents: [],
    currentSession: {
      session_id: "session",
      workspace_id: "workspace",
      title: "长会话",
      current_agent_id: "default",
      created_at: "2026-07-28T00:00:00Z",
      updated_at: "2026-07-28T00:00:00Z",
    },
    currentSessionWorkspaceId: "workspace",
    contentView: "default",
    currentGoal: null,
    currentGoalSessionId: null,
    goalLoading: false,
    goalError: null,
    compactLoading: false,
    pendingConversations: new Map(),
    activeJobIdsBySession: new Map(),
    turnTimelinesBySession: new Map(),
    traceEvents: [],
  } as unknown as AppState;
}

describe("Composer 独立状态快照", () => {
  test("Turn 和 Trace delta 不改变 Composer 订阅快照身份", () => {
    const initial = state();
    const first = reuseComposerStateSnapshot(
      null,
      selectComposerState(initial, "workspace::session"),
    );
    const timelineUpdated = {
      ...initial,
      traceEvents: [{ event_id: "delta" }],
      turnTimelinesBySession: new Map([
        ["workspace::session", { phase: "ready" }],
      ]),
    } as AppState;
    const second = reuseComposerStateSnapshot(
      first,
      selectComposerState(timelineUpdated, "workspace::session"),
    );

    expect(second).toBe(first);
  });

  test("活动 Job 控制状态变化会产生新快照", () => {
    const initial = state();
    const first = selectComposerState(initial, "workspace::session");
    const active = {
      ...initial,
      activeJobIdsBySession: new Map([["workspace::session", "job_1"]]),
    } as AppState;
    const second = reuseComposerStateSnapshot(
      first,
      selectComposerState(active, "workspace::session"),
    );

    expect(second).not.toBe(first);
    expect(second.currentActiveJobId).toBe("job_1");
  });
});
