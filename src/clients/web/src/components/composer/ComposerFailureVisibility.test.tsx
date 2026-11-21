import React, { useMemo, useRef } from "react";
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { ComposerContext, type ComposerContextType } from "../../hooks/app/appContext";
import {
  reuseComposerStateSnapshot,
  selectComposerState,
  type ComposerStateSnapshot,
} from "../../state/composerState";
import WarmConfirmProvider from "../shell/WarmConfirmProvider";
import type { AppState } from "../../types/frontend";
import type { Agent, SessionGoal } from "../../types/backend";
import Composer from "./Composer";
import { ComposerDiagnosticsContext } from "./ComposerDiagnosticsContext";
import { installComposerTestGlobals } from "./composerTestFixtures";
import type { ComposerTasks } from "./ComposerTaskList";
import type { ModelPickerRequest } from "../../state/modelPicker";

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
  models: [
    {
      model_ref: { provider_id: "provider_a", model_id: "default" },
      model: "model-a",
      custom_llm_provider: "openai",
      available: true,
      workspace_default: false,
      configuration_error: null,
    },
    {
      model_ref: { provider_id: "provider_b", model_id: "default" },
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
      revision: 0,
      control_revision: 0,
      sync_version: 0,
    },
    currentSessionWorkspaceId: WORKSPACE_ID,
    contentView: "default",
    uiSettings: {
      layout: {},
      session_sidebar: {},
      workspace_file_tree: { expanded_paths_by_workspace: {} },
      gateway_console: { view: "routing" },
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
  } as unknown as AppState;
}

type Actions = Omit<ComposerContextType, "state">;

function baseActions(overrides: Partial<Actions> = {}): Actions {
  return {
    getLatestAssistantContent: () => null,
    setStatus: () => undefined,
    sendMessage: async () => undefined,
    refreshTurnHistory: () => undefined,
    compactSession: async () => ({} as never),
    refreshGoal: async () => null,
    updateGoal: async () => undefined,
    clearGoal: async () => undefined,
    interruptSession: async () => undefined,
    switchAgent: async () => undefined,
    switchModel: async () => undefined,
    refreshAgents: async () => [agent("default"), agent("reviewer")],
    setWorkspaceDefaultAgent: async () => undefined,
    setWorkspaceDefaultModel: async () => undefined,
    switchContentView: () => undefined,
    createSession: async () => undefined,
    renameSession: async () => undefined,
    updateUiSettings: async () => undefined,
    ...overrides,
  } as unknown as Actions;
}

function mountComposer(state: AppState, actions: Actions, report: (message: string) => void = () => {}, tasks?: ComposerTasks,
  pick: (request: ModelPickerRequest) => Promise<void> = async request => request.onSelect((await request.loadModels())[1].model_ref)): ReactTestRenderer {
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
      <ComposerDiagnosticsContext.Provider value={report}><WarmConfirmProvider>
        <Boundary>
          <Composer onPickModel={pick} status={tasks ? { conversations: [], timeline: null, active: true, tasks } : undefined} />
        </Boundary>
      </WarmConfirmProvider></ComposerDiagnosticsContext.Provider>,
    );
  });
  if (!renderer) {
    throw new Error("Composer 未渲染");
  }
  return renderer;
}

let restoreComposerGlobals = () => {};

function installComposerEnvironment(): void {
  restoreComposerGlobals = installComposerTestGlobals();
}

afterEach(() => {
  restoreComposerGlobals();
  restoreComposerGlobals = () => {};
});

describe("Composer 切换类动作失败必须可见", () => {
  test("权威状态确认失败进入输出，不新增状态栏入口或错误块", () => {
    installComposerEnvironment();
    const state = appState([agent("default")]);
    state.sessionExecutionSyncError = "工作区连接已断开";
    const messages: string[] = [];
    const renderer = mountComposer(state, baseActions(), message => messages.push(message));
    expect(messages.some(message => message.includes("工作区连接已断开"))).toBe(true);
    expect(renderer.root.findAllByProps({ "data-status-section": "sync" })).toHaveLength(0);
    expect(JSON.stringify(renderer.toJSON())).not.toContain("工作区连接已断开");
    act(() => renderer.unmount());
  });

  test("当前 Goal 在统一持续任务列表中展示，不再增加独立 Goal 入口", () => {
    installComposerEnvironment();
    const state = appState([agent("default")]);
    state.currentGoal = {
      goal_id: "goal_1",
      session_id: SESSION_ID,
      thread_id: "thread_1",
      objective: "完成当前任务并验证结果",
      status: "active",
      token_budget: null,
      tokens_used: 0,
      time_used_seconds: 0,
      revision: 1,
      control_revision: 1,
      sync_version: 1,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    } satisfies SessionGoal;
    state.currentGoalSessionId = SESSION_ID;
    const renderer = mountComposer(state, baseActions(), () => {}, {
      items: [{ task_id: "goal_1", workspace_id: WORKSPACE_ID, application_type: "goal", application_version: "1",
        revision: 1, definition_revision: 1, tokens_used: 0, time_used_seconds: 0, sync_version: 1,
        configuration: undefined, lifecycle: "active", phase: "waiting_trigger",
        recovering: false, goal: { ...state.currentGoal, token_budget: state.currentGoal.token_budget ?? undefined },
        created_at: state.currentGoal.created_at, updated_at: state.currentGoal.updated_at }],
      loaded: true, error: null, onAction: async () => {}, onManage() {},
    });
    expect(renderer.root.findAllByProps({ "data-status-section": "goal" })).toHaveLength(0);
    expect(renderer.root.findByProps({ "data-status-section": "tasks" }).props["aria-label"]).toBe("持续任务 1");
    expect(renderer.root.findByProps({ "data-task-id": "goal_1" })).toBeDefined();
    expect(JSON.stringify(renderer.toJSON())).toContain("完成当前任务并验证结果");
    act(() => renderer.unmount());
  });

  test("同一任务订阅错误只输出一次，不新增入口或错误详情卡片", () => {
    installComposerEnvironment();
    const state = appState([agent("default")]);
    state.goalError = "HTTP 503 · task-stream";
    const messages: string[] = [];
    const renderer = mountComposer(state, baseActions(), message => messages.push(message), {
      items: [], loaded: true, error: state.goalError, onAction: async () => {}, onManage() {},
    });
    expect(messages).toEqual(["持续任务: HTTP 503 · task-stream"]);
    expect(renderer.root.findAllByType("button").filter(button => button.props["data-status-section"]))
      .toHaveLength(0);
    expect(JSON.stringify(renderer.toJSON())).not.toContain("HTTP 503");
    act(() => renderer.unmount());
  });

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

  test("四类切换动作的失败文案都能落到可见错误区", async () => {
    installComposerEnvironment();

    const cases: Array<{
      name: string;
      expected: string;
      actions: Partial<Actions>;
      open: (renderer: ReactTestRenderer) => void;
      select: (renderer: ReactTestRenderer) => void;
      pick?: (request: ModelPickerRequest) => Promise<void>;
    }> = [
      {
        name: "switchAgent",
        expected: "Agent 切换失败：agent 挂了",
        actions: {
          switchAgent: async () => {
            throw new Error("agent 挂了");
          },
        },
        open: (renderer) => {
          renderer.root.findByProps({ id: "agentSelectButton" }).props.onClick();
        },
        select: (renderer) => {
          renderer.root
            .findAllByProps({ role: "menuitemradio" })
            .find((node) => node.props["aria-checked"] === false)
            ?.props.onClick();
        },
      },
      {
        name: "setWorkspaceDefaultAgent",
        expected: "设置工作区默认 Agent 失败：pin 挂了",
        actions: {
          setWorkspaceDefaultAgent: async () => {
            throw new Error("pin 挂了");
          },
        },
        open: (renderer) => {
          renderer.root.findByProps({ id: "agentSelectButton" }).props.onClick();
        },
        select: (renderer) => {
          renderer.root
            .findAllByProps({ className: "composer-workspace-default-button" })[0]
            ?.props.onClick();
        },
      },
      {
        name: "switchModel",
        expected: "模型切换失败：model 挂了",
        actions: {
          switchModel: async () => {
            throw new Error("model 挂了");
          },
        },
        open: (renderer) => {
          renderer.root.findByProps({ className: "composer-model-pill" }).props.onClick({ currentTarget: {}, shiftKey: false });
        },
        select: () => {},
      },
      {
        name: "setWorkspaceDefaultModel",
        expected: "设置工作区默认模型失败：provider pin 挂了",
        actions: {
          setWorkspaceDefaultModel: async () => {
            throw new Error("provider pin 挂了");
          },
        },
        open: (renderer) => {
          renderer.root.findByProps({ className: "composer-model-pill" }).props.onClick({ currentTarget: {}, shiftKey: false });
        },
        select: () => {},
        pick: async request => request.onSetWorkspaceDefault((await request.loadModels())[0].model_ref),
      },
    ];

    for (const item of cases) {
      const renderer = mountComposer(
        appState([agent("default"), agent("reviewer")]),
        baseActions(item.actions), () => {}, undefined, item.pick,
      );
      // 先渲染出菜单，再点击条目：两次 act 分别对应 React 的两次提交。
      act(() => item.open(renderer));
      await act(async () => {
        item.select(renderer);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(`${item.name}: ${JSON.stringify(renderer.toJSON())}`).toContain(
        item.expected,
      );
      act(() => renderer.unmount());
    }
  });

  test("下一次成功的切换动作会清掉先前的可见错误", async () => {
    installComposerEnvironment();
    let shouldFail = true;
    const renderer = mountComposer(
      appState([agent("default"), agent("reviewer")]),
      baseActions({
        switchAgent: async () => {
          if (shouldFail) {
            throw new Error("请求失败 500 : 上游模型不可用");
          }
        },
      }),
    );

    const openAgentMenu = () => {
      renderer.root.findByProps({ id: "agentSelectButton" }).props.onClick();
    };
    const selectOtherAgent = () => {
      renderer.root
        .findAllByProps({ role: "menuitemradio" })
        .find((node) => node.props["aria-checked"] === false)
        ?.props.onClick();
    };

    act(() => openAgentMenu());
    await act(async () => {
      selectOtherAgent();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(JSON.stringify(renderer.toJSON())).toContain(
      "Agent 切换失败：请求失败 500 : 上游模型不可用",
    );

    // 后端恢复后再次切换：错误必须消失，不能残留成假失败。
    shouldFail = false;
    act(() => openAgentMenu());
    await act(async () => {
      selectOtherAgent();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(JSON.stringify(renderer.toJSON())).not.toContain("Agent 切换失败");
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

    await act(async () => {
      renderer.root.findByProps({ className: "composer-model-pill" }).props.onClick({ currentTarget: {}, shiftKey: false });
      await Promise.resolve();
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
    expect(JSON.stringify(renderer.toJSON())).toContain("中断生成失败：请求失败 500 : 中断执行器崩溃");
    act(() => renderer.unmount());
  });
});
