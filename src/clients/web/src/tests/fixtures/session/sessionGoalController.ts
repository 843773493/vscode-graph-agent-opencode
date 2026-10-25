import React from "react";
import type { AppState } from "../../../types/frontend";
import { useSessionGoalController } from "../../../hooks/session/useSessionGoalController";
import { mountReactTestHarness } from "./reactHarness";

type GoalController = ReturnType<typeof useSessionGoalController>;

type SessionGoalControllerHandle = GoalController;

/**
 * 挂载 useSessionGoalController，用真实 React 状态机把最新 AppState 镜像到闭包。
 * 该 hook 依赖 effect 重放，必须走 create/act，不能只做一次静态渲染。
 */
export async function mountSessionGoalControllerHarness(options: {
  initial: AppState;
}): Promise<{
  unmount: () => void;
  controller: () => SessionGoalControllerHandle;
  state: () => AppState;
}> {
  const { initial } = options;
  let controller: SessionGoalControllerHandle | null = null;
  let latestState = initial;
  function Harness(): React.ReactNode {
    const [currentState, setState] = React.useState(() => initial);
    latestState = currentState;
    controller = useSessionGoalController({
      currentMainThreadId: currentState.currentSession?.thread_id ?? null,
      apiPort: 49_406,
      currentSessionId: currentState.currentSession?.session_id ?? null,
      currentWorkspaceId: currentState.currentSessionWorkspaceId,
      setState,
    });
    return null;
  }
  const unmount = await mountReactTestHarness(Harness, 1);
  if (!controller) throw new Error("useSessionGoalController Harness 未完成渲染");
  return { unmount, controller: () => controller!, state: () => latestState };
}

/**
 * Goal 控制器 Harness 工厂：保留自持状态机，用例可切换 currentSession 并注入
 * 残留 Goal 状态，句柄通过控制器集合与状态读写器回传。
 */
export function createSessionGoalControllerHarness(options: {
  apiPort: number;
  initial: AppState;
}): {
  Harness: () => React.ReactNode;
  controller: () => ReturnType<typeof useSessionGoalController>;
  state: () => AppState;
  setState: (update: (previous: AppState) => AppState) => void;
} {
  let controller: ReturnType<typeof useSessionGoalController> | null = null;
  let latestState = options.initial;
  let applyUpdate: (update: (previous: AppState) => AppState) => void = () => {
    throw new Error("useSessionGoalController Harness 尚未挂载");
  };
  function Harness(): React.ReactNode {
    const [current, setState] = React.useState<AppState>(() => options.initial);
    latestState = current;
    applyUpdate = setState;
    controller = useSessionGoalController({
      currentMainThreadId: current.currentSession?.thread_id ?? null,
      apiPort: options.apiPort,
      currentSessionId: current.currentSession?.session_id ?? null,
      currentWorkspaceId: current.currentSessionWorkspaceId,
      setState,
    });
    return null;
  }
  return {
    Harness,
    controller: () => {
      if (!controller) throw new Error("useSessionGoalController Harness 未完成渲染");
      return controller;
    },
    state: () => latestState,
    setState: (update) => applyUpdate(update),
  };
}
