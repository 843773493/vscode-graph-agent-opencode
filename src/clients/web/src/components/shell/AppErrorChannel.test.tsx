import { afterAll, afterEach, describe, expect, mock, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { AppState } from "../../types/frontend";
import type { ComposerContextType } from "../../hooks";
import { useSessionRunActions } from "../../hooks/session/useSessionRunActions";
import { selectComposerState } from "../../state/composerState";
import {
  installGatewayFetch,
  mountSessionRunActions,
  restoreSessionHookGlobals,
} from "../../hooks/session/sessionHookTestFixtures";

/**
 * AppState.error 是「工作区初始化失败」出口的专属状态，唯一设计场景是整个应用
 * 还没有任何会话的时候。轮次动作失败绝不能借用它 —— 借用会把有会话的聊天区整块
 * 替换成初始化失败大屏，错误归因也就完全错了。
 *
 * 本用例把真实链路串起来：真实 replayTurn 写出的 state → App 的真实外壳 → 真实
 * ContentViewSlots 渲染。只替身 useAppState（否则 App 会自己去拉后端）；Composer
 * 用真实实现挂上 ComposerContext，验证聊天区确实还在。
 */

const realHooks = await import("../../hooks");
const controlled: { state: AppState } = { state: null as unknown as AppState };

const noop = () => {};
const asyncNoop = async () => {};
const noopActions = {
  createSession: asyncNoop,
  selectSession: noop,
  openWorkspaceSession: asyncNoop,
  forkSessionContext: asyncNoop,
  renameSession: asyncNoop,
  setSessionParent: asyncNoop,
  deleteSession: asyncNoop,
  loadSessionChangesets: asyncNoop,
  refreshSessionChanges: asyncNoop,
  refreshSessionResources: asyncNoop,
  reviewSessionChangeFile: asyncNoop,
  switchContentView: noop,
  controlSessionResource: asyncNoop,
  toggleAgentSessionsPanel: noop,
  activateGatewayWorkspace: asyncNoop,
  refreshGatewayState: asyncNoop,
  reconnectGatewayWorkspace: asyncNoop,
  startManagedGatewayWorkspaceBackend: asyncNoop,
  stopManagedGatewayWorkspaceBackend: asyncNoop,
  addManagedGatewayWorkspace: asyncNoop,
  addSshGatewayWorkspace: asyncNoop,
  removeGatewayWorkspace: asyncNoop,
  renameGatewayWorkspace: asyncNoop,
  setGatewayWorkspaceParent: asyncNoop,
  refreshGatewayWorkspaceSessions: asyncNoop,
  copySessionInformation: asyncNoop,
  copyWorkspaceInformation: asyncNoop,
  updateUiSettings: asyncNoop,
  saveSessionViewState: asyncNoop,
  setStatus: noop,
  replayTurn: asyncNoop,
  updatePendingRequest: asyncNoop,
  removePendingRequest: asyncNoop,
  clearPendingRequests: asyncNoop,
  updatePendingRequestPolicy: asyncNoop,
  loadAroundTurn: asyncNoop,
  loadNewerMessages: asyncNoop,
  loadOlderMessages: asyncNoop,
  loadTurnDetails: asyncNoop,
  loadAgentStateMessageRawContent: async () => "",
  refreshTurnHistory: noop,
  loadOlderTraceHistory: asyncNoop,
  refreshTraceHistory: asyncNoop,
};

// 只在本用例注入了 state 时替身 useAppState；没有注入时必须与真实实现一样抛错，
// 这样同在 components/shell 下断言「Provider 之外必须抛错」的用例不受影响。
mock.module("../../hooks", () => ({
  ...realHooks,
  useAppState: () => {
    if (!controlled.state) {
      throw new Error("useAppState must be used within AppProvider");
    }
    return {
      ...noopActions,
      state: controlled.state,
    };
  },
}));

const { default: App } = await import("../../App");
const { default: WarmConfirmProvider } = await import("./WarmConfirmProvider");

afterAll(() => {
  mock.module("../../hooks", () => realHooks);
});
afterEach(() => {
  controlled.state = null as unknown as AppState;
  restoreSessionHookGlobals();
});

const WORKSPACE_ID = "ws_error_channel";
const SESSION_ID = "ses_error_channel";
const CACHE_KEY = WORKSPACE_ID + "::" + SESSION_ID;

function session() {
  return {
    session_id: SESSION_ID,
    workspace_id: WORKSPACE_ID,
    title: "错误通道",
    current_agent_id: "default",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function baseState(current: ReturnType<typeof session> | null): AppState {
  return {
    apiPort: 8014,
    currentSession: current,
    currentSessionWorkspaceId: current ? WORKSPACE_ID : null,
    activeGatewayWorkspaceId: current ? WORKSPACE_ID : null,
    contentView: "default",
    isBootstrapping: false,
    error: null,
    status: "",
    uiSettings: {
      layout: {},
      session_sidebar: {},
      workspace_file_tree: { expanded_paths_by_workspace: {} },
      gateway_console: { view: "routing" },
      recent_local_workspace_paths: [],
    },
    gatewayWorkspaces: [],
    agents: [],
    sessions: current ? [current] : [],
    sessionHistoryReloadNonce: 0,
    pendingConversations: new Map(),
    activeJobIdsBySession: new Map(),
    unreadSessionKeys: new Set(),
    sessionAttachmentSummaries: new Map(),
    eventQueuesBySession: new Map(),
    sessionsByWorkspace: new Map(),
    sessionGatewayWorkspaceById: new Map(),
    removingGatewayWorkspaceIds: new Set(),
    gatewayUserAccess: null,
    gatewayUserViewStates: new Map(),
    gatewayError: null,
    turnTimelinesBySession: new Map(),
    sessionTraceHistoryBySession: new Map(),
    traceEvents: [],
    llmRequestLogs: [],
    llmRequestLogsError: null,
    llmRequestLogsLoading: false,
    llmRequestLogsLoadedAt: null,
    sessionResources: [],
    sessionResourcesError: null,
    sessionResourcesLoading: false,
    sessionResourcesLoadedAt: null,
    sessionChangesets: [],
    selectedChangesetId: null,
    activeChangeset: null,
    sessionChangesLoading: false,
    sessionChangesError: null,
    sessionChangesLoadedAt: null,
    expandDetails: false,
    agentSessionsPanelOpen: false,
    agentStateJsonl: "",
    agentStateMessageCount: 0,
    agentStateLoading: false,
    agentStateError: null,
    currentGoal: null,
    currentGoalSessionId: null,
    workspaceName: "",
    workspaceRoot: "",
    uiSettingsLoaded: true,
  } as unknown as AppState;
}

const composerContextValue = {
  ...noopActions,
  getLatestAssistantContent: () => null,
  compactSession: asyncNoop,
  refreshGoal: asyncNoop,
  updateGoal: asyncNoop,
  clearGoal: asyncNoop,
  interruptSession: asyncNoop,
  switchAgent: asyncNoop,
  switchModel: asyncNoop,
  refreshAgents: asyncNoop,
  setWorkspaceDefaultAgent: asyncNoop,
  setWorkspaceDefaultProvider: asyncNoop,
} as unknown as Omit<ComposerContextType, "state">;

/** 把 state 交给真实 App 渲染，返回静态 HTML。 */
function renderApp(state: AppState): string {
  controlled.state = state;
  return renderToStaticMarkup(
    <WarmConfirmProvider>
      <realHooks.ComposerContext.Provider
        value={{
          ...composerContextValue,
          state: selectComposerState(
            state,
            state.currentSession
              ? WORKSPACE_ID + "::" + state.currentSession.session_id
              : null,
          ),
        } as ComposerContextType}
      >
        <App />
      </realHooks.ComposerContext.Provider>
    </WarmConfirmProvider>,
  );
}

/** 在真实 Hook 里跑一次失败的 replayTurn，返回它写出的 state。 */
async function stateAfterFailedReplay(): Promise<AppState> {
  const current = session();
  installGatewayFetch(({ path }) => {
    if (path.endsWith("/replay")) {
      // 后端拒绝回放：这正是「轮次操作失败」的真实来源。
      return Response.json(
        { detail: "上下文窗口已失效，无法回放" },
        { status: 409 },
      );
    }
    return undefined;
  }, { token: "test-replay-token" });

  const mounted = mountSessionRunActions({
    currentSession: current,
    state: baseState(current),
    cacheKey: CACHE_KEY,
  });

  await expect(
    mounted.actions.replayTurn("msg_original", "regenerate", "原始回复"),
  ).rejects.toThrow("上下文窗口已失效");

  return mounted.state();
}

describe("AppState.error 只属于工作区初始化失败出口", () => {
  test("轮次操作失败不会把聊天区换成初始化失败大屏", async () => {
    const failed = await stateAfterFailedReplay();

    // 失败仍然可见：status 承载真实文案，error 通道不被轮次动作借用。
    expect(failed.status).toContain("轮次操作失败");
    expect(failed.error ?? null).toBeNull();

    const html = renderApp(failed);
    expect(html).not.toContain("前端初始化失败");
    expect(html).not.toContain("重新加载工作区");
    expect(html).toContain("session-view-content");
    expect(html).toContain("chat-stream-shell");
    // 聊天区外壳与 Composer 都还在：失败只走状态栏，没有吞掉对话区。
    expect(html).toContain('class="composer"');
    expect(html).toContain("sendButton");
    // 失败仍然可见，并且落在状态栏这一条通道上。
    expect(html).toContain("轮次操作失败");
  });

  test("没有会话时初始化失败出口真正可达", () => {
    const html = renderApp({
      ...baseState(null),
      error: "gateway 连接失败",
    } as AppState);

    expect(html).toContain("session-view-content");
    expect(html).toContain("前端初始化失败");
    expect(html).toContain("gateway 连接失败");
    expect(html).toContain("重新加载工作区");
  });

  test("既没有会话也没有初始化失败时不渲染聊天区外壳", () => {
    const html = renderApp(baseState(null));

    expect(html).not.toContain("session-view-content");
    expect(html).not.toContain('class="composer"');
  });
});

describe("App.tsx 的错误文案归一只有唯一实现", () => {
  test("不再内联 errorMessage 样板，统一走 utils/errorMessage", async () => {
    const source = await Bun.file(new URL("../../App.tsx", import.meta.url)).text();

    // 归一表达式只允许存在于 utils/errorMessage 这一处权威实现里。
    expect(source).not.toContain(
      "error instanceof Error ? error.message : String(error)",
    );
    expect(source).toContain('from "./utils/errorMessage"');
  });
});
