import { browserViewKey, subscribeBrowserViews } from "../../state/browserViewStore";
import { useCallback, useEffect, useRef } from "react";
import { DEFAULT_BACKEND_PORT } from "../../api";
import {
  browserGatewayId,
  getBrowserUiSettings,
  updateBrowserUiSettings,
} from "./browserUiSettings";
import type { WebUiSettings, WebUiSettingsUpdate } from "../../types/backend";
import type { SetAppState } from "../contentViewLoaderTypes";
import { errorMessage } from "../../utils/errorMessage";

async function applyUiSettings(setState: SetAppState, settings: WebUiSettings): Promise<void> {
  setState((previous) => ({
    ...previous,
    uiSettings: settings,
    uiSettingsLoaded: true,
    agentSessionsPanelOpen:
      settings.layout.agent_sessions_panel_open
      ?? previous.agentSessionsPanelOpen,
  }));
}

export function useUiSettingsController({
  apiPort,
  setState,
  applySettings,
  onError,
}: {
  apiPort: number | null;
  setState: SetAppState;
  applySettings?: (settings: WebUiSettings) => Promise<void>;
  onError: (message: string) => void;
}) {
  const updateQueueRef = useRef<Promise<void>>(Promise.resolve());
  useEffect(() => {
    let active = true, revision = 0;
    const port = apiPort ?? DEFAULT_BACKEND_PORT;
    const unsubscribe = subscribeBrowserViews(key => {
      if (!key.endsWith(',"settings"]')) return;
      const request = ++revision;
      void browserGatewayId(port).then(async gatewayId => {
        if (key !== browserViewKey(gatewayId, "settings")) return;
        const settings = await getBrowserUiSettings(port);
        if (active && request === revision) await (applySettings ? applySettings(settings) : applyUiSettings(setState, settings));
      }).catch(error => {
        if (active) onError(`同步浏览器视图失败：${errorMessage(error)}`);
      });
    });
    return () => { active = false; unsubscribe(); };
  }, [apiPort, applySettings, setState, onError]);
  return useCallback((
    input: WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate),
  ): Promise<void> => {
    const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
    const update = updateQueueRef.current.then(async () => {
      try {
        const updatedSettings = await updateBrowserUiSettings(resolvedApiPort, input);
        const settingsToApply = updatedSettings;
        await (applySettings ? applySettings(settingsToApply) : applyUiSettings(setState, settingsToApply));
      } catch (updateError) {
        try {
          const reloadedSettings = await getBrowserUiSettings(resolvedApiPort);
          const settingsToApply = reloadedSettings;
          await (applySettings ? applySettings(settingsToApply) : applyUiSettings(setState, settingsToApply));
        } catch (reloadError) {
          throw new Error(
            `页面设置保存失败，且重新读取浏览器设置失败：保存错误=${errorMessage(updateError)}；读取错误=${errorMessage(reloadError)}`,
          );
        }
        throw updateError;
      }
    });
    updateQueueRef.current = update.catch(() => undefined);
    return update;
  }, [apiPort, setState, applySettings]);
}
