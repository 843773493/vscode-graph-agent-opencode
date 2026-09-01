export const DEFAULT_VIEWPORT = Object.freeze({ width: 1280, height: 800 });
export const NAVIGATION_TIMEOUT_MS = 30000;
export const TOOL_TIMEOUT_MS = 10000;

export class BrowserToolTimeoutError extends Error {
  constructor(label, timeoutMs) {
    super(`${label} 超时: ${timeoutMs}ms；浏览器页面已重置，可重试`);
    this.name = "BrowserToolTimeoutError";
    this.code = "browser_tool_timeout";
    this.timeout_ms = timeoutMs;
    this.retryable = true;
    this.recovery = "page_reset";
  }
}

export function browserLaunchArgs() {
  const args = ["--disable-dev-shm-usage"];
  if (process.platform === "linux") {
    args.push("--no-sandbox");
  }
  return args;
}

export function browserLaunchOptions() {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH?.trim();
  return {
    headless: true,
    handleSIGINT: false,
    handleSIGTERM: false,
    handleSIGHUP: false,
    args: browserLaunchArgs(),
    ...(executablePath ? { executablePath } : {}),
  };
}

function isSerializablePrimitive(value) {
  return value === null || ["string", "number", "boolean"].includes(typeof value);
}

export async function withTimeout(promise, timeoutMs, label, { onTimeout } = {}) {
  let timeoutId;
  let timedOut = false;
  const guardedPromise = Promise.resolve(promise).catch((error) => {
    // 超时恢复可能会导航同一个页面；Playwright 原始动作因此产生的
    // navigation/page 错误不能覆盖统一的可重试超时错误。
    if (timedOut) return new Promise(() => undefined);
    throw error;
  });
  const timeout = new Promise((_, reject) => {
    timeoutId = setTimeout(() => {
      timedOut = true;
      const error = new BrowserToolTimeoutError(label, timeoutMs);
      try {
        // 超时是当前调用的终态；页面 reset 在后台有独立的队列屏障。
        // 这里不能等待导航/新 Page，否则一个 15 秒工具会被恢复动作再次
        // 阻塞，Agent 既收不到可重试结果，后续短操作也无法进入队列。
        const recovery = onTimeout?.(error);
        if (recovery) {
          void Promise.resolve(recovery).catch((recoveryError) => {
            error.recovery = "page_reset_failed";
            error.recovery_error = recoveryError instanceof Error
              ? recoveryError.message
              : String(recoveryError);
            error.message = `${error.message}；页面重置失败: ${error.recovery_error}`;
          });
        }
      } catch (recoveryError) {
        error.recovery = "page_reset_failed";
        error.recovery_error = recoveryError instanceof Error
          ? recoveryError.message
          : String(recoveryError);
        error.message = `${error.message}；页面重置失败: ${error.recovery_error}`;
      }
      reject(error);
    }, timeoutMs);
  });
  try {
    return await Promise.race([guardedPromise, timeout]);
  } finally {
    clearTimeout(timeoutId);
  }
}

export function normalizeToolResult(value) {
  if (isSerializablePrimitive(value)) {
    return value;
  }
  if (value === undefined) {
    return null;
  }
  if (Buffer.isBuffer(value)) {
    return {
      type: "buffer",
      byteLength: value.byteLength,
      base64: value.toString("base64"),
    };
  }
  try {
    return JSON.parse(JSON.stringify(value));
  } catch {
    return String(value);
  }
}
