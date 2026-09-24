import { afterEach, describe, expect, spyOn, test } from "bun:test";
import type { Session } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import {
  apiResponse,
  installGatewayFetch,
  mountSessionRunActions,
  restoreSessionHookGlobals,
} from "./sessionHookTestFixtures";
import { errorMessage } from "../../utils/errorMessage";

const CACHE_KEY = "gw_send_regression::ses_send_regression";

afterEach(restoreSessionHookGlobals);

function session(): Session {
  return {
    session_id: "ses_send_regression",
    workspace_id: "workspace_send_regression",
    title: "发送回归",
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: null,
    created_at: "2026-07-20T00:00:00Z",
    updated_at: "2026-07-20T00:00:00Z",
  };
}

function state(currentSession: Session): AppState {
  return {
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
  } as AppState;
}

describe("发送消息状态更新", () => {
  test("同一毫秒并发两次发送时，一次失败的回滚不得抹掉仍在途的另一条", async () => {
    const currentSession = session();
    const nowSpy = spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    let sendCalls = 0;
    installGatewayFetch(({ path, method }) => {
      if (path === "/api/v1/sessions/ses_send_regression/messages" && method === "POST") {
        sendCalls += 1;
        if (sendCalls === 1) {
          return Response.json({ detail: "第一条发送被拒绝" }, { status: 502 });
        }
        return new Promise<Response>((resolve) => {
          // 第二条一直挂在途，用来观察第一条失败回滚时的队列状态。
          void resolve;
        });
      }
      if (path === "/api/v1/sessions/ses_send_regression/pending-requests") {
        // 快照重取也失败：触发本地回滚分支。
        return Response.json({ detail: "队列不可读" }, { status: 503 });
      }
      return undefined;
    }, { token: "probe-concurrent-token" });

    const mounted = mountSessionRunActions({
      currentSession,
      state: state(currentSession),
      cacheKey: CACHE_KEY,
    });
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
      if (path === "/api/v1/sessions/ses_send_regression/messages" && method === "POST") {
        // 网关拒绝：但后端其实已经收下并排进队列。
        return Response.json({ detail: "模型网关拒绝" }, { status: 502 });
      }
      if (path === "/api/v1/sessions/ses_send_regression/pending-requests") {
        pendingCalls += 1;
        return apiResponse({
          session_id: currentSession.session_id,
          snapshot_version: 9,
          active_job_id: "job_server_accepted",
          queue: [],
          requests: [],
        });
      }
      return undefined;
    }, { token: "test-pending-token" });

    const mounted = mountSessionRunActions({
      currentSession,
      state: state(currentSession),
      cacheKey: CACHE_KEY,
    });
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
      if (path === "/api/v1/sessions/ses_send_regression/messages") {
        return apiResponse({
          message_id: "msg_send_regression",
          job_id: "job_send_regression",
          status: "running",
          dispatch: {
            session_id: currentSession.session_id,
            job_id: "job_send_regression",
            job_status: "running",
            active_job_id: "job_send_regression",
            queued_jobs_ahead: 0,
            queued_job_count: 0,
            pending_job_count: 0,
          },
        });
      }
      return undefined;
    }, { token: "test-local-token" });

    const mounted = mountSessionRunActions({
      currentSession,
      state: state(currentSession),
      cacheKey: CACHE_KEY,
    });
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
      if (path === "/api/v1/sessions/ses_send_regression/interrupt") {
        return Response.json(
          { detail: "Session ses_send_regression 当前没有正在运行的任务" },
          { status: 404 },
        );
      }
      return undefined;
    }, { token: "test-interrupt-token" });

    const initialState = state(currentSession);
    initialState.activeJobIdsBySession.set(CACHE_KEY, "job_stale_running");
    const mounted = mountSessionRunActions({
      currentSession,
      state: initialState,
      cacheKey: CACHE_KEY,
    });
    await mounted.actions.interruptSession();

    expect(mounted.state().activeJobIdsBySession.has(CACHE_KEY)).toBe(false);
    expect(mounted.state().sessionHistoryReloadNonce).toBe(1);
    expect(mounted.state().status).toBe("运行任务已结束，正在同步会话历史");
  });

  test("中断遇到非 404 失败会恢复后端运行态并写入可见失败", async () => {
    const currentSession = session();
    installGatewayFetch(({ path }) => {
      if (path === "/api/v1/sessions/ses_send_regression/interrupt") {
        return Response.json({ detail: "中断执行器崩溃" }, { status: 500 });
      }
      // 后端真值：任务仍在运行。前端必须校准回这个 job，而不是乐观清空。
      if (path === "/api/v1/sessions/ses_send_regression/pending-requests") {
        return apiResponse({
          session_id: currentSession.session_id,
          active_job_id: "job_backend_running",
          requests: [],
        });
      }
      return undefined;
    }, { token: "test-interrupt-token" });

    const initialState = state(currentSession);
    initialState.activeJobIdsBySession.set(CACHE_KEY, "job_stale_running");
    const mounted = mountSessionRunActions({
      currentSession,
      state: initialState,
      cacheKey: CACHE_KEY,
    });

    let thrown: unknown = null;
    try {
      await mounted.actions.interruptSession();
    } catch (error) {
      thrown = error;
    }

    // 失败仍向上抛出，调用方必须接住；但状态与运行态已经收敛。
    expect(errorMessage(thrown)).toContain(
      "中断执行器崩溃",
    );
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
      if (path === "/api/v1/sessions/ses_send_regression/messages/msg_original/replay") {
        return apiResponse({
          message_id: "msg_replay_new",
          job_id: "job_replay_new",
          session_id: currentSession.session_id,
          action: "regenerate",
          status: "running",
          replaced_message_id: "msg_original",
          removed_message_count: 1,
          notice: "已移除目标消息及其后的会话上下文；工作区文件修改不会被撤销。",
          dispatch: {
            session_id: currentSession.session_id,
            job_id: "job_replay_new",
            job_status: "running",
            active_job_id: "job_replay_new",
            queued_jobs_ahead: 0,
            queued_job_count: 0,
            pending_job_count: 0,
          },
        });
      }
      return undefined;
    }, { token: "test-replay-token" });

    const mounted = mountSessionRunActions({
      currentSession,
      state: state(currentSession),
      cacheKey: CACHE_KEY,
    });

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
      if (path === "/api/v1/sessions/ses_send_regression/messages") {
        return apiResponse({
          message_id: "msg_w10_projection",
          job_id: "job_w10_projection",
          status: "queued",
          dispatch: {
            session_id: currentSession.session_id,
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
          },
        });
      }
      return undefined;
    }, { token: "test-w10-token" });

    const mounted = mountSessionRunActions({
      currentSession,
      state: state(currentSession),
      cacheKey: CACHE_KEY,
    });
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
      if (path === "/api/v1/sessions/ses_send_regression/messages") {
        return apiResponse({
          message_id: "msg_w10_null",
          job_id: "job_w10_null",
          status: "running",
          dispatch: {
            session_id: currentSession.session_id,
            job_id: "job_w10_null",
            job_status: "running",
            active_job_id: "job_w10_null",
            queued_jobs_ahead: 0,
            queued_job_count: 0,
            pending_job_count: 1,
          },
        });
      }
      return undefined;
    }, { token: "test-w10-token" });

    const mounted = mountSessionRunActions({
      currentSession,
      state: state(currentSession),
      cacheKey: CACHE_KEY,
    });
    // 请求参数带 after_turn，但后端拒绝提供 delivery_policy。
    await mounted.actions.sendMessage("无投递策略", [], "after_turn");

    const conversation = mounted.state().pendingConversations.get(CACHE_KEY)?.[0];
    expect(conversation?.deliveryPolicy).toBeUndefined();
    expect(conversation?.enqueueSequence).toBeUndefined();
    expect(conversation?.blockedByJobId).toBeUndefined();
    expect(conversation?.queuedJobCount).toBe(0);
    expect(conversation?.pendingJobCount).toBe(1);
  });
});
