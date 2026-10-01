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
const SESSION_ID = "session";
const SCOPE_KEY = "workspace::session";

const agent = (agentId: string): Agent => ({
  agent_id: agentId,
  name: agentId,
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

function appState(): AppState {
  return {
    apiPort: 8014,
    activeGatewayWorkspaceId: WORKSPACE_ID,
    currentSession: {
      session_id: SESSION_ID,
      workspace_id: WORKSPACE_ID,
      title: "发送闸门",
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
    agents: [agent("default")],
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

function typeInput(renderer: ReactTestRenderer, value: string): void {
  act(() => {
    renderer.root.findByProps({ id: "input" }).props.onChange({
      target: { value },
    });
  });
}

describe("Composer 提交闸门：同一次提交不得被重复触发", () => {
  test("同一事件循环内连按三次发送只提交一次", async () => {
    installComposerEnvironment();
    const sent: Array<{ content: string; policy: string }> = [];
    const renderer = mountComposer(
      appState(),
      baseActions({
        sendMessage: async (content: string, _attachments, policy) => {
          sent.push({ content, policy: policy ?? "after_turn" });
        },
      }),
    );
    typeInput(renderer, "连按测试");

    const sendButton = () => renderer.root.findByProps({ id: "sendButton" });
    // 三次触发发生在同一次事件循环提交里：React 尚未 flush setInput("")，
    // 三次回调拿到的是同一份闭包输入——正是真实浏览器里连按 Enter 的场景。
    await act(async () => {
      sendButton().props.onClick();
      sendButton().props.onClick();
      sendButton().props.onClick();
      await Promise.resolve();
    });

    expect(sent.length).toBe(1);
    expect(sent[0]).toEqual({ content: "连按测试", policy: "after_turn" });
    act(() => renderer.unmount());
  });

  test("Enter 连按三次同样只提交一次", async () => {
    installComposerEnvironment();
    const sent: string[] = [];
    const renderer = mountComposer(
      appState(),
      baseActions({
        sendMessage: async (content: string) => {
          sent.push(content);
        },
      }),
    );
    typeInput(renderer, "回车连按");

    const pressEnter = () => {
      renderer.root.findByProps({ id: "input" }).props.onKeyDown({
        key: "Enter",
        shiftKey: false,
        ctrlKey: false,
        metaKey: false,
        altKey: false,
        preventDefault: () => undefined,
        currentTarget: { selectionStart: 4, selectionEnd: 4 },
      });
    };
    await act(async () => {
      pressEnter();
      pressEnter();
      pressEnter();
      await Promise.resolve();
    });

    expect(sent.length).toBe(1);
    expect(sent[0]).toBe("回车连按");
    act(() => renderer.unmount());
  });

  test("提交成功后同样的内容可以再次排队（闸门不误挡合法排队）", async () => {
    installComposerEnvironment();
    const sent: string[] = [];
    const renderer = mountComposer(
      appState(),
      baseActions({
        sendMessage: async (content: string) => {
          sent.push(content);
        },
      }),
    );

    typeInput(renderer, "排队消息");
    await act(async () => {
      renderer.root.findByProps({ id: "sendButton" }).props.onClick();
      await Promise.resolve();
    });
    expect(sent.length).toBe(1);

    // 第二次是用户在生成中显式追加的同内容消息：属受支持的排队语义，
    // 闸门必须在上一笔落定后放行，不得把合法排队一并挡掉。
    typeInput(renderer, "排队消息");
    await act(async () => {
      renderer.root.findByProps({ id: "sendButton" }).props.onClick();
      await Promise.resolve();
    });
    expect(sent.length).toBe(2);
    act(() => renderer.unmount());
  });

  test("提交失败回填后，同一内容的重试不被闸门挡住", async () => {
    installComposerEnvironment();
    const sent: string[] = [];
    let shouldFail = true;
    const renderer = mountComposer(
      appState(),
      baseActions({
        sendMessage: async (content: string) => {
          sent.push(content);
          if (shouldFail) throw new Error("请求失败 500 : 上游不可用");
        },
      }),
    );

    typeInput(renderer, "重试内容");
    await act(async () => {
      renderer.root.findByProps({ id: "sendButton" }).props.onClick();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(sent.length).toBe(1);
    expect(JSON.stringify(renderer.toJSON())).toContain("发送失败：请求失败 500 : 上游不可用");

    // 后端恢复后用户重发同一条：失败已回填并清空闸门，必须能发出去。
    shouldFail = false;
    await act(async () => {
      renderer.root.findByProps({ id: "sendButton" }).props.onClick();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(sent.length).toBe(2);
    act(() => renderer.unmount());
  });
});

