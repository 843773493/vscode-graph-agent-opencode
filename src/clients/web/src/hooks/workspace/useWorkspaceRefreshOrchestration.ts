import { useCallback, useRef } from "react";
import { DEFAULT_BACKEND_PORT } from "../../api";
import type {
  FinishWorkspaceRefresh,
  SetAppState,
} from "../contentViewLoaderTypes";
import { refreshWorkspaceSessionList } from "../sessionEventStream/sessionRefresh";
import { errorMessage } from "../../utils/errorMessage";

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
  // 切换态的进入与退出必须是同一条链路的成对操作：每个「置为切换中」的入口
  // （resetWorkspaceScopedState）与每个刷新收口（finishWorkspaceRefresh）都会
  // 推进这个步号，收口只在仍是最后一步时才写回状态，避免被顶替的旧链路覆盖
  // 新链路正在收敛的切换态。任何一次收口无论成功、作废还是抛错都会退出切换态，
  // 调用方无需记得处理 null 返回值。
  const latestSwitchStepRef = useRef(0);

  const settleWorkspaceSwitching = useCallback((failureMessage: string | null) => {
    setState((prev) => ({
      ...prev,
      workspaceSwitching: false,
      ...(failureMessage === null
        ? { error: null, status: "工作区已就绪" }
        : { gatewayError: failureMessage, error: failureMessage, status: failureMessage }),
    }));
  }, [setState]);

  const resetWorkspaceScopedState = useCallback(() => {
    abortCurrentStream();
    latestSwitchStepRef.current += 1;
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
    const switchStep = ++latestSwitchStepRef.current;
    try {
      const appliedWorkspaceId = await refreshSessions(preferredSessionId, options);
      if (switchStep !== latestSwitchStepRef.current) {
        // 已有更新的切换步骤接手收敛职责：本次不得写回，避免把新链路正在
        // 收敛的切换态反向清掉，但仍要把真实生效的工作区 id 回传给调用方。
        return appliedWorkspaceId;
      }
      settleWorkspaceSwitching(
        appliedWorkspaceId === null
          ? "工作区切换未生效：本轮刷新已被更新的请求作废"
          : null,
      );
      return appliedWorkspaceId;
    } catch (error: unknown) {
      if (switchStep === latestSwitchStepRef.current) {
        settleWorkspaceSwitching(errorMessage(error));
      }
      throw error;
    }
  }, [refreshSessions, settleWorkspaceSwitching]);

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
