import { describe, expect, test } from "bun:test";

import {
  encodeBrowserClientMessage,
  encodeBrowserServerMessage,
  parseBrowserClientMessage,
} from "./messages.js";

describe("Browser Protobuf JSON adapter", () => {
  test("保留 attach 与 detach 的资源标识", () => {
    const attach = parseBrowserClientMessage(JSON.stringify({
      type: "attach",
      browserId: "browser_123",
      participantId: "user_12345678",
    }));
    const detach = JSON.parse(encodeBrowserClientMessage({
      type: "detach",
      browserId: "browser_123",
    }));

    expect(attach.browserId).toBe("browser_123");
    expect(detach).toEqual({ type: "detach", browserId: "browser_123" });
  });

  test("通过 Struct 保留未迁移的服务端状态字段", () => {
    const message = {
      type: "state",
      browserId: "browser_123",
      state: { status: "running", pages: [{ page_id: "page_123" }] },
    };

    expect(JSON.parse(encodeBrowserServerMessage(message))).toEqual(message);
  });
});
