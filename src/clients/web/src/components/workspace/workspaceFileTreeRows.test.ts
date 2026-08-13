import { describe, expect, test } from "bun:test";

import type { WorkspaceFileNode } from "../../types/backend";
import type { DirectoryCacheEntry } from "./workspaceFileTreeCache";
import { buildVisibleFileTreeRows } from "./workspaceFileTreeRows";

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
