import { invalidateGatewayToken } from "../../api/http";

/**
 * 工作区文件接口测试的共享 fetch 桩支撑。
 *
 * 生产代码不得导入本模块；它不是 API 客户端的一部分，只是测试支撑。
 *
 * 共享 HTTP 层先读取本机凭据，再发业务请求；本桩按端点应答，避免把凭据请求当作业务请求。
 * 测试复用端口前显式作废凭据缓存。
 */

export const GATEWAY_CREDENTIAL_PATH = "/api/gateway/auth/local-credential";

export interface RecordedRequest {
  url: string;
  init?: RequestInit;
}

export interface WorkspaceFileFetchRequest {
  url: string;
  pathname: string;
  method: string;
  init: RequestInit | undefined;
}

export type WorkspaceFileFetchHandler = (
  request: WorkspaceFileFetchRequest,
) => Response | undefined;

/** 应答本机凭据；业务请求继续交给测试 handler。 */
function gatewayBarrierResponse(
  pathname: string,
  token: string,
): Response | undefined {
  if (pathname === GATEWAY_CREDENTIAL_PATH) {
    return Response.json({
      code: 0,
      message: "ok",
      request_id: "req_local_credential",
      data: { token },
    });
  }
  return undefined;
}

/**
 * 安装统一 fetch 桩：屏障隧道由本模块应答，其余交给 handler；handler 不认领的
 * 请求一律抛出，绝不允许静默返回空响应。
 */
export function installWorkspaceFileFetchMock(options: {
  port: number;
  requests: RecordedRequest[];
  handler: WorkspaceFileFetchHandler;
  token?: string;
}): void {
  const { requests, handler } = options;
  const token = options.token ?? "workspace-file-token";
  invalidateGatewayToken(options.port);

  const previousFetch = globalThis.fetch;
  globalThis.fetch = Object.assign(
    async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      const rawUrl = input instanceof Request ? input.url : String(input);
      const parsed = new URL(rawUrl, "http://127.0.0.1");
      const method = String(
        init?.method ?? (input instanceof Request ? input.method : "GET"),
      );
      requests.push({ url: rawUrl, init });
      const barrier = gatewayBarrierResponse(parsed.pathname, token);
      if (barrier) return barrier;
      const response = handler({
        url: rawUrl,
        pathname: parsed.pathname,
        method,
        init,
      });
      if (response === undefined) {
        throw new Error(`测试收到未声明请求: ${method} ${parsed.pathname}`);
      }
      return response;
    },
    { preconnect: previousFetch.preconnect },
  ) as typeof fetch;
}

/**
 * 按端点路径取出业务请求，与下发顺序解耦；取不到即响亮失败，绝不返回 undefined
 * 让调用方的断言退化成空转。
 */
export function businessRequest(
  requests: readonly RecordedRequest[],
  pathname: string,
): RecordedRequest {
  const found = requests.find(
    (candidate) => new URL(candidate.url, "http://127.0.0.1").pathname === pathname,
  );
  if (!found) throw new Error(`未捕获业务请求: ${pathname}`);
  return found;
}
