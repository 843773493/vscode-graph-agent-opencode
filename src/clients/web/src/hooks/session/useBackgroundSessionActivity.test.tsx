import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { AppState } from "../../types/frontend";
import { useBackgroundSessionActivity } from "./useBackgroundSessionActivity";
import {
  apiResponse,
  buildSessionHookState,
  installGatewayFetch,
  installTestWindow,
  restoreSessionHookGlobals,
} from "./sessionHookTestFixtures";

afterEach(restoreSessionHookGlobals);

describe("useBackgroundSessionActivity", () => {
  test("前台状态产生新的 Map 引用时不重复对账后台 Job", async () => {
    const port = 49_506;
    // window 桩只提供 location.port，getApiBaseUrl() 因此返回同源相对路径；
    // installGatewayFetch 内部用显式 base 解析，保证被测 hook 能看到响应。
    installTestWindow(port);
    let jobRequests = 0;
    installGatewayFetch(({ path }) => {
      if (path === "/api/v1/jobs/job-background") {
        jobRequests += 1;
        return apiResponse({
          job_id: "job-background",
          session_id: "session-background",
          status: "running",
        });
      }
      return undefined;
    });

    let trackedJobs = new Map([
      ["workspace-a::session-background", "job-background"],
    ]);
    function Harness(): React.ReactNode {
      const [, setState] = React.useState<AppState>(() => buildSessionHookState({
        workspaceId: "workspace-a",
        current: null,
        sessions: [],
      }));
      useBackgroundSessionActivity({
        apiPort: port,
        activeJobIdsBySession: trackedJobs,
        currentSessionCacheKey: "workspace-a::session-current",
        setState,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await new Promise<void>((resolve) => setTimeout(resolve, 50));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(jobRequests).toBe(1);

    // 前台状态产生新的 Map 引用但内容不变：签名相等，不得重复对账。
    trackedJobs = new Map(trackedJobs);
    await act(async () => {
      renderer!.update(<Harness />);
      await Promise.resolve();
    });
    expect(jobRequests).toBe(1);
    await act(async () => {
      renderer!.unmount();
    });
  });
});

