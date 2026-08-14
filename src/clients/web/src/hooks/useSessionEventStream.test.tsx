import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useSessionEventStream } from "./useSessionEventStream";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("useSessionEventStream readiness", () => {
  test("Turn projection 尚未 ready 时不请求会话 Trace stream", async () => {
    let fetchCount = 0;
    globalThis.fetch = Object.assign(async () => {
      fetchCount += 1;
      throw new Error("partial 阶段不应连接网络");
    }, { preconnect: originalFetch.preconnect });

    function Harness(): React.ReactNode {
      useSessionEventStream({
        apiPort: 49_401,
        sessionId: "session-partial",
        workspaceId: "workspace-partial",
        sessionCacheKey: "workspace-partial::session-partial",
        activeJobId: null,
        timelineReady: false,
        initialEventCursor: null,
        refreshTurnDetails: async () => undefined,
        refreshTurnHistory: () => undefined,
        setState: (update) => {
          void update;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await Promise.resolve();
    });

    expect(fetchCount).toBe(0);
    act(() => renderer!.unmount());
  });
});
