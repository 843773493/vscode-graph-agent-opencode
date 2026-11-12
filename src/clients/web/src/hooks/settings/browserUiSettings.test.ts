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
  restores.push(() => health.mockRestore());
  return port;
}

test("已有本地偏好时不读写用户档案，局部更新保留其他字段", async () => {
  const port = environment(49690, "gateway-settings");
  const settings = createDefaultWebUiSettings();
  await updateBrowserView(browserViewKey("gateway-settings", "settings"), () => ({ ...settings, layout: { auxiliary_visible: true } }));
  const network = spyOn(globalThis, "fetch").mockRejectedValue(new Error("展示偏好不应发起网络请求"));
  restores.push(() => network.mockRestore());
  await updateBrowserUiSettings(port, { session_sidebar: { sort_mode: "created" } });
  const saved = await getBrowserUiSettings(port);
  expect(saved.layout.auxiliary_visible).toBe(true);
  expect(saved.session_sidebar.sort_mode).toBe("created");
  expect(network).not.toHaveBeenCalled();
});

test("并发函数更新在真实 IndexedDB 事务中合并不同窗口的展开记录", async () => {
  const port = environment(49691, "gateway-settings-concurrent");
  await updateBrowserView(browserViewKey("gateway-settings-concurrent", "settings"), () => createDefaultWebUiSettings());
  await Promise.all(["workspace-a", "workspace-b"].map(id => updateBrowserUiSettings(port, current => ({
    session_sidebar: { collapsed_workspace_ids: [...current.session_sidebar.collapsed_workspace_ids, id] },
  }))));
  expect((await getBrowserUiSettings(port)).session_sidebar.collapsed_workspace_ids.sort()).toEqual(["workspace-a", "workspace-b"]);
});
