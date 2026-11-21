import { afterEach, expect, test } from "bun:test";
import { createWorkspaceFileDownloadRequest } from "../api/workspaceFilesystem";
import { HttpRequestError, invalidateGatewayToken } from "../api/http";
import { gatewayWorkspaceKey } from "../api/workspaceIdentity";
import { browserFileTransferHost } from "./fileTransferHost";

const restoreFns: Array<() => void> = [];
afterEach(() => { for (const restore of restoreFns.splice(0)) restore(); });

test("下载执行时读取凭据，401 刷新一次并保留 owner、正文和中文文件名", async () => {
  const port = 48_685;
  invalidateGatewayToken(port);
  const originalFetch = globalThis.fetch;
  const originalCreateUrl = URL.createObjectURL;
  const originalRevokeUrl = URL.revokeObjectURL;
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const originalSetTimeout = globalThis.setTimeout;
  let credentials = 0;
  const tokens: Array<string | null> = [];
  let clicked = 0;
  let removed = 0;
  let downloaded: Blob | undefined;
  let releaseUrl: (() => void) | undefined;
  const revoked: string[] = [];
  const anchor = { href: "", download: "", style: { display: "" },
    click() { clicked += 1; }, remove() { removed += 1; } };
  Object.defineProperty(globalThis, "document", { configurable: true, value: {
    createElement(name: string) { expect(name).toBe("a"); return anchor; },
    body: { appendChild(value: unknown) { expect(value).toBe(anchor); } },
  } });
  URL.createObjectURL = value => { downloaded = value as Blob; return "blob:download"; };
  URL.revokeObjectURL = value => { revoked.push(value); };
  globalThis.setTimeout = ((handler: TimerHandler, delay?: number, ...args: unknown[]) => {
    if (delay === 30_000) { releaseUrl = handler as () => void; return 0; }
    return originalSetTimeout(handler, delay, ...args);
  }) as typeof setTimeout;
  const owner = { gatewayId: "gateway-download", workspaceId: "00000000-0000-4000-8000-000000000085" };
  const uri = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root/file.txt`;
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/gateway/auth/local-credential") {
      return Response.json({ data: { token: ++credentials === 1 ? "old-token" : "new-token" } });
    }
    expect(url.pathname).toBe("/api/v1/workspace/files/download");
    expect(url.searchParams.get("path")).toBe(uri);
    const headers = new Headers(init?.headers);
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(owner.gatewayId);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(owner.workspaceId);
    tokens.push(headers.get("X-Local-Token"));
    return tokens.length === 1
      ? Response.json({ detail: "invalid local token" }, { status: 401 })
      : new Response("中文正文😀", { headers: {
        "Content-Disposition": "attachment; filename*=UTF-8''%E6%96%87%E4%BB%B6.txt",
      } });
  }, { preconnect: originalFetch.preconnect });
  restoreFns.push(() => {
    globalThis.fetch = originalFetch;
    URL.createObjectURL = originalCreateUrl;
    URL.revokeObjectURL = originalRevokeUrl;
    globalThis.setTimeout = originalSetTimeout;
    if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument);
    else Reflect.deleteProperty(globalThis, "document");
  });

  const request = createWorkspaceFileDownloadRequest(port, uri, "fallback.txt", gatewayWorkspaceKey(owner));
  expect(credentials).toBe(0);
  await browserFileTransferHost.downloadWorkspaceFile(request);
  expect(credentials).toBe(2);
  expect(tokens).toEqual(["old-token", "new-token"]);
  expect(await downloaded?.text()).toBe("中文正文😀");
  expect(anchor.href).toBe("blob:download");
  expect(anchor.download).toBe("文件.txt");
  expect(clicked).toBe(1);
  expect(removed).toBe(1);
  expect(releaseUrl).toBeDefined();
  releaseUrl!();
  expect(revoked).toEqual(["blob:download"]);
});

test("下载失败保留统一 HTTP 错误的结构化详情与 request_id，不重试普通拒绝", async () => {
  const port = 48_686;
  invalidateGatewayToken(port);
  const originalFetch = globalThis.fetch;
  let downloads = 0;
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
    if (new URL(String(input), "http://127.0.0.1").pathname === "/api/gateway/auth/local-credential") {
      return Response.json({ data: { token: "valid-token" } });
    }
    downloads += 1;
    return Response.json({ detail: { reason: "没有下载权限" } }, {
      status: 403, headers: { "X-Request-ID": "req-download-denied" },
    });
  }, { preconnect: originalFetch.preconnect });
  restoreFns.push(() => { globalThis.fetch = originalFetch; });
  try {
    await browserFileTransferHost.downloadWorkspaceFile(createWorkspaceFileDownloadRequest(port, "file.txt", "file.txt"));
    throw new Error("下载失败不得伪造成功");
  } catch (error) {
    expect(error).toBeInstanceOf(HttpRequestError);
    expect((error as HttpRequestError).detail).toEqual({ reason: "没有下载权限" });
    expect((error as HttpRequestError).requestId).toBe("req-download-denied");
    expect((error as Error).message).not.toContain("[object Object]");
  }
  expect(downloads).toBe(1);
});
