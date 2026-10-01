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

function appState(sessionId: string = SESSION_ID): AppState {
  return {
    apiPort: 8014,
    activeGatewayWorkspaceId: WORKSPACE_ID,
    currentSession: {
      session_id: sessionId,
      workspace_id: WORKSPACE_ID,
      title: "回填不覆盖",
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

function mountComposer(actions: Actions, sessionId: string = SESSION_ID): ReactTestRenderer {
  const state = appState(sessionId);
  function Boundary(): React.ReactElement {
    const ref = useRef<ComposerStateSnapshot | null>(null);
    const snapshot = reuseComposerStateSnapshot(
      ref.current,
      selectComposerState(state, `workspace::${sessionId}`),
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
  let renderer: ReactTestRenderer | undefined;
  act(() => {
    renderer = create(
      <WarmConfirmProvider>
        <Boundary />
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
const originalFileReader = Object.getOwnPropertyDescriptor(globalThis, "FileReader");
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
  restoreGlobalDescriptor("FileReader", originalFileReader);
  for (const name of OVERLAY_CONSTRUCTOR_NAMES) {
    Reflect.deleteProperty(globalThis, name);
  }
  restoreGlobalDescriptor("window", originalWindow);
  restoreGlobalDescriptor("BroadcastChannel", originalBroadcastChannel);
});

function inputValue(renderer: ReactTestRenderer): string {
  return renderer.root.findByProps({ id: "input" }).props.value;
}
function setInputValue(renderer: ReactTestRenderer, value: string): void {
  act(() => {
    renderer.root.findByProps({ id: "input" }).props.onChange({ target: { value } });
  });
}

describe("Composer 发送失败回填：不得覆盖在途期间新输入的草稿", () => {
  test("在途失败回填只补空白输入，不覆盖用户新输入内容", async () => {
    installComposerEnvironment();
    let rejectSend: (error: unknown) => void = () => undefined;
    const renderer = mountComposer(baseActions({
      sendMessage: () => new Promise<void>((_resolve, reject) => {
        rejectSend = reject;
      }),
    }), "session_rollback_input");

    setInputValue(renderer, "第一条");
    act(() => {
      renderer.root.findByProps({ id: "sendButton" }).props.onClick();
    });
    expect(inputValue(renderer)).toBe("");

    // 第一条仍在途，用户已经输入了下一条（合支持的排队语义，输入框可继续用）。
    setInputValue(renderer, "第二条");

    await act(async () => {
      rejectSend(new Error("网络中断"));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(inputValue(renderer)).toBe("第二条");
    act(() => renderer.unmount());
  });

  test("在途失败回填只补空白附件，不覆盖用户新添加的附件", async () => {
    installComposerEnvironment();
    class FakeFileReader {
      result: string = "data:text/plain;base64,QQ==";
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      readAsDataURL(): void {
        this.onload?.();
      }
    }
    Object.defineProperty(globalThis, "FileReader", {
      configurable: true,
      value: FakeFileReader,
    });
    let rejectSend: (error: unknown) => void = () => undefined;
    const renderer = mountComposer(baseActions({
      sendMessage: () => new Promise<void>((_resolve, reject) => {
        rejectSend = reject;
      }),
    }), "session_rollback_attachments");
    const pasteFile = async (name: string) => {
      await act(async () => {
        renderer.root.findByProps({ id: "input" }).props.onPaste({
          clipboardData: {
            files: [{ name, type: "text/plain" }],
            items: [],
          },
          preventDefault: () => undefined,
        });
        await Promise.resolve();
        await Promise.resolve();
      });
    };

    await pasteFile("a.txt");
    expect(JSON.stringify(renderer.toJSON())).toContain("a.txt");
    act(() => {
      renderer.root.findByProps({ id: "sendButton" }).props.onClick();
    });
    // 发送后附件托盘已清空。
    expect(JSON.stringify(renderer.toJSON())).not.toContain("a.txt");

    await pasteFile("b.txt");

    await act(async () => {
      rejectSend(new Error("网络中断"));
      await Promise.resolve();
      await Promise.resolve();
    });

    const rendered = JSON.stringify(renderer.toJSON());
    expect(rendered).toContain("b.txt");
    expect(rendered).not.toContain("a.txt");
    act(() => renderer.unmount());
  });
});
