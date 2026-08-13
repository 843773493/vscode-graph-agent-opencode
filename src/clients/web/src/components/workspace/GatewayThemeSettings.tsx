import { useCallback, useEffect, useState, type ChangeEvent } from "react";
import {
  deleteGatewayUiAsset,
  getGatewayThemes,
  listGatewayUiAssets,
  uploadGatewayUiAsset,
} from "../../gatewayApi";
import type {
  GatewayThemeBackground,
  GatewayThemeCatalog,
  GatewayUiAsset,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "../../types/backend";
import { DEFAULT_THEME_BACKGROUND_OVERLAY } from "../../theme";
import { copyTextToClipboard } from "../../utils/clipboard";

interface GatewayThemeSettingsProps {
  apiPort: number;
  settings: WebUiSettings;
  onUpdateSettings: (update: WebUiSettingsUpdate) => Promise<void>;
}

const DEFAULT_BACKGROUND_OPTIONS = {
  position: "center",
  size: "cover",
  repeat: "no-repeat" as const,
  appearance: "immersive" as const,
  overlay: DEFAULT_THEME_BACKGROUND_OVERLAY,
};

const THEME_CONFIG_EXAMPLE = `{
  "ui": {
    "theme": {
      "default_theme_id": "warm",
      "custom_themes": [{
        "id": "my-theme",
        "label": "我的主题",
        "extends": "warm",
        "color_scheme": "light",
        "tokens": {
          "--bt-chrome-surface": "color-mix(in srgb, var(--bt-panel-background) 82%, transparent)",
          "--bt-workspace-surface": "color-mix(in srgb, var(--bt-surface-background) 74%, transparent)"
        }
      }]
    }
  }
}`;

export default function GatewayThemeSettings({
  apiPort,
  settings,
  onUpdateSettings,
}: GatewayThemeSettingsProps) {
  const [catalog, setCatalog] = useState<GatewayThemeCatalog | null>(null);
  const [assets, setAssets] = useState<GatewayUiAsset[]>([]);
  const [remoteUrl, setRemoteUrl] = useState("");
  const [position, setPosition] = useState(DEFAULT_BACKGROUND_OPTIONS.position);
  const [size, setSize] = useState(DEFAULT_BACKGROUND_OPTIONS.size);
  const [repeat, setRepeat] = useState<GatewayThemeBackground["repeat"]>("no-repeat");
  const [appearance, setAppearance] = useState<GatewayThemeBackground["appearance"]>(DEFAULT_BACKGROUND_OPTIONS.appearance);
  const [overlay, setOverlay] = useState(DEFAULT_BACKGROUND_OPTIONS.overlay);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const reload = useCallback(async () => {
    const [nextCatalog, nextAssets] = await Promise.all([
      getGatewayThemes(apiPort),
      listGatewayUiAssets(apiPort),
    ]);
    setCatalog(nextCatalog);
    setAssets(nextAssets);
  }, [apiPort]);

  useEffect(() => {
    void reload().catch((loadError) => {
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    });
  }, [reload]);

  const handleReload = async () => {
    setBusy(true);
    setError(null);
    try {
      await reload();
      setNotice("已重新读取 Gateway 主题配置。 ");
    } catch (reloadError) {
      setError(reloadError instanceof Error ? reloadError.message : String(reloadError));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    const background = settings.theme.background;
    setRemoteUrl(background?.type === "remote" ? background.url ?? "" : "");
    setPosition(background?.position ?? DEFAULT_BACKGROUND_OPTIONS.position);
    setSize(background?.size ?? DEFAULT_BACKGROUND_OPTIONS.size);
    setRepeat(background?.repeat ?? DEFAULT_BACKGROUND_OPTIONS.repeat);
    setAppearance(background?.appearance ?? DEFAULT_BACKGROUND_OPTIONS.appearance);
    setOverlay(background?.overlay ?? DEFAULT_BACKGROUND_OPTIONS.overlay);
  }, [settings.theme.background]);

  const runUpdate = async (update: WebUiSettingsUpdate, success: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await onUpdateSettings(update);
      await reload();
      setNotice(success);
    } catch (operationError) {
      try {
        await reload();
      } catch (reloadError) {
        setError(`主题操作失败，且重新读取 Gateway 状态失败：${String(operationError)}；${String(reloadError)}`);
        return;
      }
      setError(operationError instanceof Error ? operationError.message : String(operationError));
    } finally {
      setBusy(false);
    }
  };

  const switchTheme = (themeId: string) => runUpdate(
    { theme: { theme_id: themeId } },
    `已切换到「${catalog?.items.find((item) => item.id === themeId)?.label ?? themeId}」主题。`,
  );

  const backgroundOptions = { position, size, repeat, appearance, overlay };
  const saveRemote = () => runUpdate(
    { theme: { background: { type: "remote", url: remoteUrl.trim(), ...backgroundOptions } } },
    "网络背景已保存。",
  );
  const useAsset = (assetId: string) => runUpdate(
    { theme: { background: { type: "gateway_asset", asset_id: assetId, ...backgroundOptions } } },
    "本地背景已保存。",
  );
  const clearBackground = () => runUpdate(
    { theme: { background: null } },
    "已恢复当前主题的默认背景。",
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
      setNotice("背景图片已导入 Gateway；请选择图片后点击使用。 ");
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : String(uploadError));
    } finally {
      setBusy(false);
    }
  };

  const removeAsset = async (assetId: string) => {
    setBusy(true);
    setError(null);
    try {
      setAssets(await deleteGatewayUiAsset(apiPort, assetId));
      setNotice("背景资源已删除。 ");
    } catch (deleteError) {
      try {
        await reload();
        setError(deleteError instanceof Error ? deleteError.message : String(deleteError));
      } catch (reloadError) {
        setError(`删除背景资源失败，且重新读取 Gateway 状态失败：${String(deleteError)}；${String(reloadError)}`);
      }
    } finally {
      setBusy(false);
    }
  };

  const copyConfigExample = async () => {
    setError(null);
    try {
      await copyTextToClipboard(THEME_CONFIG_EXAMPLE);
      setNotice("主题配置示例已复制。 ");
    } catch (copyError) {
      setError(copyError instanceof Error ? copyError.message : String(copyError));
    }
  };

  if (!catalog) {
    return <div className="gateway-theme-loading">正在读取 Gateway 主题配置…</div>;
  }

  return (
    <div className="gateway-theme-settings">
      {error ? <div className="gateway-console-alert" role="alert"><span className="codicon codicon-error" /><div><strong>主题设置失败</strong><span>{error}</span></div></div> : null}
      {notice ? <div className="gateway-console-notice" role="status"><span className="codicon codicon-pass-filled" />{notice}</div> : null}

      <section className="gateway-theme-section">
        <div className="gateway-theme-section-heading"><div><h2>快速切换</h2><p>主题由 Gateway 统一解析并持久化，切换无需刷新页面。</p></div><button type="button" disabled={busy} onClick={() => void handleReload()}>重新读取配置</button></div>
        <div className="gateway-theme-grid">
          {catalog.items.map((theme) => (
            <button
              type="button"
              key={theme.id}
              className={settings.theme.theme_id === theme.id ? "selected" : undefined}
              disabled={busy}
              onClick={() => void switchTheme(theme.id)}
            >
              <span className="gateway-theme-swatch" style={{
                background: theme.preview_tokens["--bt-page-background"],
                backgroundImage: theme.background_image_url
                  ? `url(${JSON.stringify(theme.background_image_url)})`
                  : undefined,
                backgroundSize: "cover",
                borderColor: theme.preview_tokens["--bt-accent"],
              }}><i style={{ background: theme.preview_tokens["--bt-panel-background"] }} /></span>
              <span><strong>{theme.label}</strong><small>{theme.source === "builtin" ? "内置主题" : `自定义 · 基于 ${theme.extends}`}</small></span>
              {settings.theme.theme_id === theme.id ? <span className="codicon codicon-check" aria-label="当前主题" /> : null}
            </button>
          ))}
        </div>
      </section>

      <section className="gateway-theme-section">
        <div className="gateway-theme-section-heading"><div><h2>背景图片</h2><p>支持直接使用网络 URL，或将本地图片导入 Gateway 后通过同源 URL 加载。</p></div><button type="button" disabled={busy} onClick={() => void clearBackground()}>使用主题默认背景</button></div>
        <div className="gateway-theme-background-options">
          <label>位置<input value={position} onChange={(event) => setPosition(event.target.value)} /></label>
          <label>尺寸<input value={size} onChange={(event) => setSize(event.target.value)} /></label>
          <label>重复<select value={repeat} onChange={(event) => setRepeat(event.target.value as GatewayThemeBackground["repeat"])}><option value="no-repeat">不重复</option><option value="repeat">重复</option><option value="repeat-x">横向重复</option><option value="repeat-y">纵向重复</option><option value="space">均匀留空</option><option value="round">缩放铺满</option></select></label>
          <label>外观<select value={appearance} onChange={(event) => setAppearance(event.target.value as GatewayThemeBackground["appearance"])}><option value="immersive">沉浸背景</option><option value="theme">保持主题配色</option></select></label>
          <label className="wide">遮罩<input value={overlay} onChange={(event) => setOverlay(event.target.value)} /></label>
        </div>
        <div className="gateway-theme-remote-row">
          <input type="url" placeholder="https://example.com/background.webp" value={remoteUrl} onChange={(event) => setRemoteUrl(event.target.value)} />
          <button type="button" disabled={busy || !remoteUrl.trim()} onClick={() => void saveRemote()}>使用网络背景</button>
          <label className="gateway-theme-upload"><input type="file" accept="image/png,image/jpeg,image/webp,image/gif" disabled={busy} onChange={(event) => void upload(event)} />导入本地图片</label>
        </div>
        <div className="gateway-theme-assets">
          {assets.length === 0 ? <p>尚未导入本地背景图片。</p> : assets.map((asset) => (
            <article key={asset.asset_id}>
              <img src={asset.url} alt="" />
              <div><strong>{asset.original_filename}</strong><small>{(asset.size / 1024).toFixed(1)} KiB{asset.referenced_theme_ids.length ? ` · ${asset.referenced_theme_ids.join("、")} 使用中` : ""}</small></div>
              <button type="button" disabled={busy} onClick={() => void useAsset(asset.asset_id)}>使用</button>
              <button type="button" disabled={busy} onClick={() => void removeAsset(asset.asset_id)}>删除</button>
            </article>
          ))}
        </div>
      </section>

      <section className="gateway-theme-config-help">
        <div className="gateway-theme-section-heading"><div><strong>自定义主题</strong></div><button type="button" onClick={() => void copyConfigExample()}>复制配置示例</button></div>
        <p>在 <code>gateway.jsonc</code> 的 <code>ui.theme.custom_themes</code> 中继承 warm、green 或 blue，并只覆盖需要调整的 <code>--bt-*</code> token。保存后重启或重载 Gateway 配置即可出现在这里。</p>
      </section>
    </div>
  );
}
