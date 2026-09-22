import { useCallback, useEffect, useRef } from "react";
import { DEFAULT_BACKEND_PORT } from "../../api";
import {
  getGatewayUiSettings,
  updateGatewayUiSettings,
} from "../../gatewayApi";
import { writeCachedUiSettings } from "../../state/storage";
import { mergeGuestWebUiSettings } from "../../state/uiSettings/preferences";
import type { WebUiSettings, WebUiSettingsUpdate } from "../../types/backend";
import type { SetAppState } from "../contentViewLoaderTypes";
import { loadAndApplyResolvedGatewayTheme } from "../../theme/theme";

async function applyUiSettings(setState: SetAppState, settings: WebUiSettings): Promise<void> {
  if (!settings.theme.resolved_theme) {
    throw new Error("Gateway UI Settings 缺少已解析主题");
  }
  let themeLoadError: unknown = null;
  try {
    await loadAndApplyResolvedGatewayTheme(settings.theme.resolved_theme);
  } catch (error) {
    themeLoadError = error;
  }
  setState((previous) => ({
    ...previous,
    uiSettings: settings,
    uiSettingsLoaded: true,
    agentSessionsPanelOpen:
      settings.layout.agent_sessions_panel_open
      ?? previous.agentSessionsPanelOpen,
  }));
  if (themeLoadError) throw themeLoadError;
}

export function useUiSettingsController({
  apiPort,
  setState,
  settings,
  isGuestView,
}: {
  apiPort: number | null;
  setState: SetAppState;
  settings: WebUiSettings;
  isGuestView: boolean;
}) {
  const updateQueueRef = useRef<Promise<void>>(Promise.resolve());
  const latestSettingsRef = useRef(settings);
  const latestIsGuestViewRef = useRef(isGuestView);

  useEffect(() => {
    latestSettingsRef.current = settings;
    latestIsGuestViewRef.current = isGuestView;
  }, [isGuestView, settings]);

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
        const settingsToApply = latestIsGuestViewRef.current
          ? mergeGuestWebUiSettings(
              latestSettingsRef.current,
              updatedSettings,
              payload,
            )
          : updatedSettings;
        latestSettingsRef.current = settingsToApply;
        writeCachedUiSettings(settingsToApply);
        await applyUiSettings(setState, settingsToApply);
      } catch (updateError) {
        try {
          const reloadedSettings = await getGatewayUiSettings(resolvedApiPort);
          const settingsToApply = latestIsGuestViewRef.current
            ? latestSettingsRef.current
            : reloadedSettings;
          latestSettingsRef.current = settingsToApply;
          writeCachedUiSettings(settingsToApply);
          await applyUiSettings(setState, settingsToApply);
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
