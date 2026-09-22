import { useCallback } from "react";
import { DEFAULT_BACKEND_PORT } from "../../api";
import type {
  FinishWorkspaceRefresh,
  SetAppState,
} from "../contentViewLoaderTypes";
import { refreshWorkspaceSessionList } from "../sessionEventStream/sessionRefresh";

/** 工作区刷新编排链路：工作区级刷新的三个原语——切换前置重置、刷新收口、强制
 * 刷新工作区会话列表——全部归属工作区层级，不再由 AppProvider 直接持有。
 *
 * finishWorkspaceRefresh 的返回值语义与 contentViewLoaderTypes.ts 中的
 * FinishWorkspaceRefresh 类型一致：null 表示本轮刷新被更新的请求作废，调用方
 * 不能当布尔使用。 */
export function useWorkspaceRefreshOrchestration({
  apiPort,
  setState,
  refreshSessions,
  abortCurrentStream,
}: {
  apiPort: number | null;
  setState: SetAppState;
  refreshSessions: FinishWorkspaceRefresh;
  abortCurrentStream: () => void;
}) {
  const resetWorkspaceScopedState = useCallback(() => {
    abortCurrentStream();
    setState((prev) => ({
      ...prev,
      workspaceSwitching: true,
      error: null,
      status: "正在切换工作区",
    }));
  }, [abortCurrentStream, setState]);

  const finishWorkspaceRefresh = useCallback<FinishWorkspaceRefresh>(async (
    preferredSessionId,
    options = {},
  ) => {
    // 返回本轮刷新真正生效的活动工作区 id：刷新被作废时 refreshSessions 返回
    // null。调用方必须比对它是否等于自己请求的 workspaceId，不能只看真值——
    // 自动健康回退可能把活动工作区切到别的 id，那不算请求的那个工作区生效。
    const appliedWorkspaceId = await refreshSessions(preferredSessionId, options);
    if (appliedWorkspaceId === null) {
      return null;
    }
    setState((prev) => ({
      ...prev,
      workspaceSwitching: false,
      error: null,
      status: "工作区已就绪",
    }));
    return appliedWorkspaceId;
  }, [refreshSessions, setState]);

  const refreshGatewayWorkspaceSessions = useCallback(async (workspaceId: string) => {
    await refreshWorkspaceSessionList(
      apiPort ?? DEFAULT_BACKEND_PORT,
      workspaceId,
      setState,
      { force: true },
    );
  }, [apiPort, setState]);

  return {
    finishWorkspaceRefresh,
    refreshGatewayWorkspaceSessions,
    resetWorkspaceScopedState,
  };
}
