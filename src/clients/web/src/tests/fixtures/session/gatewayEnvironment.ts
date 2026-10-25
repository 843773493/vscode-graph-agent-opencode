import type {
  GatewayUserAccess,
  Session,
} from "../../../types/backend";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";

/** Gateway fetch 路由、响应和测试用 API 默认值。 */

const originalFetch = globalThis.fetch;

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(
  globalThis,
  "window",
);

const originalDocumentDescriptor = Object.getOwnPropertyDescriptor(
  globalThis,
  "document",
);

export const TEST_GATEWAY_WORKSPACE_REQUEST_TARGET: GatewayWorkspaceRequestTarget = {
  workspace_id: "ws-test",
  backend_workspace_id: "00000000-0000-0000-0000-000000000001",
  connection_kind: "local",
};

const DEFAULT_WORKSPACE_REQUEST_TARGETS = [TEST_GATEWAY_WORKSPACE_REQUEST_TARGET] as const;

const TEST_WORKSPACE_TARGETS_BY_GATEWAY_ID = new Map<
  string,
  readonly GatewayWorkspaceRequestTarget[]
>([[TEST_GATEWAY_WORKSPACE_REQUEST_TARGET.workspace_id, DEFAULT_WORKSPACE_REQUEST_TARGETS]]);

export function testWorkspaceRequestTargetsFor(
  gatewayWorkspaceId: string | null,
): readonly GatewayWorkspaceRequestTarget[] {
  if (gatewayWorkspaceId === null) return DEFAULT_WORKSPACE_REQUEST_TARGETS;
  const cached = TEST_WORKSPACE_TARGETS_BY_GATEWAY_ID.get(gatewayWorkspaceId);
  if (cached) return cached;
  const targets = [{ ...TEST_GATEWAY_WORKSPACE_REQUEST_TARGET, workspace_id: gatewayWorkspaceId }];
  TEST_WORKSPACE_TARGETS_BY_GATEWAY_ID.set(gatewayWorkspaceId, targets);
  return targets;
}

/** 统一的后端响应信封，request_id 必须是合法非空字符串。 */
export function apiSuccessResponse(data: unknown, status = 200): Response {
  const message = (data as { message?: string } | null)?.message ?? "ok";
  return Response.json(
    { code: status === 200 ? 0 : status, message, request_id: "req_test", data },
    { status },
  );
}

/** 用例逐字复用的错误响应（HTTP 状态 + detail），走真实 fetch 错误通路。 */
export function apiErrorResponse(status: number, message: string): Response {
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
) => Response | Promise<Response | undefined> | undefined;

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
        return apiSuccessResponse({ token });
      }
      if (parsed.pathname === "/api/gateway/users/current") {
        return apiSuccessResponse({ kind: "guest", user_id: null });
      }
      const method = String(init?.method ?? "GET");
      const response = await handler({
        url,
        path: parsed.pathname,
        method,
        init,
      });
      if (response !== undefined) return await response;
      const breadcrumbPrefix = "/api/v1/session-catalog/breadcrumb/";
      if (method === "GET" && parsed.pathname.startsWith(breadcrumbPrefix)) {
        const sessionId = decodeURIComponent(parsed.pathname.slice(breadcrumbPrefix.length));
        return sessionCatalogBreadcrumbResponse(sessionId);
      }
      if (method === "GET" && parsed.pathname === "/api/v1/session-catalog/children") {
        return catalogChildrenResponse(
          "catalog-revision-4",
          [],
          parsed.searchParams.get("parent_node_id"),
          4,
        );
      }
      throw new Error(`测试收到未声明请求: ${method} ${parsed.pathname}`);
    },
    { preconnect: originalFetch.preconnect },
  ) as typeof fetch;
}

export function testGatewayUserAccess(): GatewayUserAccess {
  return {
    kind: "guest",
    user_id: "",
    lease_generation: 0,
    expires_at: "",
    takeover: false,
  };
}

function sessionCatalogBreadcrumbResponse(sessionId: string): Response {
  return apiSuccessResponse({
    revision: "catalog-revision-3",
    catalog_revision: 3,
    items: [{
      node_id: sessionId,
      kind: "session",
      name: sessionId,
      parent_node_id: null,
      session_id: sessionId,
      has_children: false,
      node_revision: 2,
      state: "active",
      session: null,
    }],
  });
}

/** 将 fetch、window、document 还原到夹具加载时的形态，供 afterEach 调用。 */
export function restoreGatewayTestEnvironment(): void {
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

/**
 * 先让用例自己的 handler 认领请求，未认领的导航/生成器探测回落到默认空响应。
 * 只覆盖这两条每个用例都会被动触发、但极少真正关心的路由。
 */
export function withDefaultGatewayApiResponses(
  handler: GatewayFetchHandler,
): GatewayFetchHandler {
  return async (request) => {
    const claimed = await handler(request);
    if (claimed !== undefined) return claimed;
    if (request.path.includes("/api/gateway/workspace-navigation")) {
      return apiSuccessResponse({ revision: "navigation", nodes: [] });
    }
    if (request.path.includes("/api/gateway/session-generators")) {
      return apiSuccessResponse({ revision: "generators", items: [] });
    }
    return undefined;
  };
}

/** 会话目录子节点的标准成功响应。 */
export function catalogChildrenResponse(
  revision: string,
  items: unknown[] = [],
  parentNodeId: string | null = null,
  catalogRevision = 3,
): Response {
  return apiSuccessResponse({
    revision,
    catalog_revision: catalogRevision,
    parent_node_id: parentNodeId,
    items,
    cursor: null,
    total: items.length,
  });
}

/** 会话列表的整表响应（无分页）。 */
export function sessionsListResponse(items: Session[]): Response {
  return apiSuccessResponse({ items, has_more: false, next_cursor: null });
}
