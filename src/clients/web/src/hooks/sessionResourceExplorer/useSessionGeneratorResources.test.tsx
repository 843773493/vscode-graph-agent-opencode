import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import { useSessionGeneratorResources } from "./useSessionGeneratorResources";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

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
  restoreGlobalDescriptor("window", originalWindowDescriptor);
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

  test("相同生成运行快照不会替换状态或触发重复提交", async () => {
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: {
        setTimeout: () => 1,
        clearTimeout: () => undefined,
      },
    });
    let runStatus = "running";
    let outputTitle = "Generated session";
    const run = {
      run_id: "run-1",
      generator_id: "generator-1",
      idempotency_key: "key-1",
      status: runStatus,
      trigger_type: "manual",
      scheduled_for: "2026-10-25T00:00:00Z",
      outputs: [
        {
          kind: "session",
          workspace_id: "workspace-1",
          session_id: "session-1",
          title: outputTitle,
          navigation_path: ["workspace-1", "session-1"],
        },
      ],
      execution_workspace_id: "workspace-1",
      message_id: "message-1",
      job_id: "job-1",
      report_back_job_id: null,
      error: null,
      started_at: "2026-10-25T00:00:01Z",
      ended_at: null,
    };
    let runRequests = 0;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/api/gateway/auth/local-credential")) {
        return apiResponse({ token: "local-test-token" });
      }
      if (url.includes("/api/gateway/users/current")) {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (url.endsWith("/session-generators/generator-1/runs")) {
        runRequests += 1;
        return apiResponse({
          items: [{
            ...run,
            status: runStatus,
            outputs: run.outputs.map((output) => ({
              ...output,
              title: outputTitle,
              navigation_path: [...output.navigation_path],
            })),
          }],
        });
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    let resources!: ReturnType<typeof useSessionGeneratorResources>;
    let commitCount = 0;
    function Harness(): React.ReactNode {
      resources = useSessionGeneratorResources(49_407, false);
      React.useEffect(() => { commitCount += 1; });
      return null;
    }

    let renderer: ReactTestRenderer | undefined;
    try {
      await act(async () => {
        renderer = create(<Harness />);
      });
      await act(async () => {
        await resources.refreshGenerationRuns("generator-1");
      });
      const firstSnapshot = resources.generationRuns;
      const firstRunList = firstSnapshot.get("generator-1");
      const committedUpdates = commitCount;

      await act(async () => {
        await resources.refreshGenerationRuns("generator-1");
      });

      expect(runRequests).toBe(2);
      expect(resources.generationRuns).toBe(firstSnapshot);
      expect(resources.generationRuns.get("generator-1")).toBe(firstRunList);
      expect(commitCount).toBe(committedUpdates);

      outputTitle = "Renamed generated session";
      await act(async () => {
        await resources.refreshGenerationRuns("generator-1");
      });

      const outputChangedSnapshot = resources.generationRuns;
      expect(runRequests).toBe(3);
      expect(outputChangedSnapshot).not.toBe(firstSnapshot);
      expect(outputChangedSnapshot.get("generator-1")?.[0]?.outputs[0]?.title)
        .toBe("Renamed generated session");
      expect(commitCount).toBe(committedUpdates + 1);

      runStatus = "reporting";
      await act(async () => {
        await resources.refreshGenerationRuns("generator-1");
      });

      expect(runRequests).toBe(4);
      expect(resources.generationRuns).not.toBe(outputChangedSnapshot);
      expect(resources.generationRuns.get("generator-1")?.[0]?.status).toBe("reporting");
      expect(commitCount).toBe(committedUpdates + 2);
    } finally {
      if (renderer) act(() => renderer!.unmount());
    }
  });
});
