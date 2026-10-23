import { existsSync, readFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

export async function waitForHttpOk({
  url,
  label,
  processHandle = null,
  headers = undefined,
  timeoutMs = 90_000,
}) {
  const deadlineMonotonicMs = performance.now() + timeoutMs;
  const assertProcessRunning = () => {
    if (
      processHandle !== null &&
      (processHandle.exitCode !== null || processHandle.signalCode !== null)
    ) {
      throw new Error(
        `${label}就绪前进程退出: exit=${String(processHandle.exitCode)} ` +
          `signal=${String(processHandle.signalCode)}`,
      );
    }
  };
  let lastError = null;
  while (performance.now() < deadlineMonotonicMs) {
    assertProcessRunning();
    const remainingMs = deadlineMonotonicMs - performance.now();
    if (remainingMs <= 0) break;
    let ready = false;
    const requestController = new AbortController();
    const requestTimer = setTimeout(() => {
      requestController.abort(new Error("健康请求超过就绪期限"));
    }, Math.ceil(remainingMs));
    try {
      const response = await fetch(url, {
        headers,
        signal: requestController.signal,
      });
      // 探针只观察状态码，正文不属于就绪合同；释放可能持续输出的响应。
      await response.body?.cancel();
      if (performance.now() >= deadlineMonotonicMs) {
        lastError = new Error("健康响应超过就绪期限");
      } else if (response.ok) {
        ready = true;
      } else {
        lastError = new Error(`HTTP ${response.status} ${response.statusText}`);
      }
    } catch (error) {
      lastError = error;
    } finally {
      clearTimeout(requestTimer);
      // 显式结束本次传输；部分运行时的body.cancel只停止流消费，尚未释放socket。
      requestController.abort();
    }
    assertProcessRunning();
    if (ready) {
      process.stdout.write(`[dev] ${label} ready: ${url}\n`);
      return;
    }
    const retryDelayMs = Math.min(250, Math.ceil(deadlineMonotonicMs - performance.now()));
    if (retryDelayMs > 0) await sleep(retryDelayMs);
  }
  throw new Error(
    `${label}在 ${timeoutMs}ms 内未就绪: ${url}: ${
      lastError instanceof Error ? lastError.message : String(lastError)
    }`,
    { cause: lastError },
  );
}

export async function waitForDetachedReady({ child, readyFile, logPath, timeoutMs = 120_000 }) {
  const deadlineMonotonicMs = performance.now() + timeoutMs;
  while (performance.now() < deadlineMonotonicMs) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error(
        `detached dev manager 就绪前退出: exit=${String(child.exitCode)} ` +
          `signal=${String(child.signalCode)}，日志: ${logPath}`,
      );
    }
    if (existsSync(readyFile)) {
      try {
        const payload = JSON.parse(readFileSync(readyFile, "utf8"));
        if (payload.pid === child.pid) return;
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
      }
    }
    const retryDelayMs = Math.min(250, Math.ceil(deadlineMonotonicMs - performance.now()));
    if (retryDelayMs > 0) await sleep(retryDelayMs);
  }
  throw new Error(
    `detached dev manager 在 ${timeoutMs}ms 内未就绪，日志: ${logPath}`,
  );
}
