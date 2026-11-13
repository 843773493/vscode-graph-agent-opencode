import type { ChangeEvent } from 'react';
import { t } from '../../../../shared/core/i18n/index.js';
import type { ConfigSourceDocumentDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';

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

/** 只投影当前来源草稿及下层原文，不在客户端重建一份生效配置。 */
export function ConfigurationEntities({ kind, draft, document, selectedId, error, onSelect, onEdit,
  onCreate, onRemove, onRestoreProviders, onSetSecret, pendingSubmission, onQuerySubmission, onSetDefaultAgent, onManageSessionDefaults, onOpenSource, onOpenSessionSelection, onSave }: {
  kind: DefinitionKind; draft?: DefinitionObject; document: ConfigSourceDocumentDTO; selectedId?: string; error?: string;
  onSelect: (id: string) => void;
  onEdit: (id: string, path: (string | number)[], value: unknown) => void;
  onCreate: () => void; onRemove: (id: string) => void; onRestoreProviders: () => void;
  onSetDefaultAgent: (id: string) => void;
  onOpenSource: (sourceKey?: string) => void; onSave: () => void;
  onOpenSessionSelection?: () => void;
  onManageSessionDefaults?: () => void;
  onSetSecret?: (id: string) => void; pendingSubmission?: string; onQuerySubmission?: () => void;
}) {
  const selectedLayer = document.layers.find(layer => layer.source_key === document.source_key);
  const lower = document.layers.filter(layer => selectedLayer && layer.precedence < selectedLayer.precedence).sort((a, b) => a.precedence - b.precedence);
  const localProviders = definitionValue(draft, ['llm', 'providers']);
  const localAgents = definitionObject(draft?.agents);
  const inheritedProviders = [...lower].reverse().map(layer => ({ layer, value: definitionValue(layer.payload, ['llm', 'providers']) }))
    .find(entry => Array.isArray(entry.value));
  const inheritedAgents = new Map<string, string>();
  for (const layer of lower) for (const id of Object.keys(definitionObject(layer.payload?.agents))) {
    inheritedAgents.set(id, layer.source_key);
  }
  const providers = Array.isArray(localProviders) ? localProviders : inheritedProviders?.value as unknown[] | undefined;
  const entries: { id: string; value: DefinitionObject; local: boolean; sourceKey: string }[] = kind === 'providers'
    ? (providers ?? []).map(value => ({ id: text(definitionObject(value).id), value: definitionObject(value), local: Array.isArray(localProviders),
      sourceKey: Array.isArray(localProviders) ? document.source_key : inheritedProviders!.layer.source_key }))
    : [...new Set([...inheritedAgents.keys(), ...Object.keys(localAgents)])].map(id => ({ id,
      value: Object.hasOwn(localAgents, id) ? definitionObject(localAgents[id]) : {},
      local: Object.hasOwn(localAgents, id), sourceKey: Object.hasOwn(localAgents, id) ? document.source_key : inheritedAgents.get(id)! }));
  const selected = selectedId === undefined ? entries[0] : entries.find(entry => entry.id === selectedId);
  const editable = Boolean(document.writable && selected?.local && draft);
  const inherited = (path: (string | number)[]): unknown => {
    if (!selected) return undefined;
    for (const layer of [...lower].reverse()) {
      const value = definitionValue(layer.payload, ['agents', selected.id, ...path]);
      if (value !== undefined) return value;
    }
    return undefined;
  };
  const selectedValue = (path: (string | number)[]): unknown => kind === 'agents' && !selected?.local
    ? inherited(path) : definitionValue(selected?.value, path);
  const fallbackValue = selectedValue(['model', 'fallback_providers']);
  const inheritedFallback = inherited(['model', 'fallback_providers']);
  const configuredDefault = draft?.default_agent !== undefined ? draft.default_agent
    : [...lower].reverse().find(layer => layer.payload?.default_agent !== undefined)?.payload?.default_agent;
  const field = (label: string, path: (string | number)[], multiline = false) => {
    const value = selectedValue(path);
    const placeholder = kind === 'agents' && selected?.local && value === undefined ? text(inherited(path)) : '';
    const props = { value: text(value), placeholder, disabled: !editable,
      onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => onEdit(selected!.id, path,
        path.at(-1) === 'system_prompt_file' && !event.target.value ? null : event.target.value) };
    return <label className="configuration-entity-field">{label}
      {multiline ? <textarea rows={8} {...props} /> : <input {...props} />}
      {placeholder ? <small>{t('configuration.entities.inheritedValue', placeholder)}</small> : null}
    </label>;
  };
  const active = kind === 'providers' ? (definitionValue(document.effective, ['llm', 'providers']) as unknown[] | undefined)
    ?.find(value => definitionObject(value).id === selected?.id) : definitionValue(document.effective, ['agents', selected?.id ?? '']);
  return <section className="configuration-entities" data-definition-kind={kind}>
    <div className="configuration-entity-toolbar">
      <button type="button" disabled={!document.writable || !draft} onClick={onCreate}>{t(kind === 'providers' ? 'configuration.entities.addProvider' : 'configuration.entities.addAgent')}</button>
      {kind === 'providers' && Array.isArray(localProviders) ? <button type="button" disabled={!document.writable || !draft} onClick={onRestoreProviders}>{t('configuration.entities.restoreProviders')}</button> : null}
      <button type="button" disabled={!document.writable || !draft} onClick={onSave}>{t('configuration.entities.save')}</button>
      <button type="button" onClick={() => onOpenSource()}>{t('configuration.entities.openJsonc')}</button>
      {onOpenSessionSelection ? <button type="button" onClick={onOpenSessionSelection}>{t('configuration.entities.openSessionSelection')}</button> : null}
      {onManageSessionDefaults ? <button type="button" onClick={onManageSessionDefaults}>{t('configuration.entities.manageSessionDefaults')}</button> : null}
    </div>
    {pendingSubmission ? <p role="status">{t('settings.editor.originalSaveUnknown', pendingSubmission)} <button type="button" onClick={onQuerySubmission}>{t('configuration.entities.querySave')}</button></p> : null}
    <p>{t('configuration.entities.sourceBoundary', document.source_key)}</p>
    {selectedId !== undefined && !selected ? <p role="alert">{t('configuration.entities.definitionChanged', selectedId)}</p> : null}
    {error ? <p role="alert">{error}</p> : <div className="configuration-entity-columns">
      <nav aria-label={t(kind === 'providers' ? 'configuration.entities.providerList' : 'configuration.entities.agentList')}>
        {entries.length ? entries.map(entry => <button type="button" key={entry.id} aria-pressed={entry.id === selected?.id}
          onClick={() => onSelect(entry.id)}><strong>{entry.id}</strong><small>{entry.local ? t('configuration.entities.localDefinition') : t('configuration.entities.inheritedDefinition', entry.sourceKey)}</small></button>)
          : <p>{t('configuration.entities.empty')}</p>}
      </nav>
      {selected ? <article data-definition-id={selected.id}>
        <h2>{selected.id}</h2><p>{t('configuration.entities.stableId')}</p>
        <p>{t('configuration.entities.definitionSource', selected.sourceKey, active ? t('configuration.entities.activePresent') : t('configuration.entities.activeAbsent'))}</p>
        {!selected.local ? <button type="button" onClick={() => onOpenSource(selected.sourceKey)}>{t('configuration.entities.editOwningSource')}</button> : null}
        {kind === 'providers' ? <>
          {field(t('configuration.entities.model'), ['model'])}
          {field(t('configuration.entities.endpoint'), ['endpoint'])}
          {field(t('configuration.entities.handler'), ['custom_llm_provider'])}
          <p>{t('configuration.entities.protocol', text(definitionValue(selected.value, ['api_mode', 'protocol'])))}</p>
          <p>{t('configuration.entities.credentialState', selected.value.auth ? t('configuration.entities.oauth') : selected.value.api_key ? t('configuration.entities.credentialConfigured') : t('configuration.entities.credentialMissing'))}</p>
          <p>{t('configuration.entities.credentialsHint')}</p>
          {onSetSecret ? <button type="button" disabled={!editable || Boolean(pendingSubmission)} onClick={() => onSetSecret(selected.id)}>{t('configuration.entities.setSecret')}</button> : null}
        </> : <>
          {field(t('configuration.entities.name'), ['name'])}
          {field(t('configuration.entities.description'), ['description'], true)}
          {field(t('configuration.entities.systemPrompt'), ['instructions', 'system_prompt'], true)}
          {field(t('configuration.entities.promptFile'), ['instructions', 'system_prompt_file'])}
          {field(t('configuration.entities.primaryProvider'), ['model', 'primary_provider'])}
          <label className="configuration-entity-field">{t('configuration.entities.fallbackProviders')}
            <textarea rows={3} value={Array.isArray(fallbackValue) ? fallbackValue.join('\n') : ''}
              placeholder={Array.isArray(inheritedFallback) ? inheritedFallback.join('\n') : ''} disabled={!editable}
              onChange={event => onEdit(selected.id, ['model', 'fallback_providers'], event.target.value.split('\n'))}
              onBlur={event => {
                if (fallbackValue !== undefined) onEdit(selected.id, ['model', 'fallback_providers'], event.target.value.split('\n').map(value => value.trim()).filter(Boolean));
              }} />
          </label>
          <p>{t('configuration.entities.defaultsBoundary', text(configuredDefault))}</p>
          <button type="button" disabled={!document.writable || !draft} onClick={() => onSetDefaultAgent(selected.id)}>{t('configuration.entities.setConfiguredDefault')}</button>
        </>}
        {selected.local ? <button type="button" disabled={!editable} onClick={() => onRemove(selected.id)}>{t('configuration.entities.removeLocal')}</button> : null}
      </article> : null}
    </div>}
  </section>;
}
