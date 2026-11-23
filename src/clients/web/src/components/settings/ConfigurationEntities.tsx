import { useEffect, useId, useRef, useState, type ChangeEvent, type ReactNode } from 'react';
import { t } from '../../../../shared/core/i18n/index.js';
import type { ConfigSourceDocumentDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';
import { ModelOptionFields } from './ModelOptionFields.js';
import { ModelOptionsInspection } from './ModelOptionsInspection.js';
import type { ConfigSourcePreflightDTO, ModelReferenceDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';
import type { GatewayWorkspaceRequestTarget } from '../../api/workspaceIdentity.js';
import { ModelCatalogSelect, type CatalogChoice } from './ModelCatalogSelect.js';

export type DefinitionKind = 'providers' | 'agents';
export type DefinitionObject = Record<string, unknown>;
export function definitionObject(value: unknown): DefinitionObject {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as DefinitionObject : {};
}
export function definitionValue(value: unknown, path: readonly (string | number)[]): unknown {
  for (const key of path) {
    if (!value || typeof value !== 'object') return undefined;
    value = (value as Record<string | number, unknown>)[key];
  }
  return value;
}
const text = (value: unknown): string => typeof value === 'string' ? value : '';

function AddModel({ disabled, catalog, onAdd }: { disabled: boolean; catalog: (select: (model: string) => void) => ReactNode;
  onAdd: (model: string) => void }) {
  const [model, setModel] = useState('');
  return <form className="configuration-add-model" onSubmit={event => {
    event.preventDefault(); if (model.trim()) { onAdd(model.trim()); setModel(''); }
  }}>
    <label className="configuration-entity-field">{t('configuration.catalog.newModel')}
      <input disabled={disabled} value={model} onChange={event => setModel(event.target.value)} />
    </label>
    {catalog(setModel)}
    <button type="submit" disabled={disabled || !model.trim()}>{t('configuration.entities.addModel')}</button>
  </form>;
}

/** 只投影当前来源草稿及下层原文，不在客户端重建一份生效配置。 */
export function ConfigurationEntities({ kind, draft, document, selectedId, selectedModelId, error, onSelect, onSelectModel, onAddModel, onShowActions, onEdit,
  onSetSecret, pendingSubmission, onQuerySubmission, onSetDefaultAgent, onRestoreDefaultAgent,
  onInspectDefaultAgent, onManageAgentTools, onOpenSource, onConnectionCommit, renderModelProbe, draftVersion, onInspectModel, catalogTarget, catalogSignal, onProviderTemplate, searchQuery = '', credentialNames = {}, dirty = false }: {
  searchQuery?: string; credentialNames?: Record<string, string>; dirty?: boolean;
  kind: DefinitionKind; draft?: DefinitionObject; document: ConfigSourceDocumentDTO; selectedId?: string; selectedModelId?: string; error?: string;
  onSelect: (id: string) => void;
  onSelectModel?: (id: string) => void;
  onAddModel?: (providerId: string, model: string) => void;
  onShowActions?: (id: string, modelId: string | undefined, anchor: HTMLElement) => void;
  onEdit: (id: string, path: (string | number)[], value: unknown) => void;
  onSetDefaultAgent: (id: string) => void;
  onRestoreDefaultAgent?: () => void;
  onInspectDefaultAgent?: () => void;
  onOpenSource: (sourceKey?: string) => void;
  onManageAgentTools?: (id: string) => void;
  onConnectionCommit?: (id: string) => void;
  renderModelProbe?: (id: string, models: { id: string; model: string }[], selectedModelId?: string) => ReactNode;
  onSetSecret?: (id: string, modelId?: string) => void; pendingSubmission?: string; onQuerySubmission?: () => void;
  draftVersion?: number;
  onInspectModel?: (reference: ModelReferenceDTO, signal: AbortSignal) => Promise<ConfigSourcePreflightDTO>;
  catalogTarget?: GatewayWorkspaceRequestTarget; catalogSignal?: AbortSignal;
  onProviderTemplate?: (id: string, choice: CatalogChoice) => void;
}) {
  const root = useRef<HTMLElement>(null);
  const formId = useId();
  const [searchMatches, setSearchMatches] = useState(0);
  const [customProvider, setCustomProvider] = useState<string>();
  const [expandedProvider, setExpandedProvider] = useState<string>();
  const [modelSearch, setModelSearch] = useState('');
  const [providerSearch, setProviderSearch] = useState('');
  const selectedLayer = document.layers.find(layer => layer.source_key === document.source_key);
  const lower = document.layers.filter(layer => selectedLayer && layer.precedence < selectedLayer.precedence).sort((a, b) => a.precedence - b.precedence);
  const basePath = kind === 'providers' ? ['llm', 'providers'] : ['agents'];
  const localDefinitions = definitionObject(definitionValue(draft, basePath));
  // 这里只枚举身份和原文位置；有效模型选项仍由 Backend resolver 计算。
  const inheritedDefinitions = new Map<string, string>();
  for (const layer of lower) for (const id of Object.keys(definitionObject(definitionValue(layer.payload, basePath)))) {
    inheritedDefinitions.set(id, layer.source_key);
  }
  const entries = [...new Set([...inheritedDefinitions.keys(), ...Object.keys(localDefinitions)])].map(id => ({
    id, value: definitionObject(localDefinitions[id]), local: Object.hasOwn(localDefinitions, id),
    sourceKey: Object.hasOwn(localDefinitions, id) ? document.source_key : inheritedDefinitions.get(id)!,
  }));
  const selected = selectedId === undefined ? entries[0] : entries.find(entry => entry.id === selectedId);
  const entryName = (entry: typeof entries[number]) => text(entry.value.name) || text([...lower].reverse()
    .map(layer => definitionValue(layer.payload, [...basePath, entry.id, 'name'])).find(value => value !== undefined)) || entry.id;
  const visibleEntries = entries.filter(entry => kind !== 'providers'
    || `${entryName(entry)} ${entry.id}`.toLowerCase().includes(providerSearch.toLowerCase()));
  const editable = Boolean(document.writable && selected && draft);
  const inherited = (path: (string | number)[]): unknown => {
    if (!selected) return undefined;
    for (const layer of [...lower].reverse()) {
      const value = definitionValue(layer.payload, [...basePath, selected.id, ...path]);
      if (value !== undefined) return value;
    }
    return undefined;
  };
  const selectedValue = (path: (string | number)[]): unknown => selected?.local
    ? definitionValue(selected.value, path) : inherited(path);
  const localModels = definitionObject(definitionValue(selected?.value, ['models']));
  const modelSources = new Map<string, string>();
  if (selected) for (const layer of lower) for (const id of Object.keys(definitionObject(
    definitionValue(layer.payload, ['llm', 'providers', selected.id, 'models'])))) modelSources.set(id, layer.source_key);
  const modelIds = [...new Set([...modelSources.keys(), ...Object.keys(localModels)])];
  const visibleModels = modelIds.filter(id => `${id} ${text(selectedValue(['models', id, 'name']) ?? inherited(['models', id, 'name']))} ${text(selectedValue(['models', id, 'model']) ?? inherited(['models', id, 'model']))}`
    .toLowerCase().includes(modelSearch.toLowerCase()));
  const modelId = selectedModelId === undefined ? modelIds[0] : selectedModelId;
  const modelPath = modelId === undefined ? [] : ['models', modelId];
  const modelTitle = text(selectedValue([...modelPath, 'model']) ?? inherited([...modelPath, 'model'])) || modelId || '';
  const fallbackValue = selectedValue(['model', 'fallbacks']);
  const inheritedFallback = inherited(['model', 'fallbacks']);
  const fallbacks = Array.isArray(fallbackValue) ? fallbackValue : Array.isArray(inheritedFallback) ? inheritedFallback : [];
  const configuredDefault = draft?.default_agent !== undefined ? draft.default_agent
    : [...lower].reverse().find(layer => layer.payload?.default_agent !== undefined)?.payload?.default_agent;
  useEffect(() => {
    if (!searchQuery.trim() || !root.current) { setSearchMatches(0); return; }
    const tokens = searchQuery.toLowerCase().trim().split(/\s+/);
    const opened = new Set<HTMLDetailsElement>();
    const matches = [...root.current.querySelectorAll<HTMLElement>('.configuration-entity-field, [data-parameter], .configuration-credential')].filter(element => {
      if (element.closest('[data-parameter]') && !element.hasAttribute('data-parameter')) return false;
      const inputs = [...element.querySelectorAll<HTMLInputElement>('input,textarea,select')].map(input => input.value).join(' ');
      const text = `${element.textContent} ${element.dataset.searchKey ?? ''} ${element.dataset.parameter ?? ''} ${inputs}`.toLowerCase();
      return tokens.every(token => text.includes(token));
    });
    for (const match of matches) {
      match.classList.add('configuration-search-match');
      for (let parent = match.parentElement; parent && parent !== root.current; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS' && !(parent as HTMLDetailsElement).open) {
          (parent as HTMLDetailsElement).open = true; opened.add(parent as HTMLDetailsElement);
        }
      }
    }
    setSearchMatches(matches.length);
    matches[0]?.scrollIntoView({ block: 'nearest' });
    return () => { for (const match of matches) match.classList.remove('configuration-search-match'); for (const details of opened) details.open = false; };
  }, [searchQuery, selected?.id, modelId, draftVersion]);
  const field = (label: string, path: (string | number)[], multiline = false) => {
    const value = selectedValue(path);
    const placeholder = selected?.local && value === undefined ? text(inherited(path)) : '';
    const props = { value: text(value), placeholder, disabled: !editable,
      onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => onEdit(selected!.id, path,
        path.at(-1) === 'system_prompt_file' && !event.target.value ? null
          : path.at(-1) === 'name' && !event.target.value ? undefined : event.target.value) };
    const inputId = `${formId}-${path.join('.')}`;
    return <div className="configuration-entity-field" data-search-key={path.join('.')}>
      <label htmlFor={inputId}>{label}</label>
      <div className="configuration-field-control">
      {multiline ? <textarea id={inputId} rows={8} {...props} /> : <input id={inputId} {...props} />}
      {editable && definitionValue(selected?.value, path) !== undefined && inherited(path) !== undefined
        ? <button className="configuration-field-reset" type="button" onClick={() => onEdit(selected!.id, path, undefined)}>{t('configuration.entities.restoreField')}</button> : null}
      </div>
      {placeholder ? <small>{t('configuration.entities.inheritedValue', placeholder)}</small> : null}
    </div>;
  };
  const active = definitionValue(document.effective, [...basePath, selected?.id ?? '']);
  const optionFields = (prefix: string[]) => <ModelOptionFields
    scope={prefix[0] === 'models' ? 'model' : 'provider'}
    schema={definitionObject(definitionValue(document.json_schema, ['$defs', 'modelOptions']))} disabled={!editable}
    read={path => definitionValue(selected?.value, [...prefix, ...path])} inherited={path => inherited([...prefix, ...path])}
    onEdit={(path, value) => onEdit(selected!.id, [...prefix, ...path], value)} />;
  const credential = (prefix: string[], targetModel?: string) => {
    const path = [...prefix, 'api_key'];
    const value = selectedValue(path);
    const local = definitionValue(selected?.value, path);
    const reference = text(value) || text(inherited(path));
    const auth = definitionObject(selectedValue([...prefix, 'auth']) ?? inherited([...prefix, 'auth']));
    const oauth = auth.type === 'oauth';
    const env = /^(?:\$\{(.+)\}|env:(.+))$/.exec(reference);
    const label = oauth ? t('configuration.entities.oauth') : value === '' ? t('configuration.entities.noAuthentication')
      : reference.startsWith('secret:') ? t('configuration.credentials.named', credentialNames[reference.slice(7)] ?? t('configuration.credentials.unavailable'))
      : env ? t('configuration.credentials.environment', env[1] ?? env[2])
      : reference || t(targetModel ? 'configuration.entities.useServiceDefault' : 'configuration.entities.credentialMissing');
    return <section className="configuration-credential" data-credential-scope={targetModel === undefined ? 'provider' : 'model'}>
      <h4>{t(oauth ? 'configuration.entities.authentication' : 'configuration.entities.apiKey')}</h4>
      <div className="configuration-credential-controls">
      <p>{label}</p>
      {onSetSecret && !oauth ? <button type="button" disabled={!editable || Boolean(pendingSubmission)}
        onClick={() => onSetSecret(selected!.id, targetModel)}>{t('configuration.entities.setSecret')}</button> : null}
      {!oauth && local !== undefined && (local !== '' || inherited(path) !== undefined || targetModel !== undefined) ? <button type="button" disabled={!editable || Boolean(pendingSubmission)}
        onClick={() => onEdit(selected!.id, path, inherited(path) !== undefined || targetModel !== undefined ? undefined : '')}>{t(inherited(path) !== undefined ? 'configuration.entities.restoreField'
          : targetModel !== undefined ? 'configuration.entities.useServiceDefault' : 'configuration.entities.clearKey')}</button> : null}
      </div>
    </section>;
  };
  const provider = text(selectedValue(['defaults', 'custom_llm_provider']) ?? inherited(['defaults', 'custom_llm_provider']));
  const catalog = (kind: 'provider' | 'model', onSelect: (choice: CatalogChoice) => void) => catalogTarget && catalogSignal
    ? <ModelCatalogSelect key={`${kind}:${kind === 'model' ? provider : ''}`} kind={kind} provider={provider}
      target={catalogTarget} signal={catalogSignal} disabled={!editable}
      custom={kind === 'provider' && customProvider === selected?.id}
      needsConnection={kind === 'provider' && !text(selectedValue(['defaults', 'endpoint']) ?? inherited(['defaults', 'endpoint']))}
      onSelect={onSelect} /> : null;
  return <section ref={root} className="configuration-entity-content" data-definition-kind={kind}>
    {searchQuery ? <p role="status" className="configuration-search-status">{t('configuration.search.matches', searchMatches)}</p> : null}
    {pendingSubmission || (selectedId !== undefined && !selected) ? <div className="configuration-entity-feedback">
      {pendingSubmission ? <p role="status">{t('settings.editor.originalSaveUnknown', pendingSubmission)} <button type="button" onClick={onQuerySubmission}>{t('configuration.entities.querySave')}</button></p> : null}
      {selectedId !== undefined && !selected ? <p role="alert">{t('configuration.entities.definitionChanged', selectedId)}</p> : null}
    </div> : null}
    {error ? <p role="alert">{error}</p> : <div className="configuration-entity-columns">
      <div className="configuration-definition-selector">
      {kind === 'providers' && (entries.length > 6 || providerSearch) ? <label className="configuration-entity-field configuration-provider-search">
        <input type="search" aria-label={t('configuration.catalog.searchConfigured')} placeholder={t('configuration.catalog.searchConfigured')}
          value={providerSearch} onChange={event => setProviderSearch(event.target.value)} />
      </label> : null}
      <nav aria-label={t(kind === 'providers' ? 'configuration.entities.providerList' : 'configuration.entities.agentList')}>
        {entries.length ? visibleEntries.map(entry => <button type="button" key={entry.id} aria-pressed={entry.id === selected?.id}
          title={entryName(entry)} onClick={() => { setModelSearch(''); onSelect(entry.id); }}><strong>{entryName(entry)}</strong></button>)
          : <p>{t('configuration.entities.empty')}</p>}
        {entries.length > 0 && !visibleEntries.length ? <p role="status">{t('configuration.catalog.noMatches')}</p> : null}
      </nav>
      </div>
      {selected ? <article key={selected.id} data-definition-id={selected.id}>
        <div className="configuration-definition-heading">
        <h2 title={entryName(selected)}>{entryName(selected)}</h2><span className="configuration-draft-status">{t(dirty ? 'configuration.status.unsaved' : 'configuration.status.saved')}</span>
        <details className="configuration-definition-details"><summary>{t('configuration.entities.details')}</summary>
          <p>ID: <code>{selected.id}</code></p>
          <p>{t('configuration.entities.definitionSource', selected.sourceKey, active ? t('configuration.entities.activePresent') : t('configuration.entities.activeAbsent'))}</p>
          {!selected.local ? <p>{t('configuration.entities.inheritedDefinition', selected.sourceKey)}</p> : null}
          {!selected.local ? <button type="button" onClick={() => onOpenSource(selected.sourceKey)}>{t('configuration.entities.editOwningSource')}</button> : null}
        </details>
        {selected.local && onShowActions ? <button type="button" className="configuration-actions-button"
          aria-label={t('configuration.entities.definitionActions', entryName(selected))} title={t('configuration.entities.definitionActions', entryName(selected))}
          aria-haspopup="menu" onClick={event => onShowActions(selected.id, undefined, event.currentTarget)}>
          <span className="codicon codicon-more" aria-hidden="true" />
        </button> : null}
        </div>
        {kind === 'providers' ? <>
          <section className="configuration-connection-section" aria-label={t('configuration.catalog.connection')} onBlur={event => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) onConnectionCommit?.(selected.id);
          }}>
            <h3>{t('configuration.catalog.connection')}</h3>
            <div className="configuration-form-grid">
            <div className="configuration-entity-field configuration-provider-field"><span>{t('configuration.catalog.provider')}</span>
              {catalog('provider', choice => {
                setExpandedProvider(choice.endpoint ? undefined : selected.id);
                setCustomProvider(choice.id ? undefined : selected.id);
                onProviderTemplate?.(selected.id, choice);
              })}
            </div>
            {field(t('configuration.entities.name'), ['name'])}
            <div className="configuration-field-wide">{field(t('configuration.entities.endpoint'), ['defaults', 'endpoint'])}</div>
            </div>
            {credential(['defaults'])}
            <details className="configuration-provider-connection" open={expandedProvider === selected.id}
              onToggle={event => { const open = event.currentTarget.open;
                setExpandedProvider(current => open ? selected.id : current === selected.id ? undefined : current); }}>
              <summary>{t('configuration.catalog.connectionSettings')}</summary>
              <div className="configuration-form-grid">
              {field(t('configuration.entities.handler'), ['defaults', 'custom_llm_provider'])}
              </div>
              {optionFields(['defaults'])}
            </details>
          </section>
          <section className="configuration-models-section">
          <div className="configuration-section-heading"><h3>{t('configuration.entities.models')}</h3>
            {modelIds.length > 6 || modelSearch ? <input type="search" aria-label={t('configuration.catalog.searchModels')} placeholder={t('configuration.catalog.searchModels')}
              value={modelSearch} onChange={event => setModelSearch(event.target.value)} /> : null}
          </div>
          {renderModelProbe?.(selected.id, modelIds.map(id => ({ id,
            model: text(selectedValue(['models', id, 'model']) ?? inherited(['models', id, 'model'])),
          })), modelIds.includes(modelId ?? '') ? modelId : undefined)}
          <nav aria-label={t('configuration.entities.models')} className="configuration-model-list">
            {visibleModels.map(id => {
              const name = text(selectedValue(['models', id, 'name']) ?? inherited(['models', id, 'name']));
              const upstream = text(selectedValue(['models', id, 'model']) ?? inherited(['models', id, 'model'])) || id;
              return <button type="button" key={id} aria-pressed={id === modelId} title={name && name !== upstream ? `${name}\n${upstream}` : upstream} onClick={() => onSelectModel?.(id)}>
                <strong>{name || upstream}</strong>{name && name !== upstream ? <span>{upstream}</span> : null}
                <span className="codicon codicon-chevron-right" aria-hidden="true" />
              </button>;
            })}
            {modelIds.length > 0 && !visibleModels.length ? <p role="status">{t('configuration.catalog.noMatches')}</p> : null}
          </nav>
          {onAddModel ? <details className="configuration-model-add"><summary>{t('configuration.catalog.addModel')}</summary>
            <AddModel key={selected.id} disabled={!editable} onAdd={model => onAddModel(selected.id, model)}
              catalog={select => catalog('model', choice => select(choice.id))} />
          </details> : null}
          {!modelIds.length ? <p>{t('configuration.entities.noModels')}</p> : null}
          {modelId !== undefined && modelIds.includes(modelId) ? <section key={modelId} data-model-id={modelId} className="configuration-model-detail">
            <div className="configuration-section-heading">
            <h4 title={modelTitle}>{t('configuration.catalog.selectedModel', modelTitle)}</h4>
            {onShowActions ? <button type="button" className="configuration-actions-button"
              aria-label={t('configuration.entities.modelActions', modelTitle)} title={t('configuration.entities.modelActions', modelTitle)}
              aria-haspopup="menu" onClick={event => onShowActions(selected.id, modelId, event.currentTarget)}>
              <span className="codicon codicon-more" aria-hidden="true" />
            </button> : null}
            </div>
            <div className="configuration-form-grid">
            {field(t('configuration.entities.name'), [...modelPath, 'name'])}
            {field(t('configuration.entities.model'), [...modelPath, 'model'])}
            </div>
            <details className="configuration-model-overrides"><summary>{t('configuration.entities.modelOverrides')}</summary>
            <p>{t('configuration.entities.modelDefaultsHint')}</p>
            {field(t('configuration.entities.endpoint'), [...modelPath, 'overrides', 'endpoint'])}
            {optionFields([...modelPath, 'overrides'])}
            {credential([...modelPath, 'overrides'], modelId)}
            {onInspectModel && draftVersion !== undefined ? <ModelOptionsInspection version={draftVersion}
              modelRef={{ provider_id: selected.id, model_id: modelId }} inspect={onInspectModel} onOpenSource={onOpenSource} /> : null}
            </details>
          </section> : selectedModelId !== undefined ? <p role="alert">{t('configuration.entities.definitionChanged', selectedModelId)}</p> : null}
          </section>

        </> : <>
          {field(t('configuration.entities.name'), ['name'])}
          {field(t('configuration.entities.description'), ['description'], true)}
          {field(t('configuration.entities.systemPrompt'), ['instructions', 'system_prompt'], true)}
          {field(t('configuration.entities.promptFile'), ['instructions', 'system_prompt_file'])}
          <h3>{t('configuration.entities.primaryModel')}</h3>
          {field(t('configuration.entities.providerId'), ['model', 'primary', 'provider_id'])}
          {field(t('configuration.entities.modelId'), ['model', 'primary', 'model_id'])}
          <h3>{t('configuration.entities.fallbackModels')}</h3>
          {fallbacks.map((value, index) => <div key={index} className="configuration-model-reference">
            {(['provider_id', 'model_id'] as const).map(key => <label className="configuration-entity-field" key={key}>
              {t(key === 'provider_id' ? 'configuration.entities.providerId' : 'configuration.entities.modelId')}
              <input value={text(definitionObject(value)[key])} disabled={!editable} onChange={event => onEdit(selected.id, ['model', 'fallbacks'],
                fallbacks.map((entry, offset) => offset === index ? { ...definitionObject(entry), [key]: event.target.value } : entry))} />
            </label>)}
            <button type="button" disabled={!editable} onClick={() => onEdit(selected.id, ['model', 'fallbacks'], fallbacks.filter((_, offset) => offset !== index))}>{t('configuration.entities.removeFallback')}</button>
          </div>)}
          {fallbackValue === undefined && fallbacks.length ? <small>{t('configuration.entities.inheritedValue', JSON.stringify(fallbacks))}</small> : null}
          <button type="button" disabled={!editable} onClick={() => onEdit(selected.id, ['model', 'fallbacks'], [...fallbacks, { provider_id: '', model_id: '' }])}>{t('configuration.entities.addFallback')}</button>
          {fallbackValue !== undefined ? <button type="button" disabled={!editable} onClick={() => onEdit(selected.id, ['model', 'fallbacks'], undefined)}>{t('configuration.entities.restoreField')}</button> : null}
          <p>{t('configuration.entities.defaultsBoundary', text(configuredDefault))}</p>
          <button type="button" disabled={!document.writable || !draft} onClick={() => onSetDefaultAgent(selected.id)}>{t('configuration.entities.setConfiguredDefault')}</button>
          {draft?.default_agent !== undefined && onRestoreDefaultAgent
            ? <button type="button" disabled={!document.writable || !draft} onClick={onRestoreDefaultAgent}>{t('configuration.entities.restoreDefaultAgent')}</button> : null}
          {onInspectDefaultAgent ? <button type="button" onClick={onInspectDefaultAgent}>{t('configuration.entities.inspectDefaultAgent')}</button> : null}
          {onManageAgentTools ? <button type="button" disabled={!active} onClick={() => onManageAgentTools(selected.id)}>{t('configuration.entities.manageAgentTools')}</button> : null}
        </>}
      </article> : null}
    </div>}
  </section>;
}
