import { invalidateGatewayToken, invalidateGatewayUserSession } from "../../api/http";

/**
 * 工作区文件接口测试的共享 fetch 桩支撑。
 *
 * 生产代码不得导入本模块；它不是 API 客户端的一部分，只是测试支撑。
 *
 * Gateway 用户会话屏障（api/http.ts 的 runGatewayRequest）会让每个 /api/v1/
 * 业务请求先等待 GET /api/gateway/users/current 完成，真实序列是
 * 「本地凭据 → users/current → 业务请求」。测试若按数组下标取「业务请求」，取到
 * 的是屏障请求，断言随之变成空转或直接抛错。本模块收口两条屏障隧道与「按端点
 * 路径选取业务请求」，避免各用例各写一份下标假设。
 *
 * 例外：二进制下载与 multipart 上传显式传 skipGatewayUserSession: true，不会发出
 * users/current，见 getWorkspaceRawFileBlob 与 uploadWorkspaceFileEntries。
 *
 * 端口在不同测试文件间可能被复用：进程级凭据缓存与「用户会话已就绪」缓存都必须
 * 按端口显式作废，否则会拿着上一个文件缓存的 token 或已完成的屏障直接跳过本文件的
 * 桩，让请求序列断言随机漂移。
 */

export const GATEWAY_CREDENTIAL_PATH = "/api/gateway/auth/local-credential";
export const GATEWAY_CURRENT_USER_PATH = "/api/gateway/users/current";
export const GATEWAY_GUEST_USER_PATH = "/api/gateway/users/guest";

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

/**
 * 应答本地凭据与用户会话屏障两条隧道。返回合法信封（request_id 非空、data 非
 * null），因为屏障与业务端点共用 unwrapApiData 的信封校验，字段缺失会让屏障自己
 * 响亮失败，把有意义的断言淹没在无关错误里。
 */
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
  invalidateGatewayUserSession(options.port);
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
