import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Action2, MenuId, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner, type GatewayRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceContext } from '../workspaceContext.js';
import { createManagementTargetLifecycle, managementGateway, managementWorkspace, managementTargetDisplayName, type ManagementTarget } from '../managementTarget.js';
import { inspectDefaultAgentSource } from './defaultAgent.js';
import { configureServicePlugin, type PluginConfigurationTarget } from './pluginConfiguration.js';
import { ContextKeyExpr } from '../../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { SettingsEditor2Input } from '../../upstream/src/vs/workbench/services/preferences/common/preferencesEditorInput.js';
import { captureSettingsLocation, parseSettingsLocation } from './editor.js';
import { IFileService } from '../../upstream/src/vs/platform/files/common/files.js';
import type { IUntypedFileEditorInput } from '../../upstream/src/vs/workbench/common/editor.js';
import { IEditorService, ACTIVE_GROUP, SIDE_GROUP } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IEditorGroupsService } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import type { ISettingsEditorOptions } from '../../upstream/src/vs/workbench/services/preferences/common/preferences.js';
import type { ITextEditorOptions } from '../../upstream/src/vs/platform/editor/common/editor.js';
import type { TargetSettingsLocation } from './scope.js';
import { ConfigurationSourceOwner, sourceStatus } from './sourceOwner.js';
import { configSourceFiles, configSourceScheme } from './sourceEditor.js';
import { t } from '../../../shared/core/i18n/index.js';
import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';
import type { ConfigSourceLayer } from '../../../web/src/types/protocol.js';
import type { DefinitionEditorOptions } from './entities.js';
import { manageSecrets } from './secrets.js';
import { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import { IStorageService } from '../../upstream/src/vs/platform/storage/common/storage.js';
import { findNodeAtLocation, parseTree } from '../../upstream/src/vs/base/common/json.js';
import { ITextModelService } from '../../upstream/src/vs/editor/common/services/resolverService.js';
import './gatewayCommands.js';

registerAction2(class extends Action2 {
  constructor() {
    super({ id: 'boxteam.settings.secrets', title: { value: t('settings.secrets.manage'), original: 'Manage API Keys' }, f1: true });
  }
  async run(accessor: ServicesAccessor, target?: GatewayRequestTarget): Promise<void> {
    const quick = accessor.get(IQuickInputService), dialogs = accessor.get(IDialogService);
    await workspaceContext.ensureReady();
    await manageSecrets(target ?? managementGateway('settings', workspaceContext.getSnapshot().gatewayId!), quick, dialogs, accessor.get(IStorageService));
  }
});

function commandTitle(key: keyof typeof actionTitles): { value: string; original: string } {
  return { value: t(key), original: actionTitles[key] };
}

const sourceLayerLabels: Record<ConfigSourceLayer, string> = {
  inline: t('workbench.settings.layer.inline'),
  source: t('workbench.settings.layer.source'),
  user: t('workbench.settings.layer.user'),
  user_local: t('workbench.settings.layer.userLocal'),
  workspace: t('workbench.settings.layer.workspace'),
  runtime_override: t('workbench.settings.layer.runtimeOverride'),
};

export const defaultAgentSourceCommand = 'boxteam.settings.defaultAgentSource';
export const openConfigurationSourceCommand = 'boxteam.settings.openSource';
export interface ConfigurationSourceOpenRequest {
  location: TargetSettingsLocation;
  groupId?: number;
  openToSide?: boolean;
  editorOptions?: ITextEditorOptions;
  definitionPath?: string;
}

registerAction2(class extends Action2 {
  constructor() {
    super({ id: 'boxteam.settings.pluginParameters', title: commandTitle('workbench.settings.openPluginParameters'),
      category: { value: t('workbench.settings.openPluginParametersCategory'), original: actionTitles['workbench.settings.openPluginParametersCategory'] }, f1: true });
  }
  run(accessor: ServicesAccessor, target?: PluginConfigurationTarget): Promise<void> { return configureServicePlugin(accessor, target); }
});

registerAction2(class extends Action2 {
  constructor() {
    super({ id: defaultAgentSourceCommand, title: commandTitle('workbench.settings.openDefaultAgentSource'),
      category: { value: t('workbench.settings.openPluginParametersCategory'), original: actionTitles['workbench.settings.openPluginParametersCategory'] }, f1: true });
  }
  async run(accessor: ServicesAccessor, explicit?: WorkspaceOwner | TargetSettingsLocation): Promise<void> {
    const quickInput = accessor.get(IQuickInputService), notifications = accessor.get(INotificationService);
    const commandService = accessor.get(ICommandService);
    const initial = workspaceContext.getSnapshot();
    if (initial.loading || initial.error || !initial.gatewayId) throw new Error(initial.error ?? t('workbench.settings.gatewayPending'));
    const explicitLocation = explicit && 'source_key' in explicit ? parseSettingsLocation(explicit) : undefined;
    const explicitOwner = explicit && 'workspaceId' in explicit ? explicit : undefined;
    const controller = new AbortController(), cancellation = new CancellationTokenSource();
    const invalidate = () => { controller.abort(new Error(t('workbench.settings.identityChanged'))); cancellation.cancel(); };
    const release = onGatewayTokenInvalidated(0, invalidate);
    const releaseContext = workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate(); });
    let selected: WorkspaceOwner | undefined = explicitLocation ? { gatewayId: explicitLocation.gateway_id, workspaceId: explicitLocation.workspace_id! } : undefined;
    const check = () => {
      if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate();
      controller.signal.throwIfAborted();
      if (selected && requireGatewayWorkspaceRequestTarget(workspaceContext.getSnapshot().workspaces, selected).status !== 'ready') {
        throw new Error(t('workbench.settings.targetOffline'));
      }
    };
    try {
      const target = managementGateway('settings', explicitLocation?.gateway_id ?? explicitOwner?.gatewayId);
      selected ??= explicitOwner ? { ...explicitOwner } : (await quickInput.pick(initial.workspaces.filter(workspace => workspace.gateway_id === target.gateway_id).map(workspace => ({
        label: workspace.name, description: `${workspace.gateway_id} · ${workspace.status}`, detail: workspace.workspace_id,
        owner: gatewayWorkspaceOwner(workspace),
      })), { title: t('workbench.settings.pickTarget'), prompt: t('workbench.settings.targetPrompt', managementTargetDisplayName(target), target.gateway_id),
        placeHolder: t('workbench.settings.pickTargetHint') }, cancellation.token))?.owner;
      if (!selected) return;
      check();
      const workspace = requireGatewayWorkspaceRequestTarget(workspaceContext.getSnapshot().workspaces, selected);
      if (workspace.status !== 'ready') throw new Error(t('workbench.settings.targetOffline'));
      if (explicitLocation) {
        await inspectDefaultAgentSource(explicitLocation, quickInput, notifications, check, controller.signal);
        return;
      }
      const owner = new ConfigurationSourceOwner({ gateway_id: target.gateway_id,
        gateway_connection_id: target.gateway_connection_id, workspace_id: workspace.workspace_id });
      const listed = await owner.list(controller.signal);
      check();
      const source = await quickInput.pick(listed.sources.filter(item => item.source_key).map(item => ({
        label: item.source_key!, description: t('settings.editor.layerPresencePriority', item.layer, item.presence, item.precedence),
        detail: item.uri ?? undefined, source_key: item.source_key!,
      })), { title: t('settings.editor.chooseDefaultAgentSource'), prompt: t('settings.editor.defaultAgentOwner', workspace.name,
        target.gateway_id, workspace.workspace_id), placeHolder: t('settings.editor.defaultAgentAccess') }, cancellation.token);
      check();
      if (!source) return;
      const location: TargetSettingsLocation = { ...owner.target, version: 2, source_key: source.source_key, name: workspace.name };
      await commandService.executeCommand('boxteam.settings.agents', location);
    } finally { release(); releaseContext(); cancellation.dispose(); controller.abort(); }
  }
});

registerAction2(class extends Action2 {
  constructor() {
    super({ id: openConfigurationSourceCommand, title: commandTitle('workbench.settings.openSource'),
      category: { value: t('workbench.settings.openPluginParametersCategory'), original: actionTitles['workbench.settings.openPluginParametersCategory'] }, f1: true });
  }
  async run(accessor: ServicesAccessor, request?: ConfigurationSourceOpenRequest): Promise<void> {
    const parent = accessor.get(IInstantiationService), editors = accessor.get(IEditorService), files = accessor.get(IFileService);
    const groups = accessor.get(IEditorGroupsService);
    const groupId = request?.editorOptions?.sourceGroup ?? request?.groupId ?? groups.activeGroup.id;
    const explicitLocation = request && parseSettingsLocation(request.location);
    const openToSide = request?.openToSide, editorOptions = { ...(request?.editorOptions ?? { pinned: true }), sourceGroup: groupId };
    const lifecycle = createManagementTargetLifecycle(t('workbench.settings.reopenSource'));
    try {
      const captured = explicitLocation ? { location: explicitLocation,
        document: await new ConfigurationSourceOwner(explicitLocation).read(explicitLocation.source_key, undefined, lifecycle.signal) }
        : await captureSettingsLocation(parent, lifecycle.token);
      lifecycle.check();
      if (!captured) return;
      const { location, document } = captured;
      const source = groups.getGroup(groupId);
      if (!source) throw new Error(t('workbench.settings.originGroupClosed'));
      const group = openToSide ? SIDE_GROUP : request?.groupId ?? ACTIVE_GROUP;
      const resource = configSourceFiles(files).prepareEditor(location, location.source_key, document);
      if (request?.definitionPath) {
        const reference = await accessor.get(ITextModelService).createModelReference(resource);
        try {
          lifecycle.check();
          const model = reference.object.textEditorModel;
          const path = request.definitionPath.split('/').slice(1).map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'));
          const node = findNodeAtLocation(parseTree(model.getValue()), path);
          if (!node) throw new Error(t('settings.editor.definitionMissing', request.definitionPath));
          const start = model.getPositionAt(node.offset), end = model.getPositionAt(node.offset + node.length);
          editorOptions.selection = { startLineNumber: start.lineNumber, startColumn: start.column,
            endLineNumber: end.lineNumber, endColumn: end.column };
        } finally { reference.dispose(); }
      }
      const input = { resource,
        options: editorOptions, languageId: 'jsonc', forceFile: true } satisfies IUntypedFileEditorInput;
      await editors.openEditor(input, group);
    } finally { lifecycle.dispose(); }
  }
});

for (const operation of ['sourceStatus', 'rebaseSource'] as const) registerAction2(class extends Action2 {
  constructor() {
    const title = operation === 'sourceStatus'
      ? commandTitle('workbench.settings.inspectSourceStatus')
      : commandTitle('workbench.settings.rebaseSourceDraft');
    super({ id: `boxteam.settings.${operation}`, title,
    category: { value: t('workbench.settings.openPluginParametersCategory'), original: actionTitles['workbench.settings.openPluginParametersCategory'] }, f1: true,
    precondition: ContextKeyExpr.equals('resourceScheme', configSourceScheme),
    menu: { id: MenuId.EditorTitle, when: ContextKeyExpr.equals('resourceScheme', configSourceScheme), group: 'navigation', order: operation === 'sourceStatus' ? 0 : 1 } });
  }
  async run(accessor: ServicesAccessor): Promise<void> {
    const files = accessor.get(IFileService), editors = accessor.get(IEditorService), quickInput = accessor.get(IQuickInputService);
    const resource = editors.activeEditor?.resource;
    if (resource?.scheme !== configSourceScheme) throw new Error(t('workbench.settings.selectSource'));
    const provider = configSourceFiles(files);
    if (operation === 'rebaseSource') { await provider.rebase(resource); return; }
    const document = await provider.query(resource);
    await quickInput.pick([
      { label: t('workbench.settings.sourceSaved'), detail: sourceStatus(document) },
      { label: t('workbench.settings.sourceBaseline'), detail: t('workbench.settings.revisionDigest', document.file_digest ?? t('workbench.settings.missingValue'), document.layer_revision ?? t('workbench.settings.missingValue'), document.layer_digest ?? t('workbench.settings.missingValue'), document.active_revision ?? t('workbench.settings.missingValue'), document.active_digest ?? t('workbench.settings.missingValue')) },
      { label: t('workbench.settings.appliedDeferred'), detail: t('workbench.settings.appliedDeferredValues', document.status?.applied_paths.join(t('workbench.settings.listSeparator')) || t('workbench.settings.missingValue'), document.status?.deferred_paths.join(t('workbench.settings.listSeparator')) || t('workbench.settings.missingValue')) },
      ...document.layers.map(layer => ({ label: t('workbench.settings.sourceLayer', layer.source_key), description: t('workbench.settings.layerDescription', sourceLayerLabels[layer.layer as ConfigSourceLayer] ?? layer.layer, layer.precedence, t(layer.writable ? 'workbench.settings.writable' : 'workbench.settings.readOnly')), detail: JSON.stringify(layer.payload) })),
      ...document.origins.map(origin => ({ label: origin.path, description: origin.source_key, detail: t('workbench.settings.sourceOrigin', origin.shadowed_sources.join(t('workbench.settings.listSeparator')) || t('workbench.settings.missingValue')) })),
    ], { title: t('workbench.settings.statusTitle'), prompt: `${'workspace_id' in document ? `${document.gateway_id} · ${document.workspace_id}` : t('workbench.settings.gatewayScope', document.gateway_id)} · ${document.source_key}` });
  }
});

registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.settings.openTarget', title: commandTitle('workbench.settings.openTarget'),
    category: { value: t('workbench.settings.openPluginParametersCategory'), original: actionTitles['workbench.settings.openPluginParametersCategory'] }, f1: true }); }
  async run(accessor: ServicesAccessor, target?: ManagementTarget, scope?: { workspace_id?: string; source_key?: string; source_layer?: 'user' | 'workspace' }, options?: DefinitionEditorOptions): Promise<void> {
    const parent = accessor.get(IInstantiationService), editors = accessor.get(IEditorService);
    const groups = accessor.get(IEditorGroupsService), groupId = groups.activeGroup.id;
    const frozen = { ...(target ?? managementGateway('settings')) };
    const explicit = scope ? { ...scope } : undefined;
    const lifecycle = createManagementTargetLifecycle(t('workbench.settings.reopenSource'));
    try {
      const captured = await captureSettingsLocation(parent, lifecycle.token, frozen, explicit);
      lifecycle.check();
      if (!captured) return;
      if (!groups.getGroup(groupId)) throw new Error(t('workbench.settings.originGroupClosed'));
      await editors.openEditor(parent.createInstance(SettingsEditor2Input, captured.location, captured.document), { ...options, pinned: true, sourceGroup: groupId });
    } finally { lifecycle.dispose(); }
  }
});

// 两个实体入口只选择原 Settings 输入的内容，不建立专用输入或另一份配置草稿。
for (const kind of ['providers', 'agents'] as const) registerAction2(class extends Action2 {
  constructor() { super({ id: kind === 'providers' ? 'boxteam.settings.modelsProviders' : 'boxteam.settings.agents',
    title: kind === 'providers' ? commandTitle('workbench.settings.modelsProviders') : commandTitle('workbench.settings.agents'),
    category: { value: t('workbench.settings.openPluginParametersCategory'), original: actionTitles['workbench.settings.openPluginParametersCategory'] }, f1: true }); }
  async run(accessor: ServicesAccessor, location?: TargetSettingsLocation | WorkspaceOwner, definitionId?: string, definitionModelId?: string): Promise<void> {
    const parent = accessor.get(IInstantiationService), editors = accessor.get(IEditorService);
    const groups = accessor.get(IEditorGroupsService);
    const groupId = groups.activeGroup.id;
    const frozen = location && 'source_key' in location ? parseSettingsLocation(location) : undefined;
    if (frozen && !frozen.workspace_id) throw new Error(t('configuration.entities.workspaceRequired'));
    const explicitOwner = location && 'workspaceId' in location ? { ...location } : undefined;
    const target = frozen ?? managementGateway('settings', explicitOwner?.gatewayId);
    const lifecycle = createManagementTargetLifecycle(t('workbench.settings.reopenSource'));
    try {
      await workspaceContext.ensureReady();
      lifecycle.check();
      // 全局入口直接进入后端默认来源；精确来源和会话入口保持原归属。
      const workspace = frozen?.workspace_id ? frozen : explicitOwner ? { workspace_id: explicitOwner.workspaceId }
        : managementWorkspace('settings', target);
      if (!workspace) throw new Error(t('plugins.noWorkspaces'));
      const captured = await captureSettingsLocation(parent, lifecycle.token, target, { workspace_id: workspace.workspace_id,
        source_key: frozen?.source_key, source_layer: explicitOwner ? 'workspace' : 'user' });
      lifecycle.check();
      if (!captured) return;
      if (!groups.getGroup(groupId)) throw new Error(t('workbench.settings.originGroupClosed'));
      const options: ISettingsEditorOptions & DefinitionEditorOptions = { pinned: true, sourceGroup: groupId, definitionKind: kind, definitionId, definitionModelId };
      await editors.openEditor(parent.createInstance(SettingsEditor2Input, captured.location, captured.document),
        options);
    } finally { lifecycle.dispose(); }
  }
});
