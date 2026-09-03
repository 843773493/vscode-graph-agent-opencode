import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useSessionGeneratorResources } from "./useSessionGeneratorResources";

const originalFetch = globalThis.fetch;

function apiResponse(data: unknown): Response {
  return new Response(JSON.stringify({
    code: 0,
    message: "ok",
    data,
    request_id: "request-test",
  }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("useSessionGeneratorResources 请求启用边界", () => {
  test("自动化面板未启用时不读取生成器，启用后才读取一次", async () => {
    let generatorRequests = 0;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return apiResponse({ token: "local-test-token" });
      }
      if (url.includes("/api/gateway/users/current")) {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (url.includes("/api/gateway/session-generators")) {
        generatorRequests += 1;
        return apiResponse({ items: [] });
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    function Harness({ enabled }: { enabled: boolean }): React.ReactNode {
      useSessionGeneratorResources(49_406, enabled);
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness enabled={false} />);
      await Promise.resolve();
    });
    expect(generatorRequests).toBe(0);

    await act(async () => {
      renderer!.update(<Harness enabled />);
      await Promise.resolve();
    });
    expect(generatorRequests).toBe(1);
    act(() => renderer!.unmount());
  });
});
