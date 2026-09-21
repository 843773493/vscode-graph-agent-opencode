import { useCallback, useRef, type MutableRefObject } from "react";
import { DEFAULT_BACKEND_PORT, listAgents as apiListAgents } from "../api";
import {
  activateGatewayWorkspace as apiActivateGatewayWorkspace,
} from "../gatewayApi";
import type { AppState } from "../types/frontend";
import type { FinishWorkspaceRefresh, SetAppState } from "./contentViewLoaderTypes";
import { createLatestSerialTaskQueue } from "./serialTaskQueue";

type RefreshGatewayWorkspaceStatuses = (
  expectedWorkspaceId?: string | null,
) => Promise<void>;

export function useGatewayWorkspaceActivation({
  apiPort,
  currentSessionId,
  latestStateRef,
  setState,
  invalidateWorkspaceRefreshes,
  refreshGatewayWorkspaceStatuses,
  resetWorkspaceScopedState,
  finishWorkspaceRefresh,
}: {
  apiPort: number | null;
  currentSessionId: string | null;
  latestStateRef: MutableRefObject<AppState>;
  setState: SetAppState;
  invalidateWorkspaceRefreshes: () => void;
  refreshGatewayWorkspaceStatuses: RefreshGatewayWorkspaceStatuses;
  resetWorkspaceScopedState: () => void;
  finishWorkspaceRefresh: FinishWorkspaceRefresh;
}) {
  const workspaceActivationQueueRef = useRef(createLatestSerialTaskQueue());
  const backgroundWorkspaceActivationSequenceRef = useRef(0);
  // 最近一次正式激活的激活意图序列号：正式激活用它判断自己的收敛职责是否已被
  // 更新的正式激活接手。后台激活不更新这个 ref，因为它不负责收敛 workspaceSwitching。
  const latestFormalActivationSequenceRef = useRef(0);

  const activateGatewayWorkspaceInBackground = useCallback((workspaceId: string) => {
    const requestSequence = ++backgroundWorkspaceActivationSequenceRef.current;
    const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
    invalidateWorkspaceRefreshes();
    const operation = workspaceActivationQueueRef.current.enqueue(async () => {
      await apiActivateGatewayWorkspace(resolvedApiPort, workspaceId);
    });
    void operation.then(() => {
      if (requestSequence !== backgroundWorkspaceActivationSequenceRef.current) {
        return;
      }
      void apiListAgents(resolvedApiPort, workspaceId)
        .then((agents) => {
          if (
            requestSequence !== backgroundWorkspaceActivationSequenceRef.current
            || latestStateRef.current.currentSessionWorkspaceId !== workspaceId
          ) {
            return;
          }
          setState((previous) => ({ ...previous, agents }));
        })
        .catch((error: unknown) => {
          if (
            requestSequence !== backgroundWorkspaceActivationSequenceRef.current
            || latestStateRef.current.currentSessionWorkspaceId !== workspaceId
          ) {
            return;
          }
          const message = error instanceof Error ? error.message : String(error);
          setState((previous) => ({
            ...previous,
            gatewayError: "后台加载工作区 Agent 失败: " + message,
            status: "后台加载工作区 Agent 失败: " + message,
          }));
        });
      void refreshGatewayWorkspaceStatuses(workspaceId);
    }).catch((error: unknown) => {
      if (
        requestSequence !== backgroundWorkspaceActivationSequenceRef.current
        || latestStateRef.current.currentSessionWorkspaceId !== workspaceId
      ) {
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      setState((previous) => ({
        ...previous,
        gatewayError: message,
        status: "后台切换工作区失败: " + message,
      }));
    });
  }, [
    apiPort,
    invalidateWorkspaceRefreshes,
    refreshGatewayWorkspaceStatuses,
    setState,
  ]);

  const activateGatewayWorkspace = useCallback(
    (workspaceId: string, preferredSessionId?: string | null) => {
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      backgroundWorkspaceActivationSequenceRef.current += 1;
      invalidateWorkspaceRefreshes();
      resetWorkspaceScopedState();
      const applyActivationFailure = (message: string) => {
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: "工作区切换失败",
          isBootstrapping: false,
        }));
      };
      // 收敛 workspaceSwitching 的职责只属于最近一次正式激活：被更新的正式激活
      // 顶替时它自己会 resetWorkspaceScopedState 并负责收敛，旧任务不得反向清掉。
      // 被后台激活顶替时该序列号不变，仍由本任务复位，因为后台激活即使成功也
      // 从不触碰 workspaceSwitching。
      const formalSequence = ++latestFormalActivationSequenceRef.current;
      // latest-only 队列会用两种方式吞掉正式激活的结果：尚未开始的旧任务被整条
      // 跳过，已开始但被顶替的任务其 rejection 也会被守卫吞掉。真正的结局（是否
      // 开始、是否失败、刷新是否生效）由任务体写进闭包变量，收尾统一判定，保证
      // 调用方拿到 resolve 时激活一定已生效，否则一律拿到明确失败。
      let started = false;
      let applied = false;
      let failure: unknown;
      let hasFailure = false;
      const operation = workspaceActivationQueueRef.current.enqueue(async () => {
        started = true;
        try {
          await apiActivateGatewayWorkspace(resolvedApiPort, workspaceId);
          applied = await finishWorkspaceRefresh(preferredSessionId, {
            checkGatewayWorkspaceHealth: false,
            reuseCurrentUiSettings: true,
          });
          if (applied) {
            void refreshGatewayWorkspaceStatuses(workspaceId);
          }
        } catch (error) {
          failure = error;
          hasFailure = true;
          throw error;
        }
      });
      return operation.catch(() => undefined).then(() => {
        const fail = (message: string, cause: unknown) => {
          // 只有仍是最后一次正式激活意图时才写状态，避免污染新激活正在收敛的状态。
          if (latestFormalActivationSequenceRef.current === formalSequence) {
            applyActivationFailure(message);
          }
          throw cause;
        };
        if (hasFailure) {
          const message = failure instanceof Error ? failure.message : String(failure);
          return fail(message, failure);
        }
        if (!started) {
          return fail(`工作区激活已被更新的激活请求取代，${workspaceId} 未生效`, new Error(
            `工作区激活已被更新的激活请求取代，${workspaceId} 未生效`,
          ));
        }
        if (!applied) {
          return fail(
            `工作区激活未生效：${workspaceId} 的工作区刷新已被更新的请求作废`,
            new Error(`工作区激活未生效：${workspaceId} 的工作区刷新已被更新的请求作废`),
          );
        }
      });
    },
    [
      apiPort,
      finishWorkspaceRefresh,
      invalidateWorkspaceRefreshes,
      refreshGatewayWorkspaceStatuses,
      resetWorkspaceScopedState,
      setState,
    ],
  );

  const refreshGatewayState = useCallback(async () => {
    setState((prev) => ({
      ...prev,
      gatewayError: null,
      error: null,
      isBootstrapping: prev.isBootstrapping || Boolean(prev.error),
      status: "正在刷新 Gateway 状态",
    }));
    try {
      await finishWorkspaceRefresh(currentSessionId);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((prev) => ({
        ...prev,
        gatewayError: message,
        error: message,
        status: `刷新 Gateway 状态失败: ${message}`,
      }));
      throw error;
    }
  }, [currentSessionId, finishWorkspaceRefresh, setState]);

  return {
    activateGatewayWorkspace,
    activateGatewayWorkspaceInBackground,
    refreshGatewayState,
  };
}
