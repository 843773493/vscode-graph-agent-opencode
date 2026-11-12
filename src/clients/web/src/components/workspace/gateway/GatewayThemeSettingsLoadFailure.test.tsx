import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { restoreGlobalDescriptor } from "../../../tests/testGlobals";
import { DEFAULT_BACKGROUND } from "../../../theme/theme";
import {
  installGatewayFetch,
  restoreGatewayTestEnvironment,
} from "../../../tests/fixtures/session/gatewayEnvironment";
import GatewayThemeSettings from "./GatewayThemeSettings";

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

afterEach(() => {
  restoreGatewayTestEnvironment();
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

/** 背景资源首读失败仍显示原因和重试入口。 */
test("背景资源首读失败必须显示可见错误与重试入口", async () => {
  const PORT = 49_763;
  installWindow(PORT);
  let requestCount = 0;
  installGatewayFetch(({ path }) => {
    if (path === "/api/gateway/ui-assets") {
      requestCount += 1;
      return Response.json(
        { detail: "Gateway 背景资源服务不可达" },
        { status: 503, headers: { "content-type": "application/json" } },
      );
    }
    return undefined;
  }, { token: "theme-fail-token" });

  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <GatewayThemeSettings onSelectColorTheme={async () => undefined}
        apiPort={PORT}
        background={DEFAULT_BACKGROUND}
        onUpdateBackground={async () => undefined}
      />,
    );
  });
  await flush();

  expect(requestCount).toBeGreaterThan(0);
  const text = textOf(renderer);
  // 失败必须是可见错误，而不是永久加载态。
  expect(renderer.root.findAllByProps({ role: "alert" }).length).toBeGreaterThan(0);
  expect(text).not.toContain("正在读取背景图片");

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
