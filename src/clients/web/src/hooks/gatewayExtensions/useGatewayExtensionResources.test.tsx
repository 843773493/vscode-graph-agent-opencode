import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useGatewayExtensionResources } from "./useGatewayExtensionResources";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

function installWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: "49507" },
      setInterval: globalThis.setInterval.bind(globalThis),
      clearInterval: globalThis.clearInterval.bind(globalThis),
    },
  });
}

function apiResponse(data: unknown): Response {
  return new Response(JSON.stringify({
    code: 0,
    message: "ok",
    data,
    request_id: "request-extension-resource-test",
  }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
});

describe("useGatewayExtensionResources 请求协调", () => {
  test("扩展窗口切换初始资源时不重复读取资源列表", async () => {
    installWindow();
    let resourceRequests = 0;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const path = new URL(url).pathname;
      if (path === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "local-extension-resource-test-token" });
      }
      if (path === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (path === "/api/gateway/resources") {
        resourceRequests += 1;
        return apiResponse({ items: [], errors: [] });
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    function Harness({ initialResourceKey }: { initialResourceKey: string | null }): React.ReactNode {
      useGatewayExtensionResources({
        apiPort: 49_507,
        initialResourceKey,
        enabled: true,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness initialResourceKey="resource-a" />);
      await new Promise<void>((resolve) => setTimeout(resolve, 20));
    });
    expect(resourceRequests).toBe(1);

    await act(async () => {
      renderer!.update(<Harness initialResourceKey="resource-b" />);
      await new Promise<void>((resolve) => setTimeout(resolve, 20));
    });
    expect(resourceRequests).toBe(1);

    await act(async () => {
      renderer!.unmount();
    });
  });
});
