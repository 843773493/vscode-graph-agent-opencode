import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { AppState } from "../../types/frontend";
import type { Session } from "../../types/backend";
import { useSessionChangesLoader } from "./useSessionChangesLoader";
import {
  apiResponse,
  errorResponse,
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
});
