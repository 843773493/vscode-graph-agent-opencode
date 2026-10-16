import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { HttpRequestError } from "../../api";
import type { Session, SessionResource } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { useSessionResourceLoader } from "./useSessionResourceLoader";
import {
  apiResponse,
  errorResponse,
  flushEffects,
  hangUntilReleased,
  installGatewayFetch,
  restoreSessionHookGlobals,
} from "./sessionHookTestFixtures";

const SESSION_ID = "ses_resource_owner";
const renderers: ReactTestRenderer[] = [];

afterEach(async () => {
  await act(async () => {
    for (const renderer of renderers.splice(0)) renderer.unmount();
  });
  restoreSessionHookGlobals();
});

function resource(name: string, revision: number): SessionResource {
  return {
    resource_id: "term_resource_owner",
    session_id: SESSION_ID,
    kind: "terminal",
    name,
    status: "running",
    created_at: "2026-10-05T00:00:00Z",
    updated_at: "2026-10-05T00:00:01Z",
    available_actions: ["cancel"],
    metadata: { owner_revision: revision },
  };
}

async function mountResourceLoader() {
  // 夹具只提供该 hook 消费的 Session 身份；协议 URI 字段由其独立合同测试覆盖。
  let currentSession = { session_id: SESSION_ID, workspace_id: "backend-workspace" } as Session;
  let workspaceId = "gateway-workspace-A";
  let currentState = {
    currentSession,
    currentSessionWorkspaceId: workspaceId,
    sessionResources: [resource("旧缓存", 1)],
    sessionResourcesLoadedAt: null,
    sessionResourcesLoading: false,
    sessionResourcesError: null,
    status: "资源视图",
  } as AppState;
  let loader!: ReturnType<typeof useSessionResourceLoader>;
  function Harness(): React.ReactNode {
    loader = useSessionResourceLoader({
      apiPort: 49_409,
      currentSession,
      workspaceId,
      setState: (update) => {
        currentState = typeof update === "function" ? update(currentState) : update;
      },
    });
    return null;
  }
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(<Harness />);
  });
  renderers.push(renderer);
  return {
    loader: () => loader,
    state: () => currentState,
    switchTo: async (change: "session" | "workspace" | "invalidate") => {
      if (change === "session") {
        currentSession = { ...currentSession, session_id: "ses_resource_other" };
      } else if (change === "workspace") {
        workspaceId = "gateway-workspace-B";
      } else {
        loader.invalidateSessionResources();
      }
      currentState = {
        ...currentState,
        currentSession,
        currentSessionWorkspaceId: workspaceId,
        sessionResources: [resource("当前 owner 的资源", 8)],
        sessionResourcesLoading: false,
        sessionResourcesError: null,
        status: "当前 owner 的资源视图",
      };
      await act(async () => {
        renderer.update(<Harness />);
      });
    },
  };
}

describe("Session 资源失败后的权威重读", () => {
  test("失败必须等待真实重读、完整替换 owner 对象并保留原错误", async () => {
    const read = hangUntilReleased<Response>();
    let reads = 0;
    installGatewayFetch(({ path }) => {
      if (path.endsWith("/control")) return errorResponse(409, "owner revision 版本冲突");
      if (path.endsWith("/resources")) {
        reads += 1;
        return read.promise;
      }
      return undefined;
    });
    const mounted = await mountResourceLoader();
    let settled = false;
    const control = mounted.loader().controlSessionResource("terminal", "term_resource_owner", "cancel")
      .catch((error: unknown) => { settled = true; return error; });
    await flushEffects();
    expect(reads).toBe(1);
    expect(settled).toBe(false);
    expect(mounted.state().status).toContain("owner revision 版本冲突");
    const authoritative = resource("后端实际存活资源", 4);
    read.release(apiResponse({ session_id: SESSION_ID, items: [authoritative] }));
    const error = await control;
    expect(error).toBeInstanceOf(HttpRequestError);
    expect((error as HttpRequestError).detail).toBe("owner revision 版本冲突");
    expect(mounted.state().sessionResources).toEqual([authoritative]);
    expect(mounted.state().sessionResourcesLoading).toBe(false);
    expect(mounted.state().status).toContain("失败");
    expect(mounted.state().status).toContain("owner revision 版本冲突");
  });

  test("重读失败仍抛原控制错误，并保留重读失败的详细诊断", async () => {
    installGatewayFetch(({ path }) => {
      if (path.endsWith("/control")) return errorResponse(409, "原控制冲突");
      if (path.endsWith("/resources")) return errorResponse(503, "owner 重读不可用");
      return undefined;
    });
    const mounted = await mountResourceLoader();
    const error = await mounted.loader().controlSessionResource("terminal", "term_resource_owner", "cancel")
      .catch((error: unknown) => error);
    expect((error as HttpRequestError).detail).toBe("原控制冲突");
    expect(mounted.state().status).toContain("原控制冲突");
    expect(mounted.state().sessionResourcesError).toContain("owner 重读不可用");
    expect(mounted.state().sessionResources[0]?.metadata).toEqual({ owner_revision: 1 });
  });

  test("失败后必须发起新读取，旧的在途刷新不能吞掉或覆盖 owner 重读", async () => {
    const oldRead = hangUntilReleased<Response>();
    let reads = 0;
    installGatewayFetch(({ path }) => {
      if (path.endsWith("/control")) return errorResponse(409, "失败后需要核实");
      if (path.endsWith("/resources")) {
        reads += 1;
        return reads === 1 ? oldRead.promise : apiResponse({
          session_id: SESSION_ID, items: [resource("最新 owner", 5)],
        });
      }
      return undefined;
    });
    const mounted = await mountResourceLoader();
    const oldRefresh = mounted.loader().refreshSessionResources(SESSION_ID);
    await flushEffects();
    const control = mounted.loader().controlSessionResource("terminal", "term_resource_owner", "cancel")
      .catch((error: unknown) => error);
    await flushEffects();
    expect(reads).toBe(2);
    await control;
    oldRead.release(apiResponse({ session_id: SESSION_ID, items: [resource("迟到旧读取", 2)] }));
    await oldRefresh;
    expect(mounted.state().sessionResources[0]?.name).toBe("最新 owner");
    expect(mounted.state().status).toContain("失败后需要核实");
  });

  for (const change of ["session", "workspace", "invalidate"] as const) {
    test(`控制尚在途时 ${change} 切换不能重新读旧 owner 或污染当前展示`, async () => {
      const action = hangUntilReleased<Response>();
      let reads = 0;
      installGatewayFetch(({ path }) => {
        if (path.endsWith("/control")) return action.promise;
        if (path.endsWith("/resources")) {
          reads += 1;
          return apiResponse({ session_id: SESSION_ID, items: [] });
        }
        return undefined;
      });
      const mounted = await mountResourceLoader();
      const control = mounted.loader().controlSessionResource("terminal", "term_resource_owner", "cancel")
        .catch((error: unknown) => error);
      await mounted.switchTo(change);
      action.release(errorResponse(409, "旧 owner 控制失败"));
      const error = await control;
      expect((error as HttpRequestError).detail).toBe("旧 owner 控制失败");
      expect(reads).toBe(0);
      expect(mounted.state().status).toBe("当前 owner 的资源视图");
      expect(mounted.state().sessionResources[0]?.name).toBe("当前 owner 的资源");
    });

    test(`失败重读在途时 ${change} 切换必须丢弃旧 owner 的权威结果与错误`, async () => {
      const read = hangUntilReleased<Response>();
      let reads = 0;
      installGatewayFetch(({ path }) => {
        if (path.endsWith("/control")) return errorResponse(409, "旧 owner 已失败");
        if (path.endsWith("/resources")) {
          reads += 1;
          return read.promise;
        }
        return undefined;
      });
      const mounted = await mountResourceLoader();
      const control = mounted.loader().controlSessionResource("terminal", "term_resource_owner", "cancel")
        .catch((error: unknown) => error);
      await flushEffects();
      expect(reads).toBe(1);
      await mounted.switchTo(change);
      read.release(apiResponse({ session_id: SESSION_ID, items: [resource("旧 owner 重读结果", 6)] }));
      await control;
      expect(mounted.state().status).toBe("当前 owner 的资源视图");
      expect(mounted.state().sessionResources[0]?.name).toBe("当前 owner 的资源");
      expect(mounted.state().sessionResourcesError).toBeNull();
    });
  }

  test("旧工作区保存的刷新与控制入口不能向新 owner 写入", async () => {
    let requests = 0;
    installGatewayFetch(() => { requests += 1; return apiResponse({ session_id: SESSION_ID, items: [] }); });
    const mounted = await mountResourceLoader();
    const staleLoader = mounted.loader();
    await mounted.switchTo("workspace");
    expect(await staleLoader.refreshSessionResources(SESSION_ID)).toBe("stale");
    await expect(staleLoader.controlSessionResource("terminal", "term_resource_owner", "cancel"))
      .rejects.toThrow("请求已失效");
    expect(requests).toBe(0);
    expect(mounted.state().status).toBe("当前 owner 的资源视图");
  });

  test("显式失效后迟到的成功回执不能覆盖当前资源", async () => {
    const action = hangUntilReleased<Response>();
    installGatewayFetch(({ path }) => path.endsWith("/control") ? action.promise : undefined);
    const mounted = await mountResourceLoader();
    const control = mounted.loader().controlSessionResource("terminal", "term_resource_owner", "cancel");
    await mounted.switchTo("invalidate");
    action.release(apiResponse({
      session_id: SESSION_ID, resource_id: "term_resource_owner", kind: "terminal",
      action: "cancel", status: "completed", resource: resource("失效前成功回执", 7),
    }));
    await control;
    expect(mounted.state().sessionResources[0]?.name).toBe("当前 owner 的资源");
    expect(mounted.state().status).toBe("当前 owner 的资源视图");
  });

  test("成功继续使用后端完整对象，不保留旧对象独有字段", async () => {
    const result = resource("成功后的完整资源", 7);
    installGatewayFetch(({ path }) => path.endsWith("/control") ? apiResponse({
      session_id: SESSION_ID, resource_id: result.resource_id, kind: "terminal",
      action: "cancel", status: "completed", resource: result,
    }) : undefined);
    const mounted = await mountResourceLoader();
    await mounted.loader().controlSessionResource("terminal", result.resource_id, "cancel");
    expect(mounted.state().sessionResources).toEqual([result]);
  });

  test("成功回执也不能覆盖另一工作区中同 ID 会话的资源", async () => {
    const action = hangUntilReleased<Response>();
    installGatewayFetch(({ path }) => path.endsWith("/control") ? action.promise : undefined);
    const mounted = await mountResourceLoader();
    const control = mounted.loader().controlSessionResource("terminal", "term_resource_owner", "cancel");
    await mounted.switchTo("workspace");
    action.release(apiResponse({
      session_id: SESSION_ID, resource_id: "term_resource_owner", kind: "terminal",
      action: "cancel", status: "completed", resource: resource("旧工作区成功回执", 7),
    }));
    await control;
    expect(mounted.state().sessionResources[0]?.name).toBe("当前 owner 的资源");
    expect(mounted.state().status).toBe("当前 owner 的资源视图");
  });

  test("强制刷新绕过旧在途读取并返回真实成功终态", async () => {
    const oldRead = hangUntilReleased<Response>();
    const forcedRead = hangUntilReleased<Response>();
    let reads = 0;
    installGatewayFetch(({ path }) => {
      if (!path.endsWith("/resources")) return undefined;
      reads += 1;
      return reads === 1 ? oldRead.promise : forcedRead.promise;
    });
    const mounted = await mountResourceLoader();
    const oldRefresh = mounted.loader().refreshSessionResources(SESSION_ID);
    await flushEffects();
    const forcedRefresh = mounted.loader().refreshSessionResources(SESSION_ID, { force: true });
    await flushEffects();
    expect(reads).toBe(2);

    const currentResource = resource("强制刷新后的 owner", 9);
    forcedRead.release(apiResponse({ session_id: SESSION_ID, items: [currentResource] }));
    expect(await forcedRefresh).toBe("loaded");
    oldRead.release(apiResponse({ session_id: SESSION_ID, items: [resource("迟到旧读取", 2)] }));
    expect(await oldRefresh).toBe("stale");
    expect(mounted.state().sessionResources).toEqual([currentResource]);
  });

  test("刷新失败写入资源错误并返回 failed，而不是伪报 loaded", async () => {
    installGatewayFetch(({ path }) => path.endsWith("/resources")
      ? errorResponse(503, "会话资源重读失败")
      : undefined);
    const mounted = await mountResourceLoader();
    expect(await mounted.loader().refreshSessionResources(SESSION_ID, { force: true })).toBe("failed");
    expect(mounted.state().sessionResourcesError).toContain("会话资源重读失败");
    expect(mounted.state().sessionResourcesLoading).toBe(false);
  });
});
