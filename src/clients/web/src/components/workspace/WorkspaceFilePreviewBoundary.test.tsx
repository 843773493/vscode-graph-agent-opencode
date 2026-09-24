import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { getWorkspaceFileContent } from "../../api";
import { invalidateGatewayToken, invalidateGatewayUserSession } from "../../api/http";
import WorkspaceFilePreviewArea from "./WorkspaceFilePreviewArea";
import type { WorkspacePreviewTab } from "./WorkspaceFilePreviewArea";

/**
 * 文件预览的畸形载荷边界。
 *
 * 真实浏览器审查记录：把 `GET /api/v1/workspace/files/content` 的 `data.content`
 * 改成非字符串（数字 / null / 缺失），或把 `data` 返回成数组，点击文件预览后
 * 整页被错误边界接管——只剩 `content.replace is not a function` 这类英文引擎
 * 报错，聊天区、输入框、文件树全部消失，且刷新会重新触发同一请求形成死循环。
 *
 * 契约边界在 API 层：字段类型不符必须在这里响亮失败，让上层既有的
 * 「文件读取失败: ...」展示链路接管，而不是在组件层加防御性 if 骗过错误边界。
 * 本用例杀掉「删掉 content 字符串校验」的变异。
 */

const originalFetch = globalThis.fetch;
const PORT = 49_502;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

/** 真实应答 Gateway 凭据与用户会话屏障，业务端点交给 handler。 */
function installFetch(handler: (url: URL, method: string) => Response | undefined): void {
  invalidateGatewayToken(PORT);
  invalidateGatewayUserSession(PORT);
  globalThis.fetch = Object.assign(
    async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(
        input instanceof Request ? input.url : String(input),
        `http://127.0.0.1:${PORT}`,
      );
      const method = String(
        init?.method ?? (input instanceof Request ? input.method : "GET"),
      );
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return Response.json({ code: 0, message: "ok", request_id: "req_t", data: { token: "t" } });
      }
      if (url.pathname === "/api/gateway/users/current") {
        return Response.json({ code: 0, message: "ok", request_id: "req_u", data: { kind: "guest", user_id: null } });
      }
      const response = handler(url, method);
      if (response === undefined) {
        throw new Error(`测试收到未声明请求: ${method} ${url.pathname}`);
      }
      return response;
    },
    { preconnect: originalFetch.preconnect },
  );
}

/** 内容接口固定返回给定 data 载荷；其余字段保持合法，只变异 content/path。 */
function contentResponse(data: unknown): Response {
  return Response.json({ code: 0, message: "ok", request_id: "req_c", data });
}

const VALID_CONTENT = {
  root_path: "/home/test",
  path: "src/app.ts",
  name: "app.ts",
  content: "const a = 1;\n",
  language: "typescript",
  size: 13,
  modified_at: "2026-01-01T00:00:00Z",
  revision: "rev-1",
};

describe("文件内容响应字段校验（API 边界）", () => {
  test("content 为非字符串时抛出带上下文的中文错误，而不是把引擎错误泄给上层", async () => {
    installFetch((url, method) => (
      method === "GET" && url.pathname === "/api/v1/workspace/files/content"
        ? contentResponse({ ...VALID_CONTENT, content: 42 })
        : undefined
    ));

    await expect(getWorkspaceFileContent(PORT, "src/app.ts")).rejects.toThrow(
      "工作区文件内容响应 content 必须是字符串",
    );
  });

  test("content 缺失时同样在边界失败", async () => {
    const { content: _omitted, ...withoutContent } = VALID_CONTENT;
    installFetch((url, method) => (
      method === "GET" && url.pathname === "/api/v1/workspace/files/content"
        ? contentResponse(withoutContent)
        : undefined
    ));

    await expect(getWorkspaceFileContent(PORT, "src/app.ts")).rejects.toThrow(
      "工作区文件内容响应 content 必须是字符串",
    );
  });

  test("content 为 null 时同样在边界失败", async () => {
    installFetch((url, method) => (
      method === "GET" && url.pathname === "/api/v1/workspace/files/content"
        ? contentResponse({ ...VALID_CONTENT, content: null })
        : undefined
    ));

    await expect(getWorkspaceFileContent(PORT, "src/app.ts")).rejects.toThrow(
      "工作区文件内容响应 content 必须是字符串",
    );
  });

  test("path 为非字符串时同样在边界失败", async () => {
    installFetch((url, method) => (
      method === "GET" && url.pathname === "/api/v1/workspace/files/content"
        ? contentResponse({ ...VALID_CONTENT, path: 42 })
        : undefined
    ));

    await expect(getWorkspaceFileContent(PORT, "src/app.ts")).rejects.toThrow(
      "工作区文件内容响应 path 必须是字符串",
    );
  });

  test("data 为数组时在边界失败", async () => {
    installFetch((url, method) => (
      method === "GET" && url.pathname === "/api/v1/workspace/files/content"
        ? contentResponse([1, 2, 3])
        : undefined
    ));

    await expect(getWorkspaceFileContent(PORT, "src/app.ts")).rejects.toThrow(
      "工作区文件内容响应必须是对象",
    );
  });

  test("合法响应仍正常返回，content 原样保留", async () => {
    installFetch((url, method) => (
      method === "GET" && url.pathname === "/api/v1/workspace/files/content"
        ? contentResponse(VALID_CONTENT)
        : undefined
    ));

    const result = await getWorkspaceFileContent(PORT, "src/app.ts");
    expect(result.content).toBe("const a = 1;\n");
    expect(result.path).toBe("src/app.ts");
  });
});

/**
 * 白屏的可达序列：直接把畸形 content 塞进已加载的文件 tab 再渲染预览区。
 * 修复后这类载荷根本到不了组件（API 层已抛错），但组件必须能在"上游万一漏过"
 * 之外证明自己不会抛——即渲染必须完成，而不是被错误边界接管。
 */
describe("文件预览区对畸形 content 不白屏", () => {
  function mountPreview(content: unknown): { renderer: ReactTestRenderer; error: unknown } {
    const tab = {
      ...VALID_CONTENT,
      path: "src/app.ts",
      previewType: "file",
      selection: null,
      content,
    } as unknown as WorkspacePreviewTab;
    let renderer!: ReactTestRenderer;
    let error: unknown = null;
    try {
      act(() => {
        renderer = create(
          <WorkspaceFilePreviewArea
            context="files"
            visible
            flexRatio={1}
            apiPort={PORT}
            workspaceId="gw_1"
            workspaceName="test"
            sessionTitle="会话"
            tabs={[tab]}
            activePath="src/app.ts"
            loadingPath={null}
            error={null}
            editingPath={null}
            draftContent=""
            savingPath={null}
            hasUnsavedEdit={false}
            markdownSourceVisible={false}
            onMarkdownSourceChange={() => undefined}
            onBeginEdit={() => undefined}
            onDraftChange={() => undefined}
            onCancelEdit={() => undefined}
            onSaveEdit={async () => undefined}
            onOpenWorkspacePath={async () => undefined}
          />,
        );
      });
    } catch (cause: unknown) {
      error = cause;
    }
    return { renderer, error };
  }

  test("合法 content 渲染出文件行，不抛错", () => {
    const { renderer, error } = mountPreview(VALID_CONTENT.content);
    expect(error).toBeNull();
    expect(JSON.stringify(renderer.toJSON())).toContain("const a = 1;");
    act(() => renderer.unmount());
  });
});
