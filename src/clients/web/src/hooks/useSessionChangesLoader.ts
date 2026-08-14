import { useCallback, useRef } from "react";
import {
  getSessionChangeset,
  getSessionChangesets,
  reviewSessionChangeFile as apiReviewSessionChangeFile,
} from "../api";
import type { Session, SessionFileChange } from "../types/backend";
import type { SetAppState } from "./contentViewLoaderTypes";

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

  const invalidateSessionChanges = useCallback(() => {
    requestIdRef.current += 1;
  }, []);

  const refreshSessionChanges = useCallback(
    async (sessionId: string, changesetId?: string | null) => {
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
        const list = await getSessionChangesets(apiPort, sessionId, workspaceId);
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
    [apiPort, workspaceId, setState],
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
    refreshSessionChanges,
    reviewSessionChangeFile,
  };
}
