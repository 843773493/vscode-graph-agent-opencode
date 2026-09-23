import { afterAll, afterEach, describe, expect, mock, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { AppState } from "../../types/frontend";

/**
 * 租约占用特判的回归契约。
 *
 * GatewayUserAccessMenu 里本地 gatewayUserErrorMessage 会先把 HttpRequestError 的
 * user_lease_occupied 转成人话，再兜底到共享的 utils/errorMessage。收敛内联样板时
 * 一旦把调用点误接到共享实现（此处曾经真实发生），用户看到的就会退化成裸后端文本，
 * 而组件仍然「工作」，不会被其它用例发现。本文件同时锁住两件事：
 *
 * 1. 接管被占用的用户时展示「用户正在被占用（客户端）」；
 * 2. 组件源码里所有本地包装调用点都在，兜底只剩共享实现的唯一引用。
 */

const realHooks = await import("../../hooks");

const PORT = 49_401;
const state = {
  apiPort: PORT,
  gatewayUserAccess: { kind: "user", user_id: "u1" },
  status: null,
} as unknown as AppState;

// 只替身 useAppState；spread 真实模块命名空间会重新触发求值，afterAll 重新注册
// 真实实现即可还原（与 AppErrorChannel.test.tsx 同形，探针已验证有效，不构成泄漏）。
mock.module("../../hooks", () => ({
  ...realHooks,
  useAppState: () => ({
    state,
    refreshGatewayState: async () => {},
    setStatus: () => {},
  }),
}));

afterAll(() => {
  mock.module("../../hooks", () => realHooks);
});

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
// @floating-ui 用 `instanceof window.Element` 判定引用，而 @floating-ui/utils/dom
// 直接引用裸全局 Element。测试环境两者都没有，这里只补最小构造器；document 依旧
// 不存在，AnchoredOverlay 因而走「无 document 直接内联子节点」的分支。
class OverlayElementStub {}
class OverlayNodeStub {}
const OVERLAY_CONSTRUCTORS = {
  Element: OverlayElementStub,
  Node: OverlayNodeStub,
  HTMLElement: OverlayElementStub,
};
const OVERLAY_CONSTRUCTOR_NAMES = ["Element", "Node", "HTMLElement"] as const;

function installWindow(): void {
  for (const [name, value] of Object.entries(OVERLAY_CONSTRUCTORS)) {
    Object.defineProperty(globalThis, name, { configurable: true, value });
  }
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      ...OVERLAY_CONSTRUCTORS,
      location: { port: String(PORT), origin: "http://127.0.0.1:8011" },
      setInterval: globalThis.setInterval.bind(globalThis),
      clearInterval: globalThis.clearInterval.bind(globalThis),
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
}

function envelope(data: unknown): Response {
  return Response.json(
    { code: 0, message: "ok", request_id: "req", data },
    { status: 200 },
  );
}

/** 用户列表正常返回；接管请求固定回 409 + user_lease_occupied。 */
function installFetch(): void {
  globalThis.fetch = Object.assign(
    async (input: RequestInfo | URL): Promise<Response> => {
      const url = new URL(input instanceof Request ? input.url : String(input), "http://127.0.0.1");
      if (url.pathname === "/api/gateway/auth/local-credential") return envelope({ token: "t" });
      if (url.pathname === "/api/gateway/users/current") return envelope({ kind: "guest", user_id: null });
      if (url.pathname === "/api/gateway/users") {
        return envelope({
          items: [{
            user_id: "u2",
            display_name: "被占用用户",
            created_at: "2026-01-01T00:00:00Z",
            lease: { occupied: true, client_label: "另一台电脑" },
          }],
          active_user_id: null,
        });
      }
      if (url.pathname === "/api/gateway/users/u2/takeover") {
        return Response.json(
          { detail: { code: "user_lease_occupied", client_label: "另一台电脑" } },
          { status: 409, statusText: "Conflict" },
        );
      }
      throw new Error("测试收到未声明请求: " + url.pathname);
    },
    { preconnect: originalFetch.preconnect },
  ) as typeof fetch;
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const name of OVERLAY_CONSTRUCTOR_NAMES) {
    Reflect.deleteProperty(globalThis, name);
  }
  if (originalWindowDescriptor) Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  else Reflect.deleteProperty(globalThis, "window");
});

function buttonByText(renderer: ReactTestRenderer, text: string) {
  return renderer.root.find((n) => n.type === "button" && String(n.children.join("")) === text);
}

function alertText(renderer: ReactTestRenderer): string | null {
  const nodes = renderer.root.findAll((n) => n.props?.role === "alert");
  return nodes.length ? String(nodes[0].children.join("")) : null;
}

describe("Gateway 用户访问菜单的租约占用特判", () => {
  test("接管被占用的用户时展示本地特判文案而不是裸后端文本", async () => {
    installWindow();
    installFetch();
    const { default: GatewayUserAccessMenu } = await import("./GatewayUserAccessMenu");
    let renderer!: ReactTestRenderer;
    await act(async () => {
      renderer = create(<GatewayUserAccessMenu />);
    });
    await act(async () => {
      renderer.root
        .find((n) => n.type === "button" && n.props?.["aria-label"] === "用户视图")
        .props.onClick();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    await act(async () => {
      buttonByText(renderer, "接管").props.onClick();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    expect(alertText(renderer)).toBe("用户正在被占用（另一台电脑）");
    expect(alertText(renderer)).not.toContain("请求失败 409");
    await act(async () => {
      renderer.unmount();
    });
  });

  test("组件源码里本地包装调用点齐全，兜底只剩共享实现的唯一引用", async () => {
    const source = await Bun.file(new URL("./GatewayUserAccessMenu.tsx", import.meta.url)).text();
    expect(source).toContain("function gatewayUserErrorMessage");
    expect(source).toContain('detail.code === "user_lease_occupied"');
    expect(source).toContain("${gatewayUserErrorMessage(refreshError)}");
    expect(source).toContain("${gatewayUserErrorMessage(cause)}");
    expect(source).not.toContain("${errorMessage(");
    expect(source.match(/\berrorMessage\(/g)?.length).toBe(1);
  });
});
