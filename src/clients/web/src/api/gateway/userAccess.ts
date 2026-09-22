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
// 显式切换用户（游客/select/takeover）的代际令牌。在途初始化只有在代际未变时
// 才允许写入 cookie 或返回自己的结论；一旦被切换取代，它必须作废退出，由调用方
// 在切换落地后重新判定，绝不能把切换前的旧身份回传出去（用户身份串台）。
const gatewayUserAccessGenerationByPort = new Map<number, number>();
const HEARTBEAT_RETRY_DELAYS_MS = [250, 1_000] as const;

/** 被显式切换取代的在途初始化以此信号作废，不写 cookie、不返回旧身份。 */
class SupersededUserAccessError extends Error {
  constructor() {
    super("用户访问初始化已被显式切换取代");
    this.name = "SupersededUserAccessError";
  }
}

function userAccessGeneration(port: number): number {
  return gatewayUserAccessGenerationByPort.get(port) ?? 0;
}

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
  const generation = userAccessGeneration(port);

  // current 判定与游客重建必须整体进入会话写串行锁，避免锁外的 401
  // 结论与并发 acquire 互相覆盖 cookie。
  const initialization = withGatewayUserSessionWrite(port, async () => {
    try {
      return await getCurrentGatewayUser(port);
    } catch (error: unknown) {
      if (!(error instanceof HttpRequestError) || error.status !== 401) throw error;
      // 锁外发生的显式切换已经作废本次结论：不得重建游客把身份拉回，
      // 也不得把切换前的判定交给调用方。
      if (userAccessGeneration(port) !== generation) throw new SupersededUserAccessError();
      return await requestGatewayGuest(port);
    }
  });
  registerPendingGatewayUserAccess(port, initialization);
  return await inheritSupersedingUserAccess(port, initialization);
}

/** 登记在途初始化/切换结果，并在它仍是当前条目时自动清理。 */
function registerPendingGatewayUserAccess(
  port: number,
  pending: Promise<GatewayUserAccess>,
): void {
  pendingGatewayUserAccessByPort.set(port, pending);
  const clearIfCurrent = () => {
    if (pendingGatewayUserAccessByPort.get(port) === pending) {
      pendingGatewayUserAccessByPort.delete(port);
    }
  };
  void pending.then(clearIfCurrent, clearIfCurrent);
}

/**
 * 被显式切换取代的初始化不得把切换前的身份交给调用方：把调用方转交给取代者
 * （切换请求），由它返回切换后的唯一权威身份。
 */
async function inheritSupersedingUserAccess(
  port: number,
  initialization: Promise<GatewayUserAccess>,
): Promise<GatewayUserAccess> {
  try {
    return await initialization;
  } catch (error: unknown) {
    if (!(error instanceof SupersededUserAccessError)) throw error;
    const superseding = pendingGatewayUserAccessByPort.get(port);
    if (superseding && superseding !== initialization) return await superseding;
    throw error;
  }
}

/**
 * 显式切换用户（游客/select/takeover）的唯一入口。
 *
 * 先把切换本身登记为新的 pending，再等在途初始化退出写锁，最后执行切换写入。
 * 这样既保证切换是最后写入 cookie 的一方，也让被取代的初始化能直接继承切换结果，
 * 而不是回传切换前的旧身份。
 */
async function switchGatewayUserAccess(
  port: number,
  performSwitch: () => Promise<GatewayUserAccess>,
): Promise<GatewayUserAccess> {
  const superseded = pendingGatewayUserAccessByPort.get(port);
  gatewayUserAccessGenerationByPort.set(port, userAccessGeneration(port) + 1);
  invalidateGatewayUserSession(port);
  const switching = (async () => {
    if (superseded) await superseded.catch(() => undefined);
    return await withGatewayUserSessionWrite(port, performSwitch);
  })();
  registerPendingGatewayUserAccess(port, switching);
  return await switching;
}

export async function acquireGatewayGuest(
  port: number,
): Promise<GatewayUserAccess> {
  return await switchGatewayUserAccess(port, () => requestGatewayGuest(port));
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
  return await switchGatewayUserAccess(port, async () =>
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
