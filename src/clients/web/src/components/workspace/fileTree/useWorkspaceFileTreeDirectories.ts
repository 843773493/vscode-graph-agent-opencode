import { useCallback, useRef, useState, type MutableRefObject } from "react";
import { getWorkspaceFiles } from "../../../api";
import { errorDisplayMessage } from "../../../utils/errorMessage";
import {
  failedDirectoryEntry,
  loadedDirectoryEntry,
  loadingDirectoryEntry,
  markDirectoryStale,
  pruneDirectoryCache,
  type DirectoryCacheEntry,
} from "./workspaceFileTreeCache";
import { FILESYSTEM_ROOT_PATH, parentFileTreePath, ROOT_PATH } from "./workspaceFileTreePaths";

interface DirectoryRequest {
  controller: AbortController;
  promise: Promise<boolean>;
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
  refreshExpandedDirectories: () => void;
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
      directoryRequestsRef.current.set(path, { controller, promise });
      return promise;
    },
    [onStatusChange, port, updateDirectories, workspaceId],
  );

  const refreshExpandedDirectories = useCallback(() => {
    updateDirectories((current) => Object.fromEntries(
      Object.entries(current).map(([path, entry]) => [path, markDirectoryStale(entry)]),
    ));
    for (const path of expandedPathsRef.current) {
      if (directoriesRef.current[path]) {
        void loadDirectory(path, true);
      }
    }
  }, [expandedPathsRef, loadDirectory, updateDirectories]);

  const abortAllDirectoryRequests = useCallback((): string[] => {
    const abortedPaths = [...directoryRequestsRef.current.keys()];
    for (const request of directoryRequestsRef.current.values()) {
      request.controller.abort();
    }
    directoryRequestsRef.current.clear();
    return abortedPaths;
  }, []);

  const resetDirectories = useCallback(() => {
    directoriesRef.current = {};
    setDirectories({});
  }, []);

  return {
    directories,
    directoriesRef,
    updateDirectories,
    loadDirectory,
    refreshExpandedDirectories,
    abortAllDirectoryRequests,
    resetDirectories,
  };
}
