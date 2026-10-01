import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { AppState } from "../../types/frontend";
import type { Session } from "../../types/backend";
import { useSessionChangesLoader } from "./useSessionChangesLoader";
import { useSessionResourceLoader } from "./useSessionResourceLoader";
import {
  apiResponse,
  errorResponse,
  hangUntilReleased,
  installGatewayFetch,
  restoreSessionHookGlobals,
} from "./sessionHookTestFixtures";

afterEach(restoreSessionHookGlobals);

function session(): Session {
  return {
    session_id: "ses_changes_loader",
    workspace_id: "ws_changes_loader",
    title: "变更加载器测试",
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: null,
    created_at: "2026-09-02T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
  };
}

function state(currentSession: Session): AppState {
  return {
    currentSession,
    contentView: "changes",
    sessionChangesLoading: false,
    sessionChangesError: null,
    sessionChangesets: [],
    selectedChangesetId: null,
    activeChangeset: null,
  } as unknown as AppState;
}

/** 挂载变更加载器并把最新 state 镜像到闭包，收敛三处逐字相同的 Harness 样板。 */
async function mountChangesLoader(currentSession: Session, apiPort: number) {
  let currentState = state(currentSession);
  let loader: ReturnType<typeof useSessionChangesLoader> | null = null;
  function Harness(): React.ReactNode {
    loader = useSessionChangesLoader({
      apiPort,
      currentSession,
      workspaceId: currentSession.workspace_id,
      setState: (update) => {
        currentState = typeof update === "function" ? update(currentState) : update;
      },
    });
    return null;
  }
  let renderer: ReactTestRenderer;
  await act(async () => {
    renderer = create(<Harness />);
  });
  return {
    loader: () => loader!,
    state: () => currentState,
    unmount: () => act(() => renderer!.unmount()),
  };
}

describe("会话文件变更请求协调", () => {
  test("标记已审查失败时必须给出带原因的可见诊断", async () => {
    const currentSession = session();
    installGatewayFetch(({ path }) => {
      if (path.endsWith("/review")) {
        return errorResponse(500, "审查后端崩溃");
      }
      return undefined;
    });

    const mounted = await mountChangesLoader(currentSession, 49_403);
    await mounted.loader().reviewSessionChangeFile(
      { file_path: "src/a.ts", reviewed: false } as never,
      true,
    ).catch(() => undefined);

    // 失败后状态栏必须点明失败原因，绝不能停在「正在标记」的假进行态。
    expect(mounted.state().status).toContain("审查");
    expect(mounted.state().status).toContain("审查后端崩溃");
    mounted.unmount();
  });

  test("并发和已缓存的变更列表只读取一次，显式刷新才重新读取列表", async () => {
    const currentSession = session();
    let changesetListRequestCount = 0;
    let changesetDetailRequestCount = 0;
    installGatewayFetch(({ path }) => {
      if (/^\/api\/v1\/sessions\/[^/]+\/changesets$/.test(path)) {
        changesetListRequestCount += 1;
        return apiResponse({
          items: [{
            changeset_id: "cs_default",
            session_id: path.split("/")[4],
            title: "默认变更",
            is_default: true,
            summary: { files: 1, additions: 2, deletions: 0 },
          }],
        });
      }
      if (/^\/api\/v1\/sessions\/[^/]+\/changesets\/cs_default$/.test(path)) {
        changesetDetailRequestCount += 1;
        return apiResponse({
          changeset_id: "cs_default",
          session_id: path.split("/")[4],
          title: "默认变更",
          status: "ready",
          summary: { files: 1, additions: 2, deletions: 0 },
          files: [],
        });
      }
      return undefined;
    });

    const mounted = await mountChangesLoader(currentSession, 49_403);
    const first = mounted.loader().loadSessionChangesets(currentSession.session_id);
    const second = mounted.loader().loadSessionChangesets(currentSession.session_id);
    await Promise.all([first, second]);
    await mounted.loader().refreshSessionChanges(currentSession.session_id, "cs_default");
    await mounted.loader().refreshSessionChanges(
      currentSession.session_id,
      "cs_default",
      { refreshList: true },
    );
    await mounted.loader().loadSessionChangesets("ses_other_changes_loader");
    mounted.loader().invalidateSessionChanges();
    await mounted.loader().loadSessionChangesets(currentSession.session_id);

    expect(changesetListRequestCount).toBe(3);
    expect(changesetDetailRequestCount).toBe(2);
    expect(mounted.state().activeChangeset?.changeset_id).toBe("cs_default");
    mounted.unmount();
  });

  test("显式刷新列表时不能被在途的普通列表读取吞掉", async () => {
    const currentSession = session();
    let listRequestCount = 0;
    let resolveFirstList: (response: Response) => void = () => undefined;
    const firstListResponse = new Promise<Response>((resolve) => {
      resolveFirstList = resolve;
    });
    installGatewayFetch(({ path }) => {
      if (/^\/api\/v1\/sessions\/[^/]+\/changesets$/.test(path)) {
        listRequestCount += 1;
        if (listRequestCount === 1) {
          return firstListResponse;
        }
        return apiResponse({
          items: [{
            changeset_id: "cs_refreshed",
            session_id: currentSession.session_id,
            title: "刷新后的变更",
            is_default: true,
            summary: { files: 2, additions: 4, deletions: 1 },
          }],
        });
      }
      return undefined;
    });

    const mounted = await mountChangesLoader(currentSession, 49_404);
    const inFlight = mounted.loader().loadSessionChangesets(currentSession.session_id);
    await Promise.resolve();
    const refreshed = mounted.loader().loadSessionChangesets(currentSession.session_id, true);
    await Promise.resolve();
    resolveFirstList(apiResponse({
      items: [{
        changeset_id: "cs_stale",
        session_id: currentSession.session_id,
        title: "在途的旧列表",
        is_default: true,
        summary: { files: 1, additions: 1, deletions: 0 },
      }],
    }));
    const [staleList, refreshedList] = await Promise.all([inFlight, refreshed]);

    expect(listRequestCount).toBe(2);
    expect(staleList.items.map((item) => item.changeset_id)).toEqual(["cs_stale"]);
    expect(refreshedList.items.map((item) => item.changeset_id)).toEqual(["cs_refreshed"]);
    mounted.unmount();
  });

  test("切走会话后，上一个会话在途审查的失败不得写进新会话的状态栏", async () => {
    const sessionA = session();
    const sessionB: Session = {
      ...session(),
      session_id: "ses_other_changes_loader",
      title: "另一个会话",
    };
    const { promise: reviewResponse, release: releaseReview } =
      hangUntilReleased<Response>();
    installGatewayFetch(({ path }) => {
      if (path.endsWith("/review")) return reviewResponse;
      return undefined;
    });

    let currentState = state(sessionA);
    let activeSession = sessionA;
    let loader: ReturnType<typeof useSessionChangesLoader> | null = null;
    function Harness(): React.ReactNode {
      loader = useSessionChangesLoader({
        apiPort: 49_405,
        currentSession: activeSession,
        workspaceId: activeSession.workspace_id,
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

    // 在会话 A 上发起审查，请求一直挂在途。
    const review = loader!.reviewSessionChangeFile(
      { file_path: "src/a.ts", reviewed: false } as never,
      true,
    ).catch(() => undefined);
    // 请求仍在途时切到会话 B：AppState 的 currentSession 与 hook 入参一起切换。
    await act(async () => {
      currentState = { ...currentState, currentSession: sessionB };
      activeSession = sessionB;
      renderer.update(<Harness />);
    });
    expect(currentState.currentSession?.session_id).toBe("ses_other_changes_loader");

    releaseReview(errorResponse(500, "会话 A 的审查后端崩了"));
    await act(async () => {
      await review;
    });

    // 会话 A 的失败诊断属于旧会话事实：不得污染已切到的会话 B 状态栏。
    expect(currentState.status).not.toContain("会话 A 的审查后端崩了");
    expect(currentState.status).not.toContain("标记文件已审查失败");
    expect(currentState.status).toBe("正在标记文件已审查");
    await act(async () => {
      renderer.unmount();
    });
  });

});

/**
 * 控制后台连接（取消 / 关闭 / 删除终端与浏览器）与文件审查同族：请求失败必须给出
 * 带原因的可见诊断，且失败诊断与成功路径共用同一会话守卫，切走后不得污染新会话。
 */
describe("后台连接控制请求协调", () => {
  /** 挂载资源加载器并把最新 AppState 镜像到闭包；会话可随后切换。 */
  async function mountResourceLoader(apiPort: number) {
    const sessionA = session();
    let currentState = state(sessionA);
    let activeSession = sessionA;
    let loader: ReturnType<typeof useSessionResourceLoader> | null = null;
    function Harness(): React.ReactNode {
      loader = useSessionResourceLoader({
        apiPort,
        currentSession: activeSession,
        workspaceId: activeSession.workspace_id,
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
    return {
      loader: () => loader!,
      state: () => currentState,
      switchTo: async (nextSession: Session) => {
        currentState = { ...currentState, currentSession: nextSession };
        activeSession = nextSession;
        await act(async () => {
          renderer.update(<Harness />);
        });
      },
      unmount: () => act(() => renderer.unmount()),
    };
  }

  test("控制后台连接失败时必须给出带原因的可见诊断", async () => {
    installGatewayFetch(({ path }) => {
      if (path.endsWith("/control")) {
        return errorResponse(500, "后台连接服务崩溃");
      }
      return undefined;
    });

    const mounted = await mountResourceLoader(49_407);
    await mounted.loader()
      .controlSessionResource("terminal", "term_1", "cancel")
      .catch(() => undefined);

    // 失败后状态栏必须点明失败原因，绝不能停在「正在取消/关闭」的假进行态。
    expect(mounted.state().status).toContain("失败");
    expect(mounted.state().status).toContain("后台连接服务崩溃");
    mounted.unmount();
  });

  test("切走会话后，上一个会话在途控制失败的诊断不得写进新会话的状态栏", async () => {
    const sessionB: Session = {
      ...session(),
      session_id: "ses_other_resource_loader",
      title: "另一个会话",
    };
    const { promise: controlResponse, release: releaseControl } =
      hangUntilReleased<Response>();
    installGatewayFetch(({ path }) => {
      if (path.endsWith("/control")) return controlResponse;
      return undefined;
    });

    const mounted = await mountResourceLoader(49_408);
    // 在会话 A 上发起控制，请求一直挂在途。
    const control = mounted.loader()
      .controlSessionResource("terminal", "term_2", "cancel")
      .catch(() => undefined);
    await mounted.switchTo(sessionB);
    expect(mounted.state().currentSession?.session_id).toBe("ses_other_resource_loader");

    releaseControl(errorResponse(500, "会话 A 的后台连接崩了"));
    await act(async () => {
      await control;
    });

    // 会话 A 的失败诊断属于旧会话事实：不得污染已切到的会话 B 状态栏。
    expect(mounted.state().status).not.toContain("会话 A 的后台连接崩了");
    expect(mounted.state().status).not.toContain("失败");
    expect(mounted.state().status).toBe("正在终止");
    mounted.unmount();
  });
});
