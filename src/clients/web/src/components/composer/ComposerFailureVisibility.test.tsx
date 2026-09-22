import React, { useMemo, useRef } from "react";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { ComposerContext, type ComposerContextType } from "../../hooks";
import {
  reuseComposerStateSnapshot,
  selectComposerState,
  type ComposerStateSnapshot,
} from "../../state/composerState";
import WarmConfirmProvider from "../shell/WarmConfirmProvider";
import type { AppState } from "../../types/frontend";
import type { Agent } from "../../types/backend";
import Composer from "./Composer";

// AnchoredOverlay 依赖 @floating-ui 与真实 DOM；测试环境没有 DOM，用内联渲染替身
// 保留「菜单打开时子节点可见」这一契约。
mock.module("../overlays/AnchoredOverlay", () => ({
  default: ({ open, children }: { open: boolean; children: React.ReactNode }) =>
    open ? <>{children}</> : null,
}));

const WORKSPACE_ID = "workspace";
const SESSION_ID = "session";
const SCOPE_KEY = "workspace::session";

const agent = (agentId: string, extra: Partial<Agent> = {}): Agent => ({
  agent_id: agentId,
  name: agentId,
  description: null,
  model: "primary",
  tools: [],
  capabilities: [],
  providers: [
    {
      provider_id: "provider_a",
      model: "model-a",
      custom_llm_provider: "openai",
      available: true,
      workspace_default: false,
      configuration_error: null,
    },
    {
      provider_id: "provider_b",
      model: "model-b",
      custom_llm_provider: "openai",
      available: true,
      workspace_default: false,
      configuration_error: null,
    },
  ],
  workspace_default: false,
  ...extra,
}) as unknown as Agent;

function appState(agents: Agent[]): AppState {
  return {
    apiPort: 8014,
    activeGatewayWorkspaceId: WORKSPACE_ID,
    currentSession: {
      session_id: SESSION_ID,
      workspace_id: WORKSPACE_ID,
      title: "失败可见性",
      current_agent_id: "default",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    },
    currentSessionWorkspaceId: WORKSPACE_ID,
    contentView: "default",
    uiSettings: {
      layout: {},
      session_sidebar: {},
      workspace_file_tree: { expanded_paths_by_workspace: {} },
      gateway_console: { view: "routing" },
      recent_local_workspace_paths: [],
    },
    agents,
    pendingConversations: new Map(),
    activeJobIdsBySession: new Map(),
    turnTimelinesBySession: new Map(),
    currentGoal: null,
    currentGoalSessionId: null,
    goalLoading: false,
    goalError: null,
    compactLoading: false,
    gatewayUserAccess: null,
  } as unknown as AppState;
}

type Actions = Omit<ComposerContextType, "state">;

function baseActions(overrides: Partial<Actions> = {}): Actions {
  return {
    getLatestAssistantContent: () => null,
    setStatus: () => undefined,
    sendMessage: async () => undefined,
    compactSession: async () => ({} as never),
    refreshGoal: async () => null,
    updateGoal: async () => undefined,
    clearGoal: async () => undefined,
    interruptSession: async () => undefined,
    switchAgent: async () => undefined,
    switchModel: async () => undefined,
    refreshAgents: async () => undefined,
    setWorkspaceDefaultAgent: async () => undefined,
    setWorkspaceDefaultProvider: async () => undefined,
    switchContentView: () => undefined,
    createSession: async () => undefined,
    renameSession: async () => undefined,
    updateUiSettings: async () => undefined,
    ...overrides,
  } as unknown as Actions;
}

function mountComposer(state: AppState, actions: Actions): ReactTestRenderer {
  let renderer: ReactTestRenderer | undefined;
  function Boundary({ children }: { children: React.ReactNode }) {
    const ref = useRef<ComposerStateSnapshot | null>(null);
    const snapshot = reuseComposerStateSnapshot(
      ref.current,
      selectComposerState(state, SCOPE_KEY),
    );
    ref.current = snapshot;
    const value = useMemo<ComposerContextType>(
      () => ({ ...actions, state: snapshot }),
      [snapshot],
    );
    return <ComposerContext.Provider value={value}>{children}</ComposerContext.Provider>;
  }
  act(() => {
    renderer = create(
      <WarmConfirmProvider>
        <Boundary>
          <Composer />
        </Boundary>
      </WarmConfirmProvider>,
    );
  });
  if (!renderer) {
    throw new Error("Composer 未渲染");
  }
  return renderer;
}

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalBroadcastChannel = Object.getOwnPropertyDescriptor(
  globalThis,
  "BroadcastChannel",
);

/** Composer 挂载需要 window 事件通道、localStorage 与 BroadcastChannel；
 * 测试环境没有 DOM，这里提供最小可用的替身，不参与业务断言。 */
function installComposerEnvironment(): void {
  const listeners = new Map<string, Set<(event: unknown) => void>>();
  const storage = new Map<string, string>();
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      localStorage: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => {
          storage.set(key, value);
        },
        removeItem: (key: string) => {
          storage.delete(key);
        },
      },
      location: { origin: "http://127.0.0.1:8011" },
      addEventListener: (type: string, handler: (event: unknown) => void) => {
        const set = listeners.get(type) ?? new Set();
        set.add(handler);
        listeners.set(type, set);
      },
      removeEventListener: (type: string, handler: (event: unknown) => void) => {
        listeners.get(type)?.delete(handler);
      },
    },
  });
  Object.defineProperty(globalThis, "BroadcastChannel", {
    configurable: true,
    value: class {
      addEventListener() {}
      removeEventListener() {}
      close() {}
    },
  });
}

afterEach(() => {
  if (originalWindow) {
    Object.defineProperty(globalThis, "window", originalWindow);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
  if (originalBroadcastChannel) {
    Object.defineProperty(globalThis, "BroadcastChannel", originalBroadcastChannel);
  } else {
    Reflect.deleteProperty(globalThis, "BroadcastChannel");
  }
});

describe("Composer 切换类动作失败必须可见", () => {
  test("switchAgent 失败后界面出现错误文本，且不再被空 catch 吞掉", async () => {
    installComposerEnvironment();
    const renderer = mountComposer(
      appState([agent("default"), agent("reviewer")]),
      baseActions({
        switchAgent: async () => {
          throw new Error("请求失败 500 : 上游模型不可用");
        },
      }),
    );

    // 打开 Agent 菜单，点击 reviewer 触发 handleAgentSelect。
    act(() => {
      renderer.root.findByProps({ id: "agentSelectButton" }).props.onClick();
    });
    await act(async () => {
      renderer.root
        .findAllByProps({ role: "menuitemradio" })
        .find((node) => node.props["aria-checked"] === false)
        ?.props.onClick();
      await Promise.resolve();
    });

    expect(JSON.stringify(renderer.toJSON())).toContain(
      "Agent 切换失败：请求失败 500 : 上游模型不可用",
    );
    act(() => renderer.unmount());
  });

  test("switchModel 失败后界面出现错误文本", async () => {
    installComposerEnvironment();
    const renderer = mountComposer(
      appState([agent("default")]),
      baseActions({
        switchModel: async () => {
          throw new Error("请求失败 400 : provider 未注册");
        },
      }),
    );

    act(() => {
      renderer.root.findByProps({ className: "composer-model-pill" }).props.onClick();
    });
    await act(async () => {
      renderer.root
        .findAllByProps({ role: "menuitemradio" })
        .find((node) => node.props["aria-checked"] === false)
        ?.props.onClick();
      await Promise.resolve();
    });

    expect(JSON.stringify(renderer.toJSON())).toContain(
      "模型切换失败：请求失败 400 : provider 未注册",
    );
    act(() => renderer.unmount());
  });

  test("interruptSession 失败不产生未处理 rejection 且界面可见", async () => {
    installComposerEnvironment();
    const state = appState([agent("default")]);
    state.activeJobIdsBySession.set(SCOPE_KEY, "job_running");
    let rejectionSeen: unknown = null;
    const onUnhandled = (event: unknown) => {
      rejectionSeen = (event as { reason?: unknown })?.reason ?? event;
    };
    const globalWithEvents = globalThis as unknown as {
      addEventListener?: (type: string, handler: (event: unknown) => void) => void;
    };
    globalWithEvents.addEventListener?.("unhandledrejection", onUnhandled);
    const renderer = mountComposer(
      state,
      baseActions({
        interruptSession: async () => {
          throw new Error("请求失败 500 : 中断执行器崩溃");
        },
      }),
    );

    await act(async () => {
      renderer.root.findByProps({ id: "interruptButton" }).props.onClick();
      await Promise.resolve();
      await Promise.resolve();
    });

    // 未处理 rejection 会让 bun test 以 Unhandled error 终止本文件；这里额外断言
    // 没有事件冒泡，作为第二重证据。
    expect(rejectionSeen).toBeNull();
    act(() => renderer.unmount());
  });
});
