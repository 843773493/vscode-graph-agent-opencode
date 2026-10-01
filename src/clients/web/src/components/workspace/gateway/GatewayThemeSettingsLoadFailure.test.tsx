import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { WebUiSettings } from "../../../types/backend";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";
import { createDefaultWebUiSettings } from "../../../state/uiSettings/preferences";
import {
  installGatewayFetch,
  restoreSessionHookGlobals,
} from "../../../hooks/session/sessionHookTestFixtures";
import GatewayThemeSettings from "./GatewayThemeSettings";

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

afterEach(() => {
  restoreSessionHookGlobals();
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

function installWindow(port: number): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port), origin: `http://127.0.0.1:${port}` },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function textOf(renderer: ReactTestRenderer): string {
  const collect = (value: unknown, into: string[]): void => {
    if (typeof value === "string" || typeof value === "number") {
      into.push(String(value));
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) collect(item, into);
      return;
    }
    if (value && typeof value === "object" && "props" in value) {
      collect((value as { props: { children?: unknown } }).props.children, into);
    }
  };
  const parts: string[] = [];
  collect(renderer.toJSON(), parts);
  return parts.join(" ");
}

/**
 * 主题配置首读失败的可见终态。
 *
 * 面板在 `catalog` 为空时提前 return 一句「正在读取 Gateway 主题配置…」。首读失败
 * 时 catalog 永远为空：错误只写进组件状态，却因为走不到主渲染分支而从不显示，
 * 用户看到的是永久转圈的空态，没有任何失败原因，也没有重试入口。
 */
test("主题配置首读失败必须显示可见错误与重试入口，而不是永久停在加载态", async () => {
  const PORT = 49_763;
  installWindow(PORT);
  let requestCount = 0;
  installGatewayFetch(({ path }) => {
    if (path === "/api/gateway/themes" || path === "/api/gateway/ui-assets") {
      requestCount += 1;
      return Response.json(
        { detail: "Gateway 主题服务不可达" },
        { status: 503, headers: { "content-type": "application/json" } },
      );
    }
    return undefined;
  }, { token: "theme-fail-token" });

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <GatewayThemeSettings
        apiPort={PORT}
        settings={createDefaultWebUiSettings() as WebUiSettings}
        onUpdateSettings={async () => undefined}
      />,
    );
  });
  await flush();

  expect(requestCount).toBeGreaterThan(0);
  const text = textOf(renderer);
  // 失败必须是可见错误，而不是永久加载态。
  expect(renderer.root.findAllByProps({ role: "alert" }).length).toBeGreaterThan(0);
  expect(text).not.toContain("正在读取 Gateway 主题配置");

  // 重试入口必须存在，点一下会重新发起读取。
  const retry = renderer.root.findAllByType("button").find((button) => {
    const parts: string[] = [];
    const collect = (value: unknown): void => {
      if (typeof value === "string" || typeof value === "number") {
        parts.push(String(value));
        return;
      }
      if (Array.isArray(value)) {
        for (const item of value) collect(item);
        return;
      }
      if (value && typeof value === "object" && "props" in value) {
        collect((value as { props: { children?: unknown } }).props.children);
      }
    };
    collect(button.props.children);
    return parts.join("").includes("重新读取");
  });
  expect(retry).toBeDefined();
  const before = requestCount;
  await act(async () => {
    retry!.props.onClick();
    await Promise.resolve();
  });
  await flush();
  expect(requestCount).toBeGreaterThan(before);
  renderer.unmount();
});
