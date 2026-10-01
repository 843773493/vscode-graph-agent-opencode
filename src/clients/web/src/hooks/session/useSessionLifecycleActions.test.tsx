import { afterEach, describe, expect, test } from "bun:test";
import type { Session } from "../../types/backend";
import { sessionScopeKey } from "../../state/session/sessionScope";
import {
  apiResponse,
  buildSessionHookState,
  deleteSessionResponse,
  installGatewayFetch,
  installSessionDeleteFetch,
  mountSessionLifecycleActions,
  restoreSessionHookGlobals,
  sessionsListResponse,
} from "./sessionHookTestFixtures";

const WORKSPACE_ID = "gw_read_state";
const SESSION_ID = "ses_read_state";
const CACHE_KEY = sessionScopeKey(WORKSPACE_ID, SESSION_ID);

afterEach(restoreSessionHookGlobals);

function session(
  sessionId: string = SESSION_ID,
  title: string = "未读状态测试",
): Session {
  return {
    session_id: sessionId,
    workspace_id: "ws_local",
    title,
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: null,
    created_at: "2026-07-24T00:00:00Z",
    updated_at: "2026-07-24T00:00:00Z",
  };
}

function state(value: Session) {
  return buildSessionHookState({
    workspaceId: WORKSPACE_ID,
    current: value,
    sessions: [value],
    sessionGatewayWorkspaceById: new Map([[CACHE_KEY, WORKSPACE_ID]]),
    unreadSessionKeys: new Set([CACHE_KEY]),
  });
}

describe("会话已读状态", () => {
  test("用户打开会话时清除未读蓝标", () => {
    const currentSession = session();
    const mounted = mountSessionLifecycleActions({
      apiPort: 8014,
      currentSession,
      workspaceId: WORKSPACE_ID,
      state: state(currentSession),
    });

    mounted.actions.selectSession(SESSION_ID);

    expect(mounted.state().unreadSessionKeys.has(CACHE_KEY)).toBe(false);
  });

  test("重复打开当前会话不会重启历史加载", () => {
    const currentSession = session();
    let abortCount = 0;
    const mounted = mountSessionLifecycleActions({
      apiPort: 8014,
      currentSession,
      workspaceId: WORKSPACE_ID,
      state: state(currentSession),
      abortCurrentStream: () => {
        abortCount += 1;
      },
    });

    mounted.actions.selectWorkspaceSession(WORKSPACE_ID, SESSION_ID, currentSession);

    expect(abortCount).toBe(0);
    expect(mounted.state().sessionHistoryReloadNonce).toBe(0);
    expect(mounted.state().unreadSessionKeys.has(CACHE_KEY)).toBe(false);
  });

  test("可以用目录节点返回的会话摘要立即打开尚未加载到列表的会话", () => {
    const currentSession = session();
    const targetSession = session("ses_catalog_only", "目录中的会话");
    const mounted = mountSessionLifecycleActions({
      apiPort: 8014,
      currentSession,
      workspaceId: WORKSPACE_ID,
      state: state(currentSession),
    });

    mounted.actions.selectWorkspaceSession(
      WORKSPACE_ID,
      targetSession.session_id,
      targetSession,
    );

    expect(mounted.state().currentSession).toEqual(targetSession);
    expect(
      mounted.state().sessionsByWorkspace.get(WORKSPACE_ID)?.[0],
    ).toEqual(targetSession);
    expect(mounted.state().sessionHistoryReloadNonce).toBe(0);
  });
});

// —— 竞态与失败补偿：生命周期写动作的一致性守卫 ——

const RACE_WORKSPACE = "gw_lifecycle_race";

function raceSession(sessionId: string, agentId = "default"): Session {
  return {
    session_id: sessionId,
    workspace_id: "ws_local",
    title: sessionId,
    title_source: "user",
    current_agent_id: agentId,
    parent_session_id: null,
    created_at: "2026-07-24T00:00:00Z",
    updated_at: "2026-07-24T00:00:00Z",
  };
}

function raceState(current: Session | null, sessions: Session[]) {
  return buildSessionHookState({
    workspaceId: RACE_WORKSPACE,
    current,
    sessions,
    gatewayWorkspaces: [
      { workspace_id: RACE_WORKSPACE, root_path: "/tmp/ws", name: "ws" },
    ],
  });
}

/** 挂载生命周期动作：以当前会话为焦点，raceState 作为初始镜像。 */
function mountRace(current: Session, sessions: Session[], abort?: () => void) {
  return mountSessionLifecycleActions({
    apiPort: 8014,
    currentSession: current,
    workspaceId: RACE_WORKSPACE,
    state: raceState(current, sessions),
    abortCurrentStream: abort,
  });
}

/** 挂载生命周期动作并指定初始镜像，供需要预置会话级缓存的用例复用。 */
function mountLifecycle(
  currentSession: Session | null,
  state: ReturnType<typeof raceState>,
) {
  return mountSessionLifecycleActions({
    apiPort: 8014,
    currentSession,
    workspaceId: RACE_WORKSPACE,
    state,
  });
}
describe("会话生命周期写动作的竞态与失败补偿", () => {
  test("W4 切换 Agent 回包不覆盖用户请求在途期间切换到的会话", async () => {
    const sesA = raceSession("ses_a");
    const sesB = raceSession("ses_b");
    let releasePatch: (() => void) | undefined;
    let patchStarted = false;

    installGatewayFetch(({ path, method }) => {
      if (path === "/api/v1/sessions/" + sesA.session_id && method === "PATCH") {
        patchStarted = true;
        return new Promise<Response>((resolve) => {
          releasePatch = () => resolve(
            apiResponse({ ...sesA, current_agent_id: "agent_new" }),
          );
        });
      }
      return undefined;
    });

    const { state: readState, actions } = mountRace(sesA, [sesA, sesB]);
    const pending = actions.switchAgent("agent_new");
    for (let i = 0; i < 100 && !patchStarted; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    expect(patchStarted).toBe(true);

    // 请求仍在途，用户切到会话 B。
    actions.selectSession(sesB.session_id);
    expect(readState().currentSession?.session_id).toBe("ses_b");

    releasePatch?.();
    await pending;

    // 迟到的 switchAgent 回包只能更新会话元数据，不得把用户拉回 A。
    expect(readState().currentSession?.session_id).toBe("ses_b");
    expect(
      readState().sessions.find((item) => item.session_id === sesA.session_id)
        ?.current_agent_id,
    ).toBe("agent_new");
  });

  test("W5 fork 失败时补偿重取失败不覆盖原始错误", async () => {
    const sesA = raceSession("ses_a");
    installGatewayFetch(({ path }) => {
      if (path.endsWith("/fork-context")) {
        return apiResponse({ message: "上下文快照损坏" }, 422);
      }
      if (path === "/api/v1/sessions") {
        return apiResponse({ message: "列表不可用" }, 503);
      }
      return undefined;
    });

    const { state: readState, actions } = mountRace(sesA, [sesA]);
    await expect(
      actions.forkSessionContext(RACE_WORKSPACE, sesA.session_id),
    ).rejects.toThrow("上下文快照损坏");
    // 二次失败必须保留在提示里，且不得成为调用方看到的主错误。
    expect(readState().status).toContain("上下文快照损坏");
    expect(readState().status).toContain("列表不可用");
  });

  test("W6 删除成功但列表刷新失败时不报删除失败且本地列表收敛", async () => {
    const sesA = raceSession("ses_a");
    const sesB = raceSession("ses_b");
    installSessionDeleteFetch({
      deleteResponse: deleteSessionResponse(sesB.session_id),
      listResponse: apiResponse({ message: "列表不可用" }, 500),
    });

    const { state: readState, actions } = mountRace(sesA, [sesA, sesB]);
    await actions.deleteSession(sesB.session_id);

    expect(readState().status).not.toContain("删除会话失败");
    expect(readState().status).toContain("已删除会话");
    // 成功路径不重拉会话列表（§10.3 前端半边）：即使列表端点不可用，删除也已
    // 被后端确认，收敛完全靠本地删除闭包，状态里不应再出现列表错误。
    expect(readState().status).not.toContain("列表不可用");
    expect(readState().sessions.map((item) => item.session_id)).toEqual(["ses_a"]);
  });

  test("§10.3 删除成功后零次全量列表请求，且级联删除的子会话一并从镜像消失", async () => {
    const parent = raceSession("ses_parent");
    const child = { ...raceSession("ses_child"), parent_session_id: "ses_parent" };
    const unrelated = raceSession("ses_other");
    let listCalls = 0;
    installSessionDeleteFetch({
      deleteResponse: deleteSessionResponse(parent.session_id),
      listResponse: () => {
        listCalls += 1;
        // 若成功路径误重拉，这里会返回一个不该出现的陈旧条目。
        return sessionsListResponse([unrelated, child]);
      },
    });

    const { state: readState, actions } = mountRace(parent, [parent, child, unrelated]);
    await actions.deleteSession(parent.session_id);

    // 成功路径必须零次全量列表请求。
    expect(listCalls).toBe(0);
    // 被删父会话与其逻辑后代一起消失；无关会话保留。
    expect(readState().sessions.map((item) => item.session_id)).toEqual(["ses_other"]);
    expect(readState().currentSession?.session_id).toBe("ses_other");
  });

  test("删除会话后不得把它的事件队列残留在本地镜像", async () => {
    const sesA = raceSession("ses_a");
    const sesB = raceSession("ses_b");
    installSessionDeleteFetch({
      deleteResponse: deleteSessionResponse(sesA.session_id),
      listResponse: sessionsListResponse([sesB]),
    });

    const initial = raceState(sesB, [sesA, sesB]);
    const deletedKey = sessionScopeKey(RACE_WORKSPACE, sesA.session_id);
    initial.eventQueuesBySession.set(deletedKey, []);
    initial.turnTimelinesBySession = new Map([
      [deletedKey, { session_id: sesA.session_id, turns: [], details: new Map() } as never],
    ]);
    const { state: readState, actions } = mountLifecycle(sesB, initial);
    await actions.deleteSession(sesA.session_id);

    // 被删会话的所有会话级缓存都必须随之一并消失，绝不能留成幽灵条目。
    expect(readState().eventQueuesBySession.has(deletedKey)).toBe(false);
    expect(readState().turnTimelinesBySession.has(deletedKey)).toBe(false);
  });

  test("删除当前会话的抢占分支同样清空它的事件队列与未读标记", async () => {
    const sesA = raceSession("ses_a");
    const sesB = raceSession("ses_b");
    installSessionDeleteFetch({
      deleteResponse: deleteSessionResponse(sesA.session_id),
      listResponse: sessionsListResponse([sesB]),
    });

    const initial = raceState(sesA, [sesA, sesB]);
    const deletedKey = sessionScopeKey(RACE_WORKSPACE, sesA.session_id);
    initial.eventQueuesBySession.set(deletedKey, []);
    initial.unreadSessionKeys = new Set([deletedKey]);
    const { state: readState, actions } = mountLifecycle(sesA, initial);
    await actions.deleteSession(sesA.session_id);

    // 抢占分支在删除请求发出前就切走了当前会话，它同样必须清空被删会话的缓存。
    expect(readState().currentSession?.session_id).toBe("ses_b");
    expect(readState().eventQueuesBySession.has(deletedKey)).toBe(false);
    expect(readState().unreadSessionKeys.has(deletedKey)).toBe(false);
  });

  test("W7 选择不存在的会话时显式失败且不产生切换副作用", () => {
    const sesA = raceSession("ses_a");
    let aborts = 0;
    installGatewayFetch(() => undefined);

    const { state: readState, actions } = mountRace(sesA, [sesA], () => {
      aborts += 1;
    });
    actions.selectSession("ses_missing");

    expect(readState().status).toBe("切换会话失败: 不存在会话 ses_missing");
    expect(readState().currentSession?.session_id).toBe("ses_a");
    expect(readState().sessionHistoryReloadNonce).toBe(0);
    expect(aborts).toBe(1);
  });
});

describe("会话生命周期失败后的后端重取校准", () => {
  test("W9-a Agent 切换失败后用后端真值校准 current_agent_id", async () => {
    const before = raceSession(RACE_WORKSPACE, "agent_old");
    let getCalls = 0;
    installGatewayFetch(({ path, method }) => {
      if (path === "/api/v1/sessions/" + before.session_id && method === "PATCH") {
        return apiResponse({ message: "上游模型不可用" }, 500);
      }
      if (path === "/api/v1/sessions/" + before.session_id && method === "GET") {
        getCalls += 1;
        return apiResponse({ ...before, current_agent_id: "agent_server_truth" });
      }
      return undefined;
    });

    const { state: readState, actions } = mountRace(before, [before]);
    await expect(actions.switchAgent("agent_new")).rejects.toThrow("上游模型不可用");

    expect(getCalls).toBe(1);
    expect(readState().currentSession?.current_agent_id).toBe("agent_server_truth");
    expect(
      readState().sessions.find((item) => item.session_id === before.session_id)
        ?.current_agent_id,
    ).toBe("agent_server_truth");
  });

  test("W9-b 会话命名失败后用后端真值校准标题", async () => {
    const before = raceSession(RACE_WORKSPACE, "default");
    before.title = "旧标题";
    let getCalls = 0;
    installGatewayFetch(({ path, method }) => {
      if (path === "/api/v1/sessions/" + before.session_id && method === "PATCH") {
        return apiResponse({ message: "标题已存在" }, 409);
      }
      if (path === "/api/v1/sessions/" + before.session_id && method === "GET") {
        getCalls += 1;
        return apiResponse({ ...before, title: "服务端标题" });
      }
      return undefined;
    });

    const { state: readState, actions } = mountRace(before, [before]);
    await expect(actions.renameSession(before.session_id, "新标题"))
      .rejects.toThrow("标题已存在");

    expect(getCalls).toBe(1);
    expect(readState().currentSession?.title).toBe("服务端标题");
    expect(readState().status).toContain("会话命名失败");
  });

  test("W9-b2 会话命名空标题也走统一失败上报", async () => {
    const before = raceSession(RACE_WORKSPACE, "default");
    installGatewayFetch(() => undefined);

    const { state: readState, actions } = mountRace(before, [before]);
    await expect(actions.renameSession(before.session_id, "   "))
      .rejects.toThrow("会话名称不能为空");

    expect(readState().status).toContain("会话命名失败");
  });

  test("W9-c 删除失败后用后端列表校准本地镜像", async () => {
    const sesA = raceSession("ses_a");
    const sesB = raceSession("ses_b");
    let listCalls = 0;
    installSessionDeleteFetch({
      deleteResponse: apiResponse({ message: "会话正在运行" }, 409),
      listResponse: () => {
        listCalls += 1;
        return sessionsListResponse([sesA, raceSession("ses_server")]);
      },
    });

    const { state: readState, actions } = mountRace(sesA, [sesA, sesB]);
    await expect(actions.deleteSession(sesB.session_id)).rejects.toThrow("会话正在运行");

    expect(listCalls).toBe(1);
    // 失败后本地镜像以后端为准：既有会话保留，后端新出现的会话也要补上。
    expect(readState().sessions.map((item) => item.session_id))
      .toEqual(["ses_a", "ses_server"]);
    expect(readState().status).toContain("删除会话失败");
  });

  test("W9-c2 删除失败且重取也失败时保留原始错误", async () => {
    const sesA = raceSession("ses_a");
    const sesB = raceSession("ses_b");
    installSessionDeleteFetch({
      deleteResponse: apiResponse({ message: "会话正在运行" }, 409),
      listResponse: apiResponse({ message: "列表服务不可用" }, 503),
    });

    const { state: readState, actions } = mountRace(sesA, [sesA, sesB]);
    await expect(actions.deleteSession(sesB.session_id)).rejects.toThrow("会话正在运行");

    expect(readState().status).toContain("会话正在运行");
    expect(readState().status).toContain("列表服务不可用");
    // 重取失败时不得凭空删掉本地条目。
    expect(readState().sessions.map((item) => item.session_id))
      .toEqual(["ses_a", "ses_b"]);
  });

  test("W12-a 创建会话空标题走统一失败上报", async () => {
    const before = raceSession(RACE_WORKSPACE, "default");
    installGatewayFetch(() => undefined);

    const { state: readState, actions } = mountRace(before, [before]);
    await expect(actions.createSession("   ")).rejects.toThrow("会话名称不能为空");

    expect(readState().status).toContain("创建会话失败");
  });

  test("W12-b 打开未知工作区的会话时显式失败而不是沿用旧工作区元数据", () => {
    const current = raceSession(RACE_WORKSPACE, "default");
    const target = raceSession("ses_other", "default");
    installGatewayFetch(() => undefined);

    const unknownState = raceState(current, [current]);
    unknownState.gatewayWorkspaces = [];
    unknownState.workspaceRoot = "/prev/root";
    unknownState.workspaceName = "prev-ws";
    unknownState.sessionsByWorkspace = new Map([["gw_unknown", [target]]]);
    const { state: readState, actions } = mountLifecycle(current, unknownState);

    actions.selectWorkspaceSession("gw_unknown", target.session_id);

    expect(readState().status).toBe("切换会话失败: 未知工作区 gw_unknown");
    expect(readState().currentSession?.session_id).toBe(current.session_id);
    // 旧工作区的根目录/名称不得被当成新工作区身份继续用下去。
    expect(readState().workspaceRoot).toBe("/prev/root");
  });
});

// —— 跨工作区边界：活动工作区之外的工作区列表收敛不得污染全局镜像 ——

const OTHER_WORKSPACE = "gw_other";

/** 全局 sessions 镜像归属活动工作区，sessionsByWorkspace 同时持有另一工作区。 */
function crossWorkspaceState(current: Session) {
  return buildSessionHookState({
    workspaceId: RACE_WORKSPACE,
    current,
    sessions: [current],
    gatewayWorkspaces: [
      { workspace_id: RACE_WORKSPACE, root_path: "/tmp/ws", name: "ws" },
      { workspace_id: OTHER_WORKSPACE, root_path: "/tmp/other", name: "other" },
    ],
    sessionsByWorkspace: new Map([
      [RACE_WORKSPACE, [current]],
      [OTHER_WORKSPACE, [raceSession("ses_other")]],
    ]),
  });
}

describe("会话列表收敛的活动工作区边界", () => {
  test("P2-3 删除非活动工作区会话时收敛该工作区镜像且不改动全局 sessions", async () => {
    const active = raceSession("ses_active");
    const otherInOther = raceSession("ses_other");
    const otherRemaining = raceSession("ses_other_keep");
    installSessionDeleteFetch({
      deleteResponse: deleteSessionResponse(otherInOther.session_id),
      listResponse: sessionsListResponse([otherRemaining]),
    });
    const initial = crossWorkspaceState(active);
    // 非活动工作区镜像里同时有「将被删的 ses_other」与「应保留的 ses_other_keep」。
    initial.sessionsByWorkspace.set(OTHER_WORKSPACE, [otherInOther, otherRemaining]);

    const { state: readState, actions } = mountLifecycle(active, initial);
    await actions.deleteSession(otherInOther.session_id, OTHER_WORKSPACE);

    // 非活动工作区的收敛只更新它自己的镜像：被删会话消失，其余（含后端未返回的
    // 本地同名会话）保持——成功路径不再按后端列表整表替换。
    expect(
      readState().sessionsByWorkspace.get(OTHER_WORKSPACE)?.map(
        (item) => item.session_id,
      ),
    ).toEqual([otherRemaining.session_id]);
    // 全局镜像仍严格等于活动工作区的会话列表，绝不被其它工作区灌入。
    expect(readState().sessions.map((item) => item.session_id)).toEqual([
      active.session_id,
    ]);
    expect(readState().activeGatewayWorkspaceId).toBe(RACE_WORKSPACE);
  });
});

describe("删除非当前会话的失败路径与后端真值校准", () => {
  test("删除请求报错但后端其实已删且补偿重取成功时，级联消失的当前会话必须同步切走", async () => {
    // 当前会话是被删父会话的后代：后端级联删除会把当前会话一并带走了。
    const current = raceSession("ses_child");
    current.parent_session_id = "ses_parent";
    const parent = raceSession("ses_parent");
    const survivor = raceSession("ses_survivor");
    let listCalls = 0;
    installGatewayFetch(({ path, method }) => {
      if (path === "/api/v1/sessions/" + parent.session_id && method === "DELETE") {
        // 响应丢失：前端看到删除失败，但后端其实已经提交。
        return apiResponse({ message: "响应丢失" }, 500);
      }
      if (path === "/api/v1/sessions") {
        listCalls += 1;
        // 补偿重取成功：后端权威列表里当前会话已随父会话级联消失。
        return sessionsListResponse([survivor]);
      }
      return undefined;
    });

    const { state: readState, actions } = mountRace(current, [current, parent, survivor]);
    await expect(actions.deleteSession(parent.session_id)).rejects.toThrow("响应丢失");

    expect(listCalls).toBe(1);
    // 关键：重新校准后 currentSession 绝不能悬空指向一个已不在 sessions 里的
    // 幽灵会话，必须与删除成功路径一样切到剩余会话。
    expect(readState().currentSession?.session_id).toBe(survivor.session_id);
    expect(readState().sessions.some(
      (item) => item.session_id === readState().currentSession?.session_id,
    )).toBe(true);
    expect(readState().sessions.map((item) => item.session_id)).toEqual([
      survivor.session_id,
    ]);
    expect(readState().status).toContain("删除会话失败");
  });
});
