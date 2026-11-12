import { useCallback, useRef, type MutableRefObject } from "react";
import { DEFAULT_BACKEND_PORT, listAgents as apiListAgents } from "../../api";
import {
  activateGatewayWorkspace as apiActivateGatewayWorkspace,
} from "../../gatewayApi";
import type { AppState } from "../../types/frontend";
import type { FinishWorkspaceRefresh, RunGatewayWorkspaceListTask, SetAppState } from "../contentViewLoaderTypes";
import { createLatestSerialTaskQueue } from "../runtime/serialTaskQueue";
import { errorMessage } from "../../utils/errorMessage";
import { t } from "../../../../shared/core/i18n/index.js";

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
  selectionIntentRef,
  gatewayListRequestRef,
  runGatewayWorkspaceListTask,
}: {
  apiPort: number | null;
  currentSessionId: string | null;
  latestStateRef: MutableRefObject<AppState>;
  setState: SetAppState;
  invalidateWorkspaceRefreshes: () => void;
  refreshGatewayWorkspaceStatuses: RefreshGatewayWorkspaceStatuses;
  resetWorkspaceScopedState: () => void;
  finishWorkspaceRefresh: FinishWorkspaceRefresh;
  selectionIntentRef: MutableRefObject<number>;
  gatewayListRequestRef: MutableRefObject<number>;
  runGatewayWorkspaceListTask: RunGatewayWorkspaceListTask;
}) {
  const workspaceActivationQueueRef = useRef(createLatestSerialTaskQueue());
  const backgroundWorkspaceActivationSequenceRef = useRef(0);
  // 最近一次正式激活的激活意图序列号：正式激活用它判断自己的收敛职责是否已被
  // 更新的正式激活接手。后台激活不更新这个 ref，因为它不负责收敛 workspaceSwitching。
  const latestFormalActivationSequenceRef = useRef(0);

  const activateGatewayWorkspaceInBackground = useCallback((workspaceId: string) => {
    const selectionIntent = selectionIntentRef.current;
    const currentSelection = () => selectionIntent === selectionIntentRef.current;
    const requestSequence = ++backgroundWorkspaceActivationSequenceRef.current;
    const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
    invalidateWorkspaceRefreshes();
    const operation = workspaceActivationQueueRef.current.enqueue(async () => {
      if (!currentSelection()) return;
      gatewayListRequestRef.current += 1;
      await runGatewayWorkspaceListTask(async () => {
        if (currentSelection()) await apiActivateGatewayWorkspace(resolvedApiPort, workspaceId);
      });
    });
    void operation.then(() => {
      if (!currentSelection() || requestSequence !== backgroundWorkspaceActivationSequenceRef.current) {
        return;
      }
      void apiListAgents(resolvedApiPort, workspaceId)
        .then((agents) => {
          if (
            requestSequence !== backgroundWorkspaceActivationSequenceRef.current
            || !currentSelection()
            || latestStateRef.current.currentSessionWorkspaceId !== workspaceId
          ) {
            return;
          }
          setState((previous) => (
            requestSequence === backgroundWorkspaceActivationSequenceRef.current
            && currentSelection()
            && latestStateRef.current.currentSessionWorkspaceId === workspaceId
              ? { ...previous, agents }
              : previous
          ));
        })
        .catch((error: unknown) => {
          if (
            requestSequence !== backgroundWorkspaceActivationSequenceRef.current
            || !currentSelection()
            || latestStateRef.current.currentSessionWorkspaceId !== workspaceId
          ) {
            return;
          }
          const message = errorMessage(error);
          setState((previous) => (
            requestSequence === backgroundWorkspaceActivationSequenceRef.current
            && currentSelection()
            && latestStateRef.current.currentSessionWorkspaceId === workspaceId
              ? {
                  ...previous,
                  gatewayError: t("gateway.workspace.backgroundAgentLoadFailed", message),
                  status: t("gateway.workspace.backgroundAgentLoadFailed", message),
                }
              : previous
          ));
        });
      void refreshGatewayWorkspaceStatuses(workspaceId);
    }).catch((error: unknown) => {
      if (
        requestSequence !== backgroundWorkspaceActivationSequenceRef.current
        || !currentSelection()
        || latestStateRef.current.currentSessionWorkspaceId !== workspaceId
      ) {
        return;
      }
      const message = errorMessage(error);
      // 会话选择已先更新本地镜像；激活失败后用完整工作区刷新归还权威状态所有权。
      void finishWorkspaceRefresh(currentSessionId, { selectionIntent }).then(() => {
        if (
          requestSequence !== backgroundWorkspaceActivationSequenceRef.current
          || !currentSelection()
        ) {
          return;
        }
        setState((previous) => (
          requestSequence === backgroundWorkspaceActivationSequenceRef.current
          && currentSelection()
            ? {
                ...previous,
                gatewayError: message,
                status: t("gateway.workspace.backgroundSwitchFailed", message),
              }
            : previous
        ));
      }).catch((refreshError: unknown) => {
        if (!currentSelection() || requestSequence !== backgroundWorkspaceActivationSequenceRef.current) {
          return;
        }
        const refreshMessage = errorMessage(refreshError);
        setState((previous) => (
          !currentSelection() || requestSequence !== backgroundWorkspaceActivationSequenceRef.current
            ? previous
            : {
                ...previous,
                gatewayError: message,
                status: t("gateway.workspace.backgroundSwitchAndRefreshFailed", message, refreshMessage),
              }
        ));
      });
    });
  }, [
    apiPort,
    currentSessionId,
    finishWorkspaceRefresh,
    invalidateWorkspaceRefreshes,
    refreshGatewayWorkspaceStatuses,
    setState,
  ]);

  const activateGatewayWorkspace = useCallback(
    (workspaceId: string, preferredSessionId?: string | null) => {
      const selectionIntent = ++selectionIntentRef.current;
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      backgroundWorkspaceActivationSequenceRef.current += 1;
      invalidateWorkspaceRefreshes();
      resetWorkspaceScopedState();
      // 收敛 workspaceSwitching 的职责只属于最近一次正式激活：被更新的正式激活
      // 顶替时它自己会 resetWorkspaceScopedState 并负责收敛，旧任务不得反向清掉。
      // 被后台激活顶替时该序列号不变，仍由本任务复位，因为后台激活即使成功也
      // 从不触碰 workspaceSwitching。
      const formalSequence = ++latestFormalActivationSequenceRef.current;
      const isCurrentFormalActivation = () => (
        latestFormalActivationSequenceRef.current === formalSequence
        && selectionIntent === selectionIntentRef.current
      );
      const applyActivationFailure = (message: string, status = t("gateway.workspace.switchFailed")) => {
        if (!isCurrentFormalActivation()) return;
        setState((prev) => isCurrentFormalActivation()
          ? {
              ...prev,
              workspaceSwitching: false,
              gatewayError: message,
              error: message,
              status,
              isBootstrapping: false,
            }
          : prev);
      };
      // latest-only 队列会用两种方式吞掉正式激活的结果：尚未开始的旧任务被整条
      // 跳过，已开始但被顶替的任务其 rejection 也会被守卫吞掉。真正的结局（是否
      // 开始、是否失败、刷新是否生效）由任务体写进闭包变量，收尾统一判定，保证
      // 调用方拿到 resolve 时激活一定已生效，否则一律拿到明确失败。
      let started = false;
      // 本轮刷新真正落到 state 的活动工作区 id：null 表示刷新被作废，
      // 非 null 但不同于 workspaceId 表示自动健康回退把活动工作区切到了别处。
      let appliedWorkspaceId: string | null = null;
      let failure: unknown;
      let hasFailure = false;
      let hasActivationApiFailure = false;
      let authorityRefreshFailure: unknown;
      let hasAuthorityRefreshFailure = false;
      const operation = workspaceActivationQueueRef.current.enqueue(async () => {
        started = true;
        try {
          gatewayListRequestRef.current += 1;
          await runGatewayWorkspaceListTask(async () => {
            if (selectionIntent !== selectionIntentRef.current) return;
            try {
              await apiActivateGatewayWorkspace(resolvedApiPort, workspaceId);
            } catch (error) {
              hasActivationApiFailure = true;
              throw error;
            }
          });
          if (selectionIntent !== selectionIntentRef.current) return;
          appliedWorkspaceId = await finishWorkspaceRefresh(preferredSessionId, {
            checkGatewayWorkspaceHealth: false,
            reuseCurrentUiSettings: true,
            selectionIntent,
          });
          if (appliedWorkspaceId === workspaceId) {
            void refreshGatewayWorkspaceStatuses(workspaceId);
          }
        } catch (error) {
          failure = error;
          hasFailure = true;
          throw error;
        }
      });
      return operation.catch(() => undefined).then(async () => {
        const fail = (message: string, cause: unknown, status = t("gateway.workspace.switchFailed")) => {
          // 只有仍是最后一次正式激活意图时才写状态，避免污染新激活正在收敛的状态。
          applyActivationFailure(message, status);
          throw cause;
        };
        if (hasFailure) {
          // 只对正式激活 API 本身的拒绝回读；成功后的工作区刷新失败不递归重试。
          // 队列任务已退出后再调用，避免在同一列表队列内嵌套刷新造成自锁。
          if (hasActivationApiFailure && isCurrentFormalActivation()) {
            try {
              await finishWorkspaceRefresh(preferredSessionId, { selectionIntent });
            } catch (refreshError) {
              authorityRefreshFailure = refreshError;
              hasAuthorityRefreshFailure = true;
            }
          }
          const message = errorMessage(failure);
          const status = hasActivationApiFailure && hasAuthorityRefreshFailure
            ? t("gateway.workspace.switchAndRefreshFailed", message, errorMessage(authorityRefreshFailure))
            : t("gateway.workspace.switchFailed");
          return fail(message, failure, status);
        }
        if (!started) {
          return fail(t("gateway.workspace.activationSuperseded", workspaceId), new Error(
            t("gateway.workspace.activationSuperseded", workspaceId),
          ));
        }
        if (appliedWorkspaceId === null) {
          return fail(
            t("gateway.workspace.activationRefreshSuperseded", workspaceId),
            new Error(t("gateway.workspace.activationRefreshSuperseded", workspaceId)),
          );
        }
        // 刷新本身生效了，但生效的活动工作区不是请求的那个：自动健康回退
        // （请求的工作区 offline）把活动工作区改到了别处，激活请求的工作区
        // 从未成为活动工作区，绝不能给调用方假成功。
        if (appliedWorkspaceId !== workspaceId) {
          return fail(
            t("gateway.workspace.activationDidNotBecomeActive", workspaceId, appliedWorkspaceId),
            new Error(
              t("gateway.workspace.activationDidNotBecomeActive", workspaceId, appliedWorkspaceId),
            ),
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

  const refreshGatewayState = useCallback(async (expectedSelectionIntent?: number) => {
    const selectionIntent = expectedSelectionIntent ?? selectionIntentRef.current;
    if (selectionIntent !== selectionIntentRef.current) return;
    setState((prev) => ({
      ...prev,
      gatewayError: null,
      error: null,
      isBootstrapping: prev.isBootstrapping || Boolean(prev.error),
        status: t("gateway.workspace.refreshingGatewayStatus"),
    }));
    try {
      await finishWorkspaceRefresh(currentSessionId, { selectionIntent });
    } catch (error) {
      const message = errorMessage(error);
      setState((prev) => selectionIntent === selectionIntentRef.current ? ({
        ...prev,
        gatewayError: message,
        error: message,
        status: t("gateway.workspace.refreshGatewayStatusFailed", message),
      }) : prev);
      throw error;
    }
  }, [currentSessionId, finishWorkspaceRefresh, setState]);

  return {
    activateGatewayWorkspace,
    activateGatewayWorkspaceInBackground,
    refreshGatewayState,
  };
}
