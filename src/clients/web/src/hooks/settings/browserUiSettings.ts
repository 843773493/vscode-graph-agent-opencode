import { getLatestGatewayUserViewState } from "../../api/gateway/userViewState";
import type { SessionSelection, SessionViewState } from "../../state/sessionViewState";
import { ensureGatewayUserAccess } from "../../api/gateway/userAccess";
import { migrateComposerDrafts } from "../../state/composerDrafts/storage";
import { getGatewayHealth, getGatewayUiSettings, resolveGatewayUiSettings } from "../../gatewayApi";
import { browserViewKey, readBrowserView, updateBrowserView } from "../../state/browserViewStore";
import { normalizeWebUiSettings } from "../../state/uiSettings/preferences";
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

/** 首次使用从原档案导入一次，此后浏览器数据库是展示偏好的唯一来源。 */
export async function getBrowserUiSettings(port: number): Promise<WebUiSettings> {
  const gatewayId = await browserGatewayId(port);
  if (window.localStorage.getItem("boxteam.web.composerDrafts")) {
    const access = await ensureGatewayUserAccess(port);
    await migrateComposerDrafts(gatewayId, access.user_id);
  }
  const key = browserViewKey(gatewayId, "settings");
  const saved = await readBrowserView<WebUiSettings>(key);
  if (saved) return normalizeWebUiSettings(saved);
  const initial = await getGatewayUiSettings(port);
  const access = await ensureGatewayUserAccess(port);
  if (access.kind === "user") {
    const previous = await getLatestGatewayUserViewState(port);
    if (previous) {
      const { user_id: _userId, ...view } = previous;
      await updateBrowserView<SessionViewState>(browserViewKey(gatewayId, "session", view.workspace_id, view.session_id, "main"), current => current ?? view);
      await updateBrowserView<SessionSelection>(browserViewKey(gatewayId, "selection"), current => current ?? { workspace_id: view.workspace_id, session_id: view.session_id });
    }
  }
  return (await updateBrowserView<WebUiSettings>(key, current => current ?? initial))!;
}

export async function updateBrowserUiSettings(port: number, input: WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate)): Promise<WebUiSettings> {
  const before = await getBrowserUiSettings(port);
  const patch = typeof input === "function" ? input(before) : input;
  const theme = patch.theme ? (await resolveGatewayUiSettings(port, { theme: {
    theme_id: before.theme.theme_id,
    background: before.theme.background,
    ...patch.theme,
  } })).theme : undefined;
  const key = browserViewKey(await browserGatewayId(port), "settings");
  return (await updateBrowserView<WebUiSettings>(key, current => {
    if (!current) throw new Error("浏览器视图设置尚未初始化");
    const patch = typeof input === "function" ? input(current) : input;
    return normalizeWebUiSettings({
      layout: { ...current.layout, ...patch.layout },
      session_sidebar: { ...current.session_sidebar, ...patch.session_sidebar },
      workspace_file_tree: { ...current.workspace_file_tree, ...patch.workspace_file_tree },
      gateway_console: { ...current.gateway_console, ...patch.gateway_console },
      theme: theme ?? current.theme,
    });
  }))!;
}
