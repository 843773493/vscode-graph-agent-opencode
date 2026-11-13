import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import React from "react";
import { act } from "react-test-renderer";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import type { AppState } from "../../types/frontend";
import type { Session } from "../../types/backend";
import {
  apiSuccessResponse,
  installGatewayFetch as installGatewayFetchMock,
  restoreGatewayTestEnvironment,
} from "../../tests/fixtures/session/gatewayEnvironment";
import {
  installVisibleTestDocument,
  installGatewayTestWindow,
} from "../../tests/fixtures/session/gatewayBrowserGlobals";
import { mountReactTestHarness } from "../../tests/fixtures/session/reactHarness";
import {
  mountSessionGoalControllerHarness,
  createSessionGoalControllerHarness,
} from "../../tests/fixtures/session/sessionGoalController";

const OWNER_A = { gatewayId: "gateway-goal-a", workspaceId: "00000000-0000-4000-8000-000000000070" };
const OWNER_B = { ...OWNER_A, gatewayId: "gateway-goal-b" };
const WORKSPACE_A = gatewayWorkspaceKey(OWNER_A);
const WORKSPACE_B = gatewayWorkspaceKey(OWNER_B);
let expectsBusinessRequest = false;
const businessRequests: string[] = [];

function installGatewayFetch(...[handler, options]: Parameters<typeof installGatewayFetchMock>): void {
  expectsBusinessRequest = true;
  installGatewayFetchMock((request) => {
    const requestUrl = new URL(request.url, "http://localhost");
    expect(/^\/api\/v1\/sessions\/[^/]+\/(?:goal(?:-operations\/[^/]+)?|pending-requests)$/.test(request.path)).toBe(true);
    const headers = new Headers(request.init?.headers);
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).not.toBeNull();
    expect([OWNER_A.gatewayId, OWNER_B.gatewayId]).toContain(headers.get("X-BoxTeam-Target-Gateway-Id")!);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(OWNER_A.workspaceId);
    businessRequests.push(request.path);
    const response = handler(request);
    if (response !== undefined) return response;
    const executionMatch = request.path.match(/^\/api\/v1\/sessions\/([^/]+)\/pending-requests$/);
    const executionThreadId = requestUrl.searchParams.get("thread_id");
    if (executionMatch && executionThreadId) return apiSuccessResponse({
      session_id: decodeURIComponent(executionMatch[1]!),
      thread_id: executionThreadId,
      active_job_id: null, requests: [], snapshot_version: 0,
    });
    return undefined;
  }, options);
}

afterEach(() => {
  restoreGatewayTestEnvironment();
  const count = businessRequests.length;
  const expected = expectsBusinessRequest;
  businessRequests.length = 0;
  expectsBusinessRequest = false;
  if (expected) expect(count).toBeGreaterThan(0);
});

const GOAL_PORT = 49_406;

function state(): AppState {
  return {
    currentSession: {
      session_id: "session-test",
      thread_id: "thr_goal_main",
      workspace_id: OWNER_A.workspaceId,
      title: "测试会话",
      current_agent_id: "default",
      created_at: "2026-09-02T00:00:00Z",
      updated_at: "2026-09-02T00:00:00Z",
    },
    currentSessionWorkspaceId: WORKSPACE_A,
    currentGoal: null,
    currentGoalSessionId: null,
    activeJobIdsBySession: new Map(),
    goalLoading: false,
    goalError: null,
  } as unknown as AppState;
}

/** 安装会话 Goal 控制器需要的 Gateway window 与可见性 document 桩。 */
function installSessionGoalControllerGlobals(): void {
  installGatewayTestWindow(GOAL_PORT);
  installVisibleTestDocument();
}

beforeEach(installSessionGoalControllerGlobals);

/** 标准 Goal 响应体，用例只覆盖自己关心的字段。 */
function goalResponse(overrides: Record<string, unknown> = {}): Response {
  const { goal_id, session_id, thread_id, objective, status, token_budget, tokens_used,
    time_used_seconds, created_at, updated_at, revision, control_revision, sync_version } = {
    goal_id: "goal-test",
    session_id: "session-test",
    thread_id: "thr_goal_main",
    objective: "验证请求合并",
    status: "active",
    token_budget: null,
    tokens_used: 0,
    time_used_seconds: 0,
    created_at: "2026-09-02T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
    revision: 1,
    control_revision: 1,
    sync_version: 1,
    ...overrides,
  };
  return apiSuccessResponse({ session_id, thread_id, goal: {
    goal_id, session_id, thread_id, objective, status, token_budget, tokens_used,
    time_used_seconds, created_at, updated_at, revision, control_revision, sync_version,
  }, control_revision, sync_version });
}

function operationResponse(overrides: Record<string, unknown> = {}): Response {
  return apiSuccessResponse({
    session_id: "session-test", thread_id: "thr_goal_main", operation_id: "op_goal",
    status: "applied", control_revision: 1, sync_version: 1,
    goal: { goal_id: "goal-test", session_id: "session-test", thread_id: "thr_goal_main",
      objective: "Goal", status: "active", token_budget: null, tokens_used: 0,
      time_used_seconds: 0, revision: 1, control_revision: 1, sync_version: 1,
      created_at: "2026-09-02T00:00:00Z", updated_at: "2026-09-02T00:00:00Z" },
    ...overrides,
  });
}

/** 挂载自持状态机的 Goal Harness，收敛「Harness + 闭包读写器」样板。 */
async function mountGoalHarness(options: {
  initial: AppState;
  flushes?: number;
}) {
  const harness = createSessionGoalControllerHarness({
    apiPort: GOAL_PORT,
    initial: options.initial,
  });
  const unmount = await mountReactTestHarness(harness.Harness, options.flushes ?? 2);
  return { ...harness, unmount };
}

describe("useSessionGoalController 请求合并", () => {
  test("订阅建立后撤销订阅前读取，并确认加载窗口内已提交的新版本", async () => {
    let finishOld!: (response: Response) => void;
    let reads = 0;
    installGatewayFetch(({ path }) => {
      if (!path.endsWith("/goal")) return undefined;
      reads += 1;
      if (reads === 1) return new Promise<Response>((resolve) => { finishOld = resolve; });
      return goalResponse({ objective: "订阅窗口内的更新", control_revision: 2, sync_version: 2 });
    });
    const mounted = await mountGoalHarness({ initial: state() });
    await act(async () => { await mounted.controller().confirmSessionState(true); });
    expect(reads).toBe(2);
    expect(mounted.state().currentGoal?.objective).toBe("订阅窗口内的更新");
    await act(async () => { finishOld(goalResponse()); await Promise.resolve(); });
    expect(mounted.state().currentGoalSyncVersion).toBe(2);
    mounted.unmount();
  });

  test("隐藏页面撤销在途 Goal 与执行摘要，忽略取消的 transport 也不能发布", async () => {
    const document = globalThis.document;
    const originalVisibility = Object.getOwnPropertyDescriptor(document, "visibilityState");
    let visibilityChange: (() => void) | undefined;
    const add = document.addEventListener;
    document.addEventListener = ((name: string, handler: EventListenerOrEventListenerObject) => {
      if (name === "visibilitychange") visibilityChange = handler as () => void;
    }) as typeof document.addEventListener;
    let finishGoal!: (response: Response) => void;
    let finishExecution!: (response: Response) => void;
    installGatewayFetch(({ path }) => {
      if (path.endsWith("/goal")) return new Promise<Response>((resolve) => { finishGoal = resolve; });
      if (path.endsWith("/pending-requests")) return new Promise<Response>((resolve) => { finishExecution = resolve; });
      return undefined;
    });
    const mounted = await mountGoalHarness({ initial: state() });
    try {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
      await act(async () => {
        visibilityChange!();
        finishGoal(goalResponse({ objective: "隐藏后的迟到读取" }));
        finishExecution(apiSuccessResponse({ session_id: "session-test", thread_id: "thr_goal_main",
          active_job_id: "late-job", requests: [], snapshot_version: 8 }));
        await Promise.resolve();
      });
      expect(mounted.state().currentGoal).toBeNull();
      expect(mounted.state().activeJobIdsBySession.size).toBe(0);
    } finally {
      mounted.unmount();
      document.addEventListener = add;
      if (originalVisibility) Object.defineProperty(document, "visibilityState", originalVisibility);
      else Reflect.deleteProperty(document, "visibilityState");
    }
  });

  test("每 30 秒仅在可见时向后端确认 Goal 快照", async () => {
    const previousSetInterval = window.setInterval;
    let scheduledDelay: number | undefined;
    let intervalHandler: (() => void) | null = null;
    window.setInterval = ((handler: TimerHandler, delay?: number) => {
      if (delay === 30_000) {
        intervalHandler = handler as () => void;
        scheduledDelay = delay;
        return previousSetInterval(() => {}, delay);
      }
      return previousSetInterval(handler as (...args: never[]) => void, delay);
    }) as typeof window.setInterval;
    let executionReads = 0;
    const requestedGoalWatermarks: number[] = [];
    installGatewayFetch(({ path, url }) => {
      if (path.endsWith("/pending-requests") && new URL(url, "http://localhost").searchParams.get("thread_id") === "thr_goal_main") {
        executionReads += 1;
        return apiSuccessResponse({ session_id: "session-test", thread_id: "thr_goal_main",
          active_job_id: "job_authoritative", requests: [], snapshot_version: executionReads });
      }
      if (path.endsWith("/goal")) {
        const minimumSyncVersion = Number(new URL(url, "http://localhost").searchParams.get("minimum_sync_version"));
        requestedGoalWatermarks.push(minimumSyncVersion);
        return apiSuccessResponse({ session_id: "session-test", thread_id: "thr_goal_main",
          goal: { goal_id: "goal-test", session_id: "session-test", thread_id: "thr_goal_main",
            objective: "没有事件通知的已提交目标", status: "paused", token_budget: null,
            tokens_used: 0, time_used_seconds: 0, revision: 2, control_revision: 1,
            sync_version: minimumSyncVersion + 1, created_at: "2026-09-02T00:00:00Z",
            updated_at: "2026-09-02T00:00:01Z" },
          control_revision: 1, sync_version: minimumSyncVersion + 1 });
      }
      return undefined;
    });
    const mounted = await mountSessionGoalControllerHarness({ initial: state() });
    expect(intervalHandler).toBeFunction();
    expect(scheduledDelay).toBe(30_000);
    expect(executionReads).toBeGreaterThan(0);
    const initialReads = executionReads;
    await act(async () => { intervalHandler!(); await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(executionReads).toBeGreaterThan(initialReads);
    expect(mounted.state().activeJobIdsBySession.get(JSON.stringify([WORKSPACE_A, "session-test", "thr_goal_main"])))
      .toBe("job_authoritative");
    expect(mounted.state().sessionExecutionSyncError).toBeNull();
    expect(mounted.state().currentGoalControlRevision).toBe(1);
    expect(requestedGoalWatermarks).toContain(1);
    expect(mounted.state().currentGoal?.objective).toBe("没有事件通知的已提交目标");
    expect(mounted.state().currentGoalSyncVersion).toBe(2);
    mounted.unmount();
    window.setInterval = previousSetInterval;
  });

  test("同名会话切到另一工作区后拒绝迟到的 Goal 读取错误", async () => {
    let rejectOldGoal!: (error: Error) => void;
    installGatewayFetch(({ path, init }) => {
      if (!path.endsWith("/goal")) return undefined;
      const gatewayId = new Headers(init?.headers).get("X-BoxTeam-Target-Gateway-Id");
      if (gatewayId === OWNER_B.gatewayId) return apiSuccessResponse({
        session_id: "session-test", thread_id: "thr_goal_main", goal: null,
        control_revision: 0, sync_version: 0,
      });
      return new Promise<Response>((_, reject) => { rejectOldGoal = reject; });
    });
    const mounted = await mountGoalHarness({ initial: state() });
    let pending!: Promise<unknown>;
    await act(async () => {
      pending = mounted.controller().refreshGoal().catch((error: unknown) => error);
      await Promise.resolve();
    });
    await act(async () => {
      mounted.setState((previous) => ({
        ...previous, currentSessionWorkspaceId: WORKSPACE_B, goalError: null,
      }));
      await Promise.resolve();
    });
    await act(async () => {
      rejectOldGoal(new Error("旧工作区读取失败"));
      expect(await pending).toBeNull();
    });
    expect(mounted.state().currentSessionWorkspaceId).toBe(WORKSPACE_B);
    expect(mounted.state().goalError).toBeNull();
    mounted.unmount();
  });

  test("后端返回 child Goal 时拒绝写入 main 镜像并显示错误", async () => {
    installGatewayFetch(({ path, method }) => path.endsWith("/goal") && method === "GET"
      ? goalResponse({ thread_id: "thr_child" }) : undefined);
    const mounted = await mountSessionGoalControllerHarness({ initial: state() });
    let failure: unknown;
    await act(async () => {
      failure = await mounted.controller().refreshGoal().catch((error: unknown) => error);
    });
    expect(failure).toBeInstanceOf(Error);
    expect(mounted.state().currentGoal).toBeNull();
    expect(mounted.state().goalError).toContain("main Thread 不一致");
    mounted.unmount();
  });
  test("并发的初始读取、聚焦校准共享同一个 Goal 请求", async () => {
    let releaseGoal!: (response: Response) => void;
    let goalRequests = 0;
    installGatewayFetch(({ path }) => {
      if (path.includes("/api/v1/sessions/session-test/goal")) {
        goalRequests += 1;
        return new Promise<Response>((resolve) => {
          releaseGoal = resolve;
        });
      }
      return undefined;
    }, { token: "local-test-token" });

    const { controller, unmount } = await mountSessionGoalControllerHarness({
      initial: state(),
    });

    let firstRequest: Promise<unknown>;
    let secondRequest: Promise<unknown>;
    await act(async () => {
      firstRequest = controller().refreshGoal();
      secondRequest = controller().refreshGoal(undefined, { silent: true });
      await Promise.resolve();
    });
    expect(goalRequests).toBe(1);

    await act(async () => {
      releaseGoal(goalResponse());
      await Promise.all([firstRequest!, secondRequest!]);
    });
    unmount();
  });

  test("读取 Goal 失败时把原始错误文本写入 goalError", async () => {
    installGatewayFetch(({ path }) => {
      if (path.includes("/api/v1/sessions/session-test/goal")) {
        throw new TypeError("读取 Goal 的网络请求失败");
      }
      return undefined;
    }, { token: "local-test-token" });

    const mounted = await mountSessionGoalControllerHarness({ initial: state() });

    let failure: unknown;
    await act(async () => {
      failure = await mounted.controller().refreshGoal().catch((error: unknown) => error);
    });

    // 错误文案必须来自原始抛出物，而不是任何被换掉的常量占位。
    expect(mounted.state().goalError).toBe("读取 Goal 的网络请求失败");
    expect(failure).toBeInstanceOf(TypeError);
    mounted.unmount();
  });

  test("重取校准再次失败时把二次错误的原始文本拼进抛出的错误", async () => {
    let goalRequests = 0;
    installGatewayFetch(({ path }) => {
      if (path.includes("/api/v1/sessions/session-test/goal")) {
        goalRequests += 1;
        throw new TypeError(goalRequests === 1 ? "首次写 Goal 失败" : "二次重取失败");
      }
      return undefined;
    }, { token: "local-test-token" });

    const { controller, unmount } = await mountSessionGoalControllerHarness({
      initial: state(),
    });

    let failure: Error | undefined;
    await act(async () => {
      failure = await controller().updateGoal({ objective: "验证错误拼接" })
        .then(
          () => undefined,
          (error: Error) => error,
        );
    });

    expect(failure?.message).toContain("二次重取失败");
    expect(goalRequests).toBeGreaterThan(1);
    unmount();
  });
});

describe("useSessionGoalController 读写顺序", () => {
  test("创建 Goal 后忽略创建前发出的迟到空读取", async () => {
    let releaseRead!: (response: Response) => void;
    installGatewayFetch(({ path, method, init }) => {
      if (!path.includes("/api/v1/sessions/session-test/goal")) return undefined;
      if (method === "GET") {
        if (releaseRead) return new Promise<Response>((resolve) => { releaseRead = resolve; });
        return apiSuccessResponse({ session_id: "session-test", thread_id: "thr_goal_main",
          goal: null, control_revision: 0, sync_version: 0 });
      }
      if (method === "PUT") return operationResponse({ operation_id: JSON.parse(String(init?.body)).operation_id, goal: {
        goal_id: "goal-test", session_id: "session-test", thread_id: "thr_goal_main",
        objective: "新创建的目标", status: "active", token_budget: null, tokens_used: 0,
        time_used_seconds: 0, revision: 1, control_revision: 1, sync_version: 1,
        created_at: "2026-09-02T00:00:00Z", updated_at: "2026-09-02T00:00:00Z",
      } });
      return undefined;
    }, { token: "goal-read-write-order-token" });

    const mounted = await mountSessionGoalControllerHarness({ initial: state() });
    let pendingRead!: Promise<unknown>;
    await act(async () => {
      pendingRead = mounted.controller().refreshGoal();
      await Promise.resolve();
    });
    await act(async () => {
      await mounted.controller().updateGoal({
        objective: "新创建的目标",
        status: "active",
        replace: true,
      });
    });
    expect(mounted.state().currentGoal?.objective).toBe("新创建的目标");

    await act(async () => { await pendingRead; });

    expect(mounted.state().currentGoal?.objective).toBe("新创建的目标");
    mounted.unmount();
  });

  test("写入进行中启动的读取不能覆盖写入响应", async () => {
    let releaseRead!: (response: Response) => void;
    let releaseUpdate!: () => void;
    let getCount = 0;
    installGatewayFetch(({ path, method, init }) => {
      if (!path.includes("/api/v1/sessions/session-test/goal")) return undefined;
      if (method === "GET") {
        getCount += 1;
        if (getCount > 1) return new Promise<Response>((resolve) => { releaseRead = resolve; });
        return apiSuccessResponse({ session_id: "session-test", thread_id: "thr_goal_main",
          goal: null, control_revision: 0, sync_version: 0 });
      }
      if (method === "PUT") {
        return new Promise<Response>((resolve) => {
          releaseUpdate = () => resolve(operationResponse({ operation_id: JSON.parse(String(init?.body)).operation_id, goal: {
            goal_id: "goal-test", session_id: "session-test", thread_id: "thr_goal_main",
            objective: "写入中的目标", status: "active", token_budget: null, tokens_used: 0,
            time_used_seconds: 0, revision: 1, control_revision: 1, sync_version: 1,
            created_at: "2026-09-02T00:00:00Z", updated_at: "2026-09-02T00:00:00Z",
          } }));
        });
      }
      return undefined;
    }, { token: "goal-overlapping-read-token" });

    const mounted = await mountSessionGoalControllerHarness({ initial: state() });
    let pendingUpdate!: Promise<unknown>;
    let pendingRead!: Promise<unknown>;
    await act(async () => {
      pendingUpdate = mounted.controller().updateGoal({
        objective: "写入中的目标",
        status: "active",
        replace: true,
      });
      for (let attempt = 0; attempt < 20 && !releaseUpdate; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
      expect(releaseUpdate).toBeFunction();
    });
    await act(async () => {
      pendingRead = mounted.controller().refreshGoal();
      await Promise.resolve();
    });

    await act(async () => {
      releaseUpdate();
      await pendingUpdate;
    });
    await act(async () => {
      releaseRead(apiSuccessResponse({ session_id: "session-test", thread_id: "thr_goal_main",
        goal: null, control_revision: 0, sync_version: 0 }));
      await pendingRead;
    });

    expect(mounted.state().currentGoal?.objective).toBe("写入中的目标");
    mounted.unmount();
  });
});

describe("useSessionGoalController 跨会话守卫", () => {
  const sessionA = {
    session_id: "ses_a",
    thread_id: "thr_goal_main",
    workspace_id: OWNER_A.workspaceId,
    title: "会话 A",
    current_agent_id: "default",
    created_at: "2026-09-02T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
  } as unknown as Session;
  const sessionB = { ...sessionA, session_id: "ses_b", title: "会话 B" };

  test("在途读取回包前切走会话时不得把旧 Goal 写进新会话", async () => {
    let releaseGoal!: (response: Response) => void;
    installGatewayFetch(({ path }) => {
      if (path.includes("/api/v1/sessions/ses_a/goal")) {
        return new Promise<Response>((resolve) => {
          releaseGoal = resolve;
        });
      }
      return undefined;
    }, { token: "gc-race-token" });

    // 本用例必须在请求在途期间切换 currentSession，因此保留自持状态机的装配器。
    const mounted = await mountGoalHarness({
      initial: {
        currentSession: sessionA,
        currentSessionWorkspaceId: WORKSPACE_A,
        currentGoal: null,
        currentGoalSessionId: "ses_a",
        goalLoading: false,
        goalError: null,
      } as unknown as AppState,
    });

    let pending: Promise<unknown>;
    await act(async () => {
      pending = mounted.controller().refreshGoal({
        sessionId: "ses_a",
        threadId: "thr_goal_main",
        workspaceId: WORKSPACE_A,
      });
      await Promise.resolve();
    });
    await act(async () => {
      mounted.setState((prev) => ({
        ...prev,
        currentSession: sessionB,
        currentGoal: null,
        currentGoalSessionId: "ses_b",
      }));
      await Promise.resolve();
    });
    expect(mounted.state().currentSession?.session_id).toBe("ses_b");

    await act(async () => { expect(await pending!).toBeNull(); });

    // 迟到的 A 会话响应属于旧会话事实，绝不能污染已经切到的 B 会话。
    expect(mounted.state().currentSession?.session_id).toBe("ses_b");
    expect(mounted.state().currentGoal).toBeNull();
    expect(mounted.state().currentGoalSessionId).toBe("ses_b");
    mounted.unmount();
  });

  test("先设置后清除 Goal 时，迟到的设置回包不得复活已清除的 Goal", async () => {
    let releaseUpdate!: () => void;
    installGatewayFetch(({ path, method, init }) => {
      if (path.includes("/api/v1/sessions/session-test/goal")) {
        if (method === "GET") return apiSuccessResponse({ session_id: "session-test",
          thread_id: "thr_goal_main", goal: null, control_revision: 0, sync_version: 0 });
        if (method === "DELETE") {
          return apiSuccessResponse({ session_id: "session-test", thread_id: "thr_goal_main",
            operation_id: JSON.parse(String(init?.body)).operation_id, status: "applied", control_revision: 2, sync_version: 2 });
        }
        if (method === "PUT") {
          return new Promise<Response>((resolve) => {
            releaseUpdate = () => resolve(
              operationResponse({ operation_id: JSON.parse(String(init?.body)).operation_id, goal: { goal_id: "goal_late", session_id: "session-test",
                thread_id: "thr_goal_main", objective: "迟到的设置", status: "active",
                token_budget: null, tokens_used: 0, time_used_seconds: 0, revision: 1,
                control_revision: 1, sync_version: 1, created_at: "2026-09-02T00:00:00Z",
                updated_at: "2026-09-02T00:00:00Z" } }),
            );
          });
        }
      }
      return undefined;
    }, { token: "goal-order-token" });

    const { controller, state: readState, unmount } = await mountSessionGoalControllerHarness({
      initial: state(),
    });

    let updateRequest: Promise<unknown>;
    await act(async () => {
      updateRequest = controller().updateGoal({ objective: "迟到的设置" })
        .catch(() => undefined);
      for (let attempt = 0; attempt < 20 && !releaseUpdate; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
      expect(releaseUpdate).toBeFunction();
    });
    let clearRequest!: Promise<void>;
    await act(async () => { clearRequest = controller().clearGoal(); await Promise.resolve(); });
    releaseUpdate();
    await act(async () => {
      await updateRequest!;
      await clearRequest;
    });

    // 用户最后一次操作是清除：迟到的设置回包不得把它复活。
    expect(readState().currentGoal).toBeNull();
    unmount();
  });
});

describe("useSessionGoalController 会话身份守卫", () => {
  test("目标会话为空时 refreshGoal 清空残留 Goal 状态并短路，绝不发请求", async () => {
    let goalRequests = 0;
    installGatewayFetchMock(({ path }) => {
      if (path.includes("/api/v1/sessions/")) {
        goalRequests += 1;
        return apiSuccessResponse({});
      }
      return undefined;
    }, { token: "gc-empty-target-token" });

    // 切到空会话时 AppState 里可能仍残留上一个会话的 Goal；refreshGoal 收到
    // sessionless 目标必须把它清干净，装配器保留注入残留状态的能力。
    const mounted = await mountGoalHarness({
      initial: {
        currentSession: null,
        currentSessionWorkspaceId: null,
        currentGoal: null,
        currentGoalSessionId: null,
        goalLoading: false,
        goalError: null,
      } as unknown as AppState,
      flushes: 1,
    });

    await act(async () => {
      mounted.setState((prev) => ({
        ...prev,
        // 模拟上一个会话残留的 Goal 与错误文本。
        currentGoal: {
          goal_id: "goal_stale",
          session_id: "ses_stale",
          thread_id: "thr_goal_main",
          objective: "上一个会话的目标",
          status: "active",
          token_budget: null,
          tokens_used: 0,
          time_used_seconds: 0,
          created_at: "2026-09-02T00:00:00Z",
          updated_at: "2026-09-02T00:00:00Z",
        } as never,
        currentGoalSessionId: "ses_stale",
        goalError: "上一个会话的错误",
      }));
      await Promise.resolve();
    });
    expect(mounted.state().currentGoal?.objective).toBe("上一个会话的目标");

    let result: unknown = "unset";
    await act(async () => {
      result = await mounted.controller().refreshGoal({ sessionId: "", threadId: "", workspaceId: null });
    });

    // sessionless 目标必须清空 Goal 状态并短路返回 null，绝不能残留上一个会话。
    expect(result).toBeNull();
    expect(mounted.state().currentGoal).toBeNull();
    expect(mounted.state().currentGoalSessionId).toBeNull();
    expect(mounted.state().goalLoading).toBe(false);
    expect(mounted.state().goalError).toBeNull();
    // 短路必须在任何网络请求之前发生。
    expect(goalRequests).toBe(0);
    mounted.unmount();
  });
});

