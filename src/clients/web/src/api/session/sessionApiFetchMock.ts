/**
 * `api/session/` 会话域 API 测试的共享 fetch 桩。
 *
 * 本包原先在 5 个测试文件里各写一份「第一次请求回本地凭据、之后按路径分发」的
 * 逐字重复实现；集中到这里后，凭据隧道与未声明请求的响亮失败只有一处实现。
 *
 * 共享 HTTP 层先读取本机凭据，再发业务请求；本桩按端点应答，避免把凭据请求当作业务请求。
 * 测试复用端口前显式作废凭据缓存。
 */

const originalFetch = globalThis.fetch;

const GATEWAY_CREDENTIAL_PATH = "/api/gateway/auth/local-credential";

/** 应答本机凭据；业务请求继续交给测试 handler。 */
function gatewayBarrierResponse(
  pathname: string,
  credentialToken: string | null,
): Response | undefined {
  if (credentialToken !== null && pathname === GATEWAY_CREDENTIAL_PATH) {
    return Response.json({
      code: 0,
      message: "ok",
      request_id: "req_local_credential",
      data: { token: credentialToken },
    });
  }
  return undefined;
}

export interface SessionApiFetchRequest {
  url: string;
  path: string;
  method: string;
  init: RequestInit | undefined;
}

/**
 * 返回 Response；返回 undefined 表示本用例不认领该请求。未认领的请求会被统一抛出，
 * 绝不允许静默返回空响应。
 */
export type SessionApiFetchHandler = (
  request: SessionApiFetchRequest,
) => Response | Promise<Response> | undefined;

/**
 * 安装共享 fetch 桩。
 *
 * - `credentialToken`：Gateway 本地凭据隧道返回的 token；传 null 表示不装凭据隧道
 *   （调用方自己按路径伪造凭据响应）。
 * - 业务响应一律交给 `handler`；未声明请求抛出带方法/路径的错误。
 */
export function installSessionApiFetchMock(
  handler: SessionApiFetchHandler,
  options: { credentialToken?: string | null } = {},
): void {
  const token = options.credentialToken === undefined ? "test-token" : options.credentialToken;
  globalThis.fetch = Object.assign(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const rawUrl = input instanceof Request ? input.url : String(input);
      const parsed = new URL(rawUrl, "http://127.0.0.1");
      const barrier = gatewayBarrierResponse(parsed.pathname, token);
      if (barrier !== undefined) return barrier;
      const method = String(init?.method ?? (input instanceof Request ? input.method : "GET"));
      const response = handler({
        url: rawUrl,
        path: parsed.pathname,
        method,
        init,
      });
      if (response === undefined) {
        throw new Error(`测试收到未声明请求: ${method} ${parsed.pathname}`);
      }
      return response;
    },
    { preconnect: originalFetch.preconnect },
  ) as typeof fetch;
}

/** 还原到安装前的 fetch；供 afterEach 调用。 */
export function unwrapSessionApiFetch(): void {
  globalThis.fetch = originalFetch;
}
