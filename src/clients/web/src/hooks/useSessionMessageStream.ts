import { useEffect, useRef } from "react";
import {
  getSessionMessageStreamSnapshot,
  MessageStreamCursorGoneError,
  MessageStreamConnectionError,
  streamSessionMessageEvents,
} from "../api/sessionMessageStream";
import {
  applyMessageStreamEvent,
  applyMessageStreamSnapshot,
  createMessageStreamState,
  writeMessageStreamCache,
  type MessageStreamEvent,
  type MessageStreamState,
} from "../state/messageStream";
import { cloneMaps } from "../state/appStateMaps";
import { completePendingForJob } from "../state/conversations";
import type { SetAppState } from "./sessionEventStream/sessionRefresh";
import { sessionStreamReconnectDelay } from "./sessionEventStreamPolicy";
import { waitForReconnect } from "./waitForReconnect";

export function useSessionMessageStream({
  apiPort,
  sessionId,
  turnId,
  workspaceId,
  sessionCacheKey,
  setState,
}: {
  apiPort: number | null;
  sessionId: string | null;
  turnId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  setState: SetAppState;
}) {
  const streamAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    streamAbortRef.current?.abort();
    streamAbortRef.current = null;
    if (!apiPort || !sessionId || !turnId || !sessionCacheKey) return;

    const controller = new AbortController();
    streamAbortRef.current = controller;
    const targetSessionId = sessionId;
    const targetTurnId = turnId;
    const targetWorkspaceId = workspaceId;
    const pendingStreamKey = `pending:${targetSessionId}:${targetTurnId}`;
    let lastEventSeq = 0;
    let turnStreamId: string | null = null;
    let terminalSeen = false;
    let terminalStateApplied = false;
    let terminalStatus: MessageStreamState["streamStatus"] = "open";
    let terminalFailure: MessageStreamState["failure"] = null;
    let reconnectAttempt = 0;
    let notReadyAttempts = 0;

    const updateState = (update: (current: MessageStreamState) => MessageStreamState) => {
      setState((previous) => {
        const next = cloneMaps(previous);
        const messageStreams = next.messageStreamsByTurnStream ?? new Map();
        next.messageStreamsByTurnStream = messageStreams;
        const currentKey = turnStreamId ?? pendingStreamKey;
        const existingEntry = [...messageStreams.entries()].find(([key, value]) =>
          key === currentKey
          || (value.sessionId === targetSessionId && value.turnId === targetTurnId),
        );
        const existing = existingEntry?.[1];
        const current = existing && existing.turnId === targetTurnId
          ? existing
          : createMessageStreamState(targetSessionId, targetTurnId);
        const updated = update(current);
        const resolvedKey = updated.turnStreamId || currentKey;
        next.messageStreamsByTurnStream = writeMessageStreamCache(
          messageStreams,
          resolvedKey,
          updated,
        );
        return next;
      });
    };

    const markConnection = (status: MessageStreamState["connectionStatus"]) => {
      updateState((current) => ({ ...current, connectionStatus: status }));
    };

    const notifyTerminal = () => {
      if (!terminalSeen || terminalStateApplied) return;
      terminalStateApplied = true;
      setState((previous) => {
        const next = cloneMaps(previous);
        if (previous.activeJobIdsBySession.get(sessionCacheKey) === targetTurnId) {
          next.activeJobIdsBySession.delete(sessionCacheKey);
        }
        completePendingForJob(
          next.pendingConversations,
          targetSessionId,
          targetTurnId,
          terminalStatus === "completed"
            ? "completed"
            : terminalStatus === "interrupted"
              ? "cancelled"
              : "failed",
          sessionCacheKey,
        );
        if (terminalStatus === "failed" && terminalFailure?.message) {
          next.status = `任务失败: ${terminalFailure.message}`;
        } else if (terminalStatus === "interrupted") {
          next.status = "任务已取消";
        }
        return next;
      });
    };

    // 先建立消息流展示镜像；聊天主时间线在消息流尚未连接时只显示连接状态。
    updateState((current) => ({
      ...current,
      connectionStatus: "connecting",
    }));

    const applyEvent = (event: MessageStreamEvent) => {
      turnStreamId = event.turn_stream_id;
      // React 的函数式 setState updater 可能在当前回调返回后才执行，不能
      // 在 updater 内给这里的游标和终态变量赋值。否则真实浏览器会一直用旧
      // 的 after_seq 重连，并在 SSE 结束时才一次性从历史看到完整响应。
      lastEventSeq = Math.max(lastEventSeq, event.event_seq);
      updateState((current) => applyMessageStreamEvent(current, event));

      const eventTerminalStatus = terminalStatusFromEvent(event);
      if (eventTerminalStatus) {
        terminalSeen = true;
        terminalStatus = eventTerminalStatus;
        terminalFailure = failureFromEvent(event);
        // 终态由事件本身确定，不应等待 SSE 连接自然关闭；服务端可能在
        // 发送终态后继续保持连接，前端仍必须立即允许下一条消息。
        notifyTerminal();
      }
    };

    const applySnapshot = async () => {
      const snapshot = await getSessionMessageStreamSnapshot(
        apiPort,
        targetSessionId,
        targetTurnId,
        {
          workspaceId: targetWorkspaceId,
          turnStreamId,
          signal: controller.signal,
        },
      );
      const snapshotTurnStreamId = snapshot.turn_stream_id;
      if (typeof snapshotTurnStreamId === "string" && snapshotTurnStreamId) {
        turnStreamId = snapshotTurnStreamId;
      }
      updateState((current) => applyMessageStreamSnapshot(current, snapshot));
      lastEventSeq = Math.max(lastEventSeq, snapshot.snapshot_seq);
      terminalSeen = snapshot.stream_status === "completed"
        || snapshot.stream_status === "interrupted"
        || snapshot.stream_status === "failed";
      if (terminalSeen) {
        terminalStatus = snapshot.stream_status;
        terminalFailure = snapshot.failure
          ? {
            code: snapshot.failure.code,
            message: snapshot.failure.message,
            afterInterruptRequested: snapshot.failure.after_interrupt_requested ?? false,
            resumable: snapshot.failure.resumable ?? false,
          }
          : null;
        notifyTerminal();
      }
    };

    const connect = async () => {
      while (!controller.signal.aborted) {
        try {
          markConnection("connecting");
          await streamSessionMessageEvents(
            apiPort,
            targetSessionId,
            targetTurnId,
            {
              workspaceId: targetWorkspaceId,
              turnStreamId,
              afterSeq: lastEventSeq,
              signal: controller.signal,
              onActivity: () => {
                reconnectAttempt = 0;
              },
              onConnected: (resolvedStreamId) => {
                notReadyAttempts = 0;
                turnStreamId = resolvedStreamId ?? turnStreamId;
                updateState((current) => ({
                  ...current,
                  turnStreamId: turnStreamId ?? current.turnStreamId,
                  connectionStatus: "connected",
                }));
              },
              onEvent: applyEvent,
            },
          );
          // 先让 fetch/SSE 完整消费终态帧，再清理活动 Job，避免最后一个
          // 正常响应被流清理误判为 ERR_ABORTED。
          notifyTerminal();
        } catch (error) {
          if (controller.signal.aborted) return;
          if (error instanceof MessageStreamCursorGoneError) {
            try {
              await applySnapshot();
              if (terminalSeen) {
                notifyTerminal();
                return;
              }
              continue;
            } catch (snapshotError) {
              if (controller.signal.aborted) return;
              updateState((current) => ({
                ...current,
                connectionStatus: "disconnected",
                protocolError: snapshotError instanceof Error
                  ? snapshotError.message
                  : String(snapshotError),
              }));
            }
          } else {
            if (error instanceof MessageStreamConnectionError && error.status === 404) {
              notReadyAttempts += 1;
              if (notReadyAttempts > 5) {
                updateState((current) => ({
                  ...current,
                  connectionStatus: "disconnected",
                  protocolError: "Turn 消息流在有限重试后仍不可用: HTTP 404",
                }));
                return;
              } else {
                markConnection("connecting");
              }
            } else {
              markConnection("disconnected");
            }
          }
        }
        if (terminalSeen || controller.signal.aborted) return;
        await waitForReconnect(
          controller.signal,
          sessionStreamReconnectDelay(reconnectAttempt),
        );
        reconnectAttempt += 1;
      }
    };
    // StrictMode 会在开发/热更新探测时先执行一次 effect cleanup。延迟首个
    // 网络订阅可以让这次探测在发出 fetch 前结束，避免真实 SSE 被主动 abort。
    const connectTimerId = window.setTimeout(() => {
      void connect();
    }, 120);

    return () => {
      window.clearTimeout(connectTimerId);
      controller.abort();
      if (streamAbortRef.current === controller) streamAbortRef.current = null;
    };
  }, [apiPort, sessionId, turnId, workspaceId, sessionCacheKey, setState]);
}

type TerminalStreamStatus = Extract<
  MessageStreamState["streamStatus"],
  "completed" | "interrupted" | "failed"
>;

function terminalStatusFromEvent(event: MessageStreamEvent): TerminalStreamStatus | null {
  if (
    event.type === "stream.completed"
    || event.type === "stream.interrupted"
    || event.type === "stream.failed"
  ) {
    return event.type.slice("stream.".length) as TerminalStreamStatus;
  }
  if (event.type !== "stream.snapshot") return null;
  const streamStatus = event.payload.stream_status;
  return streamStatus === "completed"
    || streamStatus === "interrupted"
    || streamStatus === "failed"
    ? streamStatus
    : null;
}

function failureFromEvent(event: MessageStreamEvent): MessageStreamState["failure"] {
  if (event.type === "stream.snapshot") {
    const failure = event.payload.failure;
    return failure
      ? {
        code: failure.code,
        message: failure.message,
        afterInterruptRequested: failure.after_interrupt_requested ?? false,
        resumable: failure.resumable ?? false,
      }
      : null;
  }
  const message = stringValue(event.payload.message);
  if (!message) return null;
  return {
    code: stringValue(event.payload.code) ?? "message_stream_failure",
    message,
    afterInterruptRequested: event.payload.after_interrupt_requested === true,
    resumable: event.payload.resumable === true,
  };
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}
