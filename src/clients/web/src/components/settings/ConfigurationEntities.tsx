import type { ChangeEvent, ReactNode } from 'react';
import { t } from '../../../../shared/core/i18n/index.js';
import type { ConfigSourceDocumentDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';
import { ModelOptionFields } from './ModelOptionFields.js';
import { ModelOptionsInspection } from './ModelOptionsInspection.js';
import type { ConfigSourcePreflightDTO, ModelReferenceDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';

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
export function ConfigurationEntities({ kind, draft, document, selectedId, selectedModelId, error, onSelect, onSelectModel, onAddModel, onCopyModel, onRemoveModel, onEdit,
  onRemove, onSetSecret, pendingSubmission, onQuerySubmission, onSetDefaultAgent, onRestoreDefaultAgent,
  onInspectDefaultAgent, onManageAgentTools, onOpenSource, onConnectionCommit, renderModelProbe, draftVersion, onInspectModel }: {
  kind: DefinitionKind; draft?: DefinitionObject; document: ConfigSourceDocumentDTO; selectedId?: string; selectedModelId?: string; error?: string;
  onSelect: (id: string) => void;
  onSelectModel?: (id: string) => void;
  onAddModel?: (providerId: string) => void;
  onCopyModel?: (providerId: string, modelId: string) => void;
  onRemoveModel?: (providerId: string, modelId: string) => void;
  onEdit: (id: string, path: (string | number)[], value: unknown) => void;
  onRemove: (id: string) => void;
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
}) {
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
  const modelId = selectedModelId === undefined ? modelIds[0] : selectedModelId;
  const modelPath = modelId === undefined ? [] : ['models', modelId];
  const fallbackValue = selectedValue(['model', 'fallbacks']);
  const inheritedFallback = inherited(['model', 'fallbacks']);
  const fallbacks = Array.isArray(fallbackValue) ? fallbackValue : Array.isArray(inheritedFallback) ? inheritedFallback : [];
  const configuredDefault = draft?.default_agent !== undefined ? draft.default_agent
    : [...lower].reverse().find(layer => layer.payload?.default_agent !== undefined)?.payload?.default_agent;
  const field = (label: string, path: (string | number)[], multiline = false) => {
    const value = selectedValue(path);
    const placeholder = selected?.local && value === undefined ? text(inherited(path)) : '';
    const props = { value: text(value), placeholder, disabled: !editable,
      onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => onEdit(selected!.id, path,
        path.at(-1) === 'system_prompt_file' && !event.target.value ? null : event.target.value) };
    return <label className="configuration-entity-field">{label}
      {multiline ? <textarea rows={8} {...props} /> : <input {...props} />}
      {placeholder ? <small>{t('configuration.entities.inheritedValue', placeholder)}</small> : null}
      {editable && definitionValue(selected?.value, path) !== undefined
        ? <button type="button" onClick={() => onEdit(selected!.id, path, undefined)}>{t('configuration.entities.restoreField')}</button> : null}
    </label>;
  };
  const active = definitionValue(document.effective, [...basePath, selected?.id ?? '']);
  const optionFields = (prefix: string[]) => <ModelOptionFields
    schema={definitionObject(definitionValue(document.json_schema, ['$defs', 'modelOptions']))} disabled={!editable}
    read={path => definitionValue(selected?.value, [...prefix, ...path])} inherited={path => inherited([...prefix, ...path])}
    onEdit={(path, value) => onEdit(selected!.id, [...prefix, ...path], value)} />;
  const credential = (prefix: string[], targetModel?: string) => {
    const path = [...prefix, 'api_key'];
    const value = selectedValue(path);
    const local = definitionValue(selected?.value, path);
    return <section className="configuration-credential" data-credential-scope={targetModel === undefined ? 'provider' : 'model'}>
      <p>{t('configuration.entities.credentialsHint')}</p>
      <p>{t('settings.secrets.reference')} <code>{value === '' ? t('configuration.entities.noAuthentication') : text(value) || t('configuration.entities.inherit')}</code></p>
      {onSetSecret ? <button type="button" disabled={!editable || Boolean(pendingSubmission)}
        onClick={() => onSetSecret(selected!.id, targetModel)}>{t('configuration.entities.setSecret')}</button> : null}
      {local !== undefined ? <button type="button" disabled={!editable || Boolean(pendingSubmission)}
        onClick={() => onEdit(selected!.id, path, undefined)}>{t('configuration.entities.restoreField')}</button> : null}
    </section>;
  };
  return <section className="configuration-entity-content" data-definition-kind={kind}>
    {pendingSubmission || (selectedId !== undefined && !selected) ? <div className="configuration-entity-feedback">
      {pendingSubmission ? <p role="status">{t('settings.editor.originalSaveUnknown', pendingSubmission)} <button type="button" onClick={onQuerySubmission}>{t('configuration.entities.querySave')}</button></p> : null}
      {selectedId !== undefined && !selected ? <p role="alert">{t('configuration.entities.definitionChanged', selectedId)}</p> : null}
    </div> : null}
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
          {field(t('configuration.entities.name'), ['name'])}
          <h3>{t('configuration.entities.providerDefaults')}</h3>
          <div onBlur={event => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) onConnectionCommit?.(selected.id);
          }}>
            {field(t('configuration.entities.endpoint'), ['defaults', 'endpoint'])}
            {field(t('configuration.entities.handler'), ['defaults', 'custom_llm_provider'])}
            {optionFields(['defaults'])}
            {credential(['defaults'])}
          </div>
          <h3>{t('configuration.entities.models')}</h3>
          <nav aria-label={t('configuration.entities.models')}>
            {modelIds.map(id => <button type="button" key={id} aria-pressed={id === modelId} onClick={() => onSelectModel?.(id)}>
              <strong>{id}</strong><small>{Object.hasOwn(localModels, id) ? t('configuration.entities.localDefinition') : t('configuration.entities.inheritedDefinition', modelSources.get(id)!)}</small>
            </button>)}
          </nav>
          {onAddModel ? <button type="button" disabled={!editable} onClick={() => onAddModel(selected.id)}>{t('configuration.entities.addModel')}</button> : null}
          {modelId !== undefined && modelIds.includes(modelId) ? <section data-model-id={modelId}>
            <h3>{modelId}</h3>
            {field(t('configuration.entities.name'), [...modelPath, 'name'])}
            {field(t('configuration.entities.model'), [...modelPath, 'model'])}
            <h4>{t('configuration.entities.modelOverrides')}</h4>
            {field(t('configuration.entities.endpoint'), [...modelPath, 'overrides', 'endpoint'])}
            {optionFields([...modelPath, 'overrides'])}
            {credential([...modelPath, 'overrides'], modelId)}
            {onInspectModel && draftVersion !== undefined ? <ModelOptionsInspection version={draftVersion}
              modelRef={{ provider_id: selected.id, model_id: modelId }} inspect={onInspectModel} onOpenSource={onOpenSource} /> : null}
            {onCopyModel ? <button type="button" disabled={!editable} onClick={() => onCopyModel(selected.id, modelId)}>{t('configuration.entities.copyModel')}</button> : null}
            {onRemoveModel && Object.hasOwn(localModels, modelId) ? <button type="button" disabled={!editable} onClick={() => onRemoveModel(selected.id, modelId)}>{t('configuration.entities.removeLocal')}</button> : null}
          </section> : selectedModelId !== undefined ? <p role="alert">{t('configuration.entities.definitionChanged', selectedModelId)}</p> : null}
          {renderModelProbe?.(selected.id, modelIds.map(id => ({ id,
            model: text(selectedValue(['models', id, 'model']) ?? inherited(['models', id, 'model'])),
          })), modelIds.includes(modelId ?? '') ? modelId : undefined)}
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
        {selected.local ? <button type="button" disabled={!editable} onClick={() => onRemove(selected.id)}>{t('configuration.entities.removeLocal')}</button> : null}
      </article> : null}
    </div>}
  </section>;
}
