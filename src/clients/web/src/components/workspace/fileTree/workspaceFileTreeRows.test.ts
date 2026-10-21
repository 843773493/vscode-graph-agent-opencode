import { describe, expect, test } from "bun:test";

import type { WorkspaceFileNode } from "../../../types/backend";
import type { DirectoryCacheEntry } from "./workspaceFileTreeCache";
import {
  buildVisibleFileTreeRows,
  createFileTreeSearchMatcher,
  fileTreeNodeMatchesQuery,
} from "./workspaceFileTreeRows";

const workspaceRootUri = "boxteam://workspace/ws-1/resources/files/root";
const gatewayId = "gateway_0123456789abcdef0123456789abcdef";
const filesystemRootUri = `boxteam://${gatewayId}/gateway/${gatewayId}/resources/files/0123456789ab7abc8abc012345678901`;

function node(name: string, path: string): WorkspaceFileNode {
  return { name, path, kind: "file", has_children: false, size: 0, modified_at: null };
}

describe("文件树搜索命中判定", () => {
  test("名称与 URI 分别匹配，不跨分隔符拼出假命中", () => {
    expect(fileTreeNodeMatchesQuery(node("b", `${workspaceRootUri}/a/b`), "b")).toBe(true);
    expect(fileTreeNodeMatchesQuery(node("b", `${workspaceRootUri}/a/b`), "a/b")).toBe(true);
    // 旧实现用 `${name}\n${path}` 拼接：URI 里的换行会被当成真实换行，
    // 使名字尾和路径头跨边界拼成并不存在的连续串。
    const tricky = node("b", `${workspaceRootUri}/see/b`);
    expect(fileTreeNodeMatchesQuery(tricky, "b\nsee")).toBe(false);
  });

  test("名字本身含换行时按子串语义正常匹配", () => {
    const multiline = node("x\ny", `${workspaceRootUri}/dir/x\ny`);
    expect(fileTreeNodeMatchesQuery(multiline, "x")).toBe(true);
    expect(fileTreeNodeMatchesQuery(multiline, "y")).toBe(true);
    expect(fileTreeNodeMatchesQuery(multiline, "dir/x")).toBe(true);
    expect(fileTreeNodeMatchesQuery(multiline, "y\ndir")).toBe(false);
  });

  test("空查询不过滤任何节点", () => {
    expect(fileTreeNodeMatchesQuery(node("a", `${workspaceRootUri}/a`), "")).toBe(true);
  });
});

function directory(items: WorkspaceFileNode[]): DirectoryCacheEntry {
  return {
    directoryKey: null, directoryRevision: null,
    items,
    loading: false,
    error: null,
    truncated: false,
    nextCursor: null,
    lastAccessedAt: 1,
  };
}

test("共享搜索匹配器复用已加载目录并保留未加载目录", () => {
  const sourceUri = `${workspaceRootUri}/src`;
  const sourceDirectory: WorkspaceFileNode = {
    name: "src",
    path: sourceUri,
    kind: "directory",
    has_children: true,
    size: null,
    modified_at: null,
  };
  const unloadedDirectory: WorkspaceFileNode = {
    name: "vendor",
    path: `${workspaceRootUri}/vendor`,
    kind: "directory",
    has_children: true,
    size: null,
    modified_at: null,
  };
  const matchesNode = createFileTreeSearchMatcher({
    [workspaceRootUri]: directory([sourceDirectory, unloadedDirectory]),
    [sourceUri]: directory([node("needle.ts", `${sourceUri}/needle.ts`)]),
  }, " NEEDLE ");

  expect(matchesNode(sourceDirectory)).toBe(true);
  expect(matchesNode(unloadedDirectory)).toBe(true);
  expect(matchesNode(node("plain.ts", `${sourceUri}/plain.ts`))).toBe(false);
});

function rowOptions(
  overrides: Partial<Parameters<typeof buildVisibleFileTreeRows>[0]> = {},
): Parameters<typeof buildVisibleFileTreeRows>[0] {
  return {
    directories: {},
    expandedPaths: new Set(),
    shortcuts: [],
    searchQuery: "",
    workspaceLabel: "project",
    workspaceTitle: workspaceRootUri,
    workspaceRootPath: workspaceRootUri,
    filesystemRootPath: filesystemRootUri,
    filesystemLabel: "文件系统",
    ...overrides,
  };
}

test("只展开可见分支并为大目录生成扁平行", () => {
  const items = Array.from({ length: 1_000 }, (_, index): WorkspaceFileNode => ({
    name: `file-${index}.ts`,
    path: `${workspaceRootUri}/file-${index}.ts`,
    kind: "file",
    has_children: false,
    size: index,
    modified_at: null,
  }));
  const rows = buildVisibleFileTreeRows(rowOptions({
    directories: { [workspaceRootUri]: directory(items) },
    expandedPaths: new Set([workspaceRootUri]),
  }));

  expect(rows).toHaveLength(1_002);
  expect(rows[0]).toMatchObject({ kind: "root", label: "project", treePath: workspaceRootUri });
  expect(rows[1]).toMatchObject({ kind: "node", depth: 0 });
  expect(rows[rows.length - 1]).toMatchObject({
    kind: "root", label: "文件系统", title: "文件系统", treePath: filesystemRootUri,
  });
});

describe("扁平文件树搜索", () => {
  test("不加载未展开目录也能保留可继续展开的目录", () => {
    const srcUri = `${workspaceRootUri}/src`;
    const rows = buildVisibleFileTreeRows(rowOptions({
      directories: {
        [workspaceRootUri]: directory([{
          name: "src",
          path: srcUri,
          kind: "directory",
          has_children: true,
          size: null,
          modified_at: null,
        }]),
      },
      expandedPaths: new Set([workspaceRootUri]),
      searchQuery: "needle",
    }));

    expect(rows.some((row) => row.kind === "node" && row.node.path === srcUri))
      .toBe(true);
  });
});

describe("符号链接与损坏目录结构的边界", () => {
  test("符号链接即使位于展开集合内也不展开其子项", () => {
    const linkUri = `${workspaceRootUri}/dir/link`;
    const escapedUri = `${linkUri}/escaped.ts`;
    const link: WorkspaceFileNode = {
      name: "link",
      path: linkUri,
      kind: "symlink",
      has_children: false,
      size: 0,
      modified_at: null,
    };
    const rows = buildVisibleFileTreeRows(rowOptions({
      directories: {
        [`${workspaceRootUri}/dir`]: directory([link]),
        // 即使调用方错误地提供了 symlink 的子目录缓存，也不应被展开。
        [linkUri]: directory([node("escaped.ts", escapedUri)]),
      },
      expandedPaths: new Set([`${workspaceRootUri}/dir`, linkUri]),
      workspaceRootPath: `${workspaceRootUri}/dir`,
    }));

    const linkRow = rows.find((row) => row.kind === "node" && row.node.path === linkUri);
    expect(linkRow).toMatchObject({ kind: "node", expanded: false });
    expect(rows.some((row) => row.kind === "node" && row.node.path === escapedUri))
      .toBe(false);
  });

  test("自引用目录不会造成无限递归", () => {
    const loopUri = `${workspaceRootUri}/loop`;
    const rows = buildVisibleFileTreeRows(rowOptions({
      directories: {
        [loopUri]: directory([{
          name: "loop",
          path: loopUri,
          kind: "directory",
          has_children: true,
          size: null,
          modified_at: null,
        }]),
      },
      expandedPaths: new Set([loopUri]),
      workspaceRootPath: loopUri,
    }));

    expect(rows.filter((row) => row.kind === "node" && row.node.path === loopUri))
      .toHaveLength(1);
  });

  test("搜索时自引用目录不会造成无限递归", () => {
    const loopUri = `${workspaceRootUri}/loop`;
    const rows = buildVisibleFileTreeRows(rowOptions({
      directories: {
        [loopUri]: directory([{
          name: "loop",
          path: loopUri,
          kind: "directory",
          has_children: true,
          size: null,
          modified_at: null,
        }]),
      },
      expandedPaths: new Set([loopUri]),
      searchQuery: "needle",
      workspaceRootPath: loopUri,
    }));

    expect(rows.filter((row) => row.kind === "node" && row.node.path === loopUri))
      .toHaveLength(1);
  });

  test("多个快捷根指向同一 URI 时各自恢复并渲染目录", () => {
    const sharedUri = `${filesystemRootUri}/shared`;
    const childUri = `${sharedUri}/a.ts`;
    const rows = buildVisibleFileTreeRows(rowOptions({
      directories: {
        [sharedUri]: directory([node("a.ts", childUri)]),
      },
      expandedPaths: new Set([sharedUri]),
      shortcuts: [
        { source: "workspace", label: "one", path: sharedUri },
        { source: "workspace", label: "two", path: sharedUri },
      ],
    }));

    expect(rows.filter((row) => row.kind === "node" && row.node.path === childUri))
      .toHaveLength(2);
    expect(rows.filter((row) => row.kind === "root" && row.treePath === sharedUri))
      .toHaveLength(2);
  });
});
