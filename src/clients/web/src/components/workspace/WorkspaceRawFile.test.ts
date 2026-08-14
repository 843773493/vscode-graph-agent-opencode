import { afterEach, describe, expect, test } from "bun:test";
import { getWorkspaceRawFileBlob } from "../../api";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("工作区原始文件 API", () => {
  test("携带 Gateway 凭据和目标工作区读取带空格的相对路径", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    globalThis.fetch = Object.assign(
      async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        requests.push({ url, init });
        if (new URL(url).pathname === "/api/gateway/auth/local-credential") {
          return new Response(JSON.stringify({ data: { token: "raw-token" } }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(new Uint8Array([137, 80, 78, 71]), {
          status: 200,
          headers: { "Content-Type": "image/png" },
        });
      },
      { preconnect: originalFetch.preconnect },
    );

    const blob = await getWorkspaceRawFileBlob(
      28014,
      "docs/images/a b.png",
      "workspace-raw",
    );

    const request = requests[1];
    expect(request?.url).toEndWith(
      "/api/v1/workspace/files/raw?path=docs%2Fimages%2Fa+b.png&scope=workspace",
    );
    const headers = new Headers(request?.init?.headers);
    expect(headers.get("X-Local-Token")).toBe("raw-token");
    expect(headers.get("X-BoxTeam-Workspace-Id")).toBe("workspace-raw");
    expect(blob.type).toBe("image/png");
    expect(blob.size).toBe(4);
  });

  test("文件系统快捷路径使用绝对路径作用域", async () => {
    const requests: string[] = [];
    globalThis.fetch = Object.assign(
      async (input: string | URL | Request) => {
        const url = String(input);
        requests.push(url);
        if (new URL(url).pathname === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "raw-token" } });
        }
        return new Response("external", { status: 200 });
      },
      { preconnect: originalFetch.preconnect },
    );

    await getWorkspaceRawFileBlob(
      28015,
      "filesystem:/home/hyf/.cache/model.bin",
      "workspace-raw",
    );

    expect(requests[1]).toEndWith(
      "/api/v1/workspace/files/raw?path=%2Fhome%2Fhyf%2F.cache%2Fmodel.bin&scope=filesystem",
    );
  });
});
