import { afterEach, describe, expect, test } from "bun:test";
import { listMessages } from "./sessionMessages";

const originalFetch = globalThis.fetch;

/** mock fetch：第一次返回 Gateway 本地凭据，之后返回给定的 API 响应。 */
function installFetchMock(respond: () => Response): void {
  let count = 0;
  globalThis.fetch = Object.assign(
    async () => {
      count += 1;
      if (count === 1) {
        return Response.json({ data: { token: "messages-token" } });
      }
      return respond();
    },
    { preconnect: originalFetch.preconnect },
  );
}

afterEach(() => {
  globalThis.fetch = originalFetch;
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
