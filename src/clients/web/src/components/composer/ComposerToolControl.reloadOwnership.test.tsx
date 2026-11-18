import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { invalidateGatewayToken } from "../../api/http";
import {
  apiSuccessResponse,
  installGatewayFetch,
  restoreGatewayTestEnvironment,
  type GatewayFetchRequest,
} from "../../tests/fixtures/session/gatewayEnvironment";
import { installComposerTestGlobals } from "./composerTestFixtures";
import ComposerToolControl from "./ComposerToolControl";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";

const OWNERS = [
  { gatewayId: "gateway_00000000000040008000000000000001", workspaceId: "00000000-0000-4000-8000-000000000001" },
  { gatewayId: "gateway_00000000000040008000000000000002", workspaceId: "00000000-0000-4000-8000-000000000001" },
];
const WORKSPACE_KEYS = OWNERS.map(gatewayWorkspaceKey);

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

let intervalCallbacks = new Map<number, () => void>();
let nextIntervalId = 1;
let restoreComposerEnvironment = () => {};

function installComposerTestEnvironment(): void {
  invalidateGatewayToken(49_501);
  intervalCallbacks.clear();
  nextIntervalId = 1;
  restoreComposerEnvironment = installComposerTestGlobals({
    port: 49_501,
    intervalController: {
      setInterval: (callback: () => void) => {
        const id = nextIntervalId;
        nextIntervalId += 1;
        intervalCallbacks.set(id, callback);
        return id;
      },
      clearInterval: (id: number) => {
        intervalCallbacks.delete(id);
      },
    },
  });
}

afterEach(() => {
  restoreComposerTestEnvironment();
  restoreGatewayTestEnvironment();
});

/** 手工触发所有仍然存活的轮询回调，不等待真实 1s 间隔。 */
function tickIntervals(): void {
  for (const [timerId, callback] of [...intervalCallbacks]) {
    if (intervalCallbacks.get(timerId) === callback) callback();
  }
}

function restoreComposerTestEnvironment(): void {
  restoreComposerEnvironment();
  restoreComposerEnvironment = () => {};
  intervalCallbacks.clear();
}

interface Deferred {
  resolve: (value: Response) => void;
}

/** 工具目录响应按 agent 分别受控：返回 pending 句柄供用例决定完成顺序。 */
function installToolCatalogFetch(catalogs: Record<string, Deferred>): void {
  installGatewayFetch((request: GatewayFetchRequest) => {
    const headers = new Headers(request.init?.headers);
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(OWNERS[0].gatewayId);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(OWNERS[0].workspaceId);
    if (request.path === "/api/v1/tools") {
      const agentId = new URL(request.url, "http://localhost").searchParams.get("agent_id");
      const deferred = agentId ? catalogs[agentId] : undefined;
      if (!deferred) return undefined;
      return new Promise<Response>((resolve) => {
        deferred.resolve = (value) => resolve(value);
      });
    }
    if (request.path === "/api/v1/tools/tests") {
      return apiSuccessResponse({ items: [], next_cursor: null, has_more: false });
    }
    if (request.path === "/api/v1/agents") {
      return apiSuccessResponse(["agent_a", "agent_b"].map(agent_id => ({
        agent_id, name: agent_id, model: "test-model", tools: [], capabilities: [],
        models: [{ model_ref: { provider_id: "test-provider", model_id: "default" }, model: "test-model", custom_llm_provider: "openai", available: true }],
      })));
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
    test_supported: true,
    execution_locked: false,
    model_visibility_locked: false,
  }];
}

function toolTestRunPayload(runId: string, status: "queued" | "running" | "completed", progress = 0) {
  const completed = status === "completed" ? 1 : 0;
  return {
    run_id: runId,
    tool_name: "shared_tool",
    status,
    progress,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    repetitions: 1,
    models: [{
      model_ref: { provider_id: "test-provider", model_id: "default" },
      model: "test-model",
      status,
      total: 1,
      completed,
      passed: completed,
      failed: 0,
      model_calls: 0,
      reasoning_only_calls: 0,
      transient_retries: 0,
      success_rate: completed * 100,
    }],
    attempts: [],
  };
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
}, options: { onStatus?: (text: string) => void; active?: boolean } = {}): Promise<ReactTestRenderer> {
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(
      <ComposerToolControl
        apiPort={49_501}
        agentId={props.agentId}
        workspaceId={props.workspaceId}
        onStatus={options.onStatus ?? (() => undefined)}
        models={[{ model_ref: { provider_id: "test-provider", model_id: "default" }, model: "test-model", custom_llm_provider: "openai", available: true }]}
        active={options.active ?? true}
      />,
    );
  });
  await act(async () => {
    findButton(renderer, "composer-tool-button").props.onClick();
  });
  await flush();
  return renderer;
}

async function confirmToolTest(renderer: ReactTestRenderer): Promise<void> {
  await act(async () => { findButton(renderer, "composer-tool-test-button").props.onClick(); });
  const testInputs = renderer.root.findAll((node) => node.type === "input");
  await act(async () => {
    testInputs[0]?.props.onChange({ target: { checked: true } });
    testInputs[1]?.props.onChange({ target: { value: "1" } });
    testInputs[2]?.props.onChange({ target: { checked: true } });
  });
  const confirm = renderer.root.find((node) => node.type === "button"
    && node.children.join("").includes("开始测试"));
  await act(async () => { confirm.props.onClick(); });
  await flush();
}

describe("工具目录加载的 owner 归属", () => {
  test("切换 Agent 后旧请求晚到不得覆盖新 Agent 的工具目录", async () => {
    installComposerTestEnvironment();
    const catalogs: Record<string, Deferred> = {
      agent_a: {} as Deferred,
      agent_b: {} as Deferred,
    };
    installToolCatalogFetch(catalogs);

    const renderer = await renderAndOpen({ agentId: "agent_a", workspaceId: WORKSPACE_KEYS[0] });
    // agent_a 的目录请求此刻仍在途；外部把 Agent 切到 agent_b 并触发重新加载。
    await act(async () => {
      renderer.update(
        <ComposerToolControl
          apiPort={49_501}
          agentId="agent_b"
          workspaceId={WORKSPACE_KEYS[0]}
          onStatus={() => undefined}
          models={[{ model_ref: { provider_id: "test-provider", model_id: "default" }, model: "test-model", custom_llm_provider: "openai", available: true }]}
        />,
      );
    });
    await flush();

    // 新 Agent 的目录先返回。
    await act(async () => {
      catalogs.agent_b.resolve(apiSuccessResponse(toolCatalogPayload("beta_tool")));
    });
    await flush();
    expect(JSON.stringify(renderer.toJSON())).toContain("beta_tool");

    // 旧 Agent 的目录后返回：必须被归属守卫丢弃。
    await act(async () => {
      catalogs.agent_a.resolve(apiSuccessResponse(toolCatalogPayload("alpha_tool")));
    });
    await flush();
    expect(JSON.stringify(renderer.toJSON())).not.toContain("alpha_tool");
    expect(JSON.stringify(renderer.toJSON())).toContain("beta_tool");
    act(() => renderer.unmount());
    restoreComposerTestEnvironment();
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
    installComposerTestEnvironment();
    const catalogs: Record<string, Deferred> = { agent_a: {} as Deferred };
    const runPoll: Deferred = { resolve: () => undefined };
    let runPollRequests = 0;

    installGatewayFetch((request: GatewayFetchRequest) => {
      const headers = new Headers(request.init?.headers);
      expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(OWNERS[0].workspaceId);
      expect(OWNERS.some(owner => owner.gatewayId === headers.get("X-BoxTeam-Target-Gateway-Id"))).toBe(true);
      if (request.path === "/api/v1/tools") {
        return new Promise<Response>((resolve) => {
          catalogs.agent_a.resolve = resolve;
        });
      }
      if (request.path === "/api/v1/tools/tests") {
        return apiSuccessResponse({ items: [], next_cursor: null, has_more: false });
      }
      if (request.path === "/api/v1/tools/alpha_tool/tests") {
        return apiSuccessResponse({
          run_id: "tooltest_1",
          tool_name: "alpha_tool",
          status: "running",
          progress: 10,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
          repetitions: 1,
          models: [],
          attempts: [],
        });
      }
      if (request.path === "/api/v1/tools/tests/tooltest_1") {
        expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(OWNERS[0].gatewayId);
        runPollRequests += 1;
        return new Promise<Response>((resolve) => {
          runPoll.resolve = resolve;
        });
      }
      return undefined;
    }, { token: "tool-poll-token" });

    const renderer = await renderAndOpen({ agentId: "agent_a", workspaceId: WORKSPACE_KEYS[0] });
    await act(async () => {
      catalogs.agent_a.resolve(apiSuccessResponse(toolCatalogPayload("alpha_tool")));
    });
    await flush();

    await act(async () => {
      findButton(renderer, "composer-tool-test-button").props.onClick();
    });
    const testInputs = renderer.root.findAll((node) => node.type === "input");
    await act(async () => {
      testInputs[0]?.props.onChange({ target: { checked: true } });
      testInputs[1]?.props.onChange({ target: { value: "1" } });
      testInputs[2]?.props.onChange({ target: { checked: true } });
    });
    const confirm = renderer.root.find((node) => node.type === "button"
      && node.children.join("").includes("开始测试"));
    await act(async () => { confirm.props.onClick(); });
    await flush();

    // 触发一轮轮询：请求发出后保持挂起，属于第一个 Gateway。
    await act(async () => {
      tickIntervals();
    });
    await flush();
    expect(runPollRequests).toBeGreaterThan(0);

    // 切到另一 Gateway 的同 UUID 工作区；旧轮询请求此刻仍在途。
    await act(async () => {
      renderer.update(
        <ComposerToolControl
          apiPort={49_501}
          agentId="agent_a"
          workspaceId={WORKSPACE_KEYS[1]}
          onStatus={() => undefined}
          models={[]}
        />,
      );
    });
    await flush();

    await act(async () => {
      runPoll.resolve(apiSuccessResponse({
        run_id: "tooltest_1",
        tool_name: "alpha_tool",
        status: "completed",
        progress: 100,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
        repetitions: 1,
        models: [{
          model_ref: { provider_id: "p", model_id: "default" },
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
    restoreComposerTestEnvironment();
  });

  test("Agent和工作区切换后，旧测试启动回执不得进入当前控制面", async () => {
    installComposerTestEnvironment();
    let resolveStart!: (value: Response) => void;
    let startRequest: GatewayFetchRequest | undefined;
    let renderer!: ReactTestRenderer;
    const statuses: string[] = [];
    installGatewayFetch((request) => {
      const headers = new Headers(request.init?.headers);
      if (request.path === "/api/v1/tools") return apiSuccessResponse(toolCatalogPayload("shared_tool"));
      if (request.path === "/api/v1/tools/tests") {
        return apiSuccessResponse({ items: [], next_cursor: null, has_more: false });
      }
      if (request.path === "/api/v1/tools/shared_tool/tests" && request.method === "POST") {
        startRequest = request;
        return new Promise<Response>((resolve) => { resolveStart = resolve; });
      }
      return undefined;
    }, { token: "tool-start-owner-token" });

    renderer = await renderAndOpen({ agentId: "agent_a", workspaceId: WORKSPACE_KEYS[0] }, {
      onStatus: (text) => statuses.push(text),
    });
    await confirmToolTest(renderer);
    expect(startRequest).toBeDefined();
    const originalHeaders = new Headers(startRequest?.init?.headers);
    expect(originalHeaders.get("X-BoxTeam-Target-Gateway-Id")).toBe(OWNERS[0].gatewayId);
    expect(originalHeaders.get("X-BoxTeam-Workspace-Id")).toBe(OWNERS[0].workspaceId);
    expect(JSON.parse(String(startRequest?.init?.body)).agent_id).toBe("agent_a");

    await act(async () => {
      renderer.update(
        <ComposerToolControl
          apiPort={49_501}
          agentId="agent_b"
          workspaceId={WORKSPACE_KEYS[1]}
          onStatus={(text) => statuses.push(text)}
          models={[{ model_ref: { provider_id: "test-provider", model_id: "default" }, model: "test-model", custom_llm_provider: "openai", available: true }]}
        />,
      );
    });
    await flush();
    expect(JSON.stringify(renderer.toJSON())).toContain("shared_tool");

    await act(async () => { resolveStart(apiSuccessResponse(toolTestRunPayload("tooltest_old_owner", "queued"))); });
    await flush();
    const html = JSON.stringify(renderer.toJSON());
    expect(html).not.toContain("等待测试");
    expect(html).not.toContain("进度 0%");
    expect(statuses.some((status) => status.includes("已启动"))).toBe(false);
    act(() => renderer.unmount());
  });

  test("关闭观察会释放轮询，同一 Agent 的新页面从历史继续观察活动测试", async () => {
    installComposerTestEnvironment();
    let pollRequests = 0;
    const running = toolTestRunPayload("tooltest_same_agent", "running", 25);
    installGatewayFetch((request) => {
      const headers = new Headers(request.init?.headers);
      expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(OWNERS[0].gatewayId);
      expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(OWNERS[0].workspaceId);
      if (request.path === "/api/v1/tools") return apiSuccessResponse(toolCatalogPayload("shared_tool"));
      if (request.path === "/api/v1/tools/tests") {
        return apiSuccessResponse({ items: [running], next_cursor: null, has_more: false });
      }
      if (request.path === "/api/v1/tools/tests/tooltest_same_agent") {
        pollRequests += 1;
        return apiSuccessResponse(toolTestRunPayload("tooltest_same_agent", "completed", 100));
      }
      return undefined;
    }, { token: "tool-resume-owner-token" });

    const firstPage = await renderAndOpen({ agentId: "agent_a", workspaceId: WORKSPACE_KEYS[0] });
    expect(intervalCallbacks.size).toBeGreaterThan(0);
    await act(async () => { findButton(firstPage, "composer-tool-close").props.onClick(); });
    await flush();
    expect(intervalCallbacks.size).toBe(0);
    act(() => firstPage.unmount());

    const newPage = await renderAndOpen({ agentId: "agent_a", workspaceId: WORKSPACE_KEYS[0] });
    expect(intervalCallbacks.size).toBeGreaterThan(0);
    await act(async () => { tickIntervals(); });
    await flush();
    expect(pollRequests).toBeGreaterThan(0);
    expect(JSON.stringify(newPage.toJSON())).toContain("100%");
    act(() => newPage.unmount());
    expect(intervalCallbacks.size).toBe(0);
  });
});
