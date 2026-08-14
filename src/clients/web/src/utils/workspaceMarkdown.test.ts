import { describe, expect, test } from "bun:test";
import { resolveWorkspaceMarkdownTarget } from "./workspaceMarkdown";

describe("工作区 Markdown 资源地址", () => {
  test("相对于 Markdown 文件所在目录解析图片和文档", () => {
    expect(resolveWorkspaceMarkdownTarget("docs/guide/readme.md", "./image.png"))
      .toEqual({ kind: "workspace", path: "docs/guide/image.png", fragment: "" });
    expect(resolveWorkspaceMarkdownTarget("docs/guide/readme.md", "../api/a.md#请求"))
      .toEqual({ kind: "workspace", path: "docs/api/a.md", fragment: "#请求" });
  });

  test("根路径、锚点和外部地址保持明确语义", () => {
    expect(resolveWorkspaceMarkdownTarget("docs/readme.md", "/asset/logo.svg"))
      .toEqual({ kind: "workspace", path: "asset/logo.svg", fragment: "" });
    expect(resolveWorkspaceMarkdownTarget("docs/readme.md", "#安装"))
      .toEqual({ kind: "anchor", href: "#安装" });
    expect(resolveWorkspaceMarkdownTarget("docs/readme.md", "https://example.com/a.md"))
      .toEqual({ kind: "external", href: "https://example.com/a.md" });
  });

  test("拒绝越过工作区根目录", () => {
    expect(() => resolveWorkspaceMarkdownTarget("readme.md", "../secret.png"))
      .toThrow("越过工作区根目录");
  });

  test("文件系统快捷路径中的相对资源保持文件系统作用域", () => {
    expect(resolveWorkspaceMarkdownTarget(
      "filesystem:/home/hyf/docs/readme.md",
      "../images/logo.png",
    )).toEqual({
      kind: "workspace",
      path: "filesystem:/home/hyf/images/logo.png",
      fragment: "",
    });
  });
});
