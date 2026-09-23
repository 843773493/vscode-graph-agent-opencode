/**
 * 可取消等待的唯一实现。api/http.ts 的请求屏障与 runtime/jsonResponseParser.ts
 * 的响应体解析都需要「等待一个 Promise，但外部 signal 一中止就立即以同一原因
 * 拒绝」，此前两处各写了一份逐字相同的实现。收敛到这里，行为只有一份。
 */

export function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("请求已取消", "AbortError");
}

export async function awaitWithAbort<T>(
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
