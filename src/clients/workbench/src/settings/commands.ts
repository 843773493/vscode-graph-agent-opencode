import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Action2, MenuId, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceContext } from '../workspaceContext.js';
import { createManagementTargetLifecycle, managementGateway } from '../managementTarget.js';
import { manageDefaultAgentSource } from './defaultAgent.js';
import { configureServicePlugin } from './pluginConfiguration.js';
import { ContextKeyExpr } from '../../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { SettingsEditor2Input } from '../../upstream/src/vs/workbench/services/preferences/common/preferencesEditorInput.js';
import { captureSettingsLocation, parseSettingsLocation } from './editor.js';
import { IFileService } from '../../upstream/src/vs/platform/files/common/files.js';
import type { IUntypedFileEditorInput } from '../../upstream/src/vs/workbench/common/editor.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IEditorGroupsService, preferredSideBySideGroupDirection } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { IConfigurationService } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import type { ITextEditorOptions } from '../../upstream/src/vs/platform/editor/common/editor.js';
import type { TargetSettingsLocation } from './scope.js';
import { ConfigurationSourceOwner, sourceStatus } from './sourceOwner.js';
import { configSourceFiles, configSourceScheme } from './sourceEditor.js';
import { t } from '../../../shared/core/i18n/index.js';
import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';
import type { ConfigSourceLayer } from '../../../web/src/types/protocol.js';

function commandTitle(key: keyof typeof actionTitles): { value: string; original: string } {
  return { value: t(key), original: actionTitles[key] };
}

const sourceLayerLabels: Record<ConfigSourceLayer, string> = {
  inline: t('workbench.settings.layer.inline'),
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
}

registerAction2(class extends Action2 {
  constructor() {
    super({ id: 'boxteam.settings.pluginParameters', title: commandTitle('workbench.settings.openPluginParameters'),
      category: { value: t('workbench.settings.openPluginParametersCategory'), original: actionTitles['workbench.settings.openPluginParametersCategory'] }, f1: true });
  }
  run(accessor: ServicesAccessor): Promise<void> { return configureServicePlugin(accessor); }
});

registerAction2(class extends Action2 {
  constructor() {
    super({ id: defaultAgentSourceCommand, title: commandTitle('workbench.settings.openDefaultAgentSource'),
      category: { value: t('workbench.settings.openPluginParametersCategory'), original: actionTitles['workbench.settings.openPluginParametersCategory'] }, f1: true });
  }
  async run(accessor: ServicesAccessor, explicit?: WorkspaceOwner): Promise<void> {
    const quickInput = accessor.get(IQuickInputService), notifications = accessor.get(INotificationService);
    const initial = workspaceContext.getSnapshot();
    if (initial.loading || initial.error || !initial.gatewayId) throw new Error(initial.error ?? t('workbench.settings.gatewayPending'));
    const controller = new AbortController(), cancellation = new CancellationTokenSource();
    const invalidate = () => { controller.abort(new Error(t('workbench.settings.identityChanged'))); cancellation.cancel(); };
    const release = onGatewayTokenInvalidated(0, invalidate);
    const releaseContext = workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate(); });
    let selected: WorkspaceOwner | undefined;
    const check = () => {
      if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate();
      controller.signal.throwIfAborted();
      if (selected && requireGatewayWorkspaceRequestTarget(workspaceContext.getSnapshot().workspaces, selected).status !== 'ready') {
        throw new Error(t('workbench.settings.targetOffline'));
      }
    };
    try {
      const target = managementGateway('settings', explicit?.gatewayId);
      selected = explicit ? { ...explicit } : (await quickInput.pick(initial.workspaces.filter(workspace => workspace.gateway_id === target.gateway_id).map(workspace => ({
        label: workspace.name, description: `${workspace.gateway_id} · ${workspace.status}`, detail: workspace.workspace_id,
        owner: gatewayWorkspaceOwner(workspace),
      })), { title: t('workbench.settings.pickTarget'), prompt: t('workbench.settings.targetPrompt', target.name, target.gateway_id),
        placeHolder: t('workbench.settings.pickTargetHint') }, cancellation.token))?.owner;
      if (!selected) return;
      check();
      const workspace = requireGatewayWorkspaceRequestTarget(workspaceContext.getSnapshot().workspaces, selected);
      await manageDefaultAgentSource(workspace, workspace.name, quickInput, notifications, check, controller.signal);
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
    const groups = accessor.get(IEditorGroupsService), configuration = accessor.get(IConfigurationService);
    const groupId = request?.groupId ?? groups.activeGroup.id;
    const explicitLocation = request && parseSettingsLocation(request.location);
    const openToSide = request?.openToSide, editorOptions = { ...(request?.editorOptions ?? { pinned: true }) };
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
      const direction = preferredSideBySideGroupDirection(configuration);
      const group = openToSide ? groups.findGroup({ direction }, source) ?? groups.addGroup(source, direction) : source;
      const resource = configSourceFiles(files).prepareEditor(location, location.source_key, document);
      const input = { resource, label: location.source_key + '.jsonc', description: `${location.gateway_id} · ${location.name}`,
        options: editorOptions, languageId: 'jsonc', forceFile: true } satisfies IUntypedFileEditorInput;
      await editors.openEditor(input, group.id);
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
  async run(accessor: ServicesAccessor): Promise<void> {
    const parent = accessor.get(IInstantiationService), editors = accessor.get(IEditorService);
    const cancellation = new CancellationTokenSource();
    try {
      const captured = await captureSettingsLocation(parent, cancellation.token);
      if (captured) await editors.openEditor(parent.createInstance(SettingsEditor2Input, captured.location, captured.document), { pinned: true });
    } finally { cancellation.dispose(); }
  }
});
