interface JsonParseSuccess {
  ok: true;
  value: unknown;
}

interface JsonParseFailure {
  ok: false;
  message: string;
}

type JsonParseResult = JsonParseSuccess | JsonParseFailure;

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
    return await awaitWithAbort(response.json() as Promise<T>, signal);
  }
  if (!Number.isSafeInteger(workerThresholdBytes) || workerThresholdBytes < 1) {
    throw new Error(
      `JSON Worker 解析阈值必须是正整数: ${workerThresholdBytes}`,
    );
  }
  const buffer = await awaitWithAbort(response.arrayBuffer(), signal);
  if (buffer.byteLength < workerThresholdBytes) {
    if (signal?.aborted) throw abortReason(signal);
    return parseJsonBuffer<T>(buffer);
  }

  if (signal?.aborted) throw abortReason(signal);

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
      reject(new Error(`JSON Worker 解析失败: ${event.data.message}`));
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
