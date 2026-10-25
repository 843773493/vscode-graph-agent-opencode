import React from "react";
import {
  act,
  create,
  type ReactTestRenderer,
} from "react-test-renderer";
import type { AppState } from "../../../types/frontend";
import {
  useSessionViewState,
  type SessionViewStateController,
  type SessionViewStateHost,
} from "../../../hooks/session/useSessionViewState";

/**
 * 视图状态控制器 Harness：真实 React 状态机把最新 AppState 镜像到闭包，并向
 * 控制器提供与 hooks.tsx 相同的 setStatus 写法。hostGetter 用函数在每次渲染时
 * 更新 host，便于用例在挂载后切换 lease 代际 / 镜像。
 */
export async function mountViewStateController(options: {
  host: SessionViewStateHost | ((state: AppState) => SessionViewStateHost);
  initial: AppState;
}): Promise<{
  controller: () => SessionViewStateController;
  state: () => AppState;
  renderer: ReactTestRenderer;
  update: () => Promise<void>;
  unmount: () => void;
}> {
  const { initial } = options;
  let controller: SessionViewStateController | null = null;
  let latestState = initial;
  function Probe(): React.ReactNode {
    const [current, setState] = React.useState(() => initial);
    latestState = current;
    controller = useSessionViewState({
      host: typeof options.host === "function" ? options.host(current) : options.host,
      setState,
      setStatus: (message) => {
        setState((previous) => ({ ...previous, status: message }));
      },
    });
    return null;
  }
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(React.createElement(Probe));
  });
  if (!controller) throw new Error("useSessionViewState Harness 未完成渲染");
  return {
    controller: () => controller!,
    state: () => latestState,
    renderer,
    update: async () => {
      await act(async () => {
        renderer.update(React.createElement(Probe));
      });
    },
    unmount: () => act(() => renderer.unmount()),
  };
}
