import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import type { Session } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { sessionScopeKey } from "../../state/session/sessionScope";
import type { SetAppState } from "../contentViewLoaderTypes";
import type { SessionGeneratorResourcesController } from "../sessionResourceExplorer/useSessionGeneratorResources";
import { useSessionGeneratorResources } from "../sessionResourceExplorer/useSessionGeneratorResources";
import { useSessionLifecycleActions } from "./useSessionLifecycleActions";
import { useSessionMessageStream } from "./useSessionMessageStream";
import { useSessionResourceExplorer } from "./useSessionResourceExplorer";

/**
 * 会话级 hook 测试共享夹具。
 *
 * 只承载跨用例逐字重复的样板：统一响应信封、统一 Gateway fetch 路由前置
 * （本地凭据 / 当前用户）、统一 window 安装与全局还原，以及各 hook 的
 * Harness 装配。它不是通用测试框架，只服务 hooks/session/ 下的测试。
 */

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(
  globalThis,
  "window",
);

/** 统一的后端响应信封，request_id 必须是合法非空字符串。 */
export function apiResponse(data: unknown, status = 200): Response {
  const message = (data as { message?: string } | null)?.message ?? "ok";
  return Response.json(
    { code: status === 200 ? 0 : status, message, request_id: "req_test", data },
    { status },
  );
}

export interface GatewayFetchRequest {
  url: string;
  path: string;
  method: string;
  init: RequestInit | undefined;
}

/**
 * 返回 Response、或返回 undefined 表示未声明该请求（由夹具统一抛错）。
 * 未预期请求必须响亮失败，绝不能静默返回空响应。
 */
export type GatewayFetchHandler = (
  request: GatewayFetchRequest,
) => Response | Promise<Response> | undefined;

/**
 * 安装统一的 fetch mock：先收口本地凭据与当前用户两条隧道，再交给用例自己的
 * handler；handler 不认领的请求一律抛出。保留 preconnect 以维持 fetch 类型。
 */
export function installGatewayFetch(
  handler: GatewayFetchHandler,
  options: { token?: string } = {},
): void {
  const token = options.token ?? "test-token";
  globalThis.fetch = Object.assign(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = input instanceof Request ? input.url : String(input);
      const parsed = new URL(url, "http://localhost");
      if (parsed.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token });
      }
      if (parsed.pathname === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      const method = String(init?.method ?? "GET");
      const response = handler({
        url,
        path: parsed.pathname,
        method,
        init,
      });
      if (response === undefined) {
        throw new Error(`测试收到未声明请求: ${method} ${parsed.pathname}`);
      }
      return await response;
    },
    { preconnect: originalFetch.preconnect },
  ) as typeof fetch;
}

/** 安装只有计时器的 window 桩；测试结束后用 restoreSessionHookGlobals 还原。 */
export function installTestWindow(port: number): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    },
  });
}

/** 将 fetch 与 window 一并还原到夹具加载时的形态，供 afterEach 调用。 */
export function restoreSessionHookGlobals(): void {
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
}

function emptyGeneratorResources(): SessionGeneratorResourcesController {
  return {
    generators: null,
    generationRuns: new Map(),
    generatorError: null,
  } as unknown as SessionGeneratorResourcesController;
}

/** 等待一轮 effect 落地。 */
export async function flushEffects(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

/**
 * 先让用例自己的 handler 认领请求，未认领的导航/生成器探测回落到默认空响应。
 * 只覆盖这两条每个用例都会被动触发、但极少真正关心的路由。
 */
export function withCatalogDefaults(
  handler: GatewayFetchHandler,
): GatewayFetchHandler {
  return (request) => {
    const claimed = handler(request);
    if (claimed !== undefined) return claimed;
    if (request.path.includes("/api/gateway/workspace-navigation")) {
      return apiResponse({ revision: "navigation", nodes: [] });
    }
    if (request.path.includes("/api/gateway/session-generators")) {
      return apiResponse({ revision: "generators", items: [] });
    }
    return undefined;
  };
}

/** 把最新 AppState 镜像到闭包，并把 setState 桥接成委托更新。 */
export function createStateMirror(initial: AppState): {
  readonly setState: SetAppState;
  current: () => AppState;
} {
  let current = initial;
  return {
    setState: (update) => {
      current = typeof update === "function"
        ? (update as (prev: AppState) => AppState)(current)
        : update;
    },
    current: () => current,
  };
}

type LifecycleActions = ReturnType<typeof useSessionLifecycleActions>;

/**
 * 挂载 useSessionLifecycleActions 并把最新 state 镜像到闭包。
 * 返回读 state 与动作引用的句柄，避免每个用例重写 Harness 与 setState 桥接。
 */
export function mountSessionLifecycleActions(options: {
  apiPort: number;
  currentSession: Session | null;
  workspaceId: string;
  state: AppState;
  abortCurrentStream?: () => void;
}): { state: () => AppState; actions: LifecycleActions } {
  const { apiPort, currentSession, workspaceId, abortCurrentStream } = options;
  const mirror = createStateMirror(options.state);
  let actions: LifecycleActions | null = null;
  function Harness(): React.ReactNode {
    actions = useSessionLifecycleActions({
      apiPort,
      currentSession,
      activeGatewayWorkspaceId: workspaceId,
      currentSessionGatewayWorkspaceId: workspaceId,
      currentSessionCacheKey: sessionScopeKey(
        workspaceId,
        currentSession?.session_id ?? "",
      ),
      defaultGatewayWorkspaceId: workspaceId,
      setState: mirror.setState,
      abortCurrentStream: abortCurrentStream ?? (() => undefined),
      invalidateAgentState: () => undefined,
    });
    return null;
  }
  renderToStaticMarkup(React.createElement(Harness));
  if (!actions) throw new Error("useSessionLifecycleActions Harness 未完成渲染");
  return { state: mirror.current, actions };
}

/** 装配 useSessionMessageStream 的 Harness 组件。 */
export function useSessionMessageStreamHarness(
  props: Omit<Parameters<typeof useSessionMessageStream>[0], "setState">,
  setState: SetAppState,
): () => React.ReactNode {
  return function Harness(): React.ReactNode {
    useSessionMessageStream({ ...props, setState });
    return null;
  };
}

export type ResourceExplorerProps = Omit<
  Parameters<typeof useSessionResourceExplorer>[0],
  "generatorResources"
>;
export type SessionResourceExplorerHandle = ReturnType<
  typeof useSessionResourceExplorer
>;

/** 探索器用例的公共 props；各用例只覆盖自己关心的字段。 */
export function explorerProps(
  overrides: Partial<ResourceExplorerProps> = {},
): ResourceExplorerProps {
  return {
    apiPort: 49_400,
    activeWorkspaceId: "ws-test",
    searchOpen: false,
    searchQuery: "",
    currentSessionId: "",
    workspaceNavigationSyncKey: "ws-test",
    catalogSyncKeys: new Map(),
    catalogRefreshVersions: new Map(),
    ...overrides,
  };
}

/** 挂载一个 React 测试渲染器并跑指定轮数的 effect。 */
export async function mountHarness(
  Harness: () => React.ReactNode,
  flushes = 2,
): Promise<{ renderer: ReactTestRenderer; unmount: () => void }> {
  let renderer: ReactTestRenderer;
  await act(async () => {
    renderer = create(React.createElement(Harness));
    for (let i = 0; i < flushes; i += 1) await flushEffects();
  });
  return { renderer: renderer!, unmount: () => act(() => renderer!.unmount()) };
}

/**
 * 装配 useSessionResourceExplorer 的 Harness。liveGeneratorResources 为真时
 * 由真实 useSessionGeneratorResources 提供资源，否则使用空控制器；两者在
 * Harness 外分派，避免条件调用 hook。
 */
export function useSessionResourceExplorerHarness(options: {
  props: ResourceExplorerProps;
  liveGeneratorResources?: boolean;
  onExplorer?: (explorer: SessionResourceExplorerHandle) => void;
}): () => React.ReactNode {
  const { props, onExplorer } = options;
  if (options.liveGeneratorResources) {
    return function Harness(): React.ReactNode {
      const generatorResources = useSessionGeneratorResources(props.apiPort);
      const explorer = useSessionResourceExplorer({ ...props, generatorResources });
      onExplorer?.(explorer);
      return null;
    };
  }
  // 空控制器必须是稳定引用，否则每次渲染都换对象会让 explorer 的依赖反复失效。
  const generatorResources = emptyGeneratorResources();
  return function Harness(): React.ReactNode {
    const explorer = useSessionResourceExplorer({
      ...props,
      generatorResources,
    });
    onExplorer?.(explorer);
    return null;
  };
}
