import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import {
  WorkspaceFileReferenceProvider,
  useWorkspaceFileReferenceContext,
  type WorkspaceFileReferenceResolution,
} from "./WorkspaceFileReferenceContext";

const port = 49_601;
const workspaceRoot = "/home/user/project";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(
  globalThis,
  "window",
);

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
});

function installWindow(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port), origin: `http://127.0.0.1:${port}` },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    },
  });
}

/**
 * 安装只认领凭据与用户会话两条隧道的 fetch mock；内容接口由 handler 决定。
 * 未预期请求一律响亮抛出，避免静默返回伪响应掩盖真实调用。
 */
function installBackend(
  content: (url: URL) => Response,
): { contentPaths: string[] } {
  const contentPaths: string[] = [];
  globalThis.fetch = Object.assign(
    async (...args: Parameters<typeof fetch>) => {
      const [input] = args;
      const url = new URL(String(input), `http://127.0.0.1:${port}`);
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return Response.json({
          code: 0,
          message: "ok",
          request_id: "req_local_credential",
          data: { token: "test-token" },
        });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return Response.json({
          code: 0,
          message: "ok",
          request_id: "req_current_user",
          data: { kind: "guest", user_id: null },
        });
      }
      if (url.pathname === "/api/v1/workspace/files") {
        return Response.json({
          code: 0,
          message: "ok",
          request_id: "req_files",
          data: {
            items: [
              {
                name: "broken.ts",
                path: "broken.ts",
                kind: "file",
                has_children: false,
                size: 10,
                modified_at: null,
              },
            ],
            next_cursor: null,
          },
        });
      }
      if (url.pathname === "/api/v1/workspace/files/content") {
        contentPaths.push(url.pathname);
        return content(url);
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    },
    { preconnect: originalFetch.preconnect },
  );
  return { contentPaths };
}

interface Harness {
  renderer: ReactTestRenderer;
  resolve: (target: string) => Promise<WorkspaceFileReferenceResolution>;
}

async function renderProvider(): Promise<Harness> {
  const holder: {
    context: ReturnType<typeof useWorkspaceFileReferenceContext>;
  } = { context: null };
  function Capture(): null {
    holder.context = useWorkspaceFileReferenceContext();
    return null;
  }
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(
      <WorkspaceFileReferenceProvider
        apiPort={port}
        workspaceId="ws_test"
        workspaceRoot={workspaceRoot}
        onOpen={() => {}}
      >
        <Capture />
      </WorkspaceFileReferenceProvider>,
    );
  });
  const context = holder.context;
  if (!context) throw new Error("Provider 未暴露文件引用上下文");
  return { renderer, resolve: context.resolve };
}

describe("工作区文件引用 404 判定", () => {
  test("真 HttpRequestError 404 判定为 missing", async () => {
    installWindow();
    installBackend(() =>
      Response.json(
        {
          code: 404,
          message: "文件不存在",
          request_id: "req_content_404",
          detail: "文件不存在",
        },
        { status: 404 },
      ));
    const { renderer, resolve } = await renderProvider();

    const resolution = await resolve("broken.ts");
    expect(resolution).toEqual({ status: "missing" });
    renderer.unmount();
  });

  test("消息文本含“请求失败 404”但类型不是 HttpRequestError 时不得判定为 missing", async () => {
    installWindow();
    installBackend(() => {
      // 模拟内容接口层抛出普通 Error，而非带状态码的 HttpRequestError。
      throw new Error("解析内容失败：请求失败 404 已被上游改写成普通错误");
    });
    const { renderer, resolve } = await renderProvider();

    const resolution = await resolve("broken.ts");
    expect(resolution.status).toBe("error");
    renderer.unmount();
  });
});
