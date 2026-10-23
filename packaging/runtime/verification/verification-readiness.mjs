import { setTimeout as sleep } from "node:timers/promises";

export async function waitForInstalledGateway({ child, url, headers, timeoutMs }) {
  const deadlineMonotonicMs = performance.now() + timeoutMs;
  const assertLauncherRunning = () => {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(
        `安装版 Launcher 提前退出: exit=${String(child.exitCode)} signal=${String(child.signalCode)}`,
      );
    }
  };
  let lastError = null;
  while (performance.now() < deadlineMonotonicMs) {
    assertLauncherRunning();
    const remainingMs = deadlineMonotonicMs - performance.now();
    if (remainingMs <= 0) break;
    let ready = false;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error("健康请求超过就绪期限")), Math.ceil(remainingMs));
    try {
      const response = await fetch(url, {
        headers,
        signal: controller.signal,
      });
      // 仅观察状态码；未结束的健康正文不应延长等待或占用连接。
      await response.body?.cancel();
      if (performance.now() >= deadlineMonotonicMs) lastError = new Error("健康响应超过就绪期限");
      else if (response.ok) ready = true;
      else lastError = new Error(`HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    } finally {
      clearTimeout(timer);
      controller.abort();
    }
    assertLauncherRunning();
    if (ready) return;
    const retryDelayMs = Math.min(250, Math.ceil(deadlineMonotonicMs - performance.now()));
    if (retryDelayMs > 0) await sleep(retryDelayMs);
  }
  throw new Error(
    `安装版 Gateway 未就绪: ${
      lastError instanceof Error ? lastError.message : String(lastError)
    }`,
    { cause: lastError },
  );
}
