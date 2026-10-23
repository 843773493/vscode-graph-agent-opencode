import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

export interface FakeScrollList {
  scrollHeight: number;
  scrollTop: number;
  clientHeight?: number;
}

/** 用可控尺寸替换 DOM 列表引用，使滚动 hook 测试不依赖浏览器布局。 */
export function attachFakeScrollList(
  ref: React.RefObject<HTMLDivElement>,
  list: FakeScrollList,
): void {
  (ref as unknown as { current: FakeScrollList | null }).current = list;
}

/** 统一测试 hook 的挂载、同步刷新和卸载，避免每个用例重复实现 Probe。 */
export function mountHookProbe<T>(renderHook: () => T) {
  let current!: T;
  let renderer: ReactTestRenderer | undefined;

  function Probe(): React.ReactNode {
    current = renderHook();
    return null;
  }

  act(() => {
    renderer = create(<Probe />);
  });

  return {
    get current(): T {
      return current;
    },
    run(action: () => void): void {
      act(action);
    },
    rerender(): void {
      act(() => renderer!.update(<Probe />));
    },
    unmount(): void {
      act(() => renderer!.unmount());
    },
  };
}
