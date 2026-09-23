import { afterEach, describe, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as traceStream from "../../api/stream/sessionTraceStream";
import type { AppState } from "../../types/frontend";
import { SESSION_STREAM_MAX_RECONNECT_ATTEMPTS } from "./sessionEventStreamPolicy";
import { useSessionEventStream } from "./useSessionEventStream";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalDocumentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");

afterEach(() => {
  globalThis.fetch = originalFetch;
  restoreGlobalDescriptor("window", originalWindowDescriptor);
  restoreGlobalDescriptor("document", originalDocumentDescriptor);
});

/** 最小 window/document 桩：定时器压成 0 延迟，避免真实退避把测试拖到分钟级。 */
function installWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      setTimeout: (handler: () => void) => globalThis.setTimeout(handler, 0),
      clearTimeout: (id: number) => globalThis.clearTimeout(id),
      setInterval: () => 0,
      clearInterval: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      visibilityState: "hidden",
      hasFocus: () => false,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    },
  });
}

async function flush(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

async function mountSessionStream(
  state: { value: AppState },
): Promise<ReactTestRenderer> {
  function Probe(): React.ReactNode {
    useSessionEventStream({
      apiPort: 49_902,
      sessionId: "session-reconnect",
      workspaceId: "workspace-reconnect",
      sessionCacheKey: "workspace-reconnect::session-reconnect",
      activeJobId: null,
      timelineReady: true,
      initialEventCursor: null,
      refreshTurnHistory: () => undefined,
      loadTerminalTurn: async () => undefined,
      setState: (update) => {
        state.value = typeof update === "function"
          ? update(state.value)
          : update;
      },
    });
    return null;
  }
  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  return renderer!;
}

function initialAppState(): AppState {
  return { status: "准备就绪" } as unknown as AppState;
}

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
        refreshTurnHistory: () => undefined,
        loadTerminalTurn: async () => undefined,
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

describe("useSessionEventStream 有界重连", () => {
  test("连续建连失败到达上限后停止重连并给出可见终态", async () => {
    installWindow();
    const streamSpy = spyOn(traceStream, "streamSessionEvents")
      .mockRejectedValue(new Error("连接被拒绝"));
    const state = { value: initialAppState() };
    const renderer = await mountSessionStream(state);

    // 冲刷远超上限的轮次：若上限判定缺失，重连次数会继续无界增长。
    for (let round = 0; round < SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 10; round += 1) {
      await flush();
    }

    expect(streamSpy).toHaveBeenCalledTimes(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 1);
    expect(state.value.status).toContain("已停止自动重连");
    streamSpy.mockRestore();
    act(() => renderer.unmount());
  });

  test("连接建立并持续有心跳活动时不会被上限误判为放弃", async () => {
    installWindow();
    // 每次建连都立即报告活动（等价于收到心跳注释），随后断开重连；
    // onActivity 归零计数，因此连续 20 次「先建立后断开」必须一直重连。
    const streamSpy = spyOn(traceStream, "streamSessionEvents")
      .mockImplementation(async (_port, _sessionId, options) => {
        options?.onActivity?.();
        throw new Error("断开");
      });
    const state = { value: initialAppState() };
    const renderer = await mountSessionStream(state);

    for (let round = 0; round < SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 14; round += 1) {
      await flush();
    }

    expect(streamSpy.mock.calls.length)
      .toBeGreaterThan(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 1);
    expect(state.value.status).not.toContain("已停止自动重连");
    streamSpy.mockRestore();
    act(() => renderer.unmount());
  });
});
