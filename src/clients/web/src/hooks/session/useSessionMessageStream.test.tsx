import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { useSessionMessageStream } from "./useSessionMessageStream";
import type { AppState } from "../../types/frontend";
import {
  apiResponse,
  createStateMirror,
  hangUntilReleased,
  installGatewayFetch,
  installTestWindow,
  restoreSessionHookGlobals,
  useSessionMessageStreamHarness,
} from "./sessionHookTestFixtures";

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

afterEach(restoreSessionHookGlobals);

describe("useSessionMessageStream 首次连接", () => {
  test("首个 404 后有限退避重试，随后 200 继续消费终态", async () => {
    const port = 49_410;
    installTestWindow(port);
    let streamRequests = 0;
    installGatewayFetch(() => {
      streamRequests += 1;
      if (streamRequests === 1) {
        return new Response("not ready", { status: 404, statusText: "Not Found" });
      }
      return streamResponse();
    }, { token: "stream-retry-token" });

    const mirror = createStateMirror(minimalState());
    const Harness = useSessionMessageStreamHarness({
      apiPort: port,
      sessionId: "ses_stream_retry",
      turnId: "turn_stream_retry",
      workspaceId: "workspace_stream_retry",
      sessionCacheKey: "workspace_stream_retry::ses_stream_retry",
    }, mirror.setState);

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1_600));
    });

    expect(streamRequests).toBe(2);
    const stream = [...(mirror.current().messageStreamsByTurnStream ?? new Map()).values()][0];
    expect(stream?.streamStatus).toBe("completed");
    expect(stream?.connectionStatus).toBe("terminal");
    expect(stream?.protocolError).toBeNull();
    act(() => renderer!.unmount());
  });

  test("组件更新不会重新建立已连接的消息流", async () => {
    const port = 49_411;
    installTestWindow(port);
    let streamRequests = 0;
    let releaseStream: ((response: Response) => void) | undefined;
    installGatewayFetch(() => {
      streamRequests += 1;
      return new Promise<Response>((resolve) => {
        releaseStream = resolve;
      });
    }, { token: "stable-stream-token" });

    const mirror = createStateMirror(minimalState());
    const Harness = useSessionMessageStreamHarness({
      apiPort: port,
      sessionId: "ses_stream_stable",
      turnId: "turn_stream_stable",
      workspaceId: "workspace_stream_stable",
      sessionCacheKey: "workspace_stream_stable::ses_stream_stable",
    }, mirror.setState);

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
    expect([...(mirror.current().messageStreamsByTurnStream ?? new Map()).values()][0]?.streamStatus)
      .toBe("completed");
    act(() => renderer!.unmount());
  });

  test("收到终态事件后不等待 SSE 关闭就清理前端运行态", async () => {
    const port = 49_412;
    installTestWindow(port);
    installGatewayFetch(
      () => openAfterTerminalStreamResponse(),
      { token: "stream-terminal-token" },
    );

    const sessionCacheKey = "workspace_stream_open::ses_stream_open";
    const initialState = minimalState();
    initialState.activeJobIdsBySession.set(sessionCacheKey, "turn_stream_open");
    const mirror = createStateMirror(initialState);
    const Harness = useSessionMessageStreamHarness({
      apiPort: port,
      sessionId: "ses_stream_open",
      turnId: "turn_stream_open",
      workspaceId: "workspace_stream_open",
      sessionCacheKey,
    }, mirror.setState);

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 200));
    });

    expect(mirror.current().activeJobIdsBySession.has(sessionCacheKey)).toBe(false);
    expect([...(mirror.current().messageStreamsByTurnStream ?? new Map()).values()][0]?.streamStatus)
      .toBe("completed");
    act(() => renderer!.unmount());
  });

  test("真实 React 异步状态更新不会丢失增量事件游标和正文", async () => {
    const port = 49_413;
    installTestWindow(port);
    const messageStreamUrls: string[] = [];
    let streamRequests = 0;
    installGatewayFetch(({ url }) => {
      messageStreamUrls.push(url);
      streamRequests += 1;
      return streamRequests === 1
        ? partialStreamResponse()
        : terminalStreamResponse();
    }, { token: "stream-async-token" });

    // 本用例必须走真实 React 状态更新，才能复现异步 setState 下的游标丢失，
    // 因此保留独立的 useState Harness，不复用闭包镜像夹具。
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

describe("useSessionMessageStream 终态 failure 归一", () => {
  function snapshotStreamResponse(failureJson: string): Response {
    const payload = {
      snapshot_seq: 1,
      stream_status: "failed",
      agent_loop_status: "failed",
      current_attempt: 1,
      blocks: [],
      tool_executions: [],
      tool_calls: [],
      model_calls: [],
      activities: [],
      resource_refs: [],
      resumable: false,
      failure: JSON.parse(failureJson),
    };
    const data = JSON.stringify({
      event_id: "evt_snapshot_failure",
      session_id: "ses_failure_norm",
      turn_id: "turn_failure_norm",
      turn_stream_id: "strm_failure_norm",
      event_seq: 1,
      type: "stream.snapshot",
      payload,
    });
    return new Response(
      `id: 1\nevent: stream.snapshot\ndata: ${data}\n\n`,
      { status: 200, headers: { "content-type": "text/event-stream" } },
    );
  }

  function failedStreamResponse(payload: Record<string, unknown>): Response {
    const data = JSON.stringify({
      event_id: "evt_stream_failed",
      session_id: "ses_failure_norm",
      turn_id: "turn_failure_norm",
      turn_stream_id: "strm_failure_norm",
      event_seq: 1,
      type: "stream.failed",
      payload,
    });
    return new Response(
      `id: 1\nevent: stream.failed\ndata: ${data}\n\n`,
      { status: 200, headers: { "content-type": "text/event-stream" } },
    );
  }

  // 真实线格式：proto3 string 无 presence，空 message 会在编码时整个键被省略，
  // 因此「缺失 message」与「显式空串」都必须按同一语义处理。
  const cases: Array<{ label: string; snapshotFailure: string; eventPayload: Record<string, unknown>; expectStatus: string }> = [
    {
      label: "空串 message",
      snapshotFailure: JSON.stringify({ code: "execution_error", message: "" }),
      eventPayload: { code: "execution_error", message: "" },
      expectStatus: "任务失败前",
    },
    {
      label: "缺失 message",
      snapshotFailure: JSON.stringify({ code: "execution_error", after_interrupt_requested: false, resumable: false }),
      eventPayload: { code: "execution_error", resumable: false },
      expectStatus: "任务失败前",
    },
    {
      // 非字符串 message：前端校验只要求 payload 是对象，不会拦下非法 failure；
      // 唯一归一实现必须判定为无效 failure，不得伪造 "任务失败: 42" 这类假文案。
      label: "非字符串 message",
      snapshotFailure: JSON.stringify({ code: "execution_error", message: 42 }),
      eventPayload: { code: "execution_error", message: 42 },
      expectStatus: "任务失败前",
    },
    {
      label: "合法 message",
      snapshotFailure: JSON.stringify({ code: "execution_error", message: "真实失败原因" }),
      eventPayload: { code: "execution_error", message: "真实失败原因" },
      expectStatus: "任务失败: 真实失败原因",
    },
  ];

  async function runFailureCase(
    port: number,
    response: Response,
  ): Promise<{ status: string; failure: unknown }> {
    installTestWindow(port);
    installGatewayFetch(() => response, { token: "failure-norm-token" });
    const initialState = minimalState();
    initialState.status = "任务失败前";
    const mirror = createStateMirror(initialState);
    const Harness = useSessionMessageStreamHarness({
      apiPort: port,
      sessionId: "ses_failure_norm",
      turnId: "turn_failure_norm",
      workspaceId: "workspace_failure_norm",
      sessionCacheKey: "workspace_failure_norm::ses_failure_norm",
    }, mirror.setState);
    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
    });
    act(() => renderer!.unmount());
    const stream = [...(mirror.current().messageStreamsByTurnStream ?? new Map()).values()][0];
    return { status: mirror.current().status, failure: stream?.failure };
  }

  let port = 49_510;
  for (const testCase of cases) {
    test(`${testCase.label}：stream.failed 与 stream.snapshot 归一一致`, async () => {
      port += 1;
      const eventResult = await runFailureCase(port, failedStreamResponse(testCase.eventPayload));
      port += 1;
      const snapshotResult = await runFailureCase(port, snapshotStreamResponse(testCase.snapshotFailure));

      expect(snapshotResult.status).toBe(eventResult.status);
      expect(snapshotResult.status).toBe(testCase.expectStatus);
      expect(snapshotResult.failure).toEqual(eventResult.failure);
    });
  }
});

describe("useSessionMessageStream 连接与终态语义", () => {
  function streamState(mirror: { current: () => AppState }) {
    return [...(mirror.current().messageStreamsByTurnStream ?? new Map()).values()][0];
  }

  test("游标失效且快照也读不到时写可见诊断，不停留在静默重连", async () => {
    const port = 49_720;
    installTestWindow(port);
    let snapshotRequests = 0;
    installGatewayFetch(({ path }) => {
      if (path.includes("/message-stream/snapshot")) {
        snapshotRequests += 1;
        return new Response(JSON.stringify({ detail: "快照服务不可用" }), {
          status: 503,
          statusText: "Service Unavailable",
          headers: { "content-type": "application/json" },
        });
      }
      if (path.includes("/message-stream")) {
        return new Response(JSON.stringify({ detail: "游标失效" }), {
          status: 410,
          statusText: "Gone",
          headers: { "content-type": "application/json" },
        });
      }
      return undefined;
    }, { token: "ms-guard-snapshot-token" });

    const mirror = createStateMirror(minimalState());
    const Harness = useSessionMessageStreamHarness({
      apiPort: port,
      sessionId: "ses_gap",
      turnId: "turn_gap",
      workspaceId: "ws_gap",
      sessionCacheKey: "ws_gap::ses_gap",
    }, mirror.setState);

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });
    act(() => renderer!.unmount());

    // 连接已放弃时必须把快照读取失败的原因写进诊断字段，让界面能区分
    // 「协议/恢复失败」与「单纯断线重连」，不能只标 disconnected 后静默重试。
    expect(snapshotRequests).toBeGreaterThan(0);
    expect(streamState(mirror)?.connectionStatus).toBe("disconnected");
    expect(streamState(mirror)?.protocolError)
      .toBe("请求失败 503 Service Unavailable: 快照服务不可用");
  });

  test("interrupted 终态归类为已取消而不是任务失败", async () => {
    const port = 49_721;
    installTestWindow(port);
    installGatewayFetch(() => new Response(
      "id: 1\n"
      + "event: stream.interrupted\n"
      + 'data: {"event_id":"evt_interrupted","session_id":"ses_cancel","turn_id":"turn_cancel","turn_stream_id":"strm_cancel","event_seq":1,"type":"stream.interrupted","payload":{"interrupt_request_id":"ir_1","status":"interrupted"}}\n\n',
      { status: 200, headers: { "content-type": "text/event-stream" } },
    ), { token: "ms-guard-cancel-token" });

    const initialState = minimalState();
    initialState.status = "任务失败前";
    initialState.activeJobIdsBySession.set("ws_cancel::ses_cancel", "turn_cancel");
    initialState.pendingConversations.set("ws_cancel::ses_cancel", [
      {
        conversationId: "msg_cancel",
        displayMode: "live",
        sessionId: "ses_cancel",
        userMessage: null,
        assistantMessages: [],
        events: [],
        status: "running",
        jobId: "turn_cancel",
        pending: true,
        activeJobOverlay: true,
        source: "pending",
      },
    ] as never);
    const mirror = createStateMirror(initialState);
    const Harness = useSessionMessageStreamHarness({
      apiPort: port,
      sessionId: "ses_cancel",
      turnId: "turn_cancel",
      workspaceId: "ws_cancel",
      sessionCacheKey: "ws_cancel::ses_cancel",
    }, mirror.setState);

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
    });
    act(() => renderer!.unmount());

    // 用户主动取消产生的 interrupted 必须归入 cancelled，不能报成任务失败。
    expect(mirror.current().status).toBe("任务已取消");
    const pending = mirror.current().pendingConversations.get("ws_cancel::ses_cancel") ?? [];
    expect(pending[0]?.turnStatus).toBe("cancelled");
  });

  test("组件卸载会 abort 在途消息流连接", async () => {
    const port = 49_722;
    installTestWindow(port);
    let streamSignal: AbortSignal | null = null;
    const hanging = hangUntilReleased<Response>();
    installGatewayFetch(({ path, init }) => {
      if (path.includes("/message-stream/snapshot")) {
        return new Response(JSON.stringify({ detail: "无快照" }), {
          status: 404,
          headers: { "content-type": "application/json" },
        });
      }
      if (path.includes("/message-stream")) {
        streamSignal = init?.signal ?? null;
        return hanging.promise;
      }
      return undefined;
    }, { token: "ms-guard-abort-token" });

    const mirror = createStateMirror(minimalState());
    const Harness = useSessionMessageStreamHarness({
      apiPort: port,
      sessionId: "ses_abort",
      turnId: "turn_abort",
      workspaceId: "ws_abort",
      sessionCacheKey: "ws_abort::ses_abort",
    }, mirror.setState);

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 200));
    });

    // 连接已建立且仍在途，卸载必须主动取消它，否则长连接会泄漏。
    expect(streamSignal).not.toBeNull();
    const signal = streamSignal as unknown as AbortSignal;
    expect(signal.aborted).toBe(false);
    act(() => renderer!.unmount());
    expect(signal.aborted).toBe(true);
  });
});
