import { afterEach, describe, expect, test } from "bun:test";
import { getWorkspaceRawFileBlob } from "../../api";
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
      "workspace-raw",
    );

    const request = businessRequest(requests, "/api/v1/workspace/files/raw");
    expect(request.url).toEndWith(
      "/api/v1/workspace/files/raw?path=docs%2Fimages%2Fa+b.png&scope=workspace",
    );
    const headers = new Headers(request.init?.headers);
    expect(headers.get("X-Local-Token")).toBe("raw-token");
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe("workspace-raw");
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
      "workspace-raw",
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
    const reading = getWorkspaceRawFileBlob(28_016, "audio.wav", "workspace-range", undefined, { start: 1, end: 2 });
    if (status === 206) expect(Array.from(new Uint8Array(await (await reading).arrayBuffer()))).toEqual([2, 3]);
    else await expect(reading).rejects.toThrow("没有按请求返回字节范围");
    const request = businessRequest(requests, "/api/v1/workspace/files/raw");
    const headers = new Headers(request.init?.headers);
    expect(headers.get("Range")).toBe("bytes=1-2");
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe("workspace-range");
    expect(headers.get("X-Local-Token")).toBe("raw-token");
  });
});
