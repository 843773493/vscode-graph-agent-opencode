import React from "react";
import { afterEach, describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import type { AppState } from "../../types/frontend";
import type { Session } from "../../types/backend";
import { sessionScopeKey } from "../../state/session/sessionScope";
import { useSessionLifecycleActions } from "./useSessionLifecycleActions";

const WORKSPACE_ID = "gw_read_state";
const SESSION_ID = "ses_read_state";
const CACHE_KEY = sessionScopeKey(WORKSPACE_ID, SESSION_ID);

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

function state(value: Session): AppState {
  return {
    gatewayWorkspaces: [],
    sessions: [value],
    sessionsByWorkspace: new Map([[WORKSPACE_ID, [value]]]),
    sessionGatewayWorkspaceById: new Map([[CACHE_KEY, WORKSPACE_ID]]),
    sessionAttachmentSummaries: new Map(),
    eventQueuesBySession: new Map(),
    pendingConversations: new Map(),
    activeJobIdsBySession: new Map(),
    unreadSessionKeys: new Set([CACHE_KEY]),
    activeGatewayWorkspaceId: WORKSPACE_ID,
    currentSession: value,
    currentSessionWorkspaceId: WORKSPACE_ID,
    contentView: "default",
    sessionHistoryReloadNonce: 0,
    status: "",
  } as unknown as AppState;
}

describe("会话已读状态", () => {
  test("用户打开会话时清除未读蓝标", () => {
    const currentSession = session();
    let currentState = state(currentSession);
    let selectSession: ((sessionId: string) => void) | undefined;

    function Harness() {
      selectSession = useSessionLifecycleActions({
        apiPort: 8014,
        currentSession,
        activeGatewayWorkspaceId: WORKSPACE_ID,
        currentSessionGatewayWorkspaceId: WORKSPACE_ID,
        currentSessionCacheKey: CACHE_KEY,
        defaultGatewayWorkspaceId: WORKSPACE_ID,
        setState: (update) => {
          currentState = typeof update === "function"
            ? update(currentState)
            : update;
        },
        abortCurrentStream: () => undefined,
        invalidateAgentState: () => undefined,
      }).selectSession;
      return null;
    }

    renderToStaticMarkup(<Harness />);
    selectSession?.(SESSION_ID);

    expect(currentState.unreadSessionKeys.has(CACHE_KEY)).toBe(false);
  });

  test("重复打开当前会话不会重启历史加载", () => {
    const currentSession = session();
    let currentState = state(currentSession);
    let abortCount = 0;
    let selectWorkspaceSession:
      ((workspaceId: string, sessionId: string, sessionOverride?: Session) => void)
      | undefined;

    function Harness() {
      selectWorkspaceSession = useSessionLifecycleActions({
        apiPort: 8014,
        currentSession,
        activeGatewayWorkspaceId: WORKSPACE_ID,
        currentSessionGatewayWorkspaceId: WORKSPACE_ID,
        currentSessionCacheKey: CACHE_KEY,
        defaultGatewayWorkspaceId: WORKSPACE_ID,
        setState: (update) => {
          currentState = typeof update === "function"
            ? update(currentState)
            : update;
        },
        abortCurrentStream: () => {
          abortCount += 1;
        },
        invalidateAgentState: () => undefined,
      }).selectWorkspaceSession;
      return null;
    }

    renderToStaticMarkup(<Harness />);
    selectWorkspaceSession?.(WORKSPACE_ID, SESSION_ID, currentSession);

    expect(abortCount).toBe(0);
    expect(currentState.sessionHistoryReloadNonce).toBe(0);
    expect(currentState.unreadSessionKeys.has(CACHE_KEY)).toBe(false);
  });

  test("可以用目录节点返回的会话摘要立即打开尚未加载到列表的会话", () => {
    const currentSession = session();
    const targetSession = session("ses_catalog_only", "目录中的会话");
    let currentState = state(currentSession);
    let selectWorkspaceSession:
      ((workspaceId: string, sessionId: string, sessionOverride?: Session) => void)
      | undefined;

    function Harness() {
      selectWorkspaceSession = useSessionLifecycleActions({
        apiPort: 8014,
        currentSession,
        activeGatewayWorkspaceId: WORKSPACE_ID,
        currentSessionGatewayWorkspaceId: WORKSPACE_ID,
        currentSessionCacheKey: CACHE_KEY,
        defaultGatewayWorkspaceId: WORKSPACE_ID,
        setState: (update) => {
          currentState = typeof update === "function"
            ? update(currentState)
            : update;
        },
        abortCurrentStream: () => undefined,
        invalidateAgentState: () => undefined,
      }).selectWorkspaceSession;
      return null;
    }

    renderToStaticMarkup(<Harness />);
    selectWorkspaceSession?.(WORKSPACE_ID, targetSession.session_id, targetSession);

    expect(currentState.currentSession).toEqual(targetSession);
    expect(
      currentState.sessionsByWorkspace.get(WORKSPACE_ID)?.[0],
    ).toEqual(targetSession);
    expect(currentState.sessionHistoryReloadNonce).toBe(0);
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

function raceState(current: Session | null, sessions: Session[]): AppState {
  return {
    gatewayWorkspaces: [
      { workspace_id: RACE_WORKSPACE, root_path: "/tmp/ws", name: "ws" },
    ],
    sessions,
    sessionsByWorkspace: new Map([[RACE_WORKSPACE, sessions]]),
    sessionGatewayWorkspaceById: new Map(),
    sessionAttachmentSummaries: new Map(),
    eventQueuesBySession: new Map(),
    pendingConversations: new Map(),
    activeJobIdsBySession: new Map(),
    unreadSessionKeys: new Set(),
    activeGatewayWorkspaceId: RACE_WORKSPACE,
    currentSession: current,
    currentSessionWorkspaceId: RACE_WORKSPACE,
    contentView: "default",
    sessionHistoryReloadNonce: 0,
    status: "",
  } as unknown as AppState;
}

function apiResponse(data: unknown, status = 200): Response {
  const message = (data as { message?: string }).message ?? "ok";
  return Response.json(
    { code: status === 200 ? 0 : status, message, request_id: "req_probe", data },
    { status },
  );
}

/** 挂载 hook 并把最新 state 镜像到调用方闭包；返回稳定的动作引用。 */
function mountLifecycle({
  currentSession,
  readState,
  writeState,
  getActions,
  abortCurrentStream = () => undefined,
}: {
  currentSession: Session | null;
  readState: () => AppState;
  writeState: (update: AppState | ((prev: AppState) => AppState)) => void;
  getActions: (actions: ReturnType<typeof useSessionLifecycleActions>) => void;
  abortCurrentStream?: () => void;
}) {
  function Harness() {
    getActions(useSessionLifecycleActions({
      apiPort: 8014,
      currentSession,
      activeGatewayWorkspaceId: RACE_WORKSPACE,
      currentSessionGatewayWorkspaceId: RACE_WORKSPACE,
      currentSessionCacheKey: sessionScopeKey(
        RACE_WORKSPACE,
        currentSession?.session_id ?? "",
      ),
      defaultGatewayWorkspaceId: RACE_WORKSPACE,
      setState: (update) => {
        const next = typeof update === "function"
          ? (update as (prev: AppState) => AppState)(readState())
          : update;
        writeState(next);
      },
      abortCurrentStream,
      invalidateAgentState: () => undefined,
    }));
    return null;
  }
  renderToStaticMarkup(<Harness />);
}

describe("会话生命周期写动作的竞态与失败补偿", () => {
  const originalFetch = globalThis.fetch;
  let currentState: AppState;
  let actions: ReturnType<typeof useSessionLifecycleActions>;

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  function installFetch(
    handler: (path: string, method: string) => Response | Promise<Response>,
  ) {
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0])).pathname;
        const method = String(
          (args[1] as RequestInit | undefined)?.method ?? "GET",
        );
        if (path === "/api/gateway/auth/local-credential") {
          return apiResponse({ token: "test-token" });
        }
        if (path === "/api/gateway/users/current") {
          return apiResponse({ kind: "guest", user_id: null });
        }
        return handler(path, method);
      },
      { preconnect: originalFetch.preconnect },
    ) as typeof fetch;
  }

  function mount(current: Session, sessions: Session[], abort?: () => void) {
    currentState = raceState(current, sessions);
    mountLifecycle({
      currentSession: current,
      readState: () => currentState,
      writeState: (update) => {
        currentState = typeof update === "function"
          ? update(currentState)
          : update;
      },
      getActions: (value) => {
        actions = value;
      },
      abortCurrentStream: abort,
    });
  }

  test("W4 切换 Agent 回包不覆盖用户请求在途期间切换到的会话", async () => {
    const sesA = raceSession("ses_a");
    const sesB = raceSession("ses_b");
    let releasePatch: (() => void) | undefined;
    let patchStarted = false;

    installFetch((path, method) => {
      if (path === `/api/v1/sessions/${sesA.session_id}` && method === "PATCH") {
        patchStarted = true;
        return new Promise<Response>((resolve) => {
          releasePatch = () => resolve(
            apiResponse({ ...sesA, current_agent_id: "agent_new" }),
          );
        });
      }
      throw new Error(`未预期请求: ${method} ${path}`);
    });

    mount(sesA, [sesA, sesB]);
    const pending = actions.switchAgent("agent_new");
    for (let i = 0; i < 100 && !patchStarted; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    expect(patchStarted).toBe(true);

    // 请求仍在途，用户切到会话 B。
    actions.selectSession(sesB.session_id);
    expect(currentState.currentSession?.session_id).toBe("ses_b");

    releasePatch?.();
    await pending;

    // 迟到的 switchAgent 回包只能更新会话元数据，不得把用户拉回 A。
    expect(currentState.currentSession?.session_id).toBe("ses_b");
    expect(
      currentState.sessions.find((item) => item.session_id === sesA.session_id)
        ?.current_agent_id,
    ).toBe("agent_new");
  });

  test("W5 fork 失败时补偿重取失败不覆盖原始错误", async () => {
    const sesA = raceSession("ses_a");
    installFetch((path) => {
      if (path.endsWith("/fork-context")) {
        return apiResponse({ message: "上下文快照损坏" }, 422);
      }
      if (path === "/api/v1/sessions") {
        return apiResponse({ message: "列表不可用" }, 503);
      }
      throw new Error(`未预期请求: ${path}`);
    });

    mount(sesA, [sesA]);
    await expect(
      actions.forkSessionContext(RACE_WORKSPACE, sesA.session_id),
    ).rejects.toThrow("上下文快照损坏");
    // 二次失败必须保留在提示里，且不得成为调用方看到的主错误。
    expect(currentState.status).toContain("上下文快照损坏");
    expect(currentState.status).toContain("列表不可用");
  });

  test("W6 删除成功但列表刷新失败时不报删除失败且本地列表收敛", async () => {
    const sesA = raceSession("ses_a");
    const sesB = raceSession("ses_b");
    installFetch((path, method) => {
      if (path.startsWith("/api/v1/sessions/") && method === "DELETE") {
        return apiResponse({ session_id: sesB.session_id });
      }
      if (path === "/api/v1/sessions") {
        return apiResponse({ message: "列表不可用" }, 500);
      }
      throw new Error(`未预期请求: ${method} ${path}`);
    });

    mount(sesA, [sesA, sesB]);
    await actions.deleteSession(sesB.session_id);

    expect(currentState.status).not.toContain("删除会话失败");
    expect(currentState.status).toContain("已删除会话");
    expect(currentState.status).toContain("列表不可用");
    expect(currentState.sessions.map((item) => item.session_id)).toEqual(["ses_a"]);
  });

  test("W7 选择不存在的会话时显式失败且不产生切换副作用", () => {
    const sesA = raceSession("ses_a");
    let aborts = 0;
    installFetch((path) => {
      throw new Error(`未预期请求: ${path}`);
    });

    mount(sesA, [sesA], () => {
      aborts += 1;
    });
    actions.selectSession("ses_missing");

    expect(currentState.status).toBe("切换会话失败: 不存在会话 ses_missing");
    expect(currentState.currentSession?.session_id).toBe("ses_a");
    expect(currentState.sessionHistoryReloadNonce).toBe(0);
    expect(aborts).toBe(1);
  });
});

describe("会话生命周期失败后的后端重取校准", () => {
  const originalFetch = globalThis.fetch;
  let currentState: AppState;
  let actions: ReturnType<typeof useSessionLifecycleActions>;

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  function installFetch(
    handler: (path: string, method: string) => Response | Promise<Response>,
  ) {
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0])).pathname;
        const method = String(
          (args[1] as RequestInit | undefined)?.method ?? "GET",
        );
        if (path === "/api/gateway/auth/local-credential") {
          return apiResponse({ token: "test-token" });
        }
        if (path === "/api/gateway/users/current") {
          return apiResponse({ kind: "guest", user_id: null });
        }
        return handler(path, method);
      },
      { preconnect: originalFetch.preconnect },
    ) as typeof fetch;
  }

  function mount(current: Session, sessions: Session[]) {
    currentState = raceState(current, sessions);
    mountLifecycle({
      currentSession: current,
      readState: () => currentState,
      writeState: (update) => {
        currentState = typeof update === "function"
          ? update(currentState)
          : update;
      },
      getActions: (value) => {
        actions = value;
      },
    });
  }

  test("W9-a Agent 切换失败后用后端真值校准 current_agent_id", async () => {
    const before = raceSession(RACE_WORKSPACE, "agent_old");
    let getCalls = 0;
    installFetch((path, method) => {
      if (path === `/api/v1/sessions/${before.session_id}` && method === "PATCH") {
        return apiResponse({ message: "上游模型不可用" }, 500);
      }
      if (path === `/api/v1/sessions/${before.session_id}` && method === "GET") {
        getCalls += 1;
        return apiResponse({ ...before, current_agent_id: "agent_server_truth" });
      }
      throw new Error(`未预期请求: ${method} ${path}`);
    });

    mount(before, [before]);
    await expect(actions.switchAgent("agent_new")).rejects.toThrow("上游模型不可用");

    expect(getCalls).toBe(1);
    expect(currentState.currentSession?.current_agent_id).toBe("agent_server_truth");
    expect(
      currentState.sessions.find((item) => item.session_id === before.session_id)
        ?.current_agent_id,
    ).toBe("agent_server_truth");
  });

  test("W9-b 会话命名失败后用后端真值校准标题", async () => {
    const before = raceSession(RACE_WORKSPACE, "default");
    before.title = "旧标题";
    let getCalls = 0;
    installFetch((path, method) => {
      if (path === `/api/v1/sessions/${before.session_id}` && method === "PATCH") {
        return apiResponse({ message: "标题已存在" }, 409);
      }
      if (path === `/api/v1/sessions/${before.session_id}` && method === "GET") {
        getCalls += 1;
        return apiResponse({ ...before, title: "服务端标题" });
      }
      throw new Error(`未预期请求: ${method} ${path}`);
    });

    mount(before, [before]);
    await expect(actions.renameSession(before.session_id, "新标题"))
      .rejects.toThrow("标题已存在");

    expect(getCalls).toBe(1);
    expect(currentState.currentSession?.title).toBe("服务端标题");
    expect(currentState.status).toContain("会话命名失败");
  });

  test("W9-b2 会话命名空标题也走统一失败上报", async () => {
    const before = raceSession(RACE_WORKSPACE, "default");
    installFetch((path) => {
      throw new Error(`未预期请求: ${path}`);
    });

    mount(before, [before]);
    await expect(actions.renameSession(before.session_id, "   "))
      .rejects.toThrow("会话名称不能为空");

    expect(currentState.status).toContain("会话命名失败");
  });

  test("W9-c 删除失败后用后端列表校准本地镜像", async () => {
    const sesA = raceSession("ses_a");
    const sesB = raceSession("ses_b");
    let listCalls = 0;
    installFetch((path, method) => {
      if (path.startsWith("/api/v1/sessions/") && method === "DELETE") {
        return apiResponse({ message: "会话正在运行" }, 409);
      }
      if (path === "/api/v1/sessions") {
        listCalls += 1;
        return apiResponse({
          items: [sesA, raceSession("ses_server")],
          has_more: false,
          next_cursor: null,
        });
      }
      throw new Error(`未预期请求: ${method} ${path}`);
    });

    mount(sesA, [sesA, sesB]);
    await expect(actions.deleteSession(sesB.session_id)).rejects.toThrow("会话正在运行");

    expect(listCalls).toBe(1);
    // 失败后本地镜像以后端为准：既有会话保留，后端新出现的会话也要补上。
    expect(currentState.sessions.map((item) => item.session_id))
      .toEqual(["ses_a", "ses_server"]);
    expect(currentState.status).toContain("删除会话失败");
  });

  test("W9-c2 删除失败且重取也失败时保留原始错误", async () => {
    const sesA = raceSession("ses_a");
    const sesB = raceSession("ses_b");
    installFetch((path, method) => {
      if (path.startsWith("/api/v1/sessions/") && method === "DELETE") {
        return apiResponse({ message: "会话正在运行" }, 409);
      }
      if (path === "/api/v1/sessions") {
        return apiResponse({ message: "列表服务不可用" }, 503);
      }
      throw new Error(`未预期请求: ${method} ${path}`);
    });

    mount(sesA, [sesA, sesB]);
    await expect(actions.deleteSession(sesB.session_id)).rejects.toThrow("会话正在运行");

    expect(currentState.status).toContain("会话正在运行");
    expect(currentState.status).toContain("列表服务不可用");
    // 重取失败时不得凭空删掉本地条目。
    expect(currentState.sessions.map((item) => item.session_id))
      .toEqual(["ses_a", "ses_b"]);
  });

  test("W12-a 创建会话空标题走统一失败上报", async () => {
    const before = raceSession(RACE_WORKSPACE, "default");
    installFetch((path) => {
      throw new Error(`未预期请求: ${path}`);
    });

    mount(before, [before]);
    await expect(actions.createSession("   ")).rejects.toThrow("会话名称不能为空");

    expect(currentState.status).toContain("创建会话失败");
  });

  test("W12-b 打开未知工作区的会话时显式失败而不是沿用旧工作区元数据", () => {
    const current = raceSession(RACE_WORKSPACE, "default");
    const target = raceSession("ses_other", "default");
    installFetch((path) => {
      throw new Error(`未预期请求: ${path}`);
    });

    currentState = raceState(current, [current]);
    currentState.gatewayWorkspaces = [];
    currentState.workspaceRoot = "/prev/root";
    currentState.workspaceName = "prev-ws";
    currentState.sessionsByWorkspace = new Map([["gw_unknown", [target]]]);
    mountLifecycle({
      currentSession: current,
      readState: () => currentState,
      writeState: (update) => {
        currentState = typeof update === "function"
          ? update(currentState)
          : update;
      },
      getActions: (value) => {
        actions = value;
      },
    });

    actions.selectWorkspaceSession("gw_unknown", target.session_id);

    expect(currentState.status).toBe("切换会话失败: 未知工作区 gw_unknown");
    expect(currentState.currentSession?.session_id).toBe(current.session_id);
    // 旧工作区的根目录/名称不得被当成新工作区身份继续用下去。
    expect(currentState.workspaceRoot).toBe("/prev/root");
  });
});
