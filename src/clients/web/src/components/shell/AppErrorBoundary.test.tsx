import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { renderToStaticMarkup } from "react-dom/server";
import { useAppState } from "../../hooks";
import App from "../../App";
import AppErrorBoundary from "./AppErrorBoundary";

/**
 * 启动链路的兜底契约：AppErrorBoundary 必须是应用最外层包装，AppProvider 必须在
 * 它内部。否则 useAppState 在 Provider 之外抛错时没有边界可接，页面会整屏白屏。
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

describe("AppErrorBoundary 启动兜底契约", () => {
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

  test("Provider 缺失时整个 App 也必须落在边界内，不得白屏", async () => {
    let renderer: ReactTestRenderer | undefined;
    await act(async () => {
      renderer = create(
        <AppErrorBoundary>
          <App />
        </AppErrorBoundary>,
      );
    });
    mountedRenderers.push(renderer!);

    const tree = JSON.stringify(renderer!.toJSON());
    // App 在 Provider 外会先用 useWarmConfirm 抛错；关键是页面仍有可见兜底文案。
    expect(tree).toContain("页面加载失败");
    expect(tree).toContain("app-fatal-error");
  });

  test("main.tsx 必须把 AppErrorBoundary 包在 AppProvider 外层", async () => {
    const main = await Bun.file(new URL("../../main.tsx", import.meta.url)).text();
    // 边界必须在 Provider 外层：Provider 自身初始化抛错时，只有外层边界能接住。
    expect(main).toMatch(
      /<AppErrorBoundary>[\s\S]*?<AppProvider>[\s\S]*?<\/AppProvider>[\s\S]*?<\/AppErrorBoundary>/,
    );
    expect(main).not.toMatch(/<AppProvider>[\s\S]*?<AppErrorBoundary>/);
  });
});
