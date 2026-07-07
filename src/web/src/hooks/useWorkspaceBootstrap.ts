import { useCallback, useEffect } from "react";
import {
  DEFAULT_BACKEND_PORT,
  getWorkspace,
  listAgents as apiListAgents,
  listSessions,
} from "../api";
import { readLastSessionId } from "../state/storage";
import type { SetAppState } from "./contentViewLoaderTypes";

export function useWorkspaceBootstrap({
  apiPort,
  setState,
}: {
  apiPort: number | null;
  setState: SetAppState;
}) {
  const refreshSessions = useCallback(async () => {
    try {
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      const [workspace, sessions, agents] = await Promise.all([
        getWorkspace(resolvedApiPort),
        listSessions(resolvedApiPort),
        apiListAgents(resolvedApiPort),
      ]);
      setState((prev) => {
        const preferredSessionId =
          prev.currentSession?.session_id ?? readLastSessionId();
        const nextCurrentSession =
          sessions.items.find(
            (session) => session.session_id === preferredSessionId,
          ) ??
          sessions.items[0] ??
          null;
        return {
          ...prev,
          workspaceRoot: workspace.root_path,
          workspaceName: workspace.name,
          agents,
          sessions: sessions.items,
          currentSession: nextCurrentSession,
          error: null,
          isBootstrapping: false,
        };
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((prev) => ({
        ...prev,
        error: message,
        status: "初始化失败",
        isBootstrapping: false,
      }));
    }
  }, [apiPort, setState]);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);
}
