import { updateWorkspaceFileContent } from "../../api";

const originalFetch = globalThis.fetch;
const requests: Array<{ url: string; init?: RequestInit }> = [];

globalThis.fetch = Object.assign(
  async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    requests.push({ url, init });
    if (new URL(url).pathname === "/api/gateway/auth/local-credential") {
      return new Response(
        JSON.stringify({
          code: 0,
          message: "ok",
          request_id: "req_auth",
          data: { token: "test-token" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(
      JSON.stringify({
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
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  },
  { preconnect: originalFetch.preconnect },
);

try {
  const saved = await updateWorkspaceFileContent(
    18014,
    "notes/a b.txt",
    {
      content: "after\n",
      expected_revision: "a".repeat(64),
    },
    "workspace-test",
  );
  const request = requests[1];
  if (!request.url.endsWith("/api/v1/workspace/files/content?path=notes%2Fa+b.txt")) {
    throw new Error(`保存接口路径错误: ${request.url}`);
  }
  if (request.init?.method !== "PUT") {
    throw new Error(`保存接口方法错误: ${request.init?.method}`);
  }
  const headers = new Headers(request.init?.headers);
  if (headers.get("X-BoxTeam-Workspace-Id") !== "workspace-test") {
    throw new Error("保存请求没有携带目标 Gateway 工作区 ID");
  }
  if (headers.get("X-Local-Token") !== "test-token") {
    throw new Error("保存请求没有携带 Gateway 本地凭据");
  }
  if (
    request.init?.body !== JSON.stringify({
      content: "after\n",
      expected_revision: "a".repeat(64),
    })
  ) {
    throw new Error(`保存请求体错误: ${String(request.init?.body)}`);
  }
  if (saved.revision !== "b".repeat(64)) {
    throw new Error("保存结果没有使用后端返回的最新 revision");
  }
} finally {
  globalThis.fetch = originalFetch;
}
