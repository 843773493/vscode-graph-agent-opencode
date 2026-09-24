/**
 * `api/session/` 会话域 API 测试的共享 fetch 桩。
 *
 * 本包原先在 5 个测试文件里各写一份「第一次请求回本地凭据、之后按路径分发」的
 * 逐字重复实现；集中到这里后，凭据隧道与未声明请求的响亮失败只有一处实现。
 *
 * 生产代码不得导入本模块；它不是 API 客户端的一部分，只是测试支撑。
 */

const originalFetch = globalThis.fetch;

export interface SessionCatalogFetchRequest {
  url: string;
  path: string;
  method: string;
  init: RequestInit | undefined;
}

/**
 * 返回 Response；返回 undefined 表示本用例不认领该请求。未认领的请求会被统一抛出，
 * 绝不允许静默返回空响应。
 */
export type SessionCatalogFetchHandler = (
  request: SessionCatalogFetchRequest,
) => Response | undefined;

/**
 * 安装共享 fetch 桩。
 *
 * - `credentialToken`：Gateway 本地凭据隧道返回的 token；传 null 表示不装凭据隧道
 *   （调用方自己按路径伪造凭据响应）。
 * - 业务响应一律交给 `handler`；未声明请求抛出带方法/路径的错误。
 */
export function installSessionCatalogFetchMock(
  handler: SessionCatalogFetchHandler,
  options: { credentialToken?: string | null } = {},
): void {
  const token = options.credentialToken === undefined ? "test-token" : options.credentialToken;
  globalThis.fetch = Object.assign(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const rawUrl = input instanceof Request ? input.url : String(input);
      const parsed = new URL(rawUrl, "http://127.0.0.1");
      if (token !== null && parsed.pathname === "/api/gateway/auth/local-credential") {
        return Response.json({ data: { token } });
      }
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
export function unwrapSessionCatalogFetch(): void {
  globalThis.fetch = originalFetch;
}
