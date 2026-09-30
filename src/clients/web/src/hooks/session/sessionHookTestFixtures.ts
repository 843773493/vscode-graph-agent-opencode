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
import { useSessionRunActions } from "./useSessionRunActions";
import { useSessionGoalController } from "./useSessionGoalController";
import {
  useSessionViewState,
  type SessionViewStateController,
  type SessionViewStateHost,
} from "./useSessionViewState";
import { invalidateGatewayToken } from "../../api/http";

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
const originalDocumentDescriptor = Object.getOwnPropertyDescriptor(
  globalThis,
  "document",
);

/** 统一的后端响应信封，request_id 必须是合法非空字符串。 */
export function apiResponse(data: unknown, status = 200): Response {
  const message = (data as { message?: string } | null)?.message ?? "ok";
  return Response.json(
    { code: status === 200 ? 0 : status, message, request_id: "req_test", data },
    { status },
  );
}

/** 用例逐字复用的错误响应（HTTP 状态 + detail），走真实 fetch 错误通路。 */
export function errorResponse(status: number, message: string): Response {
  return Response.json(
    { detail: message },
    { status, headers: { "content-type": "application/json" } },
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

/** 安装只有计时器与空监听器的 window 桩；用 restoreSessionHookGlobals 还原。 */
export function installTestWindow(port: number): void {
  // 进程级 token 缓存按端口存活；同一端口在别的测试文件里可能已缓存过 token，
  // 这里显式作废，保证本文件发出的凭据请求一定打到自己的 fetch 桩上。
  invalidateGatewayToken(port);
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      setInterval: globalThis.setInterval.bind(globalThis),
      clearInterval: globalThis.clearInterval.bind(globalThis),
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
}

/** 安装只读可见性 document 桩；用 restoreSessionHookGlobals 还原。 */
export function installTestDocument(): void {
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      visibilityState: "visible",
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
}

/** 将 fetch、window、document 还原到夹具加载时的形态，供 afterEach 调用。 */
export function restoreSessionHookGlobals(): void {
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
  if (originalDocumentDescriptor) {
    Object.defineProperty(globalThis, "document", originalDocumentDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "document");
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
 * 挂起一个请求直到显式释放，用于验收「请求在途期间卸载或切换会话」的并发
 * 场景（例如 SSE 长连接、后端无响应）。不释放就永不落地。
 */
export function hangUntilReleased<T>(): {
  promise: Promise<T>;
  release: (value: T) => void;
} {
  let release!: (value: T) => void;
  const promise = new Promise<T>((resolve) => {
    release = resolve;
  });
  return { promise, release };
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

/** useSessionRunActions 的入参：除 setState 外与生产签名逐字段一致。 */
type RunActionsProps = Omit<
  Parameters<typeof useSessionRunActions>[0],
  "setState"
>;
type RunActions = ReturnType<typeof useSessionRunActions>;
type SessionRunActionsHandle = RunActions;

/**
 * 挂载 useSessionRunActions 并把最新 state 镜像到闭包。所有用例共用同一组
 * 网关工作区标识与固定 refreshAgentStateSnapshot，只覆盖自己关心的动作句柄。
 */
export function mountSessionRunActions(options: {
  currentSession: Session | null;
  state: AppState;
  cacheKey: string;
}): { state: () => AppState; actions: SessionRunActionsHandle } {
  const { currentSession, cacheKey } = options;
  const mirror = createStateMirror(options.state);
  let actions: SessionRunActionsHandle | null = null;
  function Harness(): React.ReactNode {
    actions = useSessionRunActions({
      apiPort: 8014,
      currentSession,
      activeGatewayWorkspaceId: "gw_send_regression",
      currentSessionGatewayWorkspaceId: "gw_send_regression",
      currentSessionCacheKey: cacheKey,
      defaultGatewayWorkspaceId: "gw_send_regression",
      contentView: "default",
      setState: mirror.setState,
      refreshAgentStateSnapshot: async () => undefined,
    });
    return null;
  }
  renderToStaticMarkup(React.createElement(Harness));
  if (!actions) throw new Error("useSessionRunActions Harness 未完成渲染");
  return { state: mirror.current, actions };
}

type GoalController = ReturnType<typeof useSessionGoalController>;
type SessionGoalControllerHandle = GoalController;

/**
 * 挂载 useSessionGoalController，用真实 React 状态机把最新 AppState 镜像到闭包。
 * 该 hook 依赖 effect 重放，必须走 create/act，不能只做一次静态渲染。
 */
export async function mountSessionGoalController(options: {
  initial: AppState;
}): Promise<{
  unmount: () => void;
  controller: () => SessionGoalControllerHandle;
  state: () => AppState;
}> {
  const { initial } = options;
  let controller: SessionGoalControllerHandle | null = null;
  let latestState = initial;
  function Harness(): React.ReactNode {
    const [currentState, setState] = React.useState(() => initial);
    latestState = currentState;
    controller = useSessionGoalController({
      apiPort: 49_406,
      currentSessionId: currentState.currentSession?.session_id ?? null,
      currentWorkspaceId: currentState.currentSessionWorkspaceId,
      setState,
    });
    return null;
  }
  const unmount = await mountHarness(Harness, 1);
  if (!controller) throw new Error("useSessionGoalController Harness 未完成渲染");
  return { unmount, controller: () => controller!, state: () => latestState };
}

type ResourceExplorerProps = Omit<
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

/** 挂载一个 React 测试渲染器并跑指定轮数的 effect，返回卸载函数。 */
export async function mountHarness(
  Harness: () => React.ReactNode,
  flushes = 2,
): Promise<() => void> {
  let renderer: ReactTestRenderer;
  await act(async () => {
    renderer = create(React.createElement(Harness));
    for (let i = 0; i < flushes; i += 1) await flushEffects();
  });
  return () => act(() => renderer!.unmount());
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

// —— 第二批（生命周期动作 / 资源探索器）共享装配 ——

interface BuildSessionHookStateOverrides {
  workspaceId: string;
  current: Session | null;
  sessions: Session[];
  gatewayWorkspaces?: { workspace_id: string; root_path: string; name: string }[];
  sessionsByWorkspace?: Map<string, Session[]>;
  sessionGatewayWorkspaceById?: Map<string, string>;
  eventQueuesBySession?: Map<string, unknown>;
  pendingConversations?: Map<string, unknown>;
  activeJobIdsBySession?: Map<string, string>;
  unreadSessionKeys?: Set<string>;
  sessionAttachmentSummaries?: Map<string, unknown>;
}

/**
 * 生命周期动作用例的 AppState 装配：会话列表与当前会话是最常覆盖的字段，
 * 其余会话级镜像统一给空值。只用于测试镜像，AppState 大部分字段与断言无关。
 */
export function buildSessionHookState(
  overrides: BuildSessionHookStateOverrides,
): AppState {
  const {
    workspaceId,
    current,
    sessions,
    gatewayWorkspaces = [],
    sessionsByWorkspace = new Map([[workspaceId, sessions]]),
    sessionGatewayWorkspaceById = new Map(),
    eventQueuesBySession = new Map(),
    pendingConversations = new Map(),
    activeJobIdsBySession = new Map(),
    unreadSessionKeys = new Set(),
    sessionAttachmentSummaries = new Map(),
  } = overrides;
  return {
    gatewayWorkspaces,
    sessions,
    sessionsByWorkspace,
    sessionGatewayWorkspaceById,
    sessionAttachmentSummaries,
    eventQueuesBySession,
    pendingConversations,
    activeJobIdsBySession,
    unreadSessionKeys,
    activeGatewayWorkspaceId: workspaceId,
    currentSession: current,
    currentSessionWorkspaceId: workspaceId,
    contentView: "default",
    sessionHistoryReloadNonce: 0,
    status: "",
  } as unknown as AppState;
}

/** 会话目录子节点的标准成功响应。 */
export function catalogChildrenResponse(
  revision: string,
  items: unknown[] = [],
  parentNodeId: string | null = null,
): Response {
  return apiResponse({
    revision,
    parent_node_id: parentNodeId,
    items,
    cursor: null,
    total: items.length,
  });
}

/** 会话列表的整表响应（无分页）。 */
export function sessionsListResponse(items: Session[]): Response {
  return apiResponse({ items, has_more: false, next_cursor: null });
}

/** 删除会话成功的响应。 */
export function deleteSessionResponse(sessionId: string): Response {
  return apiResponse({ session_id: sessionId });
}

/**
 * 会话删除类用例的常用 fetch 桩：DELETE /api/v1/sessions/{id} 与列表重读。
 * 两个响应都可传静态 Response 或按调用次数生成的工厂。
 */
export function installSessionDeleteFetch(options: {
  deleteResponse: Response | (() => Response);
  listResponse: Response | (() => Response);
}): void {
  const pick = (value: Response | (() => Response)): Response =>
    typeof value === "function" ? (value as () => Response)() : value;
  installGatewayFetch(({ path, method }) => {
    if (path.startsWith("/api/v1/sessions/") && method === "DELETE") {
      return pick(options.deleteResponse);
    }
    if (path === "/api/v1/sessions") return pick(options.listResponse);
    return undefined;
  });
}

/**
 * 挂载资源探索器并暴露句柄读取器，收敛「onExplorer 收集 + 空句柄检查」样板。
 * liveGeneratorResources 为真时由真实 useSessionGeneratorResources 供资源。
 */
export async function mountResourceExplorer(options: {
  props?: Partial<ResourceExplorerProps>;
  liveGeneratorResources?: boolean;
  flushes?: number;
}): Promise<{
  explorer: () => SessionResourceExplorerHandle;
  unmount: () => void;
}> {
  let explorer: SessionResourceExplorerHandle | null = null;
  const Harness = useSessionResourceExplorerHarness({
    props: explorerProps(options.props),
    liveGeneratorResources: options.liveGeneratorResources,
    onExplorer: (value) => {
      explorer = value;
    },
  });
  const unmount = await mountHarness(Harness, options.flushes ?? 2);
  if (!explorer) throw new Error("useSessionResourceExplorer Harness 未完成渲染");
  return { explorer: () => explorer!, unmount };
}

/**
 * 直播生成器资源版本的探索器 Harness 工厂：用例用 syncKey/currentSessionId
 * 驱动 props 更新，句柄通过 onExplorer 回传。
 */
export function liveExplorerHarness(options: {
  apiPort: number;
  activeWorkspaceId?: string | null;
  workspaceNavigationSyncKey?: string;
  onExplorer: (explorer: SessionResourceExplorerHandle) => void;
}): (props: { syncKey?: string; currentSessionId?: string }) => React.ReactNode {
  const activeWorkspaceId = options.activeWorkspaceId ?? "ws-test";
  return function Explorer(props): React.ReactNode {
    const generatorResources = useSessionGeneratorResources(options.apiPort);
    const explorer = useSessionResourceExplorer({
      ...explorerProps({
        apiPort: options.apiPort,
        activeWorkspaceId,
        workspaceNavigationSyncKey:
          props.syncKey ?? options.workspaceNavigationSyncKey ?? "ws-test",
        currentSessionId: props.currentSessionId ?? "",
      }),
      generatorResources,
    });
    options.onExplorer(explorer);
    return null;
  };
}

// —— 第三批（视图状态 / Goal 控制器）共享装配 ——

/**
 * 视图状态控制器 Harness：真实 React 状态机把最新 AppState 镜像到闭包，并向
 * 控制器提供与 hooks.tsx 相同的 setStatus 写法。hostGetter 用函数在每次渲染时
 * 更新 host，便于用例在挂载后切换 lease 代际 / 镜像。
 */
export async function mountViewStateController(options: {
  host: SessionViewStateHost | ((state: AppState) => SessionViewStateHost);
  initial: AppState;
}): Promise<{
  controller: () => SessionViewStateController;
  state: () => AppState;
  renderer: ReactTestRenderer;
  update: () => Promise<void>;
  unmount: () => void;
}> {
  const { initial } = options;
  let controller: SessionViewStateController | null = null;
  let latestState = initial;
  function Probe(): React.ReactNode {
    const [current, setState] = React.useState(() => initial);
    latestState = current;
    controller = useSessionViewState({
      host: typeof options.host === "function" ? options.host(current) : options.host,
      setState,
      setStatus: (message) => {
        setState((previous) => ({ ...previous, status: message }));
      },
    });
    return null;
  }
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(React.createElement(Probe));
  });
  if (!controller) throw new Error("useSessionViewState Harness 未完成渲染");
  return {
    controller: () => controller!,
    state: () => latestState,
    renderer,
    update: async () => {
      await act(async () => {
        renderer.update(React.createElement(Probe));
      });
    },
    unmount: () => act(() => renderer.unmount()),
  };
}

/**
 * Goal 控制器 Harness 工厂：保留自持状态机，用例可切换 currentSession 并注入
 * 残留 Goal 状态，句柄通过控制器集合与状态读写器回传。
 */
export function useGoalControllerHarness(options: {
  apiPort: number;
  initial: AppState;
}): {
  Harness: () => React.ReactNode;
  controller: () => ReturnType<typeof useSessionGoalController>;
  state: () => AppState;
  setState: (update: (previous: AppState) => AppState) => void;
} {
  let controller: ReturnType<typeof useSessionGoalController> | null = null;
  let latestState = options.initial;
  let applyUpdate: (update: (previous: AppState) => AppState) => void = () => {
    throw new Error("useSessionGoalController Harness 尚未挂载");
  };
  function Harness(): React.ReactNode {
    const [current, setState] = React.useState<AppState>(() => options.initial);
    latestState = current;
    applyUpdate = setState;
    controller = useSessionGoalController({
      apiPort: options.apiPort,
      currentSessionId: current.currentSession?.session_id ?? null,
      currentWorkspaceId: current.currentSessionWorkspaceId,
      setState,
    });
    return null;
  }
  return {
    Harness,
    controller: () => {
      if (!controller) throw new Error("useSessionGoalController Harness 未完成渲染");
      return controller;
    },
    state: () => latestState,
    setState: (update) => applyUpdate(update),
  };
}
