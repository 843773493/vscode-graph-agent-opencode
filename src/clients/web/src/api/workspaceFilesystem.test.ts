import { afterEach, describe, expect, test } from "bun:test";
import {
  getWorkspaceFiles,
  getWorkspaceRawFileMimeType,
  pasteWorkspaceFileEntries,
  uploadWorkspaceFileEntries,
} from "./workspaceFilesystem";

import type { WorkspaceFileMutationIntent } from "../types/backend";
import { gatewayWorkspaceKey } from "./workspaceIdentity";
import { installSessionApiFetchMock, unwrapSessionApiFetch } from "./session/sessionApiFetchMock";

const OWNER = { gatewayId: "gateway-files", workspaceId: "00000000-0000-4000-8000-000000000061" };
const WORKSPACE_KEY = gatewayWorkspaceKey(OWNER);
const ROOT = `boxteam://workspace/${OWNER.workspaceId}/resources/files/root`;

const intent: WorkspaceFileMutationIntent = { client_operation_id: "op_00000000000040008000000000000001",
  client_sequence: 1, expected_directory_revision: 1, depends_on: [], created_by_operation_id: null,
  relative_child_path: "", directory_revision_from_operation_id: null, client_ref: "file-unit",
  directory_key: { workspace_id: OWNER.workspaceId, scope: "workspace", root: ROOT, path: ROOT + "/dest" } };

const originalFetch = globalThis.fetch;

/** 屏障由共享桩应答，只记录真正到达指定业务端点的请求。 */
function installFetchMock(respond: () => Response, pathname = "/api/v1/workspace/files"): string[] {
  const requests: string[] = [];
  installSessionApiFetchMock(({ path, method, init }) => {
    expect(path).toBe(pathname);
    const headers = new Headers(init?.headers);
    expect(headers.get("X-BoxTeam-Target-Gateway-Id")).toBe(OWNER.gatewayId);
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe(OWNER.workspaceId);
    requests.push(`${method} ${path}`);
    return respond();
  }, { credentialToken: "files-token" });
  return requests;
}

afterEach(() => {
  unwrapSessionApiFetch();
});

describe("工作区文件列表 API 的 items 契约", () => {
  test("合法空数组正常返回并完成路径编码", async () => {
    const requests = installFetchMock(() => Response.json({
      data: { root_path: ROOT, path: ROOT + "/repo", items: [],
        directory_key: { workspace_id: OWNER.workspaceId, scope: "workspace", root: ROOT, path: ROOT + "/repo" }, directory_revision: 1 },
      request_id: "req-files-empty",
    }));

    await expect(getWorkspaceFiles(48_310, "/repo", WORKSPACE_KEY)).resolves
      .toMatchObject({ path: ROOT + "/repo", items: [] });
    expect(requests).toEqual(["GET /api/v1/workspace/files"]);
  });

  test("items 非数组：响亮失败而不是静默收敛为空列表", async () => {
    const requests = installFetchMock(() => Response.json({
      data: { root_path: "/", path: "/repo", items: "NOT-AN-ARRAY" },
      request_id: "req-files-broken-items",
    }));

    await expect(getWorkspaceFiles(48_311, "/repo", WORKSPACE_KEY)).rejects
      .toThrow("工作区文件列表响应 items 必须是数组");
    expect(requests).toEqual(["GET /api/v1/workspace/files"]);
  });

  test("items 为 null：响亮失败而不是被 ?? [] 静默吸收", async () => {
    const requests = installFetchMock(() => Response.json({
      data: { root_path: "/", path: "/repo", items: null },
      request_id: "req-files-null-items",
    }));

    await expect(getWorkspaceFiles(48_312, "/repo", WORKSPACE_KEY)).rejects
      .toThrow("工作区文件列表响应 items 必须是数组");
    expect(requests).toEqual(["GET /api/v1/workspace/files"]);
  });
});

describe("原文件 HEAD 的 MIME 契约", () => {
  test("保留完整 owner 与规范路径，只消费 MIME 响应头", async () => {
    const path = `boxteam://${OWNER.gatewayId}/workspace/${OWNER.workspaceId}/resources/attachments/att_media/original/content`;
    const response = new Response("正文不得被消费", { headers: { "Content-Type": "Image/PNG; charset=binary" } });
    const controller = new AbortController();
    installSessionApiFetchMock(({ url, method, init }) => {
      const target = new URL(url);
      expect(target.pathname).toBe("/api/v1/workspace/files/raw");
      expect(target.searchParams.get("path")).toBe(path);
      expect(method).toBe("HEAD");
      expect(new Headers(init?.headers).get("X-BoxTeam-Target-Gateway-Id")).toBe(OWNER.gatewayId);
      expect(new Headers(init?.headers).get("X-BoxTeam-Workspace-Id")).toBe(OWNER.workspaceId);
      expect(init?.signal?.aborted).toBe(false);
      return response;
    });
    await expect(getWorkspaceRawFileMimeType(48_330, path, WORKSPACE_KEY, controller.signal)).resolves.toBe("image/png");
    expect(response.bodyUsed).toBe(false);
  });

  test("调用方取消透传到原请求", async () => {
    const controller = new AbortController();
    let started!: () => void;
    const requested = new Promise<void>(resolve => { started = resolve; });
    installSessionApiFetchMock(({ init }) => new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal;
      if (!signal) throw new Error("原请求未传取消信号");
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      started();
    }));
    const pending = getWorkspaceRawFileMimeType(48_332, ROOT + "/content", WORKSPACE_KEY, controller.signal);
    await requested;
    controller.abort(new Error("已取消原件 MIME 读取"));
    await expect(pending).rejects.toThrow("已取消原件 MIME 读取");
  });

  test("缺失 MIME 响亮失败，不自行推断正文类型", async () => {
    const requests = installFetchMock(() => new Response(null), "/api/v1/workspace/files/raw");
    await expect(getWorkspaceRawFileMimeType(48_331, ROOT + "/content", WORKSPACE_KEY)).rejects.toThrow("原文件响应缺少 MIME 类型");
    expect(requests).toEqual(["HEAD /api/v1/workspace/files/raw"]);
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
    const requests = installFetchMock(() => Response.json({
      data: { operation_id: intent.client_operation_id, state: "queued", effect: "none" },
      request_id: "req-files-upload-100",
    }), "/api/v1/workspace/files/upload");
    const files = Array.from({ length: 100 }, (_, index) =>
      ({ file: new File(["x"], `f${index}.txt`), name: `f${index}.txt`, relativePath: `f${index}.txt` }));

    await expect(uploadWorkspaceFileEntries(48_321, ROOT + "/dest", files, intent, WORKSPACE_KEY)).resolves
      .toMatchObject({ operation_id: intent.client_operation_id, state: "queued" });
    expect(requests).toEqual(["POST /api/v1/workspace/files/upload"]);
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
