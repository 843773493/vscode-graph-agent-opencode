import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { Virtuoso } from "react-virtuoso";
import {
  addSessionFileTreeShortcut,
  applyFileTreeShortcutToWorkspace,
  copyWorkspaceFileEntry,
  createWorkspaceFileDownloadRequest,
  createWorkspaceFileEntry,
  decodeFileTreePath,
  DEFAULT_BACKEND_PORT,
  filesystemFileTreePath,
  getSessionFileTreeSettings,
  getWorkspaceFiles,
  pasteWorkspaceFileEntries,
  removeSessionFileTreeShortcut,
  revealWorkspaceFileEntry,
  uploadWorkspaceFileEntries,
} from "../../api";
import type {
  FileTreeShortcut,
  SessionFileTreeSettings,
  WorkspaceFileList,
  WorkspaceFileNode,
} from "../../types/backend";
import type { WorkspaceFileLocation } from "../../api";
import {
  copyTextToClipboard,
  readFilePathTextFromClipboardData,
  readFilePathTextFromClipboard,
} from "../../utils/clipboard";
import {
  filesFromClipboardData,
  getFileTransferHost,
} from "../../utils/fileTransferHost";
import {
  WORKSPACE_FILE_CHANGES_EVENT,
  type WorkspaceFileChangesEventDetail,
} from "../../state/workspaceFileTreeEvents";
import { useWorkspaceFileWatch } from "../../hooks/useWorkspaceFileWatch";
import AnchoredOverlay from "../AnchoredOverlay";
import {
  type DirectoryCacheEntry,
  pruneDirectoryCache,
  restoreDirectoriesInOrder,
} from "./workspaceFileTreeCache";
import {
  buildVisibleFileTreeRows,
  FILE_TREE_VIRTUALIZATION_THRESHOLD,
  type WorkspaceFileTreeRow,
} from "./workspaceFileTreeRows";

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

const ROOT_PATH = "";
const FILESYSTEM_ROOT_PATH = filesystemFileTreePath("/");

interface FileTreeContextMenu {
  treePath: string;
  absolutePath: string;
  label: string;
  kind: WorkspaceFileNode["kind"];
  shortcutSource: "session" | "workspace" | null;
  x: number;
  y: number;
}

interface WorkspaceClipboardEntry {
  location: WorkspaceFileLocation;
  absolutePath: string;
  label: string;
  workspaceId: string | null;
}

interface DirectoryRequest {
  controller: AbortController;
  promise: Promise<boolean>;
}

export async function runCurrentAndDefaultShortcutMutation(
  updateCurrentSession: () => Promise<SessionFileTreeSettings>,
  updateWorkspaceDefault: () => Promise<SessionFileTreeSettings>,
  recoverAuthoritativeState: () => Promise<void>,
): Promise<SessionFileTreeSettings> {
  try {
    await updateCurrentSession();
    return await updateWorkspaceDefault();
  } catch (mutationError) {
    try {
      await recoverAuthoritativeState();
    } catch (recoveryError) {
      throw new AggregateError(
        [mutationError, recoveryError],
        "快捷路径组合操作失败，且重新同步后端状态失败",
      );
    }
    throw mutationError;
  }
}

function fileIcon(node: WorkspaceFileNode): string {
  if (node.kind === "directory") {
    return "▣";
  }
  if (node.kind === "symlink") {
    return "↪";
  }
  return "◇";
}

function formatFileSize(size: number | null | undefined): string {
  if (typeof size !== "number") {
    return "";
  }
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function shortWorkspaceLabel(workspaceRoot: string | null, workspaceName: string | null): string {
  return workspaceName || workspaceRoot?.split(/[\\/]/).filter(Boolean).pop() || "workspace";
}

function parentFileTreePath(treePath: string): string {
  const location = decodeFileTreePath(treePath);
  const normalized = location.path.replace(/\\/g, "/").replace(/\/$/, "");
  const separatorIndex = normalized.lastIndexOf("/");
  if (location.scope === "workspace") {
    return separatorIndex < 0 ? ROOT_PATH : normalized.slice(0, separatorIndex);
  }
  let parent = separatorIndex <= 0 ? "/" : normalized.slice(0, separatorIndex);
  if (/^[A-Za-z]:$/.test(parent)) {
    parent += "/";
  }
  return filesystemFileTreePath(parent);
}

function changedPathToTreePath(
  path: string,
  workspaceRoot: string | null,
): string | null {
  const normalized = path.trim().replace(/\\/g, "/");
  if (!normalized) {
    return null;
  }
  const isAbsolute = normalized.startsWith("/") || /^[A-Za-z]:\//.test(normalized);
  if (!isAbsolute) {
    const relative = normalized.replace(/^\.\//, "");
    if (relative.split("/").includes("..")) {
      return null;
    }
    return relative;
  }
  const rawRoot = workspaceRoot?.replace(/\\/g, "/") ?? "";
  const normalizedRoot = rawRoot === "/" ? "/" : rawRoot.replace(/\/$/, "");
  const comparePath = /^[A-Za-z]:\//.test(normalized)
    ? normalized.toLowerCase()
    : normalized;
  const compareRoot = /^[A-Za-z]:\//.test(normalizedRoot)
    ? normalizedRoot.toLowerCase()
    : normalizedRoot;
  if (compareRoot && comparePath === compareRoot) {
    return ROOT_PATH;
  }
  if (compareRoot === "/" && comparePath.startsWith("/")) {
    return normalized.slice(1);
  }
  if (compareRoot && comparePath.startsWith(`${compareRoot}/`)) {
    return normalized.slice(normalizedRoot.length + 1);
  }
  return filesystemFileTreePath(normalized);
}

function isTreePathInside(candidate: string, ancestor: string): boolean {
  const candidateLocation = decodeFileTreePath(candidate);
  const ancestorLocation = decodeFileTreePath(ancestor);
  if (candidateLocation.scope !== ancestorLocation.scope) {
    return false;
  }
  const candidatePath = candidateLocation.path.replace(/\\/g, "/").replace(/\/$/, "");
  const ancestorPath = ancestorLocation.path.replace(/\\/g, "/").replace(/\/$/, "");
  if (!ancestorPath) {
    return true;
  }
  return candidatePath === ancestorPath || candidatePath.startsWith(`${ancestorPath}/`);
}

export function parseClipboardFilePaths(text: string): [string, ...string[]] {
  const paths = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => (
      line
      && line !== "copy"
      && line !== "cut"
      && !line.startsWith("#")
    ))
    .map((line) => {
      const unquoted = line.length >= 2 && line.startsWith('"') && line.endsWith('"')
        ? line.slice(1, -1)
        : line;
      if (unquoted.startsWith("file://")) {
        const url = new URL(unquoted);
        const decodedPath = decodeURIComponent(url.pathname);
        if (url.hostname) {
          return `//${url.hostname}${decodedPath}`;
        }
        return /^\/[A-Za-z]:\//.test(decodedPath)
          ? decodedPath.slice(1)
          : decodedPath;
      }
      if (unquoted.startsWith("/") || /^[A-Za-z]:[\\/]/.test(unquoted)) {
        return unquoted;
      }
      throw new Error(`剪贴板内容不是绝对文件路径: ${unquoted}`);
    });
  if (paths.length === 0) {
    throw new Error("剪贴板中没有可粘贴的文件路径");
  }
  return [...new Set(paths)] as [string, ...string[]];
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
  const [directories, setDirectories] = useState<Record<string, DirectoryCacheEntry>>({});
  const [searchQuery, setSearchQuery] = useState("");
  const [settings, setSettings] = useState<SessionFileTreeSettings | null>(null);
  const [contextMenu, setContextMenu] = useState<FileTreeContextMenu | null>(null);
  const [copiedEntry, setCopiedEntry] = useState<WorkspaceClipboardEntry | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const uploadTargetRef = useRef<FileTreeContextMenu | null>(null);
  const lastCollapseVersionRef = useRef(collapseVersion);
  const restoredExpandedPathsRef = useRef(restoredExpandedPaths);
  const directoryRequestsRef = useRef<Map<string, DirectoryRequest>>(new Map());
  const directoriesRef = useRef<Record<string, DirectoryCacheEntry>>({});
  const shortcutTreePathsRef = useRef<Set<string>>(new Set());
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

  const updateDirectories = useCallback((
    updater: (
      current: Record<string, DirectoryCacheEntry>,
    ) => Record<string, DirectoryCacheEntry>,
  ) => {
    setDirectories((current) => {
      const candidate = updater(current);
      const protectedPaths = new Set(expandedPathsRef.current);
      protectedPaths.add(ROOT_PATH);
      protectedPaths.add(FILESYSTEM_ROOT_PATH);
      for (const shortcutPath of shortcutTreePathsRef.current) {
        protectedPaths.add(shortcutPath);
      }
      let activePath = activeFilePathRef.current;
      const visitedActivePaths = new Set<string>();
      while (activePath && !visitedActivePaths.has(activePath)) {
        visitedActivePaths.add(activePath);
        const parentPath = parentFileTreePath(activePath);
        protectedPaths.add(parentPath);
        activePath = parentPath;
      }
      const next = pruneDirectoryCache(candidate, protectedPaths);
      directoriesRef.current = next;
      return next;
    });
  }, []);

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

  const loadDirectory = useCallback(
    (path: string, force = false, append = false): Promise<boolean> => {
      const existingRequest = directoryRequestsRef.current.get(path);
      if (existingRequest && !force) {
        return existingRequest.promise;
      }
      existingRequest?.controller.abort();
      const currentEntry = directoriesRef.current[path];
      const cursor = append ? currentEntry?.nextCursor : null;
      if (append && !cursor) {
        return Promise.resolve(true);
      }
      const controller = new AbortController();
      updateDirectories((prev) => ({
        ...prev,
        [path]: {
          items: prev[path]?.items ?? [],
          loading: true,
          error: null,
          truncated: prev[path]?.truncated ?? false,
          nextCursor: prev[path]?.nextCursor ?? null,
          stale: prev[path]?.stale ?? false,
          lastAccessedAt: Date.now(),
        },
      }));

      const promise = (async (): Promise<boolean> => {
        try {
          const result = await getWorkspaceFiles(
            port,
            path,
            workspaceId,
            controller.signal,
            cursor,
          );
          if (directoryRequestsRef.current.get(path)?.controller !== controller) {
            return false;
          }
          updateDirectories((prev) => {
            const previousItems = append ? prev[path]?.items ?? [] : [];
            const itemsByPath = new Map(
              previousItems.map((item) => [item.path, item]),
            );
            for (const item of result.items ?? []) {
              itemsByPath.set(item.path, item);
            }
            return {
              ...prev,
              [path]: {
                items: [...itemsByPath.values()],
                loading: false,
                error: null,
                truncated: result.truncated ?? false,
                nextCursor: result.next_cursor ?? null,
                stale: false,
                lastAccessedAt: Date.now(),
              },
            };
          });
          return true;
        } catch (error) {
          if (error instanceof Error && error.name === "AbortError") {
            return false;
          }
          if (directoryRequestsRef.current.get(path)?.controller !== controller) {
            return false;
          }
          const message = error instanceof Error ? error.message : String(error);
          updateDirectories((prev) => ({
            ...prev,
            [path]: {
              items: prev[path]?.items ?? [],
              loading: false,
              error: message,
              truncated: prev[path]?.truncated ?? false,
              nextCursor: prev[path]?.nextCursor ?? null,
              stale: prev[path]?.stale ?? false,
              lastAccessedAt: Date.now(),
            },
          }));
          onStatusChange(`文件树加载失败: ${message}`);
          return false;
        } finally {
          if (directoryRequestsRef.current.get(path)?.controller === controller) {
            directoryRequestsRef.current.delete(path);
          }
        }
      })();
      directoryRequestsRef.current.set(path, { controller, promise });
      return promise;
    },
    [onStatusChange, port, updateDirectories, workspaceId],
  );

  const refreshExpandedDirectories = useCallback(() => {
    updateDirectories((current) => Object.fromEntries(
      Object.entries(current).map(([path, entry]) => [path, { ...entry, stale: true }]),
    ));
    for (const path of expandedPathsRef.current) {
      if (directoriesRef.current[path]) {
        void loadDirectory(path, true);
      }
    }
  }, [loadDirectory, updateDirectories]);

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

      updateDirectories((current) => {
        const next = { ...current };
        for (const change of changes) {
          const treePath = changedPathToTreePath(change.path, workspaceRoot);
          if (treePath === null) {
            onStatusChange(`忽略无法定位的文件变更路径: ${change.path}`);
            continue;
          }
          changedParents.add(parentFileTreePath(treePath));
          if (change.kind !== "delete") {
            continue;
          }
          for (const cachedPath of Object.keys(next)) {
            if (isTreePathInside(cachedPath, treePath)) {
              delete next[cachedPath];
            }
          }
          for (const expandedPath of nextExpanded) {
            if (isTreePathInside(expandedPath, treePath)) {
              nextExpanded.delete(expandedPath);
              expandedChanged = true;
            }
          }
        }
        for (const parentPath of changedParents) {
          const entry = next[parentPath];
          if (
            entry
            && (!activeRef.current || !expandedPathsRef.current.has(parentPath))
          ) {
            next[parentPath] = { ...entry, stale: true };
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
    workspaceId,
    workspaceRoot,
  ]);

  const absolutePathForTreePath = useCallback((treePath: string): string => {
    const location = decodeFileTreePath(treePath);
    if (location.scope === "filesystem") {
      return location.path;
    }
    if (!workspaceRoot) {
      throw new Error("当前工作区缺少根目录，无法添加快捷路径");
    }
    if (!location.path) {
      return workspaceRoot;
    }
    const separator = workspaceRoot.includes("\\") ? "\\" : "/";
    return `${workspaceRoot.replace(/[\\/]$/, "")}${separator}${location.path.split("/").join(separator)}`;
  }, [workspaceRoot]);

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
    updateDirectories((current) => current);
    if (!sessionId) {
      return;
    }
    let cancelled = false;
    void getSessionFileTreeSettings(port, sessionId, workspaceId)
      .then((result) => {
        if (!cancelled) {
          acceptFileTreeSettings(result);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const message = error instanceof Error ? error.message : String(error);
          onStatusChange(`快捷路径加载失败: ${message}`);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [
    acceptFileTreeSettings,
    onStatusChange,
    port,
    sessionId,
    updateDirectories,
    workspaceId,
  ]);

  useEffect(() => {
    for (const request of directoryRequestsRef.current.values()) {
      request.controller.abort();
    }
    directoryRequestsRef.current.clear();
    const restoredPaths = new Set(restoredExpandedPathsRef.current);
    commitExpandedPaths(restoredPaths);
    directoriesRef.current = {};
    setDirectories({});
    if (activeRef.current) {
      void restoreDirectoriesInOrder(
        [...restoredPaths],
        (path) => loadDirectory(path),
        parentFileTreePath,
      );
    }
    return () => {
      for (const request of directoryRequestsRef.current.values()) {
        request.controller.abort();
      }
      directoryRequestsRef.current.clear();
    };
  }, [loadDirectory, workspaceId, workspaceRoot]);

  useEffect(() => {
    const resumedAfterPause = !previousActiveRef.current && active;
    previousActiveRef.current = active;
    activeRef.current = active;
    if (!active) {
      const abortedPaths = [...directoryRequestsRef.current.keys()];
      for (const request of directoryRequestsRef.current.values()) {
        request.controller.abort();
      }
      directoryRequestsRef.current.clear();
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
  }, [active, loadDirectory, updateDirectories]);

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

  const openContextMenu = (
    event: ReactMouseEvent,
    treePath: string,
    label: string,
    kind: WorkspaceFileNode["kind"],
    shortcutSource: "session" | "workspace" | null = null,
  ) => {
    event.preventDefault();
    setContextMenu({
      treePath,
      absolutePath: absolutePathForTreePath(treePath),
      label,
      kind,
      shortcutSource,
      x: event.clientX,
      y: event.clientY,
    });
  };

  const replaceDirectory = (result: WorkspaceFileList) => {
    updateDirectories((prev) => ({
      ...prev,
      [result.path]: {
        items: result.items ?? [],
        loading: false,
        error: null,
        truncated: result.truncated ?? false,
        nextCursor: result.next_cursor ?? null,
        stale: false,
        lastAccessedAt: Date.now(),
      },
    }));
    if (!expandedPathsRef.current.has(result.path)) {
      const next = new Set(expandedPathsRef.current);
      next.add(result.path);
      commitExpandedPaths(next);
      scheduleExpandedPathsPersistence([...next].sort());
    }
  };

  const contextTargetDirectory = (target: FileTreeContextMenu): string =>
    target.kind === "directory" ? target.treePath : parentFileTreePath(target.treePath);

  const createEntry = async (
    target: FileTreeContextMenu,
    kind: "file" | "directory",
  ) => {
    const name = window.prompt(kind === "file" ? "新文件名称" : "新文件夹名称");
    if (name === null) {
      return;
    }
    const directoryPath = contextTargetDirectory(target);
    const result = await createWorkspaceFileEntry(
      port,
      directoryPath,
      { name, kind },
      workspaceId,
    );
    replaceDirectory(result);
    onStatusChange(`已创建${kind === "file" ? "文件" : "文件夹"}: ${name}`);
  };

  const pasteEntries = async (
    target: FileTreeContextMenu,
    clipboardText?: string,
  ) => {
    const directoryPath = contextTargetDirectory(target);
    try {
      const sourcePaths = parseClipboardFilePaths(
        clipboardText ?? await readFilePathTextFromClipboard(),
      );
      if (
        copiedEntry
        && sourcePaths.length === 1
        && sourcePaths[0] === copiedEntry.absolutePath
      ) {
        if (copiedEntry.workspaceId !== workspaceId) {
          throw new Error("暂不支持跨工作区粘贴，请在来源工作区下载后再上传");
        }
        const result = await copyWorkspaceFileEntry(
          port,
          directoryPath,
          copiedEntry.location,
          workspaceId,
        );
        replaceDirectory(result);
        onStatusChange(`已粘贴: ${copiedEntry.label}`);
        return;
      }
      const result = await pasteWorkspaceFileEntries(
        port,
        directoryPath,
        { source_paths: sourcePaths },
        workspaceId,
      );
      replaceDirectory(result);
      onStatusChange(`已粘贴 ${sourcePaths.length} 个文件或目录`);
    } catch (error) {
      await loadDirectory(directoryPath, true);
      throw error;
    }
  };

  const uploadEntries = async (
    target: FileTreeContextMenu,
    files: readonly File[],
  ) => {
    const directoryPath = contextTargetDirectory(target);
    try {
      const result = await uploadWorkspaceFileEntries(
        port,
        directoryPath,
        files,
        workspaceId,
      );
      replaceDirectory(result);
      onStatusChange(`已上传 ${files.length} 个本地文件`);
    } catch (error) {
      await loadDirectory(directoryPath, true);
      throw error;
    }
  };

  const copyEntryToClipboard = async (target: FileTreeContextMenu) => {
    const location = decodeFileTreePath(target.treePath);
    setCopiedEntry({
      location,
      absolutePath: target.absolutePath,
      label: target.label,
      workspaceId,
    });
    await copyTextToClipboard(target.absolutePath);
    onStatusChange(`已复制文件: ${target.absolutePath}`);
  };

  useEffect(() => {
    if (!contextMenu) {
      return;
    }
    const handlePaste = (event: ClipboardEvent) => {
      event.preventDefault();
      const target = contextMenu;
      setContextMenu(null);
      const files = filesFromClipboardData(event.clipboardData);
      if (files.length > 0) {
        void uploadEntries(target, files).catch((error: unknown) => {
          const message = error instanceof Error ? error.message : String(error);
          onStatusChange(`上传本地文件失败: ${message}`);
        });
        return;
      }
      let clipboardText: string;
      try {
        clipboardText = readFilePathTextFromClipboardData(event.clipboardData);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        onStatusChange(`粘贴失败: ${message}`);
        return;
      }
      void pasteEntries(target, clipboardText).catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        onStatusChange(`粘贴失败: ${message}`);
      });
    };
    const handleCopyShortcut = (event: KeyboardEvent) => {
      if (
        !(event.ctrlKey || event.metaKey)
        || event.key.toLowerCase() !== "c"
        || contextMenu.treePath === ROOT_PATH
        || contextMenu.treePath === FILESYSTEM_ROOT_PATH
      ) {
        return;
      }
      event.preventDefault();
      const target = contextMenu;
      setContextMenu(null);
      void copyEntryToClipboard(target).catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        onStatusChange(`复制文件失败: ${message}`);
      });
    };
    window.addEventListener("paste", handlePaste);
    window.addEventListener("keydown", handleCopyShortcut);
    return () => {
      window.removeEventListener("paste", handlePaste);
      window.removeEventListener("keydown", handleCopyShortcut);
    };
  }, [contextMenu, copiedEntry]);

  const runContextAction = (
    target: FileTreeContextMenu,
    failurePrefix: string,
    action: () => Promise<unknown>,
  ) => {
    setContextMenu(null);
    setActionError(null);
    void action().catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      const failureMessage = `${failurePrefix}: ${message}`;
      setActionError(failureMessage);
      onStatusChange(failureMessage);
    });
  };

  const addShortcut = async (treePath: string, label: string) => {
    if (!sessionId) {
      throw new Error("添加快捷路径需要当前会话");
    }
    const result = await addSessionFileTreeShortcut(
      port,
      sessionId,
      { path: absolutePathForTreePath(treePath), label },
      workspaceId,
    );
    acceptFileTreeSettings(result);
    onStatusChange(`已添加会话快捷路径: ${label}`);
  };

  const refreshShortcutSettings = async () => {
    if (!sessionId) {
      throw new Error("刷新快捷路径需要当前会话");
    }
    const result = await getSessionFileTreeSettings(port, sessionId, workspaceId);
    acceptFileTreeSettings(result);
  };

  const addAbsoluteShortcutAndDefault = async (path: string, label: string) => {
    if (!sessionId) {
      throw new Error("添加当前会话和新会话默认快捷路径需要当前会话");
    }
    const result = await runCurrentAndDefaultShortcutMutation(
      () => addSessionFileTreeShortcut(
        port,
        sessionId,
        { path, label },
        workspaceId,
      ),
      () => applyFileTreeShortcutToWorkspace(
        port,
        sessionId,
        path,
        label,
        workspaceId,
      ),
      refreshShortcutSettings,
    );
    acceptFileTreeSettings(result);
    onStatusChange(`已将 ${label} 添加到当前会话，并设为新会话默认快捷路径`);
  };

  const addShortcutAndDefault = async (treePath: string, label: string) => {
    await addAbsoluteShortcutAndDefault(
      absolutePathForTreePath(treePath),
      label,
    );
  };

  const removeShortcutAndDefault = async (shortcut: FileTreeShortcut) => {
    if (!sessionId) {
      throw new Error("删除当前会话和新会话默认快捷路径需要当前会话");
    }
    const result = await runCurrentAndDefaultShortcutMutation(
      () => removeSessionFileTreeShortcut(
        port,
        sessionId,
        shortcut.path,
        "session",
        workspaceId,
      ),
      () => removeSessionFileTreeShortcut(
        port,
        sessionId,
        shortcut.path,
        "workspace",
        workspaceId,
      ),
      refreshShortcutSettings,
    );
    acceptFileTreeSettings(result);
    onStatusChange(
      `已从当前会话和新会话默认快捷路径中删除: ${shortcut.path}`,
    );
  };

  const removeShortcut = async (path: string) => {
    if (!sessionId) {
      throw new Error("删除快捷路径需要当前会话");
    }
    const result = await removeSessionFileTreeShortcut(
      port,
      sessionId,
      path,
      "session",
      workspaceId,
    );
    acceptFileTreeSettings(result);
    onStatusChange(`已删除会话快捷路径: ${path}`);
  };

  const handleNodeClick = (node: WorkspaceFileNode) => {
    if (node.kind !== "directory") {
      const size = formatFileSize(node.size);
      onOpenFile(node);
      const absolutePath = absolutePathForTreePath(node.path);
      onStatusChange(size ? `${absolutePath} · ${size}` : absolutePath);
      return;
    }
    toggleDirectory(node.path, absolutePathForTreePath(node.path));
  };

  const nodeMatchesSearch = (node: WorkspaceFileNode): boolean => {
    const normalizedQuery = searchQuery.trim().toLowerCase();
    if (!normalizedQuery) {
      return true;
    }
    const nodeTextMatches = `${node.name}\n${node.path}`.toLowerCase().includes(normalizedQuery);
    if (nodeTextMatches || node.kind !== "directory") {
      return nodeTextMatches;
    }
    const loadedDirectory = directories[node.path];
    if (!loadedDirectory) {
      return true;
    }
    return loadedDirectory.items.some(nodeMatchesSearch);
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

  const renderNode = (node: WorkspaceFileNode, depth: number) => {
    const isDirectory = node.kind === "directory";
    const expanded = expandedPaths.has(node.path);
    return (
      <div className="files-tree-node" key={node.path}>
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
            <span className="files-tree-meta">{formatFileSize(node.size)}</span>
          ) : null}
        </button>
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
        if (`${node.name}\n${node.path}`.toLowerCase().includes(normalizedQuery)) {
          hasDirectMatch = true;
          break;
        }
        if (node.kind === "directory" && !directories[node.path]) {
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
    active && sessionId && (settings === null || directoryLoading),
  );

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
      const { node } = row;
      const isDirectory = node.kind === "directory";
      return (
        <button
          type="button"
          className={`files-tree-item files-tree-row${isDirectory ? " directory" : ""}${activeFilePath === node.path ? " active" : ""}`}
          title={displayPathForTreePath(node.path)}
          style={{ paddingLeft: `${8 + row.depth * 14}px` }}
          onClick={() => handleNodeClick(node)}
          onContextMenu={(event) => openContextMenu(
            event,
            node.path,
            node.name,
            node.kind,
          )}
        >
          <span
            className={`codicon files-tree-chevron${isDirectory ? ` codicon-chevron-${row.expanded ? "down" : "right"}` : ""}`}
            aria-hidden="true"
          />
          <span className={`file-icon ${node.kind}`}>{fileIcon(node)}</span>
          <span className="file-label">{node.name}</span>
          {node.kind === "file" ? (
            <span className="files-tree-meta">{formatFileSize(node.size)}</span>
          ) : null}
        </button>
      );
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
      {actionError ? (
        <div className="files-tree-action-error" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>{actionError}</span>
          <button
            type="button"
            aria-label="关闭文件操作错误"
            onClick={() => setActionError(null)}
          >
            <span className="codicon codicon-close" aria-hidden="true" />
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
      {fileTreeLoading ? (
        <div className="files-tree-loading-status" role="status">
          <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />
          <span>正在加载工作区文件…</span>
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
      <input
        ref={uploadInputRef}
        type="file"
        multiple
        hidden
        aria-label="上传本地文件"
        onChange={(event) => {
          const target = uploadTargetRef.current;
          const files = Array.from(event.currentTarget.files ?? []);
          event.currentTarget.value = "";
          uploadTargetRef.current = null;
          if (!target || files.length === 0) {
            return;
          }
          void uploadEntries(target, files).catch((error: unknown) => {
            const message = error instanceof Error ? error.message : String(error);
            onStatusChange(`上传本地文件失败: ${message}`);
          });
        }}
      />
      {contextMenu ? (
        <AnchoredOverlay
          open
          point={contextMenu}
          placement="bottom-start"
          offset={2}
          onClose={() => setContextMenu(null)}
        >
          <div
            className="agent-sessions-session-menu files-tree-context-menu"
            role="menu"
            onPointerDown={(event) => event.stopPropagation()}
          >
            <button type="button" role="menuitem" onClick={() => {
              const target = contextMenu;
              runContextAction(target, "新建文件失败", () => createEntry(target, "file"));
            }}>
              <span className="codicon codicon-new-file agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">新建文件</span>
            </button>
            <button type="button" role="menuitem" onClick={() => {
              const target = contextMenu;
              runContextAction(target, "新建文件夹失败", () => createEntry(target, "directory"));
            }}>
              <span className="codicon codicon-new-folder agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">新建文件夹</span>
            </button>
            <div className="files-tree-context-separator" role="separator" />
            <button type="button" role="menuitem" onClick={() => {
              const target = contextMenu;
              setContextMenu(null);
              uploadTargetRef.current = target;
              uploadInputRef.current?.click();
            }}>
              <span className="codicon codicon-cloud-upload agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">上传本地文件</span>
            </button>
            {contextMenu.treePath !== ROOT_PATH
              && contextMenu.treePath !== FILESYSTEM_ROOT_PATH ? (
                <button type="button" role="menuitem" onClick={() => {
                  const target = contextMenu;
                  setContextMenu(null);
                  void copyEntryToClipboard(target)
                    .catch((error: unknown) => {
                      const message = error instanceof Error ? error.message : String(error);
                      onStatusChange(`复制文件失败: ${message}`);
                    });
                }}>
                  <span className="codicon codicon-copy agent-sessions-menu-item-icon" aria-hidden="true" />
                  <span className="agent-sessions-menu-item-label">复制</span>
                  <span className="files-tree-context-keybinding">Ctrl+C</span>
                </button>
              ) : null}
            <button type="button" role="menuitem" onClick={() => {
              const target = contextMenu;
              runContextAction(
                target,
                "粘贴失败",
                () => pasteEntries(target, copiedEntry?.absolutePath),
              );
            }}>
              <span className="codicon codicon-clippy agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">粘贴</span>
              <span className="files-tree-context-keybinding">Ctrl+V</span>
            </button>
            <div className="files-tree-context-separator" role="separator" />
            {contextMenu.shortcutSource ? (
              <div className="files-tree-context-shortcut-action">
                <button type="button" role="menuitem" onClick={() => {
                  const target = contextMenu;
                  runContextAction(
                    target,
                    "删除快捷路径失败",
                    () => removeShortcut(target.absolutePath),
                  );
                }}>
                  <span className="codicon codicon-trash agent-sessions-menu-item-icon" aria-hidden="true" />
                  <span className="agent-sessions-menu-item-label">
                    删除当前会话快捷路径
                  </span>
                </button>
                <button
                  type="button"
                  className="files-tree-context-apply"
                  title={defaultShortcutPaths.has(contextMenu.absolutePath)
                    ? "从当前会话和新会话默认快捷路径中删除"
                    : "添加到当前会话，并设为新会话默认快捷路径"}
                  aria-label={defaultShortcutPaths.has(contextMenu.absolutePath)
                    ? `从当前会话和新会话默认快捷路径中删除 ${contextMenu.label}`
                    : `将 ${contextMenu.label} 添加到当前会话并设为新会话默认快捷路径`}
                  onClick={() => {
                    const target = shortcuts.find(
                      (item) => item.path === contextMenu.absolutePath,
                    );
                    const isDefault = defaultShortcutPaths.has(
                      contextMenu.absolutePath,
                    );
                    setContextMenu(null);
                    if (!target) {
                      onStatusChange(`快捷路径已失效: ${contextMenu.absolutePath}`);
                      return;
                    }
                    const action = isDefault
                      ? removeShortcutAndDefault(target)
                      : addAbsoluteShortcutAndDefault(target.path, target.label);
                    void action.catch((error: unknown) => {
                      const message = error instanceof Error ? error.message : String(error);
                      onStatusChange(`更新当前会话和新会话默认快捷路径失败: ${message}`);
                    });
                  }}
                >
                  <span
                    className={`codicon ${defaultShortcutPaths.has(contextMenu.absolutePath)
                      ? "codicon-pinned"
                      : "codicon-pin"}`}
                    aria-hidden="true"
                  />
                </button>
              </div>
            ) : contextMenu.kind === "directory" ? (
              <div className="files-tree-context-shortcut-action">
                <button type="button" role="menuitem" onClick={() => {
                  const target = contextMenu;
                  runContextAction(
                    target,
                    "添加快捷路径失败",
                    () => addShortcut(target.treePath, target.label),
                  );
                }}>
                  <span className="codicon codicon-bookmark agent-sessions-menu-item-icon" aria-hidden="true" />
                  <span className="agent-sessions-menu-item-label">添加到当前会话快捷路径</span>
                </button>
                <button
                  type="button"
                  className="files-tree-context-apply"
                  title="添加到当前会话，并设为新会话默认快捷路径"
                  aria-label={`将 ${contextMenu.label} 添加到当前会话并设为新会话默认快捷路径`}
                  onClick={() => {
                    const target = contextMenu;
                    runContextAction(
                      target,
                      "添加当前会话和新会话默认快捷路径失败",
                      () => addShortcutAndDefault(target.treePath, target.label),
                    );
                  }}
                >
                  <span className="codicon codicon-pin" aria-hidden="true" />
                </button>
              </div>
            ) : null}
            <div className="files-tree-context-separator" role="separator" />
            <button type="button" role="menuitem" onClick={() => {
              const target = contextMenu;
              setContextMenu(null);
              void copyTextToClipboard(target.absolutePath)
                .then(() => onStatusChange(`已复制路径: ${target.absolutePath}`))
                .catch((error: unknown) => {
                  const message = error instanceof Error ? error.message : String(error);
                  onStatusChange(`复制路径失败: ${message}`);
                });
            }}>
              <span className="codicon codicon-copy agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">复制路径</span>
            </button>
            {contextMenu.treePath !== ROOT_PATH
              && contextMenu.treePath !== FILESYSTEM_ROOT_PATH ? (
                <button type="button" role="menuitem" onClick={() => {
                  const target = contextMenu;
                  runContextAction(
                    target,
                    "下载失败",
                    async () => {
                      const request = await createWorkspaceFileDownloadRequest(
                        port,
                        target.treePath,
                        target.kind === "directory"
                          ? `${target.label}.zip`
                          : target.label,
                        workspaceId,
                      );
                      await getFileTransferHost().downloadWorkspaceFile(request);
                      onStatusChange(`已开始下载: ${target.label}`);
                    },
                  );
                }}>
                  <span className="codicon codicon-cloud-download agent-sessions-menu-item-icon" aria-hidden="true" />
                  <span className="agent-sessions-menu-item-label">下载</span>
                </button>
              ) : null}
            <button type="button" role="menuitem" onClick={() => {
              const target = contextMenu;
              runContextAction(
                target,
                "在系统中显示失败",
                async () => {
                  const result = await revealWorkspaceFileEntry(
                    port,
                    target.treePath,
                    workspaceId,
                  );
                  onStatusChange(`已请求系统显示: ${result.path}`);
                },
              );
            }}>
              <span className="codicon codicon-folder-opened agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">在系统中显示</span>
            </button>
            <button type="button" role="menuitem" onClick={() => {
              const target = contextMenu;
              const directoryPath = contextTargetDirectory(target);
              runContextAction(
                target,
                "刷新目录失败",
                async () => {
                  await loadDirectory(directoryPath, true);
                  onStatusChange(`已刷新目录: ${absolutePathForTreePath(directoryPath)}`);
                },
              );
            }}>
              <span className="codicon codicon-refresh agent-sessions-menu-item-icon" aria-hidden="true" />
              <span className="agent-sessions-menu-item-label">刷新根目录文件树</span>
            </button>
          </div>
        </AnchoredOverlay>
      ) : null}
    </div>
  );
}
