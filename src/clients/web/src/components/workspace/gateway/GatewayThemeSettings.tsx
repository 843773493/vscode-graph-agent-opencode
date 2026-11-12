import { useCallback, useEffect, useState, type ChangeEvent } from "react";
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
      setNotice("已重新读取背景图片。 ");
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
    "网络背景已保存。",
  );
  const useAsset = (asset: GatewayUiAsset) => runUpdate(
    { image: new URL(asset.url, window.location.href).href, ...backgroundOptions },
    "本地背景已保存。",
  );
  const clearBackground = () => runUpdate(
    { image: "", ...backgroundOptions },
    "已清除背景图片。",
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
      setNotice("背景资源已删除。 ");
    } catch (deleteError) {
      try {
        await reload();
        setError(errorMessage(deleteError));
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
      setError(errorMessage(copyError));
    }
  };

  return (
    <div className="gateway-theme-settings">
      {error ? <div className="gateway-console-alert" role="alert"><span className="codicon codicon-error" /><div><strong>主题设置失败</strong><span>{error}</span></div></div> : null}
      {notice ? <div className="gateway-console-notice" role="status"><span className="codicon codicon-pass-filled" />{notice}</div> : null}

      <section className="gateway-theme-section">
        <div className="gateway-theme-section-heading"><div><h2>工作台主题</h2><p>会话与工作台共用主题扩展和当前 profile 的 settings.json，切换立即生效。</p></div>
          <button type="button" onClick={() => void onSelectColorTheme().catch((cause: unknown) => setError(errorMessage(cause)))}>选择颜色主题</button>
        </div>
      </section>

      <section className="gateway-theme-section">
        <div className="gateway-theme-section-heading"><div><h2>背景图片</h2><p>背景随当前 Workbench profile 的 settings.json 保存。Gateway 只保存导入的图片；删除图片会影响引用它的其他 profile。</p></div><button type="button" disabled={busy} onClick={() => void clearBackground()}>清除背景图片</button></div>
        <div className="gateway-theme-background-options">
          <label>位置<input value={position} onChange={(event) => setPosition(event.target.value)} /></label>
          <label>尺寸<input value={size} onChange={(event) => setSize(event.target.value)} /></label>
          <label>重复<select value={repeat} onChange={(event) => setRepeat(event.target.value as AppearanceBackground["repeat"])}><option value="no-repeat">不重复</option><option value="repeat">重复</option><option value="repeat-x">横向重复</option><option value="repeat-y">纵向重复</option><option value="space">均匀留空</option><option value="round">缩放铺满</option></select></label>
          <label className="wide">遮罩<input value={overlay} onChange={(event) => setOverlay(event.target.value)} /></label>
        </div>
        <div className="gateway-theme-remote-row">
          <input type="url" placeholder="https://example.com/background.webp" value={remoteUrl} onChange={(event) => setRemoteUrl(event.target.value)} />
          <button type="button" disabled={busy || !remoteUrl.trim()} onClick={() => void saveRemote()}>使用网络背景</button>
          <label className="gateway-theme-upload"><input type="file" accept="image/png,image/jpeg,image/webp,image/gif" disabled={busy} onChange={(event) => void upload(event)} />导入本地图片</label>
        </div>
        <button type="button" disabled={busy} onClick={() => void handleReload()}>重新读取配置</button>
        <div className="gateway-theme-assets">
          {assets === null ? (error ? null : <p>正在读取背景图片…</p>) : assets.length === 0 ? <p>尚未导入本地背景图片。</p> : assets.map((asset) => (
            <article key={asset.asset_id}>
              <img src={asset.url} alt="" />
              <div><strong>{asset.original_filename}</strong><small>{(asset.size / 1024).toFixed(1)} KiB{background.image === new URL(asset.url, window.location.href).href ? " · 当前使用中" : ""}</small></div>
              <button type="button" disabled={busy} onClick={() => void useAsset(asset)}>使用</button>
              <button type="button" disabled={busy || background.image === new URL(asset.url, window.location.href).href} onClick={() => void removeAsset(asset.asset_id)}>删除</button>
            </article>
          ))}
        </div>
      </section>

      <section className="gateway-theme-config-help">
        <div className="gateway-theme-section-heading"><div><strong>自定义主题</strong></div><button type="button" onClick={() => void copyConfigExample()}>复制配置示例</button></div>
        <p>在当前 profile 的 <code>settings.json</code> 中使用 <code>workbench.colorTheme</code> 选择已安装主题，以 <code>workbench.colorCustomizations</code> 调整标准颜色或 <code>boxteam.chat.*</code> 聊天颜色；背景使用 <code>boxteam.appearance.background</code>，无需重启 Gateway。</p>
      </section>
    </div>
  );
}
