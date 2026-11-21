import { useEffect, useRef, useState } from 'react';
import { t } from '../../../../shared/core/i18n/index.js';
import { discoverLlmModels, getConfigSourceDocument, testLlmModel } from '../../api/configSources.js';
import { observeSecretMetadata, SecretsClient } from '../../api/secrets.js';
import type { GatewaySecretsDTO } from '../../types/protocol_generated/boxteam/gateway/v2/secrets.js';
import type { GatewayWorkspaceRequestTarget } from '../../api/workspaceIdentity.js';
import { ConfigSourceBaselineDTO, type ConfigSourceDocumentDTO, type LlmDiscoverRequest, type LlmDiscoverResultDTO,
  type LlmDiscoveredModelDTO, type LlmProbeObservationDTO, type LlmTestResultDTO, type LlmTestStartDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';

export interface ModelProbeEditor {
  target: GatewayWorkspaceRequestTarget;
  jsonc: string;
  version: number;
  dirty: boolean;
  signal: AbortSignal;
  connectionCommit: number;
  autoDiscover: boolean;
  onAutoDiscover: (enabled: boolean) => void;
}

interface TestObservation {
  modelId: string;
  mode: 'configured' | 'stream' | 'complete';
  status: 'running' | 'passed' | 'failed' | 'cancelled';
  version: number;
  preview: string;
  start?: LlmTestStartDTO;
  result?: LlmTestResultDTO;
  error?: string;
}

function baselineKey(document: ConfigSourceDocumentDTO | LlmProbeObservationDTO): string {
  return JSON.stringify(Object.entries(document.source_baseline).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) => [key, ConfigSourceBaselineDTO.toJSON(ConfigSourceBaselineDTO.fromJSON(value))]));
}

/** 只保留本表单的临时观察；配置修改仍交回同一个来源文本模型。 */
export function ModelConnectionProbe({ providerId, models, selectedModelId, document, editor, onImport, onApplyMode, onOpenSource }: {
  providerId: string; models: { id: string; model: string }[]; selectedModelId?: string;
  document: ConfigSourceDocumentDTO; editor: ModelProbeEditor;
  onImport: (models: LlmDiscoveredModelDTO[], version: number) => void;
  onApplyMode: (modelId: string, streaming: boolean) => void;
  onOpenSource?: () => void;
}) {
  const [observedDocument, setObservedDocument] = useState(document);
  const [knownSecrets, setKnownSecrets] = useState<GatewaySecretsDTO>();
  const [mode, setMode] = useState<'draft' | 'active'>();
  const [secret, setSecret] = useState('');
  const [secretScope, setSecretScope] = useState<'provider' | 'model'>('provider');
  const [secretModelId, setSecretModelId] = useState<string>();
  const [secretVersion, setSecretVersion] = useState(0);
  const revision = useRef({ stamp: '', value: 0 });
  const stamp = `${editor.version}:${secretVersion}`;
  if (revision.current.stamp !== stamp) revision.current = { stamp, value: revision.current.value + 1 };
  const version = revision.current.value;
  const [catalog, setCatalog] = useState<{ result: LlmDiscoverResultDTO; version: number }>();
  const [discovering, setDiscovering] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string>();
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [testSelection, setTestSelection] = useState<Set<string>>(new Set());
  const [tests, setTests] = useState<Record<string, TestObservation>>({});
  const [testing, setTesting] = useState(false);
  const discovery = useRef<AbortController>();
  const batch = useRef<AbortController>();
  const versionRead = useRef<AbortController>();
  const committed = useRef(editor.connectionCommit);
  const secretEdited = useRef(false);
  const actualMode = mode ?? (editor.dirty || secret ? 'draft' : 'active');
  const failureText = (cause: unknown) => cause instanceof Error ? cause.message : String(cause);
  const changeSecret = (value: string) => { setSecret(value); setSecretModelId(selectedModelId); setSecretVersion(value => value + 1); };

  useEffect(() => { setObservedDocument(document); }, [document]);
  useEffect(() => {
    setKnownSecrets(undefined);
    return observeSecretMetadata(editor.target.gateway_id, snapshot => setKnownSecrets(previous => {
      if ('items' in snapshot) return snapshot;
      const created = snapshot.item;
      if (!previous || !created) return previous;
      return { ...previous, items: [...previous.items.filter(item => item.id !== created.id), created] };
    }));
  }, [editor.target.gateway_id]);
  useEffect(() => {
    // 编辑使旧目录失效；测试保留其冻结观察，按版本明确显示过期。
    discovery.current?.abort();
  }, [version]);
  useEffect(() => {
    if (secretScope === 'model' && secretModelId !== selectedModelId) changeSecret('');
  }, [selectedModelId]);
  useEffect(() => {
    const stop = () => { discovery.current?.abort(); batch.current?.abort(); versionRead.current?.abort(); };
    const invalidate = () => { stop(); changeSecret(''); };
    editor.signal.addEventListener('abort', invalidate, { once: true });
    if (editor.signal.aborted) invalidate();
    return () => { editor.signal.removeEventListener('abort', invalidate); stop(); };
  }, [editor.signal]);

  const request = (inputMode: 'draft' | 'active'): LlmDiscoverRequest => {
    editor.signal.throwIfAborted();
    if (inputMode === 'active' && secret) throw new Error(t('configuration.probe.activeSecret'));
    const common = { gateway_id: editor.target.gateway_id, workspace_id: editor.target.workspace_id,
      mode: inputMode, provider_id: providerId, edit_version: version };
    if (inputMode === 'draft') return { ...common, source_key: document.source_key, jsonc: editor.jsonc,
      expected_source_baseline: observedDocument.source_baseline,
      ...(secret ? { credential_input: { scope: secretScope, provider_id: providerId,
        ...(secretScope === 'model' ? { model_id: secretModelId } : {}), value: secret } } : {}) };
    if (typeof observedDocument.active_revision !== 'number' || typeof observedDocument.active_digest !== 'string') {
      throw new Error(t('configuration.probe.activeMissing'));
    }
    return { ...common, expected_source_baseline: {}, expected_active_revision: observedDocument.active_revision,
      expected_active_digest: observedDocument.active_digest };
  };
  const stale = (observation: LlmProbeObservationDTO | undefined, resultVersion: number): boolean => {
    if (resultVersion !== version) return true;
    if (!observation) return false;
    const credential = observation.credential;
    if (credential?.source === 'vault' && knownSecrets?.gateway_id === observation.gateway_id
      && knownSecrets.items.find(item => item.id === credential.secret_id)?.revision !== credential.revision) return true;
    return observation.mode === 'draft' ? baselineKey(observation) !== baselineKey(observedDocument)
      : observation.active_revision !== observedDocument.active_revision || observation.active_digest !== observedDocument.active_digest;
  };
  const discover = async (inputMode = actualMode) => {
    discovery.current?.abort();
    const controller = new AbortController(); discovery.current = controller;
    setDiscovering(true); setError(undefined);
    try {
      const result = await discoverLlmModels(0, editor.target, request(inputMode), controller.signal);
      if (controller.signal.aborted || discovery.current !== controller || revision.current.value !== version) return;
      setCatalog({ result, version }); setSelected(new Set());
    } catch (cause) {
      if (!controller.signal.aborted) setError(failureText(cause));
    } finally {
      if (discovery.current === controller) { discovery.current = undefined; setDiscovering(false); }
    }
  };
  useEffect(() => {
    if (committed.current === editor.connectionCommit) return;
    committed.current = editor.connectionCommit;
    if (editor.autoDiscover) void discover('draft');
  }, [editor.connectionCommit]);

  const refreshVersions = async () => {
    versionRead.current?.abort();
    const controller = new AbortController(); versionRead.current = controller;
    setRefreshing(true); setError(undefined);
    try {
      const usesVault = catalog?.result.observation?.credential?.source === 'vault'
        || Object.values(tests).some(entry => (entry.result?.observation ?? entry.start?.observation)?.credential?.source === 'vault');
      const [current] = await Promise.all([
        getConfigSourceDocument(0, editor.target, document.source_key, { signal: controller.signal }),
        usesVault ? new SecretsClient(editor.target, controller.signal).list() : undefined,
      ]);
      if (!controller.signal.aborted) setObservedDocument(current);
    } catch (cause) { if (!controller.signal.aborted) setError(failureText(cause)); }
    finally { if (versionRead.current === controller) { versionRead.current = undefined; setRefreshing(false); } }
  };
  const runTests = async (ids: string[], testMode: TestObservation['mode'] | 'both') => {
    if (!ids.length || batch.current) return;
    const controller = new AbortController(); batch.current = controller;
    setTesting(true); setError(undefined);
    try {
      // 一次点击冻结全部输入，最多三个消费者；停止后不再领取剩余模型。
      const input = request(actualMode);
      const modes = testMode === 'both' ? ['stream', 'complete'] as const : [testMode];
      const queue = ids.flatMap(modelId => modes.map(mode => ({ modelId, mode })));
      let cursor = 0;
      const worker = async () => {
        while (!controller.signal.aborted && cursor < queue.length) {
          const { modelId, mode } = queue[cursor++], key = `${modelId}:${mode}`;
          const entry: TestObservation = { modelId, mode, status: 'running', version, preview: '' };
          const update = () => { if (batch.current === controller) setTests(previous => ({ ...previous, [key]: { ...entry } })); };
          update();
          try {
            const result = await testLlmModel(0, editor.target, { ...input, model_id: modelId, timeout_seconds: 60,
              ...(mode === 'configured' ? {} : { streaming: mode === 'stream' }) }, {
              signal: controller.signal,
              onStart: start => { entry.start = start; update(); },
              onDelta: delta => { entry.preview += delta.text; update(); },
            });
            entry.result = result; entry.preview = result.preview;
            entry.status = result.status === 'cancelled' ? 'cancelled' : result.status === 'passed' ? 'passed' : 'failed';
          } catch (cause) { entry.status = controller.signal.aborted ? 'cancelled' : 'failed'; entry.error = controller.signal.aborted ? undefined : failureText(cause); }
          update();
        }
      };
      await Promise.all(Array.from({ length: Math.min(3, queue.length) }, worker));
    } catch (cause) { if (!controller.signal.aborted) setError(failureText(cause)); }
    finally { if (batch.current === controller) { batch.current = undefined; setTesting(false); } }
  };
  const cancel = () => { discovery.current?.abort(); batch.current?.abort(); secretEdited.current = false; changeSecret(''); };
  const visible = catalog?.result.models.filter(item => `${item.model} ${item.name ?? ''}`.toLowerCase().includes(search.toLowerCase())) ?? [];
  const configured = new Set(models.map(model => model.model));
  const missing = models.filter(model => !catalog?.result.models.some(item => item.model === model.model));
  const observation = (value: LlmProbeObservationDTO) => <small>{value.mode} · {value.protocol} · {value.endpoint} · {value.observed_at}
    {' · '}{t('configuration.probe.editVersion', value.edit_version)} · {value.credential?.source}
    {value.credential?.revision == null ? '' : ` r${value.credential.revision}`}
    {value.mode === 'active' ? ` · active r${value.active_revision}` : ` · ${value.source_key}`}</small>;

  return <section className="configuration-model-probe" aria-label={t('configuration.probe.title')}>
    <h3>{t('configuration.probe.title')}</h3>
    <details><summary>{t('configuration.probe.options')}</summary>
    <p>{editor.target.gateway_id} · {editor.target.workspace_id} · {providerId}</p>
    <label className="configuration-entity-field">{t('configuration.probe.inputMode')}
      <select value={actualMode} disabled={discovering || testing} onChange={event => setMode(event.target.value as 'draft' | 'active')}>
        <option value="draft">{t('configuration.probe.draft')}</option><option value="active">{t('configuration.probe.active')}</option>
      </select>
      <small>{t('configuration.probe.versionSummary', editor.version, observedDocument.active_revision ?? '—', document.source_key)}</small>
    </label>
    <button type="button" disabled={refreshing} onClick={() => void refreshVersions()}>{t('configuration.probe.refreshVersions')}</button>
    <details><summary>{t('configuration.probe.temporaryKey')}</summary>
      <p>{t('configuration.probe.temporaryKeyHint')}</p>
      <label className="configuration-entity-field">{t('configuration.probe.keyScope')}
        <select value={secretScope} disabled={actualMode === 'active' || testing || discovering} onChange={event => {
          setSecretScope(event.target.value as 'provider' | 'model'); changeSecret('');
        }}><option value="provider">{providerId}</option>
          {selectedModelId ? <option value="model">{providerId} / {selectedModelId}</option> : null}</select>
      </label>
      <label className="configuration-entity-field">{t('configuration.probe.temporaryKey')}
        <input type="password" autoComplete="off" value={secret} disabled={actualMode === 'active'} onChange={event => {
          secretEdited.current = true; changeSecret(event.target.value);
        }} onBlur={() => {
          if (!secretEdited.current) return;
          secretEdited.current = false;
          if (editor.autoDiscover && secretScope === 'provider') void discover('draft');
        }} />
      </label>
    </details>
    </details>
    <h4>{t('configuration.probe.catalog')}</h4>
    <label><input type="checkbox" checked={editor.autoDiscover} onChange={event => editor.onAutoDiscover(event.target.checked)} /> {t('configuration.probe.autoDiscover')}</label>
    <div className="configuration-probe-actions">
      <button type="button" disabled={discovering || Boolean(secret && secretScope === 'model')} onClick={() => void discover()}>{t('configuration.probe.discover')}</button>
      {discovering ? <button type="button" onClick={cancel}>{t('configuration.probe.cancel')}</button> : null}
    </div>
    {catalog ? <>
      <p role="status">{t(catalog.result.complete ? 'configuration.probe.completeCatalog' : 'configuration.probe.incompleteCatalog', catalog.result.models.length)}
        {stale(catalog.result.observation, catalog.version) ? ` · ${t('configuration.probe.stale')}` : ''}</p>
      {catalog.result.observation ? observation(catalog.result.observation) : null}
      {catalog.result.error ? <p role="alert">{catalog.result.error.message}</p> : null}
      <label className="configuration-entity-field">{t('configuration.probe.search')}<input value={search} onChange={event => setSearch(event.target.value)} /></label>
      <button type="button" onClick={() => setSelected(new Set([...selected, ...visible.map(item => item.model)]))}>{t('configuration.probe.selectMatching')}</button>
      <div className="configuration-probe-models">{visible.map((item, index) => <label key={`${item.model}:${index}`}>
        <input type="checkbox" checked={selected.has(item.model)} onChange={event => setSelected(previous => {
          const next = new Set(previous); if (event.target.checked) next.add(item.model); else next.delete(item.model); return next;
        })} /> {item.name ?? item.model} <code>{item.model}</code> · {t(configured.has(item.model) ? 'configuration.probe.configured' : 'configuration.probe.newModel')}
      </label>)}</div>
      {missing.length ? <p>{t('configuration.probe.notReturned', missing.map(model => model.id).join(', '))}</p> : null}
      <button type="button" disabled={!document.writable || !selected.size || stale(catalog.result.observation, catalog.version)} onClick={() => {
        try { onImport(catalog.result.models.filter(item => selected.has(item.model)), editor.version); setSelected(new Set()); }
        catch (cause) { setError(failureText(cause)); }
      }}>{t('configuration.probe.importSelected')}</button>
    </> : null}
    <h4>{t('configuration.probe.quickTest')}</h4>
    <p>{t('configuration.probe.testHint')}</p>
    <div className="configuration-probe-models">{models.map(model => <label key={model.id}>
      <input type="checkbox" checked={testSelection.has(model.id)} onChange={event => setTestSelection(previous => {
        const next = new Set(previous); if (event.target.checked) next.add(model.id); else next.delete(model.id); return next;
      })} /> {model.id} · {model.model}
    </label>)}</div>
    <div className="configuration-probe-actions">{(['configured', 'stream', 'complete', 'both'] as const).map(testMode => <button type="button" key={testMode}
      disabled={testing || !selectedModelId} onClick={() => void runTests([selectedModelId!], testMode)}>{t(`configuration.probe.test.${testMode}`)}</button>)}
      <button type="button" disabled={testing || Boolean(secret && secretScope === 'model') || !models.some(model => testSelection.has(model.id))}
        onClick={() => void runTests(models.filter(model => testSelection.has(model.id)).map(model => model.id), 'configured')}>{t('configuration.probe.testSelected')}</button>
      {testing ? <button type="button" onClick={cancel}>{t('configuration.probe.stop')}</button> : null}
    </div>
    {Object.entries(tests).map(([key, entry]) => <section key={key} className="configuration-probe-result" data-probe-model={entry.modelId}>
      <p role="status">{entry.modelId} · {t(`configuration.probe.test.${entry.mode}`)} · {t(`configuration.probe.status.${entry.status}`)}
        {stale(entry.result?.observation ?? entry.start?.observation, entry.version) ? ` · ${t('configuration.probe.stale')}` : ''}</p>
      {entry.start?.observation ? observation(entry.start.observation) : null}
      {entry.preview ? <pre>{entry.preview}</pre> : null}
      {entry.result ? <p>{t('configuration.probe.timing', entry.result.first_content_ms?.toFixed(0) ?? '—', entry.result.elapsed_ms.toFixed(0))}
        {entry.result.preview_truncated ? ` · ${t('configuration.probe.truncated')}` : ''}
        {entry.result.usage ? ` · usage ${JSON.stringify(entry.result.usage)}` : ''}</p> : null}
      {entry.start ? <small>request_id: {entry.start.request_id}</small> : null}
      {entry.error || entry.result?.error ? <>
        <p role="alert">{(entry.error ?? entry.result!.error!.message).split('\n')[0]}</p>
        <details><summary>{t('configuration.probe.errorDetails')}</summary>
          <pre>{entry.error ?? `${entry.result!.error!.field ?? ''}\n${entry.result!.error!.message}`}</pre>
          {onOpenSource ? <button type="button" onClick={onOpenSource}>{t('configuration.entities.openJsonc')}</button> : null}
        </details>
      </> : null}
      {entry.result ? <button type="button" disabled={!document.writable || !models.some(model => model.id === entry.modelId)} onClick={() => onApplyMode(entry.modelId, entry.result!.streaming)}>{t('configuration.probe.applyMode')}</button> : null}
    </section>)}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
