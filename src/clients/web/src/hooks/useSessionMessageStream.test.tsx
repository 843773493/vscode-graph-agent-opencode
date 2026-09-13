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

function partialStreamResponse(): Response {
  const body = [
    "id: 1\n",
    "event: stream.opened\n",
    'data: {"event_id":"evt_opened","session_id":"ses_stream_async","turn_id":"turn_stream_async","turn_stream_id":"strm_stream_async","event_seq":1,"type":"stream.opened","payload":{"status":"open"}}\n\n',
    "id: 2\n",
    "event: model.started\n",
    'data: {"event_id":"evt_model_started","session_id":"ses_stream_async","turn_id":"turn_stream_async","turn_stream_id":"strm_stream_async","event_seq":2,"type":"model.started","payload":{"model_call_id":"call_async","attempt":1}}\n\n',
    "id: 3\n",
    "event: block.started\n",
    'data: {"event_id":"evt_block_started","session_id":"ses_stream_async","turn_id":"turn_stream_async","turn_stream_id":"strm_stream_async","event_seq":3,"type":"block.started","payload":{"block_id":"block_async","block_index":0,"carrier_type":"text"}}\n\n',
    "id: 4\n",
    "event: block.delta\n",
    'data: {"event_id":"evt_block_delta","session_id":"ses_stream_async","turn_id":"turn_stream_async","turn_stream_id":"strm_stream_async","event_seq":4,"type":"block.delta","payload":{"block_id":"block_async","operation":"append","text":"实时增量"}}\n\n',
  ].join("");
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function terminalStreamResponse(): Response {
  const body = [
    "id: 5\n",
    "event: stream.completed\n",
    'data: {"event_id":"evt_completed","session_id":"ses_stream_async","turn_id":"turn_stream_async","turn_stream_id":"strm_stream_async","event_seq":5,"type":"stream.completed","payload":{}}\n\n',
  ].join("");
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function openAfterTerminalStreamResponse(
  sessionId = "ses_stream_open",
  turnId = "turn_stream_open",
): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode([
        "id: 1\n",
        "event: stream.opened\n",
        `data: {"event_id":"evt_opened","session_id":"${sessionId}","turn_id":"${turnId}","turn_stream_id":"strm_stream_open","event_seq":1,"type":"stream.opened","payload":{"status":"open"}}\n\n`,
        "id: 2\n",
        "event: stream.completed\n",
        `data: {"event_id":"evt_completed","session_id":"${sessionId}","turn_id":"${turnId}","turn_stream_id":"strm_stream_open","event_seq":2,"type":"stream.completed","payload":{}}\n\n`,
      ].join("")));
      // 模拟服务端发送终态后仍保持 SSE 连接，直到客户端主动取消。
    },
  });
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
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), "http://localhost").pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "stream-retry-token" } });
        }
        streamRequests += 1;
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
        setState,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1_600));
    });

    expect(streamRequests).toBe(2);
    const stream = [...(state.messageStreamsByTurnStream ?? new Map()).values()][0];
    expect(stream?.streamStatus).toBe("completed");
    expect(stream?.connectionStatus).toBe("terminal");
    expect(stream?.protocolError).toBeNull();
    act(() => renderer!.unmount());
  });

  test("组件更新不会重新建立已连接的消息流", async () => {
    const port = 49_411;
    installWindow(port);
    let streamRequests = 0;
    let releaseStream: ((response: Response) => void) | undefined;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), "http://localhost").pathname;
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
    const setState: SetAppState = (update) => {
      state = typeof update === "function" ? update(state) : update;
    };
    function Harness(): React.ReactNode {
      useSessionMessageStream({
        apiPort: port,
        sessionId: "ses_stream_stable",
        turnId: "turn_stream_stable",
        workspaceId: "workspace_stream_stable",
        sessionCacheKey: "workspace_stream_stable::ses_stream_stable",
        setState,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 180));
    });
    expect(streamRequests).toBe(1);

    await act(async () => {
      renderer!.update(<Harness />);
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(streamRequests).toBe(1);

    await act(async () => {
      releaseStream?.(streamResponse("ses_stream_stable", "turn_stream_stable"));
      await new Promise((resolve) => setTimeout(resolve, 80));
    });
    expect([...(state.messageStreamsByTurnStream ?? new Map()).values()][0]?.streamStatus)
      .toBe("completed");
    act(() => renderer!.unmount());
  });

  test("收到终态事件后不等待 SSE 关闭就清理前端运行态", async () => {
    const port = 49_412;
    installWindow(port);
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), "http://localhost").pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "stream-terminal-token" } });
        }
        return openAfterTerminalStreamResponse();
      },
      { preconnect: originalFetch.preconnect },
    );

    let state = minimalState();
    const sessionCacheKey = "workspace_stream_open::ses_stream_open";
    state.activeJobIdsBySession.set(sessionCacheKey, "turn_stream_open");
    const setState: SetAppState = (update) => {
      state = typeof update === "function" ? update(state) : update;
    };
    function Harness(): React.ReactNode {
      useSessionMessageStream({
        apiPort: port,
        sessionId: "ses_stream_open",
        turnId: "turn_stream_open",
        workspaceId: "workspace_stream_open",
        sessionCacheKey,
        setState,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 200));
    });

    expect(state.activeJobIdsBySession.has(sessionCacheKey)).toBe(false);
    expect([...(state.messageStreamsByTurnStream ?? new Map()).values()][0]?.streamStatus)
      .toBe("completed");
    act(() => renderer!.unmount());
  });

  test("真实 React 异步状态更新不会丢失增量事件游标和正文", async () => {
    const port = 49_413;
    installWindow(port);
    const messageStreamUrls: string[] = [];
    let streamRequests = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const url = String(args[0]);
        const path = new URL(url, "http://localhost").pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "stream-async-token" } });
        }
        messageStreamUrls.push(url);
        streamRequests += 1;
        return streamRequests === 1
          ? partialStreamResponse()
          : terminalStreamResponse();
      },
      { preconnect: originalFetch.preconnect },
    );

    let latestState = minimalState();
    function Harness(): React.ReactNode {
      const [state, setState] = React.useState(minimalState);
      latestState = state;
      useSessionMessageStream({
        apiPort: port,
        sessionId: "ses_stream_async",
        turnId: "turn_stream_async",
        workspaceId: "workspace_stream_async",
        sessionCacheKey: "workspace_stream_async::ses_stream_async",
        setState,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await new Promise((resolve) => setTimeout(resolve, 1_500));
    });

    expect(messageStreamUrls).toHaveLength(2);
    const reconnectUrl = new URL(messageStreamUrls[1]!, "http://localhost");
    expect(reconnectUrl.searchParams.get("after_seq")).toBe("4");
    const stream = [...(latestState.messageStreamsByTurnStream ?? new Map()).values()][0];
    expect(stream?.blocks[0]?.text).toBe("实时增量");
    expect(stream?.streamStatus).toBe("completed");
    act(() => renderer!.unmount());
  });
});
