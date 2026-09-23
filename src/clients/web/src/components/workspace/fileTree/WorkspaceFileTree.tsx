import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Virtuoso } from "react-virtuoso";
import {
  decodeFileTreePath,
  DEFAULT_BACKEND_PORT,
  filesystemFileTreePath,
  getSessionFileTreeSettings,
} from "../../../api";
import type {
  SessionFileTreeSettings,
  WorkspaceFileList,
  WorkspaceFileNode,
} from "../../../types/backend";
import {
  WORKSPACE_FILE_CHANGES_EVENT,
  type WorkspaceFileChangesEventDetail,
} from "../../../state/workspaceFileTreeEvents";
import { useWorkspaceFileWatch } from "../../../hooks/workspace/useWorkspaceFileWatch";
import { errorDisplayMessage } from "../../../utils/errorMessage";
import { formatByteSize } from "../../../utils/format";
import {
  loadedDirectoryEntry,
  markDirectoryStale,
  restoreDirectoriesInOrder,
} from "./workspaceFileTreeCache";
import { useWorkspaceFileTreeDirectories } from "./useWorkspaceFileTreeDirectories";
import {
  useWorkspaceFileTreeContextMenu,
  type FileTreeContextMenuTarget,
  type WorkspaceFileTreeContextMenuApi,
} from "./useWorkspaceFileTreeContextMenu";
import WorkspaceFileTreeContextMenu from "./WorkspaceFileTreeContextMenu";
import {
  buildVisibleFileTreeRows,
  FILE_TREE_VIRTUALIZATION_THRESHOLD,
  fileTreeNodeMatchesQuery,
  isExpandableFileTreeNode,
  type WorkspaceFileTreeRow,
} from "./workspaceFileTreeRows";
import {
  absolutePathForTreePath as resolveTreeAbsolutePath,
  changedPathToTreePath,
  FILESYSTEM_ROOT_PATH,
  isTreePathInside,
  parentFileTreePath,
  ROOT_PATH,
  shortWorkspaceLabel,
} from "./workspaceFileTreePaths";

interface WorkspaceFileTreeProps {
  active: boolean;
  apiPort: number | null;
  workspaceId: string | null;
  workspaceName: string | null;
  workspaceRoot: string | null;
  sessionId: string;
  activeFilePath: string | null;
  searchOpen: boolean;
  collapseVersion: number;
  expandedPaths: string[];
  onExpandedPathsChange: (paths: string[]) => void;
  onCloseSearch: () => void;
  onOpenFile: (node: WorkspaceFileNode) => void;
  onStatusChange: (text: string) => void;
}

const SESSION_AUXILIARY_LOAD_DELAY_MS = 200;

function fileIcon(node: WorkspaceFileNode): string {
  if (node.kind === "directory") {
    return "▣";
  }
  if (node.kind === "symlink") {
    return "↪";
  }
  return "◇";
}

export default function WorkspaceFileTree({
  active,
  apiPort,
  workspaceId,
  workspaceName,
  workspaceRoot,
  sessionId,
  activeFilePath,
  searchOpen,
  collapseVersion,
  expandedPaths: restoredExpandedPaths,
  onExpandedPathsChange,
  onCloseSearch,
  onOpenFile,
  onStatusChange,
}: WorkspaceFileTreeProps) {
  const port = apiPort ?? DEFAULT_BACKEND_PORT;
  const rootLabel = useMemo(
    () => shortWorkspaceLabel(workspaceRoot, workspaceName),
    [workspaceName, workspaceRoot],
  );
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(
    () => new Set(restoredExpandedPaths),
  );
  const [searchQuery, setSearchQuery] = useState("");
  const [settings, setSettings] = useState<SessionFileTreeSettings | null>(null);
  // 快捷路径设置读取失败的独立终态：settings 保持 null 是「尚未读到」，
  // 若无此状态，设置接口一旦失败就再也无法区分「加载中」与「加载失败」，
  // 顶部会永久停在「正在加载工作区文件…」。
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [settingsReloadNonce, setSettingsReloadNonce] = useState(0);
  const lastCollapseVersionRef = useRef(collapseVersion);
  const restoredExpandedPathsRef = useRef(restoredExpandedPaths);
  const shortcutTreePathsRef = useRef<Set<string>>(new Set());
  // 搜索递归的当前递归链去重集合，避免损坏载荷造成无限递归。
  const searchMatchChainRef = useRef<Set<string>>(new Set());
  const activeFilePathRef = useRef(activeFilePath);
  const activeRef = useRef(active);
  const previousActiveRef = useRef(active);
  const expandedPathsRef = useRef(expandedPaths);
  const pendingExpandedPersistenceRef = useRef<{
    callback: (paths: string[]) => void;
    paths: string[];
  } | null>(null);
  const expandedPersistenceTimerRef = useRef<number | null>(null);
  const pendingFileChangesRef = useRef<Map<string, { kind: string; path: string }>>(
    new Map(),
  );
  const fileChangeFlushTimerRef = useRef<number | null>(null);

  const {
    directories,
    directoriesRef,
    updateDirectories,
    loadDirectory,
    refreshExpandedDirectories,
    invalidateDirectoriesUnder,
    abortAllDirectoryRequests,
    resetDirectories,
  } = useWorkspaceFileTreeDirectories({
    port,
    workspaceId,
    expandedPathsRef,
    shortcutTreePathsRef,
    activeFilePathRef,
    onStatusChange,
  });

  const acceptFileTreeSettings = useCallback((result: SessionFileTreeSettings) => {
    shortcutTreePathsRef.current = new Set(
      (result.effective_shortcuts ?? []).map((shortcut) => (
        filesystemFileTreePath(shortcut.path)
      )),
    );
    setSettings(result);
    updateDirectories((current) => current);
  }, [updateDirectories]);

  const commitExpandedPaths = (next: Set<string>) => {
    expandedPathsRef.current = next;
    setExpandedPaths(next);
  };

  const flushExpandedPathsPersistence = useCallback(() => {
    if (expandedPersistenceTimerRef.current !== null) {
      window.clearTimeout(expandedPersistenceTimerRef.current);
      expandedPersistenceTimerRef.current = null;
    }
    const pending = pendingExpandedPersistenceRef.current;
    pendingExpandedPersistenceRef.current = null;
    pending?.callback(pending.paths);
  }, []);

  const scheduleExpandedPathsPersistence = useCallback((paths: string[]) => {
    pendingExpandedPersistenceRef.current = {
      callback: onExpandedPathsChange,
      paths,
    };
    if (expandedPersistenceTimerRef.current !== null) {
      window.clearTimeout(expandedPersistenceTimerRef.current);
    }
    expandedPersistenceTimerRef.current = window.setTimeout(
      flushExpandedPathsPersistence,
      250,
    );
  }, [flushExpandedPathsPersistence, onExpandedPathsChange]);
  const scheduleExpandedPathsPersistenceRef = useRef(
    scheduleExpandedPathsPersistence,
  );

  useEffect(() => {
    scheduleExpandedPathsPersistenceRef.current = scheduleExpandedPathsPersistence;
  }, [scheduleExpandedPathsPersistence]);

  useEffect(() => {
    restoredExpandedPathsRef.current = restoredExpandedPaths;
  }, [restoredExpandedPaths]);

  useEffect(() => {
    activeFilePathRef.current = activeFilePath;
    updateDirectories((current) => current);
  }, [activeFilePath, updateDirectories]);

  useEffect(() => {
    updateDirectories((current) => current);
  }, [expandedPaths, updateDirectories]);

  useEffect(() => flushExpandedPathsPersistence, [
    flushExpandedPathsPersistence,
    workspaceId,
  ]);

  const watchedShortcutPaths = useMemo(
    () => (settings?.effective_shortcuts ?? [])
      .map((shortcut) => shortcut.path)
      .filter((path) => !/^\/$|^[A-Za-z]:[\\/]?$/.test(path.trim())),
    [settings],
  );
  useWorkspaceFileWatch({
    active: active && (!sessionId || settings !== null),
    port,
    workspaceId,
    paths: watchedShortcutPaths,
    onOverflow: refreshExpandedDirectories,
    onStatusChange,
  });

  useEffect(() => {
    const flushFileChanges = () => {
      fileChangeFlushTimerRef.current = null;
      const changes = [...pendingFileChangesRef.current.values()];
      pendingFileChangesRef.current.clear();
      const changedParents = new Set<string>();
      let nextExpanded = new Set(expandedPathsRef.current);
      let expandedChanged = false;

      // 删除必须先让对应子树整体失效：丢弃缓存并中止在途请求，
      // 否则迟到的响应会把已删除目录重新写回缓存。
      const deletedTreePaths: string[] = [];
      for (const change of changes) {
        if (change.kind !== "delete") {
          continue;
        }
        const treePath = changedPathToTreePath(change.path, workspaceRoot);
        if (treePath === null) {
          continue;
        }
        deletedTreePaths.push(treePath);
        for (const expandedPath of nextExpanded) {
          if (isTreePathInside(expandedPath, treePath)) {
            nextExpanded.delete(expandedPath);
            expandedChanged = true;
          }
        }
      }
      for (const treePath of deletedTreePaths) {
        invalidateDirectoriesUnder(treePath);
      }

      updateDirectories((current) => {
        const next = { ...current };
        for (const change of changes) {
          const treePath = changedPathToTreePath(change.path, workspaceRoot);
          if (treePath === null) {
            onStatusChange(`忽略无法定位的文件变更路径: ${change.path}`);
            continue;
          }
          changedParents.add(parentFileTreePath(treePath));
        }
        for (const parentPath of changedParents) {
          const entry = next[parentPath];
          if (
            entry
            && (!activeRef.current || !expandedPathsRef.current.has(parentPath))
          ) {
            next[parentPath] = markDirectoryStale(entry);
          }
        }
        return next;
      });

      if (expandedChanged) {
        commitExpandedPaths(nextExpanded);
        scheduleExpandedPathsPersistenceRef.current([...nextExpanded].sort());
      }
      for (const parentPath of changedParents) {
        if (
          activeRef.current
          &&
          expandedPathsRef.current.has(parentPath)
          && directoriesRef.current[parentPath]
        ) {
          void loadDirectory(parentPath, true);
        }
      }
    };

    const handleFileChanges = (event: Event) => {
      const detail = (event as CustomEvent<WorkspaceFileChangesEventDetail>).detail;
      if (detail.workspaceId && detail.workspaceId !== workspaceId) {
        return;
      }
      for (const change of detail.changes) {
        pendingFileChangesRef.current.set(`${change.kind}:${change.path}`, change);
      }
      if (fileChangeFlushTimerRef.current !== null) {
        window.clearTimeout(fileChangeFlushTimerRef.current);
      }
      fileChangeFlushTimerRef.current = window.setTimeout(flushFileChanges, 250);
    };

    window.addEventListener(WORKSPACE_FILE_CHANGES_EVENT, handleFileChanges);
    return () => {
      window.removeEventListener(WORKSPACE_FILE_CHANGES_EVENT, handleFileChanges);
      if (fileChangeFlushTimerRef.current !== null) {
        window.clearTimeout(fileChangeFlushTimerRef.current);
        fileChangeFlushTimerRef.current = null;
      }
      pendingFileChangesRef.current.clear();
    };
  }, [
    loadDirectory,
    onStatusChange,
    updateDirectories,
    invalidateDirectoriesUnder,
    workspaceId,
    workspaceRoot,
  ]);

  const absolutePathForTreePath = useCallback(
    (treePath: string): string => resolveTreeAbsolutePath(treePath, workspaceRoot),
    [workspaceRoot],
  );

  const displayPathForTreePath = useCallback((treePath: string): string => {
    const location = decodeFileTreePath(treePath);
    if (location.scope === "filesystem" || workspaceRoot) {
      return absolutePathForTreePath(treePath);
    }
    return location.path || rootLabel;
  }, [absolutePathForTreePath, rootLabel, workspaceRoot]);

  useEffect(() => {
    shortcutTreePathsRef.current.clear();
    setSettings(null);
    setSettingsError(null);
    updateDirectories((current) => current);
    if (!sessionId) {
      return;
    }
    let cancelled = false;
    const timerId = window.setTimeout(() => {
      void getSessionFileTreeSettings(port, sessionId, workspaceId)
        .then((result) => {
          if (!cancelled) {
            acceptFileTreeSettings(result);
            setSettingsError(null);
          }
        })
        .catch((error: unknown) => {
          if (!cancelled) {
            const message = errorDisplayMessage(error);
            setSettingsError(message);
            onStatusChange(`快捷路径加载失败: ${message}`);
          }
        });
    }, SESSION_AUXILIARY_LOAD_DELAY_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timerId);
    };
  }, [
    acceptFileTreeSettings,
    onStatusChange,
    port,
    sessionId,
    settingsReloadNonce,
    updateDirectories,
    workspaceId,
  ]);

  useEffect(() => {
    abortAllDirectoryRequests();
    const restoredPaths = new Set(restoredExpandedPathsRef.current);
    commitExpandedPaths(restoredPaths);
    resetDirectories();
    if (activeRef.current) {
      void restoreDirectoriesInOrder(
        [...restoredPaths],
        (path) => loadDirectory(path),
        parentFileTreePath,
      );
    }
    return () => {
      abortAllDirectoryRequests();
    };
  }, [abortAllDirectoryRequests, loadDirectory, resetDirectories, workspaceId, workspaceRoot]);

  useEffect(() => {
    const resumedAfterPause = !previousActiveRef.current && active;
    previousActiveRef.current = active;
    activeRef.current = active;
    if (!active) {
      const abortedPaths = abortAllDirectoryRequests();
      updateDirectories((current) => {
        const next = { ...current };
        for (const path of abortedPaths) {
          const entry = next[path];
          if (entry) {
            next[path] = { ...entry, loading: false };
          }
        }
        return next;
      });
      return;
    }
    const pathsToRestore = [...expandedPathsRef.current].filter((path) => {
      const entry = directoriesRef.current[path];
      return resumedAfterPause || !entry || entry.stale;
    });
    void restoreDirectoriesInOrder(
      pathsToRestore,
      (path) => loadDirectory(
        path,
        resumedAfterPause || Boolean(directoriesRef.current[path]?.stale),
      ),
      parentFileTreePath,
    );
  }, [abortAllDirectoryRequests, active, loadDirectory, updateDirectories]);

  useEffect(() => {
    if (lastCollapseVersionRef.current === collapseVersion) {
      return;
    }
    lastCollapseVersionRef.current = collapseVersion;
    const collapsedPaths = [ROOT_PATH];
    commitExpandedPaths(new Set(collapsedPaths));
    scheduleExpandedPathsPersistence(collapsedPaths);
    onStatusChange("文件树已全部折叠");
  }, [collapseVersion, onStatusChange, scheduleExpandedPathsPersistence]);

  useEffect(() => {
    if (!searchOpen) {
      setSearchQuery("");
    }
  }, [searchOpen]);

  const toggleDirectory = (path: string, status: string) => {
    const next = new Set(expandedPathsRef.current);
    const wasExpanded = next.has(path);
    if (wasExpanded) {
      next.delete(path);
    } else {
      next.add(path);
    }
    commitExpandedPaths(next);
    scheduleExpandedPathsPersistence([...next].sort());
    if (!wasExpanded) {
      const cached = directoriesRef.current[path];
      if (cached) {
        updateDirectories((current) => ({
          ...current,
          [path]: {
            ...cached,
            lastAccessedAt: Date.now(),
          },
        }));
      }
      if (!cached || cached.stale) {
        void loadDirectory(path, Boolean(cached?.stale));
      }
    }
    onStatusChange(status);
  };

  const replaceDirectory = (result: WorkspaceFileList) => {
    updateDirectories((prev) => ({
      ...prev,
      [result.path]: loadedDirectoryEntry(result, Date.now()),
    }));
    if (!expandedPathsRef.current.has(result.path)) {
      const next = new Set(expandedPathsRef.current);
      next.add(result.path);
      commitExpandedPaths(next);
      scheduleExpandedPathsPersistence([...next].sort());
    }
  };

  const menu: WorkspaceFileTreeContextMenuApi = useWorkspaceFileTreeContextMenu({
    port,
    workspaceId,
    sessionId,
    absolutePathForTreePath,
    replaceDirectory,
    loadDirectory,
    acceptFileTreeSettings,
    onStatusChange,
  });
  const openContextMenu = menu.openContextMenu;

  const handleNodeClick = (node: WorkspaceFileNode) => {
    if (!isExpandableFileTreeNode(node)) {
      const size = formatByteSize(node.size);
      onOpenFile(node);
      const absolutePath = absolutePathForTreePath(node.path);
      onStatusChange(size ? `${absolutePath} · ${size}` : absolutePath);
      return;
    }
    toggleDirectory(node.path, absolutePathForTreePath(node.path));
  };

  // 与 buildVisibleFileTreeRows 的搜索判定保持同一语义：目录才向下递归，
  // 并对当前递归链去重，避免损坏载荷造成栈溢出。
  const nodeMatchesSearch = (node: WorkspaceFileNode): boolean => {
    const normalizedQuery = searchQuery.trim().toLowerCase();
    if (!normalizedQuery) {
      return true;
    }
    const nodeTextMatches = fileTreeNodeMatchesQuery(node, normalizedQuery);
    if (nodeTextMatches || !isExpandableFileTreeNode(node)) {
      return nodeTextMatches;
    }
    if (searchMatchChainRef.current.has(node.path)) {
      return true;
    }
    const loadedDirectory = directories[node.path];
    if (!loadedDirectory) {
      return true;
    }
    searchMatchChainRef.current.add(node.path);
    try {
      return loadedDirectory.items.some(nodeMatchesSearch);
    } finally {
      searchMatchChainRef.current.delete(node.path);
    }
  };

  const renderDirectory = (path: string, depth: number) => {
    const directory = directories[path];
    if (!directory || (directory.loading && directory.items.length === 0)) {
      return (
        <div className="files-tree-item files-tree-loading" style={{ paddingLeft: `${22 + depth * 14}px` }}>
          <span className="file-icon">◇</span>
          <span className="file-label">正在读取...</span>
        </div>
      );
    }

    if (directory.error) {
      return (
        <div className="files-tree-error" style={{ marginLeft: `${22 + depth * 14}px` }}>
          <span>{directory.error}</span>
          <button type="button" onClick={() => void loadDirectory(path)}>
            重试
          </button>
        </div>
      );
    }

    const visibleItems = directory.items.filter(nodeMatchesSearch);

    if (directory.items.length === 0) {
      return (
        <div className="files-tree-item muted" style={{ paddingLeft: `${22 + depth * 14}px` }}>
          <span className="file-icon">◇</span>
          <span className="file-label">空目录</span>
        </div>
      );
    }

    if (visibleItems.length === 0) {
      return (
        <div className="files-tree-item muted" style={{ paddingLeft: `${22 + depth * 14}px` }}>
          <span className="file-icon">◇</span>
          <span className="file-label">无匹配文件</span>
        </div>
      );
    }

    return (
      <>
        {visibleItems.map((node) => renderNode(node, depth))}
        {directory.nextCursor ? (
          <button
            type="button"
            className="files-tree-load-more"
            style={{ marginLeft: `${22 + depth * 14}px` }}
            disabled={directory.loading}
            onClick={() => void loadDirectory(path, false, true)}
          >
            {directory.loading
              ? "正在加载下一页..."
              : `加载更多（当前 ${directory.items.length} 项）`}
          </button>
        ) : directory.truncated ? (
          <div className="files-tree-note" style={{ marginLeft: `${22 + depth * 14}px` }}>
            目录仍有未加载项目，请刷新后重试
          </div>
        ) : null}
      </>
    );
  };

  // 文件树节点行的唯一渲染实现：树形递归路径与虚拟滚动扁平行共用，
  // 避免两处逐字重复维护出偏移。
  const renderNodeButton = (
    node: WorkspaceFileNode,
    depth: number,
    expanded: boolean,
  ) => {
    const isDirectory = isExpandableFileTreeNode(node);
    return (
      <button
        type="button"
        className={`files-tree-item files-tree-row${isDirectory ? " directory" : ""}${activeFilePath === node.path ? " active" : ""}`}
        title={displayPathForTreePath(node.path)}
        style={{ paddingLeft: `${8 + depth * 14}px` }}
        onClick={() => handleNodeClick(node)}
        onContextMenu={(event) => openContextMenu(
          event,
          node.path,
          node.name,
          node.kind,
        )}
      >
        <span
          className={`codicon files-tree-chevron${isDirectory ? ` codicon-chevron-${expanded ? "down" : "right"}` : ""}`}
          aria-hidden="true"
        />
        <span className={`file-icon ${node.kind}`}>{fileIcon(node)}</span>
        <span className="file-label">{node.name}</span>
        {node.kind === "file" ? (
          <span className="files-tree-meta">{formatByteSize(node.size)}</span>
        ) : null}
      </button>
    );
  };

  const renderNode = (node: WorkspaceFileNode, depth: number) => {
    const isDirectory = isExpandableFileTreeNode(node);
    const expanded = expandedPaths.has(node.path);
    return (
      <div className="files-tree-node" key={node.path}>
        {renderNodeButton(node, depth, expanded)}
        {isDirectory && expanded ? renderDirectory(node.path, depth + 1) : null}
      </div>
    );
  };

  const shortcuts = settings?.effective_shortcuts ?? [];
  const defaultShortcutPaths = new Set(
    (settings?.default_shortcuts ?? []).map((shortcut) => shortcut.path),
  );
  const rootExpanded = expandedPaths.has(ROOT_PATH);
  const filesystemRootExpanded = expandedPaths.has(FILESYSTEM_ROOT_PATH);
  const visibleRows = useMemo(() => buildVisibleFileTreeRows({
    directories,
    expandedPaths,
    shortcuts,
    searchQuery,
    workspaceLabel: rootLabel,
    workspaceTitle: workspaceRoot || rootLabel,
    workspaceRootPath: ROOT_PATH,
    filesystemRootPath: FILESYSTEM_ROOT_PATH,
    shortcutPath: filesystemFileTreePath,
  }), [
    directories,
    expandedPaths,
    rootLabel,
    searchQuery,
    shortcuts,
    workspaceRoot,
  ]);

  const searchStatus = useMemo(() => {
    const query = searchQuery.trim();
    if (!query) {
      return null;
    }
    const loadedDirectories = Object.values(directories).filter(
      (directory) => !directory.loading && !directory.error,
    );
    if (loadedDirectories.length === 0) {
      return "正在筛选已加载文件...";
    }
    const normalizedQuery = query.toLowerCase();
    let hasDirectMatch = false;
    let hasUnloadedDirectory = false;
    for (const directory of loadedDirectories) {
      for (const node of directory.items) {
        if (fileTreeNodeMatchesQuery(node, normalizedQuery)) {
          hasDirectMatch = true;
          break;
        }
        if (isExpandableFileTreeNode(node) && !directories[node.path]) {
          hasUnloadedDirectory = true;
        }
      }
      if (hasDirectMatch) {
        break;
      }
    }
    if (hasDirectMatch) {
      return `正在筛选：${query}`;
    }
    return hasUnloadedDirectory
      ? `未找到已加载文件“${query}”（展开目录后可继续搜索）`
      : `未找到“${query}”`;
  }, [directories, searchQuery]);

  const directoryLoading = Object.values(directories).some((directory) => directory.loading);
  const fileTreeLoading = Boolean(
    active && sessionId && (
      (settings === null && settingsError === null) || directoryLoading
    ),
  );
  const showSettingsError = active && sessionId !== "" && settingsError !== null;

  const renderFlatRow = (row: WorkspaceFileTreeRow) => {
    if (row.kind === "root") {
      return (
        <button
          type="button"
          className="files-tree-item root files-tree-row"
          title={row.title}
          aria-expanded={row.expanded}
          onClick={() => toggleDirectory(row.treePath, row.title)}
          onContextMenu={(event) => openContextMenu(
            event,
            row.treePath,
            row.label,
            "directory",
            row.shortcutSource,
          )}
        >
          <span
            className={`codicon files-tree-chevron codicon-chevron-${row.expanded ? "down" : "right"}`}
            aria-hidden="true"
          />
          {row.icon === "shortcut" ? (
            <span className="codicon codicon-bookmark file-icon" aria-hidden="true" />
          ) : row.icon === "filesystem" ? (
            <span className="codicon codicon-file-directory file-icon directory" aria-hidden="true" />
          ) : (
            <span className="file-icon directory">▣</span>
          )}
          <span className="file-label">{row.label}</span>
          {row.icon === "shortcut" ? (
            <span className="files-tree-shortcut-kind">快捷路径</span>
          ) : null}
        </button>
      );
    }
    if (row.kind === "node") {
      return renderNodeButton(row.node, row.depth, row.expanded);
    }
    if (row.status === "error") {
      return (
        <div className="files-tree-error" style={{ marginLeft: `${22 + row.depth * 14}px` }}>
          <span>{row.text}</span>
          <button type="button" onClick={() => void loadDirectory(row.directoryPath)}>
            重试
          </button>
        </div>
      );
    }
    if (row.status === "load-more") {
      return (
        <button
          type="button"
          className="files-tree-load-more"
          style={{ marginLeft: `${22 + row.depth * 14}px` }}
          disabled={directories[row.directoryPath]?.loading}
          onClick={() => void loadDirectory(row.directoryPath, false, true)}
        >
          {row.text}
        </button>
      );
    }
    if (row.status === "truncated") {
      return (
        <div className="files-tree-note" style={{ marginLeft: `${22 + row.depth * 14}px` }}>
          {row.text}
        </div>
      );
    }
    return (
      <div
        className={`files-tree-item${row.status === "loading" ? " files-tree-loading" : " muted"}`}
        style={{ paddingLeft: `${22 + row.depth * 14}px` }}
      >
        <span className="file-icon">◇</span>
        <span className="file-label">{row.text}</span>
      </div>
    );
  };

  return (
    <div className="workspace-file-tree">
      {searchOpen ? (
        <div className="files-tree-search">
          <input
            type="search"
            value={searchQuery}
            placeholder="筛选文件"
            aria-label="筛选文件"
            autoFocus
            onChange={(event) => setSearchQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                onCloseSearch();
              }
            }}
          />
          {searchQuery ? (
            <button
              type="button"
              className="files-tree-search-clear"
              aria-label="清除文件搜索"
              title="清除搜索"
              onClick={() => setSearchQuery("")}
            >
              <span className="codicon codicon-close" aria-hidden="true" />
            </button>
          ) : null}
        </div>
      ) : null}
      {menu.actionError ? (
        <div className="files-tree-action-error" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>{menu.actionError}</span>
          <button
            type="button"
            aria-label="关闭文件操作错误"
            onClick={menu.clearActionError}
          >
            <span className="codicon codicon-close" aria-hidden="true" />
          </button>
        </div>
      ) : null}
      {showSettingsError ? (
        <div className="files-tree-error files-tree-settings-error" role="alert">
          <span>快捷路径加载失败：{settingsError}</span>
          <button
            type="button"
            className="files-tree-settings-retry"
            onClick={() => setSettingsReloadNonce((nonce) => nonce + 1)}
          >
            重试
          </button>
        </div>
      ) : null}
      {searchStatus ? (
        <div className="files-tree-search-status" role="status">
          <span>{searchStatus}</span>
          {searchQuery ? (
            <button type="button" onClick={() => setSearchQuery("")}>清除</button>
          ) : null}
        </div>
      ) : null}
      <div className="files-tree-root" role="tree" aria-label="工作区文件树" aria-busy={fileTreeLoading}>
        {visibleRows.length > FILE_TREE_VIRTUALIZATION_THRESHOLD ? (
          <Virtuoso
            className="files-tree-virtualized"
            data={visibleRows}
            computeItemKey={(_, row) => row.key}
            increaseViewportBy={240}
            itemContent={(_, row) => renderFlatRow(row)}
          />
        ) : (
          <>
        {shortcuts.map((shortcut) => {
          const treePath = filesystemFileTreePath(shortcut.path);
          const expanded = expandedPaths.has(treePath);
          return (
            <div className="files-tree-shortcut" key={shortcut.path}>
              <div className="files-tree-quick-row">
                <button
                  type="button"
                  className="files-tree-item root files-tree-row files-tree-quick-main"
                  title={shortcut.path}
                  aria-expanded={expanded}
                  onClick={() => toggleDirectory(treePath, shortcut.path)}
                  onContextMenu={(event) => openContextMenu(
                    event,
                    treePath,
                    shortcut.label,
                    "directory",
                    shortcut.source,
                  )}
                >
                  <span
                    className={`codicon files-tree-chevron codicon-chevron-${expanded ? "down" : "right"}`}
                    aria-hidden="true"
                  />
                  <span className="codicon codicon-bookmark file-icon" aria-hidden="true" />
                  <span className="file-label">{shortcut.label}</span>
                  <span className="files-tree-shortcut-kind">快捷路径</span>
                </button>
              </div>
              {expanded ? renderDirectory(treePath, 0) : null}
            </div>
          );
        })}
        <button
          type="button"
          className="files-tree-item root files-tree-row"
          title={workspaceRoot || rootLabel}
          aria-expanded={rootExpanded}
          onClick={() => toggleDirectory(
            ROOT_PATH,
            workspaceRoot || rootLabel,
          )}
          onContextMenu={(event) => openContextMenu(
            event,
            ROOT_PATH,
            rootLabel,
            "directory",
          )}
        >
          <span
            className={`codicon files-tree-chevron codicon-chevron-${rootExpanded ? "down" : "right"}`}
            aria-hidden="true"
          />
          <span className="file-icon directory">▣</span>
          <span className="file-label">{rootLabel}</span>
        </button>
        {rootExpanded ? renderDirectory(ROOT_PATH, 0) : null}
        <button
          type="button"
          className="files-tree-item root files-tree-row"
          title="/"
          aria-expanded={filesystemRootExpanded}
          onClick={() => toggleDirectory(FILESYSTEM_ROOT_PATH, "/")}
          onContextMenu={(event) => openContextMenu(
            event,
            FILESYSTEM_ROOT_PATH,
            "/",
            "directory",
          )}
        >
          <span
            className={`codicon files-tree-chevron codicon-chevron-${filesystemRootExpanded ? "down" : "right"}`}
            aria-hidden="true"
          />
          <span className="codicon codicon-file-directory file-icon directory" aria-hidden="true" />
          <span className="file-label">/</span>
        </button>
        {filesystemRootExpanded ? renderDirectory(FILESYSTEM_ROOT_PATH, 0) : null}
          </>
        )}
      </div>
      {fileTreeLoading ? (
        <div className="files-tree-loading-status" role="status">
          <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />
          <span>正在加载工作区文件…</span>
        </div>
      ) : null}
      <input
        ref={menu.uploadInputRef}
        type="file"
        multiple
        hidden
        aria-label="上传本地文件"
        onChange={(event) => {
          const files = Array.from(event.currentTarget.files ?? []);
          event.currentTarget.value = "";
          menu.handleUploadInput(files);
        }}
      />
      <WorkspaceFileTreeContextMenu
        menu={menu}
        shortcuts={shortcuts}
        defaultShortcutPaths={defaultShortcutPaths}
      />
    </div>
  );
}
