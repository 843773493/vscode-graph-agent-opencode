import { getGatewayHealth } from "../../gatewayApi";
import { browserViewKey, readBrowserView, updateBrowserView } from "../../state/browserViewStore";
import { createDefaultWebUiSettings, normalizeWebUiSettings } from "../../state/uiSettings/preferences";
import type { WebUiSettings, WebUiSettingsUpdate } from "../../types/backend";

const gatewayIds = new Map<number, Promise<string>>();

export function browserGatewayId(port: number): Promise<string> {
  let pending = gatewayIds.get(port);
  if (!pending) {
    pending = getGatewayHealth(port).then(health => {
      if (!health.gateway_id) throw new Error("Gateway 未提供稳定身份，无法保存浏览器视图");
      return health.gateway_id;
    });
    gatewayIds.set(port, pending);
    void pending.catch(() => gatewayIds.delete(port));
  }
  return pending;
}

/** 展示偏好按浏览器与 Gateway 保存，首次打开使用产品默认值。 */
export async function getBrowserUiSettings(port: number): Promise<WebUiSettings> {
  const key = browserViewKey(await browserGatewayId(port), "settings");
  const saved = await readBrowserView<WebUiSettings>(key);
  if (saved) return normalizeWebUiSettings(saved);
  return (await updateBrowserView<WebUiSettings>(key, current => current ?? createDefaultWebUiSettings()))!;
}

export async function updateBrowserUiSettings(port: number, input: WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate)): Promise<WebUiSettings> {
  await getBrowserUiSettings(port);
  const key = browserViewKey(await browserGatewayId(port), "settings");
  return (await updateBrowserView<WebUiSettings>(key, current => {
    if (!current) throw new Error("浏览器视图设置尚未初始化");
    const patch = typeof input === "function" ? input(current) : input;
    return normalizeWebUiSettings({
      layout: { ...current.layout, ...patch.layout },
      session_sidebar: { ...current.session_sidebar, ...patch.session_sidebar },
      workspace_file_tree: { ...current.workspace_file_tree, ...patch.workspace_file_tree },
      gateway_console: { ...current.gateway_console, ...patch.gateway_console },
    });
  }))!;
}
