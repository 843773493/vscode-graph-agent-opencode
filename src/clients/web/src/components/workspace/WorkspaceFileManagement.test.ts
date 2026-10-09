import { afterEach, describe, expect, test } from "bun:test";
import {
  copyWorkspaceFileEntry,
  createWorkspaceFileDownloadRequest,
  createWorkspaceFileEntry,
  getWorkspaceFiles,
  pasteWorkspaceFileEntries,
  revealWorkspaceFileEntry,
  uploadWorkspaceFileEntries,
} from "../../api";
import {
  businessRequest,
  GATEWAY_CURRENT_USER_PATH,
  installWorkspaceFileFetchMock,
  type RecordedRequest,
} from "./workspaceFileRequestMock";

import type { WorkspaceFileMutationIntent } from "../../types/backend";

const intent: WorkspaceFileMutationIntent = { client_operation_id: "op_00000000000040008000000000000002",
  client_sequence: 1, expected_directory_revision: 1, depends_on: [], created_by_operation_id: null,
  relative_child_path: "", directory_revision_from_operation_id: null, client_ref: "file-api-unit",
  directory_key: { workspace_id: "backend_files", scope: "filesystem", root: "/", path: "/home/hyf" } };

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

/**
 * 文件树管理的统一后端：屏障隧道由 installWorkspaceFileFetchMock 应答，其余按端点
 * 路径分派；未声明路径一律抛出，绝不静默返回空响应。
 */
function installFileManagementBackend(
  port: number,
  requests: RecordedRequest[],
): void {
  installWorkspaceFileFetchMock({
    port,
    requests,
    token: "file-manager-token",
    handler: ({ pathname }) => {
      if (["/entries", "/paste", "/copy", "/upload"].some((suffix) => pathname.endsWith(suffix))) {
        return Response.json({ request_id: "req_file_accepted", data: { operation_id: intent.client_operation_id,
          state: "queued", effect: "none", directories: [], snapshots: [] } }, { status: 202 });
      }
      if (pathname.endsWith("/reveal")) {
        return Response.json({
          request_id: "req_file_reveal",
          data: { path: "/home/hyf/torch_home" },
        });
      }
      if (pathname.startsWith("/api/v1/workspace/files")) {
        return Response.json({
          request_id: "req_file_management",
          data: {
            directory_key: { workspace_id: "backend_files", scope: "filesystem", root: "/", path: "/home/hyf" },
            directory_revision: 7,
            root_path: "/",
            path: "/home/hyf",
            items: [{
              name: "torch_home",
              path: "/home/hyf/torch_home",
              kind: "directory",
              has_children: true,
              size: null,
              modified_at: null,
            }],
            truncated: false,
            limit: 500,
            next_cursor: "next-page",
          },
        });
      }
      return undefined;
    },
  });
}

describe("文件树管理 API", () => {
  test("外部根目录创建文件夹返回原操作202回执", async () => {
    const requests: RecordedRequest[] = [];
    installFileManagementBackend(38_014, requests);

    const result = await createWorkspaceFileEntry(
      38_014,
      "filesystem:/home/hyf",
      { name: "torch_home", kind: "directory", intent },
      "workspace-files",
    );

    const request = businessRequest(requests, "/api/v1/workspace/files/entries");
    expect(request.url).toEndWith(
      "/api/v1/workspace/files/entries?path=%2Fhome%2Fhyf&scope=filesystem",
    );
    expect(request.init?.body).toBe(JSON.stringify({
      name: "torch_home",
      kind: "directory",
      intent,
    }));
    expect(request.init?.method).toBe("POST");
    expect(result.operation_id).toBe(intent.client_operation_id);
    expect(result.state).toBe("queued");
  });

  test("粘贴发送多个绝对来源路径", async () => {
    const requests: RecordedRequest[] = [];
    installFileManagementBackend(38_015, requests);

    await pasteWorkspaceFileEntries(
      38_015,
      "filesystem:/home/hyf",
      { source_paths: ["/data/model.bin", "/clipboard/cache"], intent },
      "workspace-files",
    );

    const request = businessRequest(requests, "/api/v1/workspace/files/paste");
    expect(request.url).toEndWith(
      "/api/v1/workspace/files/paste?path=%2Fhome%2Fhyf&scope=filesystem",
    );
    expect(request.init?.body).toBe(JSON.stringify({
      source_paths: ["/data/model.bin", "/clipboard/cache"],
      intent,
    }));
    expect(request.init?.method).toBe("POST");
  });

  test("工作区内复制发送带 scope 的来源位置", async () => {
    const requests: RecordedRequest[] = [];
    installFileManagementBackend(38_018, requests);

    await copyWorkspaceFileEntry(
      38_018,
      "target",
      { source_path: "source.txt", source_scope: "workspace", intent },
      "workspace-files",
    );

    const request = businessRequest(requests, "/api/v1/workspace/files/copy");
    expect(request.url).toEndWith(
      "/api/v1/workspace/files/copy?path=target&scope=workspace",
    );
    expect(request.init?.body).toBe(JSON.stringify({
      source_path: "source.txt",
      source_scope: "workspace",
      intent,
    }));
    expect(new Headers(request.init?.headers).get("X-Local-Token"))
      .toBe("file-manager-token");
  });

  test("本地 File 使用 multipart 上传且保留相对路径", async () => {
    const requests: RecordedRequest[] = [];
    installFileManagementBackend(38_019, requests);
    const file = new File(["hello\n"], "hello.txt", { type: "text/plain" });

    await uploadWorkspaceFileEntries(
      38_019,
      "uploads",
      [{ file, name: file.name, relativePath: file.name }],
      intent,
      "workspace-files",
    );

    const request = businessRequest(requests, "/api/v1/workspace/files/upload");
    expect(request.url).toEndWith(
      "/api/v1/workspace/files/upload?path=uploads&scope=workspace",
    );
    const body = request.init?.body;
    expect(body).toBeInstanceOf(FormData);
    expect((body as FormData).get("relative_paths")).toBe("hello.txt");
    expect((body as FormData).get("intent")).toBe(JSON.stringify(intent));
    expect(((body as FormData).get("files") as File).name).toBe("hello.txt");
    // 上传走 skipGatewayUserSession 的 multipart 通路，不建立用户会话屏障。
    expect(
      requests.some(
        (entry) => new URL(entry.url).pathname === GATEWAY_CURRENT_USER_PATH,
      ),
    ).toBe(false);
  });

  test("下载请求保留工作区路由和建议文件名", async () => {
    const requests: RecordedRequest[] = [];
    installFileManagementBackend(38_020, requests);

    const request = await createWorkspaceFileDownloadRequest(
      38_020,
      "models/model.bin",
      "model.bin",
      "workspace-files",
    );

    expect(request.url).toEndWith(
      "/api/v1/workspace/files/download?path=models%2Fmodel.bin&scope=workspace",
    );
    expect(request.headers["X-BoxTeam-Workspace-Id"]).toBe("workspace-files");
    expect(request.headers["X-Local-Token"]).toBe("file-manager-token");
    expect(request.suggestedName).toBe("model.bin");
  });

  test("系统定位使用节点自身路径而不是父目录缓存键", async () => {
    const requests: RecordedRequest[] = [];
    installFileManagementBackend(38_016, requests);

    const result = await revealWorkspaceFileEntry(
      38_016,
      "filesystem:/home/hyf/torch_home",
      "workspace-files",
    );

    const request = businessRequest(requests, "/api/v1/workspace/files/reveal");
    expect(request.url).toEndWith(
      "/api/v1/workspace/files/reveal?path=%2Fhome%2Fhyf%2Ftorch_home&scope=filesystem",
    );
    expect(request.init?.method).toBe("POST");
    expect(result.path).toBe("/home/hyf/torch_home");
  });

  test("目录下一页请求携带后端游标", async () => {
    const requests: RecordedRequest[] = [];
    installFileManagementBackend(38_017, requests);

    const result = await getWorkspaceFiles(
      38_017,
      "filesystem:/home/hyf",
      "workspace-files",
      undefined,
      "opaque-cursor",
    );

    const request = businessRequest(requests, "/api/v1/workspace/files");
    expect(request.url).toEndWith(
      "/api/v1/workspace/files?path=%2Fhome%2Fhyf&scope=filesystem&cursor=opaque-cursor",
    );
    expect(result.next_cursor).toBe("next-page");
  });
});
