import { describe, expect, test } from "bun:test";
import { BrowserSession } from "./browserSession.js";

function session() {
  let persistCount = 0;
  const manager = {
    attachUrl: (id) => `http://browser.test/?browserId=${id}`,
    persist: async () => {
      persistCount += 1;
    },
  };
  return {
    browser: new BrowserSession({
      manager,
      record: {
        browser_id: "browser_lock_test",
        session_id: "session_lock_test",
        status: "running",
      },
    }),
    persistCount: () => persistCount,
  };
}

describe("浏览器 AI 操作锁", () => {
  test("默认共享，用户锁定后拒绝 AI 操作", async () => {
    const fixture = session();
    expect(fixture.browser.snapshot().agent_access_locked).toBe(false);

    const ownerId = "user_lock_test_owner";
    const locked = await fixture.browser.setAgentAccessLocked(true, ownerId);
    expect(locked.agent_access_locked).toBe(true);
    expect(locked.agent_lock_owner_id).toBe(ownerId);
    expect(fixture.persistCount()).toBe(1);
    expect(() => fixture.browser.assertAgentAccessAllowed()).toThrow(
      "用户锁定了浏览器，你暂时不能操作这个页面",
    );

    await expect(
      fixture.browser.setAgentAccessLocked(false, "user_other_person"),
    ).rejects.toThrow("另一位用户锁定了 AI 操作");
    await fixture.browser.setAgentAccessLocked(false, ownerId);
    expect(() => fixture.browser.assertAgentAccessAllowed()).not.toThrow();
  });
});
