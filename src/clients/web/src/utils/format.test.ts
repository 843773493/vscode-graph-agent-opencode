import { describe, expect, test } from "bun:test";

import { formatByteSize } from "./format";

describe("formatByteSize", () => {
  test("非数字输入返回空串，交由调用方决定占位", () => {
    expect(formatByteSize(null)).toBe("");
    expect(formatByteSize(undefined)).toBe("");
  });

  test("按 B / KB / MB 分段并保留一位小数", () => {
    expect(formatByteSize(0)).toBe("0 B");
    expect(formatByteSize(1023)).toBe("1023 B");
    expect(formatByteSize(1024)).toBe("1.0 KB");
    expect(formatByteSize(1536)).toBe("1.5 KB");
    expect(formatByteSize(1024 * 1024 - 1)).toBe("1024.0 KB");
    expect(formatByteSize(1024 * 1024)).toBe("1.0 MB");
    expect(formatByteSize(3 * 1024 * 1024 + 512 * 1024)).toBe("3.5 MB");
  });
});
