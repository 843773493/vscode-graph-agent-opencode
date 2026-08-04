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

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function installFileManagementBackend(requests: Array<{
  init?: RequestInit;
  url: string;
}>): void {
  globalThis.fetch = Object.assign(
    async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      requests.push({ url, init });
      if (new URL(url).pathname === "/api/gateway/auth/local-credential") {
        return Response.json({ data: { token: "file-manager-token" } });
      }
      if (new URL(url).pathname.endsWith("/reveal")) {
        return Response.json({
          request_id: "req_file_reveal",
          data: { path: "/home/hyf/torch_home" },
        });
      }
      return Response.json({
        request_id: "req_file_management",
        data: {
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
    },
    { preconnect: originalFetch.preconnect },
  );
}

describe("文件树管理 API", () => {
  test("外部根目录创建文件夹后使用后端完整目录快照", async () => {
    const requests: Array<{ init?: RequestInit; url: string }> = [];
    installFileManagementBackend(requests);

    const result = await createWorkspaceFileEntry(
      38014,
      "filesystem:/home/hyf",
      { name: "torch_home", kind: "directory" },
      "workspace-files",
    );

    expect(requests[1]?.url).toEndWith(
      "/api/v1/workspace/files/entries?path=%2Fhome%2Fhyf&scope=filesystem",
    );
    expect(requests[1]?.init?.body).toBe(JSON.stringify({
      name: "torch_home",
      kind: "directory",
    }));
    expect(result.path).toBe("filesystem:/home/hyf");
    expect(result.items?.[0]?.path).toBe("filesystem:/home/hyf/torch_home");
  });

  test("粘贴发送多个绝对来源路径", async () => {
    const requests: Array<{ init?: RequestInit; url: string }> = [];
    installFileManagementBackend(requests);

    await pasteWorkspaceFileEntries(
      38015,
      "filesystem:/home/hyf",
      { source_paths: ["/data/model.bin", "/tmp/cache"] },
      "workspace-files",
    );

    expect(requests[1]?.url).toEndWith(
      "/api/v1/workspace/files/paste?path=%2Fhome%2Fhyf&scope=filesystem",
    );
    expect(requests[1]?.init?.body).toBe(JSON.stringify({
      source_paths: ["/data/model.bin", "/tmp/cache"],
    }));
  });

  test("工作区内复制发送带 scope 的来源位置", async () => {
    const requests: Array<{ init?: RequestInit; url: string }> = [];
    installFileManagementBackend(requests);

    await copyWorkspaceFileEntry(
      38018,
      "target",
      { path: "source.txt", scope: "workspace" },
      "workspace-files",
    );

    expect(requests[1]?.url).toEndWith(
      "/api/v1/workspace/files/copy?path=target&scope=workspace",
    );
    expect(requests[1]?.init?.body).toBe(JSON.stringify({
      source_path: "source.txt",
      source_scope: "workspace",
    }));
  });

  test("本地 File 使用 multipart 上传且保留相对路径", async () => {
    const requests: Array<{ init?: RequestInit; url: string }> = [];
    installFileManagementBackend(requests);
    const file = new File(["hello\n"], "hello.txt", { type: "text/plain" });

    await uploadWorkspaceFileEntries(
      38019,
      "uploads",
      [file],
      "workspace-files",
    );

    expect(requests[1]?.url).toEndWith(
      "/api/v1/workspace/files/upload?path=uploads&scope=workspace",
    );
    const body = requests[1]?.init?.body;
    expect(body).toBeInstanceOf(FormData);
    expect((body as FormData).get("relative_paths")).toBe("hello.txt");
    expect(((body as FormData).get("files") as File).name).toBe("hello.txt");
  });

  test("下载请求保留工作区路由和建议文件名", async () => {
    const requests: Array<{ init?: RequestInit; url: string }> = [];
    installFileManagementBackend(requests);

    const request = await createWorkspaceFileDownloadRequest(
      38020,
      "models/model.bin",
      "model.bin",
      "workspace-files",
    );

    expect(request.url).toEndWith(
      "/api/v1/workspace/files/download?path=models%2Fmodel.bin&scope=workspace",
    );
    expect(request.headers["X-BoxTeam-Workspace-Id"]).toBe("workspace-files");
    expect(request.suggestedName).toBe("model.bin");
  });

  test("系统定位使用节点自身路径而不是父目录缓存键", async () => {
    const requests: Array<{ init?: RequestInit; url: string }> = [];
    installFileManagementBackend(requests);

    const result = await revealWorkspaceFileEntry(
      38016,
      "filesystem:/home/hyf/torch_home",
      "workspace-files",
    );

    expect(requests[1]?.url).toEndWith(
      "/api/v1/workspace/files/reveal?path=%2Fhome%2Fhyf%2Ftorch_home&scope=filesystem",
    );
    expect(result.path).toBe("/home/hyf/torch_home");
  });

  test("目录下一页请求携带后端游标", async () => {
    const requests: Array<{ init?: RequestInit; url: string }> = [];
    installFileManagementBackend(requests);

    const result = await getWorkspaceFiles(
      38017,
      "filesystem:/home/hyf",
      "workspace-files",
      undefined,
      "opaque-cursor",
    );

    expect(requests[1]?.url).toEndWith(
      "/api/v1/workspace/files?path=%2Fhome%2Fhyf&scope=filesystem&cursor=opaque-cursor",
    );
    expect(result.next_cursor).toBe("next-page");
  });
});
