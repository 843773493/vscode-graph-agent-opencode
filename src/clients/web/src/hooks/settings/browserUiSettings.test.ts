import "fake-indexeddb/auto";
import { afterEach, expect, spyOn, test } from "bun:test";
import * as gateway from "../../gatewayApi";
import { browserViewKey, updateBrowserView } from "../../state/browserViewStore";
import { createDefaultWebUiSettings } from "../../state/uiSettings/preferences";
import { getBrowserUiSettings, updateBrowserUiSettings } from "./browserUiSettings";
const restores: Array<() => void> = [];
afterEach(() => restores.splice(0).forEach(restore => restore()));

function environment(port: number, id: string) {
  const health = spyOn(gateway, "getGatewayHealth").mockResolvedValue({ gateway_id: id } as Awaited<ReturnType<typeof gateway.getGatewayHealth>>);
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "window", { configurable: true, value: { localStorage: { getItem: () => null } } });
  restores.push(() => health.mockRestore(), () => { if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow); else Reflect.deleteProperty(globalThis, "window"); });
  return port;
}

test("已有本地偏好时不读写用户档案，局部更新保留其他字段", async () => {
  const port = environment(49690, "gateway-settings");
  const settings = createDefaultWebUiSettings();
  await updateBrowserView(browserViewKey("gateway-settings", "settings"), () => ({ ...settings, layout: { auxiliary_visible: true } }));
  const reader = spyOn(gateway, "getGatewayUiSettings").mockRejectedValue(new Error("不应读取用户档案"));
  const writer = spyOn(gateway, "updateGatewayUiSettings").mockRejectedValue(new Error("不应写入用户档案"));
  restores.push(() => reader.mockRestore(), () => writer.mockRestore());
  await updateBrowserUiSettings(port, { session_sidebar: { sort_mode: "created" } });
  const saved = await getBrowserUiSettings(port);
  expect(saved.layout.auxiliary_visible).toBe(true);
  expect(saved.session_sidebar.sort_mode).toBe("created");
  expect(reader).not.toHaveBeenCalled(); expect(writer).not.toHaveBeenCalled();
});

test("并发函数更新在真实 IndexedDB 事务中合并不同窗口的展开记录", async () => {
  const port = environment(49691, "gateway-settings-concurrent");
  await updateBrowserView(browserViewKey("gateway-settings-concurrent", "settings"), () => createDefaultWebUiSettings());
  await Promise.all(["workspace-a", "workspace-b"].map(id => updateBrowserUiSettings(port, current => ({
    session_sidebar: { collapsed_workspace_ids: [...current.session_sidebar.collapsed_workspace_ids, id] },
  }))));
  expect((await getBrowserUiSettings(port)).session_sidebar.collapsed_workspace_ids.sort()).toEqual(["workspace-a", "workspace-b"]);
});

test("主题切换仅发送可写字段，保存完整解析结果并保留本地布局", async () => {
  const port = environment(49692, "gateway-settings-theme");
  const settings = createDefaultWebUiSettings();
  const background = {
    type: "remote" as const, url: "https://example.com/background.png",
    position: "center", size: "cover", repeat: "no-repeat" as const,
    appearance: "immersive" as const, overlay: "none",
  };
  await updateBrowserView(browserViewKey("gateway-settings-theme", "settings"), () => ({
    ...settings, layout: { auxiliary_visible: true }, theme: { ...settings.theme, background },
  }));
  const resolved = { ...settings, theme: { ...settings.theme, theme_id: "blue", background } };
  const resolver = spyOn(gateway, "resolveGatewayUiSettings").mockResolvedValue(resolved);
  restores.push(() => resolver.mockRestore());
  await updateBrowserUiSettings(port, { theme: { theme_id: "blue" } });
  expect(resolver).toHaveBeenCalledWith(port, { theme: { theme_id: "blue", background } });
  const saved = await getBrowserUiSettings(port);
  expect(saved.theme).toEqual(resolved.theme);
  expect(saved.layout.auxiliary_visible).toBe(true);
});
