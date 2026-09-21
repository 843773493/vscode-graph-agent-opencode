import { useCallback, useRef, type MutableRefObject } from "react";
import { DEFAULT_BACKEND_PORT, getSession as apiGetSession } from "../api";
import type { Session } from "../types/backend";
import type { AppState } from "../types/frontend";
import { createLatestSerialTaskQueue } from "./serialTaskQueue";
import type { SessionViewStateController } from "./useSessionViewState";

/** 工作区会话选择链路：先切换会话再加载视图状态；openWorkspaceSession 用
 * latest-only 队列加 intent 守卫保证连续打开同一会话只有最后一次生效。 */
export function useWorkspaceSessionSelection({
  apiPort,
  latestStateRef,
  selectSession,
  selectWorkspaceSession,
  loadSessionViewState,
  activateGatewayWorkspaceInBackground,
}: {
  apiPort: number | null;
  latestStateRef: MutableRefObject<AppState>;
  selectSession: (sessionId: string) => void;
  selectWorkspaceSession: (
    workspaceId: string,
    sessionId: string,
    sessionOverride?: Session,
  ) => void;
  loadSessionViewState: SessionViewStateController["loadSessionViewState"];
  activateGatewayWorkspaceInBackground: (workspaceId: string) => void;
}) {
  const selectionQueueRef = useRef(createLatestSerialTaskQueue());
  const selectionIntentRef = useRef(0);

  const selectSessionWithViewState = useCallback((sessionId: string) => {
    selectSession(sessionId);
    const latest = latestStateRef.current;
    void loadSessionViewState(
      latest.currentSessionWorkspaceId ?? latest.activeGatewayWorkspaceId,
      sessionId,
    );
  }, [loadSessionViewState, selectSession]);

  const selectWorkspaceSessionWithViewState = useCallback((
    workspaceId: string,
    sessionId: string,
    sessionOverride?: Session,
  ) => {
    selectWorkspaceSession(workspaceId, sessionId, sessionOverride);
    void loadSessionViewState(workspaceId, sessionId);
  }, [loadSessionViewState, selectWorkspaceSession]);

  const openWorkspaceSession = useCallback((workspaceId: string, sessionId: string) => {
    const intent = ++selectionIntentRef.current;
    return selectionQueueRef.current.enqueue(async () => {
      // 被顶替的任务由 latest-only 队列在回调入口直接短路，无需在回调内重复判定意图。
      const latestState = latestStateRef.current;
      const cachedSession = latestState.sessionsByWorkspace
        .get(workspaceId)
        ?.find((session) => session.session_id === sessionId);
      const shouldActivateWorkspace =
        workspaceId !== latestState.activeGatewayWorkspaceId
        || latestState.workspaceSwitching;
      const selectedSession = cachedSession
        ?? await apiGetSession(apiPort ?? DEFAULT_BACKEND_PORT, sessionId, workspaceId);
      if (intent !== selectionIntentRef.current) {
        return;
      }
      selectWorkspaceSessionWithViewState(workspaceId, sessionId, selectedSession);
      if (shouldActivateWorkspace) {
        activateGatewayWorkspaceInBackground(workspaceId);
      }
    });
  }, [activateGatewayWorkspaceInBackground, selectWorkspaceSessionWithViewState, apiPort]);

  return {
    selectSession: selectSessionWithViewState,
    selectWorkspaceSession: selectWorkspaceSessionWithViewState,
    openWorkspaceSession,
  };
}
