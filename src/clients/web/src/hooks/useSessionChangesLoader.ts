import { useCallback, useRef } from "react";
import {
  getSessionChangeset,
  getSessionChangesets,
  reviewSessionChangeFile as apiReviewSessionChangeFile,
} from "../api";
import type {
  Session,
  SessionChangesetList,
  SessionFileChange,
} from "../types/backend";
import type { SetAppState } from "./contentViewLoaderTypes";

export type SessionChangesRefreshOptions = {
  refreshList?: boolean;
};

const SESSION_CHANGESET_CACHE_LIMIT = 16;

function writeChangesetCache(
  cache: Map<string, SessionChangesetList>,
  key: string,
  value: SessionChangesetList,
): void {
  cache.delete(key);
  cache.set(key, value);
  while (cache.size > SESSION_CHANGESET_CACHE_LIMIT) {
    const oldestKey = cache.keys().next().value;
    if (typeof oldestKey !== "string") break;
    cache.delete(oldestKey);
  }
}

export function useSessionChangesLoader({
  apiPort,
  currentSession,
  workspaceId,
  setState,
}: {
  apiPort: number;
  currentSession: Session | null;
  workspaceId: string | null;
  setState: SetAppState;
}) {
  const requestIdRef = useRef(0);
  const changesetsCacheRef = useRef(new Map<string, SessionChangesetList>());
  const changesetsRequestRef = useRef(new Map<string, Promise<SessionChangesetList>>());

  const invalidateSessionChanges = useCallback(() => {
    requestIdRef.current += 1;
  }, []);

  const loadSessionChangesets = useCallback(
    async (
      sessionId: string,
      force: boolean = false,
    ): Promise<SessionChangesetList> => {
      const key = `${workspaceId ?? ""}:${sessionId}`;
      const inFlight = changesetsRequestRef.current.get(key);
      if (inFlight) {
        return await inFlight;
      }
      const cached = changesetsCacheRef.current.get(key);
      if (!force && cached) {
        changesetsCacheRef.current.delete(key);
        changesetsCacheRef.current.set(key, cached);
        return cached;
      }
      const promise = getSessionChangesets(apiPort, sessionId, workspaceId).then(
        (value) => {
          writeChangesetCache(changesetsCacheRef.current, key, value);
          return value;
        },
      );
      changesetsRequestRef.current.set(key, promise);
      void promise.then(() => {
        if (changesetsRequestRef.current.get(key) === promise) {
          changesetsRequestRef.current.delete(key);
        }
      }, () => {
        if (changesetsRequestRef.current.get(key) === promise) {
          changesetsRequestRef.current.delete(key);
        }
      });
      return await promise;
    },
    [apiPort, workspaceId],
  );

  const refreshSessionChanges = useCallback(
    async (
      sessionId: string,
      changesetId?: string | null,
      options: SessionChangesRefreshOptions = {},
    ) => {
      const requestId = requestIdRef.current + 1;
      requestIdRef.current = requestId;
      setState((prev) => ({
        ...prev,
        contentView: "changes",
        sessionChangesLoading: true,
        sessionChangesError: null,
        status: "正在读取文件变更",
      }));

      try {
        const list = await loadSessionChangesets(
          sessionId,
          options.refreshList === true,
        );
        const selectedId =
          changesetId ||
          list.items.find((item) => item.is_default)?.changeset_id ||
          list.items[0]?.changeset_id ||
          "all";
        const changeset = await getSessionChangeset(
          apiPort,
          sessionId,
          selectedId,
          workspaceId,
        );
        setState((prev) => {
          if (
            requestId !== requestIdRef.current ||
            prev.currentSession?.session_id !== sessionId ||
            prev.contentView !== "changes"
          ) {
            return prev;
          }
          return {
            ...prev,
            sessionChangesets: list.items,
            selectedChangesetId: selectedId,
            activeChangeset: changeset,
            sessionChangesLoadedAt: new Date().toISOString(),
            sessionChangesLoading: false,
            sessionChangesError: null,
            status: `文件变更已加载 (${changeset.summary.files} 个文件)`,
          };
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => {
          if (
            requestId !== requestIdRef.current ||
            prev.currentSession?.session_id !== sessionId ||
            prev.contentView !== "changes"
          ) {
            return prev;
          }
          return {
            ...prev,
            sessionChangesLoading: false,
            sessionChangesError: message,
            status: `文件变更加载失败: ${message}`,
          };
        });
      }
    },
    [loadSessionChangesets, setState],
  );

  const reviewSessionChangeFile = useCallback(
    async (file: SessionFileChange, reviewed: boolean) => {
      if (!currentSession) {
        throw new Error("当前没有可审查文件变更的会话");
      }
      const sessionId = currentSession.session_id;
      const changesetId = "all";
      setState((prev) => ({
        ...prev,
        status: reviewed ? "正在标记文件已审查" : "正在取消文件已审查",
      }));
      const result = await apiReviewSessionChangeFile(
        apiPort,
        sessionId,
        changesetId,
        file.file_path,
        reviewed,
        workspaceId,
      );
      setState((prev) => {
        if (prev.currentSession?.session_id !== sessionId) {
          return prev;
        }
        const nextActiveChangeset = prev.activeChangeset
          ? {
              ...prev.activeChangeset,
              files: prev.activeChangeset.files.map((item) =>
                item.file_path === result.file_path
                  ? { ...item, reviewed: result.reviewed }
                  : item,
              ),
            }
          : prev.activeChangeset;
        return {
          ...prev,
          activeChangeset: nextActiveChangeset,
          status: reviewed
            ? `已标记为已审查: ${result.file_path}`
            : `已取消已审查: ${result.file_path}`,
        };
      });
    },
    [apiPort, currentSession, workspaceId, setState],
  );

  return {
    invalidateSessionChanges,
    loadSessionChangesets,
    refreshSessionChanges,
    reviewSessionChangeFile,
  };
}
