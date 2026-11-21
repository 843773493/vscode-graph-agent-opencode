import { useEffect, useRef, useState } from 'react';
import { t } from '../../../../shared/core/i18n/index.js';
import type { ConfigSourcePreflightDTO, ModelReferenceDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';

/** 只展示原来源预检的版本化回执，编辑时不在前端重算或自动重测。 */
export function ModelOptionsInspection({ modelRef, version, inspect, onOpenSource }: {
  modelRef: ModelReferenceDTO; version: number;
  inspect: (modelRef: ModelReferenceDTO, signal: AbortSignal) => Promise<ConfigSourcePreflightDTO>;
  onOpenSource: (sourceKey: string) => void;
}) {
  const [receipt, setReceipt] = useState<{ version: number; data: ConfigSourcePreflightDTO }>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const request = useRef<AbortController>();
  useEffect(() => {
    setLoading(false); setError(undefined);
    return () => { request.current?.abort(); request.current = undefined; };
  }, [version, modelRef.provider_id, modelRef.model_id]);
  const run = async () => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setLoading(true); setError(undefined);
    try {
      const data = await inspect(modelRef, controller.signal);
      if (!controller.signal.aborted) setReceipt({ version, data });
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (request.current === controller) { request.current = undefined; setLoading(false); }
    }
  };
  const current = receipt?.data.model_options?.model_ref?.provider_id === modelRef.provider_id
    && receipt.data.model_options.model_ref.model_id === modelRef.model_id ? receipt : undefined;
  return <section className="configuration-model-inspection">
    <h4>{t('configuration.inspect.title')}</h4>
    <p>{t('configuration.inspect.boundary')}</p>
    <button type="button" disabled={loading} onClick={() => void run()}>
      {t(loading ? 'configuration.inspect.loading' : 'configuration.inspect.run')}
    </button>
    {error ? <p role="alert">{error}</p> : null}
    {current ? <>
      <p role="status">{t(current.version === version ? 'configuration.inspect.current' : 'configuration.inspect.stale')}</p>
      {(['draft', 'active'] as const).map(kind => {
        const options = kind === 'draft' ? current.data.model_options : current.data.active_model_options;
        return <section key={kind} data-model-options={kind}>
          <h5>{t(`configuration.inspect.${kind}`)}</h5>
          {kind === 'active' ? <p>{t('configuration.inspect.activeRevision', current.data.document?.active_revision ?? '')}</p> : null}
          {options ? <>
            <dl><dt>{t('configuration.inspect.requestUrl')}</dt><dd><code>{options.request_url}</code></dd>
              <dt>{t('configuration.inspect.catalogUrl')}</dt><dd><code>{options.catalog_url ?? t('configuration.inspect.noCatalog')}</code></dd></dl>
            <pre>{JSON.stringify(options.options, null, 2)}</pre>
            <details><summary>{t('configuration.inspect.origins')}</summary>
              <table><thead><tr><th>{t('configuration.inspect.field')}</th><th>{t('configuration.inspect.definition')}</th><th>{t('configuration.inspect.source')}</th></tr></thead>
                <tbody>{options.origins.map(origin => <tr key={origin.path}>
                  <td><code>{origin.path}</code></td><td>{t(origin.definition === 'defaults' ? 'configuration.entities.providerDefaults' : 'configuration.entities.modelOverrides')}</td>
                  <td><button type="button" title={origin.definition_path} onClick={() => onOpenSource(origin.source_key)}>{origin.source_key}</button></td>
                </tr>)}</tbody></table>
            </details>
          </> : <p>{t('configuration.inspect.notActive')}</p>}
        </section>;
      })}
    </> : null}
  </section>;
}
