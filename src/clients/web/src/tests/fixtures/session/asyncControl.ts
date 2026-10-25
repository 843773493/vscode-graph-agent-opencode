/** 等待一轮 effect 落地。 */
export async function flushReactEffects(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

/**
 * 挂起一个请求直到显式释放，用于验收「请求在途期间卸载或切换会话」的并发
 * 场景（例如 SSE 长连接、后端无响应）。不释放就永不落地。
 */
export function createDeferredResult<T>(): {
  promise: Promise<T>;
  release: (value: T) => void;
} {
  let release!: (value: T) => void;
  const promise = new Promise<T>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}
