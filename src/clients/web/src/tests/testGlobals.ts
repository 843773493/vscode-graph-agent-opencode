/**
 * 浏览器全局桩的还原入口。
 *
 * 测试给 `globalThis` 装上 `window` / `document` / `Worker` 等桩后，必须在
 * `afterEach` 里还原成安装前的形态：有原描述符就写回，测试环境本来没有该全局
 * 就删除。bun 的 `globalThis` 是进程级的，漏还原会污染同一进程后续所有测试文件。
 *
 * 这段「有则写回、无则删除」逐字重复在 20+ 个测试文件里，统一收敛到此处。
 */
export function restoreGlobalDescriptor(
  name: string,
  descriptor: PropertyDescriptor | undefined,
): void {
  if (descriptor) {
    Object.defineProperty(globalThis, name, descriptor);
  } else {
    Reflect.deleteProperty(globalThis, name);
  }
}

/** 安装只提供计时器的 window 桩，并返回与本次安装配对的还原函数。 */
export function installWindowWithTimers(options: {
  port?: string;
  origin?: string;
} = {}): () => void {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const location = options.port === undefined && options.origin === undefined
    ? undefined
    : { ...options };

  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      ...(location ? { location } : {}),
      setInterval: globalThis.setInterval.bind(globalThis),
      clearInterval: globalThis.clearInterval.bind(globalThis),
    },
  });

  return () => restoreGlobalDescriptor("window", originalWindow);
}
