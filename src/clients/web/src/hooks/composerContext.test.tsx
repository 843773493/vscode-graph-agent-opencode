import React, { useEffect, useMemo, useRef } from "react";
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import {
  ComposerContext,
  useComposerState,
  type ComposerContextType,
} from "../hooks";
import { useComposerDraft } from "./useComposerDraft";
import {
  reuseComposerStateSnapshot,
  selectComposerState,
  type ComposerStateSnapshot,
} from "../state/composerState";
import { writeComposerDraft } from "../state/composerDrafts/storage";
import type { AppState } from "../types/frontend";
import { createSessionTurnTimeline } from "../state/session/turnTimeline";

const SESSION_CACHE_KEY = "workspace::session";

function appState(): AppState {
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

const composerActions: Omit<ComposerContextType, "state"> = {
  getLatestAssistantContent: () => null,
  setStatus: () => undefined,
  sendMessage: async () => undefined,
  compactSession: async () => undefined,
  refreshGoal: async () => null,
  updateGoal: async () => {
    throw new Error("测试不调用 updateGoal");
  },
  clearGoal: async () => undefined,
  interruptSession: () => undefined,
  switchAgent: async () => undefined,
  switchModel: async () => undefined,
  setWorkspaceDefaultAgent: async () => undefined,
  setWorkspaceDefaultProvider: async () => undefined,
  switchContentView: () => undefined,
  createSession: async () => {
    throw new Error("测试不调用 createSession");
  },
  startNewSessionDraft: () => undefined,
  renameSession: async () => undefined,
  activateGatewayWorkspace: async () => undefined,
  updateUiSettings: async () => undefined,
};

function ComposerBoundary({
  state,
  children,
}: {
  state: AppState;
  children: React.ReactNode;
}) {
  const snapshotRef = useRef<ComposerStateSnapshot | null>(null);
  const snapshot = reuseComposerStateSnapshot(
    snapshotRef.current,
    selectComposerState(state, SESSION_CACHE_KEY),
  );
  snapshotRef.current = snapshot;
  const value = useMemo<ComposerContextType>(
    () => ({ ...composerActions, state: snapshot }),
    [snapshot],
  );
  return <ComposerContext.Provider value={value}>{children}</ComposerContext.Provider>;
}

function createMemoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => {
      values.delete(key);
    },
    setItem: (key, value) => {
      values.set(key, value);
    },
  };
}

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

afterEach(() => {
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
});

describe("Composer React 状态边界", () => {
  test("timeline 和 trace 更新不触发 Context consumer 重渲染", () => {
    let renderCount = 0;
    const Consumer = React.memo(() => {
      useComposerState();
      renderCount += 1;
      return null;
    });
    const initial = appState();
    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(
        <ComposerBoundary state={initial}>
          <Consumer />
        </ComposerBoundary>,
      );
    });
    const timelineDelta = {
      ...initial,
      traceEvents: [{ event_id: "event_delta" }],
      turnTimelinesBySession: new Map([
        [SESSION_CACHE_KEY, {
          ...createSessionTurnTimeline(SESSION_CACHE_KEY),
          phase: "ready",
        }],
      ]),
    } as unknown as AppState;
    act(() => {
      renderer.update(
        <ComposerBoundary state={timelineDelta}>
          <Consumer />
        </ComposerBoundary>,
      );
    });

    expect(renderCount).toBe(1);
  });

  test("scope 切换后的首次可见草稿提交不包含旧会话值", () => {
    const storage = createMemoryStorage();
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: { localStorage: storage },
    });
    writeComposerDraft("workspace::session-a", "草稿 A");
    writeComposerDraft("workspace::session-b", "草稿 B");
    const commits: string[] = [];
    function DraftConsumer({ sessionId }: { sessionId: string }) {
      const [draft] = useComposerDraft("workspace", sessionId);
      useEffect(() => {
        commits.push(draft);
      }, [draft]);
      return <span>{draft}</span>;
    }
    let renderer!: ReactTestRenderer;
    act(() => {
      renderer = create(<DraftConsumer sessionId="session-a" />);
    });
    act(() => {
      renderer.update(<DraftConsumer sessionId="session-b" />);
    });

    expect(commits).toEqual(["草稿 A", "草稿 B"]);
    expect(renderer.toJSON()).toEqual({
      type: "span",
      props: {},
      children: ["草稿 B"],
    });
  });
});
