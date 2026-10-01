import React, { useMemo, useRef } from "react";
import { afterEach, describe, expect, test } from "bun:test";
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
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

const WORKSPACE_ID = "workspace";
const SESSION_A = "session_a";
const SESSION_B = "session_b";
const SCOPE_KEY = "workspace::session_a";

const agent = (): Agent => ({
  agent_id: "default",
  name: "default",
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
  ],
  workspace_default: false,
}) as unknown as Agent;

function appState(sessionId: string): AppState {
  return {
    apiPort: 8014,
    activeGatewayWorkspaceId: WORKSPACE_ID,
    currentSession: {
      session_id: sessionId,
      workspace_id: WORKSPACE_ID,
      title: sessionId,
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
    agents: [agent()],
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

function mountComposer(
  readState: () => AppState,
  actions: Actions,
): { renderer: ReactTestRenderer; element: () => React.ReactElement } {
  function Boundary(): React.ReactElement {
    const state = readState();
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
    return (
      <ComposerContext.Provider value={value}>
        <Composer />
      </ComposerContext.Provider>
    );
  }
  const element = () => (
    <WarmConfirmProvider>
      <Boundary />
    </WarmConfirmProvider>
  );
  let renderer: ReactTestRenderer | undefined;
  act(() => {
    renderer = create(element());
  });
  if (!renderer) {
    throw new Error("Composer 未渲染");
  }
  return { renderer, element };
}

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalBroadcastChannel = Object.getOwnPropertyDescriptor(globalThis, "BroadcastChannel");
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
const OVERLAY_CONSTRUCTOR_NAMES = ["Element", "Node", "HTMLElement"] as const;

class OverlayElementStub {}
class OverlayNodeStub {}
const OVERLAY_CONSTRUCTORS = {
  Element: OverlayElementStub,
  Node: OverlayNodeStub,
  HTMLElement: OverlayElementStub,
};

function installComposerEnvironment(): void {
  for (const [name, value] of Object.entries(OVERLAY_CONSTRUCTORS)) {
    Object.defineProperty(globalThis, name, { configurable: true, value });
  }
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
      ...OVERLAY_CONSTRUCTORS,
      location: { origin: "http://127.0.0.1:8011" },
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
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
  restoreGlobalDescriptor("document", originalDocument);
  for (const name of OVERLAY_CONSTRUCTOR_NAMES) {
    Reflect.deleteProperty(globalThis, name);
  }
  restoreGlobalDescriptor("window", originalWindow);
  restoreGlobalDescriptor("BroadcastChannel", originalBroadcastChannel);
});

function inputValue(renderer: ReactTestRenderer): string {
  return renderer.root.findByProps({ id: "input" }).props.value;
}

describe("Composer 会话作用域守卫：在途提交只回写发起它的会话", () => {
  test("在途发送失败不得把 A 的输入回填进已切换的 B 会话", async () => {
    installComposerEnvironment();
    let rejectSend: (error: unknown) => void = () => undefined;
    let current = appState(SESSION_A);
    const { renderer, element } = mountComposer(() => current, baseActions({
      sendMessage: () => new Promise<void>((_resolve, reject) => {
        rejectSend = reject;
      }),
    }));

    act(() => {
      renderer.root.findByProps({ id: "input" }).props.onChange({
        target: { value: "会话A内容" },
      });
    });
    act(() => {
      renderer.root.findByProps({ id: "sendButton" }).props.onClick();
    });
    expect(inputValue(renderer)).toBe("");

    // 用户切到另一个会话：B 的草稿是空的。
    current = appState(SESSION_B);
    act(() => {
      renderer.update(element());
    });
    expect(inputValue(renderer)).toBe("");

    // A 的发送此时才失败。失败回填必须留在 A，不能污染 B。
    await act(async () => {
      rejectSend(new Error("网络中断"));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(inputValue(renderer)).toBe("");
    expect(JSON.stringify(renderer.toJSON())).not.toContain("会话A内容");
    act(() => renderer.unmount());
  });
});
