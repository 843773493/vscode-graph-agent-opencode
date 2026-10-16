import { afterEach, describe, expect, test } from "bun:test";

import { streamWorkspaceFileEvents } from "../../api";
import type { WorkspaceFileChangeBatchDTO } from "../../protocol/jsonTypes";

const originalFetch = globalThis.fetch;

function streamResponse(chunks: string[], init: RequestInit | undefined): Response {
  const encoder = new TextEncoder();
  const subscription = (JSON.parse(String(init?.body)) as { subscriptions: [{ subscription_id: string; files_after?: number }] }).subscriptions[0];
  const envelope = (state: string, frame?: string) => encoder.encode(`event: observation\ndata: ${JSON.stringify({
    subscription_id: subscription.subscription_id, state,
    ...(frame === undefined ? { cursor: String(subscription.files_after ?? 0) } : { frame }),
  })}\n\n`);
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(envelope("connected"));
      for (const chunk of chunks) controller.enqueue(envelope("frame", chunk));
      controller.close();
    },
  }), {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("工作区文件监听 SSE 客户端", () => {
  test("发送快捷路径并解析批量文件变化", async () => {
    const port = 48_101;
    let requestCount = 0;
    let streamRequest: RequestInit | undefined;
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      requestCount += 1;
      if (requestCount === 1) {
        return Response.json({ data: { token: "watch-token" } });
      }
      streamRequest = args[1];
      return streamResponse([
        ": heartbeat\n\n",
        "event: changes\ndata: {\"overflow\":false,\"directories\":[],\"event_id\":7,\"invalidated_roots\":[],\"snapshots\":[],\"changes\":[{\"kind\":\"edit\",\"path\":\"/workspace/a.ts\"}]}\n\n",
      ], args[1]);
    }, { preconnect: originalFetch.preconnect });
    const batches: WorkspaceFileChangeBatchDTO[] = [];
    let connected = false;

    await streamWorkspaceFileEvents(port, ["/workspace/shortcut"], {
      workspaceId: "gw_watch",
      onBatch: (batch) => batches.push(batch),
      onConnected: () => {
        connected = true;
      },
    });

    expect(JSON.parse(String(streamRequest?.body)).subscriptions).toEqual([{
      subscription_id: expect.any(String), kind: "workspace_files", paths: ["/workspace/shortcut"],
    }]);
    expect(new Headers(streamRequest?.headers).get("X-BoxTeam-Workspace-Id"))
      .toBe("gw_watch");
    expect(connected).toBe(true);
    expect(batches).toEqual([{
      overflow: false, directories: [], event_id: 7, invalidated_roots: [], snapshots: [],
      changes: [{ kind: "edit", path: "/workspace/a.ts" }],
    }]);
  });

  test("服务端监听错误会直接暴露", async () => {
    const port = 48_102;
    let requestCount = 0;
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      requestCount += 1;
      if (requestCount === 1) {
        return Response.json({ data: { token: "watch-token" } });
      }
      return streamResponse([
        "event: error\ndata: {\"message\":\"watch failed\"}\n\n",
      ], args[1]);
    }, { preconnect: originalFetch.preconnect });

    await expect(streamWorkspaceFileEvents(port, [])).rejects.toThrow("watch failed");
  });

  test("发送durable cursor且保留非空配对目录快照", async () => {
    let requests = 0;
    let init: RequestInit | undefined;
    const key = { workspace_id: "workspace-sse", scope: "workspace", root: "/workspace", path: "src" };
    const batch = { changes: [], overflow: false, event_id: 31, invalidated_roots: [],
      directories: [{ directory_key: key, directory_revision: 9, exists: true }],
      snapshots: [{ root_path: "/workspace", path: "src", directory_key: key, directory_revision: 9,
        items: [{ name: "a.ts", path: "src/a.ts", kind: "file" }], truncated: false, limit: 500, next_cursor: null }] };
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      requests += 1;
      if (requests === 1) return Response.json({ data: { token: "paired-watch-token" } });
      init = args[1];
      return streamResponse([`event: changes\ndata: ${JSON.stringify(batch)}\n\n`], args[1]);
    }, { preconnect: originalFetch.preconnect });
    const batches: WorkspaceFileChangeBatchDTO[] = [];
    await streamWorkspaceFileEvents(48_103, ["/workspace/src"], {
      workspaceId: "gw-paired", afterEventId: 30, onBatch: (value) => batches.push(value),
    });
    expect(JSON.parse(String(init?.body)).subscriptions).toEqual([{
      subscription_id: expect.any(String), kind: "workspace_files", paths: ["/workspace/src"], files_after: 30,
    }]);
    expect(batches).toEqual([batch]);
  });
});
