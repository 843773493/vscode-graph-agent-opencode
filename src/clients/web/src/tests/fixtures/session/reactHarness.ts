import React from "react";
import {
  act,
  create,
  type ReactTestRenderer,
} from "react-test-renderer";
import { flushReactEffects } from "./asyncControl";

/** 挂载一个 React 测试渲染器并跑指定轮数的 effect，返回卸载函数。 */
export async function mountReactTestHarness(
  Harness: () => React.ReactNode,
  flushes = 2,
): Promise<() => void> {
  let renderer: ReactTestRenderer;
  await act(async () => {
    renderer = create(React.createElement(Harness));
    for (let i = 0; i < flushes; i += 1) await flushReactEffects();
  });
  return () => act(() => renderer!.unmount());
}
