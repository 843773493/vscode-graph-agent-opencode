import { useEffect, useState, type ChangeEvent } from 'react';
import { formatNumber, t } from '../../../../../shared/core/i18n/index.js';
import { deleteGatewayUiAsset, listGatewayUiAssets, uploadGatewayUiAsset } from '../../../gatewayApi';
import type { GatewayUiAsset } from '../../../types/backend';
import type { AppearanceBackground } from '../../../theme/theme';
import { errorMessage } from '../../../utils/errorMessage';
import { copyTextToClipboard } from '../../../utils/clipboard';

/** Gateway 只持有背景图片资产；应用到窗口仍写原生用户设置。 */
export default function GatewayBackgroundAssets({ apiPort, background, onUpdateBackground }: {
  apiPort: number; background: AppearanceBackground; onUpdateBackground: (value: AppearanceBackground) => Promise<void>;
}) {
  const [assets, setAssets] = useState<GatewayUiAsset[]>();
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    void listGatewayUiAssets(apiPort).then(value => { if (active) setAssets(value); })
      .catch(cause => { if (active) setError(errorMessage(cause)); });
    return () => { active = false; };
  }, [apiPort]);
  const run = async (action: () => Promise<void>) => {
    setBusy(true); setError(undefined); setNotice(undefined);
    try { await action(); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setBusy(false); }
  };
  const reload = async () => { setAssets(await listGatewayUiAssets(apiPort)); };
  const upload = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]; event.target.value = '';
    if (file) void run(async () => { await uploadGatewayUiAsset(apiPort, file); await reload(); setNotice(t('gateway.theme.assetImported')); });
  };
  const remove = async (asset: GatewayUiAsset) => {
    try { setAssets(await deleteGatewayUiAsset(apiPort, asset.asset_id)); }
    catch (cause) {
      // 写入失败后重读权威列表；不能只从本地列表移除来假装删除成功。
      try { await reload(); }
      catch (readError) { throw new AggregateError([cause, readError], t('gateway.theme.deleteAndReloadFailed', errorMessage(cause), errorMessage(readError))); }
      throw cause;
    }
  };
  return <section className="gateway-theme-section panel-feedback-host" aria-label={t('gateway.theme.backgroundImage')}>
    <p>{t('gateway.settings.assetScope')}</p>
    {error ? <p role="alert">{t('gateway.theme.settingsFailed')} · {error}</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
    <div className="gateway-control-actions">
      <label className="gateway-theme-upload"><input type="file" accept="image/png,image/jpeg,image/webp,image/gif" disabled={busy} onChange={upload} />{t('gateway.theme.importLocalImage')}</label>
      <button type="button" disabled={busy} onClick={() => void run(reload)}>{t('gateway.theme.reloadConfig')}</button>
    </div>
    <div className="gateway-theme-assets">
      {assets === undefined ? (error ? null : <p role="status">{t('gateway.theme.loadingAssets')}</p>) : assets.length === 0 ? <p>{t('gateway.theme.noLocalAssets')}</p> : assets.map(asset => {
        const url = new URL(asset.url, window.location.href).href;
        const inUse = background.image === url;
        return <article key={asset.asset_id}>
          <img src={asset.url} alt="" />
          <div><strong>{asset.original_filename}</strong><small>{formatNumber(asset.size / 1024, { maximumFractionDigits: 1 })} KiB{inUse ? ` · ${t('gateway.theme.inUse')}` : ''}</small></div>
          <button type="button" disabled={busy} onClick={() => void run(() => onUpdateBackground({ ...background, image: url }))}>{t('gateway.theme.use')}</button>
          <button type="button" disabled={busy} onClick={() => void run(async () => { await copyTextToClipboard(url); setNotice(t('gateway.theme.exampleCopied')); })}>{t('gateway.diagnostics.copy')}</button>
          <button type="button" disabled={busy || inUse} onClick={() => void run(() => remove(asset))}>{t('gateway.theme.delete')}</button>
        </article>;
      })}
    </div>
  </section>;
}
