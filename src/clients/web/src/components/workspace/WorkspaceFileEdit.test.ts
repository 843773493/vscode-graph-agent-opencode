import { afterEach, describe, expect, test } from "bun:test";
import { updateWorkspaceFileContent } from "../../api";
import {
  businessRequest,
  GATEWAY_CURRENT_USER_PATH,
  installWorkspaceFileFetchMock,
  type RecordedRequest,
} from "./workspaceFileRequestMock";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function installSaveBackend(requests: RecordedRequest[]): void {
  installWorkspaceFileFetchMock({
    port: 18_014,
    requests,
    token: "test-token",
    handler: ({ pathname, method }) => {
      if (method === "PUT" && pathname === "/api/v1/workspace/files/content") {
        return Response.json({
          code: 0,
          message: "ok",
          request_id: "req_save",
          data: {
            root_path: "/workspace",
            path: "notes/a b.txt",
            name: "a b.txt",
            content: "after\n",
            language: "plaintext",
            size: 6,
            modified_at: "2026-07-21T00:00:00Z",
            revision: "b".repeat(64),
          },
        });
      }
      return undefined;
    },
  });
}

describe("工作区文件保存请求", () => {
  test("保存按端点路径定位请求，携带方法、目标工作区、凭据与请求体", async () => {
    const requests: RecordedRequest[] = [];
    installSaveBackend(requests);

    const saved = await updateWorkspaceFileContent(
      18_014,
      "notes/a b.txt",
      {
        content: "after\n",
        expected_revision: "a".repeat(64),
      },
      "workspace-test",
    );

    // 业务请求按端点路径选取：users/current 屏障会插在本地凭据与业务请求之间，
    // 按下标取 requests[1] 拿到的是屏障请求而不是保存请求。
    const request = businessRequest(requests, "/api/v1/workspace/files/content");
    expect(request.url).toEndWith(
      "/api/v1/workspace/files/content?path=notes%2Fa+b.txt&scope=workspace",
    );
    expect(request.init?.method).toBe("PUT");
    const headers = new Headers(request.init?.headers);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe("workspace-test");
    expect(headers.get("X-Local-Token")).toBe("test-token");
    expect(request.init?.body).toBe(JSON.stringify({
      content: "after\n",
      expected_revision: "a".repeat(64),
    }));
    expect(saved.revision).toBe("b".repeat(64));
  });

  test("保存请求经过 Gateway 用户会话屏障后才发出", async () => {
    const requests: RecordedRequest[] = [];
    installSaveBackend(requests);

    await updateWorkspaceFileContent(
      18_014,
      "notes/a b.txt",
      { content: "after\n", expected_revision: "a".repeat(64) },
      "workspace-test",
    );

    const barrierIndex = requests.findIndex(
      (entry) => new URL(entry.url).pathname === GATEWAY_CURRENT_USER_PATH,
    );
    const saveIndex = requests.findIndex(
      (entry) => new URL(entry.url).pathname === "/api/v1/workspace/files/content",
    );
    expect(barrierIndex).toBeGreaterThanOrEqual(0);
    expect(saveIndex).toBeGreaterThan(barrierIndex);
  });
});
