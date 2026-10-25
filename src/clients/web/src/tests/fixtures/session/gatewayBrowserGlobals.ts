import { invalidateGatewayToken } from "../../../api/http";

/** 安装带计时器的 Gateway window 桩，并作废该端口缓存凭据。 */
export function installGatewayTestWindow(port: number): void {
  invalidateGatewayToken(port);
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      setInterval: globalThis.setInterval.bind(globalThis),
      clearInterval: globalThis.clearInterval.bind(globalThis),
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
}

/** 安装只读的可见性 document 桩。 */
export function installVisibleTestDocument(): void {
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      visibilityState: "visible",
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
}
