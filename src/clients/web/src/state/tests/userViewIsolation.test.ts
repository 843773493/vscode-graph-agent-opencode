import { describe, expect, test } from "bun:test";
import type { GatewayUserViewState } from "../../types/backend";
import {
  normalizeWebUiSettings,
  resolveAgentSessionsPreferences,
} from "../uiSettings/preferences";
import { composerDraftScopeKey } from "../composerDrafts/storage";
import { cloneMaps } from "../appStateMaps";
import {
  createSessionTurnTimeline,
  writeTurnTimelineCache,
} from "../session/turnTimeline";
import { sessionScopeKey } from "../session/sessionScope";
import { selectBootstrapSessionId } from "../../hooks/useWorkspaceBootstrap";
import type { AppState } from "../../types/frontend";

const workspaceId = "workspace-a";
const sessionId = "session-a";

function viewState(
  userId: string,
  anchor: string,
  offset: number,
): GatewayUserViewState {
  return {
    user_id: userId,
    workspace_id: workspaceId,
    session_id: sessionId,
    turn_anchor: anchor,
    scroll_offset: offset,
    follow_latest: false,
    projection_version: 1,
    tool_details_expanded: false,
    updated_at: "2026-08-16T00:00:00Z",
  };
}

function minimalState(): AppState {
  return {
    gatewayUserViewStates: new Map([
      [sessionScopeKey(workspaceId, sessionId), viewState("user-a", "turn-a", 12)],
    ]),
    turnTimelinesBySession: new Map([
      [sessionScopeKey(workspaceId, sessionId), createSessionTurnTimeline(sessionScopeKey(workspaceId, sessionId))],
    ]),
  } as unknown as AppState;
}

describe("用户视图隔离", () => {
  test("主题、布局和侧栏设置来自当前用户而不是共享浏览器缓存", () => {
    const userA = normalizeWebUiSettings({
      layout: { content_view: "changes", agent_sessions_panel_open: false },
      theme: { theme_id: "dark", background: null, resolved_theme: null },
      session_sidebar: { filter_mode: "attachments" },
    });
    const userB = normalizeWebUiSettings({
      layout: { content_view: "default", agent_sessions_panel_open: true },
      theme: { theme_id: "warm", background: null, resolved_theme: null },
      session_sidebar: { filter_mode: "all" },
    });

    expect(userA.theme.theme_id).toBe("dark");
    expect(userB.theme.theme_id).toBe("warm");
    expect(userA.layout.content_view).toBe("changes");
    expect(userB.layout.content_view).toBe("default");
    expect(resolveAgentSessionsPreferences(userA).filterMode).toBe("attachments");
    expect(resolveAgentSessionsPreferences(userB).filterMode).toBe("all");
  });

  test("同一会话的草稿和历史缓存必须带用户作用域", () => {
    expect(composerDraftScopeKey(workspaceId, sessionId, "user:user-a")).not.toBe(
      composerDraftScopeKey(workspaceId, sessionId, "user:user-b"),
    );
    expect(composerDraftScopeKey(workspaceId, sessionId, null)).toBeNull();

    const scope = sessionScopeKey(workspaceId, sessionId);
    const timelines = writeTurnTimelineCache(
      new Map(),
      scope,
      { ...createSessionTurnTimeline(scope), olderCursor: "cursor-user-a", hasMore: true },
    );
    const userBTimelines = new Map<string, ReturnType<typeof createSessionTurnTimeline>>();
    expect(timelines.get(scope)?.olderCursor).toBe("cursor-user-a");
    expect(userBTimelines.has(scope)).toBe(false);
  });

  test("用户切换清空旧用户会话、历史游标和滚动锚点，随后只接受新用户状态", () => {
    const before = minimalState();
    const afterSwitch = cloneMaps({
      ...before,
      gatewayUserViewStates: new Map(),
      turnTimelinesBySession: new Map(),
    });
    expect(afterSwitch.gatewayUserViewStates.size).toBe(0);
    expect(afterSwitch.turnTimelinesBySession.size).toBe(0);

    const scope = sessionScopeKey(workspaceId, sessionId);
    const userBState = viewState("user-b", "turn-b", 48);
    afterSwitch.gatewayUserViewStates.set(scope, userBState);
    expect(afterSwitch.gatewayUserViewStates.get(scope)).toEqual(userBState);
    expect(afterSwitch.gatewayUserViewStates.get(scope)?.turn_anchor).not.toBe("turn-a");
    expect(afterSwitch.gatewayUserViewStates.get(scope)?.scroll_offset).toBe(48);
  });

  test("用户切换时优先恢复该用户服务器保存的当前会话", () => {
    expect(selectBootstrapSessionId({
      persistedSessionId: "session-b",
      previousSessionId: "session-a",
      userChanged: true,
    })).toBe("session-b");
    expect(selectBootstrapSessionId({
      previousSessionId: "session-a",
      userChanged: true,
    })).toBeNull();
  });
});
