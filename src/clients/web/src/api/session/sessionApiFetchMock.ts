/**
 * `api/session/` 会话域 API 测试的共享 fetch 桩。
 *
 * 本包原先在 5 个测试文件里各写一份「第一次请求回本地凭据、之后按路径分发」的
 * 逐字重复实现；集中到这里后，凭据隧道与未声明请求的响亮失败只有一处实现。
 *
 * Gateway 用户会话屏障（api/http.ts 的 runGatewayRequest）会让每个 /api/v1/ 业务
 * 请求先等待 `GET /api/gateway/users/current` 完成，真实序列是「本地凭据 →
 * users/current → 业务请求」。若桩只应答凭据而把屏障请求交给用例 handler，用例里
 * 「无视路径一律返回某个响应」的桩就会把该响应喂给屏障请求：断言看似通过，业务端点
 * 其实从未被访问（断言空转）。因此本模块与 workspaceFileRequestMock 一致，在桩内部
 * 应答凭据与用户会话两条屏障隧道，只有业务请求才交给 handler。
 *
 * 生产代码不得导入本模块；它不是 API 客户端的一部分，只是测试支撑。
 */

const originalFetch = globalThis.fetch;

const GATEWAY_CREDENTIAL_PATH = "/api/gateway/auth/local-credential";
const GATEWAY_CURRENT_USER_PATH = "/api/gateway/users/current";
const GATEWAY_GUEST_USER_PATH = "/api/gateway/users/guest";

/**
 * 应答本地凭据与用户会话屏障两条隧道。
 *
 * 返回合法信封（request_id 非空、data 非 null）：屏障与业务端点共用
 * unwrapApiData 的信封校验，字段缺失会让屏障自己响亮失败，把有意义的断言淹没在
 * 无关错误里。`credentialToken` 为 null 表示调用方自行伪造凭据响应，此时凭据隧道
 * 不在此应答。
 */
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
  if (pathname === GATEWAY_CURRENT_USER_PATH) {
    return Response.json({
      code: 0,
      message: "ok",
      request_id: "req_current_user",
      data: { kind: "guest", user_id: null },
    });
  }
  if (pathname === GATEWAY_GUEST_USER_PATH) {
    return Response.json({
      code: 0,
      message: "ok",
      request_id: "req_guest_user",
      data: { kind: "guest", user_id: null },
    });
  }
  return undefined;
}

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
export function unwrapSessionCatalogFetch(): void {
  globalThis.fetch = originalFetch;
}
