import { afterEach, describe, expect, test } from "bun:test";
import { getWorkspaceRawFileBlob } from "../../api";
import { invalidateGatewayUserSession } from "../../api/http";
import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { awaitWithAbort } from "../../utils/abortable";
import {
  businessRequest,
  GATEWAY_CURRENT_USER_PATH,
  installWorkspaceFileFetchMock,
  type RecordedRequest,
} from "./workspaceFileRequestMock";

const originalFetch = globalThis.fetch;
const RAW_OWNER = { gatewayId: "gateway-raw", workspaceId: "00000000-0000-4000-8000-000000000063" };
const RANGE_OWNER = { gatewayId: "gateway-range", workspaceId: "00000000-0000-4000-8000-000000000064" };

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("工作区原始文件 API", () => {
  test("携带 Gateway 凭据和目标工作区读取带空格的相对路径", async () => {
    const requests: RecordedRequest[] = [];
    installWorkspaceFileFetchMock({
      port: 28_014,
      requests,
      token: "raw-token",
      handler: ({ pathname }) => {
        if (pathname === "/api/v1/workspace/files/raw") {
          return new Response(new Uint8Array([137, 80, 78, 71]), {
            status: 200,
            headers: { "Content-Type": "image/png" },
          });
        }
        return undefined;
      },
    });

    const blob = await getWorkspaceRawFileBlob(
      28_014,
      "docs/images/a b.png",
      gatewayWorkspaceKey(RAW_OWNER),
    );

    const request = businessRequest(requests, "/api/v1/workspace/files/raw");
    expect(request.url).toEndWith(
      "/api/v1/workspace/files/raw?path=docs%2Fimages%2Fa+b.png&scope=workspace",
    );
    const headers = new Headers(request.init?.headers);
    expect(headers.get("X-Local-Token")).toBe("raw-token");
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(RAW_OWNER.gatewayId);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(RAW_OWNER.workspaceId);
    expect(blob.type).toBe("image/png");
    expect(blob.size).toBe(4);
    // 二进制下载显式跳过用户会话屏障，这正是本文件不受下标漂移影响的根因：
    // 序列只有「本地凭据 → raw」两条，requests[1] 恰好是业务请求。
    expect(
      requests.some(
        (entry) => new URL(entry.url).pathname === GATEWAY_CURRENT_USER_PATH,
      ),
    ).toBe(false);
  });

  test("文件系统快捷路径使用绝对路径作用域", async () => {
    const requests: RecordedRequest[] = [];
    installWorkspaceFileFetchMock({
      port: 28_015,
      requests,
      token: "raw-token",
      handler: ({ pathname }) => {
        if (pathname === "/api/v1/workspace/files/raw") {
          return new Response("external", { status: 200 });
        }
        return undefined;
      },
    });

    await getWorkspaceRawFileBlob(
      28_015,
      "C:\\Models\\model.bin",
      gatewayWorkspaceKey(RAW_OWNER),
    );

    expect(businessRequest(requests, "/api/v1/workspace/files/raw").url).toEndWith(
      "/api/v1/workspace/files/raw?path=C%3A%5CModels%5Cmodel.bin&scope=filesystem",
    );
  });

  test.each([206, 200])("媒体范围请求必须保持目标身份并收到 206（实际 %i）", async (status) => {
    const requests: RecordedRequest[] = [];
    installWorkspaceFileFetchMock({
      port: 28_016,
      requests,
      token: "raw-token",
      handler: ({ pathname }) => pathname === "/api/v1/workspace/files/raw"
        ? new Response(new Uint8Array([2, 3]), { status }) : undefined,
    });
    const reading = getWorkspaceRawFileBlob(28_016, "audio.wav", gatewayWorkspaceKey(RANGE_OWNER), undefined, { start: 1, end: 2 });
    if (status === 206) expect(Array.from(new Uint8Array(await (await reading).arrayBuffer()))).toEqual([2, 3]);
    else await expect(reading).rejects.toThrow("没有按请求返回字节范围");
    const request = businessRequest(requests, "/api/v1/workspace/files/raw");
    const headers = new Headers(request.init?.headers);
    expect(headers.get("Range")).toBe("bytes=1-2");
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(RANGE_OWNER.gatewayId);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(RANGE_OWNER.workspaceId);
    expect(headers.get("X-Local-Token")).toBe("raw-token");
  });
});

/** 专门核验真实 HTTP 的响应头／正文生命周期；不是产品 Gateway 或文件 owner 装具。 */
async function withPendingRawBody(check: (context: {
  port: number;
  started: Promise<void>;
  disconnected: Promise<void>;
  request: () => { url: URL; headers: Headers; signal: AbortSignal };
  complete: () => void;
}) => Promise<void>, responseStatus?: 200 | 206): Promise<void> {
  const started = Promise.withResolvers<void>(), disconnected = Promise.withResolvers<void>();
  let body: ReadableStreamDefaultController<Uint8Array> | undefined;
  let rawRequest: { url: URL; headers: Headers; signal: AbortSignal } | undefined;
  let closed = false;
  const server = Bun.serve({ hostname: "127.0.0.1", port: 0, idleTimeout: 0,
    fetch(request) {
      const url = new URL(request.url);
      if (url.pathname === "/api/gateway/auth/local-credential") return Response.json({ data: { token: "raw-lifetime-token" } });
      if (url.pathname !== "/api/v1/workspace/files/raw") throw new Error(`未声明的真实 HTTP 请求：${url.pathname}`);
      request.signal.addEventListener("abort", () => disconnected.resolve(), { once: true });
      return new Response(new ReadableStream<Uint8Array>({
        start(controller) { body = controller; controller.enqueue(new Uint8Array([1])); },
        cancel() { closed = true; disconnected.resolve(); },
      }), { status: responseStatus ?? (request.headers.has("Range") ? 206 : 200),
        headers: { "Content-Type": "application/octet-stream", "X-Request-ID": "req-raw-lifetime" } });
    },
  });
  const complete = () => {
    if (!closed && body) { closed = true; body.enqueue(new Uint8Array([2])); body.close(); }
  };
  globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
    const response = await originalFetch(...args);
    const url = new URL(String(args[0]));
    if (url.pathname === "/api/v1/workspace/files/raw") {
      rawRequest = { url, headers: new Headers(args[1]?.headers), signal: args[1]!.signal! };
      const blob = response.blob.bind(response);
      // 只观察实际消费阶段，不替换 fetch、字节正文或取消协议。
      response.blob = () => { started.resolve(); return blob(); };
    }
    return response;
  }, { preconnect: originalFetch.preconnect });
  try {
    await check({ port: server.port!, started: started.promise, disconnected: disconnected.promise,
      request: () => { if (!rawRequest) throw new Error("raw 请求尚未开始"); return rawRequest; }, complete });
  } finally {
    complete();
    await server.stop(true);
    globalThis.fetch = originalFetch;
  }
}

describe("普通文件真实 HTTP 正文的取消与身份", () => {
  const owner = { gatewayId: "gateway-raw-lifetime", workspaceId: "00000000-0000-4000-8000-000000000062" };
  const path = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root/body.bin`;
  test.each([
    ["调用方取消／完整正文", false, false], ["调用方取消／Range", false, true],
    ["身份失效／完整正文", true, false], ["身份失效／Range", true, true],
  ] as const)("%s 在响应头之后仍撤销读取", async (_label, invalidated, range) => {
    await withPendingRawBody(async context => {
      const controller = new AbortController(), reason = new Error("原调用方终止正文");
      const reading = getWorkspaceRawFileBlob(context.port, path, gatewayWorkspaceKey(owner), controller.signal,
        range ? { start: 0, end: 1 } : undefined);
      const outcome = reading.then(() => null, (error: unknown) => error);
      try {
        await awaitWithAbort(context.started, AbortSignal.timeout(1_000));
        expect(context.request().signal.aborted).toBe(false);
        if (invalidated) invalidateGatewayUserSession(context.port);
        else controller.abort(reason);
        const error = await awaitWithAbort(outcome, AbortSignal.timeout(1_000));
        if (invalidated) expect(error).toMatchObject({ name: "AbortError", message: "Gateway 或用户身份已改变，请重新读取文件" });
        else expect(error).toBe(reason);
        expect(context.request().signal.aborted).toBe(true);
        await awaitWithAbort(context.disconnected, AbortSignal.timeout(1_000));
        expect(context.request().url.searchParams.get("path")).toBe(path);
        expect(context.request().headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(owner.gatewayId);
        expect(context.request().headers.get("X-BoxTeam-Workspace-Id")).toBe(owner.workspaceId);
        expect(context.request().headers.get("Range")).toBe(range ? "bytes=0-1" : null);
      } finally { context.complete(); await outcome; }
    });
  });

  test("其它 Gateway 失效不影响当前读取，完成后释放身份监听", async () => {
    await withPendingRawBody(async context => {
      const reading = getWorkspaceRawFileBlob(context.port, path, gatewayWorkspaceKey(owner));
      await awaitWithAbort(context.started, AbortSignal.timeout(1_000));
      invalidateGatewayUserSession(context.port + 1);
      expect(context.request().signal.aborted).toBe(false);
      context.complete();
      const blob = await reading;
      expect(Array.from(new Uint8Array(await blob.arrayBuffer()))).toEqual([1, 2]);
      invalidateGatewayUserSession(context.port);
      expect(context.request().signal.aborted).toBe(false);
    });
  });

  test("服务端忽略 Range 返回 200 时，拒绝并终止未消费的完整正文", async () => {
    await withPendingRawBody(async context => {
      await expect(getWorkspaceRawFileBlob(context.port, path, gatewayWorkspaceKey(owner), undefined, { start: 0, end: 1 }))
        .rejects.toThrow("目标文件服务没有按请求返回字节范围");
      expect(context.request().signal.aborted).toBe(true);
      await awaitWithAbort(context.disconnected, AbortSignal.timeout(1_000));
    }, 200);
  });
});
