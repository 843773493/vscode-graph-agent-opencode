import { useEffect, useRef, useState } from 'react';
import { t } from '../../../../shared/core/i18n/index.js';
import type { ConfigActiveOptionsInspectionDTO, ConfigSourcePreflightDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';
import type { ConfigurationSelector } from './ConfigurationEntities.js';

export type ConfigurationOptionsReceipt = ConfigSourcePreflightDTO | ConfigActiveOptionsInspectionDTO;

/** 只展示原来源预检的版本化回执，编辑时不在前端重算或自动重测。 */
export function ModelOptionsInspection({ modelRef, version, inspect, onOpenSource }: {
  modelRef: ConfigurationSelector; version: number;
  inspect: (modelRef: ConfigurationSelector, signal: AbortSignal) => Promise<ConfigurationOptionsReceipt>;
  onOpenSource: (sourceKey: string, definitionPath: string) => void;
}) {
  const [receipt, setReceipt] = useState<{ version: number; data: ConfigurationOptionsReceipt }>();
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
  const inspected = modelRef.model_id === undefined ? receipt?.data.provider_options : receipt?.data.model_options;
  const current = inspected && ('provider_id' in inspected ? inspected.provider_id === modelRef.provider_id
    : inspected.model_ref?.provider_id === modelRef.provider_id && inspected.model_ref.model_id === modelRef.model_id) ? receipt : undefined;
  return <section className="configuration-model-inspection">
    <h4>{t('configuration.inspect.title')}</h4>
    <p>{t('configuration.inspect.boundary')}</p>
    <button type="button" disabled={loading} onClick={() => void run()}>
      {t(loading ? 'configuration.inspect.loading' : 'configuration.inspect.run')}
    </button>
    {error ? <p role="alert">{error}</p> : null}
    {current ? <>
      <p role="status">{t(current.version !== version ? 'configuration.inspect.stale'
        : 'active_digest' in current.data ? 'configuration.inspect.published' : 'configuration.inspect.current')}</p>
      {('active_digest' in current.data ? ['active'] as const : ['draft', 'active'] as const).map(kind => {
        const data = current.data;
        const options = modelRef.model_id === undefined
          ? kind === 'draft' || 'active_digest' in data ? data.provider_options : data.active_provider_options
          : kind === 'draft' || 'active_digest' in data ? data.model_options : data.active_model_options;
        return <section key={kind} data-model-options={kind}>
          <h5>{t(`configuration.inspect.${kind}`)}</h5>
          {kind === 'active' ? <p>{t('configuration.inspect.activeRevision', 'active_digest' in data ? data.active_revision : data.document?.active_revision ?? '')}</p> : null}
          {options ? <>
            <dl>{'request_url' in options ? <><dt>{t('configuration.inspect.requestUrl')}</dt><dd><code>{options.request_url}</code></dd></> : null}
              <dt>{t('configuration.inspect.catalogUrl')}</dt><dd><code>{options.catalog_url ?? t('configuration.inspect.noCatalog')}</code></dd></dl>
            <p>{t('configuration.node.reference')} <code>{options.config_ref}</code></p>
            <ol>{options.chain.map(node => <li key={node.node_id}><button type="button" title={node.source_uri ? node.source_uri + '#' + node.definition_path : node.node_id}
              disabled={!node.source_uri} onClick={() => onOpenSource(node.source_key, node.definition_path)}>{node.source_key}<code>{node.definition_path}</code></button></li>)}</ol>
            <details><summary>{t('configuration.node.local')}</summary><pre>{JSON.stringify(options.local_settings, null, 2)}</pre></details>
            <details><summary>{t('configuration.node.inherited')}</summary><pre>{JSON.stringify(options.inherited_settings, null, 2)}</pre></details>
            <details><summary>{t('configuration.node.network')}</summary><pre>{JSON.stringify(options.network, null, 2)}</pre></details>
            <pre>{JSON.stringify(options.options, null, 2)}</pre>
            <details><summary>{t('configuration.inspect.origins')}</summary>
              <table><thead><tr><th>{t('configuration.inspect.field')}</th><th>{t('configuration.inspect.definition')}</th><th>{t('configuration.inspect.source')}</th></tr></thead>
                <tbody>{options.origins.map(origin => <tr key={origin.path}>
                  <td><code>{origin.path}</code></td><td>{origin.definition === 'catalog' || origin.definition === 'builtin'
                    ? t('configuration.catalog.automatic') : t('configuration.node.title')}</td>
                  <td>{origin.definition === 'catalog' || origin.definition === 'builtin' ? <span title={origin.definition_path}>{origin.source_key}</span>
                    : <button type="button" title={origin.definition_path} onClick={() => onOpenSource(origin.source_key, origin.definition_path)}>{origin.source_key}</button>}</td>
                </tr>)}</tbody></table>
            </details>
          </> : <p>{t('configuration.inspect.notActive')}</p>}
        </section>;
      })}
    </> : null}
  </section>;
}
