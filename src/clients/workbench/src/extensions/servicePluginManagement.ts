import { append, $, clearNode } from '../../upstream/src/vs/base/browser/dom.js';
import { ViewPane, type IViewPaneOptions } from '../../upstream/src/vs/workbench/browser/parts/views/viewPane.js';
import { IKeybindingService } from '../../upstream/src/vs/platform/keybinding/common/keybinding.js';
import { IContextMenuService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import { IConfigurationService } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import { IContextKeyService } from '../../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { IViewDescriptorService } from '../../upstream/src/vs/workbench/common/views.js';
import { IInstantiationService, type ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IOpenerService } from '../../upstream/src/vs/platform/opener/common/opener.js';
import { IThemeService } from '../../upstream/src/vs/platform/theme/common/themeService.js';
import { IHoverService } from '../../upstream/src/vs/platform/hover/browser/hover.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { IProgressService, ProgressLocation } from '../../upstream/src/vs/platform/progress/common/progress.js';
import { timeout } from '../../upstream/src/vs/base/common/async.js';
import { generateUuid } from '../../upstream/src/vs/base/common/uuid.js';
import type { InstallPluginRequest, PluginBinding, PluginInstallOperationDTO, PluginInventoryResultDTO, PluginRuntimeStatusDTO, PluginSourceCatalogDTO, PluginTarget } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { gateway } from '../agentApi.js';
import { managementGateway, onDidChangeExtensionManagementTarget } from '../managementTarget.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { DEFAULT_API_REQUEST_TIMEOUT_MS } from '../../../web/src/api/http.js';
import type { GatewayDiagnosticsDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';

import { manageConfig, setPluginBindings, readWorkspaceDefaults, readWorkspaceBindings, choosePluginWorkspace, writeOnlyFields, type Scope } from '../settings/pluginConfiguration.js';

type ConfigurationBinding = Omit<PluginBinding, 'workspace_id'>;
const errorsText = (error: unknown) => error instanceof Error ? error.message : String(error);
const activePluginInstallations = new Map<string, Promise<void>>();

function requireInstallationOwner(target: PluginTarget, operationId: string, operation: PluginInstallOperationDTO): PluginInstallOperationDTO {
  if (operation.gateway_id !== target.gateway_id || operation.operation_id !== operationId) {
    throw new Error(`插件安装操作回执不属于原 Gateway 或 operation_id：${operationId}`);
  }
  return operation;
}

/** 原 Extensions Viewlet 中展示 Gateway 自有插件模型，不伪造 IExtension 或 Gallery 对象。 */
export class ServicePluginPackagesView extends ViewPane {
  private scope: Scope = 'gateway';
  private content: HTMLElement | undefined;
  private selected: { packageId: string; pluginId: string; version: string } | undefined;
  private workspaceTarget: GatewayWorkspaceRequestTarget | undefined;
  private refreshRevision = 0;
  private searchInput: HTMLInputElement | undefined;
  private query = '';
  private controls: HTMLElement | undefined;

  constructor(options: IViewPaneOptions, @IKeybindingService keybindings: IKeybindingService,
    @IContextMenuService contextMenu: IContextMenuService, @IConfigurationService configuration: IConfigurationService,
    @IContextKeyService contextKeys: IContextKeyService, @IViewDescriptorService descriptors: IViewDescriptorService,
    @IInstantiationService instantiation: IInstantiationService, @IOpenerService opener: IOpenerService,
    @IThemeService theme: IThemeService, @IHoverService hover: IHoverService,
    @IQuickInputService private readonly quickInput: IQuickInputService,
    @INotificationService private readonly notifications: INotificationService,
    @ICommandService private readonly commands: ICommandService,
    @IProgressService private readonly progress: IProgressService,
    @IDialogService private readonly dialogs: IDialogService) {
    super(options, keybindings, contextMenu, configuration, contextKeys, descriptors, instantiation, opener, theme, hover);
    this._register(onDidChangeExtensionManagementTarget(() => {
      this.refreshRevision++;
      this.workspaceTarget = undefined;
      this.selected = undefined;
      void this.refresh();
    }));
  }

  protected override renderBody(container: HTMLElement): void {
    super.renderBody(container);
    container.classList.add('service-plugin-packages-view');
    this.searchInput = append(container, $('input')) as HTMLInputElement;
    this.searchInput.type = 'search';
    this.searchInput.setAttribute('aria-label', '搜索已安装插件包');
    this.searchInput.placeholder = '搜索已安装插件包';
    this.searchInput.value = this.query;
    this.searchInput.addEventListener('input', () => void this.commands.executeCommand('workbench.extensions.search', this.searchInput!.value)
      .catch(error => this.notifications.error(errorsText(error))));
    this.controls = append(container, $('div'));
    const selector = append(this.controls, $('select')) as HTMLSelectElement;
    selector.setAttribute('aria-label', '插件管理范围');
    selector.innerHTML = '<option value="gateway">当前 Gateway</option><option value="workspace">当前工作区</option><option value="defaults">以后新建工作区</option>';
    append(this.controls, $('p')).textContent = '“以后新建工作区”只影响之后创建的新工作区，不会启用当前或已有工作区插件。';
    selector.addEventListener('change', () => {
      if (selector.value === 'gateway' || selector.value === 'workspace' || selector.value === 'defaults') this.scope = selector.value;
      this.workspaceTarget = undefined;
      this.selected = undefined;
      this.refreshRevision++;
      void this.refresh();
    });
    const install = append(this.controls, $('button'));
    install.textContent = '安装插件包';
    install.addEventListener('click', () => void this.commands.executeCommand('boxteam.plugins.installPackage')
      .catch(error => this.notifications.error(errorsText(error))));
    this.content = append(container, $('.service-plugin-packages-content'));
    void this.refresh();
  }

  get searchValue(): string { return this.query; }

  search(query: string): void {
    this.query = query;
    if (this.searchInput) this.searchInput.value = query;
    const otherView = /(?:^|\s)@(mcp|agentPlugins)(?:\s|$)/i.test(query);
    if (this.controls) this.controls.hidden = otherView;
    if (this.content) this.content.hidden = otherView;
    for (const section of this.content?.querySelectorAll<HTMLElement>('.service-plugin-package') ?? []) {
      section.hidden = !section.dataset.search?.includes(query.trim().toLocaleLowerCase());
    }
  }

  override dispose(): void {
    this.refreshRevision++;
    this.content = undefined;
    super.dispose();
  }

  async refresh(): Promise<void> {
    if (!this.content) return;
    const revision = ++this.refreshRevision;
    const scope = this.scope;
    const container = this.content;
    clearNode(container);
    append(container, $('p')).textContent = '正在读取目标插件…';
    try {
      await this.renderPackages(container, revision, scope);
    } catch (error) {
      if (revision !== this.refreshRevision || this.content !== container) return;
      clearNode(container);
      append(container, $('p')).textContent = errorsText(error);
      const retry = append(container, $('button'));
      retry.textContent = '重试';
      retry.addEventListener('click', () => void this.refresh());
      this.notifications.error(errorsText(error));
    }
  }

  private async runOperation(operation: () => Promise<void>): Promise<void> {
    try { await operation(); }
    catch (error) { this.notifications.error(errorsText(error)); }
    finally { await this.refresh(); }
  }

  private async renderPackages(container: HTMLElement, revision: number, scope: Scope): Promise<void> {
    const target = managementGateway('extensions');
    const workspace = scope === 'workspace' ? (this.workspaceTarget ?? await choosePluginWorkspace(target, this.quickInput)) : undefined;
    if (revision !== this.refreshRevision) return;
    this.workspaceTarget = workspace;
    if (scope === 'workspace' && !workspace) { clearNode(container); append(container, $('p')).textContent = '请选择目标工作区后查看插件。'; return; }
    const pluginTarget: PluginTarget = { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id };
    const [inventory, runtime, scopeBindings] = await Promise.all([
      gateway.request<PluginInventoryResultDTO>('/api/gateway/service-plugins/inventory', { method: 'POST', body: JSON.stringify(pluginTarget) }),
      scope === 'defaults' ? undefined
        : gateway.request<PluginRuntimeStatusDTO>('/api/gateway/service-plugins/status', { method: 'POST', body: JSON.stringify(pluginTarget) }),
      scope === 'defaults' ? readWorkspaceDefaults(target) : workspace ? readWorkspaceBindings(workspace) : undefined,
    ]);
    if (this.content !== container || revision !== this.refreshRevision) return;
    clearNode(container);
    append(container, $('p')).textContent = inventory.sources?.message ?? '插件源尚未配置';
    if (workspace) {
      const handoff = runtime!.workspace_configurations.find(item => item.workspace_id === workspace.workspace_id);
      const summary = append(container, $('p'));
      summary.setAttribute('aria-label', '工作区插件配置交接');
      summary.textContent = `工作区 ${workspace.workspace_id} 配置交接：${handoff?.state ?? '尚未收到'}${handoff?.error ? ` · ${handoff.error}` : ''}`;
    }
    for (const pkg of inventory.packages) {
      const manifest = pkg.manifest;
      if (!manifest) continue;
      const pluginScope = scope === 'gateway' ? 'gateway' : 'workspace';
      const matching = manifest.plugins.filter(plugin => plugin.instance_scope === pluginScope);
      if (!matching.length) continue;
      const packageSection = append(container, $('section.service-plugin-package'));
      packageSection.dataset.search = `${manifest.package_id} ${manifest.name} ${manifest.description ?? ''} ${matching.map(plugin => `${plugin.id} ${plugin.name}`).join(' ')}`.toLocaleLowerCase();
      append(packageSection, $('h3')).textContent = `${manifest.name} · ${manifest.package_id}@${manifest.version}`;
      append(packageSection, $('p')).textContent = manifest.description ?? '';
      const packageActions = append(packageSection, $('.service-plugin-package-actions'));
      const batchButtons = [true, false].map(enabled => {
        const button = append(packageActions, $('button')) as HTMLButtonElement;
        button.textContent = enabled ? '全部启用此范围' : '全部停用此范围';
        button.addEventListener('click', () => void this.runOperation(async () => {
          batchButtons.forEach(item => { item.disabled = true; });
          try {
            const excluded = manifest.plugins.filter(plugin => plugin.instance_scope !== pluginScope);
            const owner = `Gateway ${target.gateway_id} · ${scope}${workspace ? ` · 工作区 ${workspace.workspace_id}` : ''}`;
            const confirmation = await this.dialogs.confirm({ message: `${enabled ? '启用' : '停用'} ${manifest.package_id}@${manifest.version} 的当前范围插件？`,
              detail: `${owner}\n纳入：${matching.map(plugin => plugin.id).join('、')}\n排除（固定范围不匹配）：${excluded.map(plugin => `${plugin.id}（${plugin.instance_scope}）`).join('、') || '无'}\n`
                + (scope === 'defaults' ? '仅修改以后新建工作区的默认值，不操作当前实例。' : '每个插件独立提交，失败不回滚已完成的操作。'),
              primaryButton: enabled ? '全部启用此范围' : '全部停用此范围' });
            if (!confirmation.confirmed) return;
            const results = await setPluginBindings(scope, target, workspace, this.quickInput,
              matching.map(plugin => ({ package_id: manifest.package_id, plugin_id: plugin.id, package_version: manifest.version, enabled })), enabled);
            if (!results) return;
            let current: PluginRuntimeStatusDTO | undefined, readError: string | undefined;
            if (scope !== 'defaults') {
              try { current = await gateway.request<PluginRuntimeStatusDTO>('/api/gateway/service-plugins/status', { method: 'POST', body: JSON.stringify(pluginTarget) }); }
              catch (error) { readError = errorsText(error); }
            }
            const lines = results.map(({ binding: item, configuration }) => {
              const actual = current?.plugins.find(slot => slot.gateway_id === target.gateway_id && slot.binding?.package_id === item.package_id
                && slot.binding.plugin_id === item.plugin_id && slot.binding.package_version === item.package_version
                && (scope === 'workspace' ? slot.binding.workspace_id === workspace?.workspace_id : slot.binding.workspace_id == null));
              const state = scope === 'defaults' ? '仅修改新建默认值，未操作当前实例' : readError ? `运行状态读取失败：${readError}`
                : actual ? `${actual.binding?.enabled ? '启用' : '停用'} · ${actual.declarative ? '声明式，无运行实例' : actual.state}${actual.error ? ` · ${actual.error}` : ''}${actual.instance ? ` · 实例 ${actual.instance.instance_id}` : ''}` : '未发布运行状态';
              return `${owner} · ${item.package_id}/${item.plugin_id}@${item.package_version}\n配置：${configuration}\n实际：${state}`;
            });
            await this.dialogs.info('插件批量操作结果', lines.join('\n\n'));
          } finally { batchButtons.forEach(item => { item.disabled = false; }); }
        }));
        return button;
      });
      const installInput = append(packageActions, $('button'));
      installInput.textContent = '重新安装/更新';
      installInput.addEventListener('click', () => void this.runOperation(() => installPackageFromView(this.quickInput, this.notifications, this.progress, pluginTarget)));
      const uninstall = append(packageActions, $('button')) as HTMLButtonElement;
      uninstall.textContent = '卸载整个包';
      uninstall.title = '由目标 Gateway 检查在用引用；有引用时显示具体原因';
      uninstall.addEventListener('click', () => {
        uninstall.disabled = true;
        void this.runOperation(() => uninstallPackageFromView(this.quickInput, this.notifications, pluginTarget, manifest.package_id, manifest.version));
      });
      for (const plugin of matching) {
        const binding = (scopeBindings ?? runtime!.configured_bindings).find(item => item.package_id === manifest.package_id && item.plugin_id === plugin.id
          && item.package_version === manifest.version && (scope !== 'workspace' ? item.workspace_id == null : item.workspace_id === workspace?.workspace_id));
        const status = runtime?.plugins.find(item => item.gateway_id === target.gateway_id
          && item.binding?.package_id === manifest.package_id && item.binding.plugin_id === plugin.id
          && item.binding.package_version === manifest.version
          && (scope === 'workspace' ? item.binding.workspace_id === workspace?.workspace_id : item.binding.workspace_id == null));
        const row = append(packageSection, $('article'));
        const open = append(row, $('button'));
        open.textContent = `${plugin.name} · ${plugin.id}`;
        open.addEventListener('click', () => { this.selected = { packageId: manifest.package_id, pluginId: plugin.id, version: manifest.version };
          this.showDetails(container, scope, target, workspace, manifest, plugin, binding, status); });
        append(row, $('span')).textContent = scope === 'defaults'
          ? `${binding?.enabled ? '新建时启用' : binding ? '新建时停用' : '未加入默认值'} · 不代表已运行`
          : `${binding?.enabled ? '启用' : '停用'} · ${plugin.runtime ? status?.state ?? '未运行' : '声明式，无运行实例'}`;
        if (revision === this.refreshRevision && this.selected?.packageId === manifest.package_id && this.selected.pluginId === plugin.id && this.selected.version === manifest.version) {
          this.showDetails(container, scope, target, workspace, manifest, plugin, binding, status);
        }
      }
    }
    this.search(this.query);
    if (!inventory.packages.length) append(container, $('p')).textContent = `尚无插件包。${inventory.sources?.message ?? ''}`;
  }

  private showDetails(container: HTMLElement, scope: Scope, target: ReturnType<typeof managementGateway>, workspace: GatewayWorkspaceRequestTarget | undefined,
    manifest: NonNullable<PluginInventoryResultDTO['packages'][number]['manifest']>, plugin: NonNullable<typeof manifest.plugins[number]>,
    binding: PluginBinding | undefined, status: PluginRuntimeStatusDTO['plugins'][number] | undefined): void {
    const old = container.querySelector('.service-plugin-details');
    old?.remove();
    const details = append(container, $('section.service-plugin-details'));
    append(details, $('h3')).textContent = `${manifest.package_id}/${plugin.id}`;
    append(details, $('p')).textContent = scope === 'defaults' ? `${plugin.instance_scope} · 新建工作区默认值，不会在此 Gateway 启动插件`
      : `${plugin.instance_scope} · ${plugin.runtime ? status?.state ?? '未运行' : '声明式，无运行实例'}${status?.error ? ` · ${status.error}` : ''}`;
    append(details, $('p')).textContent = `Gateway ${target.gateway_id}${workspace ? ` · 工作区 ${workspace.workspace_id}` : ''} · ${manifest.version}${status?.instance ? ` · 实例 ${status.instance.instance_id}` : ''}`;
    const logId = status?.log_id;
    if (logId) {
      const logs = append(details, $('button')) as HTMLButtonElement;
      logs.textContent = '查看实例日志';
      const output = append(details, $('pre'));
      output.setAttribute('aria-label', '插件实例日志');
      logs.addEventListener('click', () => {
        logs.disabled = true;
        void readPluginInstanceLog(target, logId).then(text => { output.textContent = text; })
          .catch(error => { output.textContent = errorsText(error); this.notifications.error(errorsText(error)); })
          .finally(() => { logs.disabled = false; });
      });
    }
    const bindingValue: ConfigurationBinding = { package_id: manifest.package_id, plugin_id: plugin.id, package_version: manifest.version, enabled: true };
    const toggle = append(details, $('button'));
    toggle.textContent = binding?.enabled ? '停用此范围' : '启用此范围';
    toggle.addEventListener('click', () => void this.runOperation(() => manageConfig(scope, target, workspace, this.quickInput, this.notifications, bindingValue, !binding?.enabled)));
    if (binding) {
      const unbind = append(details, $('button'));
      unbind.textContent = '解除此范围绑定';
      unbind.addEventListener('click', () => void this.runOperation(() => manageConfig(scope, target, workspace, this.quickInput, this.notifications, bindingValue, null)));
    }
    if (scope !== 'defaults') {
      append(details, $('p')).textContent = binding
        ? `参数按 ${manifest.package_id}/${plugin.id}@${manifest.version} 校验；保存与实际运行分别报告。`
        : '插件未绑定，参数仅留在原来源且未生效；绑定时按确切版本完整校验。';
      const parameters = append(details, $('button'));
      parameters.textContent = '编辑参数 JSON';
      parameters.addEventListener('click', () => void this.runOperation(() => manageConfig(scope, target, workspace,
        this.quickInput, this.notifications, bindingValue, null, true)));
      const privateFields = writeOnlyFields(plugin.contributes?.configuration);
      for (const field of privateFields) {
        const secret = append(details, $('button')) as HTMLButtonElement;
        secret.textContent = field.pointer ? `管理秘密参数 ${field.pointer}` : '按位置管理秘密参数';
        secret.addEventListener('click', () => {
          secret.disabled = true;
          void this.runOperation(() => manageConfig(scope, target, workspace,
            this.quickInput, this.notifications, bindingValue, null, false, field));
        });
      }
      if (plugin.contributes?.configuration) {
        const schema = append(details, $('pre'));
        schema.setAttribute('aria-label', '此安装版本的参数 schema');
        schema.textContent = JSON.stringify(plugin.contributes.configuration, null, 2);
      }
    }
    if (status?.revision && plugin.runtime) {
      const restart = append(details, $('button')) as HTMLButtonElement;
      restart.textContent = '重启实例';
      restart.addEventListener('click', () => {
        restart.disabled = true;
        void this.runOperation(async () => {
          await gateway.request('/api/gateway/service-plugins/restart', { method: 'POST', body: JSON.stringify({ gateway_id: target.gateway_id,
            gateway_connection_id: target.gateway_connection_id, package_id: manifest.package_id, package_version: manifest.version,
            plugin_id: plugin.id, revision: status.revision, ...(workspace ? { workspace_id: workspace.workspace_id } : {}) }) });
        });
      });
    }
  }
}

async function readPluginInstanceLog(target: PluginTarget, logId: string): Promise<string> {
  const query = new URLSearchParams({ log_id: logId, tail_lines: '300' });
  if (target.gateway_connection_id) query.set('gateway_connection_id', target.gateway_connection_id);
  const diagnostics = await gateway.request<GatewayDiagnosticsDTO>(`/api/gateway/diagnostics?${query}`, {
    headers: { 'X-BoxTeam-Target-Gateway-Id': target.gateway_id },
  });
  if (diagnostics.gateway_id !== target.gateway_id || diagnostics.selected_log_id !== logId) throw new Error('实例日志回执不属于原 Gateway 或日志');
  const log = diagnostics.logs.find(item => item.log_id === logId);
  if (!log) throw new Error('原实例日志不在目标诊断回执中');
  if (log.status === 'empty') return '当前实例日志为空。';
  if (log.status !== 'available') throw new Error(log.error ?? '原实例日志当前不可读');
  return `${log.truncated ? '仅显示最近的日志尾部。\n' : ''}${log.tail}`;
}

async function installPackageFromView(quickInput: IQuickInputService, notifications: INotificationService,
  progress: IProgressService, target: PluginTarget): Promise<void> {
  const type = await quickInput.pick([{ label: 'ZIP 文件', kind: 'zip' }, { label: '开发目录', kind: 'directory' },
    { label: '自有来源', kind: 'source' }, { label: '查询安装操作', kind: 'query' }],
    { title: `插件安装 · ${target.gateway_id}`, canPickMany: false });
  if (!type) return;
  if (type.kind === 'query') {
    const operationId = await quickInput.input({ title: '输入原安装操作 ID', prompt: target.gateway_id });
    if (operationId) await observeInstallation(progress, notifications, target, operationId);
    return;
  }
  let acquisition: Omit<InstallPluginRequest, keyof PluginTarget | 'operation_id'>;
  if (type.kind === 'source') {
    const inventory = await gateway.request<PluginInventoryResultDTO>('/api/gateway/service-plugins/inventory',
      { method: 'POST', body: JSON.stringify(target) });
    const sources = inventory.sources?.entries.filter(source => source.enabled) ?? [];
    if (!sources.length) { notifications.info(inventory.sources?.message ?? '插件源尚未配置'); return; }
    const source = await quickInput.pick(sources.map(source => ({ label: source.source_id, source })),
      { title: '选择自有来源', canPickMany: false });
    if (!source) return;
    const query = await quickInput.input({ title: '搜索自有插件包', prompt: source.source.source_id });
    if (query === undefined) return;
    let cursor: string | undefined, selected: PluginSourceCatalogDTO['items'][number] | undefined, revision: string | undefined;
    while (!selected) {
      const page = await gateway.request<PluginSourceCatalogDTO>('/api/gateway/service-plugins/source-catalog',
        { method: 'POST', body: JSON.stringify({ ...target, source_id: source.source.source_id, query, cursor }) });
      if (page.gateway_id !== target.gateway_id || page.source_id !== source.source.source_id) {
        throw new Error('插件来源目录回执不属于原 Gateway 或 source_id');
      }
      if (revision !== undefined && page.catalog_revision !== revision) {
        throw new Error(`插件目录分页期间版本已变化：${revision} → ${page.catalog_revision}；请重新查询`);
      }
      revision = page.catalog_revision;
      const items = page.items.map(artifact => ({ label: `${artifact.package_id}@${artifact.package_version}`,
        description: artifact.target ? `${artifact.target.os}/${artifact.target.architecture} · ${artifact.target.system_baseline}` : '纯声明插件包', artifact }));
      const choice = await quickInput.pick([...items, ...(page.next_cursor ? [{ label: '下一页', description: '', artifact: undefined }] : [])],
        { title: items.length ? '选择确切版本与目标制品' : '此页无匹配插件包', canPickMany: false });
      if (!choice) return;
      if (choice.artifact) selected = choice.artifact;
      else cursor = page.next_cursor;
    }
    if (!revision) throw new Error('插件来源目录缺少 catalog_revision');
    acquisition = { source_id: source.source.source_id, catalog_revision: revision, artifact: selected,
      development_directory: false, path: '' };
  } else {
    const path = await quickInput.input({ title: '输入目标 Gateway 上的原生绝对路径', prompt: target.gateway_id,
      placeHolder: type.kind === 'directory' ? '/path/to/plugin-directory' : '/path/to/plugin.zip', validateInput: async value =>
        value.trim() && !value.startsWith('/') && !/^[A-Za-z]:[\\/]/.test(value) && !/^\\\\/.test(value) ? '请输入目标机器原生绝对路径' : null });
    if (!path) return;
    acquisition = { path, development_directory: type.kind === 'directory', artifact: undefined };
  }
  await runPluginInstallation(target, acquisition, notifications, async () => {
    const operationId = generateUuid();
    let operation: PluginInstallOperationDTO;
    try {
      operation = requireInstallationOwner(target, operationId, await gateway.request<PluginInstallOperationDTO>(
        '/api/gateway/service-plugins/install',
        { method: 'POST', body: JSON.stringify({ ...target, ...acquisition, operation_id: operationId }) }));
    } catch (error) {
      // 仅查询原操作；响应丢失不能变成第二次安装。
      try { operation = await readInstallation(target, operationId); }
      catch (queryError) {
        throw new Error(`安装响应未确认：${errorsText(error)}。原操作查询失败：${errorsText(queryError)}。请查询原操作 ${operationId}（Gateway ${target.gateway_id}）。`);
      }
    }
    await observeInstallation(progress, notifications, target, operationId, operation);
  });
}

function installationKey(target: PluginTarget, acquisition: Omit<InstallPluginRequest, keyof PluginTarget | 'operation_id'>): string {
  const artifact = acquisition.artifact;
  return JSON.stringify([
    target.gateway_id, target.gateway_connection_id ?? null,
    acquisition.source_id ?? null, acquisition.catalog_revision ?? null,
    artifact ?? [acquisition.development_directory, acquisition.path, acquisition.expected_digest ?? null],
  ]);
}

async function runPluginInstallation(target: PluginTarget,
  acquisition: Omit<InstallPluginRequest, keyof PluginTarget | 'operation_id'>,
  notifications: INotificationService, submit: () => Promise<void>): Promise<void> {
  const key = installationKey(target, acquisition);
  let pending = activePluginInstallations.get(key);
  if (!pending) {
    pending = Promise.resolve().then(submit);
    activePluginInstallations.set(key, pending);
  } else {
    notifications.info('相同插件制品的原安装操作仍在进行，正在等待该操作');
  }
  try { await pending; }
  finally {
    if (activePluginInstallations.get(key) === pending) activePluginInstallations.delete(key);
  }
}

async function readInstallation(target: PluginTarget, operationId: string): Promise<PluginInstallOperationDTO> {
  const operation = await gateway.request<PluginInstallOperationDTO>('/api/gateway/service-plugins/install-operation',
    { method: 'POST', body: JSON.stringify({ ...target, operation_id: operationId }) });
  return requireInstallationOwner(target, operationId, operation);
}

async function observeInstallation(progress: IProgressService, notifications: INotificationService,
  target: PluginTarget, operationId: string, initial?: PluginInstallOperationDTO): Promise<void> {
  await progress.withProgress({ location: ProgressLocation.Notification,
    title: `安装插件包 · ${target.gateway_id} · ${operationId}`, cancellable: true }, async reporter => {
    const deadline = performance.now() + DEFAULT_API_REQUEST_TIMEOUT_MS;
    let operation = initial ?? await readInstallation(target, operationId);
    while (operation.status === 'pending' || operation.status === 'running') {
      reporter.report({ message: operation.cancel_requested ? '正在结清取消请求…' : '正在安装…' });
      if (performance.now() >= deadline) throw new Error(`等待安装结果超时，请查询原操作 ${operationId}；不会重新安装。`);
      await timeout(300);
      operation = await readInstallation(target, operationId);
    }
    if (operation.status !== 'succeeded') throw new Error(`安装 ${operation.status}：${operation.error ?? operationId}`);
    notifications.info(`已安装 ${operation.artifact?.package_id}@${operation.artifact?.package_version}`);
  }, () => { void gateway.request('/api/gateway/service-plugins/cancel-install',
    { method: 'POST', body: JSON.stringify({ ...target, operation_id: operationId }) })
    .catch(error => notifications.error(`取消未确认，请查询原操作 ${operationId}：${errorsText(error)}`)); });
}

export async function installServicePluginPackage(accessor: ServicesAccessor): Promise<void> {
  const target = managementGateway('extensions');
  await installPackageFromView(accessor.get(IQuickInputService), accessor.get(INotificationService), accessor.get(IProgressService),
    { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id });
}

async function uninstallPackageFromView(quickInput: IQuickInputService, notifications: INotificationService, target: PluginTarget, packageId: string, version: string): Promise<void> {
  const confirm = await quickInput.pick([{ label: '取消', accept: false }, { label: '卸载整包', accept: true,
    description: `${packageId}@${version}` }], { title: '卸载插件包', canPickMany: false });
  if (!confirm?.accept) return;
  await gateway.request('/api/gateway/service-plugins/uninstall', { method: 'POST', body: JSON.stringify({ ...target, package_id: packageId, package_version: version }) });
  notifications.info(`已卸载 ${packageId}@${version}`);
}
