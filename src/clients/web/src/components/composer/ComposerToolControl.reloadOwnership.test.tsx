import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import {
  apiResponse,
  installGatewayFetch,
  installTestWindow,
  restoreSessionHookGlobals,
  type GatewayFetchRequest,
} from "../../hooks/session/sessionHookTestFixtures";
import ComposerToolControl from "./ComposerToolControl";

/**
 * 工具面板的 owner 归属契约。
 *
 * ComposerToolControl 的加载与轮询都带着发起时的 owner（agentId / workspaceId）。
 * 这两个 prop 会在浮层保持打开时被外部改写（Gateway 状态刷新、另一窗口激活
 * 工作区等）。旧实现没有归属守卫：先发起的旧请求返回更晚时会覆盖新 owner 的
 * 状态，界面于是稳定显示另一个 Agent 的工具开关、或把旧工作区的测试进度写进
 * 新工作区。
 *
 * 计时器由用例接管（记录回调、手工触发），不依赖真实轮询间隔，因此不受并发
 * 文件抢占 CPU 的影响。
 */

afterEach(restoreSessionHookGlobals);

const OVERLAY_CONSTRUCTOR_NAMES = ["Element", "Node", "HTMLElement"] as const;

let intervalCallbacks: Array<() => void> = [];
let nextIntervalId = 1;
const clearedIntervalIds = new Set<number>();

function installOverlayGlobals(): void {
  class OverlayElementStub {}
  class OverlayNodeStub {}
  for (const [name, value] of Object.entries({
    Element: OverlayElementStub,
    Node: OverlayNodeStub,
    HTMLElement: OverlayElementStub,
  })) {
    Object.defineProperty(globalThis, name, { configurable: true, value });
  }
  installTestWindow(49_501);
  intervalCallbacks = [];
  nextIntervalId = 1;
  clearedIntervalIds.clear();
  // @floating-ui 会对引用做 `instanceof window.Element` 判定；窗口桩必须同时带上
  // 这些构造器，否则 useFloating 的挂载副作用会在 render 阶段直接抛错。document
  // 仍不存在，AnchoredOverlay 因而走「无 document 直接内联子节点」分支。
  Object.assign(
    (globalThis as unknown as { window: Record<string, unknown> }).window,
    {
      Element: OverlayElementStub,
      Node: OverlayNodeStub,
      HTMLElement: OverlayElementStub,
      setInterval: (callback: () => void) => {
        const id = nextIntervalId;
        nextIntervalId += 1;
        intervalCallbacks.push(callback);
        return id;
      },
      clearInterval: (id: number) => {
        clearedIntervalIds.add(id);
      },
    },
  );
}

/** 手工触发所有仍然存活的轮询回调，不等待真实 1s 间隔。 */
function tickIntervals(): void {
  for (const callback of intervalCallbacks) callback();
}

function uninstallOverlayGlobals(): void {
  for (const name of OVERLAY_CONSTRUCTOR_NAMES) {
    Reflect.deleteProperty(globalThis, name);
  }
}

interface Deferred {
  resolve: (value: Response) => void;
}

/** 工具目录响应按 agent 分别受控：返回 pending 句柄供用例决定完成顺序。 */
function installToolCatalogFetch(catalogs: Record<string, Deferred>): void {
  installGatewayFetch((request: GatewayFetchRequest) => {
    if (request.path === "/api/v1/tools") {
      const agentId = new URL(request.url, "http://localhost").searchParams.get("agent_id");
      const deferred = agentId ? catalogs[agentId] : undefined;
      if (!deferred) return undefined;
      return new Promise<Response>((resolve) => {
        deferred.resolve = (value) => resolve(value);
      });
    }
    if (request.path === "/api/v1/tools/tests") {
      return apiResponse({ items: [], next_cursor: null, has_more: false });
    }
    return undefined;
  }, { token: "tool-reload-token" });
}

function toolCatalogPayload(toolId: string) {
  return [{
    tool_id: toolId,
    name: toolId,
    origin: "builtin",
    description: `${toolId} description`,
    parameters: {},
    category: "general",
    group_id: toolId,
    group_name: toolId,
    kind: "default",
    execution_enabled: true,
    model_visible: true,
    test_supported: false,
  }];
}

function findButton(renderer: ReactTestRenderer, className: string) {
  return renderer.root.find(
    (node) => node.type === "button"
      && String(node.props.className ?? "").includes(className),
  );
}

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

async function renderAndOpen(props: {
  agentId: string;
  workspaceId: string;
}): Promise<ReactTestRenderer> {
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(
      <ComposerToolControl
        apiPort={49_501}
        agentId={props.agentId}
        workspaceId={props.workspaceId}
        onStatus={() => undefined}
      />,
    );
  });
  await act(async () => {
    findButton(renderer, "composer-tool-button").props.onClick();
  });
  await flush();
  return renderer;
}

describe("工具目录加载的 owner 归属", () => {
  test("切换 Agent 后旧请求晚到不得覆盖新 Agent 的工具目录", async () => {
    installOverlayGlobals();
    const catalogs: Record<string, Deferred> = {
      agent_a: {} as Deferred,
      agent_b: {} as Deferred,
    };
    installToolCatalogFetch(catalogs);

    const renderer = await renderAndOpen({ agentId: "agent_a", workspaceId: "ws_1" });
    // agent_a 的目录请求此刻仍在途；外部把 Agent 切到 agent_b 并触发重新加载。
    await act(async () => {
      renderer.update(
        <ComposerToolControl
          apiPort={49_501}
          agentId="agent_b"
          workspaceId="ws_1"
          onStatus={() => undefined}
        />,
      );
    });
    await flush();

    // 新 Agent 的目录先返回。
    await act(async () => {
      catalogs.agent_b.resolve(apiResponse(toolCatalogPayload("beta_tool")));
    });
    await flush();
    expect(JSON.stringify(renderer.toJSON())).toContain("beta_tool");

    // 旧 Agent 的目录后返回：必须被归属守卫丢弃。
    await act(async () => {
      catalogs.agent_a.resolve(apiResponse(toolCatalogPayload("alpha_tool")));
    });
    await flush();
    expect(JSON.stringify(renderer.toJSON())).not.toContain("alpha_tool");
    expect(JSON.stringify(renderer.toJSON())).toContain("beta_tool");
    act(() => renderer.unmount());
    uninstallOverlayGlobals();
  });
});

/**
 * 测试进度轮询与目录加载同属一条 owner 链路：轮询结果带着发起时的
 * apiPort/workspaceId。没有归属守卫时，切换工作区后旧轮询请求返回会把上一个
 * 工作区的测试结果写进当前工作区，并且 testingTools 不会清空，还会继续用新
 * 工作区去查旧 run_id。
 */
describe("工具测试进度轮询的 owner 归属", () => {
  test("切换工作区后旧轮询结果不得写回，也不得继续轮询旧运行", async () => {
    installOverlayGlobals();
    const catalogs: Record<string, Deferred> = { agent_a: {} as Deferred };
    const runPoll: Deferred = { resolve: () => undefined };
    let runPollRequests = 0;

    installGatewayFetch((request: GatewayFetchRequest) => {
      if (request.path === "/api/v1/tools") {
        return new Promise<Response>((resolve) => {
          catalogs.agent_a.resolve = resolve;
        });
      }
      if (request.path === "/api/v1/tools/tests") {
        return apiResponse({ items: [], next_cursor: null, has_more: false });
      }
      if (request.path === "/api/v1/tools/alpha_tool/tests") {
        return apiResponse({
          run_id: "tooltest_1",
          tool_name: "alpha_tool",
          status: "running",
          progress: 10,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
          repetitions: 1,
          providers: [],
          attempts: [],
        });
      }
      if (request.path === "/api/v1/tools/tests/tooltest_1") {
        runPollRequests += 1;
        return new Promise<Response>((resolve) => {
          runPoll.resolve = resolve;
        });
      }
      return undefined;
    }, { token: "tool-poll-token" });

    const renderer = await renderAndOpen({ agentId: "agent_a", workspaceId: "ws_1" });
    await act(async () => {
      catalogs.agent_a.resolve(apiResponse(toolCatalogPayload("alpha_tool")));
    });
    await flush();

    await act(async () => {
      findButton(renderer, "composer-tool-test-button").props.onClick();
    });
    await flush();

    // 触发一轮轮询：请求发出后保持挂起，属于 ws_1。
    await act(async () => {
      tickIntervals();
    });
    await flush();
    expect(runPollRequests).toBeGreaterThan(0);

    // 外部把工作区切到 ws_2；旧轮询请求此刻仍在途。
    await act(async () => {
      renderer.update(
        <ComposerToolControl
          apiPort={49_501}
          agentId="agent_a"
          workspaceId="ws_2"
          onStatus={() => undefined}
        />,
      );
    });
    await flush();

    await act(async () => {
      runPoll.resolve(apiResponse({
        run_id: "tooltest_1",
        tool_name: "alpha_tool",
        status: "completed",
        progress: 100,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
        repetitions: 1,
        providers: [{
          provider_id: "p",
          model: "m",
          status: "completed",
          total: 7,
          completed: 7,
          passed: 7,
          failed: 0,
          model_calls: 0,
          reasoning_only_calls: 0,
          transient_retries: 0,
          success_rate: 100,
        }],
        attempts: [],
      }));
    });
    await flush();

    const html = JSON.stringify(renderer.toJSON());
    expect(html).not.toContain("成功率 100% · 7/7 通过");
    expect(html).not.toContain("测试进度读取失败");
    act(() => renderer.unmount());
    uninstallOverlayGlobals();
  });
});
