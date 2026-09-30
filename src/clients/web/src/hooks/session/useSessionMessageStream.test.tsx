import { afterEach, describe, expect, jest, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { SSE_IDLE_TIMEOUT_MS } from "../../sse/sseIdleTimeout";
import { useSessionMessageStream } from "./useSessionMessageStream";
import * as messageStreamApi from "../../api/stream/sessionMessageStream";
import {
  MessageStreamConnectionError,
  MessageStreamCursorGoneError,
} from "../../api/stream/sessionMessageStream";
import { SESSION_STREAM_MAX_RECONNECT_ATTEMPTS } from "../sessionEventStream/sessionEventStreamPolicy";
import type { AppState } from "../../types/frontend";
import type { MessageStreamEvent, MessageStreamState } from "../../state/messageStream/index";
import {
  createStateMirror,
  hangUntilReleased,
  installGatewayFetch,
  installTestWindow,
  restoreSessionHookGlobals,
  useSessionMessageStreamHarness,
} from "./sessionHookTestFixtures";

afterEach(restoreSessionHookGlobals);

afterEach(() => {
  jest.useRealTimers();
});

/** 逐字复用的 AppState 镜像：会话级 map 全部给空值，用例只覆盖关心的字段。 */
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

type Mirror = ReturnType<typeof createStateMirror>;

/** 读镜像里第一条（唯一一条）Turn 消息流状态。 */
function streamOf(mirror: Mirror): MessageStreamState | undefined {
  return [...(mirror.current().messageStreamsByTurnStream ?? new Map()).values()][0];
}

/**
 * 把 window.setTimeout 压成 0 延迟，避免真实退避把有界重连用例拖到分钟级。
 * 由 afterEach(restoreSessionHookGlobals) 还原。
 */
function installZeroDelayWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      setTimeout: (handler: () => void) => globalThis.setTimeout(handler, 0),
      clearTimeout: (id: number) => globalThis.clearTimeout(id),
    },
  });
}

/** 构造一个 Turn 消息流事件信封，逐字段拼装避免每条用例手写完整 JSON。 */
function streamEvent(
  streamId: string,
  sessionId: string,
  turnId: string,
  eventSeq: number,
  type: MessageStreamEvent["type"],
  payload: Record<string, unknown>,
): MessageStreamEvent {
  return {
    event_id: `evt_${streamId}_${eventSeq}`,
    session_id: sessionId,
    turn_id: turnId,
    turn_stream_id: `strm_${streamId}`,
    event_seq: eventSeq,
    type,
    payload,
  } as MessageStreamEvent;
}

/** SSE 线格式的事件块。 */
function sseBlock(event: MessageStreamEvent): string {
  return `id: ${event.event_seq}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
}

/** 由事件序列拼出完整的 text/event-stream 响应。 */
function sseResponse(events: MessageStreamEvent[]): Response {
  return new Response(events.map(sseBlock).join(""), {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

/** 保持连接打开的 SSE 响应（用于「终态后服务端不关流」「半死连接」等场景）。 */
function sseStreamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

/** opened + completed 的最小成功流。 */
function terminalStreamResponse(
  streamId: string,
  sessionId: string,
  turnId: string,
): Response {
  return sseResponse([
    streamEvent(streamId, sessionId, turnId, 1, "stream.opened", { status: "open" }),
    streamEvent(streamId, sessionId, turnId, 2, "stream.completed", {}),
  ]);
}

/** 只含前缀四种事件的非终态流，用于验收增量游标与正文。 */
function partialStreamResponse(): Response {
  return sseResponse([
    streamEvent("stream_async", "ses_stream_async", "turn_stream_async", 1, "stream.opened", { status: "open" }),
    streamEvent("stream_async", "ses_stream_async", "turn_stream_async", 2, "model.started", { model_call_id: "call_async", attempt: 1 }),
    streamEvent("stream_async", "ses_stream_async", "turn_stream_async", 3, "block.started", { block_id: "block_async", block_index: 0, carrier_type: "text" }),
    streamEvent("stream_async", "ses_stream_async", "turn_stream_async", 4, "block.delta", { block_id: "block_async", operation: "append", text: "实时增量" }),
  ]);
}

/** 终态后服务端仍保持 SSE 连接：客户端不等待流关闭就必须收口。 */
function openAfterTerminalStreamResponse(): Response {
  return sseStreamResponse([
    sseBlock(streamEvent("stream_open", "ses_stream_open", "turn_stream_open", 1, "stream.opened", { status: "open" })),
    sseBlock(streamEvent("stream_open", "ses_stream_open", "turn_stream_open", 2, "stream.completed", {})),
  ]);
}

/** 终态 failure 用例的 stream.snapshot 快照流。 */
function snapshotStreamResponse(failureJson: string): Response {
  return sseResponse([
    streamEvent("failure_norm", "ses_failure_norm", "turn_failure_norm", 1, "stream.snapshot", {
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
    }),
  ]);
}

/** 终态 failure 用例的 stream.failed 事件流。 */
function failedStreamResponse(payload: Record<string, unknown>): Response {
  return sseResponse([
    streamEvent("failure_norm", "ses_failure_norm", "turn_failure_norm", 1, "stream.failed", payload),
  ]);
}

/** 权威 Turn 快照响应：只覆盖用例关心的字段，其余给安全的非终态默认值。 */
function streamSnapshot(
  overrides: Record<string, unknown>,
): Record<string, unknown> {
  return {
    session_id: "ses_snapshot",
    turn_id: "turn_snapshot",
    turn_stream_id: "strm_snapshot",
    snapshot_seq: 0,
    stream_status: "open",
    agent_loop_status: "running",
    current_attempt: 1,
    blocks: [],
    tool_executions: [],
    tool_calls: [],
    model_calls: [],
    activities: [],
    resource_refs: [],
    resumable: true,
    ...overrides,
  };
}

/**
 * 装配消息流用例：镜像 + Harness。sessionCacheKey 与 workspaceId/sessionId 一致，
 * 省掉每条用例重写的 props 块。
 */
function streamHarness(props: {
  port: number;
  sessionId: string;
  turnId: string;
  workspaceId: string;
  initialState?: AppState;
}): { mirror: Mirror; Harness: () => React.ReactNode } {
  const mirror = createStateMirror(props.initialState ?? minimalState());
  const Harness = useSessionMessageStreamHarness({
    apiPort: props.port,
    sessionId: props.sessionId,
    turnId: props.turnId,
    workspaceId: props.workspaceId,
    sessionCacheKey: `${props.workspaceId}::${props.sessionId}`,
  }, mirror.setState);
  return { mirror, Harness };
}

/** 挂载渲染器，返回卸载函数。 */
async function renderHarness(
  Harness: () => React.ReactNode,
): Promise<{ renderer: ReactTestRenderer; unmount: () => void }> {
  let renderer: ReactTestRenderer;
  await act(async () => {
    renderer = create(<Harness />);
  });
  return { renderer: renderer!, unmount: () => act(() => renderer!.unmount()) };
}

/** 挂载、等待真实时间 waitMs、卸载；用于单次终态收敛类用例。 */
async function runFor(
  Harness: () => React.ReactNode,
  waitMs: number,
): Promise<void> {
  const { unmount } = await renderHarness(Harness);
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, waitMs));
  });
  unmount();
}

/** 有界重连类用例：压平退避后冲刷远超上限的轮次。 */
async function flushReconnectRounds(
  Harness: () => React.ReactNode,
): Promise<void> {
  const { unmount } = await renderHarness(Harness);
  for (let round = 0; round < SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 10; round += 1) {
    await act(async () => {
      await new Promise<void>((resolve) => setTimeout(resolve, 0));
    });
  }
  unmount();
}

describe("useSessionMessageStream 有界重连", () => {
  test("连续建连失败到达上限后停止重连并写出可见终态", async () => {
    installZeroDelayWindow();
    const streamSpy = spyOn(messageStreamApi, "streamSessionMessageEvents")
      .mockRejectedValue(new MessageStreamConnectionError(404, "Not Found"));

    const { mirror, Harness } = streamHarness({
      port: 49_731,
      sessionId: "ses_bounded",
      turnId: "turn_bounded",
      workspaceId: "ws_bounded",
    });
    // 冲刷远超上限的轮次：若上限判定缺失，重连次数会继续无界增长。
    await flushReconnectRounds(Harness);

    expect(streamSpy).toHaveBeenCalledTimes(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 1);
    expect(streamOf(mirror)?.connectionStatus).toBe("retry_exhausted");
    expect(streamOf(mirror)?.protocolError).toContain("无法连接 Turn 消息流");
    streamSpy.mockRestore();
  });

  test("建立连接并持续有心跳活动时不会被上限误判为放弃", async () => {
    installZeroDelayWindow();
    // 每次建连都立即报告活动（等价于收到心跳），随后断开重连；onActivity
    // 归零计数，因此连续远超上限次「先建立后断开」必须一直重连。
    const streamSpy = spyOn(messageStreamApi, "streamSessionMessageEvents")
      .mockImplementation(async (_port, _sessionId, _turnId, options) => {
        options?.onActivity?.();
        options?.onConnected?.(null);
        throw new Error("断开");
      });

    const { mirror, Harness } = streamHarness({
      port: 49_732,
      sessionId: "ses_bounded_active",
      turnId: "turn_bounded_active",
      workspaceId: "ws_bounded_active",
    });
    await flushReconnectRounds(Harness);

    expect(streamSpy.mock.calls.length)
      .toBeGreaterThan(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 1);
    expect(streamOf(mirror)?.connectionStatus).not.toBe("retry_exhausted");
    streamSpy.mockRestore();
  });

  test("410 游标失效且快照非终态时同走有界重连，不形成无上限紧循环", async () => {
    installZeroDelayWindow();
    // 上游持续对该 Turn 返回 410（保留窗口已裁掉游标），而快照端点仍可用且
    // 始终是非终态：恢复路径若不退避也不计数，就会退化成「流请求→快照→流请求」
    // 的紧循环，状态永远停在 connecting。这里必须与普通重连同一口径收敛。
    const streamSpy = spyOn(messageStreamApi, "streamSessionMessageEvents")
      .mockRejectedValue(new MessageStreamCursorGoneError(7));
    const snapshotSpy = spyOn(messageStreamApi, "getSessionMessageStreamSnapshot")
      .mockResolvedValue(streamSnapshot({
        session_id: "ses_gone_loop",
        turn_id: "turn_gone_loop",
        turn_stream_id: "strm_gone_loop",
        snapshot_seq: 7,
      }) as never);

    const { mirror, Harness } = streamHarness({
      port: 49_733,
      sessionId: "ses_gone_loop",
      turnId: "turn_gone_loop",
      workspaceId: "ws_gone_loop",
    });
    await flushReconnectRounds(Harness);

    // 快照恢复能力必须保留：每一轮都先读了权威快照。
    expect(snapshotSpy).toHaveBeenCalledTimes(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 1);
    // 到达上限即停止，流请求次数与普通重连完全一致，不得无界增长。
    expect(streamSpy).toHaveBeenCalledTimes(SESSION_STREAM_MAX_RECONNECT_ATTEMPTS + 1);
    expect(streamOf(mirror)?.connectionStatus).toBe("retry_exhausted");
    expect(streamOf(mirror)?.protocolError).toBe("消息流 event_seq 游标已失效: 7");
    streamSpy.mockRestore();
    snapshotSpy.mockRestore();
  });

  test("410 游标失效后快照给出终态时立即收口且不再重连", async () => {
    installZeroDelayWindow();
    const streamSpy = spyOn(messageStreamApi, "streamSessionMessageEvents")
      .mockRejectedValue(new MessageStreamCursorGoneError(7));
    const snapshotSpy = spyOn(messageStreamApi, "getSessionMessageStreamSnapshot")
      .mockResolvedValue(streamSnapshot({
        session_id: "ses_gone_terminal",
        turn_id: "turn_gone_terminal",
        turn_stream_id: "strm_gone_terminal",
        snapshot_seq: 7,
        stream_status: "completed",
        agent_loop_status: "completed",
        resumable: false,
      }) as never);

    const { mirror, Harness } = streamHarness({
      port: 49_734,
      sessionId: "ses_gone_terminal",
      turnId: "turn_gone_terminal",
      workspaceId: "ws_gone_terminal",
    });
    await flushReconnectRounds(Harness);

    expect(streamSpy).toHaveBeenCalledTimes(1);
    expect(snapshotSpy).toHaveBeenCalledTimes(1);
    expect(streamOf(mirror)?.streamStatus).toBe("completed");
    expect(streamOf(mirror)?.connectionStatus).toBe("terminal");
    streamSpy.mockRestore();
    snapshotSpy.mockRestore();
  });
});

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
      return terminalStreamResponse("stream_retry", "ses_stream_retry", "turn_stream_retry");
    }, { token: "stream-retry-token" });

    const { mirror, Harness } = streamHarness({
      port,
      sessionId: "ses_stream_retry",
      turnId: "turn_stream_retry",
      workspaceId: "workspace_stream_retry",
    });
    await runFor(Harness, 1_600);

    expect(streamRequests).toBe(2);
    expect(streamOf(mirror)?.streamStatus).toBe("completed");
    expect(streamOf(mirror)?.connectionStatus).toBe("terminal");
    expect(streamOf(mirror)?.protocolError).toBeNull();
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

    const { mirror, Harness } = streamHarness({
      port,
      sessionId: "ses_stream_stable",
      turnId: "turn_stream_stable",
      workspaceId: "workspace_stream_stable",
    });
    const { renderer, unmount } = await renderHarness(Harness);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 180));
    });
    expect(streamRequests).toBe(1);

    await act(async () => {
      renderer.update(<Harness />);
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(streamRequests).toBe(1);

    await act(async () => {
      releaseStream?.(terminalStreamResponse("stream_stable", "ses_stream_stable", "turn_stream_stable"));
      await new Promise((resolve) => setTimeout(resolve, 80));
    });
    expect(streamOf(mirror)?.streamStatus).toBe("completed");
    unmount();
  });

  test("收到终态事件后不等待 SSE 关闭就清理前端运行态", async () => {
    const port = 49_412;
    installTestWindow(port);
    installGatewayFetch(
      () => openAfterTerminalStreamResponse(),
      { token: "stream-terminal-token" },
    );

    const initialState = minimalState();
    initialState.activeJobIdsBySession.set("workspace_stream_open::ses_stream_open", "turn_stream_open");
    const { mirror, Harness } = streamHarness({
      port,
      sessionId: "ses_stream_open",
      turnId: "turn_stream_open",
      workspaceId: "workspace_stream_open",
      initialState,
    });
    await runFor(Harness, 200);

    expect(mirror.current().activeJobIdsBySession.has("workspace_stream_open::ses_stream_open")).toBe(false);
    expect(streamOf(mirror)?.streamStatus).toBe("completed");
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
        : sseResponse([
            streamEvent("stream_async", "ses_stream_async", "turn_stream_async", 5, "stream.completed", {}),
          ]);
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
    const { mirror, Harness } = streamHarness({
      port,
      sessionId: "ses_failure_norm",
      turnId: "turn_failure_norm",
      workspaceId: "workspace_failure_norm",
      initialState,
    });
    await runFor(Harness, 250);
    return { status: mirror.current().status, failure: streamOf(mirror)?.failure };
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
  test("协议非法事件必须写出可见诊断，不能只标记断开后无限静默重连", async () => {
    const port = 49_719;
    installTestWindow(port);
    installGatewayFetch(
      (): Response => new Response(
        // 信封缺失 event_id / turn_stream_id：非法事件，重复取同一字节永远失败。
        "id: 1\n"
        + "event: block.delta\n"
        + 'data: {"session_id":"ses_protocol","turn_id":"turn_protocol","event_seq":1,"type":"block.delta","payload":{}}\n\n',
        { status: 200, headers: { "content-type": "text/event-stream" } },
      ),
      { token: "ms-protocol-token" },
    );

    const { mirror, Harness } = streamHarness({
      port,
      sessionId: "ses_protocol",
      turnId: "turn_protocol",
      workspaceId: "ws_protocol",
    });
    await runFor(Harness, 250);

    // 非法协议事件不会自愈；必须把真实原因写进诊断字段供用户与开发者定位。
    expect(streamOf(mirror)?.protocolError).toBe("消息流事件缺少合法的信封字段");
  });

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

    const { mirror, Harness } = streamHarness({
      port,
      sessionId: "ses_gap",
      turnId: "turn_gap",
      workspaceId: "ws_gap",
    });
    await runFor(Harness, 400);

    // 连接已放弃时必须把快照读取失败的原因写进诊断字段，让界面能区分
    // 「协议/恢复失败」与「单纯断线重连」，不能只标 disconnected 后静默重试。
    expect(snapshotRequests).toBeGreaterThan(0);
    expect(streamOf(mirror)?.connectionStatus).toBe("disconnected");
    expect(streamOf(mirror)?.protocolError).toBe("请求失败 503 Service Unavailable: 快照服务不可用");
  });

  test("interrupted 终态归类为已取消而不是任务失败", async () => {
    const port = 49_721;
    installTestWindow(port);
    installGatewayFetch(() => sseResponse([
      streamEvent("cancel", "ses_cancel", "turn_cancel", 1, "stream.interrupted", { interrupt_request_id: "ir_1", status: "interrupted" }),
    ]), { token: "ms-guard-cancel-token" });

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
    const { mirror, Harness } = streamHarness({
      port,
      sessionId: "ses_cancel",
      turnId: "turn_cancel",
      workspaceId: "ws_cancel",
      initialState,
    });
    await runFor(Harness, 250);

    // 用户主动取消产生的 interrupted 必须归入 cancelled，不能报成任务失败。
    expect(mirror.current().status).toBe("任务已取消");
    const pending = mirror.current().pendingConversations.get("ws_cancel::ses_cancel") ?? [];
    expect(pending[0]?.turnStatus).toBe("cancelled");
  });

  test("半死连接在默认空闲阈值后写出可见断开诊断，而不是无限挂起", async () => {
    const port = 49_723;
    jest.useFakeTimers();
    installTestWindow(port);
    installGatewayFetch(({ path }) => {
      if (path.includes("/message-stream")) {
        // 建立连接后一个字节都不再发送：进程被 SIGSTOP / TCP 半开 / 网络黑洞。
        return new Response(
          new ReadableStream<Uint8Array>({ start() {} }),
          { status: 200, headers: { "content-type": "text/event-stream" } },
        );
      }
      return undefined;
    }, { token: "ms-idle-token" });

    const { mirror, Harness } = streamHarness({
      port,
      sessionId: "ses_idle_visible",
      turnId: "turn_idle_visible",
      workspaceId: "ws_idle_visible",
    });
    const { unmount } = await renderHarness(Harness);
    // 逐轮推进计时器直到连接建立：凭据 + 流两条请求都要落地，且全程无业务字节。
    for (let round = 0; round < 20 && streamOf(mirror)?.connectionStatus !== "connected"; round += 1) {
      await act(async () => {
        jest.advanceTimersByTime(200);
        for (let micro = 0; micro < 50; micro += 1) await Promise.resolve();
      });
    }
    expect(streamOf(mirror)?.connectionStatus).toBe("connected");

    await act(async () => {
      // 越过全仓唯一的 SSE 空闲阈值：无字节连接必须响亮失败并留下可见诊断。
      jest.advanceTimersByTime(SSE_IDLE_TIMEOUT_MS);
      for (let round = 0; round < 60; round += 1) await Promise.resolve();
    });

    expect(streamOf(mirror)?.connectionStatus).toBe("disconnected");
    expect(streamOf(mirror)?.protocolError).toContain("未收到任何数据");
    unmount();
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

    const { Harness } = streamHarness({
      port,
      sessionId: "ses_abort",
      turnId: "turn_abort",
      workspaceId: "ws_abort",
    });
    const { unmount } = await renderHarness(Harness);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 200));
    });

    // 连接已建立且仍在途，卸载必须主动取消它，否则长连接会泄漏。
    expect(streamSignal).not.toBeNull();
    const signal = streamSignal as unknown as AbortSignal;
    expect(signal.aborted).toBe(false);
    unmount();
    expect(signal.aborted).toBe(true);
  });

  test("gap 态主动读取一次权威快照补齐，同一缺口不重试也不轮询", async () => {
    const port = 49_724;
    installTestWindow(port);
    // 连接保持打开：1、2 连续，随后直接跳到 5，制造一个本连接内的事件序号缺口。
    // 这条路径没有任何错误分支可命中（连接没断），旧实现只能被动等 service 补发
    // stream.snapshot，页面会永久停在缺口态。
    installGatewayFetch(({ path }) => {
      if (!path.includes("/message-stream")) return undefined;
      const encoder = new TextEncoder();
      return new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(encoder.encode([
            sseBlock(streamEvent("gap_rescue", "ses_gap_rescue", "turn_gap_rescue", 1, "stream.opened", { status: "open" })),
            sseBlock(streamEvent("gap_rescue", "ses_gap_rescue", "turn_gap_rescue", 2, "model.started", { model_call_id: "call_gap", attempt: 1 })),
            sseBlock(streamEvent("gap_rescue", "ses_gap_rescue", "turn_gap_rescue", 5, "block.delta", { block_id: "block_gap", operation: "append", text: "缺口后" })),
          ].join("")));
          // 该乱序帧在兜底快照（seq 5）落地、兜底锁存之后才到达：同一缺口内的
          // 第二次乱序不得再次触发兜底请求。
          globalThis.setTimeout(() => {
            controller.enqueue(encoder.encode(
              sseBlock(streamEvent("gap_rescue", "ses_gap_rescue", "turn_gap_rescue", 8, "block.delta", { block_id: "block_gap", operation: "append", text: "仍在缺口" })),
            ));
          }, 150);
          // 之后保持连接不关闭。
        },
      }), { status: 200, headers: { "content-type": "text/event-stream" } });
    }, { token: "ms-gap-rescue-token" });

    let snapshotRequests = 0;
    const snapshotSpy = spyOn(messageStreamApi, "getSessionMessageStreamSnapshot")
      .mockImplementation(async () => {
        snapshotRequests += 1;
        return streamSnapshot({
          session_id: "ses_gap_rescue",
          turn_id: "turn_gap_rescue",
          turn_stream_id: "strm_gap_rescue",
          snapshot_seq: 5,
        }) as never;
      });

    const { mirror, Harness } = streamHarness({
      port,
      sessionId: "ses_gap_rescue",
      turnId: "turn_gap_rescue",
      workspaceId: "ws_gap_rescue",
    });
    const { unmount } = await renderHarness(Harness);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 500));
    });

    const stream = streamOf(mirror);
    // 缺口必须被主动补齐，而不是无限停在缺口态。这份快照只到 seq 5，未覆盖后续
    // 的乱序事件，因此缺口在兜底之后仍然存在——正是「服务端不补发」的真实场景。
    expect(snapshotRequests).toBe(1);
    expect(stream?.pendingEvents.map((item: { event_seq: number }) => item.event_seq))
      .toEqual([8]);
    expect(stream?.lastEventSeq).toBe(5);
    expect(stream?.connectionStatus).toBe("gap");

    // 再冲刷若干轮：缺口始终未被恢复，同一缺口内不得再次请求快照，证明兜底是
    // 一次性的、绝不重试或轮询。
    for (let round = 0; round < 5; round += 1) {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 30));
      });
    }
    expect(snapshotRequests).toBe(1);

    snapshotSpy.mockRestore();
    unmount();
  });
});
