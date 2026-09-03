import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { AppState } from "../types/frontend";
import { useBackgroundSessionActivity } from "./useBackgroundSessionActivity";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

function installWindow(port: number): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
      setInterval: globalThis.setInterval.bind(globalThis),
      clearInterval: globalThis.clearInterval.bind(globalThis),
    },
  });
}

function apiResponse(data: unknown): Response {
  return Response.json({
    code: 0,
    message: "ok",
    request_id: "request-background-activity-test",
    data,
  });
}

function emptyState(): AppState {
  return {
    activeJobIdsBySession: new Map(),
    pendingConversations: new Map(),
    unreadSessionKeys: new Set(),
    currentSession: null,
    currentSessionWorkspaceId: null,
  } as AppState;
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
});

describe("useBackgroundSessionActivity", () => {
  test("前台状态产生新的 Map 引用时不重复对账后台 Job", async () => {
    const port = 49_506;
    installWindow(port);
    let jobRequests = 0;
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const path = new URL(url).pathname;
      if (path === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: "local-background-activity-test-token" });
      }
      if (path === "/api/gateway/users/current") {
        return apiResponse({ kind: "guest", user_id: null });
      }
      if (path === "/api/v1/jobs/job-background") {
        jobRequests += 1;
        return apiResponse({
          job_id: "job-background",
          session_id: "session-background",
          status: "running",
        });
      }
      throw new Error(`测试收到未声明请求: ${url}`);
    }, { preconnect: originalFetch.preconnect });

    function Harness({ revision }: { revision: number }): React.ReactNode {
      const [, setState] = React.useState<AppState>(() => emptyState());
      void revision;
      useBackgroundSessionActivity({
        apiPort: port,
        activeJobIdsBySession: new Map([
          ["workspace-a::session-background", "job-background"],
        ]),
        currentSessionCacheKey: "workspace-a::session-current",
        setState,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness revision={0} />);
      await new Promise<void>((resolve) => setTimeout(resolve, 50));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(jobRequests).toBe(1);

    await act(async () => {
      renderer!.update(<Harness revision={1} />);
      await Promise.resolve();
    });
    expect(jobRequests).toBe(1);
    await act(async () => {
      renderer!.unmount();
    });
  });
});
