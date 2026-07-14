export const DEFAULT_VIEWPORT = Object.freeze({ width: 1280, height: 800 });
export const NAVIGATION_TIMEOUT_MS = 30000;
export const TOOL_TIMEOUT_MS = 10000;

export function browserLaunchArgs() {
  const args = ["--disable-dev-shm-usage"];
  if (process.platform === "linux") {
    args.push("--no-sandbox");
  }
  return args;
}

function isSerializablePrimitive(value) {
  return value === null || ["string", "number", "boolean"].includes(typeof value);
}

export async function withTimeout(promise, timeoutMs, label) {
  let timeoutId;
  const timeout = new Promise((_, reject) => {
    timeoutId = setTimeout(() => {
      reject(new Error(`${label} 超时: ${timeoutMs}ms`));
    }, timeoutMs);
  });
  try {
    return await Promise.race([promise, timeout]);
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
