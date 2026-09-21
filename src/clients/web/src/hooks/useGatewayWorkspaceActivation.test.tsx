import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React, { type MutableRefObject } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as api from "../api";
import { DEFAULT_BACKEND_PORT } from "../api";
import * as gatewayApi from "../gatewayApi";
import type { Agent } from "../types/backend";
import type { AppState } from "../types/frontend";
import type { FinishWorkspaceRefresh } from "./contentViewLoaderTypes";
import { useGatewayWorkspaceActivation } from "./useGatewayWorkspaceActivation";

const API_PORT = 49_621;
const PREFERRED_SESSION_ID = "session-preferred";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (cause: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function agent(agentId: string): Agent {
  return {
    agent_id: agentId,
    name: `Agent ${agentId}`,
    model: "model-test",
    tools: [],
    capabilities: [],
    providers: [],
  };
}

/** 只覆盖本 hook 会读写的状态字段，其余字段对本链路无意义。 */
function appState(overrides: Partial<AppState> = {}): AppState {
  return {
    agents: [],
    currentSessionWorkspaceId: null,
    workspaceSwitching: false,
    gatewayError: null,
    error: null,
    status: "",
    isBootstrapping: false,
    ...overrides,
  } as unknown as AppState;
}

interface MountOptions {
  apiPort?: number | null;
  currentSessionId?: string | null;
  initialState?: AppState;
  finishWorkspaceRefresh?: FinishWorkspaceRefresh;
  /** 刷新后落到 state 的活动工作区 id；null 表示刷新被作废。缺省为 null。 */
  appliedWorkspaceId?: string | null;
  refreshGatewayWorkspaceStatuses?: (
    expectedWorkspaceId?: string | null,
  ) => Promise<void>;
  /** 需要真实还原切换态置位时启用，默认只计数，避免干扰既有的状态断言。 */
  resetWorkspaceScopedState?: (previous: AppState) => AppState;
}

interface MountedHook {
  hook: ReturnType<typeof useGatewayWorkspaceActivation>;
  state: () => AppState;
  /** 同步更新状态闭包与 latestStateRef，模拟外部链路写回权威状态。 */
  setLatest: (next: AppState) => void;
  /** 只改 ref、不动状态闭包，用于验证回调读取的是最新 ref 值。 */
  latestStateRef: MutableRefObject<AppState>;
  calls: {
    invalidate: number;
    reset: number;
    finish: Array<{ preferredSessionId?: string | null; options?: unknown }>;
    refreshStatuses: Array<string | null | undefined>;
  };
}

const mountedRenderers: ReactTestRenderer[] = [];
const restoreSpies: Array<() => void> = [];

async function mountHook(options: MountOptions = {}): Promise<MountedHook> {
  let current = options.initialState ?? appState();
  let hook: ReturnType<typeof useGatewayWorkspaceActivation> | undefined;
  const latestStateRef: MutableRefObject<AppState> = { current };
  const setLatest = (next: AppState) => {
    current = next;
    latestStateRef.current = next;
  };
  const calls: MountedHook["calls"] = {
    invalidate: 0,
    reset: 0,
    finish: [],
    refreshStatuses: [],
  };

  function Probe(): React.ReactNode {
    hook = useGatewayWorkspaceActivation({
      apiPort: options.apiPort === undefined ? API_PORT : options.apiPort,
      currentSessionId: options.currentSessionId ?? null,
      latestStateRef,
      setState: (update) => {
        current = typeof update === "function" ? update(current) : update;
        latestStateRef.current = current;
      },
      invalidateWorkspaceRefreshes: () => {
        calls.invalidate += 1;
      },
      refreshGatewayWorkspaceStatuses: async (expectedWorkspaceId) => {
        calls.refreshStatuses.push(expectedWorkspaceId);
        await options.refreshGatewayWorkspaceStatuses?.(expectedWorkspaceId);
      },
      resetWorkspaceScopedState: () => {
        calls.reset += 1;
        if (options.resetWorkspaceScopedState) {
          setLatest(options.resetWorkspaceScopedState(current));
        }
      },
      finishWorkspaceRefresh: async (preferredSessionId, refreshOptions) => {
        calls.finish.push({ preferredSessionId, options: refreshOptions });
        if (options.finishWorkspaceRefresh) {
          return await options.finishWorkspaceRefresh(preferredSessionId, refreshOptions);
        }
        return options.appliedWorkspaceId ?? null;
      },
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  mountedRenderers.push(renderer!);
  return { hook: hook!, state: () => current, setLatest, latestStateRef, calls };
}

/** 冲刷微任务与宏任务，让串行队列内部的 promise 链推进到下一个等待点。 */
async function flush(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

function spyOnGatewayApi<Name extends keyof typeof gatewayApi>(
  name: Name,
): ReturnType<typeof spyOn<typeof gatewayApi, Name>> {
  const spy = spyOn(gatewayApi, name);
  restoreSpies.push(() => spy.mockRestore());
  return spy;
}

function spyOnApi<Name extends keyof typeof api>(
  name: Name,
): ReturnType<typeof spyOn<typeof api, Name>> {
  const spy = spyOn(api, name);
  restoreSpies.push(() => spy.mockRestore());
  return spy;
}

afterEach(() => {
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
  for (const restore of restoreSpies.splice(0)) restore();
});

describe("useGatewayWorkspaceActivation 串行队列", () => {
  test("两个入口共用同一个串行队列，正式入口必须等后台激活完成后才开始", async () => {
    const pending: Array<Deferred<string>> = [];
    let inFlight = 0;
    let maxInFlight = 0;
    spyOnGatewayApi("activateGatewayWorkspace").mockImplementation(async (port, workspaceId) => {
      expect(port).toBe(API_PORT);
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      const operation = deferred<string>();
      pending.push(operation);
      const activeWorkspaceId = await operation.promise;
      inFlight -= 1;
      return activeWorkspaceId ?? workspaceId;
    });
    spyOnApi("listAgents").mockResolvedValue([]);
    const { hook, setLatest } = await mountHook({ appliedWorkspaceId: "ws-formal" });
    // 让后台入口的收尾守卫提前退出，专注观察队列的串行行为。
    setLatest(appState({ currentSessionWorkspaceId: null }));

    hook.activateGatewayWorkspaceInBackground("ws-background");
    await flush();
    expect(pending).toHaveLength(1);

    const formalActivation = hook.activateGatewayWorkspace("ws-formal");
    await flush();
    // 正式入口仍排在后台激活之后，此时不能发起第二次激活请求。
    expect(pending).toHaveLength(1);
    expect(maxInFlight).toBe(1);

    pending[0].resolve("ws-background");
    await flush();
    expect(pending).toHaveLength(2);
    expect(maxInFlight).toBe(1);

    pending[1].resolve("ws-formal");
    await act(async () => {
      await formalActivation;
    });
    expect(maxInFlight).toBe(1);
  });

  test("正式入口递增序列号后丢弃在途后台激活的迟到 Agent 结果", async () => {
    spyOnGatewayApi("activateGatewayWorkspace").mockImplementation(
      async (_port, workspaceId) => workspaceId,
    );
    const backgroundAgents = deferred<Agent[]>();
    spyOnApi("listAgents").mockImplementation(async () => await backgroundAgents.promise);
    const formalAgent = agent("agent-formal");
    const { hook, state, setLatest, calls } = await mountHook({
      finishWorkspaceRefresh: async () => {
        // 代表正式激活链路写回的权威状态。
        return "ws-formal";
      },
    });
    setLatest(appState({ currentSessionWorkspaceId: "ws-background" }));

    hook.activateGatewayWorkspaceInBackground("ws-background");
    await flush();
    expect(calls.refreshStatuses).toEqual(["ws-background"]);

    const formalActivation = hook.activateGatewayWorkspace("ws-formal");
    await act(async () => {
      await formalActivation;
    });
    expect(calls.finish).toEqual([
      {
        preferredSessionId: undefined,
        options: { checkGatewayWorkspaceHealth: false, reuseCurrentUiSettings: true },
      },
    ]);
    // 正式激活完成后写入权威状态，后台迟到结果不得覆盖它。
    setLatest(appState({
      currentSessionWorkspaceId: "ws-formal",
      agents: [formalAgent],
      status: "正式激活完成",
    }));

    backgroundAgents.resolve([agent("agent-background")]);
    await flush();
    expect(state().agents).toEqual([formalAgent]);
    expect(state().status).toBe("正式激活完成");
  });

  test("后台 Agent 列表的迟到失败不写入错误状态", async () => {
    spyOnGatewayApi("activateGatewayWorkspace").mockImplementation(
      async (_port, workspaceId) => workspaceId,
    );
    const backgroundAgents = deferred<Agent[]>();
    spyOnApi("listAgents").mockImplementation(async () => await backgroundAgents.promise);
    const { hook, state, setLatest } = await mountHook({ appliedWorkspaceId: "ws-formal" });
    setLatest(appState({ currentSessionWorkspaceId: "ws-background" }));

    hook.activateGatewayWorkspaceInBackground("ws-background");
    await flush();

    const formalActivation = hook.activateGatewayWorkspace("ws-formal");
    await act(async () => {
      await formalActivation;
    });
    setLatest(appState({
      currentSessionWorkspaceId: "ws-formal",
      status: "正式激活完成",
    }));

    backgroundAgents.reject(new Error("后台列表不可用"));
    await flush();
    expect(state().gatewayError).toBeNull();
    expect(state().status).toBe("正式激活完成");
  });

  test("同一工作区上正式入口只靠序列号作废在途后台激活结果", async () => {
    spyOnGatewayApi("activateGatewayWorkspace").mockImplementation(
      async (_port, workspaceId) => workspaceId,
    );
    const backgroundAgents = deferred<Agent[]>();
    spyOnApi("listAgents").mockImplementation(async () => await backgroundAgents.promise);
    const formalAgent = agent("agent-formal");
    const { hook, state, setLatest } = await mountHook({ appliedWorkspaceId: "ws-same" });
    // 前后台指向同一工作区，latestStateRef 的工作区判断无法区分，只剩序列号一道闸。
    setLatest(appState({ currentSessionWorkspaceId: "ws-same" }));

    hook.activateGatewayWorkspaceInBackground("ws-same");
    await flush();

    const formalActivation = hook.activateGatewayWorkspace("ws-same");
    await act(async () => {
      await formalActivation;
    });
    setLatest(appState({
      currentSessionWorkspaceId: "ws-same",
      agents: [formalAgent],
      status: "正式激活完成",
    }));

    backgroundAgents.resolve([agent("agent-background")]);
    await flush();

    expect(state().agents).toEqual([formalAgent]);
    expect(state().status).toBe("正式激活完成");
  });

  test("被取代的等待任务不发起激活请求：后台激活尚未开始就被正式激活顶替", async () => {
    const calls: Array<[number, string]> = [];
    spyOnGatewayApi("activateGatewayWorkspace").mockImplementation(
      async (port, workspaceId) => {
        calls.push([port, workspaceId]);
        return workspaceId;
      },
    );
    const { hook, calls: hookCalls, setLatest } = await mountHook({
      appliedWorkspaceId: "ws-formal",
    });
    setLatest(appState({ currentSessionWorkspaceId: "ws-background" }));

    // 两次入队之间不冲刷微任务，后台激活的任务体保持未开始执行。
    hook.activateGatewayWorkspaceInBackground("ws-background");
    const formalActivation = hook.activateGatewayWorkspace("ws-formal");
    await act(async () => {
      await formalActivation;
    });
    await flush();

    // latest-only 语义：被顶替的后台激活必须整条链路跳过，连激活请求都不发。
    expect(calls).toEqual([[API_PORT, "ws-formal"]]);
    // 后台激活被跳过且序列号已作废，收尾分支不得刷新 Gateway 状态。
    expect(hookCalls.refreshStatuses).toEqual(["ws-formal"]);
  });

  test("正式激活被随后入队的后台激活取代时显式失败并复位切换态", async () => {
    const calls: Array<[number, string]> = [];
    spyOnGatewayApi("activateGatewayWorkspace").mockImplementation(
      async (port, workspaceId) => {
        calls.push([port, workspaceId]);
        return workspaceId;
      },
    );
    const { hook, state, calls: hookCalls } = await mountHook({
      // 两个标志都置真，才能验证失败分支确实把它们压回 false，否则断言恒真。
      initialState: appState({ isBootstrapping: true, workspaceSwitching: true }),
    });

    const formalActivation = hook.activateGatewayWorkspace("ws-formal");
    hook.activateGatewayWorkspaceInBackground("ws-background");
    // 正式激活的任务体从未开始执行，必须让调用方拿到明确失败而不是假成功。
    await act(async () => {
      await expect(formalActivation).rejects.toThrow("工作区激活已被更新的激活请求取代");
    });
    await flush();

    expect(calls).toEqual([[API_PORT, "ws-background"]]);
    // 正式激活的收尾刷新不会发生，只剩后台激活的收尾分支在跑。
    expect(hookCalls.refreshStatuses).toEqual(["ws-background"]);
    // 被取代的正式激活必须收敛为可见失败态，且不得把切换态永久卡真。
    const next = state();
    expect(next.workspaceSwitching).toBe(false);
    expect(next.gatewayError).toBe("工作区激活已被更新的激活请求取代，ws-formal 未生效");
    expect(next.error).toBe("工作区激活已被更新的激活请求取代，ws-formal 未生效");
    expect(next.status).toBe("工作区切换失败");
    expect(next.isBootstrapping).toBe(false);
  });

  test("正式激活被另一个正式激活顶替时不得清掉新激活正在收敛的切换态", async () => {
    const pending: Array<{
      resolve: (value: string) => void;
      reject: (cause: unknown) => void;
      workspaceId: string;
    }> = [];
    spyOnGatewayApi("activateGatewayWorkspace").mockImplementation(
      (_port, workspaceId) =>
        new Promise<string>((resolve, reject) => {
          pending.push({ resolve, reject, workspaceId });
        }),
    );
    spyOnApi("listAgents").mockResolvedValue([]);
    const { hook, state } = await mountHook({
      // 忠实还原 resetWorkspaceScopedState 的置位，才能观察旧任务是否反向清掉它。
      resetWorkspaceScopedState: (previous) => ({
        ...previous,
        workspaceSwitching: true,
        error: null,
        status: "正在切换工作区",
      }),
      appliedWorkspaceId: "ws-b",
    });

    const formalActivationA = hook.activateGatewayWorkspace("ws-a");
    await flush();
    expect(pending.map((entry) => entry.workspaceId)).toEqual(["ws-a"]);

    // 正式激活 B 入队顶替 A，B 的 resetWorkspaceScopedState 已把切换态置真。
    const formalActivationB = hook.activateGatewayWorkspace("ws-b");
    expect(state().workspaceSwitching).toBe(true);

    // A 的请求失败：A 已被 B 顶替，只允许失败传播，不得写回任何状态。
    pending[0].reject(new Error("A 激活接口失败"));
    await act(async () => {
      await expect(formalActivationA).rejects.toThrow("A 激活接口失败");
    });
    await flush();

    // B 正在收敛的切换态与错误位必须保持原样，不能被旧任务 A 污染。
    expect(state().workspaceSwitching).toBe(true);
    expect(state().gatewayError).toBeNull();
    expect(state().error).toBeNull();
    expect(state().status).toBe("正在切换工作区");

    // 清理：让 B 正常跑完，避免悬挂的任务体影响 unmount。
    expect(pending.map((entry) => entry.workspaceId)).toEqual(["ws-a", "ws-b"]);
    pending[1].resolve("ws-b");
    await act(async () => {
      await formalActivationB;
    });
  });

  test("正式激活已开始执行后被后台激活顶替且自身失败时仍显式失败", async () => {
    const failure = new Error("正式激活接口失败");
    const pending: Array<{
      resolve: (value: string) => void;
      reject: (cause: unknown) => void;
      workspaceId: string;
    }> = [];
    spyOnGatewayApi("activateGatewayWorkspace").mockImplementation(
      (_port, workspaceId) =>
        new Promise<string>((resolve, reject) => {
          pending.push({ resolve, reject, workspaceId });
        }),
    );
    spyOnApi("listAgents").mockResolvedValue([]);
    const { hook, state } = await mountHook({
      initialState: appState({ isBootstrapping: true, workspaceSwitching: true }),
    });

    const formalActivation = hook.activateGatewayWorkspace("ws-formal");
    // 冲刷到正式激活的任务体已进入执行并停在激活请求上。
    await flush();
    expect(pending.map((entry) => entry.workspaceId)).toEqual(["ws-formal"]);

    // 正式激活仍在途时后台激活入队顶替它，随后正式请求失败。
    hook.activateGatewayWorkspaceInBackground("ws-background");
    pending[0].reject(failure);
    // 队列守卫会吞掉被顶替任务的 rejection，调用方绝不能因此拿到假成功。
    await act(async () => {
      await expect(formalActivation).rejects.toBe(failure);
    });
    await flush();

    // 被顶替的失败正式激活必须把切换态收敛掉，不能永久卡真。
    const next = state();
    expect(next.workspaceSwitching).toBe(false);
    expect(next.gatewayError).toBe("正式激活接口失败");
    expect(next.error).toBe("正式激活接口失败");
    expect(next.status).toBe("工作区切换失败");
    expect(next.isBootstrapping).toBe(false);
  });
});

describe("useGatewayWorkspaceActivation 后台激活", () => {
  test("成功时调用激活与 Agent 列表接口并按字段名写回状态", async () => {
    const agents = [agent("agent-a"), agent("agent-b")];
    const activate = spyOnGatewayApi("activateGatewayWorkspace").mockResolvedValue("ws-background");
    const listAgents = spyOnApi("listAgents").mockResolvedValue(agents);
    const { hook, state, setLatest, calls } = await mountHook();
    setLatest(appState({ currentSessionWorkspaceId: "ws-background" }));

    hook.activateGatewayWorkspaceInBackground("ws-background");
    await flush();

    expect(activate).toHaveBeenCalledTimes(1);
    expect(activate).toHaveBeenCalledWith(API_PORT, "ws-background");
    expect(listAgents).toHaveBeenCalledTimes(1);
    expect(listAgents).toHaveBeenCalledWith(API_PORT, "ws-background");
    expect(state().agents).toEqual(agents);
    expect(calls.invalidate).toBe(1);
    expect(calls.refreshStatuses).toEqual(["ws-background"]);
  });

  test("apiPort 为空时回退到默认后端端口", async () => {
    const activate = spyOnGatewayApi("activateGatewayWorkspace").mockResolvedValue("ws-background");
    spyOnApi("listAgents").mockResolvedValue([]);
    const { hook, setLatest } = await mountHook({ apiPort: null });
    setLatest(appState({ currentSessionWorkspaceId: "ws-background" }));

    hook.activateGatewayWorkspaceInBackground("ws-background");
    await flush();

    expect(activate).toHaveBeenCalledWith(DEFAULT_BACKEND_PORT, "ws-background");
  });

  test("激活失败时写入后台切换失败文案且不抛出", async () => {
    const failure = new Error("激活被拒绝");
    spyOnGatewayApi("activateGatewayWorkspace").mockRejectedValue(failure);
    const listAgents = spyOnApi("listAgents").mockResolvedValue([]);
    const { hook, state, setLatest } = await mountHook();
    setLatest(appState({ currentSessionWorkspaceId: "ws-background" }));

    hook.activateGatewayWorkspaceInBackground("ws-background");
    await flush();

    expect(listAgents).not.toHaveBeenCalled();
    const next = state();
    expect(next.gatewayError).toBe("激活被拒绝");
    expect(next.status).toBe("后台切换工作区失败: 激活被拒绝");
  });

  test("Agent 列表失败时写入后台加载 Agent 失败文案且不抛出", async () => {
    spyOnGatewayApi("activateGatewayWorkspace").mockResolvedValue("ws-background");
    spyOnApi("listAgents").mockRejectedValue(new Error("Agent 接口不可用"));
    const { hook, state, setLatest } = await mountHook();
    setLatest(appState({ currentSessionWorkspaceId: "ws-background" }));

    hook.activateGatewayWorkspaceInBackground("ws-background");
    await flush();

    const message = "后台加载工作区 Agent 失败: Agent 接口不可用";
    const next = state();
    expect(next.gatewayError).toBe(message);
    expect(next.status).toBe(message);
  });

  test("读的是最新的 latestStateRef，触发后再更新 ref 也能写回结果", async () => {
    const agents = [agent("agent-latest")];
    spyOnGatewayApi("activateGatewayWorkspace").mockResolvedValue("ws-background");
    const pending = deferred<Agent[]>();
    spyOnApi("listAgents").mockImplementation(async () => await pending.promise);
    const { hook, state, latestStateRef } = await mountHook();
    // 触发时 ref 仍指向别的工作区，闭包若捕获旧值就会丢弃结果。
    latestStateRef.current = appState({ currentSessionWorkspaceId: "ws-other" });

    hook.activateGatewayWorkspaceInBackground("ws-background");
    await flush();

    latestStateRef.current = appState({ currentSessionWorkspaceId: "ws-background" });
    pending.resolve(agents);
    await flush();

    expect(state().agents).toEqual(agents);
  });

  test("结果返回前 ref 切走后丢弃 Agent 结果，不写入过期数据", async () => {
    spyOnGatewayApi("activateGatewayWorkspace").mockResolvedValue("ws-background");
    const pending = deferred<Agent[]>();
    spyOnApi("listAgents").mockImplementation(async () => await pending.promise);
    const { hook, state, latestStateRef } = await mountHook();
    latestStateRef.current = appState({ currentSessionWorkspaceId: "ws-background" });

    hook.activateGatewayWorkspaceInBackground("ws-background");
    await flush();

    latestStateRef.current = appState({ currentSessionWorkspaceId: "ws-other" });
    pending.resolve([agent("agent-stale")]);
    await flush();

    expect(state().agents).toEqual([]);
    expect(state().gatewayError).toBeNull();
  });
});

describe("useGatewayWorkspaceActivation 正式激活", () => {
  test("成功时先重置工作区状态并复用同一端口调用激活接口", async () => {
    const activate = spyOnGatewayApi("activateGatewayWorkspace").mockResolvedValue("ws-formal");
    const { hook, calls, setLatest } = await mountHook({
      currentSessionId: PREFERRED_SESSION_ID,
      appliedWorkspaceId: "ws-formal",
    });
    setLatest(appState({ currentSessionWorkspaceId: "ws-formal" }));

    await hook.activateGatewayWorkspace("ws-formal", PREFERRED_SESSION_ID);

    expect(activate).toHaveBeenCalledWith(API_PORT, "ws-formal");
    expect(calls.invalidate).toBe(1);
    expect(calls.reset).toBe(1);
    expect(calls.finish).toEqual([
      {
        preferredSessionId: PREFERRED_SESSION_ID,
        options: { checkGatewayWorkspaceHealth: false, reuseCurrentUiSettings: true },
      },
    ]);
    expect(calls.refreshStatuses).toEqual(["ws-formal"]);
  });

  test("刷新被作废未生效时显式失败、不刷新 Gateway 状态并复位切换态", async () => {
    spyOnGatewayApi("activateGatewayWorkspace").mockResolvedValue("ws-formal");
    const { hook, state, calls } = await mountHook({
      // 模拟 invalidateWorkspaceRefreshes 作废本次刷新：刷新没写回
      // activeGatewayWorkspaceId，激活并未生效，绝不能给调用方假成功。
      initialState: appState({ isBootstrapping: true, workspaceSwitching: true }),
      finishWorkspaceRefresh: async () => null,
    });

    await act(async () => {
      await expect(hook.activateGatewayWorkspace("ws-formal"))
        .rejects.toThrow("工作区激活未生效");
    });

    expect(calls.finish).toHaveLength(1);
    expect(calls.refreshStatuses).toEqual([]);
    const next = state();
    expect(next.workspaceSwitching).toBe(false);
    expect(next.gatewayError).toBe("工作区激活未生效：ws-formal 的工作区刷新已被更新的请求作废");
    expect(next.error).toBe("工作区激活未生效：ws-formal 的工作区刷新已被更新的请求作废");
    expect(next.status).toBe("工作区切换失败");
    expect(next.isBootstrapping).toBe(false);
  });

  test("刷新生效但活动工作区被健康回退切到别处时显式失败并复位切换态", async () => {
    spyOnGatewayApi("activateGatewayWorkspace").mockResolvedValue("ws-offline");
    const { hook, state, calls } = await mountHook({
      // 请求 offline 工作区：bootstrap 的自动健康回退会把活动工作区改到
      // ws-healthy，请求的 ws-offline 从未成为活动工作区，绝不能假成功。
      initialState: appState({ isBootstrapping: true, workspaceSwitching: true }),
      appliedWorkspaceId: "ws-healthy",
    });

    const message = "工作区激活未生效：ws-offline 未成为活动工作区（当前活动工作区为 ws-healthy）";
    await act(async () => {
      await expect(hook.activateGatewayWorkspace("ws-offline")).rejects.toThrow(message);
    });

    expect(calls.finish).toHaveLength(1);
    // 请求的工作区没有生效，不应刷新它的 Gateway 状态。
    expect(calls.refreshStatuses).toEqual([]);
    const next = state();
    expect(next.workspaceSwitching).toBe(false);
    expect(next.gatewayError).toBe(message);
    expect(next.error).toBe(message);
    expect(next.status).toBe("工作区切换失败");
    expect(next.isBootstrapping).toBe(false);
  });

  test("激活失败时写入五个失败字段并原样抛出", async () => {
    const failure = new Error("激活接口不可用");
    spyOnGatewayApi("activateGatewayWorkspace").mockRejectedValue(failure);
    // 初始必须为真，失败分支把两者压回 false 才是可观测的收敛，否则断言恒真。
    const { hook, state } = await mountHook({
      initialState: appState({ isBootstrapping: true, workspaceSwitching: true }),
    });

    await expect(hook.activateGatewayWorkspace("ws-formal")).rejects.toBe(failure);

    const next = state();
    expect(next.gatewayError).toBe("激活接口不可用");
    expect(next.error).toBe("激活接口不可用");
    expect(next.status).toBe("工作区切换失败");
    expect(next.workspaceSwitching).toBe(false);
    expect(next.isBootstrapping).toBe(false);
  });

  test("工作区刷新失败时同样走失败路径并原样抛出", async () => {
    const failure = new Error("刷新失败");
    spyOnGatewayApi("activateGatewayWorkspace").mockResolvedValue("ws-formal");
    // 同上：初始为真才能验证失败分支确实复位了这两个标志。
    const { hook, state, calls } = await mountHook({
      initialState: appState({ isBootstrapping: true, workspaceSwitching: true }),
      finishWorkspaceRefresh: async () => {
        throw failure;
      },
    });

    await expect(hook.activateGatewayWorkspace("ws-formal")).rejects.toThrow("刷新失败");

    expect(calls.refreshStatuses).toEqual([]);
    const next = state();
    expect(next.gatewayError).toBe("刷新失败");
    expect(next.error).toBe("刷新失败");
    expect(next.status).toBe("工作区切换失败");
    expect(next.workspaceSwitching).toBe(false);
    expect(next.isBootstrapping).toBe(false);
  });
});

describe("useGatewayWorkspaceActivation 刷新 Gateway 状态", () => {
  test("成功时以当前会话 ID 调用工作区刷新并只传一个参数", async () => {
    const { hook, state, calls } = await mountHook({ currentSessionId: PREFERRED_SESSION_ID });

    await hook.refreshGatewayState();

    expect(calls.finish).toEqual([
      { preferredSessionId: PREFERRED_SESSION_ID, options: undefined },
    ]);
    const next = state();
    expect(next.gatewayError).toBeNull();
    expect(next.error).toBeNull();
    expect(next.status).toBe("正在刷新 Gateway 状态");
  });

  test("刷新前已有错误时把 isBootstrapping 置为真", async () => {
    const { hook, state } = await mountHook({
      initialState: appState({ error: "既有错误", isBootstrapping: false }),
    });

    let pending!: Promise<void>;
    await act(async () => {
      pending = hook.refreshGatewayState();
      await Promise.resolve();
    });
    // 成功路径不再重置 isBootstrapping，状态保持刷新前推导出的值。
    expect(state().isBootstrapping).toBe(true);
    expect(state().error).toBeNull();
    expect(state().status).toBe("正在刷新 Gateway 状态");

    await act(async () => {
      await pending;
    });
    expect(state().isBootstrapping).toBe(true);
  });

  test("刷新失败时写入失败文案并原样抛出", async () => {
    const failure = new Error("刷新接口不可用");
    const { hook, state } = await mountHook({
      finishWorkspaceRefresh: async () => {
        throw failure;
      },
    });

    await expect(hook.refreshGatewayState()).rejects.toBe(failure);

    const next = state();
    expect(next.gatewayError).toBe("刷新接口不可用");
    expect(next.error).toBe("刷新接口不可用");
    expect(next.status).toBe("刷新 Gateway 状态失败: 刷新接口不可用");
  });
});
