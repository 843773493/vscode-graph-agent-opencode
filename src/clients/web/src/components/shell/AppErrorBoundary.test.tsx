import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import { useAppState } from "../../hooks/app/appContext";
import AppErrorBoundary from "./AppErrorBoundary";

/**
 * 共享边界的异常显示契约。真实宿主的边界位置、Provider 初始化与双根隔离
 * 由 tests/integration/clients/web/components/react_surface.mjs 验证。
 */

const mountedRenderers: ReactTestRenderer[] = [];

afterEach(() => {
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
});

/** 一个故意在 Provider 之外消费 AppState 的消费者，模拟 hook 被提到边界外的后果。 */
function ProviderLessConsumer(): React.ReactNode {
  const { state } = useAppState();
  return <div>{state.status}</div>;
}

describe("AppErrorBoundary 共享异常显示契约", () => {
  test("Provider 之外的 useAppState 必须抛出可诊断错误，而不是静默降级", () => {
    expect(() => renderToStaticMarkup(<ProviderLessConsumer />)).toThrow(
      "useAppState must be used within AppProvider",
    );
  });

  test("边界接住 Provider 之外的 hook 抛错并给出可诊断界面，而不是白屏", async () => {
    let renderer: ReactTestRenderer | undefined;
    await act(async () => {
      renderer = create(
        <AppErrorBoundary>
          <ProviderLessConsumer />
        </AppErrorBoundary>,
      );
    });
    mountedRenderers.push(renderer!);

    const tree = JSON.stringify(renderer!.toJSON());
    expect(tree).toContain("页面加载失败");
    expect(tree).toContain("useAppState must be used within AppProvider");
    expect(tree).toContain("刷新页面");
    expect(tree).toContain("app-fatal-error");
  });

});
