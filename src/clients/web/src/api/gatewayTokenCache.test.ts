import { afterEach, describe, expect, test } from "bun:test";

import { getGatewayToken, invalidateGatewayToken } from "./http";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  invalidateGatewayToken(PORT);
});

const PORT = 49_301;

/** 记录凭据请求次数并下发指定 token 的 fetch 桩。 */
function installCredentialBackend(tokens: string[]): { credentialRequests: number } {
  const stats = { credentialRequests: 0 };
  globalThis.fetch = Object.assign(
    async (input: string | URL | Request) => {
      const url = input instanceof Request ? input.url : String(input);
      if (!url.includes("/api/gateway/auth/local-credential")) {
        throw new Error(`未预期请求: ${url}`);
      }
      const index = Math.min(stats.credentialRequests, tokens.length - 1);
      stats.credentialRequests += 1;
      return Response.json({
        code: 0,
        message: "ok",
        data: { token: tokens[index] },
        request_id: "req_token",
      });
    },
    { preconnect: originalFetch.preconnect },
  ) as typeof fetch;
  return stats;
}

describe("Gateway 本地凭据缓存的作废入口", () => {
  test("同一端口默认复用已缓存的凭据 Promise", async () => {
    const stats = installCredentialBackend(["token-a"]);
    expect(await getGatewayToken(PORT)).toBe("token-a");
    expect(await getGatewayToken(PORT)).toBe("token-a");
    expect(stats.credentialRequests).toBe(1);
  });

  test("invalidateGatewayToken 后重新获取凭据而不是复用旧值", async () => {
    const stats = installCredentialBackend(["token-old", "token-new"]);
    expect(await getGatewayToken(PORT)).toBe("token-old");

    // Gateway 重启轮换凭据：没有作废入口时这里会永远返回 token-old。
    invalidateGatewayToken(PORT);
    expect(await getGatewayToken(PORT)).toBe("token-new");
    expect(stats.credentialRequests).toBe(2);
  });

  test("其它端口的缓存不受影响", async () => {
    const stats = installCredentialBackend(["token-port-a"]);
    const otherPort = PORT + 1;
    expect(await getGatewayToken(otherPort)).toBe("token-port-a");

    invalidateGatewayToken(PORT);
    expect(await getGatewayToken(otherPort)).toBe("token-port-a");
    expect(stats.credentialRequests).toBe(1);

    invalidateGatewayToken(otherPort);
  });
});
