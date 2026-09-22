import type {
  AcquireGatewayUserRequest,
  APIResponse,
  CreateGatewayGuestRequest,
  CreateGatewayUserRequest,
  GatewayUser,
  GatewayUserAccess,
  GatewayUserList,
} from "../../types/backend";
import {
  HttpRequestError,
  invalidateGatewayUserSession,
  registerGatewayUserSessionInitializer,
  requestJson,
  unwrapApiData,
  withGatewayUserSessionWrite,
} from "../http";

const WEB_GUEST_REQUEST: CreateGatewayGuestRequest = {
  // Guest 请求只发送协议必需字段，避免旧 Gateway/新 Gateway 间的
  // tracking 结构差异把初始化阻断为 422。
};

const pendingGatewayUserAccessByPort = new Map<
  number,
  Promise<GatewayUserAccess>
>();
const HEARTBEAT_RETRY_DELAYS_MS = [250, 1_000] as const;

/** 取消（页面卸载、会话切换）必须立即放弃，绝不当成网络抖动重试。 */
function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

// Guest 重建请求的唯一构造点：认证初始化的 401 兜底与显式切换游客都走这里，
// 并发编排（写串行锁、pending 失效）由各自调用方负责。
async function requestGatewayGuest(port: number): Promise<GatewayUserAccess> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUserAccess>>(
      port,
      "/api/gateway/users/guest",
      {
        method: "POST",
        body: JSON.stringify(WEB_GUEST_REQUEST),
        skipGatewayUserSession: true,
      },
    ),
  );
}

// 所有工作区业务请求都经由 http.ts 的屏障等待这里完成 current/guest。
// 认证探测本身显式跳过屏障，避免初始化请求递归等待自己。
registerGatewayUserSessionInitializer((port) => ensureGatewayUserAccess(port));

export async function getCurrentGatewayUser(
  port: number,
): Promise<GatewayUserAccess> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUserAccess>>(
      port,
      "/api/gateway/users/current",
      { skipGatewayUserSession: true },
    ),
  );
}

export async function ensureGatewayUserAccess(
  port: number,
): Promise<GatewayUserAccess> {
  const pending = pendingGatewayUserAccessByPort.get(port);
  if (pending) return pending;

  const initialization = (async () => {
    // current 判定与游客重建必须整体进入会话写串行锁，避免锁外的 401
    // 结论与并发 acquire 互相覆盖 cookie。
    return await withGatewayUserSessionWrite(port, async () => {
      try {
        return await getCurrentGatewayUser(port);
      } catch (error: unknown) {
        if (!(error instanceof HttpRequestError) || error.status !== 401) throw error;
        return await requestGatewayGuest(port);
      }
    });
  })();
  pendingGatewayUserAccessByPort.set(port, initialization);
  void initialization.then(() => {
    if (pendingGatewayUserAccessByPort.get(port) === initialization) {
      pendingGatewayUserAccessByPort.delete(port);
    }
  }, () => {
    if (pendingGatewayUserAccessByPort.get(port) === initialization) {
      pendingGatewayUserAccessByPort.delete(port);
    }
  });
  return initialization;
}

function invalidatePendingGatewayUserAccess(port: number): void {
  pendingGatewayUserAccessByPort.delete(port);
}

export async function acquireGatewayGuest(
  port: number,
): Promise<GatewayUserAccess> {
  invalidatePendingGatewayUserAccess(port);
  invalidateGatewayUserSession(port);
  return await withGatewayUserSessionWrite(port, () => requestGatewayGuest(port));
}

export async function listGatewayUsers(port: number): Promise<GatewayUserList> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUserList>>(port, "/api/gateway/users"),
  );
}

export async function createGatewayUser(
  port: number,
  payload: CreateGatewayUserRequest,
): Promise<GatewayUser> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUser>>(port, "/api/gateway/users", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  );
}

export async function deleteGatewayUser(port: number, userId: string): Promise<void> {
  await requestJson<APIResponse<{ user_id: string }>>(
    port,
    `/api/gateway/users/${encodeURIComponent(userId)}`,
    { method: "DELETE" },
  );
}

async function acquireGatewayUser(
  port: number,
  userId: string,
  path: "access" | "takeover",
  clientLabel?: string,
): Promise<GatewayUserAccess> {
  invalidatePendingGatewayUserAccess(port);
  invalidateGatewayUserSession(port);
  return await withGatewayUserSessionWrite(port, async () =>
    unwrapApiData(
      await requestJson<APIResponse<GatewayUserAccess>>(
        port,
        `/api/gateway/users/${encodeURIComponent(userId)}/${path}`,
        {
          method: "POST",
          body: JSON.stringify({
            client_label: clientLabel ?? null,
          } satisfies AcquireGatewayUserRequest),
          skipGatewayUserSession: true,
        },
      ),
    ),
  );
}

export function selectGatewayUser(
  port: number,
  userId: string,
  clientLabel?: string,
): Promise<GatewayUserAccess> {
  return acquireGatewayUser(port, userId, "access", clientLabel);
}

export function takeoverGatewayUser(
  port: number,
  userId: string,
  clientLabel?: string,
): Promise<GatewayUserAccess> {
  return acquireGatewayUser(port, userId, "takeover", clientLabel);
}

export async function heartbeatGatewayUser(
  port: number,
): Promise<GatewayUserAccess> {
  return unwrapApiData(
    await requestJson<APIResponse<GatewayUserAccess>>(
      port,
      "/api/gateway/users/current/heartbeat",
      { method: "POST" },
    ),
  );
}

export async function heartbeatGatewayUserWithRetry(
  port: number,
): Promise<GatewayUserAccess> {
  for (let attempt = 0; attempt <= HEARTBEAT_RETRY_DELAYS_MS.length; attempt += 1) {
    try {
      return await heartbeatGatewayUser(port);
    } catch (error) {
      // 409/401 是业务鉴权结果、AbortError 是调用方主动取消，都必须立即交给
      // 上层处理；这里只恢复重启或网络切换造成的 fetch 传输失败，重试严格有界。
      if (
        error instanceof HttpRequestError
        || isAbortError(error)
        || attempt === HEARTBEAT_RETRY_DELAYS_MS.length
      ) {
        throw error;
      }
      await new Promise<void>((resolve) => {
        globalThis.setTimeout(resolve, HEARTBEAT_RETRY_DELAYS_MS[attempt]);
      });
    }
  }
  throw new Error("heartbeat 重试未返回结果");
}
