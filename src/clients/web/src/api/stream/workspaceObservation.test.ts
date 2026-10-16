import { afterEach, describe, expect, test } from "bun:test";
import { streamSessionActivity } from "../session/sessionActivity";
import { SessionStreamIdleTimeoutError, streamSessionEvents, TraceCursorGoneError } from "./sessionTraceStream";
import { streamWorkspaceFileEvents } from "./workspaceFileEvents";
import type { WorkspaceObservationSubscription } from "../../types/backend";

const originalFetch = globalThis.fetch;
const encoder = new TextEncoder();
type Kind = "session_activity" | "session_trace" | "workspace_files";

interface Connection {
  subscriptions: WorkspaceObservationSubscription[];
  headers: Headers;
  cancelled: boolean;
  controller: ReadableStreamDefaultController<Uint8Array>;
}

function envelope(value: object): Uint8Array {
  return encoder.encode(`event: observation\ndata: ${JSON.stringify(value)}\n\n`);
}

function installBackend(): Connection[] {
  const connections: Connection[] = [];
  globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
    const path = new URL(String(args[0]), "http://127.0.0.1").pathname;
    if (path === "/api/gateway/auth/local-credential") {
      return Response.json({ data: { token: "observation-test-token" } });
    }
    expect(path).toBe("/api/v1/workspace/observations/stream");
    const init = args[1];
    const subscriptions = (JSON.parse(String(init?.body)) as { subscriptions: WorkspaceObservationSubscription[] }).subscriptions;
    let connection!: Connection;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        connection = { subscriptions, headers: new Headers(init?.headers), cancelled: false, controller };
        connections.push(connection);
        for (const subscription of subscriptions) {
          controller.enqueue(envelope({
            subscription_id: subscription.subscription_id, state: "connected",
            ...(subscription.kind === "workspace_files" ? { cursor: String(subscription.files_after ?? 27) } : {}),
          }));
        }
      },
      cancel() { connection.cancelled = true; },
    });
    return new Response(body, { headers: { "Content-Type": "text/event-stream", "X-BoxTeam-Route-Revision": "gateway:9" } });
  }, { preconnect: originalFetch.preconnect });
  return connections;
}

async function waitUntil(check: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (check()) return;
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  throw new Error("观察者测试条件未收敛");
}

function owner(connection: Connection, kind: Kind): WorkspaceObservationSubscription {
  const subscription = connection.subscriptions.find((item) => item.kind === kind);
  if (!subscription) throw new Error(`连接中没有 ${kind}`);
  return subscription;
}

function send(connection: Connection, kind: Kind, frame: string): void {
  connection.controller.enqueue(envelope({ subscription_id: owner(connection, kind).subscription_id, state: "frame", frame }));
}

function activity(cursor: number): string {
  return `id: ${cursor}\nevent: session_activity\ndata: ${JSON.stringify({ event_seq: cursor, event_id: `activity-${cursor}`,
    session_id: "session", status: "completed", summary: "完成", occurred_at: "2026-10-12T00:00:00Z" })}\n\n`;
}

function trace(cursor: string): string {
  return `id: ${cursor}\nevent: trace\ndata: ${JSON.stringify({ event_id: cursor, session_id: "session", job_id: "job",
    timestamp: "2026-10-12T00:00:00Z", type: "job_started", phase: "job", title: "开始", content: "开始", raw: {} })}\n\n`;
}

function files(cursor: number): string {
  return `event: changes\ndata: ${JSON.stringify({ event_id: cursor, overflow: false, directories: [],
    invalidated_roots: [], snapshots: [], changes: [] })}\n\n`;
}

function readers(port: number, workspaceId = "workspace", traceTimeout?: number) {
  const controllers = { activity: new AbortController(), trace: new AbortController(), files: new AbortController() };
  const received = { activity: [] as number[], trace: [] as string[], files: [] as number[] };
  const target = { workspace_id: workspaceId, backend_workspace_id: `backend-${workspaceId}`, connection_kind: "local" } as const;
  const promises = {
    activity: streamSessionActivity(port, target, { after: 4, signal: controllers.activity.signal,
      onEvent: (_event, cursor) => { received.activity.push(cursor); } }),
    trace: streamSessionEvents(port, "session", { workspaceId, afterCursor: "tc1.4", idleTimeoutMs: traceTimeout,
      signal: controllers.trace.signal, onEvent: (_event, cursor) => { received.trace.push(cursor); } }),
    files: streamWorkspaceFileEvents(port, ["shortcut"], { workspaceId, signal: controllers.files.signal,
      onBatch: (batch) => { received.files.push(batch.event_id); } }),
  };
  const outcomes = {
    activity: promises.activity.then(() => undefined, (error: unknown) => error),
    trace: promises.trace.then(() => undefined, (error: unknown) => error),
    files: promises.files.then(() => undefined, (error: unknown) => error),
  };
  return { controllers, received, outcomes, target, async stop() {
    for (const controller of Object.values(controllers)) controller.abort();
    await Promise.all(Object.values(outcomes));
  } };
}

afterEach(() => { globalThis.fetch = originalFetch; });

describe("Workspace 单物理 SSE 观察者", () => {
  test("三个领域入口共享一个准确目标连接并分别交付原事件", async () => {
    const connections = installBackend();
    const active = readers(49_101);
    try {
      await waitUntil(() => connections.length === 1);
      const connection = connections[0];
      expect(connection.subscriptions).toHaveLength(3);
      expect(connection.headers.get("X-BoxTeam-Workspace-Id")).toBe("workspace");
      expect(connection.headers.get("X-BoxTeam-Backend-Workspace-Id")).toBe("backend-workspace");
      expect(owner(connection, "session_activity").activity_after).toBe(4);
      expect(owner(connection, "session_trace").trace_after).toBe("tc1.4");
      send(connection, "session_activity", activity(5));
      send(connection, "session_trace", trace("tc1.5"));
      send(connection, "workspace_files", files(28));
      await waitUntil(() => active.received.files.length === 1);
      expect(active.received).toEqual({ activity: [5], trace: ["tc1.5"], files: [28] });
    } finally { await active.stop(); }
    expect(connections).toHaveLength(1);
    expect(connections[0].cancelled).toBe(true);
  });

  test("取消一个观察者时保留其他独立游标和首次文件high-water", async () => {
    const connections = installBackend();
    const active = readers(49_102);
    try {
      await waitUntil(() => connections.length === 1);
      send(connections[0], "session_activity", activity(5));
      send(connections[0], "session_trace", trace("tc1.5"));
      await waitUntil(() => active.received.trace.length === 1);
      active.controllers.trace.abort();
      await active.outcomes.trace;
      await waitUntil(() => connections.length === 2);
      expect(connections[0].cancelled).toBe(true);
      expect(connections[1].subscriptions).toHaveLength(2);
      expect(owner(connections[1], "session_activity").activity_after).toBe(5);
      expect(owner(connections[1], "workspace_files").files_after).toBe(27);
      send(connections[1], "session_activity", activity(6));
      send(connections[1], "workspace_files", files(28));
      await waitUntil(() => active.received.files.length === 1);
      expect(active.received.activity).toEqual([5, 6]);
    } finally { await active.stop(); }
    expect(connections[1].cancelled).toBe(true);
  });

  test("Trace 410 独立报错，其余两个观察者继续使用原连接", async () => {
    const connections = installBackend();
    const active = readers(49_103);
    try {
      await waitUntil(() => connections.length === 1);
      connections[0].controller.enqueue(envelope({ subscription_id: owner(connections[0], "session_trace").subscription_id,
        state: "error", status: 410, detail_json: JSON.stringify({ code: "trace_cursor_gone" }) }));
      expect(await active.outcomes.trace).toBeInstanceOf(TraceCursorGoneError);
      expect(connections[0].cancelled).toBe(false);
      send(connections[0], "session_activity", activity(5));
      send(connections[0], "workspace_files", files(28));
      await waitUntil(() => active.received.files.length === 1);
      expect(connections).toHaveLength(1);
      expect(active.received.activity).toEqual([5]);
    } finally { await active.stop(); }
  });

  test("其他逻辑流的心跳不能掩盖Trace自身空闲，超时后它们仍能恢复", async () => {
    const connections = installBackend();
    const active = readers(49_104, "workspace", 20);
    try {
      await waitUntil(() => connections.length === 1);
      send(connections[0], "session_activity", ": ping\n\n");
      send(connections[0], "workspace_files", ": heartbeat\n\n");
      expect(await active.outcomes.trace).toBeInstanceOf(SessionStreamIdleTimeoutError);
      await waitUntil(() => connections.length === 2);
      expect(owner(connections[1], "workspace_files").files_after).toBe(27);
      send(connections[1], "session_activity", activity(5));
      send(connections[1], "workspace_files", files(28));
      await waitUntil(() => active.received.files.length === 1);
      expect(active.received.activity).toEqual([5]);
    } finally { await active.stop(); }
  });

  test("物理连接损坏向所有活跃观察者响亮失败并释放连接", async () => {
    const connections = installBackend();
    const active = readers(49_105);
    try {
      await waitUntil(() => connections.length === 1);
      connections[0].controller.error(new TypeError("physical connection lost"));
      for (const error of await Promise.all(Object.values(active.outcomes))) {
        expect(error).toBeInstanceOf(TypeError);
        expect((error as Error).message).toBe("physical connection lost");
      }
      expect(connections).toHaveLength(1);
    } finally { await active.stop(); }
  });

  test("工作区切换使用不同物理scope和游标，关闭旧scope后不接受迟到事件", async () => {
    const connections = installBackend();
    const previous = readers(49_106, "previous");
    const current = readers(49_106, "current");
    try {
      await waitUntil(() => connections.length === 2);
      const oldConnection = connections.find((item) => item.headers.get("X-BoxTeam-Workspace-Id") === "previous")!;
      const newConnection = connections.find((item) => item.headers.get("X-BoxTeam-Workspace-Id") === "current")!;
      send(oldConnection, "session_activity", activity(8));
      send(newConnection, "session_activity", activity(5));
      await waitUntil(() => previous.received.activity.length === 1 && current.received.activity.length === 1);
      await previous.stop();
      expect(oldConnection.cancelled).toBe(true);
      send(newConnection, "session_activity", activity(6));
      await waitUntil(() => current.received.activity.length === 2);
      expect(previous.received.activity).toEqual([8]);
      expect(current.received.activity).toEqual([5, 6]);
    } finally { await Promise.all([previous.stop(), current.stop()]); }
  });

  test("Catalog离开后仍保准确后端身份，新冲突身份明确失败", async () => {
    const connections = installBackend();
    const active = readers(49_107);
    try {
      await waitUntil(() => connections.length === 1);
      active.controllers.activity.abort();
      await active.outcomes.activity;
      await waitUntil(() => connections.length === 2);
      expect(connections[1].headers.get("X-BoxTeam-Backend-Workspace-Id")).toBe("backend-workspace");
      await expect(streamSessionActivity(49_107, { ...active.target, backend_workspace_id: "other-backend" }))
        .rejects.toThrow("Workspace 观察者目标身份冲突");
      send(connections[1], "session_trace", trace("tc1.5"));
      await waitUntil(() => active.received.trace.length === 1);
      expect(connections).toHaveLength(2);
    } finally { await active.stop(); }
  });
});
