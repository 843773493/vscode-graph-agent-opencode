import { afterEach, describe, expect, test } from "bun:test";

import { streamWorkspaceFileEvents } from "./api";

const originalFetch = globalThis.fetch;

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
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
        "event: changes\ndata: {\"overflow\":false,\"changes\":[{\"kind\":\"edit\",\"path\":\"/tmp/a.ts\"}]}\n\n",
      ]);
    }, { preconnect: originalFetch.preconnect });
    const batches: Array<{ overflow: boolean; changes: unknown[] }> = [];
    let connected = false;

    await streamWorkspaceFileEvents(port, ["/tmp/shortcut"], {
      workspaceId: "gw_watch",
      onBatch: (batch) => batches.push(batch),
      onConnected: () => {
        connected = true;
      },
    });

    expect(JSON.parse(String(streamRequest?.body))).toEqual({
      paths: ["/tmp/shortcut"],
    });
    expect(new Headers(streamRequest?.headers).get("X-BoxTeam-Workspace-Id"))
      .toBe("gw_watch");
    expect(connected).toBe(true);
    expect(batches).toEqual([{
      overflow: false,
      changes: [{ kind: "edit", path: "/tmp/a.ts" }],
    }]);
  });

  test("服务端监听错误会直接暴露", async () => {
    const port = 48_102;
    let requestCount = 0;
    globalThis.fetch = Object.assign(async () => {
      requestCount += 1;
      if (requestCount === 1) {
        return Response.json({ data: { token: "watch-token" } });
      }
      return streamResponse([
        "event: error\ndata: {\"message\":\"watch failed\"}\n\n",
      ]);
    }, { preconnect: originalFetch.preconnect });

    await expect(streamWorkspaceFileEvents(port, [])).rejects.toThrow("watch failed");
  });
});
