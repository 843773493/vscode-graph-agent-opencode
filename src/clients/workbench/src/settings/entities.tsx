import { t } from '../../../shared/core/i18n/index.js';
import { ConfigurationEntities, definitionObject, definitionValue, type DefinitionKind } from '../../../web/src/components/settings/ConfigurationEntities.js';
import styles from '../../../web/src/components/settings/configurationEntities.css?text';
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
import { listAgents, setWorkspaceDefaultAgent, setWorkspaceDefaultProvider } from '../../../web/src/api/workspace.js';
import type { SourceSettings } from './editor.js';

export interface DefinitionEditorOptions { definitionKind?: DefinitionKind; definitionId?: string }

/** 原 Settings Pane 的业务内容；所有字段直接编辑原来源模型。 */
export class ConfigurationEntityView extends Disposable {
  private readonly surface: ReactSurface;
  private readonly lifetime = this._register(createManagementTargetLifecycle(t('workbench.settings.reopenSource')));
  private kind: DefinitionKind = 'providers';
  private selectedId?: string;
  constructor(element: HTMLElement, private readonly source: SourceSettings, private readonly groupId: number,
    @IQuickInputService private readonly quick: IQuickInputService,
    @IDialogService private readonly dialogs: IDialogService,
    @INotificationService private readonly notifications: INotificationService,
    @IEditorService private readonly editors: IEditorService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService,
    @ICommandService private readonly commands: ICommandService) {
    super();
    this.surface = this._register(new ReactSurface(element, [styles]));
    this._register(source.onDidChange(() => this.render()));
  }
  get selection(): DefinitionEditorOptions { return { definitionKind: this.kind, definitionId: this.selectedId }; }
  show(kind: DefinitionKind, selectedId?: string): void { this.kind = kind; this.selectedId = selectedId; this.render(); }
  focus(): void { this.surface.focus('.configuration-entity-columns nav button, .configuration-entity-toolbar button'); }
  private run(action: () => void | Promise<void>): void {
    const report = (error: unknown) => { if (!this._store.isDisposed) this.notifications.error(error instanceof Error ? error : String(error)); };
    try { this.check(); Promise.resolve(action()).catch(report); } catch (error) { report(error); }
  }
  private check(): void { this.lifetime.signal.throwIfAborted(); this.lifetime.check(); }
  private render(): void {
    const document = this.source.document;
    if (!('workspace_id' in document)) throw new Error(t('configuration.entities.workspaceRequired'));
    let draft: Record<string, unknown> | undefined, error: string | undefined;
    try { draft = this.source.readDraft(); } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    this.surface.render(<ConfigurationEntities kind={this.kind} selectedId={this.selectedId} draft={draft} document={document} error={error}
      pendingSubmission={this.source.pendingSubmission}
      onQuerySubmission={() => this.run(() => this.source.querySubmission())}
      onSetSecret={id => this.run(() => this.setSecret(id))}
      onSelect={id => { this.selectedId = id; this.render(); }}
      onEdit={(id, path, value) => this.run(() => {
        const prefix = this.kind === 'agents' ? ['agents', id] : ['llm', 'providers', this.providerIndex(id)];
        this.source.editPath([...prefix, ...path], value);
      })}
      onCreate={() => this.run(() => this.create())} onRemove={id => this.run(() => this.remove(id))}
      onRestoreProviders={() => this.run(() => this.restoreProviders())}
      onSetDefaultAgent={id => this.run(() => this.source.editPath(['default_agent'], id))}
      onRestoreDefaultAgent={() => this.run(() => this.source.editPath(['default_agent'], undefined))}
      onInspectDefaultAgent={() => this.run(() => this.commands.executeCommand(defaultAgentSourceCommand, this.source.location))}
      onManageSessionDefaults={() => this.run(() => this.manageSessionDefaults())}
      onOpenSessionSelection={() => this.run(() => this.openSessionSelection())}
      onOpenSource={sourceKey => this.run(async () => {
        await this.commands.executeCommand('boxteam.settings.openSource', {
          location: { ...this.source.location, source_key: sourceKey ?? this.source.location.source_key }, groupId: this.groupId,
        });
      })}
      onSave={() => this.run(async () => {
        if (!await this.source.model.save()) throw new Error(t('configuration.entities.saveIncomplete'));
      })} />);
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
        agent.providers.find(provider => provider.workspace_default)?.provider_id ?? '',
        agent.workspace_default ? t('configuration.entities.workspaceDefaultAgent') : ''), agent })),
      { title: t('configuration.entities.manageSessionDefaults'),
        placeHolder: t('configuration.entities.sessionDefaultsBoundary', gateway_id, workspace_id) }, this.lifetime.token);
    this.check(); if (!selected) return;
    const { agent } = selected;
    const action = await this.quick.pick([
      { label: t('configuration.entities.useWorkspaceDefaultAgent'), description: agent.agent_id, providerId: undefined as string | undefined },
      ...agent.providers.map(provider => ({ label: provider.provider_id, description: provider.model,
        detail: t('configuration.entities.useWorkspaceDefaultProvider', agent.agent_id), providerId: provider.provider_id })),
    ], { title: t('configuration.entities.manageSessionDefaults'),
      placeHolder: t('configuration.entities.sessionDefaultsBoundary', gateway_id, workspace_id) }, this.lifetime.token);
    this.check(); if (!action) return;
    const confirmed = await this.dialogs.confirm({ type: 'question', message: action.providerId === undefined
      ? t('configuration.entities.useWorkspaceDefaultAgent') : t('configuration.entities.useWorkspaceDefaultProvider', agent.agent_id),
      detail: `${gateway_id} · ${workspace_id} · ${agent.agent_id}${action.providerId === undefined ? '' : ' · ' + action.providerId}\n`
        + t('configuration.entities.sessionDefaultsSaveBoundary'), primaryButton: t('configuration.entities.confirmSessionDefault') });
    this.check(); if (!confirmed.confirmed) return;
    const summary = (value: Awaited<ReturnType<typeof listAgents>>) => t('configuration.entities.sessionDefaultsCurrent',
      value.find(item => item.workspace_default)?.agent_id ?? '', agent.agent_id,
      value.find(item => item.agent_id === agent.agent_id)?.providers.find(item => item.workspace_default)?.provider_id ?? '');
    try {
      const updated = action.providerId === undefined ? await setWorkspaceDefaultAgent(0, agent.agent_id, target)
        : await setWorkspaceDefaultProvider(0, agent.agent_id, action.providerId, target);
      this.notifications.info(summary(updated));
    } catch (error) {
      // 失败仅重读原 owner，不重放写入；原回执和重读错误都保留。
      const latest = await listAgents(0, target).catch(readError => { throw new AggregateError([error, readError],
        [error, readError].map(cause => cause instanceof Error ? cause.message : String(cause)).join('\n')); });
      throw new Error(`${error instanceof Error ? error.message : String(error)}\n${summary(latest)}`, { cause: error });
    }
  }
  private async setSecret(id: string): Promise<void> {
    if (this.source.pendingSubmission) { await this.source.querySubmission(); return; }
    if (this.source.model.isDirty()) {
      const accepted = await this.dialogs.confirm({ type: 'question', message: t('configuration.entities.secretSettleDraft'),
        detail: t('configuration.entities.secretSettleDetail'), primaryButton: t('configuration.entities.save') });
      this.check();
      if (!accepted.confirmed || !await this.source.model.save() || this.source.model.isDirty()) return;
    }
    const version = this.source.model.textEditorModel!.getVersionId();
    let password = await this.quick.input({ title: t('configuration.entities.setSecret'), password: true,
      prompt: t('configuration.entities.secretTarget', this.source.location.gateway_id, this.source.location.workspace_id!, id),
      validateInput: async value => value ? undefined : t('configuration.entities.required') }, this.lifetime.token);
    try {
      this.check();
      if (password === undefined) return;
      await this.source.saveProviderSecret(id, password, version, () => this.check());
    } finally { password = undefined; }
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
  private providerIndex(id: string): number {
    const providers = definitionValue(this.source.readDraft(), ['llm', 'providers']);
    const index = Array.isArray(providers) ? providers.findIndex(value => definitionObject(value).id === id) : -1;
    if (index < 0) throw new Error(t('configuration.entities.definitionChanged', id));
    return index;
  }
  private inheritedProviders(): unknown[] {
    const document = this.source.document;
    const selected = document.layers.find(layer => layer.source_key === document.source_key);
    for (const layer of [...document.layers].sort((a, b) => b.precedence - a.precedence)) {
      if (!selected || layer.precedence >= selected.precedence) continue;
      const value = definitionValue(layer.payload, ['llm', 'providers']);
      if (Array.isArray(value)) return value;
    }
    return [];
  }
  private async input(prompt: string, validate?: (value: string) => string | undefined): Promise<string | undefined> {
    const value = await this.quick.input({ title: t(this.kind === 'providers' ? 'configuration.entities.addProvider' : 'configuration.entities.addAgent'),
      prompt, validateInput: async value => validate?.(value) ?? (!value.trim() ? t('configuration.entities.required') : undefined) }, this.lifetime.token);
    this.check();
    return value?.trim();
  }
  private async create(): Promise<void> {
    const kind = this.kind;
    const id = await this.input(t('configuration.entities.newId'), value => /^[a-zA-Z0-9._-]{2,64}$/.test(value) ? undefined : t('configuration.entities.invalidId'));
    if (id === undefined) return;
    if (kind === 'providers') {
      const model = await this.input(t('configuration.entities.model')); if (model === undefined) return;
      const endpoint = await this.input(t('configuration.entities.endpoint')); if (endpoint === undefined) return;
      const handler = await this.input(t('configuration.entities.handler')); if (handler === undefined) return;
      const protocol = await this.quick.pick(['chat_completions', 'responses', 'anthropic_messages'].map(label => ({ label })),
        { title: t('configuration.entities.chooseProtocol') }, this.lifetime.token);
      this.check(); if (!protocol) return;
      const apiKey = await this.input(t('configuration.entities.environmentReference'), value => /^\$\{[A-Za-z_][A-Za-z0-9_]*\}$|^env:[A-Za-z_][A-Za-z0-9_]*$/.test(value) ? undefined : t('configuration.entities.environmentRequired'));
      if (apiKey === undefined) return;
      const current = definitionValue(this.source.readDraft(), ['llm', 'providers']);
      const providers = Array.isArray(current) ? current : [];
      if (providers.some(value => definitionObject(value).id === id)) throw new Error(t('configuration.entities.duplicateId', id));
      if (!Array.isArray(current)) {
        const version = this.source.model.textEditorModel!.getVersionId();
        const hidden = this.inheritedProviders().map(value => String(definitionObject(value).id)).join(', ');
        const result = await this.dialogs.confirm({ type: 'warning', message: t('configuration.entities.confirmProviderOverride'),
          detail: t('configuration.entities.hiddenProviders', hidden), primaryButton: t('configuration.entities.addProvider') });
        this.check(); if (!result.confirmed) return;
        if (this.source.model.textEditorModel!.getVersionId() !== version) throw new Error(t('configuration.entities.definitionChanged', id));
      }
      // 凭据仅允许显式引用；字面秘密仍走单独的非持久化密码流程。
      const supportsReasoning = protocol.label === 'responses' ? { reasoning_items: { summary: false, encrypted_content: false } }
        : protocol.label === 'anthropic_messages' ? { thinking_blocks: { thinking: false, redacted_thinking: false } } : { reasoning_content: false };
      this.source.editPath(['llm', 'providers'], [...providers, { id, model, endpoint, custom_llm_provider: handler, api_key: apiKey,
        api_mode: { protocol: protocol.label, model_info: {}, supports_reasoning: supportsReasoning } }]);
    } else {
      const name = await this.input(t('configuration.entities.name')); if (name === undefined) return;
      const prompt = await this.input(t('configuration.entities.systemPrompt')); if (prompt === undefined) return;
      const providers = definitionValue(this.source.readDraft(), ['llm', 'providers']) ?? this.inheritedProviders();
      const provider = await this.quick.pick((Array.isArray(providers) ? providers : []).map(value => ({ label: String(definitionObject(value).id) })),
        { title: t('configuration.entities.primaryProvider') }, this.lifetime.token);
      this.check(); if (!provider) return;
      const agents = definitionObject(this.source.readDraft().agents);
      if (Object.hasOwn(agents, id)) throw new Error(t('configuration.entities.duplicateId', id));
      const document = this.source.document;
      const selected = document.layers.find(layer => layer.source_key === document.source_key);
      const inherited = document.layers.some(layer => selected && layer.precedence < selected.precedence
        && Object.hasOwn(definitionObject(layer.payload?.agents), id));
      if (inherited) {
        const version = this.source.model.textEditorModel!.getVersionId();
        const confirmed = await this.dialogs.confirm({ type: 'warning', message: t('configuration.entities.overrideInheritedAgent', id),
          detail: t('configuration.entities.overrideAgentBoundary'), primaryButton: t('configuration.entities.addAgent') });
        this.check(); if (!confirmed.confirmed) return;
        if (this.source.model.textEditorModel!.getVersionId() !== version) throw new Error(t('configuration.entities.definitionChanged', id));
      }
      this.source.editPath(['agents', id], { name, instructions: { system_prompt: prompt, ...(inherited ? { system_prompt_file: null } : {}) },
        model: { primary_provider: provider.label } });
    }
    this.selectedId = id; this.render();
  }
  private async remove(id: string): Promise<void> {
    const kind = this.kind;
    const result = await this.dialogs.confirm({ type: 'warning', message: t('configuration.entities.confirmRemove', id),
      detail: t(kind === 'providers' ? 'configuration.entities.removeProviderBoundary' : 'configuration.entities.removeAgentBoundary'),
      primaryButton: t('configuration.entities.removeLocal') });
    this.check(); if (!result.confirmed) return;
    this.source.editPath(kind === 'agents' ? ['agents', id] : ['llm', 'providers', this.providerIndex(id)], undefined);
  }
  private async restoreProviders(): Promise<void> {
    const result = await this.dialogs.confirm({ type: 'warning', message: t('configuration.entities.restoreProviders'),
      detail: t('configuration.entities.restoreProvidersBoundary'), primaryButton: t('configuration.entities.restoreProviders') });
    this.check(); if (result.confirmed) this.source.editPath(['llm', 'providers'], undefined);
  }
}
