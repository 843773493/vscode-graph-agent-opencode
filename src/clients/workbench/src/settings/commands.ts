import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Action2, MenuId, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { onGatewayUserSessionInvalidated } from '../../../web/src/api/http.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceContext } from '../workspaceContext.js';
import { managementGateway } from '../managementTarget.js';
import { manageDefaultAgentSource } from './defaultAgent.js';
import { configureServicePlugin } from './pluginConfiguration.js';
import { ContextKeyExpr } from '../../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { SettingsEditor2Input } from '../../upstream/src/vs/workbench/services/preferences/common/preferencesEditorInput.js';
import { captureSettingsLocation } from './editor.js';
import { IFileService } from '../../upstream/src/vs/platform/files/common/files.js';
import type { IUntypedFileEditorInput } from '../../upstream/src/vs/workbench/common/editor.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { ConfigurationSourceOwner, sourceStatus } from './sourceOwner.js';
import { configSourceFiles, configSourceScheme } from './sourceEditor.js';

export const defaultAgentSourceCommand = 'boxteam.settings.defaultAgentSource';

registerAction2(class extends Action2 {
  constructor() {
    super({ id: 'boxteam.settings.pluginParameters', title: { value: '编辑目标插件参数', original: 'Edit Target Plugin Parameters' },
      category: { value: 'BoxTeam 设置', original: 'BoxTeam Settings' }, f1: true });
  }
  run(accessor: ServicesAccessor): Promise<void> { return configureServicePlugin(accessor); }
});

registerAction2(class extends Action2 {
  constructor() {
    super({ id: defaultAgentSourceCommand, title: { value: '编辑目标工作区默认 Agent 来源', original: 'Edit Target Workspace Default Agent Source' },
      category: { value: 'BoxTeam 设置', original: 'BoxTeam Settings' }, f1: true });
  }
  async run(accessor: ServicesAccessor, explicit?: WorkspaceOwner): Promise<void> {
    const quickInput = accessor.get(IQuickInputService), notifications = accessor.get(INotificationService);
    const initial = workspaceContext.getSnapshot();
    if (initial.loading || initial.error || !initial.gatewayId) throw new Error(initial.error ?? '工作区目标尚未完成初始化');
    const controller = new AbortController(), cancellation = new CancellationTokenSource();
    const invalidate = () => { controller.abort(new Error('Gateway 或用户身份已改变，请重新选择配置目标')); cancellation.cancel(); };
    const release = onGatewayUserSessionInvalidated(0, invalidate);
    const releaseContext = workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate(); });
    let selected: WorkspaceOwner | undefined;
    const check = () => {
      if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate();
      controller.signal.throwIfAborted();
      if (selected && requireGatewayWorkspaceRequestTarget(workspaceContext.getSnapshot().workspaces, selected).status !== 'ready') {
        throw new Error('原配置目标已离线，请先显式连接或启动 Backend');
      }
    };
    try {
      const target = managementGateway('settings', explicit?.gatewayId);
      selected = explicit ? { ...explicit } : (await quickInput.pick(initial.workspaces.filter(workspace => workspace.gateway_id === target.gateway_id).map(workspace => ({
        label: workspace.name, description: `${workspace.gateway_id} · ${workspace.status}`, detail: workspace.workspace_id,
        owner: gatewayWorkspaceOwner(workspace),
      })), { title: '选择业务配置目标', prompt: `管理目标：${target.name} · ${target.gateway_id}`,
        placeHolder: '选择当前设置目标内的工作区来源；未就绪工作区需先显式启动' }, cancellation.token))?.owner;
      if (!selected) return;
      check();
      const workspace = requireGatewayWorkspaceRequestTarget(workspaceContext.getSnapshot().workspaces, selected);
      await manageDefaultAgentSource(workspace, workspace.name, quickInput, notifications, check, controller.signal);
    } finally { release(); releaseContext(); cancellation.dispose(); controller.abort(); }
  }
});

registerAction2(class extends Action2 {
  constructor() {
    super({ id: 'boxteam.settings.openSource', title: { value: '打开目标设置来源 JSONC', original: 'Open Target Settings Source JSONC' },
      category: { value: 'BoxTeam 设置', original: 'BoxTeam Settings' }, f1: true });
  }
  async run(accessor: ServicesAccessor): Promise<void> {
    const quickInput = accessor.get(IQuickInputService), editors = accessor.get(IEditorService), files = accessor.get(IFileService);
    const initial = workspaceContext.getSnapshot(), target = managementGateway('settings');
    const controller = new AbortController(), cancellation = new CancellationTokenSource();
    const invalidate = () => { controller.abort(new Error('Gateway 或访问凭据已改变，请重新打开设置来源')); cancellation.cancel(); };
    const release = onGatewayUserSessionInvalidated(0, invalidate);
    const releaseContext = workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate(); });
    try {
      const picked = await quickInput.pick([
        { label: 'Gateway 配置', description: target.name, detail: target.gateway_id, workspaceId: undefined as string | undefined },
        ...initial.workspaces.filter(workspace => workspace.gateway_id === target.gateway_id).map(workspace => ({ label: workspace.name,
          description: `工作区 · ${workspace.status}`, detail: workspace.workspace_id, workspaceId: workspace.workspace_id })),
      ], { title: '选择设置来源的所属目标', prompt: `Gateway ${target.gateway_id}`, placeHolder: '读取来源不启动 Backend 或插件；离线来源由原 owner 明确报错' }, cancellation.token);
      controller.signal.throwIfAborted();
      if (!picked) return;
      const owner = new ConfigurationSourceOwner({ gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id,
        ...(picked.workspaceId ? { workspace_id: picked.workspaceId } : {}) });
      const sources = await owner.list(controller.signal);
      controller.signal.throwIfAborted();
      const source = await quickInput.pick(sources.sources.filter(item => item.source_key).map(item => ({ label: item.source_key!,
        description: `${item.layer} · ${item.presence} · 优先级 ${item.precedence}`, detail: item.uri ?? undefined, source: item })),
      { title: '选择正式配置来源', prompt: `${target.gateway_id} · ${picked.label}`, placeHolder: '内置与只读来源仍可查看；保存仅经原预检与 CAS', canPickMany: false }, cancellation.token);
      controller.signal.throwIfAborted();
      if (!source) return;
      const document = await owner.read(source.source.source_key!, undefined, controller.signal);
      controller.signal.throwIfAborted();
      const resource = configSourceFiles(files).prepareEditor(owner.target, source.source.source_key!, document);
      const input = { resource, label: source.label + '.jsonc', description: `${target.gateway_id} · ${picked.label}`,
        options: { pinned: true }, languageId: 'jsonc', forceFile: true } satisfies IUntypedFileEditorInput;
      await editors.openEditor(input);
    } finally { release(); releaseContext(); cancellation.dispose(); controller.abort(); }
  }
});

for (const operation of ['sourceStatus', 'rebaseSource'] as const) registerAction2(class extends Action2 {
  constructor() {
    super({ id: `boxteam.settings.${operation}`, title: operation === 'sourceStatus'
      ? { value: '查看当前设置来源与生效状态', original: 'Inspect Current Settings Source and Apply Status' }
      : { value: '重新预检当前设置来源草稿', original: 'Rebase Current Settings Source Draft' },
    category: { value: 'BoxTeam 设置', original: 'BoxTeam Settings' }, f1: true,
    precondition: ContextKeyExpr.equals('resourceScheme', configSourceScheme),
    menu: { id: MenuId.EditorTitle, when: ContextKeyExpr.equals('resourceScheme', configSourceScheme), group: 'navigation', order: operation === 'sourceStatus' ? 0 : 1 } });
  }
  async run(accessor: ServicesAccessor): Promise<void> {
    const files = accessor.get(IFileService), editors = accessor.get(IEditorService), quickInput = accessor.get(IQuickInputService);
    const resource = editors.activeEditor?.resource;
    if (resource?.scheme !== configSourceScheme) throw new Error('请先选择正式设置来源编辑器');
    const provider = configSourceFiles(files);
    if (operation === 'rebaseSource') { await provider.rebase(resource); return; }
    const document = await provider.query(resource);
    await quickInput.pick([
      { label: '配置保存与运行生效', detail: sourceStatus(document) },
      { label: '来源及运行基线', detail: `file=${document.file_digest ?? '无'} · layer=${document.layer_revision ?? '无'} / ${document.layer_digest ?? '无'} · active=${document.active_revision ?? '无'} / ${document.active_digest ?? '无'}` },
      { label: '已应用与延后路径', detail: `已应用：${document.status?.applied_paths.join('、') || '无'} · 延后：${document.status?.deferred_paths.join('、') || '无'}` },
      ...document.layers.map(layer => ({ label: `来源层 ${layer.source_key}`, description: `${layer.layer} · 优先级 ${layer.precedence} · ${layer.writable ? '可写' : '只读'}`, detail: JSON.stringify(layer.payload) })),
      ...document.origins.map(origin => ({ label: origin.path, description: origin.source_key, detail: `被遮蔽：${origin.shadowed_sources.join('、') || '无'}` })),
    ], { title: '目标设置来源与生效状态', prompt: `${document.gateway_id}${'workspace_id' in document ? ' · ' + document.workspace_id : ' · Gateway'} · ${document.source_key}` });
  }
});

registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.settings.openTarget', title: { value: '打开目标业务设置', original: 'Open Target Business Settings' },
    category: { value: 'BoxTeam 设置', original: 'BoxTeam Settings' }, f1: true }); }
  async run(accessor: ServicesAccessor): Promise<void> {
    const parent = accessor.get(IInstantiationService), editors = accessor.get(IEditorService);
    const cancellation = new CancellationTokenSource();
    try {
      const captured = await captureSettingsLocation(parent, cancellation.token);
      if (captured) await editors.openEditor(parent.createInstance(SettingsEditor2Input, captured.location, captured.document), { pinned: true });
    } finally { cancellation.dispose(); }
  }
});
