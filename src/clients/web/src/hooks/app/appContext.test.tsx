import { describe, expect, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  AppContext,
  ComposerContext,
  useAppState,
  useComposerState,
  type AppContextType,
  type ComposerContextType,
} from "./appContext";

/**
 * 根上下文契约：契约模块下沉后，消费者必须在 Provider 之外响亮失败，而不是拿到
 * 半成品上下文继续渲染。两个 Context 对象的身份也必须是模块级单例。
 */

function ProviderLessAppConsumer(): React.ReactNode {
  useAppState();
  return <div>app</div>;
}

function ProviderLessComposerConsumer(): React.ReactNode {
  useComposerState();
  return <div>composer</div>;
}

describe("根上下文契约", () => {
  test("AppContext 与 ComposerContext 是模块级单例", () => {
    // hot-reload 只做身份复用，生产构建下两者必须是稳定对象，否则消费者会各自
    // 拿到新 Context 并永远读不到 Provider 写入的值。
    expect(AppContext).toBe(AppContext);
    expect(ComposerContext).toBe(ComposerContext);
    expect(AppContext).not.toBe(ComposerContext as unknown as typeof AppContext);
  });

  test("Provider 之外 useAppState 抛出可诊断错误", () => {
    expect(() => renderToStaticMarkup(<ProviderLessAppConsumer />)).toThrow(
      "useAppState must be used within AppProvider",
    );
  });

  test("Provider 之外 useComposerState 抛出可诊断错误", () => {
    expect(() => renderToStaticMarkup(<ProviderLessComposerConsumer />)).toThrow(
      "useComposerState must be used within AppProvider",
    );
  });

  test("消费者在 Provider 内取回的是注入的同一份上下文", () => {
    const injected = { state: { status: "注入状态" } } as unknown as AppContextType;
    const received: AppContextType[] = [];
    function Consumer(): React.ReactNode {
      const ctx = useAppState();
      received.push(ctx);
      return <div>{ctx.state.status}</div>;
    }

    const html = renderToStaticMarkup(
      <AppContext.Provider value={injected}>
        <Consumer />
      </AppContext.Provider>,
    );

    expect(received[0]).toBe(injected);
    expect(html).toContain("注入状态");
  });

  test("Composer 消费者在 Provider 内取回注入的同一份上下文", () => {
    const injected = { state: {} } as unknown as ComposerContextType;
    const received: ComposerContextType[] = [];
    function Consumer(): React.ReactNode {
      received.push(useComposerState());
      return <div>composer-consumer</div>;
    }

    renderToStaticMarkup(
      <ComposerContext.Provider value={injected}>
        <Consumer />
      </ComposerContext.Provider>,
    );

    expect(received[0]).toBe(injected);
  });
});
