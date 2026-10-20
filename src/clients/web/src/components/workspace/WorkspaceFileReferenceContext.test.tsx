import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import {
  WorkspaceFileReferenceProvider,
  useWorkspaceFileReferenceContext,
  type WorkspaceFileReferenceResolution,
} from "./WorkspaceFileReferenceContext";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";
import MarkdownContent from "../chat/MarkdownContent";

const port = 49_601;
const workspaceRoot = "boxteam://workspace/backend-uuid/resources/files/root";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(
  globalThis,
  "window",
);

afterEach(() => {
  globalThis.fetch = originalFetch;
  restoreGlobalDescriptor("window", originalWindowDescriptor);
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
): { contentPaths: string[]; listingPaths: string[] } {
  const contentPaths: string[] = [];
  const listingPaths: string[] = [];
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
        listingPaths.push(url.searchParams.get("path") ?? "");
        return Response.json({
          code: 0,
          message: "ok",
          request_id: "req_files",
          data: {
            path: workspaceRoot,
            directory_key: { workspace_id: "backend-uuid", scope: "workspace", root: workspaceRoot, path: workspaceRoot },
            directory_revision: 1,
            items: [
              { name: ".boxteam", path: `${workspaceRoot}/.boxteam`, kind: "directory", has_children: true },
              {
                name: "broken.ts",
                path: `${workspaceRoot}/broken.ts`,
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
        contentPaths.push(url.searchParams.get("path") ?? "");
        return content(url);
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    },
    { preconnect: originalFetch.preconnect },
  );
  return { contentPaths, listingPaths };
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


function fileContent(path = `${workspaceRoot}/broken.ts`): Response {
  return Response.json({ code: 0, message: "ok", request_id: "req_file", data: {
    root_path: workspaceRoot, path, name: "broken.ts", content: "目标原件", encoding: "utf-8",
    language: "typescript", size: 12, modified_at: null, revision: "a".repeat(64),
  } });
}

describe("文件引用固定目标与 URI 回执", () => {
  test.each([
    "/native/外部 目录%/broken.ts",
    String.raw`C:\目标 目录\broken.ts`,
    String.raw`\\server\share\broken.ts`,
    "boxteam://gateway_target/workspace/backend-uuid/resources/files/root/含%20%252F.ts",
  ])("显式输入原样直达后端且不枚举同名文件：%s", async (target) => {
    installWindow();
    const observed = installBackend(() => fileContent());
    const { renderer, resolve } = await renderProvider();
    const result = await resolve(`${target}#L2-L3`);
    expect(result).toMatchObject({ status: "resolved", reference: {
      path: `${workspaceRoot}/broken.ts`, selection: { startLine: 2, endLine: 3 },
    } });
    expect(observed.contentPaths).toEqual([target]);
    expect(observed.listingPaths).toEqual([]);
    renderer.unmount();
  });

  test.each([404, 403, 502])("显式目标失败 %i 不改投工作区同名文件", async (status) => {
    installWindow();
    const observed = installBackend(() => Response.json({ code: status, message: "目标失败",
      request_id: "req_failure", data: null }, { status }));
    const { renderer, resolve } = await renderProvider();
    expect((await resolve("/external/broken.ts")).status).toBe(status === 404 ? "missing" : "error");
    expect(observed.contentPaths).toEqual(["/external/broken.ts"]);
    expect(observed.listingPaths).toEqual([]);
    renderer.unmount();
  });

  test("相对文件名查找消费 URI 文件树并以内容回执为权威", async () => {
    installWindow();
    const observed = installBackend((url) => url.searchParams.get("path") === "broken.ts"
      ? Response.json({ code: 404, message: "简写不存在", request_id: "req_missing", data: null }, { status: 404 })
      : fileContent());
    const { renderer, resolve } = await renderProvider();
    const result = await resolve("broken.ts:4:2");
    expect(result).toMatchObject({ status: "resolved", reference: { path: `${workspaceRoot}/broken.ts`,
      selection: { startLine: 4, endLine: 4, startColumn: 2 } } });
    expect(observed.contentPaths).toEqual(["broken.ts", `${workspaceRoot}/broken.ts`]);
    expect(observed.listingPaths).toEqual([""]);
    renderer.unmount();
  });
});


test("损坏 file URL 在接口返回明确错误且不请求其他文件", async () => {
  installWindow();
  const observed = installBackend(() => fileContent());
  const { renderer, resolve } = await renderProvider();
  expect((await resolve("file:///tmp/%zz.ts")).status).toBe("error");
  expect(observed.contentPaths).toEqual([]);
  expect(observed.listingPaths).toEqual([]);
  renderer.unmount();
});

test("真实 Markdown 管线保留原生路径的空格、中文和字面百分号", async () => {
  installWindow();
  const observed = installBackend(() => fileContent());
  const targets = ["/native/引用 外部%2F/broken.ts", "引用 相对%2F/broken.ts",
    "boxteam://workspace/backend-uuid/resources/files/root/引用%20%252F.ts"];
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(<WorkspaceFileReferenceProvider apiPort={port} workspaceId="ws_test" onOpen={() => {}}>
      <MarkdownContent value={targets.map((target, index) => `[目标${index}](<${target}#L2>)`).join("\n\n")
        + "\n\n[普通链接](running) [外部链接](https://example.com/file.ts)"} />
    </WorkspaceFileReferenceProvider>);
  });
  expect(observed.contentPaths).toEqual(targets);
  expect(observed.listingPaths).toEqual([]);
  expect(renderer.root.findAllByProps({ className: "chat-file-reference" })).toHaveLength(targets.length);
  renderer.unmount();
});
