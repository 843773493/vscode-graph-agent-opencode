import { useEffect, useId, useRef, useState } from 'react';
import { t } from '../../../../shared/core/i18n/index.js';
import { listLlmCatalog, listLlmProviders } from '../../api/configSources.js';
import type { GatewayWorkspaceRequestTarget } from '../../api/workspaceIdentity.js';
import AnchoredOverlay from '../overlays/AnchoredOverlay.js';
import { modelMetadataDetails } from './modelMetadata.js';
import { errorSummary } from '../../utils/errorMessage.js';

export interface CatalogChoice {
  id: string;
  label: string;
  detail?: string;
  endpoint?: string;
  protocol?: string;
}

/** 目录只负责查询与选择；浮层复用已有定位、裁切和焦点管理。 */
export function ModelCatalogSelect({ kind, provider, target, signal, disabled, custom = false, needsConnection = false, onSelect }: {
  kind: 'provider' | 'model'; provider: string; target: GatewayWorkspaceRequestTarget; signal: AbortSignal;
  disabled: boolean; custom?: boolean; needsConnection?: boolean; onSelect: (choice: CatalogChoice) => void;
}) {
  const [choices, setChoices] = useState<CatalogChoice[]>();
  const [error, setError] = useState<unknown>();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const anchor = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    const controller = new AbortController();
    const requestSignal = AbortSignal.any([controller.signal, signal]);
    setChoices(undefined); setError(undefined); setOpen(false);
    const request = kind === 'provider'
      ? listLlmProviders(target, requestSignal).then(result => result.providers
        .sort((a, b) => Number(Boolean(b.default_endpoint)) - Number(Boolean(a.default_endpoint)) || a.provider.localeCompare(b.provider)).map(item => ({
        id: item.provider, label: item.provider,
        endpoint: item.default_endpoint, protocol: item.protocol,
        detail: t(item.default_endpoint ? 'configuration.catalog.connectionTemplate' : 'configuration.catalog.manualSetup'),
      })))
      : provider ? listLlmCatalog(target, provider, requestSignal).then(result => result.models.map(item => ({
        id: item.model, label: item.model,
        detail: modelMetadataDetails(item.metadata),
      }))) : Promise.resolve([]);
    request.then(items => { if (!requestSignal.aborted) setChoices(items); }, cause => {
      if (!requestSignal.aborted) setError(cause);
    });
    return () => controller.abort();
  }, [kind, kind === 'model' ? provider : '', target.gateway_id, target.workspace_id, target.gateway_connection_id, signal]);
  const customChoice: CatalogChoice = { id: '', label: t('configuration.catalog.customProvider') };
  const items = [...(choices ?? []), ...(kind === 'provider' ? [customChoice] : [])]
    .filter(item => `${item.label} ${item.detail ?? ''}`.toLowerCase().includes(query.trim().toLowerCase()));
  const current = kind === 'provider' ? choices?.find(item => item.id === provider) : undefined;
  const label = t(kind === 'provider' ? 'configuration.catalog.chooseProvider' : 'configuration.catalog.chooseModel');
  const select = (choice: CatalogChoice) => { setOpen(false); onSelect(choice); };
  useEffect(() => {
    list.current?.querySelectorAll('[role="option"]')[active]?.scrollIntoView({ block: 'nearest' });
  }, [active, query, open]);
  return <span className="configuration-catalog-select">
    <button ref={anchor} type="button" className="configuration-catalog-trigger" aria-label={label}
      aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? id : undefined}
      disabled={disabled || (!choices && !error)} aria-busy={!choices && !error}
      onClick={() => { setQuery(''); setActive(0); setOpen(value => !value); }}>
      <span>{(custom ? customChoice.label : current?.label) || (kind === 'provider' && (provider || custom) ? provider || customChoice.label
        : t(!choices && !error ? 'configuration.catalog.loading' : 'configuration.catalog.select'))}</span>
      <span className="codicon codicon-chevron-down" aria-hidden="true" />
    </button>
    <AnchoredOverlay open={open && !disabled} anchorRef={anchor} manageFocus offset={2} onClose={() => setOpen(false)}>
      <div className="configuration-catalog-popup">
        <input role="combobox" aria-label={t('configuration.catalog.search')} placeholder={t('configuration.catalog.search')}
          aria-autocomplete="list" aria-expanded="true" aria-controls={id} aria-activedescendant={items[active] ? `${id}-${active}` : undefined}
          value={query} onChange={event => { setQuery(event.target.value); setActive(0); }} onKeyDown={event => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Home' || event.key === 'End') {
              event.preventDefault();
              setActive(index => event.key === 'Home' ? 0 : event.key === 'End' ? Math.max(0, items.length - 1)
                : Math.max(0, Math.min(items.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1))));
            } else if (event.key === 'Enter' && items[active]) { event.preventDefault(); select(items[active]); }
            else if (event.key === 'Tab') setOpen(false);
          }} />
        <div ref={list} id={id} role="listbox" aria-label={label} className="configuration-catalog-results">
          {items.map((item, index) => <div key={item.id}>
            {kind === 'provider' && !query && (index === 0 || Boolean(item.endpoint) !== Boolean(items[index - 1].endpoint) || !item.id)
              ? <div className="configuration-catalog-group" role="presentation">{t(!item.id ? 'configuration.catalog.manualGroup'
                : item.endpoint ? 'configuration.catalog.templatesGroup' : 'configuration.catalog.otherGroup')}</div> : null}
            <button type="button" tabIndex={-1} role="option" id={`${id}-${index}`} aria-selected={active === index}
              title={item.detail} onMouseMove={() => setActive(index)} onClick={() => select(item)}>
              <span>{item.label}</span>{item.detail ? <small>{item.detail}</small> : null}
            </button>
          </div>)}
          {!items.length ? <p role="status">{t('configuration.catalog.noMatches')}</p> : null}
        </div>
      </div>
    </AnchoredOverlay>
    {error ? <small className="failure-summary" role="alert">{errorSummary(error)}</small> : null}
    {current && kind === 'provider' && !current.endpoint && needsConnection ? <small>{t('configuration.catalog.manualConnection')}</small> : null}
  </span>;
}
