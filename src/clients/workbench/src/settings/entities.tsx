import { t } from '../../../shared/core/i18n/index.js';
import { ConfigurationEntities, definitionObject, definitionValue, localProfilePath, type ConfigurationSelector, type DefinitionKind } from '../../../web/src/components/settings/ConfigurationEntities.js';
import type { ConfigModelInspectionDTO, ConfigProviderInspectionDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { ModelConnectionProbe } from '../../../web/src/components/settings/ModelConnectionProbe.js';
import styles from '../../../web/src/components/settings/configurationEntities.css?text';
import { Action, toAction } from '../../upstream/src/vs/base/common/actions.js';
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
import { ILogService } from '../../upstream/src/vs/platform/log/common/log.js';
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
import { SecretsClient, observeSecretMetadata } from '../../../web/src/api/secrets.js';
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
    // 原文本文件保存链负责报告错误；取消或失败时保留草稿，不再弹出第二条笼统提示。
    await this.source.model.save();
  })));
  private readonly restoreAction = this._register(new Action('settings.definition.restore', t('configuration.entities.restoreProviders'), '', false, () => this.run(() => this.restoreProviders())));
  private readonly secretsAction = this._register(new Action('settings.definition.secrets', t('settings.secrets.manage'), ThemeIcon.asClassName(Codicon.key), true,
    () => this.run(() => manageSecrets(this.source.location, this.quick, this.dialogs, this.storage))));
  private readonly openSourceAction = this._register(new Action('settings.definition.source', t('configuration.entities.openJsonc'), '', true, () => this.run(() => this.openSource())));
  private readonly sessionAction = this._register(new Action('settings.definition.session', t('configuration.entities.openSessionSelection'), '', true, () => this.run(() => this.openSessionSelection())));
  private readonly defaultsAction = this._register(new Action('settings.definition.defaults', t('configuration.entities.manageSessionDefaults'), '', true, () => this.run(() => this.manageSessionDefaults())));
  private toolbarMode?: string;
  private credentialNames: Record<string, string> = {};
  private searchQuery = '';
  private readonly lifetime = this._register(createManagementTargetLifecycle(t('workbench.settings.reopenSource')));
  private kind: DefinitionKind = 'providers';
  private selectedId?: string;
  private selectedModelId?: string;
  private visible = true;
  private readonly connectionEdits = new Map<string, number>();
  private readonly connectionCommits = new Map<string, number>();
  private readonly nodeTargets = new Map<string, { version: number; path: string[]; options: Record<string, unknown>; create?: { path: string[]; value: unknown }[] }>();
  private editingNode = false;
  constructor(private readonly element: HTMLElement, private readonly source: SourceSettings, private readonly groupId: number,
    @IQuickInputService private readonly quick: IQuickInputService,
    @IDialogService private readonly dialogs: IDialogService,
    @INotificationService private readonly notifications: INotificationService,
    @IEditorService private readonly editors: IEditorService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService,
    @ICommandService private readonly commands: ICommandService,
    @IStorageService private readonly storage: IStorageService,
    @IContextMenuService private readonly menus: IContextMenuService,
    @ILogService private readonly log: ILogService) {
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
    this.toolbar = this._register(new ToolBar(actions, this.menus));
    const body = document.createElement('div'); body.style.cssText = 'flex:1;min-height:0';
    container.append(header, body); element.replaceChildren(container);
    this.surface = this._register(new ReactSurface(body, [styles], diagnostic => this.log.error(diagnostic)));
    this._register(source.onDidChange(() => { if (!this.editingNode) { this.nodeTargets.clear(); this.render(); } }));
    const receive = (snapshot: Awaited<ReturnType<SecretsClient['list']>>) => {
      this.credentialNames = Object.fromEntries(snapshot.items.map(item => [item.id, item.name])); this.render();
    };
    this._register({ dispose: observeSecretMetadata(source.location.gateway_id, snapshot => {
      if ('items' in snapshot) receive(snapshot);
      else if (snapshot.item) { this.credentialNames = { ...this.credentialNames, [snapshot.item.id]: snapshot.item.name }; this.render(); }
    }) });
    void new SecretsClient(source.location, this.lifetime.signal).list().catch(error => {
      if (!this.lifetime.signal.aborted) this.notifications.error(error);
    });
  }
  get selection(): DefinitionEditorOptions { return { definitionKind: this.kind, definitionId: this.selectedId, definitionModelId: this.selectedModelId }; }
  show(kind: DefinitionKind, selectedId?: string, selectedModelId?: string, searchQuery = ''): void { this.searchQuery = searchQuery; this.kind = kind; this.selectedId = selectedId; this.selectedModelId = selectedModelId; this.setVisible(true); }
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
    const localProviders = Object.keys(definitionObject(definitionValue(draft, ['settings', 'workspace', 'llm', 'providers']))).length > 0;
    const toolbarMode = `${this.kind}:${localProviders}`;
    if (this.toolbarMode !== toolbarMode) {
      this.toolbarMode = toolbarMode;
      this.toolbar.setActions([this.addAction, this.saveAction], [this.openSourceAction, this.sessionAction, this.defaultsAction,
        ...(this.kind === 'providers' ? [this.secretsAction] : []),
        ...(this.kind === 'providers' && localProviders ? [this.restoreAction] : [])]);
    }
    this.surface.render(<ConfigurationEntities kind={this.kind} selectedId={this.selectedId} selectedModelId={this.selectedModelId} draft={draft} document={document} error={error} searchQuery={this.searchQuery} credentialNames={this.credentialNames} dirty={this.source.model.isDirty()}
      draftVersion={this.source.model.textEditorModel!.getVersionId()}
      isNodeReady={selector => this.nodeReady(selector)}
      onPrepareOptions={(selector, signal) => this.prepareNodes(selector, signal)}
      onInspectModel={async (modelRef, signal) => {
        this.check();
        const result = await this.source.inspectOptions(AbortSignal.any([signal, this.lifetime.signal]), modelRef);
        this.check();
        if (!('provider_options' in result) || !result.provider_options) throw new Error(t('configuration.inspect.missing'));
        return result;
      }}
      onEditOptions={(selector, path, value) => this.run(() => this.editNode(selector, path, value))}
      onEditNodeParent={(selector, value) => this.run(() => this.editNode(selector, ['extends'], value))}
      pendingSubmission={this.source.pendingSubmission}
      onQuerySubmission={() => this.run(() => this.source.querySubmission())}
      onSetSecret={(id, modelId) => this.run(() => this.setSecret(id, modelId))}
      onSelect={id => { this.selectedId = id; this.selectedModelId = undefined; this.render(); }}
      onSelectModel={id => { this.selectedModelId = id; this.render(); }}
      catalogTarget={{ gateway_id: this.source.location.gateway_id, workspace_id: document.workspace_id,
        gateway_connection_id: this.source.location.gateway_connection_id }} catalogSignal={this.lifetime.signal}
      onProviderTemplate={(id, choice) => this.run(async () => {
        const original = definitionObject(definitionValue(this.source.readDraft(), ['settings', 'workspace', 'llm', 'providers', id]));
        const before = this.connectionEdits.get(id) ?? 0;
        this.editNodeFields({ provider_id: id }, [
          { path: ['model_options', 'custom_llm_provider'], value: choice.id || 'openai' },
          { path: ['model_options', 'endpoint'], value: choice.endpoint ?? '' },
          { path: ['model_options', 'api_mode'], value: { protocol: choice.protocol ?? 'chat_completions', supports_reasoning: {} } },
        ]);
        const target = this.nodeTargets.get(this.nodeKey({ provider_id: id }));
        if (!target) throw new Error(t('configuration.inspect.missing'));
        const options = definitionObject(definitionValue(this.source.readDraft(), [...target.path, 'settings', 'model_options']));
        if (choice.id && (original.name === t('configuration.catalog.newProvider') || original.name === target.options.custom_llm_provider)) {
          this.source.editPath(['llm', 'providers', id, 'name'], choice.id);
          this.rememberNode({ provider_id: id }, target.path, target.options);
        }
        // 首次选服务商只填模板；存在明确凭据后才自动发现目录。
        if ((options.api_key ?? target.options.api_key) || (options.auth ?? target.options.auth)) this.commitConnection(id);
        else this.connectionEdits.set(id, before);
      })}
      onAddModel={(id, model) => this.run(() => this.addModel(id, model))}
      onShowActions={(id, modelId, anchor) => this.run(() => this.showDefinitionActions(id, modelId, anchor))}
      onConnectionCommit={id => this.run(() => this.commitConnection(id))}
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
            this.selectedModelId = modelId;
            this.createModelNode(id, modelId, { model: item.model, ...(item.name ? { name: item.name } : {}) });
          }
          this.render();
        }} canApplyMode={modelId => this.nodeReady({ provider_id: id, model_id: modelId })}
        onApplyMode={(modelId, streaming) => this.run(() => this.editNode({ provider_id: id, model_id: modelId }, ['model_options', 'api_mode', 'streaming'], streaming))}
        onOpenSource={() => this.run(() => this.openSource())} />}
      onEdit={(id, path, value) => this.run(() => {
        const targets = path.at(-1) === 'config_ref' ? [] : [...this.nodeTargets];
        this.source.editPath([...(this.kind === 'agents' ? ['agents', id] : ['llm', 'providers', id]), ...path], value);
        for (const [key, target] of targets) this.nodeTargets.set(key, { ...target, version: this.source.model.textEditorModel!.getVersionId() });
      })}
      onSetDefaultAgent={id => this.run(() => this.source.editPath(['default_agent'], id))}
      onRestoreDefaultAgent={() => this.run(() => this.source.editPath(['default_agent'], undefined))}
      onInspectDefaultAgent={() => this.run(() => this.commands.executeCommand(defaultAgentSourceCommand, this.source.location))}
      onManageAgentTools={id => this.run(() => this.commands.executeCommand('boxteam.tools.revealAgent', {
        gateway_id: this.source.location.gateway_id, workspace_id: this.source.location.workspace_id,
        gateway_connection_id: this.source.location.gateway_connection_id, agent_id: id,
      }))}
      onOpenSource={(sourceKey, definitionPath) => this.run(() => this.openSource(sourceKey, definitionPath))} />);
  }
  private commitConnection(id: string): void {
    const edit = this.connectionEdits.get(id) ?? 0;
    if (edit === (this.connectionCommits.get(id) ?? 0)) return;
    this.connectionCommits.set(id, edit); this.render();
  }
  private nodeKey(selector: ConfigurationSelector): string { return JSON.stringify([selector.provider_id, selector.model_id]); }
  private rememberNode(selector: ConfigurationSelector, path: string[], options: Record<string, unknown>): void {
    this.nodeTargets.set(this.nodeKey(selector), { version: this.source.model.textEditorModel!.getVersionId(), path, options });
  }
  private nodeReady(selector: ConfigurationSelector): boolean {
    return this.nodeTargets.get(this.nodeKey(selector))?.version === this.source.model.textEditorModel!.getVersionId();
  }
  private async prepareNodes(selector: ConfigurationSelector, signal: AbortSignal): Promise<void> {
    const provider = { provider_id: selector.provider_id };
    if (this.nodeReady(provider) && this.nodeReady(selector)) return;
    const version = this.source.model.textEditorModel!.getVersionId();
    const result = await this.source.inspectOptions(AbortSignal.any([signal, this.lifetime.signal]), selector);
    this.check(); signal.throwIfAborted();
    if (version !== this.source.model.textEditorModel!.getVersionId()) throw new Error(t('configuration.entities.definitionChanged', selector.provider_id));
    if (!('provider_options' in result) || !result.provider_options || selector.model_id !== undefined && !result.model_options) throw new Error(t('configuration.inspect.missing'));
    this.prepareNode(provider, result.provider_options);
    if (selector.model_id !== undefined && result.model_options) this.prepareNode(selector, result.model_options);
    this.render();
  }
  private prepareNode(selector: ConfigurationSelector, inspection: ConfigProviderInspectionDTO | ConfigModelInspectionDTO): void {
    if (this.nodeReady(selector)) return;
    const selected = inspection.chain.find(node => node.node_id === inspection.node_id);
    if (!selected) throw new Error(t('configuration.inspect.missing'));
    const referencePath = ['settings', 'workspace', 'llm', 'providers', selector.provider_id,
      ...(selector.model_id === undefined ? [] : ['models', selector.model_id]), 'config_ref'];
    const document = this.source.document;
    if (!('workspace_id' in document)) throw new Error(t('configuration.entities.workspaceRequired'));
    const localPath = localProfilePath(definitionValue(this.source.readDraft(), referencePath), document.source_baseline[document.source_key]?.uri);
    const options = definitionObject(inspection.options);
    if (localPath && selected.source_key === document.source_key && !inspection.shared) {
      this.rememberNode(selector, localPath, options);
    } else {
      const profile = `private-${generateUuid()}`, path = ['profiles', profile];
      this.nodeTargets.set(this.nodeKey(selector), { version: this.source.model.textEditorModel!.getVersionId(), path, options,
        create: [{ path, value: { extends: selected.reference, settings: {} } }, { path: referencePath, value: `#/profiles/${profile}` }] });
    }
  }
  private editNode(selector: ConfigurationSelector, path: (string | number)[], value: unknown): void {
    this.editNodeFields(selector, [{ path, value }]);
  }
  private editNodeFields(selector: ConfigurationSelector, changes: { path: (string | number)[]; value: unknown }[]): void {
    this.check();
    const target = this.nodeTargets.get(this.nodeKey(selector));
    if (!target || !this.nodeReady(selector)) throw new Error(t('configuration.entities.definitionChanged', selector.provider_id));
    const edits: { path: (string | number)[]; value: unknown }[] = [...(target.create ?? [])];
    for (const { path, value } of changes) {
      edits.push({ path: path[0] === 'extends' ? [...target.path, ...path] : [...target.path, 'settings', ...path], value });
      if (path.join('.') === 'model_options.api_mode') {
        const handler = changes.find(change => change.path.join('.') === 'model_options.custom_llm_provider')?.value
          ?? definitionValue(this.source.readDraft(), [...target.path, 'settings', 'model_options', 'custom_llm_provider']) ?? target.options.custom_llm_provider;
        const protocol = definitionObject(value).protocol;
        if (['', 'openai', 'anthropic'].includes(String(handler ?? '')) && ['chat_completions', 'responses', 'anthropic_messages'].includes(String(protocol))) {
          edits.push({ path: [...target.path, 'settings', 'model_options', 'custom_llm_provider'], value: protocol === 'anthropic_messages' ? 'anthropic' : 'openai' });
        }
      }
    }
    // 编辑前取得归属，确认后同步写完整操作；原文本保存绝不会抢在字段写入前执行。
    this.editingNode = true;
    try { this.source.editDocumentPaths(edits); } finally { this.editingNode = false; }
    const provider = selector.model_id === undefined ? undefined : this.nodeTargets.get(this.nodeKey({ provider_id: selector.provider_id }));
    this.nodeTargets.clear();
    if (provider) this.nodeTargets.set(this.nodeKey({ provider_id: selector.provider_id }), { ...provider, version: this.source.model.textEditorModel!.getVersionId() });
    this.rememberNode(selector, target.path, changes.some(change => change.path[0] === 'extends') ? {} : target.options);
    if (selector.model_id === undefined && changes.some(({ path }) => path[0] === 'model_options' && ['endpoint', 'custom_llm_provider', 'api_key', 'api_mode', 'request_options'].includes(String(path[1])))) {
      this.connectionEdits.set(selector.provider_id, (this.connectionEdits.get(selector.provider_id) ?? 0) + 1);
    }
    this.render();
  }
  private createModelNode(providerId: string, modelId: string, definition: Record<string, unknown>, node: { extends?: string; settings: Record<string, unknown> } = { extends: `context:provider/${providerId}`, settings: {} }): void {
    const profile = `model-${generateUuid()}`;
    const providerKey = this.nodeKey({ provider_id: providerId });
    const provider = this.nodeReady({ provider_id: providerId }) ? this.nodeTargets.get(providerKey) : undefined;
    this.source.editDocumentPaths([{ path: ['profiles', profile], value: node },
      { path: ['settings', 'workspace', 'llm', 'providers', providerId, 'models', modelId], value: { ...definition, config_ref: `#/profiles/${profile}` } }]);
    if (provider) this.nodeTargets.set(providerKey, { ...provider, version: this.source.model.textEditorModel!.getVersionId() });
    this.rememberNode({ provider_id: providerId, model_id: modelId }, ['profiles', profile], {});
  }
  private showDefinitionActions(id: string, modelId: string | undefined, anchor: HTMLElement): void {
    const draft = this.source.readDraft();
    const enabled = this.source.document.writable;
    const path = this.kind === 'agents' ? ['agents', id] : ['llm', 'providers', id, ...(modelId === undefined ? [] : ['models', modelId])];
    const actions = modelId === undefined ? this.kind === 'providers' ? [toAction({ id: 'settings.provider.copy',
      label: t('configuration.node.copyProvider'), enabled, run: () => this.run(() => this.copyProvider(id)) })] : [] : [toAction({ id: 'settings.model.copy',
      label: t('configuration.entities.copyModel'), enabled, run: () => this.run(() => this.addModel(id, undefined, modelId)) })];
    if (definitionValue(draft, ['settings', 'workspace', ...path]) !== undefined) actions.push(toAction({ id: 'settings.definition.remove', enabled,
      label: t(modelId !== undefined ? 'configuration.entities.removeModel' : this.kind === 'providers'
        ? 'configuration.entities.removeProvider' : 'configuration.entities.removeAgent'),
      run: () => this.run(() => modelId === undefined ? this.remove(id) : this.removeModel(id, modelId)) }));
    this.menus.showContextMenu({ getAnchor: () => anchor, getActions: () => actions });
  }
  private async openSource(sourceKey?: string, definitionPath?: string): Promise<void> {
    await this.commands.executeCommand('boxteam.settings.openSource', {
      location: { ...this.source.location, source_key: sourceKey ?? this.source.location.source_key }, groupId: this.groupId, definitionPath,
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
      this.editNode({ provider_id: id, ...(modelId === undefined ? {} : { model_id: modelId }) }, ['model_options', 'api_key'], credential);
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
    const label = kind === 'providers' ? t('configuration.catalog.newProvider') : await this.input(t('configuration.entities.newId'),
      value => /^[a-zA-Z0-9._-]{2,64}$/.test(value) ? undefined : t('configuration.entities.invalidId'), `agent-${generateUuid().slice(0, 8)}`);
    if (label === undefined) return;
    const id = kind === 'providers' ? `provider-${generateUuid()}` : label;
    if (kind === 'providers') {
      const profile = `provider-${generateUuid()}`;
      this.source.editDocumentPaths([{ path: ['profiles', profile], value: { extends: 'context:workspace', settings: { model_options: { api_mode: { protocol: 'chat_completions' } } } } },
        { path: ['settings', 'workspace', 'llm', 'providers', id], value: { name: label, config_ref: `#/profiles/${profile}`, models: {} } }]);
      this.rememberNode({ provider_id: id }, ['profiles', profile], {});
      this.selectedModelId = undefined;
    } else {
      const name = await this.input(t('configuration.entities.name')); if (name === undefined) return;
      const prompt = await this.input(t('configuration.entities.systemPrompt')); if (prompt === undefined) return;
      const activeProviders = definitionObject(definitionValue(this.source.document.source_effective, ['llm', 'providers']));
      const localProviders = definitionObject(definitionValue(this.source.readDraft(), ['settings', 'workspace', 'llm', 'providers']));
      const choices = Object.keys({ ...activeProviders, ...localProviders }).flatMap(providerId => {
        const saved = definitionObject(definitionObject(activeProviders[providerId]).models);
        const local = definitionObject(definitionObject(localProviders[providerId]).models);
        return Object.keys({ ...saved, ...local }).map(modelId => ({ label: `${providerId} / ${modelId}`,
          model: { provider_id: providerId, model_id: modelId } }));
      });
      const provider = await this.quick.pick(choices, { title: t('configuration.entities.primaryModel') }, this.lifetime.token);
      this.check(); if (!provider) return;
      if (this.source.model.textEditorModel!.getVersionId() !== version) throw new Error(t('configuration.entities.definitionChanged', id));
      const agents = definitionObject(definitionValue(this.source.readDraft(), ['settings', 'workspace', 'agents']));
      if (Object.hasOwn(agents, id)) throw new Error(t('configuration.entities.duplicateId', id));
      const inherited = Object.hasOwn(definitionObject(definitionValue(this.source.document.source_inherited, ['agents'])), id);
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
  private async addModel(providerId: string, modelName?: string, copyId?: string): Promise<void> {
    const document = this.source.document;
    if (!('workspace_id' in document)) throw new Error(t('configuration.entities.workspaceRequired'));
    const version = this.source.model.textEditorModel!.getVersionId();
    const path = ['llm', 'providers', providerId, 'models'];
    const saved = definitionObject(definitionValue(this.source.document.source_effective, path));
    const current = definitionObject(definitionValue(this.source.readDraft(), ['settings', 'workspace', ...path]));
    const preview = copyId === undefined ? undefined : await this.source.preflightDraft(this.lifetime.signal, { provider_id: providerId, model_id: copyId });
    this.check();
    if (version !== this.source.model.textEditorModel!.getVersionId()) throw new Error(t('configuration.entities.definitionChanged', providerId));
    const original = copyId === undefined ? undefined : definitionValue(preview?.candidate, [...path, copyId]);
    if (copyId !== undefined && !original) throw new Error(t('configuration.entities.definitionChanged', copyId));
    const label = original ? await this.input(t('configuration.entities.name')) : modelName;
    if (!label) return;
    const model = original ? { ...definitionObject(original), name: label } : { model: label };
    const id = /^[a-zA-Z0-9._-]{1,64}$/.test(label) && !Object.hasOwn(saved, label) && !Object.hasOwn(current, label)
      ? label : `model-${generateUuid()}`;
    if (version !== this.source.model.textEditorModel!.getVersionId()) throw new Error(t('configuration.entities.definitionChanged', id));
    const inspection = preview && 'model_options' in preview ? preview.model_options : undefined;
    this.createModelNode(providerId, id, model, inspection ? this.copyNode(inspection) : undefined);
    this.selectedId = providerId; this.selectedModelId = id; this.render();
  }
  private copyNode(inspection: ConfigProviderInspectionDTO | ConfigModelInspectionDTO): { extends?: string; settings: Record<string, unknown> } {
    if (inspection.config_ref.startsWith('context:')) return { extends: inspection.config_ref, settings: {} };
    const node = inspection.chain.find(item => item.node_id === inspection.node_id);
    const parent = inspection.chain.find(item => item.node_id === node?.parent_id);
    const settings = definitionObject(inspection.local_settings);
    const containsMask = (value: unknown): boolean => typeof value === 'string' ? value.startsWith('boxteam-redacted:')
      : value !== null && typeof value === 'object' && Object.values(value).some(containsMask);
    if (containsMask(settings)) throw new Error(t('configuration.node.copySecret'));
    const localPath = node?.source_key === this.source.document.source_key && node.definition_path.startsWith('/profiles/')
      ? node.definition_path.slice(1).split('/').map(part => part.replace(/~1/g, '/').replace(/~0/g, '~')) : undefined;
    const localParent = localPath ? definitionValue(this.source.readDraft(), [...localPath, 'extends']) : undefined;
    const extendsRef = typeof localParent === 'string' ? localParent : parent?.reference;
    return { ...(extendsRef === undefined ? {} : { extends: extendsRef }), settings };
  }
  private async copyProvider(providerId: string): Promise<void> {
    const version = this.source.model.textEditorModel!.getVersionId();
    const preview = await this.source.preflightDraft(this.lifetime.signal, { provider_id: providerId });
    this.check();
    if (!('provider_options' in preview) || !preview.provider_options) throw new Error(t('configuration.inspect.missing'));
    const original = definitionObject(definitionValue(preview.candidate, ['llm', 'providers', providerId]));
    const label = await this.input(t('configuration.entities.name'));
    if (!label) return;
    const modelNodes: { id: string; definition: Record<string, unknown>; node: { extends?: string; settings: Record<string, unknown> }; followsProvider: boolean }[] = [];
    for (const [id, model] of Object.entries(definitionObject(original.models))) {
      const result = await this.source.preflightDraft(this.lifetime.signal, { provider_id: providerId, model_id: id });
      this.check();
      if (!('model_options' in result) || !result.model_options) throw new Error(t('configuration.inspect.missing'));
      const node = this.copyNode(result.model_options);
      modelNodes.push({ id, definition: definitionObject(model), node, followsProvider: node.extends === `context:provider/${providerId}` });
    }
    if (version !== this.source.model.textEditorModel!.getVersionId()) throw new Error(t('configuration.entities.definitionChanged', providerId));
    const id = `provider-${generateUuid()}`, profile = `provider-${generateUuid()}`;
    const edits: { path: string[]; value: unknown }[] = [{ path: ['profiles', profile], value: this.copyNode(preview.provider_options) }];
    const models: Record<string, unknown> = {};
    for (const model of modelNodes) {
      const key = `model-${generateUuid()}`;
      if (model.followsProvider) model.node.extends = `context:provider/${id}`;
      edits.push({ path: ['profiles', key], value: model.node });
      models[model.id] = { ...model.definition, config_ref: `#/profiles/${key}` };
    }
    edits.push({ path: ['settings', 'workspace', 'llm', 'providers', id], value: { name: label, config_ref: `#/profiles/${profile}`, models } });
    this.source.editDocumentPaths(edits);
    this.selectedId = id; this.selectedModelId = undefined; this.render();
  }
  private async removeModel(providerId: string, modelId: string): Promise<void> {
    const confirmed = await this.dialogs.confirm({ type: 'warning', message: t('configuration.entities.confirmRemove', `${providerId} / ${modelId}`),
      detail: t('configuration.entities.removeModelBoundary'), primaryButton: t('configuration.entities.removeModel') });
    this.check(); if (confirmed.confirmed) this.source.editPath(['llm', 'providers', providerId, 'models', modelId], undefined);
  }
  private async remove(id: string): Promise<void> {
    const kind = this.kind;
    const result = await this.dialogs.confirm({ type: 'warning', message: t('configuration.entities.confirmRemove', id),
      detail: t(kind === 'providers' ? 'configuration.entities.removeProviderBoundary' : 'configuration.entities.removeAgentBoundary'),
      primaryButton: t(kind === 'providers' ? 'configuration.entities.removeProvider' : 'configuration.entities.removeAgent') });
    this.check(); if (!result.confirmed) return;
    this.source.editPath(kind === 'agents' ? ['agents', id] : ['llm', 'providers', id], undefined);
  }
  private async restoreProviders(): Promise<void> {
    const result = await this.dialogs.confirm({ type: 'warning', message: t('configuration.entities.restoreProviders'),
      detail: t('configuration.entities.restoreProvidersBoundary'), primaryButton: t('configuration.entities.restoreProviders') });
    this.check(); if (result.confirmed) this.source.editPath(['llm', 'providers'], undefined);
  }
}
