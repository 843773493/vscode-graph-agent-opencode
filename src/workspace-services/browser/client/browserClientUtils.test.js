import { describe, expect, test } from "bun:test";
import { shortUrlLabel } from "./browserClientUtils.js";

describe("浏览器地址摘要", () => {
  test("内部错误页不会显示成 null/", () => {
    expect(shortUrlLabel("chrome-error://chromewebdata/")).toBe(
      "chrome-error://chromewebdata/",
    );
  });
});
