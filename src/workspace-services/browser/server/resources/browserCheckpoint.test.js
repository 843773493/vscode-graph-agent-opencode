import { describe, expect, test } from "bun:test";
import { checkpointRestoreUrl } from "./browserCheckpoint.js";

describe("浏览器检查点恢复地址", () => {
  test("普通SPA优先恢复pushState后的实际地址", () => {
    expect(checkpointRestoreUrl({
      url: "https://example.test/app/current-route",
      requested_url: "https://example.test/app",
      navigation_error: null,
    })).toBe("https://example.test/app/current-route");
  });

  test("Chromium错误页改用用户请求地址重试", () => {
    expect(checkpointRestoreUrl({
      url: "chrome-error://chromewebdata/",
      requested_url: "https://unreachable.example.test/",
      navigation_error: { message: "ERR_CONNECTION_REFUSED" },
    })).toBe("https://unreachable.example.test/");
  });
});
