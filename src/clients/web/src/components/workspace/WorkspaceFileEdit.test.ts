import { gatewayWorkspaceKey } from "../../api/workspaceIdentity";
import { afterEach, describe, expect, test } from "bun:test";
import { updateWorkspaceFileContent } from "../../api";
import {
  businessRequest,

  installWorkspaceFileFetchMock,
  type RecordedRequest,
} from "./workspaceFileRequestMock";

const FILE_OWNER = { gatewayId: "gateway_00000000000040008000000000000001", workspaceId: "00000000-0000-4000-8000-000000000002" };
const FILE_SELECTION = gatewayWorkspaceKey(FILE_OWNER);

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
            root_path: "boxteam://workspace/workspace-test/resources/files/root",
            path: "boxteam://workspace/workspace-test/resources/files/root/notes/a%20b.txt",
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
      FILE_SELECTION,
    );

    // 按业务端点定位请求，不依赖缓存导致的凭据请求次数。
    const request = businessRequest(requests, "/api/v1/workspace/files/content");
    expect(request.url).toEndWith(
      "/api/v1/workspace/files/content?path=notes%2Fa+b.txt&scope=workspace",
    );
    expect(request.init?.method).toBe("PUT");
    const headers = new Headers(request.init?.headers);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(FILE_OWNER.workspaceId);
    expect(headers.get("X-Local-Token")).toBe("test-token");
    expect(request.init?.body).toBe(JSON.stringify({
      content: "after\n",
      expected_revision: "a".repeat(64),
    }));
    expect(saved.revision).toBe("b".repeat(64));
  });

  test("保存请求仅初始化本机凭据后发出", async () => {
    const requests: RecordedRequest[] = [];
    installSaveBackend(requests);

    await updateWorkspaceFileContent(
      18_014,
      "notes/a b.txt",
      { content: "after\n", expected_revision: "a".repeat(64) },
      FILE_SELECTION,
    );

    const credentialIndex = requests.findIndex(
      (entry) => new URL(entry.url).pathname === "/api/gateway/auth/local-credential",
    );
    const saveIndex = requests.findIndex(
      (entry) => new URL(entry.url).pathname === "/api/v1/workspace/files/content",
    );
    expect(credentialIndex).toBe(0);
    expect(saveIndex).toBe(1);
    expect(requests).toHaveLength(2);
  });
});
