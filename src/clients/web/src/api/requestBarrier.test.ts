import { afterEach, describe, expect, test } from "bun:test";

import {
  HttpRequestError,
  requestGatewayResponse,
} from "./http";
import { getSessionAttachmentBlob } from "./session/sessionMessages";
import { streamSessionActivity } from "./session/sessionActivity";
import { streamSessionMessageEvents } from "./sessionMessageStream";
import { streamSessionEvents } from "./sessionTraceStream";
import { streamWorkspaceFileEvents } from "./workspaceFileEvents";
import { getWorkspaceRawFileBlob } from "./workspaceFilesystem";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

/**
 * 模拟 Gateway 重启轮换本地凭据：第一次下发的 token 已失效，业务请求返回
 * 401 invalid local token，第二次下发的新 token 才有效。统一请求屏障必须据此
 * 刷新凭据并重试；任何绕过屏障的手写 fetch 都只发一次就以普通 Error 失败。
 */
function installRotatingCredentialBackend(options: {
  businessResponse: () => Response;
}): { businessRequests: string[]; credentialRequests: number } {
  let credentialRequests = 0;
  const businessRequests: string[] = [];
  globalThis.fetch = Object.assign(
    async (...args: Parameters<typeof fetch>) => {
      const [input, init] = args;
      const url = new URL(String(input), "http://127.0.0.1");
      if (url.pathname === "/api/gateway/auth/local-credential") {
        credentialRequests += 1;
        return Response.json({
          data: { token: credentialRequests === 1 ? "stale-token" : "fresh-token" },
        });
      }
      const token = new Headers(init?.headers).get("X-Local-Token") ?? "";
      businessRequests.push(`${url.pathname}#${token}`);
      if (token === "stale-token") {
        return Response.json({ detail: "invalid local token" }, { status: 401 });
      }
      return options.businessResponse();
    },
    { preconnect: originalFetch.preconnect },
  );
  return {
    businessRequests,
    get credentialRequests() {
      return credentialRequests;
    },
  } as { businessRequests: string[]; credentialRequests: number };
}

const emptySseStream = () => new Response(": heartbeat\n\n", {
  status: 200,
  headers: { "content-type": "text/event-stream" },
});

describe("统一请求屏障 requestGatewayResponse", () => {
  test("凭据轮换后刷新 token 并重试，返回原始 Response", async () => {
    const backend = installRotatingCredentialBackend({
      businessResponse: () => new Response("raw-bytes", { status: 200 }),
    });

    const response = await requestGatewayResponse(48_601, "/api/v1/workspace/files/raw", {
      skipGatewayUserSession: true,
    });

    expect(await response.text()).toBe("raw-bytes");
    expect(backend.businessRequests).toEqual([
      "/api/v1/workspace/files/raw#stale-token",
      "/api/v1/workspace/files/raw#fresh-token",
    ]);
  });

  test("不可刷新的失败统一抛 HttpRequestError 并携带状态码", async () => {
    let credentialRequests = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const url = new URL(String(args[0]), "http://127.0.0.1");
        if (url.pathname === "/api/gateway/auth/local-credential") {
          credentialRequests += 1;
          return Response.json({ data: { token: "only-token" } });
        }
        return Response.json({ detail: "permission denied" }, { status: 403 });
      },
      { preconnect: originalFetch.preconnect },
    );

    const failure = requestGatewayResponse(48_602, "/api/v1/workspace/files/raw", {
      skipGatewayUserSession: true,
    });
    await expect(failure).rejects.toBeInstanceOf(HttpRequestError);
    await expect(failure).rejects.toMatchObject({ status: 403 });
    expect(credentialRequests).toBe(1);
  });
});

describe("二进制与流式路径共享统一凭据刷新屏障", () => {
  test("getWorkspaceRawFileBlob 凭据轮换后重试并返回二进制", async () => {
    const backend = installRotatingCredentialBackend({
      businessResponse: () => new Response(new Uint8Array([1, 2, 3]), {
        status: 200,
        headers: { "content-type": "image/png" },
      }),
    });

    const blob = await getWorkspaceRawFileBlob(48_610, "docs/a.png", "workspace-raw");

    expect(blob.size).toBe(3);
    expect(backend.businessRequests).toEqual([
      "/api/v1/workspace/files/raw#stale-token",
      "/api/v1/workspace/files/raw#fresh-token",
    ]);
  });

  test("getSessionAttachmentBlob 凭据轮换后重试并返回二进制", async () => {
    const backend = installRotatingCredentialBackend({
      businessResponse: () => new Response(new Uint8Array([9, 9]), { status: 200 }),
    });

    const blob = await getSessionAttachmentBlob(48_611, "ses_a", "file_a", "workspace-raw");

    expect(blob.size).toBe(2);
    expect(backend.businessRequests).toHaveLength(2);
  });

  test("getWorkspaceRawFileBlob 不可刷新失败时抛 HttpRequestError", async () => {
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const url = new URL(String(args[0]), "http://127.0.0.1");
        if (url.pathname === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "raw-token" } });
        }
        return Response.json({ detail: "not found" }, { status: 404 });
      },
      { preconnect: originalFetch.preconnect },
    );

    await expect(getWorkspaceRawFileBlob(48_612, "missing.png", "workspace-raw"))
      .rejects.toBeInstanceOf(HttpRequestError);
  });

  test("streamSessionMessageEvents 凭据轮换后重试再消费流", async () => {
    const backend = installRotatingCredentialBackend({ businessResponse: emptySseStream });

    await streamSessionMessageEvents(48_620, "ses_a", "turn_a", { workspaceId: "w" });

    expect(backend.businessRequests).toEqual([
      "/api/v1/sessions/ses_a/turns/turn_a/message-stream#stale-token",
      "/api/v1/sessions/ses_a/turns/turn_a/message-stream#fresh-token",
    ]);
  });

  test("streamSessionEvents 凭据轮换后重试再消费流", async () => {
    const backend = installRotatingCredentialBackend({ businessResponse: emptySseStream });

    await streamSessionEvents(48_621, "ses_a", { workspaceId: "w" });

    expect(backend.businessRequests).toEqual([
      "/api/v1/sessions/ses_a/traces/stream#stale-token",
      "/api/v1/sessions/ses_a/traces/stream#fresh-token",
    ]);
  });

  test("streamSessionActivity 凭据轮换后重试再消费流", async () => {
    const backend = installRotatingCredentialBackend({ businessResponse: emptySseStream });

    await streamSessionActivity(48_622, "w", {});

    expect(backend.businessRequests).toEqual([
      "/api/v1/session-catalog/events/stream#stale-token",
      "/api/v1/session-catalog/events/stream#fresh-token",
    ]);
  });

  test("streamWorkspaceFileEvents 凭据轮换后重试再消费流", async () => {
    const backend = installRotatingCredentialBackend({ businessResponse: emptySseStream });

    await streamWorkspaceFileEvents(48_623, ["/tmp/a.ts"], { workspaceId: "w" });

    expect(backend.businessRequests).toEqual([
      "/api/v1/workspace/files/events#stale-token",
      "/api/v1/workspace/files/events#fresh-token",
    ]);
  });
});
