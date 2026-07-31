import type {
  FileTreeShortcut,
  WorkspaceFileNode,
} from "../../types/backend";
import type { DirectoryCacheEntry } from "./workspaceFileTreeCache";

export const FILE_TREE_VIRTUALIZATION_THRESHOLD = 300;

export type WorkspaceFileTreeRow =
  | {
      key: string;
      kind: "root";
      treePath: string;
      label: string;
      title: string;
      expanded: boolean;
      shortcutSource: "session" | "workspace" | null;
      icon: "shortcut" | "workspace" | "filesystem";
    }
  | {
      key: string;
      kind: "node";
      node: WorkspaceFileNode;
      depth: number;
      expanded: boolean;
    }
  | {
      key: string;
      kind: "status";
      status: "loading" | "error" | "empty" | "no-match" | "load-more" | "truncated";
      directoryPath: string;
      depth: number;
      text: string;
    };

interface BuildVisibleFileTreeRowsOptions {
  directories: Readonly<Record<string, DirectoryCacheEntry>>;
  expandedPaths: ReadonlySet<string>;
  shortcuts: readonly FileTreeShortcut[];
  searchQuery: string;
  workspaceLabel: string;
  workspaceTitle: string;
  workspaceRootPath: string;
  filesystemRootPath: string;
  shortcutPath: (path: string) => string;
}

export function buildVisibleFileTreeRows({
  directories,
  expandedPaths,
  shortcuts,
  searchQuery,
  workspaceLabel,
  workspaceTitle,
  workspaceRootPath,
  filesystemRootPath,
  shortcutPath,
}: BuildVisibleFileTreeRowsOptions): WorkspaceFileTreeRow[] {
  const rows: WorkspaceFileTreeRow[] = [];
  const normalizedQuery = searchQuery.trim().toLowerCase();
  const searchMatchCache = new Map<string, boolean>();

  const nodeMatchesSearch = (node: WorkspaceFileNode): boolean => {
    if (!normalizedQuery) {
      return true;
    }
    const cached = searchMatchCache.get(node.path);
    if (cached !== undefined) {
      return cached;
    }
    const matches = `${node.name}\n${node.path}`.toLowerCase().includes(normalizedQuery);
    const result = matches || (
      node.kind === "directory"
      && (directories[node.path]?.items.some(nodeMatchesSearch) ?? true)
    );
    searchMatchCache.set(node.path, result);
    return result;
  };

  const appendDirectory = (directoryPath: string, depth: number) => {
    const directory = directories[directoryPath];
    if (!directory || (directory.loading && directory.items.length === 0)) {
      rows.push({
        key: `status:loading:${directoryPath}`,
        kind: "status",
        status: "loading",
        directoryPath,
        depth,
        text: "正在读取...",
      });
      return;
    }
    if (directory.error) {
      rows.push({
        key: `status:error:${directoryPath}`,
        kind: "status",
        status: "error",
        directoryPath,
        depth,
        text: directory.error,
      });
      return;
    }
    const visibleItems = directory.items.filter(nodeMatchesSearch);
    if (directory.items.length === 0 || visibleItems.length === 0) {
      rows.push({
        key: `status:${directory.items.length === 0 ? "empty" : "no-match"}:${directoryPath}`,
        kind: "status",
        status: directory.items.length === 0 ? "empty" : "no-match",
        directoryPath,
        depth,
        text: directory.items.length === 0 ? "空目录" : "无匹配文件",
      });
      return;
    }
    for (const node of visibleItems) {
      const expanded = node.kind === "directory" && expandedPaths.has(node.path);
      rows.push({
        key: `node:${node.path}`,
        kind: "node",
        node,
        depth,
        expanded,
      });
      if (expanded) {
        appendDirectory(node.path, depth + 1);
      }
    }
    if (directory.nextCursor) {
      rows.push({
        key: `status:load-more:${directoryPath}`,
        kind: "status",
        status: "load-more",
        directoryPath,
        depth,
        text: directory.loading
          ? "正在加载下一页..."
          : `加载更多（当前 ${directory.items.length} 项）`,
      });
    } else if (directory.truncated) {
      rows.push({
        key: `status:truncated:${directoryPath}`,
        kind: "status",
        status: "truncated",
        directoryPath,
        depth,
        text: "目录仍有未加载项目，请刷新后重试",
      });
    }
  };

  const appendRoot = (
    treePath: string,
    label: string,
    title: string,
    shortcutSource: "session" | "workspace" | null,
    icon: "shortcut" | "workspace" | "filesystem",
  ) => {
    const expanded = expandedPaths.has(treePath);
    rows.push({
      key: `root:${treePath || "workspace"}`,
      kind: "root",
      treePath,
      label,
      title,
      expanded,
      shortcutSource,
      icon,
    });
    if (expanded) {
      appendDirectory(treePath, 0);
    }
  };

  for (const shortcut of shortcuts) {
    appendRoot(
      shortcutPath(shortcut.path),
      shortcut.label,
      shortcut.path,
      shortcut.source,
      "shortcut",
    );
  }
  appendRoot(
    workspaceRootPath,
    workspaceLabel,
    workspaceTitle,
    null,
    "workspace",
  );
  appendRoot(filesystemRootPath, "/", "/", null, "filesystem");
  return rows;
}
