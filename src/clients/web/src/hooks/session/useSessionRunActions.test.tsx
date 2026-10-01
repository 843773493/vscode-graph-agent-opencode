import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Session } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import {
  apiResponse,
  createStateMirror,
  errorResponse,
  hangUntilReleased,
  installGatewayFetch,
  mountSessionRunActions,
  restoreSessionHookGlobals,
} from "./sessionHookTestFixtures";
import { errorMessage } from "../../utils/errorMessage";
import { INITIAL_APP_STATE } from "../app/appStateSeed";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { useSessionRunActions } from "./useSessionRunActions";

const CACHE_KEY = "gw_send_regression::ses_send_regression";
const SESSION_ID = "ses_send_regression";

afterEach(restoreSessionHookGlobals);

/** 回归用的固定会话：网关工作区、当前 agent 与时间戳都取稳定值。 */
function session(): Session {
  return {
    session_id: SESSION_ID,
    workspace_id: "workspace_send_regression",
    title: "发送回归",
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: null,
    created_at: "2026-07-20T00:00:00Z",
    updated_at: "2026-07-20T00:00:00Z",
  };
}

/** 会话级镜像：只保留本文件断言涉及的字段，其余沿用生产初始状态。 */
function state(currentSession: Session | null): AppState {
  return {
    ...INITIAL_APP_STATE,
    eventQueuesBySession: new Map(),
    pendingConversations: new Map(),
    activeJobIdsBySession: new Map(),
    unreadSessionKeys: new Set(),
    sessionAttachmentSummaries: new Map(),
    sessionsByWorkspace: new Map(),
    sessionGatewayWorkspaceById: new Map(),
    currentSession,
    sessionHistoryReloadNonce: 0,
    status: "",
    contentView: "default",
  };
}

/** 种一个「后端仍在运行」的 job，用于验收失败路径不得乐观清空运行态。 */
function stateWithActiveJob(currentSession: Session, jobId: string): AppState {
  const initial = state(currentSession);
  initial.activeJobIdsBySession.set(CACHE_KEY, jobId);
  return initial;
}

/** 挂载 runActions；默认用固定会话与其空镜像，用例只覆盖自己关心的入参。 */
function runActions(
  currentSession: Session | null = session(),
  initialState: AppState = state(currentSession),
): ReturnType<typeof mountSessionRunActions> {
  return mountSessionRunActions({
    currentSession,
    state: initialState,
    cacheKey: CACHE_KEY,
  });
}

/** 后端 dispatch 事实块；queue 计数与阻塞来源只有显式覆盖时才出现。 */
function dispatchFacts(overrides: {
  session_id?: string;
  job_id: string;
  job_status?: string;
  active_job_id: string | null;
  delivery_policy?: string;
  blocked_by_job_id?: string;
  queued_jobs_ahead?: number;
  queued_job_count?: number;
  pending_job_count?: number;
  enqueue_sequence?: number;
  queue_snapshot_version?: number;
}): Record<string, unknown> {
  return {
    session_id: SESSION_ID,
    job_status: "running",
    queued_jobs_ahead: 0,
    queued_job_count: 0,
    pending_job_count: 0,
    ...overrides,
  };
}

/** 投递一份 pending-requests 快照（队列校准用例的权威真值）。 */
function pendingRequestsResponse(overrides: {
  active_job_id?: string | null;
  snapshot_version?: number;
  queue?: unknown[];
  requests?: unknown[];
}): Response {
  return apiResponse({
    session_id: SESSION_ID,
    snapshot_version: 1,
    active_job_id: null,
    queue: [],
    requests: [],
    ...overrides,
  });
}

/** 重新生成成功响应（replay 用例共用同一份替换消息事实）。 */
function replayResponse(): Response {
  return apiResponse({
    message_id: "msg_replay_new",
    job_id: "job_replay_new",
    session_id: SESSION_ID,
    action: "regenerate",
    status: "running",
    replaced_message_id: "msg_original",
    removed_message_count: 1,
    notice: "已移除目标消息及其后的会话上下文；工作区文件修改不会被撤销。",
    dispatch: dispatchFacts({ job_id: "job_replay_new", active_job_id: "job_replay_new" }),
  });
}

const PENDING_PATH = `/api/v1/sessions/${SESSION_ID}/pending-requests`;
const MESSAGES_PATH = `/api/v1/sessions/${SESSION_ID}/messages`;
const INTERRUPT_PATH = `/api/v1/sessions/${SESSION_ID}/interrupt`;
const REPLAY_PATH = `/api/v1/sessions/${SESSION_ID}/messages/msg_original/replay`;
const COMPACT_PATH = `/api/v1/sessions/${SESSION_ID}/compact`;

describe("发送消息状态更新", () => {
  test("同一毫秒并发两次发送时，一次失败的回滚不得抹掉仍在途的另一条", async () => {
    const currentSession = session();
    const nowSpy = spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    let sendCalls = 0;
    installGatewayFetch(({ path, method }) => {
      if (path === MESSAGES_PATH && method === "POST") {
        sendCalls += 1;
        if (sendCalls === 1) {
          return errorResponse(502, "第一条发送被拒绝");
        }
        return new Promise<Response>((resolve) => {
          // 第二条一直挂在途，用来观察第一条失败回滚时的队列状态。
          void resolve;
        });
      }
      if (path === PENDING_PATH) {
        // 快照重取也失败：触发本地回滚分支。
        return errorResponse(503, "队列不可读");
      }
      return undefined;
    }, { token: "probe-concurrent-token" });

    const mounted = runActions(currentSession);
    const first = mounted.actions.sendMessage("第一条");
    void mounted.actions.sendMessage("第二条").catch(() => undefined);
    await first.catch(() => undefined);

    // 第一条失败回滚后，第二条仍在途：它的乐观回合必须还在队列里。
    const conversations = mounted.state().pendingConversations.get(CACHE_KEY) ?? [];
    expect(conversations.length).toBe(1);
    nowSpy.mockRestore();
  });

  test("W9-d 发送失败后按后端队列快照校准而不是抹掉已接受的回合", async () => {
    const currentSession = session();
    let pendingCalls = 0;
    installGatewayFetch(({ path, method }) => {
      if (path === MESSAGES_PATH && method === "POST") {
        // 网关拒绝：但后端其实已经收下并排进队列。
        return errorResponse(502, "模型网关拒绝");
      }
      if (path === PENDING_PATH) {
        pendingCalls += 1;
        return pendingRequestsResponse({
          snapshot_version: 9,
          active_job_id: "job_server_accepted",
        });
      }
      return undefined;
    }, { token: "test-pending-token" });

    const mounted = runActions(currentSession);
    await expect(mounted.actions.sendMessage("你好")).rejects.toThrow("模型网关拒绝");

    expect(pendingCalls).toBe(1);
    // 乐观回合被后端权威快照替换：后端已接受的 job 必须留在本地队列里。
    expect(mounted.state().activeJobIdsBySession.get(CACHE_KEY))
      .toBe("job_server_accepted");
    expect(mounted.state().status).toContain("发送失败");
    expect(mounted.state().status).toContain("模型网关拒绝");
  });

  test("API 接受请求前的乐观更新不读取尚未返回的 accepted", async () => {
    const currentSession = session();
    installGatewayFetch(({ path }) => {
      if (path === MESSAGES_PATH) {
        return apiResponse({
          message_id: "msg_send_regression",
          job_id: "job_send_regression",
          status: "running",
          dispatch: dispatchFacts({
            job_id: "job_send_regression",
            active_job_id: "job_send_regression",
          }),
        });
      }
      return undefined;
    }, { token: "test-local-token" });

    const mounted = runActions(currentSession);
    await mounted.actions.sendMessage("请只回复：收到");

    expect(mounted.state().status).toBe("已发送，等待生成");
    expect(mounted.state().sessionHistoryReloadNonce).toBe(0);
    expect(mounted.state().activeJobIdsBySession.get(CACHE_KEY)).toBe(
      "job_send_regression",
    );
    expect(
      mounted.state().pendingConversations.get(CACHE_KEY)?.[0]?.conversationId,
    ).toBe("msg_send_regression");
  });

  test("后端已无运行任务时中断会清掉残留前端运行态并触发历史同步", async () => {
    const currentSession = session();
    installGatewayFetch(({ path }) => {
      if (path === INTERRUPT_PATH) {
        return errorResponse(
          404,
          `Session ${SESSION_ID} 当前没有正在运行的任务`,
        );
      }
      return undefined;
    }, { token: "test-interrupt-token" });

    const mounted = runActions(
      currentSession,
      stateWithActiveJob(currentSession, "job_stale_running"),
    );
    await mounted.actions.interruptSession();

    expect(mounted.state().activeJobIdsBySession.has(CACHE_KEY)).toBe(false);
    expect(mounted.state().sessionHistoryReloadNonce).toBe(1);
    expect(mounted.state().status).toBe("运行任务已结束，正在同步会话历史");
  });

  test("中断遇到非 404 失败会恢复后端运行态并写入可见失败", async () => {
    const currentSession = session();
    installGatewayFetch(({ path }) => {
      if (path === INTERRUPT_PATH) {
        return errorResponse(500, "中断执行器崩溃");
      }
      // 后端真值：任务仍在运行。前端必须校准回这个 job，而不是乐观清空。
      if (path === PENDING_PATH) {
        return pendingRequestsResponse({ active_job_id: "job_backend_running" });
      }
      return undefined;
    }, { token: "test-interrupt-token" });

    const mounted = runActions(
      currentSession,
      stateWithActiveJob(currentSession, "job_stale_running"),
    );

    let thrown: unknown = null;
    try {
      await mounted.actions.interruptSession();
    } catch (error) {
      thrown = error;
    }

    // 失败仍向上抛出，调用方必须接住；但状态与运行态已经收敛。
    expect(errorMessage(thrown)).toContain("中断执行器崩溃");
    expect(mounted.state().status).toContain("中断生成失败");
    expect(mounted.state().status).toContain("中断执行器崩溃");
    // 后端仍在运行：activeJobId 必须回到后端真值，而不是被乐观清空。
    expect(mounted.state().activeJobIdsBySession.get(CACHE_KEY)).toBe(
      "job_backend_running",
    );
    // 非 404 失败不得触发「任务已结束」的历史重载。
    expect(mounted.state().sessionHistoryReloadNonce).toBe(0);
  });

  test("重新生成成功后保留可见的乐观运行回合", async () => {
    const currentSession = session();
    installGatewayFetch(({ path }) => {
      if (path === REPLAY_PATH) return replayResponse();
      return undefined;
    }, { token: "test-replay-token" });

    const mounted = runActions(currentSession);

    await mounted.actions.replayTurn("msg_original", "regenerate", "原始回复");

    const replay = mounted.state().pendingConversations.get(CACHE_KEY)?.[0];
    expect(replay?.conversationId).toBe("msg_replay_new");
    expect(replay?.activeJobOverlay).toBe(true);
    expect(replay?.userMessage?.metadata?.replay_action).toBe("regenerate");
    expect(mounted.state().activeJobIdsBySession.get(CACHE_KEY))
      .toBe("job_replay_new");
    expect(mounted.state().sessionHistoryReloadNonce).toBe(1);
  });

  test("W10-发送成功分支整体投影后端 dispatch 的全部队列事实", async () => {
    const currentSession = session();
    installGatewayFetch(({ path }) => {
      if (path === MESSAGES_PATH) {
        return apiResponse({
          message_id: "msg_w10_projection",
          job_id: "job_w10_projection",
          status: "queued",
          dispatch: dispatchFacts({
            job_id: "job_w10_projection",
            job_status: "queued",
            active_job_id: "job_w10_active",
            blocked_by_job_id: "job_w10_active",
            queued_jobs_ahead: 2,
            queued_job_count: 3,
            pending_job_count: 4,
            delivery_policy: "after_interrupt",
            enqueue_sequence: 7,
            queue_snapshot_version: 11,
          }),
        });
      }
      return undefined;
    }, { token: "test-w10-token" });

    const mounted = runActions(currentSession);
    await mounted.actions.sendMessage("投递投影");

    const conversation = mounted.state().pendingConversations.get(CACHE_KEY)?.[0];
    expect(conversation?.deliveryPolicy).toBe("after_interrupt");
    expect(conversation?.enqueueSequence).toBe(7);
    expect(conversation?.pendingPosition).toBe(2);
    expect(conversation?.queueSnapshotVersion).toBe(11);
    // 此前被逐字段手挑丢掉的队列计数与阻塞来源必须进入前端状态。
    expect(conversation?.queuedJobCount).toBe(3);
    expect(conversation?.pendingJobCount).toBe(4);
    expect(conversation?.blockedByJobId).toBe("job_w10_active");
  });

  test("W10-后端未提供 delivery_policy 时不伪造本地默认值", async () => {
    const currentSession = session();
    installGatewayFetch(({ path }) => {
      if (path === MESSAGES_PATH) {
        return apiResponse({
          message_id: "msg_w10_null",
          job_id: "job_w10_null",
          status: "running",
          dispatch: dispatchFacts({
            job_id: "job_w10_null",
            active_job_id: "job_w10_null",
            pending_job_count: 1,
          }),
        });
      }
      return undefined;
    }, { token: "test-w10-token" });

    const mounted = runActions(currentSession);
    // 请求参数带 after_turn，但后端拒绝提供 delivery_policy。
    await mounted.actions.sendMessage("无投递策略", [], "after_turn");

    const conversation = mounted.state().pendingConversations.get(CACHE_KEY)?.[0];
    expect(conversation?.deliveryPolicy).toBeUndefined();
    expect(conversation?.enqueueSequence).toBeUndefined();
    expect(conversation?.blockedByJobId).toBeUndefined();
    expect(conversation?.queuedJobCount).toBe(0);
    expect(conversation?.pendingJobCount).toBe(1);
  });

  test("中断失败且运行状态重取也失败时，两条原因都要拼进可见失败文案", async () => {
    const currentSession = session();
    installGatewayFetch(({ path }) => {
      if (path === INTERRUPT_PATH) return errorResponse(500, "中断执行器崩溃");
      if (path === PENDING_PATH) {
        // 补偿重取也失败：用户必须同时看到「中断失败」和「重取失败」两条事实。
        return errorResponse(503, "队列服务不可用");
      }
      return undefined;
    }, { token: "test-interrupt-recovery-token" });

    const mounted = runActions(currentSession);

    await expect(mounted.actions.interruptSession())
      .rejects.toThrow("中断执行器崩溃");

    // 两条事实必须同时可见：中断失败本身 + 补偿重取也失败。
    expect(mounted.state().status).toBe(
      "中断生成失败: 请求失败 500 : 中断执行器崩溃"
      + "；重新读取运行状态也失败: 请求失败 503 : 队列服务不可用",
    );
  });

  test("发送失败且待处理队列重取也失败时，两条原因都要拼进可见失败文案", async () => {
    const currentSession = session();
    installGatewayFetch(({ path, method }) => {
      if (path === MESSAGES_PATH && method === "POST") {
        return errorResponse(502, "模型网关拒绝");
      }
      if (path === PENDING_PATH) {
        // 补偿快照也读不到：用户必须同时看到「发送失败」和「重取失败」两条事实。
        return errorResponse(503, "队列不可读");
      }
      return undefined;
    }, { token: "test-send-recovery-token" });

    const mounted = runActions(currentSession);

    await expect(mounted.actions.sendMessage("你好")).rejects.toThrow("模型网关拒绝");

    // 两条事实必须同时可见：发送失败本身 + 补偿读队列也失败。
    expect(mounted.state().status).toBe(
      "发送失败: 请求失败 502 : 模型网关拒绝"
      + "；重新读取待处理队列也失败: 请求失败 503 : 队列不可读",
    );
  });

  test("replay 失败时必须递增历史重载计数，界面不停在脏状态", async () => {
    const currentSession = session();
    installGatewayFetch(({ path }) => {
      if (path === REPLAY_PATH) return errorResponse(409, "上下文窗口已失效");
      return undefined;
    }, { token: "test-replay-failure-token" });

    const mounted = runActions(currentSession);

    await expect(
      mounted.actions.replayTurn("msg_original", "regenerate", "原始回复"),
    ).rejects.toThrow("上下文窗口已失效");

    // 轮次回放失败会改动后端历史投影，必须触发一次历史 bootstrap 重载。
    expect(mounted.state().sessionHistoryReloadNonce).toBe(1);
    expect(mounted.state().status).toBe("轮次操作失败: 请求失败 409 : 上下文窗口已失效");
  });

  test("P3-1 replay 失败不改动本地运行态，不覆盖后端真值也不做多余队列重取", async () => {
    const currentSession = session();
    let pendingRequestsFetches = 0;
    installGatewayFetch(({ path }) => {
      if (path === REPLAY_PATH) return errorResponse(409, "上下文窗口已失效");
      if (path === PENDING_PATH) {
        pendingRequestsFetches += 1;
        return pendingRequestsResponse({ active_job_id: null });
      }
      return undefined;
    }, { token: "test-replay-no-overreach-token" });

    // replay 没有写入任何乐观 pending 回合/运行态，本 hook 守卫的是「不得在
    // 探测到失败时凭一次多余重取覆盖后端真值」。种一个后端仍在运行的 job：
    // 失败处理若误用空快照收敛，会把运行态静默抹掉。
    const mounted = runActions(
      currentSession,
      stateWithActiveJob(currentSession, "job_still_running"),
    );

    await expect(
      mounted.actions.replayTurn("msg_original", "regenerate", "原始回复"),
    ).rejects.toThrow("上下文窗口已失效");

    // 后端真值（仍在运行的 job）绝不被 replay 失败路径覆盖或抹掉。
    expect(mounted.state().activeJobIdsBySession.get(CACHE_KEY)).toBe(
      "job_still_running",
    );
    // 失败侧显式可见：状态串带原始原因，且触发一次历史 bootstrap 承接权威收敛。
    expect(mounted.state().status).toBe("轮次操作失败: 请求失败 409 : 上下文窗口已失效");
    expect(mounted.state().sessionHistoryReloadNonce).toBe(1);
    // replay 失败不在此 hook 内做本地待处理队列重取（无乐观态可失配）；
    // 若将来引入，必须是「后端权威快照校准」而非此处的一次多余探测。
    expect(pendingRequestsFetches).toBe(0);
  });

  test("未选中会话时发送消息会先创建会话再发送", async () => {
    // 空工作区首次发消息：currentSession 为空，必须显式创建会话后继续发送。
    let createCalls = 0;
    // 用对象承载被闭包写入的观测值：裸变量会被 TS 的类型收窄判成 null，
    // 这里必须如实反映「闭包内异步赋值」这一契约。
    const observed: { sentSessionId: string | null } = { sentSessionId: null };
    installGatewayFetch(({ path, method }) => {
      if (path === "/api/v1/sessions" && method === "POST") {
        createCalls += 1;
        return apiResponse({
          session_id: "ses_created_on_send",
          workspace_id: "workspace_send_regression",
          title: "新会话",
          current_agent_id: "default",
          created_at: "2026-07-20T00:00:00Z",
          updated_at: "2026-07-20T00:00:00Z",
        });
      }
      if (path === "/api/v1/sessions/ses_created_on_send/messages" && method === "POST") {
        observed.sentSessionId = "ses_created_on_send";
        return apiResponse({
          message_id: "msg_created_on_send",
          job_id: "job_created_on_send",
          status: "running",
          dispatch: dispatchFacts({
            session_id: "ses_created_on_send",
            job_id: "job_created_on_send",
            active_job_id: "job_created_on_send",
          }),
        });
      }
      return undefined;
    }, { token: "test-create-on-send-token" });

    const mounted = runActions(null);

    await mounted.actions.sendMessage("空工作区首条消息");

    expect(createCalls).toBe(1);
    expect(observed.sentSessionId).toBe("ses_created_on_send");
    expect(mounted.state().currentSession?.session_id).toBe("ses_created_on_send");
  });

  test("未选中会话时中断必须显式失败，不能静默成功", async () => {
    installGatewayFetch(() => undefined, { token: "test-no-session-interrupt-token" });
    const mounted = runActions(null);

    await expect(mounted.actions.interruptSession())
      .rejects.toThrow("当前没有可中断的会话");
  });
});

describe("在途运行动作的迟到回写不得污染已切到的会话", () => {
  const OTHER_SESSION_ID = "ses_switched_away";

  /** 切走后的目标会话：只有它才应出现在 AppState.currentSession 上。 */
  function otherSession(): Session {
    return { ...session(), session_id: OTHER_SESSION_ID, title: "切走后的会话" };
  }

  /**
   * 静态渲染一次，再在动作归还后把 AppState.currentSession 替换成目标会话，
   * 用来模拟「请求在途期间用户已切走会话」这一并发窗口。
   */
  function mountWithSessionSwitch(options: {
    startedSession: Session;
    switchedSession: Session;
  }): {
    mirror: { current: () => AppState };
    actions: ReturnType<typeof useSessionRunActions>;
    switchAway: () => void;
  } {
    const mirror = createStateMirror({
      ...state(options.startedSession),
      sessions: [options.startedSession, options.switchedSession],
    });
    let actions: ReturnType<typeof useSessionRunActions> | null = null;
    function Harness(): React.ReactNode {
      actions = useSessionRunActions({
        apiPort: 8014,
        currentSession: options.startedSession,
        activeGatewayWorkspaceId: "gw_send_regression",
        currentSessionGatewayWorkspaceId: "gw_send_regression",
        currentSessionCacheKey: sessionScopeKey(
          "gw_send_regression",
          options.startedSession.session_id,
        ),
        defaultGatewayWorkspaceId: "gw_send_regression",
        contentView: "default",
        setState: mirror.setState,
        refreshAgentStateSnapshot: async () => undefined,
      });
      return null;
    }
    renderToStaticMarkup(React.createElement(Harness));
    if (!actions) throw new Error("Harness 未完成渲染");
    return {
      mirror,
      actions,
      switchAway: () => {
        mirror.setState((prev) => ({
          ...prev,
          currentSession: options.switchedSession,
        }));
      },
    };
  }

  test("发送失败回填不得把上一个会话的失败文案写进新会话状态栏", async () => {
    const started = session();
    const { promise: sendResponse, release: releaseSend } =
      hangUntilReleased<Response>();
    installGatewayFetch(({ path, method }) => {
      if (path === MESSAGES_PATH && method === "POST") return sendResponse;
      if (path === PENDING_PATH) {
        return pendingRequestsResponse({ active_job_id: null });
      }
      return undefined;
    }, { token: "test-run-actions-session-switch-send" });

    const { mirror, actions, switchAway } = mountWithSessionSwitch({
      startedSession: started,
      switchedSession: otherSession(),
    });

    const sending = actions.sendMessage("在途发送").catch(() => undefined);
    // 请求仍在途时用户切到另一个会话。
    switchAway();
    expect(mirror.current().currentSession?.session_id).toBe(OTHER_SESSION_ID);

    releaseSend(errorResponse(502, "模型网关拒绝"));
    await sending;

    // 旧会话的发送失败属于旧会话事实，不得写进新会话状态栏。
    expect(mirror.current().status).not.toContain("模型网关拒绝");
    expect(mirror.current().status).not.toContain("发送失败");
  });

  test("压缩在途切走后，迟到的结果仍须复位全局 compactLoading", async () => {
    const started = session();
    const { promise: compactResponse, release: releaseCompact } =
      hangUntilReleased<Response>();
    installGatewayFetch(({ path }) => {
      if (path === COMPACT_PATH) return compactResponse;
      return undefined;
    }, { token: "test-run-actions-session-switch-compact" });

    const { mirror, actions, switchAway } = mountWithSessionSwitch({
      startedSession: started,
      switchedSession: otherSession(),
    });

    const compacting = actions.compactSession().catch(() => undefined);
    switchAway();
    expect(mirror.current().currentSession?.session_id).toBe(OTHER_SESSION_ID);
    expect(mirror.current().compactLoading).toBe(true);

    releaseCompact(apiResponse({
      session_id: SESSION_ID,
      status: "compacted",
      message: "ok",
      summarized_message_count: 3,
      before_message_count: 10,
      effective_message_count_before: 10,
      effective_message_count_after: 4,
      retained_message_count: 7,
      history_file_path: null,
    }));
    await compacting;

    // compactLoading 是全局单值，压缩请求无论落在哪个会话都必须复位，
    // 否则新会话的 /compact 命令会被永久禁用。
    expect(mirror.current().compactLoading).toBe(false);
    // 但旧会话的压缩结果属于旧会话事实，不得写进新会话状态栏。
    expect(mirror.current().status).not.toContain("已压缩上下文");
  });

  test("中断在途切走后，迟到的成功结果不得改写新会话状态栏", async () => {
    const started = session();
    const { promise: interruptResponse, release: releaseInterrupt } =
      hangUntilReleased<Response>();
    installGatewayFetch(({ path }) => {
      if (path === INTERRUPT_PATH) return interruptResponse;
      return undefined;
    }, { token: "test-run-actions-session-switch-interrupt" });

    const { mirror, actions, switchAway } = mountWithSessionSwitch({
      startedSession: started,
      switchedSession: otherSession(),
    });

    const interrupting = actions.interruptSession().catch(() => undefined);
    switchAway();
    expect(mirror.current().currentSession?.session_id).toBe(OTHER_SESSION_ID);

    releaseInterrupt(apiResponse({ phase: "interrupted" }));
    await interrupting;

    // 旧会话的中断结果属于旧会话事实，不得写进新会话状态栏。
    expect(mirror.current().status).not.toContain("已中断");
  });

  test("轮次回放失败发生在切走之后时，不得把失败原因写进新会话状态栏", async () => {
    const started = session();
    const { promise: replayResponsePromise, release: releaseReplay } =
      hangUntilReleased<Response>();
    installGatewayFetch(({ path }) => {
      if (path === REPLAY_PATH) return replayResponsePromise;
      return undefined;
    }, { token: "test-run-actions-session-switch-replay" });

    const { mirror, actions, switchAway } = mountWithSessionSwitch({
      startedSession: started,
      switchedSession: otherSession(),
    });

    const replaying = actions
      .replayTurn("msg_original", "regenerate", "原始回复")
      .catch(() => undefined);
    switchAway();
    expect(mirror.current().currentSession?.session_id).toBe(OTHER_SESSION_ID);

    releaseReplay(errorResponse(409, "上下文窗口已失效"));
    await replaying;

    // 旧会话的回放失败属于旧会话事实，不得写进新会话状态栏。
    expect(mirror.current().status).not.toContain("上下文窗口已失效");
    expect(mirror.current().status).not.toContain("轮次操作失败");
  });
});
