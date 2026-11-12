import { useCallback, useEffect, useState, type ChangeEvent } from "react";
import { t } from "../../../../../shared/core/i18n/index.js";
import {
  deleteGatewayUiAsset,
  listGatewayUiAssets,
  uploadGatewayUiAsset,
} from "../../../gatewayApi";
import type {
  GatewayUiAsset,
} from "../../../types/backend";
import type { AppearanceBackground } from "../../../theme/theme";
import { copyTextToClipboard } from "../../../utils/clipboard";
import { errorMessage } from "../../../utils/errorMessage";

interface GatewayThemeSettingsProps {
  apiPort: number;
  background: AppearanceBackground;
  onUpdateBackground: (value: AppearanceBackground) => Promise<void>;
  onSelectColorTheme: () => Promise<void>;
}

const THEME_CONFIG_EXAMPLE = `{
  "workbench.colorTheme": "Default Dark Modern",
  "boxteam.appearance.background": { "image": "https://example.com/background.webp", "size": "cover" },
  "workbench.colorCustomizations": {
    "boxteam.chat.background": "#1f1f1f",
    "boxteam.chat.inputBackground": "#313131",
    "boxteam.chat.requestBackground": "#2b2b2b"
  }
}`;

export default function GatewayThemeSettings({
  apiPort,
  background,
  onUpdateBackground,
  onSelectColorTheme,
}: GatewayThemeSettingsProps) {
  const [assets, setAssets] = useState<GatewayUiAsset[] | null>(null);
  const [remoteUrl, setRemoteUrl] = useState(background.image);
  const [position, setPosition] = useState(background.position);
  const [size, setSize] = useState(background.size);
  const [repeat, setRepeat] = useState(background.repeat);
  const [overlay, setOverlay] = useState(background.overlay);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setAssets(await listGatewayUiAssets(apiPort));
  }, [apiPort]);

  useEffect(() => {
    void reload().catch((loadError) => {
      setError(errorMessage(loadError));
    });
  }, [reload]);

  const handleReload = async () => {
    setBusy(true);
    setError(null);
    try {
      await reload();
      setNotice(t("gateway.theme.reloadAssetsDone"));
    } catch (reloadError) {
      setError(errorMessage(reloadError));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    setRemoteUrl(background.image);
    setPosition(background.position);
    setSize(background.size);
    setRepeat(background.repeat);
    setOverlay(background.overlay);
  }, [background]);

  const runUpdate = async (update: AppearanceBackground, success: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await onUpdateBackground(update);
      setNotice(success);
    } catch (operationError) {
      setError(errorMessage(operationError));
    } finally {
      setBusy(false);
    }
  };

  const backgroundOptions = { position, size, repeat, overlay };
  const saveRemote = () => runUpdate(
    { image: remoteUrl.trim(), ...backgroundOptions },
    t("gateway.theme.remoteSaved"),
  );
  const useAsset = (asset: GatewayUiAsset) => runUpdate(
    { image: new URL(asset.url, window.location.href).href, ...backgroundOptions },
    t("gateway.theme.localSaved"),
  );
  const clearBackground = () => runUpdate(
    { image: "", ...backgroundOptions },
    t("gateway.theme.backgroundCleared"),
  );

  const upload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await uploadGatewayUiAsset(apiPort, file);
      await reload();
      setNotice(t("gateway.theme.assetImported"));
    } catch (uploadError) {
      setError(errorMessage(uploadError));
    } finally {
      setBusy(false);
    }
  };

  const removeAsset = async (assetId: string) => {
    setBusy(true);
    setError(null);
    try {
      setAssets(await deleteGatewayUiAsset(apiPort, assetId));
      setNotice(t("gateway.theme.assetDeleted"));
    } catch (deleteError) {
      try {
        await reload();
        setError(errorMessage(deleteError));
      } catch (reloadError) {
        setError(t("gateway.theme.deleteAndReloadFailed", String(deleteError), String(reloadError)));
      }
    } finally {
      setBusy(false);
    }
  };

  const copyConfigExample = async () => {
    setError(null);
    try {
      await copyTextToClipboard(THEME_CONFIG_EXAMPLE);
      setNotice(t("gateway.theme.exampleCopied"));
    } catch (copyError) {
      setError(errorMessage(copyError));
    }
  };

  return (
    <div className="gateway-theme-settings">
      {error ? <div className="gateway-console-alert" role="alert"><span className="codicon codicon-error" /><div><strong>{t("gateway.theme.settingsFailed")}</strong><span>{error}</span></div></div> : null}
      {notice ? <div className="gateway-console-notice" role="status"><span className="codicon codicon-pass-filled" />{notice}</div> : null}

      <section className="gateway-theme-section">
        <div className="gateway-theme-section-heading"><div><h2>{t("gateway.theme.workbenchTheme")}</h2><p>{t("gateway.theme.workbenchThemeDetail")}</p></div>
          <button type="button" onClick={() => void onSelectColorTheme().catch((cause: unknown) => setError(errorMessage(cause)))}>{t("gateway.theme.selectColorTheme")}</button>
        </div>
      </section>

      <section className="gateway-theme-section">
        <div className="gateway-theme-section-heading"><div><h2>{t("gateway.theme.backgroundImage")}</h2><p>{t("gateway.theme.backgroundDetail")}</p></div><button type="button" disabled={busy} onClick={() => void clearBackground()}>{t("gateway.theme.clearBackground")}</button></div>
        <div className="gateway-theme-background-options">
          <label>{t("gateway.theme.position")}<input value={position} onChange={(event) => setPosition(event.target.value)} /></label>
          <label>{t("gateway.theme.size")}<input value={size} onChange={(event) => setSize(event.target.value)} /></label>
          <label>{t("gateway.theme.repeat")}<select value={repeat} onChange={(event) => setRepeat(event.target.value as AppearanceBackground["repeat"])}><option value="no-repeat">{t("gateway.theme.noRepeat")}</option><option value="repeat">{t("gateway.theme.repeatAll")}</option><option value="repeat-x">{t("gateway.theme.repeatHorizontal")}</option><option value="repeat-y">{t("gateway.theme.repeatVertical")}</option><option value="space">{t("gateway.theme.spaceEvenly")}</option><option value="round">{t("gateway.theme.scaleToFit")}</option></select></label>
          <label className="wide">{t("gateway.theme.overlay")}<input value={overlay} onChange={(event) => setOverlay(event.target.value)} /></label>
        </div>
        <div className="gateway-theme-remote-row">
          <input type="url" placeholder="https://example.com/background.webp" value={remoteUrl} onChange={(event) => setRemoteUrl(event.target.value)} />
          <button type="button" disabled={busy || !remoteUrl.trim()} onClick={() => void saveRemote()}>{t("gateway.theme.useRemoteBackground")}</button>
          <label className="gateway-theme-upload"><input type="file" accept="image/png,image/jpeg,image/webp,image/gif" disabled={busy} onChange={(event) => void upload(event)} />{t("gateway.theme.importLocalImage")}</label>
        </div>
        <button type="button" disabled={busy} onClick={() => void handleReload()}>{t("gateway.theme.reloadConfig")}</button>
        <div className="gateway-theme-assets">
          {assets === null ? (error ? null : <p>{t("gateway.theme.loadingAssets")}</p>) : assets.length === 0 ? <p>{t("gateway.theme.noLocalAssets")}</p> : assets.map((asset) => (
            <article key={asset.asset_id}>
              <img src={asset.url} alt="" />
              <div><strong>{asset.original_filename}</strong><small>{(asset.size / 1024).toFixed(1)} KiB{background.image === new URL(asset.url, window.location.href).href ? ` · ${t("gateway.theme.inUse")}` : ""}</small></div>
              <button type="button" disabled={busy} onClick={() => void useAsset(asset)}>{t("gateway.theme.use")}</button>
              <button type="button" disabled={busy || background.image === new URL(asset.url, window.location.href).href} onClick={() => void removeAsset(asset.asset_id)}>{t("gateway.theme.delete")}</button>
            </article>
          ))}
        </div>
      </section>

      <section className="gateway-theme-config-help">
        <div className="gateway-theme-section-heading"><div><strong>{t("gateway.theme.customTheme")}</strong></div><button type="button" onClick={() => void copyConfigExample()}>{t("gateway.theme.copyExample")}</button></div>
        <p>{t("gateway.theme.customThemeDetailPrefix")} <code>settings.json</code> {t("gateway.theme.customThemeDetailMiddle")} <code>workbench.colorTheme</code> {t("gateway.theme.customThemeDetailContinue")} <code>workbench.colorCustomizations</code> {t("gateway.theme.customThemeDetailColors")} <code>boxteam.chat.*</code> {t("gateway.theme.customThemeDetailBackground")} <code>boxteam.appearance.background</code>{t("gateway.theme.customThemeDetailEnd")}</p>
      </section>
    </div>
  );
}
