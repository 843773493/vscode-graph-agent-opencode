import { useCallback, useEffect, useRef } from "react";
import { DEFAULT_BACKEND_PORT } from "../api";
import {
  getGatewayUiSettings,
  updateGatewayUiSettings,
} from "../gatewayApi";
import { writeCachedUiSettings } from "../state/storage";
import type { WebUiSettings, WebUiSettingsUpdate } from "../types/backend";
import type { SetAppState } from "./contentViewLoaderTypes";

function applyUiSettings(setState: SetAppState, settings: WebUiSettings): void {
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
  settings,
}: {
  apiPort: number | null;
  setState: SetAppState;
  settings: WebUiSettings;
}) {
  const updateQueueRef = useRef<Promise<void>>(Promise.resolve());
  const latestSettingsRef = useRef(settings);

  useEffect(() => {
    latestSettingsRef.current = settings;
  }, [settings]);

  return useCallback((
    input: WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate),
  ): Promise<void> => {
    const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
    const update = updateQueueRef.current.then(async () => {
      const payload = typeof input === "function"
        ? input(latestSettingsRef.current)
        : input;
      try {
        const updatedSettings = await updateGatewayUiSettings(resolvedApiPort, payload);
        latestSettingsRef.current = updatedSettings;
        writeCachedUiSettings(updatedSettings);
        applyUiSettings(setState, updatedSettings);
      } catch (updateError) {
        try {
          const reloadedSettings = await getGatewayUiSettings(resolvedApiPort);
          latestSettingsRef.current = reloadedSettings;
          writeCachedUiSettings(reloadedSettings);
          applyUiSettings(setState, reloadedSettings);
        } catch (reloadError) {
          throw new Error(
            `页面设置保存失败，且重新读取 Gateway 设置失败：保存错误=${String(updateError)}；读取错误=${String(reloadError)}`,
          );
        }
        throw updateError;
      }
    });
    updateQueueRef.current = update.catch(() => undefined);
    return update;
  }, [apiPort, setState]);
}
