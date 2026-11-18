import { t } from '../../../shared/core/i18n/index.js';
import { ConfigurationEntities, definitionObject, definitionValue, type DefinitionKind } from '../../../web/src/components/settings/ConfigurationEntities.js';
import { ModelConnectionProbe } from '../../../web/src/components/settings/ModelConnectionProbe.js';
import styles from '../../../web/src/components/settings/configurationEntities.css?text';
import { Action } from '../../upstream/src/vs/base/common/actions.js';
import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import { generateUuid } from '../../upstream/src/vs/base/common/uuid.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import { ToolBar } from '../../upstream/src/vs/base/browser/ui/toolbar/toolbar.js';
import { IContextMenuService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import { IStorageService, StorageScope, StorageTarget } from '../../upstream/src/vs/platform/storage/common/storage.js';
import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IEditorGroupsService } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { EditorsOrder } from '../../upstream/src/vs/workbench/common/editor.js';
import { ThreadInput } from '../threadInput.js';
import { ThreadPane } from '../threadPane.js';
import { ReactSurface } from '../react/surface.js';
import { createManagementTargetLifecycle } from '../managementTarget.js';
import { defaultAgentSourceCommand } from './commands.js';
import { listAgents, setWorkspaceDefaultAgent, setWorkspaceDefaultModel } from '../../../web/src/api/workspace.js';
import type { ModelReference } from '../../../web/src/types/backend.js';
import type { SourceSettings } from './editor.js';
import { chooseProviderCredential, manageSecrets } from './secrets.js';
import type { SettingsCategoryOptions } from './categories.js';

export interface DefinitionEditorOptions extends SettingsCategoryOptions { definitionKind?: DefinitionKind; definitionId?: string; definitionModelId?: string }

/** 原 Settings Pane 的业务内容；所有字段直接编辑原来源模型。 */
export class ConfigurationEntityView extends Disposable {
  private readonly surface: ReactSurface;
  private readonly toolbar: ToolBar;
  private readonly heading: HTMLElement;
  private readonly addAction = this._register(new Action('settings.definition.add', '', ThemeIcon.asClassName(Codicon.add), false, () => this.run(() => this.create())));
  private readonly saveAction = this._register(new Action('settings.definition.save', t('configuration.entities.save'), ThemeIcon.asClassName(Codicon.save), false, () => this.run(async () => {
    if (!await this.source.model.save()) throw new Error(t('configuration.entities.saveIncomplete'));
  })));
  private readonly restoreAction = this._register(new Action('settings.definition.restore', t('configuration.entities.restoreProviders'), '', false, () => this.run(() => this.restoreProviders())));
  private readonly secretsAction = this._register(new Action('settings.definition.secrets', t('settings.secrets.manage'), ThemeIcon.asClassName(Codicon.key), true,
    () => this.run(() => manageSecrets(this.source.location, this.quick, this.dialogs, this.storage))));
  private readonly openSourceAction = this._register(new Action('settings.definition.source', t('configuration.entities.openJsonc'), '', true, () => this.run(() => this.openSource())));
  private readonly sessionAction = this._register(new Action('settings.definition.session', t('configuration.entities.openSessionSelection'), '', true, () => this.run(() => this.openSessionSelection())));
  private readonly defaultsAction = this._register(new Action('settings.definition.defaults', t('configuration.entities.manageSessionDefaults'), '', true, () => this.run(() => this.manageSessionDefaults())));
  private toolbarMode?: string;
  private readonly lifetime = this._register(createManagementTargetLifecycle(t('workbench.settings.reopenSource')));
  private kind: DefinitionKind = 'providers';
  private selectedId?: string;
  private selectedModelId?: string;
  private visible = true;
  private readonly connectionEdits = new Map<string, number>();
  private readonly connectionCommits = new Map<string, number>();
  constructor(private readonly element: HTMLElement, private readonly source: SourceSettings, private readonly groupId: number,
    @IQuickInputService private readonly quick: IQuickInputService,
    @IDialogService private readonly dialogs: IDialogService,
    @INotificationService private readonly notifications: INotificationService,
    @IEditorService private readonly editors: IEditorService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService,
    @ICommandService private readonly commands: ICommandService,
    @IStorageService private readonly storage: IStorageService,
    @IContextMenuService menus: IContextMenuService) {
    super();
    element.className = 'configuration-entities';
    const container = document.createElement('div');
    container.style.cssText = 'display:flex;flex-direction:column;height:100%;min-height:0';
    const header = document.createElement('div');
    header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:12px;padding:0 24px;height:42px;flex-shrink:0;line-height:1.4em';
    this.heading = document.createElement('h2');
    this.heading.className = 'settings-group-title-label settings-group-level-1';
    this.heading.style.cssText = 'min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
    const actions = document.createElement('div'); actions.style.flexShrink = '0'; header.append(this.heading, actions);
    this.toolbar = this._register(new ToolBar(actions, menus));
    const body = document.createElement('div'); body.style.cssText = 'flex:1;min-height:0';
    container.append(header, body); element.replaceChildren(container);
    this.surface = this._register(new ReactSurface(body, [styles]));
    this._register(source.onDidChange(() => this.render()));
  }
  get selection(): DefinitionEditorOptions { return { definitionKind: this.kind, definitionId: this.selectedId, definitionModelId: this.selectedModelId }; }
  show(kind: DefinitionKind, selectedId?: string, selectedModelId?: string): void { this.kind = kind; this.selectedId = selectedId; this.selectedModelId = selectedModelId; this.setVisible(true); }
  setVisible(visible: boolean): void {
    this.visible = visible;
    if (visible) this.render(); else this.surface.render(null);
  }
  focus(): void { if (!this.surface.focus('.configuration-entity-columns nav button')) this.toolbar.focus(); }
  private run(action: () => void | Promise<void>): void {
    const report = (error: unknown) => { if (!this._store.isDisposed) this.notifications.error(error instanceof Error ? error : String(error)); };
    try { this.check(); Promise.resolve(action()).catch(report); } catch (error) { report(error); }
  }
  private check(): void { this.lifetime.signal.throwIfAborted(); this.lifetime.check(); }
  private render(): void {
    if (!this.visible) return;
    const document = this.source.document;
    if (!('workspace_id' in document)) throw new Error(t('configuration.entities.workspaceRequired'));
    let draft: Record<string, unknown> | undefined, error: string | undefined;
    try { draft = this.source.readDraft(); } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    this.element.dataset.definitionKind = this.kind;
    this.heading.textContent = t(this.kind === 'providers' ? 'workbench.settings.modelsProviders' : 'configuration.entities.agentsTitle');
    this.heading.title = `${this.heading.textContent}\n${t('configuration.entities.sourceBoundary', document.source_key)}`;
    this.addAction.label = t(this.kind === 'providers' ? 'configuration.entities.addProvider' : 'configuration.entities.addAgent');
    this.addAction.enabled = this.saveAction.enabled = document.writable && Boolean(draft);
    this.restoreAction.enabled = this.addAction.enabled;
    const localProviders = Object.keys(definitionObject(definitionValue(draft, ['llm', 'providers']))).length > 0;
    const toolbarMode = `${this.kind}:${localProviders}`;
    if (this.toolbarMode !== toolbarMode) {
      this.toolbarMode = toolbarMode;
      this.toolbar.setActions([this.addAction, this.saveAction], [this.openSourceAction, this.sessionAction, this.defaultsAction,
        ...(this.kind === 'providers' ? [this.secretsAction] : []),
        ...(this.kind === 'providers' && localProviders ? [this.restoreAction] : [])]);
    }
    this.surface.render(<ConfigurationEntities kind={this.kind} selectedId={this.selectedId} selectedModelId={this.selectedModelId} draft={draft} document={document} error={error}
      draftVersion={this.source.model.textEditorModel!.getVersionId()}
      onInspectModel={async (modelRef, signal) => {
        this.check();
        const result = await this.source.preflightDraft(AbortSignal.any([signal, this.lifetime.signal]), modelRef);
        this.check();
        if (!('model_options' in result) || !result.model_options) throw new Error(t('configuration.inspect.missing'));
        return result;
      }}
      pendingSubmission={this.source.pendingSubmission}
      onQuerySubmission={() => this.run(() => this.source.querySubmission())}
      onSetSecret={(id, modelId) => this.run(() => this.setSecret(id, modelId))}
      onSelect={id => { this.selectedId = id; this.selectedModelId = undefined; this.render(); }}
      onSelectModel={id => { this.selectedModelId = id; this.render(); }}
      onAddModel={id => this.run(() => this.addModel(id))}
      onCopyModel={(id, modelId) => this.run(() => this.addModel(id, modelId))}
      onRemoveModel={(id, modelId) => this.run(() => this.removeModel(id, modelId))}
      onConnectionCommit={id => this.commitConnection(id)}
      renderModelProbe={(id, models, selectedModelId) => <ModelConnectionProbe key={id} providerId={id} models={models}
        selectedModelId={selectedModelId} document={document} editor={{
          target: { gateway_id: this.source.location.gateway_id, workspace_id: document.workspace_id,
            gateway_connection_id: this.source.location.gateway_connection_id },
          jsonc: this.source.model.textEditorModel!.getValue(), version: this.source.model.textEditorModel!.getVersionId(),
          dirty: this.source.model.isDirty(), signal: this.lifetime.signal, connectionCommit: this.connectionCommits.get(id) ?? 0,
          autoDiscover: this.storage.getBoolean('boxteam.settings.autoDiscoverModels', StorageScope.PROFILE, true),
          onAutoDiscover: enabled => { this.storage.store('boxteam.settings.autoDiscoverModels', enabled, StorageScope.PROFILE, StorageTarget.USER); this.render(); },
        }} onImport={(items, version) => {
          this.check();
          if (this.source.model.textEditorModel!.getVersionId() !== version) throw new Error(t('configuration.entities.definitionChanged', id));
          const occupied = new Set(models.map(model => model.id));
          for (const item of items) {
            const base = item.model.replace(/[^a-zA-Z0-9._-]/g, '-').slice(0, 50) || 'model';
            let modelId = base, suffix = 1;
            while (occupied.has(modelId)) modelId = `${base}-${++suffix}`;
            occupied.add(modelId);
            this.source.editPath(['llm', 'providers', id, 'models', modelId], { model: item.model, ...(item.name ? { name: item.name } : {}) });
          }
        }} onApplyMode={(modelId, streaming) => this.run(() => this.source.editPath(['llm', 'providers', id, 'models', modelId, 'overrides', 'api_mode', 'streaming'], streaming))}
        onOpenSource={() => this.run(() => this.openSource())} />}
      onEdit={(id, path, value) => this.run(() => {
        const prefix = this.kind === 'agents' ? ['agents', id] : ['llm', 'providers', id];
        if (this.kind === 'providers' && path[0] === 'defaults' && (
          ['endpoint', 'custom_llm_provider', 'api_key'].includes(String(path[1]))
          || path[1] === 'api_mode' && path.length === 2
          || path[1] === 'request_options' && ['default_headers', 'no_proxy'].includes(String(path[2]))
        )) this.connectionEdits.set(id, (this.connectionEdits.get(id) ?? 0) + 1);
        this.source.editPath([...prefix, ...path], value);
      })}
      onRemove={id => this.run(() => this.remove(id))}
      onSetDefaultAgent={id => this.run(() => this.source.editPath(['default_agent'], id))}
      onRestoreDefaultAgent={() => this.run(() => this.source.editPath(['default_agent'], undefined))}
      onInspectDefaultAgent={() => this.run(() => this.commands.executeCommand(defaultAgentSourceCommand, this.source.location))}
      onManageAgentTools={id => this.run(() => this.commands.executeCommand('boxteam.tools.revealAgent', {
        gateway_id: this.source.location.gateway_id, workspace_id: this.source.location.workspace_id,
        gateway_connection_id: this.source.location.gateway_connection_id, agent_id: id,
      }))}
      onOpenSource={sourceKey => this.run(() => this.openSource(sourceKey))} />);
  }
  private commitConnection(id: string): void {
    const edit = this.connectionEdits.get(id) ?? 0;
    if (edit === (this.connectionCommits.get(id) ?? 0)) return;
    this.connectionCommits.set(id, edit); this.render();
  }
  private async openSource(sourceKey?: string): Promise<void> {
    await this.commands.executeCommand('boxteam.settings.openSource', {
      location: { ...this.source.location, source_key: sourceKey ?? this.source.location.source_key }, groupId: this.groupId,
    });
  }

  private async manageSessionDefaults(): Promise<void> {
    // 新会话默认值属于 SessionDefaults，与本来源尚未保存的定义分开操作。
    const { gateway_id, workspace_id, gateway_connection_id } = this.source.location;
    if (!workspace_id) throw new Error(t('configuration.entities.workspaceRequired'));
    const target = { gateway_id, workspace_id, gateway_connection_id };
    const agents = await listAgents(0, target, this.lifetime.signal);
    this.check();
    const selected = await this.quick.pick(agents.map(agent => ({ label: agent.name, description: agent.agent_id,
      detail: t('configuration.entities.sessionDefaultsSummary', agent.model,
        agent.models.find(model => model.workspace_default)?.model_ref?.model_id ?? '',
        agent.workspace_default ? t('configuration.entities.workspaceDefaultAgent') : ''), agent })),
      { title: t('configuration.entities.manageSessionDefaults'),
        placeHolder: t('configuration.entities.sessionDefaultsBoundary', gateway_id, workspace_id) }, this.lifetime.token);
    this.check(); if (!selected) return;
    const { agent } = selected;
    const action = await this.quick.pick([
      { label: t('configuration.entities.useWorkspaceDefaultAgent'), description: agent.agent_id, model: undefined as ModelReference | undefined },
      ...agent.models.map(model => ({ label: `${model.model_ref.provider_id} / ${model.model_ref.model_id}`, description: model.model,
        detail: t('configuration.entities.useWorkspaceDefaultProvider', agent.agent_id), model: model.model_ref })),
    ], { title: t('configuration.entities.manageSessionDefaults'),
      placeHolder: t('configuration.entities.sessionDefaultsBoundary', gateway_id, workspace_id) }, this.lifetime.token);
    this.check(); if (!action) return;
    const confirmed = await this.dialogs.confirm({ type: 'question', message: action.model === undefined
      ? t('configuration.entities.useWorkspaceDefaultAgent') : t('configuration.entities.useWorkspaceDefaultProvider', agent.agent_id),
      detail: `${gateway_id} · ${workspace_id} · ${agent.agent_id}${action.model === undefined ? '' : ` · ${action.model.provider_id} / ${action.model.model_id}`}\n`
        + t('configuration.entities.sessionDefaultsSaveBoundary'), primaryButton: t('configuration.entities.confirmSessionDefault') });
    this.check(); if (!confirmed.confirmed) return;
    const summary = (value: Awaited<ReturnType<typeof listAgents>>) => t('configuration.entities.sessionDefaultsCurrent',
      value.find(item => item.workspace_default)?.agent_id ?? '', agent.agent_id,
      value.find(item => item.agent_id === agent.agent_id)?.models.find(item => item.workspace_default)?.model_ref?.model_id ?? '');
    try {
      const updated = action.model === undefined ? await setWorkspaceDefaultAgent(0, agent.agent_id, target)
        : await setWorkspaceDefaultModel(0, agent.agent_id, action.model, target);
      this.notifications.info(summary(updated));
    } catch (error) {
      // 失败仅重读原 owner，不重放写入；原回执和重读错误都保留。
      const latest = await listAgents(0, target).catch(readError => { throw new AggregateError([error, readError],
        [error, readError].map(cause => cause instanceof Error ? cause.message : String(cause)).join('\n')); });
      throw new Error(`${error instanceof Error ? error.message : String(error)}\n${summary(latest)}`, { cause: error });
    }
  }
  private async setSecret(id: string, modelId?: string): Promise<void> {
    if (this.source.pendingSubmission) { await this.source.querySubmission(); return; }
    const version = this.source.model.textEditorModel!.getVersionId();
    const credential = await chooseProviderCredential(this.source.location, this.quick, this.lifetime, this.storage);
    this.check();
    if (credential !== undefined) {
      if (this.source.model.textEditorModel!.getVersionId() !== version) throw new Error(t('configuration.entities.definitionChanged', id));
      this.source.editPath(['llm', 'providers', id, ...(modelId === undefined ? ['defaults'] : ['models', modelId, 'overrides']), 'api_key'], credential);
      if (modelId === undefined) {
        this.connectionEdits.set(id, (this.connectionEdits.get(id) ?? 0) + 1);
        this.commitConnection(id);
      }
    }
  }
  private async openSessionSelection(): Promise<void> {
    const kind = this.kind, location = this.source.location;
    const targets = this.editors.getEditors(EditorsOrder.SEQUENTIAL).flatMap(entry => entry.editor instanceof ThreadInput
      && entry.editor.owner.gatewayId === location.gateway_id && entry.editor.owner.workspaceId === location.workspace_id
      ? [{ ...entry, editor: entry.editor }] : []);
    if (!targets.length) throw new Error(t('configuration.entities.openThreadFirst'));
    const selected = await this.quick.pick(targets.map(entry => ({ ...entry, label: entry.editor.owner.title,
      description: t('workbench.composer.recipientDetail', entry.editor.owner.sessionId, entry.editor.owner.threadId, entry.groupId) })),
    { title: t('configuration.entities.openSessionSelection') }, this.lifetime.token);
    this.check();
    if (!selected) return;
    const group = this.groups.getGroup(selected.groupId);
    if (!group?.contains(selected.editor)) throw new Error(t('workbench.composer.recipientChanged'));
    const pane = await this.editors.openEditor(selected.editor, { pinned: true }, selected.groupId);
    if (!(pane instanceof ThreadPane) || pane.threadTarget?.input !== selected.editor) throw new Error(t('workbench.composer.recipientChanged'));
    pane.threadTarget.conversation.focusConfigurationControl(kind);
  }
  private async input(prompt: string, validate?: (value: string) => string | undefined, initialValue?: string): Promise<string | undefined> {
    const value = await this.quick.input({ title: t(this.kind === 'providers' ? 'configuration.entities.addProvider' : 'configuration.entities.addAgent'),
      prompt, value: initialValue, validateInput: async value => validate?.(value) ?? (!value.trim() ? t('configuration.entities.required') : undefined) }, this.lifetime.token);
    this.check();
    return value?.trim();
  }
  private async create(): Promise<void> {
    const kind = this.kind;
    const version = this.source.model.textEditorModel!.getVersionId();
    const id = await this.input(t('configuration.entities.newId'), value => /^[a-zA-Z0-9._-]{2,64}$/.test(value) ? undefined : t('configuration.entities.invalidId'),
      `${kind === 'providers' ? 'provider' : 'agent'}-${generateUuid().slice(0, 8)}`);
    if (id === undefined) return;
    if (kind === 'providers') {
      const providers = definitionObject(definitionValue(this.source.document.source_effective, ['llm', 'providers']));
      const current = definitionObject(definitionValue(this.source.readDraft(), ['llm', 'providers']));
      if (Object.hasOwn(current, id) || Object.hasOwn(providers, id)) throw new Error(t('configuration.entities.duplicateId', id));
      const protocol = await this.quick.pick(['chat_completions', 'responses', 'anthropic_messages'].map(label => ({ label })),
        { title: t('configuration.entities.chooseProtocol') }, this.lifetime.token);
      this.check(); if (!protocol) return;
      if (this.source.model.textEditorModel!.getVersionId() !== version) throw new Error(t('configuration.entities.definitionChanged', id));
      this.source.editPath(['llm', 'providers', id], {
        defaults: { custom_llm_provider: protocol.label === 'anthropic_messages' ? 'anthropic' : 'openai', api_key: '',
          api_mode: { protocol: protocol.label, model_info: {}, supports_reasoning: {} } },
        models: {},
      });
      this.selectedModelId = undefined;
    } else {
      const name = await this.input(t('configuration.entities.name')); if (name === undefined) return;
      const prompt = await this.input(t('configuration.entities.systemPrompt')); if (prompt === undefined) return;
      const activeProviders = definitionObject(definitionValue(this.source.document.source_effective, ['llm', 'providers']));
      const localProviders = definitionObject(definitionValue(this.source.readDraft(), ['llm', 'providers']));
      const choices = Object.keys({ ...activeProviders, ...localProviders }).flatMap(providerId => {
        const saved = definitionObject(definitionObject(activeProviders[providerId]).models);
        const local = definitionObject(definitionObject(localProviders[providerId]).models);
        return Object.keys({ ...saved, ...local }).map(modelId => ({ label: `${providerId} / ${modelId}`,
          model: { provider_id: providerId, model_id: modelId } }));
      });
      const provider = await this.quick.pick(choices, { title: t('configuration.entities.primaryModel') }, this.lifetime.token);
      this.check(); if (!provider) return;
      if (this.source.model.textEditorModel!.getVersionId() !== version) throw new Error(t('configuration.entities.definitionChanged', id));
      const agents = definitionObject(this.source.readDraft().agents);
      if (Object.hasOwn(agents, id)) throw new Error(t('configuration.entities.duplicateId', id));
      const document = this.source.document;
      const selected = document.layers.find(layer => layer.source_key === document.source_key);
      const inherited = document.layers.some(layer => selected && layer.precedence < selected.precedence
        && Object.hasOwn(definitionObject(layer.payload?.agents), id));
      if (inherited) {
        const confirmed = await this.dialogs.confirm({ type: 'warning', message: t('configuration.entities.overrideInheritedAgent', id),
          detail: t('configuration.entities.overrideAgentBoundary'), primaryButton: t('configuration.entities.addAgent') });
        this.check(); if (!confirmed.confirmed) return;
        if (this.source.model.textEditorModel!.getVersionId() !== version) throw new Error(t('configuration.entities.definitionChanged', id));
      }
      this.source.editPath(['agents', id], { name, instructions: { system_prompt: prompt, ...(inherited ? { system_prompt_file: null } : {}) },
        model: { primary: provider.model, fallbacks: [] } });
    }
    this.selectedId = id; this.render();
  }
  private async addModel(providerId: string, copyId?: string): Promise<void> {
    const version = this.source.model.textEditorModel!.getVersionId();
    const id = await this.input(t('configuration.entities.modelId'), value => /^[a-zA-Z0-9._-]{1,64}$/.test(value) ? undefined : t('configuration.entities.invalidId'),
      `model-${generateUuid().slice(0, 8)}`);
    if (id === undefined) return;
    const path = ['llm', 'providers', providerId, 'models'];
    const saved = definitionObject(definitionValue(this.source.document.source_effective, path));
    const current = definitionObject(definitionValue(this.source.readDraft(), path));
    if (Object.hasOwn(saved, id) || Object.hasOwn(current, id)) throw new Error(t('configuration.entities.duplicateId', id));
    const preview = copyId === undefined ? undefined : await this.source.preflightDraft(this.lifetime.signal);
    this.check();
    if (version !== this.source.model.textEditorModel!.getVersionId()) throw new Error(t('configuration.entities.definitionChanged', id));
    const original = copyId === undefined ? undefined : definitionValue(preview?.candidate, [...path, copyId]);
    if (copyId !== undefined && !original) throw new Error(t('configuration.entities.definitionChanged', copyId));
    const model = original ?? { model: await this.input(t('configuration.entities.model')) };
    if (!original && definitionObject(model).model === undefined) return;
    if (version !== this.source.model.textEditorModel!.getVersionId()) throw new Error(t('configuration.entities.definitionChanged', id));
    this.source.editPath([...path, id], model);
    this.selectedId = providerId; this.selectedModelId = id; this.render();
  }
  private async removeModel(providerId: string, modelId: string): Promise<void> {
    const confirmed = await this.dialogs.confirm({ type: 'warning', message: t('configuration.entities.confirmRemove', `${providerId} / ${modelId}`),
      detail: t('configuration.entities.removeModelBoundary'), primaryButton: t('configuration.entities.removeLocal') });
    this.check(); if (confirmed.confirmed) this.source.editPath(['llm', 'providers', providerId, 'models', modelId], undefined);
  }
  private async remove(id: string): Promise<void> {
    const kind = this.kind;
    const result = await this.dialogs.confirm({ type: 'warning', message: t('configuration.entities.confirmRemove', id),
      detail: t(kind === 'providers' ? 'configuration.entities.removeProviderBoundary' : 'configuration.entities.removeAgentBoundary'),
      primaryButton: t('configuration.entities.removeLocal') });
    this.check(); if (!result.confirmed) return;
    this.source.editPath(kind === 'agents' ? ['agents', id] : ['llm', 'providers', id], undefined);
  }
  private async restoreProviders(): Promise<void> {
    const result = await this.dialogs.confirm({ type: 'warning', message: t('configuration.entities.restoreProviders'),
      detail: t('configuration.entities.restoreProvidersBoundary'), primaryButton: t('configuration.entities.restoreProviders') });
    this.check(); if (result.confirmed) this.source.editPath(['llm', 'providers'], undefined);
  }
}
