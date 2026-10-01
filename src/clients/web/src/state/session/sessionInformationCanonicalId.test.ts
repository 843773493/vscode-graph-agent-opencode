import { describe, expect, test } from "bun:test";
import { extractSessionIdFromClipboardText, isSessionId } from "./sessionInformation";

// 后端 canonical（app/core/session_catalog_store.py:validate_session_id）：
// ses_ + 32 位小写 hex + UUIDv7 version/variant 位。
const CANONICAL_ID = "ses_0190f2a3b4c570008000000000000001";

describe("前端会话 ID 校验与后端 UUIDv7 口径一致", () => {
  test("复制出的 canonical ses_ 会话 ID 能原样通过剪贴板解析", () => {
    // 真实链路：右键「复制 ID」写入 canonical ID，粘贴移动时按同一口径读回。
    expect(extractSessionIdFromClipboardText(CANONICAL_ID)).toBe(CANONICAL_ID);

    const text = JSON.stringify({
      kind: "session_diagnostic_snapshot",
      session: { id: CANONICAL_ID },
    });
    expect(extractSessionIdFromClipboardText(text)).toBe(CANONICAL_ID);
  });

  test("校验器只认 UUIDv7 位 profile，拒绝 v4 与非法 variant", () => {
    expect(isSessionId(CANONICAL_ID)).toBe(true);
    // 同一形态但 version 位仍是 v4：必须拒绝（这正是本次缺陷的根因）。
    expect(isSessionId("ses_0190f2a3b4c540008000000000000001")).toBe(false);
    // variant 位非法（"c" 不在 "89ab"）：必须拒绝。
    expect(isSessionId("ses_0190f2a3b4c57000c000000000000001")).toBe(false);
    // 形态非 canonical：不是 32 位 hex。
    expect(isSessionId("ses_direct_123")).toBe(false);
  });
});
