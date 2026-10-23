import { describe, expect, test } from "bun:test";
import { waitForBootstrapRetry } from "./workspaceBootstrapLoader";

describe("waitForBootstrapRetry", () => {
  test("等待期间被中止时立刻结束并抛出，不再空等完整退避时长", async () => {
    const controller = new AbortController();
    // 退避时长取一个大于本次断言时间窗的值：若等待不可中断，测试会明显变慢。
    const pending = waitForBootstrapRetry(3_000, controller.signal);
    const startedAt = Date.now();
    controller.abort();
    await expect(pending).rejects.toThrow();
    expect(Date.now() - startedAt).toBeLessThan(1_500);
  });

  test("signal 已中止时不再等待，直接抛出", async () => {
    const controller = new AbortController();
    controller.abort();
    const startedAt = Date.now();
    await expect(waitForBootstrapRetry(3_000, controller.signal)).rejects.toThrow();
    expect(Date.now() - startedAt).toBeLessThan(1_500);
  });

  test("未中止时等待到时后正常返回", async () => {
    const controller = new AbortController();
    await waitForBootstrapRetry(0, controller.signal);
    expect(controller.signal.aborted).toBe(false);
  });
});
