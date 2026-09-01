import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useSessionMessageStream } from "./useSessionMessageStream";
import type { AppState } from "../types/frontend";
import type { SetAppState } from "./sessionEventStream/sessionRefresh";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

function installWindow(port: number): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    },
  });
}

function streamResponse(
  sessionId = "ses_stream_retry",
  turnId = "turn_stream_retry",
): Response {
  const body = [
    "id: 1\n",
    "event: stream.opened\n",
    `data: {"event_id":"evt_opened","session_id":"${sessionId}","turn_id":"${turnId}","turn_stream_id":"strm_stream_retry","event_seq":1,"type":"stream.opened","payload":{"status":"open"}}\n\n`,
    "id: 2\n",
    "event: stream.completed\n",
    `data: {"event_id":"evt_completed","session_id":"${sessionId}","turn_id":"${turnId}","turn_stream_id":"strm_stream_retry","event_seq":2,"type":"stream.completed","payload":{}}\n\n`,
  ].join("");
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function minimalState(): AppState {
  return {
    eventQueuesBySession: new Map(),
    sessionTraceHistoryBySession: new Map(),
    pendingConversations: new Map(),
    activeJobIdsBySession: new Map(),
    unreadSessionKeys: new Set(),
    gatewayUserViewStates: new Map(),
    sessionAttachmentSummaries: new Map(),
    sessionsByWorkspace: new Map(),
    sessionGatewayWorkspaceById: new Map(),
    turnTimelinesBySession: new Map(),
    messageStreamsByTurnStream: new Map(),
  } as unknown as AppState;
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
});

describe("useSessionMessageStream 首次连接", () => {
  test("首个 404 后有限退避重试，随后 200 继续消费终态", async () => {
    const port = 49_410;
    installWindow(port);
    let streamRequests = 0;
    let streamSignal: AbortSignal | null = null;
    let signalAbortedWhenTerminalWasNotified: boolean | null = null;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0])).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "stream-retry-token" } });
        }
        streamRequests += 1;
        streamSignal = args[1]?.signal ?? null;
        if (streamRequests === 1) {
          return new Response("not ready", { status: 404, statusText: "Not Found" });
        }
        return streamResponse();
      },
      { preconnect: originalFetch.preconnect },
    );

    let state = minimalState();
    const setState: SetAppState = (update) => {
      state = typeof update === "function" ? update(state) : update;
    };
    function Harness(): React.ReactNode {
      useSessionMessageStream({
        apiPort: port,
        sessionId: "ses_stream_retry",
        turnId: "turn_stream_retry",
        workspaceId: "workspace_stream_retry",
        sessionCacheKey: "workspace_stream_retry::ses_stream_retry",
        onTerminal: () => {
          signalAbortedWhenTerminalWasNotified = streamSignal?.aborted ?? null;
        },
        setState,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await new Promise((resolve) => setTimeout(resolve, 1_600));
    });

    expect(streamRequests).toBe(2);
    const stream = [...state.messageStreamsByTurnStream.values()][0];
    expect(stream?.streamStatus).toBe("completed");
    expect(stream?.connectionStatus).toBe("terminal");
    expect(stream?.protocolError).toBeNull();
    expect(signalAbortedWhenTerminalWasNotified).toBe(false);
    act(() => renderer!.unmount());
  });

  test("终端回调更新不会主动 abort 已建立的消息流", async () => {
    const port = 49_411;
    installWindow(port);
    let streamRequests = 0;
    let releaseStream: ((response: Response) => void) | undefined;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0])).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "stable-stream-token" } });
        }
        streamRequests += 1;
        return new Promise<Response>((resolve) => {
          releaseStream = resolve;
        });
      },
      { preconnect: originalFetch.preconnect },
    );

    let state = minimalState();
    let terminalCallbackVersion = 0;
    const setState: SetAppState = (update) => {
      state = typeof update === "function" ? update(state) : update;
    };
    function Harness({ version }: { version: number }): React.ReactNode {
      useSessionMessageStream({
        apiPort: port,
        sessionId: "ses_stream_stable",
        turnId: "turn_stream_stable",
        workspaceId: "workspace_stream_stable",
        sessionCacheKey: "workspace_stream_stable::ses_stream_stable",
        onTerminal: () => {
          terminalCallbackVersion = version;
        },
        setState,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness version={1} />);
      await new Promise((resolve) => setTimeout(resolve, 180));
    });
    expect(streamRequests).toBe(1);

    await act(async () => {
      renderer!.update(<Harness version={2} />);
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(streamRequests).toBe(1);

    await act(async () => {
      releaseStream?.(streamResponse("ses_stream_stable", "turn_stream_stable"));
      await new Promise((resolve) => setTimeout(resolve, 80));
    });
    expect([...state.messageStreamsByTurnStream.values()][0]?.streamStatus)
      .toBe("completed");
    expect(terminalCallbackVersion).toBe(2);
    act(() => renderer!.unmount());
  });
});
