import { afterEach, describe, expect, jest, test } from "bun:test";
import { getLLMRequestLogs, listMessages } from "./sessionMessages";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import {
  installSessionCatalogFetchMock,
  unwrapSessionCatalogFetch,
} from "./sessionApiFetchMock";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

function installWindow(port: number): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port), origin: `http://127.0.0.1:${port}` },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    },
  });
}

function installFetchMock(respond: () => Response): void {
  installSessionCatalogFetchMock(() => respond(), { credentialToken: "messages-token" });
}

afterEach(() => {
  jest.useRealTimers();
  unwrapSessionCatalogFetch();
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

describe("会话消息列表 API 的 CursorPage 契约", () => {
  test("合法空页：items 为空数组正常返回", async () => {
    installFetchMock(() => Response.json({
      data: { items: [], next_cursor: null, has_more: false },
      request_id: "req-messages-empty",
    }));

    await expect(listMessages(48_308, "ses_x", "workspace-1")).resolves.toEqual({
      items: [],
      next_cursor: null,
      has_more: false,
    });
  });

  test("items 为数字：响亮失败而不是静默收敛为空页", async () => {
    installFetchMock(() => Response.json({
      data: { items: 5, has_more: true },
      request_id: "req-messages-broken-items",
    }));

    await expect(listMessages(48_309, "ses_x", "workspace-1")).rejects
      .toThrow("会话消息列表响应 items 必须是数组，实际为 number");
  });
});

describe("请求日志读取必须带上限，不能无限等待", () => {
  test("后端一直不响应时按全局默认上限超时并响亮报错", async () => {
    const port = 48_310;
    installWindow(port);
    jest.useFakeTimers();
    let requestSignal: AbortSignal | null = null;
    let markRequestStarted: (() => void) | null = null;
    const requestStarted = new Promise<void>((resolve) => {
      markRequestStarted = resolve;
    });
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input, init] = args;
        const path = new URL(String(input), `http://127.0.0.1:${port}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "llm-log-token" } });
        }
        requestSignal = init?.signal ?? null;
        markRequestStarted!();
        // 模拟后端卡死：只有在被 abort 时才结束，绝不自己返回。
        return await new Promise<Response>((_, reject) => {
          const abort = () => reject(
            requestSignal?.reason ?? new DOMException("aborted", "AbortError"),
          );
          if (requestSignal?.aborted) {
            abort();
            return;
          }
          requestSignal?.addEventListener("abort", abort, { once: true });
        });
      },
      { preconnect: originalFetch.preconnect },
    );

    const pending = getLLMRequestLogs(port, "ses_timeout", "workspace-1");
    // 让凭据与业务请求走到 fetch，然后推进时间越过超时上限。
    await requestStarted;
    jest.advanceTimersByTime(15_000);
    await expect(pending).rejects.toThrow("请求超时");
    // 超时必须是真实的中止，而不是把请求晾在一边继续等。
    expect((requestSignal as AbortSignal | null)?.aborted).toBe(true);
  });
});
