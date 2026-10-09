import { afterEach, describe, expect, test } from "bun:test";
import {
  getWorkspaceFiles,
  pasteWorkspaceFileEntries,
  uploadWorkspaceFileEntries,
} from "./workspaceFilesystem";

import type { WorkspaceFileMutationIntent } from "../types/backend";

const intent: WorkspaceFileMutationIntent = { client_operation_id: "op_00000000000040008000000000000001",
  client_sequence: 1, expected_directory_revision: 1, depends_on: [], created_by_operation_id: null,
  relative_child_path: "", directory_revision_from_operation_id: null, client_ref: "file-unit",
  directory_key: { workspace_id: "backend_files", scope: "workspace", root: "/repo", path: "dest" } };

const originalFetch = globalThis.fetch;

/** mock fetch：第一次返回 Gateway 本地凭据，之后返回给定的 API 响应。 */
function installFetchMock(respond: () => Response): void {
  let count = 0;
  globalThis.fetch = Object.assign(
    async () => {
      count += 1;
      if (count === 1) {
        return Response.json({ data: { token: "files-token" } });
      }
      return respond();
    },
    { preconnect: originalFetch.preconnect },
  );
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("工作区文件列表 API 的 items 契约", () => {
  test("合法空数组正常返回并完成路径编码", async () => {
    installFetchMock(() => Response.json({
      data: { root_path: "/", path: "/repo", items: [],
        directory_key: { workspace_id: "backend_files", scope: "workspace", root: "/", path: "/repo" }, directory_revision: 1 },
      request_id: "req-files-empty",
    }));

    await expect(getWorkspaceFiles(48_310, "/repo", "workspace-1")).resolves
      .toMatchObject({ path: "/repo", items: [] });
  });

  test("items 非数组：响亮失败而不是静默收敛为空列表", async () => {
    installFetchMock(() => Response.json({
      data: { root_path: "/", path: "/repo", items: "NOT-AN-ARRAY" },
      request_id: "req-files-broken-items",
    }));

    await expect(getWorkspaceFiles(48_311, "/repo", "workspace-1")).rejects
      .toThrow("工作区文件列表响应 items 必须是数组");
  });

  test("items 为 null：响亮失败而不是被 ?? [] 静默吸收", async () => {
    installFetchMock(() => Response.json({
      data: { root_path: "/", path: "/repo", items: null },
      request_id: "req-files-null-items",
    }));

    await expect(getWorkspaceFiles(48_312, "/repo", "workspace-1")).rejects
      .toThrow("工作区文件列表响应 items 必须是数组");
  });
});

describe("批量文件操作的条目上限前置校验", () => {
  test("上传超过 100 个文件时在发请求前失败并说明分批", async () => {
    let requests = 0;
    globalThis.fetch = Object.assign(async () => {
      requests += 1;
      return Response.json({ data: { token: "t" } });
    }, { preconnect: originalFetch.preconnect });
    const files = Array.from({ length: 101 }, (_, index) =>
      ({ file: new File(["x"], `f${index}.txt`), name: `f${index}.txt`, relativePath: `f${index}.txt` }));

    await expect(uploadWorkspaceFileEntries(48_320, "dest", files, intent)).rejects
      .toThrow("上传一次最多 100 项，当前 101 项，请分批操作");
    expect(requests).toBe(0);
  });

  test("上传恰好 100 个文件仍放行", async () => {
    installFetchMock(() => Response.json({
      data: { operation_id: intent.client_operation_id, state: "queued", effect: "none" },
      request_id: "req-files-upload-100",
    }));
    const files = Array.from({ length: 100 }, (_, index) =>
      ({ file: new File(["x"], `f${index}.txt`), name: `f${index}.txt`, relativePath: `f${index}.txt` }));

    await expect(uploadWorkspaceFileEntries(48_321, "dest", files, intent)).resolves
      .toMatchObject({ operation_id: intent.client_operation_id, state: "queued" });
  });

  test("粘贴超过 100 个来源时在发请求前失败并说明分批", async () => {
    let requests = 0;
    globalThis.fetch = Object.assign(async () => {
      requests += 1;
      return Response.json({ data: { token: "t" } });
    }, { preconnect: originalFetch.preconnect });
    const sourcePaths = Array.from({ length: 101 }, (_, index) => `/clipboard/f${index}`);

    await expect(
      pasteWorkspaceFileEntries(48_322, "dest", { source_paths: sourcePaths, intent }),
    ).rejects.toThrow("粘贴一次最多 100 项，当前 101 项，请分批操作");
    expect(requests).toBe(0);
  });
});
