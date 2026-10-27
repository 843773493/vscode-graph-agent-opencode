import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create } from "react-test-renderer";
import { useSessionResourceExplorer } from "./useSessionResourceExplorer";
import {
  isNavigationBackpressureStatus,
  NAVIGATION_BACKPRESSURE_STATUSES,
} from "../../api/session/sessionCatalogOperations";
import {
  apiSuccessResponse,
  installGatewayFetch,
  restoreGatewayTestEnvironment,
  withDefaultGatewayApiResponses,
} from "../../tests/fixtures/session/gatewayEnvironment";
import {
  installVisibleTestDocument,
  installGatewayTestWindow,
} from "../../tests/fixtures/session/gatewayBrowserGlobals";
import {
  createSessionResourceExplorerProps,
  type SessionResourceExplorerController,
} from "../../tests/fixtures/session/sessionResourceExplorer";
import { flushReactEffects } from "../../tests/fixtures/session/asyncControl";

afterEach(restoreGatewayTestEnvironment);

/**
 * 目录分支读取的重试与迟到请求守卫（F3/F5 回归）。
 *
 * 这两个用例是针对「按文案嗅探错误类型」和「迟到失败覆盖新终态」的真实回归防护，
 * 分别对应修复前的两个真实缺陷；用例自身不依赖新的夹具导出，只使用既有夹具。
 */

function mountExplorer(apiPort: number) {
  installGatewayTestWindow(apiPort);
  installVisibleTestDocument();
  let explorer: SessionResourceExplorerController | null = null;
  function Harness(): React.ReactNode {

    explorer = useSessionResourceExplorer({
      ...createSessionResourceExplorerProps({ apiPort, activeWorkspaceId: "ws-test" }),
    });
    return null;
  }
  return { Harness, explorer: () => explorer! };
}

describe("目录分支重试按错误类型判定", () => {
  test("背压状态集合是唯一权威定义，覆盖重试集合", () => {
    expect([...NAVIGATION_BACKPRESSURE_STATUSES].sort((left, right) => left - right))
      .toEqual([408, 425, 429, 502, 503, 504]);
    expect(isNavigationBackpressureStatus(503)).toBe(true);
    expect(isNavigationBackpressureStatus(500)).toBe(false);
  });

  test("503 背压重试到上限（3 次），不再因文案不匹配退化为 1 次", async () => {
    let calls = 0;
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        calls += 1;
        return apiSuccessResponse({ message: "上游不可用" }, 503);
      }
      return undefined;
    }));
    const { Harness, explorer } = mountExplorer(49_611);
    let renderer!: ReturnType<typeof create>;
    await act(async () => {
      renderer = create(React.createElement(Harness));
      await flushReactEffects();
      await new Promise<void>((resolve) => setTimeout(resolve, 2000));
      await flushReactEffects();
    });
    expect(calls).toBe(3);
    expect(explorer().branches.get("ws-test:root")?.error).toContain("503");
    act(() => renderer.unmount());
  });

  test("500 内部故障不是背压，只请求 1 次", async () => {
    let calls = 0;
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        calls += 1;
        return apiSuccessResponse({ message: "后端内部错误" }, 500);
      }
      return undefined;
    }));
    const { Harness } = mountExplorer(49_612);
    let renderer!: ReturnType<typeof create>;
    await act(async () => {
      renderer = create(React.createElement(Harness));
      await flushReactEffects();
      await new Promise<void>((resolve) => setTimeout(resolve, 1500));
      await flushReactEffects();
    });
    expect(calls).toBe(1);
    act(() => renderer.unmount());
  });

  test("4xx 明确拒绝只请求 1 次", async () => {
    let calls = 0;
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        calls += 1;
        return apiSuccessResponse({ message: "目标不存在" }, 404);
      }
      return undefined;
    }));
    const { Harness } = mountExplorer(49_613);
    let renderer!: ReturnType<typeof create>;
    await act(async () => {
      renderer = create(React.createElement(Harness));
      await flushReactEffects();
      await new Promise<void>((resolve) => setTimeout(resolve, 1200));
      await flushReactEffects();
    });
    expect(calls).toBe(1);
    act(() => renderer.unmount());
  });
});

describe("迟到定位请求的失败不得覆盖新分支终态", () => {
  test("被顶掉的定位分页失败后，已成功的新分支保持无错误", async () => {
    let pagingResolve: ((response: Response) => void) | null = null;
    let childrenCalls = 0;
    installGatewayFetch(withDefaultGatewayApiResponses(({ path }) => {
      if (path.includes("/api/v1/session-catalog/children")) {
        childrenCalls += 1;
        if (childrenCalls === 1) {
          return apiSuccessResponse({ revision: "mount", catalog_revision: 7, parent_node_id: null, items: [], cursor: null, total: 0 });
        }
        if (childrenCalls === 2) {
          return apiSuccessResponse({ revision: "refresh", catalog_revision: 7, parent_node_id: null, items: [], cursor: "c1", total: 0 });
        }
        if (childrenCalls === 3) {
          return new Promise<Response>((resolve) => { pagingResolve = resolve; });
        }
        return apiSuccessResponse({ revision: "ok", catalog_revision: 8, parent_node_id: null, items: [], cursor: null, total: 0 });
      }
      return undefined;
    }));
    const { Harness, explorer } = mountExplorer(49_621);
    let renderer!: ReturnType<typeof create>;
    await act(async () => {
      renderer = create(React.createElement(Harness));
      await flushReactEffects();
    });

    await act(async () => {
      void explorer().revealSearchResult("ws-test", ["folder-a"], "folder").catch(() => undefined);
      await flushReactEffects();
    });
    expect(pagingResolve).not.toBeNull();

    await act(async () => {
      await explorer().loadBranch("ws-test", null);
      await flushReactEffects();
    });
    expect(explorer().branches.get("ws-test:root")?.revision).toBe("ok");

    await act(async () => {
      pagingResolve!(apiSuccessResponse({ message: "分页崩了" }, 503));
      await flushReactEffects();
    });
    const after = explorer().branches.get("ws-test:root");
    expect(after?.error).toBeNull();
    expect(after?.revision).toBe("ok");
    act(() => renderer.unmount());
  });
});
