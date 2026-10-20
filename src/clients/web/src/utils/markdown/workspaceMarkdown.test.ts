import { describe, expect, test } from "bun:test";
import { resolveWorkspaceMarkdownTarget } from "./workspaceMarkdown";

const ROOT = "boxteam://workspace/backend_files/resources/files/root";
const EXTERNAL = "boxteam://gateway/gateway_01900000000070008000000000000001/resources/files/01900000000070008000000000000001";

describe("工作区 Markdown 资源地址", () => {
  test("相对于 Markdown 文件所在目录解析图片和文档", () => {
    expect(resolveWorkspaceMarkdownTarget(`${ROOT}/docs/guide/readme.md`, "./image.png"))
      .toEqual({ kind: "workspace", path: `${ROOT}/docs/guide/image.png`, fragment: "" });
    expect(resolveWorkspaceMarkdownTarget(`${ROOT}/docs/guide/readme.md`, "../api/a.md#请求"))
      .toEqual({ kind: "workspace", path: `${ROOT}/docs/api/a.md`, fragment: "#请求" });
  });

  test("根路径、锚点和外部地址保持明确语义", () => {
    expect(resolveWorkspaceMarkdownTarget(`${ROOT}/docs/readme.md`, "/asset/logo.svg"))
      .toEqual({ kind: "workspace", path: "/asset/logo.svg", fragment: "" });
    expect(resolveWorkspaceMarkdownTarget(`${ROOT}/docs/readme.md`, "#安装"))
      .toEqual({ kind: "anchor", href: "#安装" });
    expect(resolveWorkspaceMarkdownTarget(`${ROOT}/docs/readme.md`, "https://example.com/a.md"))
      .toEqual({ kind: "external", href: "https://example.com/a.md" });
  });

  test("Windows 绝对路径交给目标机器解释", () => {
    expect(resolveWorkspaceMarkdownTarget(`${ROOT}/readme.md`, "C:\\data\\a.md"))
      .toEqual({ kind: "workspace", path: "C:\\data\\a.md", fragment: "" });
  });

  test("拒绝越过工作区根目录", () => {
    expect(() => resolveWorkspaceMarkdownTarget(`${ROOT}/readme.md`, "../secret.png"))
      .toThrow("越过工作区根目录");
  });

  test("文件系统快捷路径中的相对资源保持文件系统作用域", () => {
    expect(resolveWorkspaceMarkdownTarget(
      `${EXTERNAL}/docs/readme.md`,
      "../images/logo.png",
    )).toEqual({
      kind: "workspace",
      path: `${EXTERNAL}/images/logo.png`,
      fragment: "",
    });
  });
});
