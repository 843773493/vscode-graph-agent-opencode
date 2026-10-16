import { useCallback, useRef, type MutableRefObject } from "react";
import { DEFAULT_BACKEND_PORT, getSession as apiGetSession } from "../../api";
import type { Session } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { errorMessage } from "../../utils/errorMessage";
import { createLatestSerialTaskQueue } from "../runtime/serialTaskQueue";
import type { SessionViewStateController } from "../session/useSessionViewState";

/** 工作区会话选择链路：先切换会话再加载视图状态；openWorkspaceSession 用
 * latest-only 队列加 intent 守卫保证连续打开同一会话只有最后一次生效。 */
export function useWorkspaceSessionSelection({
  apiPort,
  latestStateRef,
  selectSession,
  selectWorkspaceSession,
  loadSessionViewState,
  activateGatewayWorkspaceInBackground,
  setStatus,
  selectionIntentRef,
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
  setStatus: (message: string) => void;
  selectionIntentRef: MutableRefObject<number>;
}) {
  const selectionQueueRef = useRef(createLatestSerialTaskQueue());

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

  const openWorkspaceSession = useCallback((workspaceId: string, sessionId: string, expectedSelectionIntent?: number) => {
    const intent = expectedSelectionIntent ?? ++selectionIntentRef.current;
    if (intent !== selectionIntentRef.current) return Promise.resolve();
    return selectionQueueRef.current.enqueue(async () => {
      if (intent !== selectionIntentRef.current) return;
      const latestState = latestStateRef.current;
      const cachedSession = latestState.sessionsByWorkspace
        .get(workspaceId)
        ?.find((session) => session.session_id === sessionId);
      const shouldActivateWorkspace =
        workspaceId !== latestState.activeGatewayWorkspaceId
        || latestState.workspaceSwitching;
      let selectedSession: Session;
      if (cachedSession) {
        selectedSession = cachedSession;
      } else {
        try {
          selectedSession = await apiGetSession(
            apiPort ?? DEFAULT_BACKEND_PORT,
            sessionId,
            workspaceId,
          );
        } catch (error: unknown) {
          // 缓存未命中且后端不可达时，以前的分支会让这次点击静默失败。会话树的
          // 点击回调签名是 (sessionId) => void，没有任何调用方能接住这个 rejection，
          // 继续抛出只会变成未处理拒绝。这里把失败写成用户可见状态后正常返回，
          // 让「点击已被处理，只是失败了」这件事既可见又不残留未处理拒绝。
          if (intent === selectionIntentRef.current) {
            setStatus(`打开会话失败: ${errorMessage(error)}`);
          }
          return;
        }
      }
      if (intent !== selectionIntentRef.current) {
        return;
      }
      selectWorkspaceSessionWithViewState(workspaceId, sessionId, selectedSession);
      if (shouldActivateWorkspace) {
        activateGatewayWorkspaceInBackground(workspaceId);
      }
    });
  }, [
    activateGatewayWorkspaceInBackground,
    selectWorkspaceSessionWithViewState,
    apiPort,
    setStatus,
  ]);

  return {
    selectSession: selectSessionWithViewState,
    selectWorkspaceSession: selectWorkspaceSessionWithViewState,
    openWorkspaceSession,
  };
}
