import type {
  FileTreeShortcut,
  WorkspaceFileNode,
} from "../../../types/backend";
import type { DirectoryCacheEntry } from "./workspaceFileTreeCache";

export const FILE_TREE_VIRTUALIZATION_THRESHOLD = 300;

// 只有 kind === "directory" 的节点可以展开。后端列举目录时使用
// follow_symlinks=False（见 app/services/infrastructure/workspace_service.py），
// 符号链接一律返回 kind: "symlink" 且 has_children=false，是刻意的 fail-closed
// 设计：跟随符号链接既可能读到工作区外的内容，也可能因成环导致无限递归。
// 因此这里只认 "directory"，不得为了「支持 symlink 展开」放开。
export function isExpandableFileTreeNode(node: WorkspaceFileNode): boolean {
  return node.kind === "directory";
}

/**
 * 文件树按搜索词过滤的唯一命中判定。名称与路径分别匹配，绝不用分隔符把两者拼成
 * 一个字符串再 includes：那样「名字尾 + 路径头」会跨边界拼出假命中（例如名字
 * 含换行时 `` ${name}\n${path} `` 里两行会被当成一个连续串）。
 */
export function fileTreeNodeTextMatchesQuery(
  node: WorkspaceFileNode,
  normalizedQuery: string,
): boolean {
  if (!normalizedQuery) {
    return true;
  }
  return node.name.toLowerCase().includes(normalizedQuery)
    || node.path.toLowerCase().includes(normalizedQuery);
}

export type WorkspaceFileTreeSearchMatcher = (
  node: WorkspaceFileNode,
) => boolean;

export function createFileTreeSearchMatcher(
  directories: Readonly<Record<string, DirectoryCacheEntry>>,
  searchQuery: string,
): WorkspaceFileTreeSearchMatcher {
  const normalizedQuery = searchQuery.trim().toLowerCase();
  const matchCache = new Map<string, boolean>();
  const activeSearchChain = new Set<string>();

  const matchesNode = (node: WorkspaceFileNode): boolean => {
    if (!normalizedQuery) {
      return true;
    }
    const cached = matchCache.get(node.path);
    if (cached !== undefined) {
      return cached;
    }
    const directMatch = fileTreeNodeTextMatchesQuery(node, normalizedQuery);
    if (directMatch || !isExpandableFileTreeNode(node)) {
      matchCache.set(node.path, directMatch);
      return directMatch;
    }
    if (activeSearchChain.has(node.path)) {
      return true;
    }
    activeSearchChain.add(node.path);
    let matchesLoadedDescendant: boolean;
    try {
      matchesLoadedDescendant = directories[node.path]?.items.some(matchesNode) ?? true;
    } finally {
      activeSearchChain.delete(node.path);
    }
    matchCache.set(node.path, matchesLoadedDescendant);
    return matchesLoadedDescendant;
  };

  return matchesNode;
}

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
  searchMatcher?: WorkspaceFileTreeSearchMatcher;
  workspaceLabel: string;
  workspaceTitle: string;
  workspaceRootPath: string;
  filesystemRootPath: string;
  filesystemLabel: string;
}

export function buildVisibleFileTreeRows({
  directories,
  expandedPaths,
  shortcuts,
  searchQuery,
  searchMatcher,
  workspaceLabel,
  workspaceTitle,
  workspaceRootPath,
  filesystemRootPath,
  filesystemLabel,
}: BuildVisibleFileTreeRowsOptions): WorkspaceFileTreeRow[] {
  const rows: WorkspaceFileTreeRow[] = [];
  const nodeMatchesSearch = searchMatcher
    ?? (searchQuery.trim()
      ? createFileTreeSearchMatcher(directories, searchQuery)
      : undefined);
  // 展开是深度优先树遍历：后端构造子路径时始终是 `父路径/子项名`，子路径必然
  // 严格长于父路径，因此合法数据不可能成环。但损坏载荷可能给出自引用目录，
  // 使递归无限深入。这里只对**当前递归链**去重：合法数据中同一目录可在多个根下
  // 重复出现（例如多条快捷路径指向同一目录），那种重复必须照常渲染，不能全局去重。
  const activeDirectoryChain = new Set<string>();

  const appendDirectory = (directoryPath: string, depth: number) => {
    if (activeDirectoryChain.has(directoryPath)) {
      return;
    }
    activeDirectoryChain.add(directoryPath);
    const directory = directories[directoryPath];
    try {
      appendDirectoryContents(directoryPath, depth, directory);
    } finally {
      activeDirectoryChain.delete(directoryPath);
    }
  };

  const appendDirectoryContents = (
    directoryPath: string,
    depth: number,
    directory: DirectoryCacheEntry | undefined,
  ) => {
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
    const visibleItems = nodeMatchesSearch
      ? directory.items.filter(nodeMatchesSearch)
      : directory.items;
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
      const expanded = isExpandableFileTreeNode(node) && expandedPaths.has(node.path);
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
      shortcut.path,
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
  appendRoot(filesystemRootPath, filesystemLabel, filesystemLabel, null, "filesystem");
  return rows;
}
