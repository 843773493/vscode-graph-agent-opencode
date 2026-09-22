interface JsonParseSuccess {
  ok: true;
  value: unknown;
}

interface JsonParseFailure {
  ok: false;
  message: string;
}

type JsonParseResult = JsonParseSuccess | JsonParseFailure;

/**
 * JSON 解析失败时携带已读取的正文前缀。引擎自带的 SyntaxError 只说明语法错误，
 * 不含路径与响应体形态，调用方无法据此判断「Gateway 未启动返回了 HTML」还是
 * 「响应体为空」；上层（api/http.ts）用它组合可诊断的中文错误。
 */
export class JsonResponseBodyError extends Error {
  constructor(readonly bodyPrefix: string) {
    super("响应体不是 JSON");
    this.name = "JsonResponseBodyError";
  }
}

/** 诊断只需正文开头：巨型载荷不得整段进入错误文案。 */
const DIAGNOSTIC_BODY_PREFIX_LIMIT = 512;

function textPrefix(text: string): string {
  return text.length > DIAGNOSTIC_BODY_PREFIX_LIMIT
    ? text.slice(0, DIAGNOSTIC_BODY_PREFIX_LIMIT)
    : text;
}

function bufferPrefix(buffer: ArrayBuffer): string {
  return new TextDecoder().decode(buffer.slice(0, DIAGNOSTIC_BODY_PREFIX_LIMIT));
}

function parseJsonBuffer<T>(buffer: ArrayBuffer): T {
  return JSON.parse(new TextDecoder().decode(buffer)) as T;
}

function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("请求已取消", "AbortError");
}

async function awaitWithAbort<T>(
  pending: Promise<T>,
  signal: AbortSignal | undefined,
): Promise<T> {
  if (!signal) return await pending;
  if (signal.aborted) throw abortReason(signal);

  return await new Promise<T>((resolve, reject) => {
    let settled = false;
    const cleanup = () => signal.removeEventListener("abort", onAbort);
    const onAbort = () => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(abortReason(signal));
    };
    signal.addEventListener("abort", onAbort, { once: true });
    pending.then(
      (value) => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(value);
      },
      (error: unknown) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(error);
      },
    );
  });
}

export async function parseJsonResponse<T>(
  response: Response,
  workerThresholdBytes: number | null,
  signal?: AbortSignal,
): Promise<T> {
  if (workerThresholdBytes === null) {
    const text = await awaitWithAbort(response.text(), signal);
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new JsonResponseBodyError(textPrefix(text));
    }
  }
  if (!Number.isSafeInteger(workerThresholdBytes) || workerThresholdBytes < 1) {
    throw new Error(
      `JSON Worker 解析阈值必须是正整数: ${workerThresholdBytes}`,
    );
  }
  const buffer = await awaitWithAbort(response.arrayBuffer(), signal);
  if (buffer.byteLength < workerThresholdBytes) {
    if (signal?.aborted) throw abortReason(signal);
    try {
      return parseJsonBuffer<T>(buffer);
    } catch {
      throw new JsonResponseBodyError(bufferPrefix(buffer));
    }
  }

  if (signal?.aborted) throw abortReason(signal);

  // buffer 会以 transfer 交给 Worker 而失效，诊断前缀必须在移交前取出。
  const prefix = bufferPrefix(buffer);
  const worker = new Worker(
    new URL("./jsonParseWorker.ts", import.meta.url),
    { type: "module", name: "boxteam-json-parser" },
  );
  return await new Promise<T>((resolve, reject) => {
    let settled = false;
    const finish = () => {
      if (settled) return false;
      settled = true;
      signal?.removeEventListener("abort", onAbort);
      worker.terminate();
      return true;
    };
    const onAbort = () => {
      if (!finish()) return;
      reject(abortReason(signal!));
    };
    worker.onmessage = (event: MessageEvent<JsonParseResult>) => {
      if (!finish()) return;
      if (event.data.ok) {
        resolve(event.data.value as T);
        return;
      }
      reject(new JsonResponseBodyError(prefix));
    };
    worker.onerror = (event) => {
      if (!finish()) return;
      reject(new Error(`JSON Worker 执行失败: ${event.message}`));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    if (signal?.aborted) {
      onAbort();
      return;
    }
    worker.postMessage(buffer, [buffer]);
  });
}
