import { describe, expect, test } from "bun:test";

import type { WorkspaceFileNode } from "../../../types/backend";
import type { DirectoryCacheEntry } from "./workspaceFileTreeCache";
import {
  buildVisibleFileTreeRows,
  fileTreeNodeMatchesQuery,
} from "./workspaceFileTreeRows";

function node(name: string, path: string): WorkspaceFileNode {
  return { name, path, kind: "file", has_children: false, size: 0, modified_at: null };
}

describe("文件树搜索命中判定", () => {
  test("名称与路径分别匹配，不跨分隔符拼出假命中", () => {
    expect(fileTreeNodeMatchesQuery(node("b", "a/b"), "b")).toBe(true);
    expect(fileTreeNodeMatchesQuery(node("b", "a/b"), "a/b")).toBe(true);
    // 旧实现用 `${name}\n${path}` 拼接：路径里的换行会被当成真实换行，
    // 使「名字尾 + 路径头」跨行拼成 "b\nsee" 这种并不存在的连续串。
    const tricky = node("b", "see/b");
    expect(fileTreeNodeMatchesQuery(tricky, "b\nsee")).toBe(false);
  });

  test("名字本身含换行时按子串语义正常匹配", () => {
    const multiline = node("x\ny", "dir/x\ny");
    expect(fileTreeNodeMatchesQuery(multiline, "x")).toBe(true);
    expect(fileTreeNodeMatchesQuery(multiline, "y")).toBe(true);
    expect(fileTreeNodeMatchesQuery(multiline, "dir/x")).toBe(true);
    expect(fileTreeNodeMatchesQuery(multiline, "y\ndir")).toBe(false);
  });

  test("空查询不过滤任何节点", () => {
    expect(fileTreeNodeMatchesQuery(node("a", "a"), "")).toBe(true);
  });
});

function directory(items: WorkspaceFileNode[]): DirectoryCacheEntry {
  return {
    items,
    loading: false,
    error: null,
    truncated: false,
    nextCursor: null,
    stale: false,
    lastAccessedAt: 1,
  };
}

test("只展开可见分支并为大目录生成扁平行", () => {
  const items = Array.from({ length: 1_000 }, (_, index): WorkspaceFileNode => ({
    name: `file-${index}.ts`,
    path: `file-${index}.ts`,
    kind: "file",
    has_children: false,
    size: index,
    modified_at: null,
  }));
  const rows = buildVisibleFileTreeRows({
    directories: { "": directory(items) },
    expandedPaths: new Set([""]),
    shortcuts: [],
    searchQuery: "",
    workspaceLabel: "project",
    workspaceTitle: "/workspace/project",
    workspaceRootPath: "",
    filesystemRootPath: "filesystem:/",
    shortcutPath: (path) => `filesystem:${path}`,
  });

  expect(rows).toHaveLength(1_002);
  expect(rows[0]).toMatchObject({ kind: "root", label: "project" });
  expect(rows[1]).toMatchObject({ kind: "node", depth: 0 });
  expect(rows[rows.length - 1]).toMatchObject({ kind: "root", label: "/" });
});

describe("扁平文件树搜索", () => {
  test("不加载未展开目录也能保留可继续展开的目录", () => {
    const rows = buildVisibleFileTreeRows({
      directories: {
        "": directory([{
          name: "src",
          path: "src",
          kind: "directory",
          has_children: true,
          size: null,
          modified_at: null,
        }]),
      },
      expandedPaths: new Set([""]),
      shortcuts: [],
      searchQuery: "needle",
      workspaceLabel: "project",
      workspaceTitle: "/workspace/project",
      workspaceRootPath: "",
      filesystemRootPath: "filesystem:/",
      shortcutPath: (path) => `filesystem:${path}`,
    });

    expect(rows.some((row) => row.kind === "node" && row.node.path === "src"))
      .toBe(true);
  });
});

describe("符号链接与损坏目录结构的边界", () => {
  test("符号链接即使位于展开集合内也不展开其子项", () => {
    const link: WorkspaceFileNode = {
      name: "link",
      path: "dir/link",
      kind: "symlink",
      has_children: false,
      size: 0,
      modified_at: null,
    };
    const rows = buildVisibleFileTreeRows({
      directories: {
        dir: directory([link]),
        // 即使调用方错误地提供了 symlink 的子目录缓存，也不应被展开。
        "dir/link": directory([{
          name: "escaped.ts",
          path: "dir/link/escaped.ts",
          kind: "file",
          has_children: false,
          size: 1,
          modified_at: null,
        }]),
      },
      expandedPaths: new Set(["dir", "dir/link"]),
      shortcuts: [],
      searchQuery: "",
      workspaceLabel: "project",
      workspaceTitle: "/workspace/project",
      workspaceRootPath: "dir",
      filesystemRootPath: "filesystem:/",
      shortcutPath: (path) => "filesystem:" + path,
    });

    const linkRow = rows.find((row) => row.kind === "node" && row.node.path === "dir/link");
    expect(linkRow).toMatchObject({ kind: "node", expanded: false });
    expect(rows.some((row) => row.kind === "node" && row.node.path === "dir/link/escaped.ts"))
      .toBe(false);
  });

  test("自引用目录不会造成无限递归", () => {
    const rows = buildVisibleFileTreeRows({
      directories: {
        loop: directory([{
          name: "loop",
          path: "loop",
          kind: "directory",
          has_children: true,
          size: null,
          modified_at: null,
        }]),
      },
      expandedPaths: new Set(["loop"]),
      shortcuts: [],
      searchQuery: "",
      workspaceLabel: "project",
      workspaceTitle: "/workspace/project",
      workspaceRootPath: "loop",
      filesystemRootPath: "filesystem:/",
      shortcutPath: (path) => "filesystem:" + path,
    });

    expect(rows.filter((row) => row.kind === "node" && row.node.path === "loop"))
      .toHaveLength(1);
  });

  test("搜索时自引用目录不会造成无限递归", () => {
    const rows = buildVisibleFileTreeRows({
      directories: {
        loop: directory([{
          name: "loop",
          path: "loop",
          kind: "directory",
          has_children: true,
          size: null,
          modified_at: null,
        }]),
      },
      expandedPaths: new Set(["loop"]),
      shortcuts: [],
      searchQuery: "needle",
      workspaceLabel: "project",
      workspaceTitle: "/workspace/project",
      workspaceRootPath: "loop",
      filesystemRootPath: "filesystem:/",
      shortcutPath: (path) => "filesystem:" + path,
    });

    expect(rows.filter((row) => row.kind === "node" && row.node.path === "loop"))
      .toHaveLength(1);
  });

  test("同一目录在互不嵌套的多条根下重复展开时仍各自渲染", () => {
    const rows = buildVisibleFileTreeRows({
      directories: {
        "filesystem:shared": directory([{
          name: "a.ts",
          path: "shared/a.ts",
          kind: "file",
          has_children: false,
          size: 1,
          modified_at: null,
        }]),
      },
      expandedPaths: new Set(["filesystem:shared"]),
      shortcuts: [
        { source: "workspace", label: "one", path: "shared" },
        { source: "workspace", label: "two", path: "shared" },
      ],
      searchQuery: "",
      workspaceLabel: "project",
      workspaceTitle: "/workspace/project",
      workspaceRootPath: "",
      filesystemRootPath: "filesystem:/",
      shortcutPath: (path) => "filesystem:" + path,
    });

    expect(rows.filter((row) => row.kind === "node" && row.node.path === "shared/a.ts"))
      .toHaveLength(2);
  });
});
