import { restoreGlobalDescriptor } from "../../tests/testGlobals";

interface ComposerTestIntervalController {
  setInterval: (callback: () => void, delay?: number) => number;
  clearInterval: (timerId: number) => void;
}

interface ComposerTestGlobalsOptions {
  port?: number;
  intervalController?: ComposerTestIntervalController;
}

const OVERLAY_CONSTRUCTOR_NAMES = ["Element", "Node", "HTMLElement"] as const;

class OverlayElementStub {}
class OverlayNodeStub {}

const OVERLAY_CONSTRUCTORS = {
  Element: OverlayElementStub,
  Node: OverlayNodeStub,
  HTMLElement: OverlayElementStub,
};

const RESTORED_GLOBALS = [
  "window",
  "BroadcastChannel",
  "document",
  "FileReader",
  ...OVERLAY_CONSTRUCTOR_NAMES,
] as const;

/** Composer hook 测试共用的最小浏览器环境；返回函数按安装前描述符逐项还原。 */
export function installComposerTestGlobals(
  options: ComposerTestGlobalsOptions = {},
): () => void {
  const originalDescriptors = RESTORED_GLOBALS.map((name) => [
    name,
    Object.getOwnPropertyDescriptor(globalThis, name),
  ] as const);
  const storage = new Map<string, string>();
  const windowListeners = new Map<string, Set<(event: unknown) => void>>();
  const port = options.port ?? 8011;

  // Floating UI 同时检查 window.Element 和全局 Element，二者必须指向同一个构造器。
  for (const [name, value] of Object.entries(OVERLAY_CONSTRUCTORS)) {
    Object.defineProperty(globalThis, name, { configurable: true, value });
  }
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      localStorage: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => {
          storage.set(key, value);
        },
        removeItem: (key: string) => {
          storage.delete(key);
        },
      },
      ...OVERLAY_CONSTRUCTORS,
      location: {
        port: String(port),
        origin: `http://127.0.0.1:${port}`,
      },
      setInterval: options.intervalController?.setInterval
        ?? globalThis.setInterval.bind(globalThis),
      clearInterval: options.intervalController?.clearInterval
        ?? globalThis.clearInterval.bind(globalThis),
      addEventListener: (type: string, handler: (event: unknown) => void) => {
        const listeners = windowListeners.get(type) ?? new Set();
        listeners.add(handler);
        windowListeners.set(type, listeners);
      },
      removeEventListener: (type: string, handler: (event: unknown) => void) => {
        windowListeners.get(type)?.delete(handler);
      },
    },
  });
  Object.defineProperty(globalThis, "BroadcastChannel", {
    configurable: true,
    value: class {
      addEventListener() {}
      removeEventListener() {}
      close() {}
    },
  });

  return () => {
    for (const [name, descriptor] of originalDescriptors) {
      restoreGlobalDescriptor(name, descriptor);
    }
  };
}
