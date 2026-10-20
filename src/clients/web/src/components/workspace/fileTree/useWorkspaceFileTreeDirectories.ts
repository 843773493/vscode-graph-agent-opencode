import { useCallback, useRef, useState, type MutableRefObject } from "react";
import { getWorkspaceFiles } from "../../../api";
import { HttpRequestError } from "../../../api/http";
import type { WorkspaceDirectoryKey, WorkspaceDirectoryRevision, WorkspaceFileList } from "../../../types/backend";
import { errorDisplayMessage } from "../../../utils/errorMessage";
import {
  failedDirectoryEntry,
  loadedDirectoryEntry,
  loadingDirectoryEntry,
  pruneDirectoryCache,
  restoreDirectoriesInOrder,
  type DirectoryCacheEntry,
} from "./workspaceFileTreeCache";
import {
  isTreePathInside,
  parentFileTreePath,
} from "./workspaceFileTreePaths";

interface DirectoryRequest {
  controller: AbortController;
  promise: Promise<boolean>;
  minimumRevision: number;
}

interface UseWorkspaceFileTreeDirectoriesOptions {
  port: number;
  workspaceId: string | null;
  expandedPathsRef: MutableRefObject<Set<string>>;
  shortcutTreePathsRef: MutableRefObject<Set<string>>;
  activeFilePathRef: MutableRefObject<string | null>;
  onStatusChange: (text: string) => void;
}

interface WorkspaceFileTreeDirectories {
  directories: Record<string, DirectoryCacheEntry>;
  directoriesRef: MutableRefObject<Record<string, DirectoryCacheEntry>>;
  updateDirectories: (
    updater: (
      current: Record<string, DirectoryCacheEntry>,
    ) => Record<string, DirectoryCacheEntry>,
  ) => void;
  loadDirectory: (path: string, force?: boolean, append?: boolean) => Promise<boolean>;
  reloadExpandedDirectories: (roots: readonly string[]) => void;
  reconcileDirectories: (directories: readonly WorkspaceDirectoryRevision[], forcePath?: string) => Promise<void>;
  acceptSnapshots: (snapshots: readonly WorkspaceFileList[], forcePath?: string) => void;
  directoryVersion: (path: string) => { key: WorkspaceDirectoryKey; revision: number } | null;
  invalidateDirectoriesUnder: (treePath: string) => void;
  abortAllDirectoryRequests: () => string[];
  resetDirectories: () => void;
}

/**
 * 目录缓存的唯一所有者：负责按路径懒加载目录、合并分页结果、在写入时按
 * 展开态/快捷路径/活动文件计算保护集做 LRU 淘汰，并把在途请求去重。
 * 展开态与活动文件夹在外部，通过 ref 传入以保证淘汰保护集读取到最新值。
 */
export function useWorkspaceFileTreeDirectories({
  port,
  workspaceId,
  expandedPathsRef,
  shortcutTreePathsRef,
  activeFilePathRef,
  onStatusChange,
}: UseWorkspaceFileTreeDirectoriesOptions): WorkspaceFileTreeDirectories {
  const [directories, setDirectories] = useState<Record<string, DirectoryCacheEntry>>({});
  const directoriesRef = useRef<Record<string, DirectoryCacheEntry>>({});
  const directoryRequestsRef = useRef<Map<string, DirectoryRequest>>(new Map());
  const minimumRevisionsRef = useRef<Map<string, number>>(new Map());

  const updateDirectories = useCallback((
    updater: (
      current: Record<string, DirectoryCacheEntry>,
    ) => Record<string, DirectoryCacheEntry>,
  ) => {
    {
      const current = directoriesRef.current;
      const candidate = updater(current);
      const protectedPaths = new Set(expandedPathsRef.current);
      for (const [path, entry] of Object.entries(candidate)) {
        if (entry.directoryKey?.path === entry.directoryKey?.root) protectedPaths.add(path);
      }
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
      for (const path of minimumRevisionsRef.current.keys()) {
        if (!next[path]) minimumRevisionsRef.current.delete(path);
      }
      setDirectories(next);
    }
  }, [activeFilePathRef, expandedPathsRef, shortcutTreePathsRef]);

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
        [path]: loadingDirectoryEntry(prev[path], Date.now()),
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
          const previous = directoriesRef.current[path];
          if ((result.directory_revision < (minimumRevisionsRef.current.get(path) ?? 0))
            || (sameDirectoryKey(previous?.directoryKey, result.directory_key)
              && result.directory_revision < (previous?.directoryRevision ?? 0))) {
            updateDirectories((prev) => ({ ...prev, [path]: { ...prev[path], loading: false } }));
            return false;
          }
          if (append && (!sameDirectoryKey(previous?.directoryKey, result.directory_key)
            || previous?.directoryRevision !== result.directory_revision)) {
            return await loadDirectory(path, true);
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
              [path]: loadedDirectoryEntry(result, Date.now(), [...itemsByPath.values()]),
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
          if (append && error instanceof HttpRequestError && error.status === 409) {
            return await loadDirectory(path, true);
          }
          const message = errorDisplayMessage(error);
          updateDirectories((prev) => ({
            ...prev,
            [path]: failedDirectoryEntry(prev[path], message, Date.now()),
          }));
          onStatusChange(`文件树加载失败: ${message}`);
          return false;
        } finally {
          if (directoryRequestsRef.current.get(path)?.controller === controller) {
            directoryRequestsRef.current.delete(path);
          }
        }
      })();
      directoryRequestsRef.current.set(path, { controller, promise, minimumRevision: minimumRevisionsRef.current.get(path) ?? 0 });
      return promise;
    },
    [onStatusChange, port, updateDirectories, workspaceId],
  );

  // 子树失效的唯一入口：丢弃该路径及其后代的缓存，并中止对应在途请求，
  // 使迟到的响应因请求身份不匹配而被丢弃，不能回填已被失效的目录。
  const invalidateDirectoriesUnder = useCallback((treePath: string) => {
    for (const [path, request] of directoryRequestsRef.current) {
      if (isTreePathInside(path, treePath)) {
        request.controller.abort();
        directoryRequestsRef.current.delete(path);
      }
    }
    updateDirectories((current) => {
      const next = { ...current };
      for (const cachedPath of Object.keys(next)) {
        if (isTreePathInside(cachedPath, treePath)) {
          delete next[cachedPath];
        }
      }
      return next;
    });
  }, [updateDirectories]);

  // overflow 只使后端指定根下的已观察目录失效，cold 根不触发读取。
  const reloadExpandedDirectories = useCallback((roots: readonly string[]) => {
    const affected = Object.entries(directoriesRef.current).filter(([, entry]) => {
      const key = entry.directoryKey;
      if (!key) return false;
      const absolute = key.path;
      return roots.some((root) => {
        const normalized = root.replace(/\\/g, "/").replace(/\/$/, "");
        return absolute === normalized || absolute.startsWith(`${normalized}/`);
      });
    }).map(([path]) => path);
    const cachedExpanded = affected.filter((path) => expandedPathsRef.current.has(path));
    for (const path of affected) invalidateDirectoriesUnder(path);
    void restoreDirectoriesInOrder(
      cachedExpanded,
      (path) => loadDirectory(path),
      parentFileTreePath,
    );
  }, [expandedPathsRef, directoriesRef, invalidateDirectoriesUnder, loadDirectory]);

  const reconcileDirectories = useCallback(async (revisions: readonly WorkspaceDirectoryRevision[], forcePath?: string) => {
    const reload = new Set<string>();
    for (const revision of revisions) {
      const key = revision.directory_key;
      const path = key.path;
      const cached = directoriesRef.current[path];
      if (!cached && path !== forcePath) continue;
      if (cached?.directoryKey && !sameDirectoryKey(cached.directoryKey, key)) continue;
      if (!revision.exists) { invalidateDirectoriesUnder(path); continue; }
      const minimum = Math.max(minimumRevisionsRef.current.get(path) ?? 0, revision.directory_revision);
      minimumRevisionsRef.current.set(path, minimum);
      const request = directoryRequestsRef.current.get(path);
      if ((cached?.directoryRevision ?? 0) < minimum && (!request || request.minimumRevision < minimum)) reload.add(path);
    }
    if (forcePath !== undefined && (revisions.length === 0 || !directoriesRef.current[forcePath])) reload.add(forcePath);
    await Promise.all([...reload].map((path) => loadDirectory(path, true)));
  }, [invalidateDirectoriesUnder, loadDirectory]);

  const acceptSnapshots = useCallback((snapshots: readonly WorkspaceFileList[], forcePath?: string) => {
    for (const snapshot of snapshots) {
      const key = snapshot.directory_key;
      const path = key.path;
      const cached = directoriesRef.current[path];
      if (!cached && path !== forcePath) continue;
      if (cached?.directoryKey && !sameDirectoryKey(cached.directoryKey, key)) continue;
      if (snapshot.directory_revision < Math.max(cached?.directoryRevision ?? 0, minimumRevisionsRef.current.get(path) ?? 0)) continue;
      const request = directoryRequestsRef.current.get(path);
      request?.controller.abort();
      directoryRequestsRef.current.delete(path);
      minimumRevisionsRef.current.set(path, snapshot.directory_revision);
      const result = snapshot;
      updateDirectories((current) => ({ ...current, [path]: loadedDirectoryEntry(result, Date.now()) }));
    }
  }, [updateDirectories]);

  const directoryVersion = useCallback((path: string) => {
    const cached = directoriesRef.current[path];
    return cached?.directoryKey && cached.directoryRevision !== null
      ? { key: cached.directoryKey, revision: cached.directoryRevision } : null;
  }, []);

  const abortAllDirectoryRequests = useCallback((): string[] => {
    const abortedPaths = [...directoryRequestsRef.current.keys()];
    for (const request of directoryRequestsRef.current.values()) {
      request.controller.abort();
    }
    directoryRequestsRef.current.clear();
    return abortedPaths;
  }, []);

  const resetDirectories = useCallback(() => {
    minimumRevisionsRef.current.clear();
    directoriesRef.current = {};
    setDirectories({});
  }, []);

  return {
    directories,
    directoriesRef,
    updateDirectories,
    loadDirectory,
    reloadExpandedDirectories,
    reconcileDirectories,
    acceptSnapshots,
    directoryVersion,
    invalidateDirectoriesUnder,
    abortAllDirectoryRequests,
    resetDirectories,
  };
}

function sameDirectoryKey(left: WorkspaceDirectoryKey | null | undefined, right: WorkspaceDirectoryKey): boolean {
  return !!left && left.workspace_id === right.workspace_id && left.scope === right.scope
    && left.root === right.root && left.path === right.path;
}
