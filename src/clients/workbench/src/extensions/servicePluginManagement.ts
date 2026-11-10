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
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import type { PluginBinding, PluginInventoryResultDTO, PluginRuntimeStatusDTO, PluginTarget } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { gateway } from '../agentApi.js';
import { managementGateway, onDidChangeExtensionManagementTarget } from '../managementTarget.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';

import { manageConfig, readWorkspaceDefaults, readWorkspaceBindings, choosePluginWorkspace, writeOnlyFields, type Scope } from '../settings/pluginConfiguration.js';

type ConfigurationBinding = Omit<PluginBinding, 'workspace_id'>;
const errorsText = (error: unknown) => error instanceof Error ? error.message : String(error);

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
    @ICommandService private readonly commands: ICommandService) {
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
    const [inventory, runtime, defaults, workspaceBindings] = await Promise.all([
      gateway.request<PluginInventoryResultDTO>('/api/gateway/service-plugins/inventory', { method: 'POST', body: JSON.stringify(pluginTarget) }),
      gateway.request<PluginRuntimeStatusDTO>('/api/gateway/service-plugins/status', { method: 'POST', body: JSON.stringify(pluginTarget) }),
      readWorkspaceDefaults(target),
      workspace ? readWorkspaceBindings(workspace) : undefined,
    ]);
    if (this.content !== container || revision !== this.refreshRevision) return;
    clearNode(container);
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
      const installInput = append(packageActions, $('button'));
      installInput.textContent = '重新安装/更新';
      installInput.addEventListener('click', () => void this.runOperation(() => installPackageFromView(this.quickInput, this.notifications, pluginTarget)));
      const uninstall = append(packageActions, $('button')) as HTMLButtonElement;
      uninstall.textContent = '卸载整个包';
      const references = [...runtime.configured_bindings, ...defaults].filter(item => item.package_id === manifest.package_id && item.package_version === manifest.version);
      uninstall.disabled = references.length > 0;
      uninstall.title = references.length ? `先解除所有 ${references.length} 个当前绑定，再卸载包` : '';
      uninstall.addEventListener('click', () => void this.runOperation(() => uninstallPackageFromView(this.quickInput, this.notifications, pluginTarget, manifest.package_id, manifest.version)));
      for (const plugin of matching) {
        const binding = (scope === 'defaults' ? defaults : workspaceBindings ?? runtime.configured_bindings).find(item => item.package_id === manifest.package_id && item.plugin_id === plugin.id
          && item.package_version === manifest.version && (scope !== 'workspace' ? item.workspace_id == null : item.workspace_id === workspace?.workspace_id));
        const status = scope === 'defaults' ? undefined : runtime.plugins.find(item => item.binding?.package_id === manifest.package_id && item.binding.plugin_id === plugin.id
          && item.binding.package_version === manifest.version
          && (scope === 'workspace' ? item.binding.workspace_id === workspace?.workspace_id : item.binding.workspace_id == null));
        const row = append(packageSection, $('article'));
        const open = append(row, $('button'));
        open.textContent = `${plugin.name} · ${plugin.id}`;
        open.addEventListener('click', () => { this.selected = { packageId: manifest.package_id, pluginId: plugin.id, version: manifest.version };
          this.showDetails(container, scope, target, workspace, manifest, plugin, binding, status); });
        append(row, $('span')).textContent = scope === 'defaults'
          ? `${binding?.enabled ? '新建时启用' : binding ? '新建时停用' : '未加入默认值'} · 不代表已运行`
          : `${binding?.enabled ? '启用' : '停用'} · ${status?.state ?? (plugin.runtime ? '未运行' : '声明式')}`;
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
      : `${plugin.instance_scope} · ${status?.state ?? '未运行'}${status?.error ? ` · ${status.error}` : ''}`;
    append(details, $('p')).textContent = `Gateway ${target.gateway_id}${workspace ? ` · 工作区 ${workspace.workspace_id}` : ''} · ${manifest.version}${status?.instance ? ` · 实例 ${status.instance.instance_id}` : ''}`;
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
        const secret = append(details, $('button'));
        secret.textContent = `管理秘密参数 ${field.pointer}`;
        secret.addEventListener('click', () => void this.runOperation(() => manageConfig(scope, target, workspace,
          this.quickInput, this.notifications, bindingValue, null, false, field)));
      }
      if (plugin.contributes?.configuration) {
        const schema = append(details, $('pre'));
        schema.setAttribute('aria-label', '此安装版本的参数 schema');
        schema.textContent = JSON.stringify(plugin.contributes.configuration, null, 2);
      }
    }
    if (status?.revision && plugin.runtime) {
      const restart = append(details, $('button'));
      restart.textContent = '重启实例';
      restart.addEventListener('click', () => void this.runOperation(async () => {
        await gateway.request('/api/gateway/service-plugins/restart', { method: 'POST', body: JSON.stringify({ gateway_id: target.gateway_id,
          gateway_connection_id: target.gateway_connection_id, package_id: manifest.package_id, package_version: manifest.version,
          plugin_id: plugin.id, revision: status.revision, ...(workspace ? { workspace_id: workspace.workspace_id } : {}) }) });
      }));
    }
  }
}

async function installPackageFromView(quickInput: IQuickInputService, notifications: INotificationService, target: PluginTarget): Promise<void> {
  const type = await quickInput.pick([{ label: 'ZIP 文件', development_directory: false }, { label: '开发目录', development_directory: true }],
    { title: '安装本地插件包', canPickMany: false });
  if (!type) return;
  const path = await quickInput.input({ title: '输入目标 Gateway 上的原生绝对路径', prompt: target.gateway_id,
    placeHolder: type.development_directory ? '/path/to/plugin-directory' : '/path/to/plugin.zip', validateInput: async value =>
      value.trim() && !value.startsWith('/') && !/^[A-Za-z]:[\\/]/.test(value) && !/^\\\\/.test(value) ? '请输入目标机器原生绝对路径' : null });
  if (!path) return;
  await gateway.request('/api/gateway/service-plugins/install', { method: 'POST', body: JSON.stringify({ ...target, path, development_directory: type.development_directory }) });
  notifications.info('插件包已安装；正在使用 Gateway 原生库存。');
}

export async function installServicePluginPackage(accessor: ServicesAccessor): Promise<void> {
  const target = managementGateway('extensions');
  await installPackageFromView(accessor.get(IQuickInputService), accessor.get(INotificationService),
    { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id });
}

async function uninstallPackageFromView(quickInput: IQuickInputService, notifications: INotificationService, target: PluginTarget, packageId: string, version: string): Promise<void> {
  const confirm = await quickInput.pick([{ label: '取消', accept: false }, { label: '卸载整包', accept: true,
    description: `${packageId}@${version}` }], { title: '卸载插件包', canPickMany: false });
  if (!confirm?.accept) return;
  await gateway.request('/api/gateway/service-plugins/uninstall', { method: 'POST', body: JSON.stringify({ ...target, package_id: packageId, package_version: version }) });
  notifications.info(`已卸载 ${packageId}@${version}`);
}
