import { afterEach, expect, spyOn, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import * as gatewayApi from "./browserUiSettings";
import type { AppState } from "../../types/frontend";
import { createDefaultWebUiSettings } from "../../state/uiSettings/preferences";
import { useUiSettingsController } from "./useUiSettingsController";
import * as browserViews from "../../state/browserViewStore";

const API_PORT = 49_641;

const mountedRenderers: ReactTestRenderer[] = [];
const restoreSpies: Array<() => void> = [];

function spyOnGatewayApi<Name extends keyof typeof gatewayApi>(
  name: Name,
): ReturnType<typeof spyOn<typeof gatewayApi, Name>> {
  const spy = spyOn(gatewayApi, name);
  restoreSpies.push(() => spy.mockRestore());
  return spy;
}

afterEach(() => {
  for (const renderer of mountedRenderers.splice(0)) {
    act(() => renderer.unmount());
  }
  for (const restore of restoreSpies.splice(0)) restore();
});

test("同源设置同步读取失败交给宿主错误出口，不伪造设置成功", async () => {
  let notify!: (key: string) => void;
  const subscription = spyOn(browserViews, "subscribeBrowserViews").mockImplementation(listener => {
    notify = listener;
    return () => undefined;
  });
  restoreSpies.push(() => subscription.mockRestore());
  spyOnGatewayApi("browserGatewayId").mockResolvedValue("gateway-sync");
  spyOnGatewayApi("getBrowserUiSettings").mockRejectedValue(new Error("设置事务不可读"));
  const errors: string[] = [];
  const updates: unknown[] = [];
  function Probe(): React.ReactNode {
    useUiSettingsController({ apiPort: API_PORT, onError: message => errors.push(message),
      setState: next => { updates.push(next); } });
    return null;
  }
  await act(async () => { mountedRenderers.push(create(<Probe />)); });
  await act(async () => { notify(browserViews.browserViewKey("gateway-sync", "settings")); });
  expect(errors).toEqual(["同步浏览器视图失败：设置事务不可读"]);
  expect(updates).toEqual([]);
});

test("保存与重新读取同时失败时的复合文案走唯一错误归一，不泄漏 Error: 前缀", async () => {
  // 复合文案此前用裸 String(error)，会把「Error: 」前缀一起展示给用户；全仓只允许
  // utils/errorMessage 这一处做错误归一。
  spyOnGatewayApi("updateBrowserUiSettings").mockRejectedValue(new Error("保存被拒绝"));
  spyOnGatewayApi("getBrowserUiSettings").mockRejectedValue(new Error("读取被拒绝"));

  let updateUiSettings!: ReturnType<typeof useUiSettingsController>;
  let state = { uiSettings: createDefaultWebUiSettings() } as unknown as AppState;

  function Probe(): React.ReactNode {
    updateUiSettings = useUiSettingsController({
      apiPort: API_PORT,
      onError: message => { throw new Error(message); },
      setState: (next) => {
        state = typeof next === "function" ? next(state) : next;
      },
    });
    return null;
  }

  let renderer: ReactTestRenderer | undefined;
  await act(async () => {
    renderer = create(<Probe />);
  });
  mountedRenderers.push(renderer!);

  await act(async () => {
    await expect(updateUiSettings({ layout: { auxiliary_visible: true } }))
      .rejects.toThrow(
        "页面设置保存失败，且重新读取浏览器设置失败：保存错误=保存被拒绝；读取错误=读取被拒绝",
      );
  });
});
