import { applyEdits, setProperty } from '../../upstream/src/vs/base/common/jsonEdit.js';
import { parse, parseTree, type ParseError } from '../../upstream/src/vs/base/common/json.js';
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
import type { GatewayConfigSourceDocumentDTO, GatewayConfigSourceEditRequest, GatewayConfigSourcesDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import type { PluginBinding, PluginInventoryResultDTO, PluginPrivateInputReference, PluginPrivateInputWriteRequest,
  PluginRuntimeStatusDTO, PluginTarget } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import type { ConfigSourceDocumentDTO, ConfigSourceEditRequest } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { getConfigSourceDocument, listConfigSources, preflightConfigSource, saveConfigSource } from '../../../web/src/api/configSources.js';
import { gatewayWorkspaceRequestHeaders } from '../../../web/src/api/http.js';
import { gateway } from '../agentApi.js';
import { managementGateway, onDidChangeExtensionManagementTarget } from '../managementTarget.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';

type Scope = 'gateway' | 'workspace' | 'defaults';
type ConfigDocument = GatewayConfigSourceDocumentDTO | ConfigSourceDocumentDTO;
type ConfigIntent = GatewayConfigSourceEditRequest | ConfigSourceEditRequest;
type ConfigurationBinding = Omit<PluginBinding, 'workspace_id'>;
type Source = { source_key?: string | null; layer: string; presence: string; uri?: string | null; writable?: boolean };
type PrivateParameter = { pointer: string; schema: Record<string, unknown> };

const targetHeader = (gatewayId: string) => ({ 'X-BoxTeam-Target-Gateway-Id': gatewayId });
const errorsText = (error: unknown) => error instanceof Error ? error.message : String(error);
const sameBinding = (a: Pick<PluginBinding, 'package_id' | 'plugin_id'>, b: Pick<PluginBinding, 'package_id' | 'plugin_id'>) =>
  a.package_id === b.package_id && a.plugin_id === b.plugin_id;

function updateJsonc(text: string, path: (string | number)[], value: unknown): string {
  const errors: ParseError[] = [];
  const root = parseTree(text, errors);
  if (errors.length || root?.type !== 'object') throw new Error('配置来源不是有效 JSONC 对象，未提交插件变更');
  const edits = setProperty(text, path, value, { insertSpaces: true, tabSize: 2, eol: text.includes('\r\n') ? '\r\n' : '\n' });
  return applyEdits(text, edits);
}

function sourceBindings(document: ConfigDocument, path: string[]): PluginBinding[] {
  return bindingsAt(parse(document.jsonc), path);
}

function bindingsAt(value: unknown, path: string[]): PluginBinding[] {
  let current: unknown = value;
  for (const key of path) current = current && typeof current === 'object' ? (current as Record<string, unknown>)[key] : undefined;
  if (current === undefined) return [];
  if (!Array.isArray(current) || current.some(item => !item || typeof item !== 'object')) throw new Error('插件绑定配置不是有效数组，未提交修改');
  return current as PluginBinding[];
}

async function chooseSource(quickInput: IQuickInputService, sources: Source[], title: string): Promise<Source | undefined> {
  const selected = await quickInput.pick(sources.filter(item => item.source_key).map(source => ({
    label: source.source_key!, description: `${source.layer} · ${source.presence}${source.writable === false ? ' · 只读' : ''}`,
    detail: source.uri ?? undefined, source,
  })), { title, placeHolder: '选择原配置来源；写入仍由来源 owner 的 CAS 决定', canPickMany: false });
  return selected?.source;
}

async function readWorkspaceDefaults(target: ReturnType<typeof managementGateway>): Promise<PluginBinding[]> {
  const query = target.gateway_connection_id ? `?gateway_connection_id=${encodeURIComponent(target.gateway_connection_id)}` : '';
  const headers = targetHeader(target.gateway_id);
  const sources = await gateway.request<GatewayConfigSourcesDTO>(`/api/gateway/config/sources${query}`, { headers });
  const source = [...sources.sources].filter(item => item.loaded && item.source_key).sort((a, b) => b.precedence - a.precedence)[0];
  if (!source?.source_key) throw new Error('Gateway 没有可读取 workspace_defaults 的配置来源');
  const document = await gateway.request<GatewayConfigSourceDocumentDTO>(
    `/api/gateway/config/sources/${encodeURIComponent(source.source_key)}${query}`, { headers });
  if (document.gateway_id !== target.gateway_id || document.source_key !== source.source_key) throw new Error('workspace_defaults 文档不属于目标 Gateway 来源');
  return bindingsAt(document.source_effective, ['plugins', 'workspace_defaults', 'entries']);
}

async function readWorkspaceBindings(target: GatewayWorkspaceRequestTarget): Promise<PluginBinding[]> {
  const listed = await listConfigSources(0, target);
  const source = listed.sources.filter(item => item.loaded && item.source_key).sort((a, b) => b.precedence - a.precedence)[0];
  if (!source?.source_key) throw new Error('工作区没有可读取插件绑定的配置来源');
  const document = await getConfigSourceDocument(0, target, source.source_key);
  // 期望由原配置 owner 返回；Gateway 的异步交接回执只用于显示实际状态。
  return bindingsAt(document.source_effective, ['plugins', 'runtime', 'entries'])
    .map(binding => ({ ...binding, workspace_id: target.workspace_id }));
}

async function manageConfig(scope: Scope, gatewayTarget: ReturnType<typeof managementGateway>, workspaceTarget: GatewayWorkspaceRequestTarget | undefined,
  quickInput: IQuickInputService, notification: INotificationService, binding: ConfigurationBinding, enabled: boolean | null,
  editParameters = false, privateParameter?: PrivateParameter): Promise<void> {
  const path = scope === 'defaults' ? ['plugins', 'workspace_defaults', 'entries'] : ['plugins', 'runtime', 'entries'];
  let document: ConfigDocument;
  let route: { gateway_id: string; workspace_id?: string };
  let source: Source;
  let gatewaySource = false;
  if (scope !== 'workspace') {
    const query = gatewayTarget.gateway_connection_id ? `?gateway_connection_id=${encodeURIComponent(gatewayTarget.gateway_connection_id)}` : '';
    const headers = targetHeader(gatewayTarget.gateway_id);
    const listed = await gateway.request<GatewayConfigSourcesDTO>(`/api/gateway/config/sources${query}`, { headers });
    const selected = await chooseSource(quickInput, listed.sources, '选择 Gateway 插件配置来源');
    if (!selected) return;
    source = selected;
    route = { gateway_id: gatewayTarget.gateway_id };
    document = await gateway.request<GatewayConfigSourceDocumentDTO>(
      `/api/gateway/config/sources/${encodeURIComponent(source.source_key!)}${query}`, { headers });
    gatewaySource = true;
  } else {
    if (!workspaceTarget) throw new Error('当前没有已选择的工作区');
    route = workspaceTarget;
    const listed = await listConfigSources(0, workspaceTarget);
    const selected = await chooseSource(quickInput, listed.sources, '选择工作区插件配置来源');
    if (!selected) return;
    source = selected;
    document = await getConfigSourceDocument(0, workspaceTarget, source.source_key!);
  }
  if (!document.writable) throw new Error(`配置来源 ${source.source_key} 只读，未提交插件变更`);
  let jsonc: string;
  let privateInputConfirmed = false;
  if (privateParameter) {
    const parameterPath = ['plugins', 'configuration', 'values', binding.package_id, binding.plugin_id,
      ...privateParameter.pointer.split('/').slice(1).map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'))];
    let current: unknown = parse(document.jsonc);
    for (const key of parameterPath) current = current && typeof current === 'object' ? (current as Record<string, unknown>)[key] : undefined;
    const configured = current !== undefined;
    const action = await quickInput.pick([{ label: configured ? '更换已设置秘密参数' : '设置秘密参数', clear: false },
      ...(current !== undefined
        ? [{ label: '清除此来源的秘密覆盖', clear: true }] : [])],
    { title: `${binding.package_id}/${binding.plugin_id}${privateParameter.pointer}`, placeHolder: '原值不会回读；更换会创建新的不可变秘密版本' });
    if (!action) return;
    let storedValue: unknown;
    if (!action.clear) {
      const text = await quickInput.input({ title: '输入插件秘密参数', prompt: `${gatewayTarget.gateway_id}${workspaceTarget ? ` · ${workspaceTarget.workspace_id}` : ''} · ${source.source_key}`,
        password: true, validateInput: async value => {
          if (privateParameter.schema.type === 'string' || !value) return null;
          try { JSON.parse(value); return null; } catch { return '此秘密字段需要输入有效 JSON'; }
        } });
      if (text === undefined) return;
      storedValue = privateParameter.schema.type === 'string' ? text : JSON.parse(text);
      const accepted = await quickInput.pick([{ label: '取消' }, { label: '确认保存秘密', save: true,
        description: `${gatewayTarget.gateway_id}${workspaceTarget ? ` · ${workspaceTarget.workspace_id}` : ''} · ${source.source_key} · ${binding.package_id}@${binding.package_version}/${binding.plugin_id}${privateParameter.pointer}` }],
      { title: '确认目标、来源与秘密参数' });
      if (!accepted?.save) return;
      privateInputConfirmed = true;
      const writeTarget: PluginPrivateInputWriteRequest = { gateway_id: gatewayTarget.gateway_id, ...(gatewayTarget.gateway_connection_id
        ? { gateway_connection_id: gatewayTarget.gateway_connection_id } : {}),
        ...(workspaceTarget ? { workspace_id: workspaceTarget.workspace_id } : {}), package_id: binding.package_id,
        plugin_id: binding.plugin_id, package_version: binding.package_version, source_key: source.source_key!,
        parameter_pointer: privateParameter.pointer, value: storedValue };
      const privatePath = workspaceTarget
        ? `/api/v1/workspaces/${encodeURIComponent(workspaceTarget.workspace_id)}/config/plugin-private-inputs`
        : '/api/gateway/service-plugins/private-inputs';
      const reference = await gateway.request<PluginPrivateInputReference>(privatePath, {
        method: 'POST', headers: workspaceTarget ? gatewayWorkspaceRequestHeaders(workspaceTarget)
          : targetHeader(gatewayTarget.gateway_id),
        body: JSON.stringify(writeTarget),
      });
      if (reference.kind !== 'plugin_private_file' || reference.gateway_id !== gatewayTarget.gateway_id
        || (reference.workspace_id ?? null) !== (workspaceTarget?.workspace_id ?? null) || reference.package_id !== binding.package_id
        || reference.plugin_id !== binding.plugin_id || reference.parameter_pointer !== privateParameter.pointer
        || !reference.version) throw new Error('插件秘密输入写入回执归属或引用版本无效');
      storedValue = reference;
    }
    jsonc = updateJsonc(document.jsonc, parameterPath, action.clear ? undefined : storedValue);
  } else if (editParameters) {
    if (scope === 'defaults') throw new Error('新建工作区默认值只保存绑定；参数由目标工作区配置 owner 管理');
    const parameterPath = ['plugins', 'configuration', 'values', binding.package_id, binding.plugin_id];
    let current: unknown = parse(document.jsonc);
    for (const key of parameterPath) current = current && typeof current === 'object' ? (current as Record<string, unknown>)[key] : undefined;
    const operation = await quickInput.pick([{ label: '编辑此来源的参数', remove: false }, { label: '删除此来源的参数覆盖', remove: true }],
      { title: `${binding.package_id}/${binding.plugin_id} · ${binding.package_version}`, placeHolder: '删除只影响当前来源；原 owner 计算下层有效值' });
    if (!operation) return;
    let values: unknown;
    if (!operation.remove) {
      const text = await quickInput.input({ title: '编辑插件参数 JSON 对象', prompt: `${gatewayTarget.gateway_id}${workspaceTarget ? ` · ${workspaceTarget.workspace_id}` : ''} · ${source.source_key}`,
        value: JSON.stringify(current ?? {}), validateInput: async value => {
          try { const parsed = JSON.parse(value); return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? null : '参数必须是 JSON 对象'; }
          catch { return '请输入有效 JSON；确切版本 schema 由原配置 owner 预检'; }
        } });
      if (text === undefined) return;
      values = JSON.parse(text);
    }
    jsonc = updateJsonc(document.jsonc, parameterPath, values);
  } else {
    const original = sourceBindings(document, path);
    const index = original.findIndex(item => sameBinding(item, binding));
    const updated = original.slice();
    if (enabled === null) { if (index >= 0) updated.splice(index, 1); }
    else if (index < 0) updated.push({ ...binding, enabled });
    else updated[index] = { ...binding, enabled };
    jsonc = updateJsonc(document.jsonc, path, updated);
  }
  if (jsonc === document.jsonc) return;
  const intent = Object.freeze({ ...route, source_key: source.source_key!, jsonc,
    base_file_digest: document.file_digest, base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
    expected_active_revision: document.active_revision, expected_active_digest: document.active_digest,
    idempotency_key: crypto.randomUUID() }) as ConfigIntent;
  let saved: ConfigDocument;
  const change = privateParameter ? '修改秘密参数' : editParameters ? '修改参数' : enabled === null ? '解除绑定' : enabled ? '启用' : '停用';
  const confirmation = privateParameter ? '插件秘密参数' : editParameters ? '插件参数' : '插件绑定';
  if (gatewaySource) {
    const query = gatewayTarget.gateway_connection_id ? `?gateway_connection_id=${encodeURIComponent(gatewayTarget.gateway_connection_id)}` : '';
    const headers = targetHeader(gatewayTarget.gateway_id);
    const key = encodeURIComponent(source.source_key!);
    const preflight = await gateway.request<{ document?: GatewayConfigSourceDocumentDTO; changed_paths: string[] }>(
      `/api/gateway/config/sources/${key}/preflight${query}`, { method: 'POST', headers, body: JSON.stringify(intent) });
    if (!preflight.document || preflight.document.gateway_id !== gatewayTarget.gateway_id || preflight.document.source_key !== source.source_key) throw new Error('Gateway 配置预检回执归属不匹配');
    const accepted = privateInputConfirmed ? { save: true } : await quickInput.pick([{ label: '取消' }, { label: `确认保存 Gateway ${confirmation}`, save: true,
      description: `${binding.package_id}/${binding.plugin_id} · ${change}`, detail: preflight.changed_paths.join(' · ') }],
    { title: '确认配置提交' });
    if (!accepted?.save) return;
    saved = await gateway.request<GatewayConfigSourceDocumentDTO>(`/api/gateway/config/sources/${key}${query}`,
      { method: 'PUT', headers, body: JSON.stringify(intent) });
    if (saved.gateway_id !== gatewayTarget.gateway_id || saved.source_key !== source.source_key || saved.edit?.idempotency_key !== intent.idempotency_key) throw new Error('Gateway 配置保存回执归属不匹配');
  } else {
    const workspace = workspaceTarget!;
    const preflight = await preflightConfigSource(0, workspace, intent as ConfigSourceEditRequest);
    const accepted = privateInputConfirmed ? { save: true } : await quickInput.pick([{ label: '取消' }, { label: `确认保存工作区${confirmation}`, save: true,
      description: `${binding.package_id}/${binding.plugin_id} · ${change}`, detail: preflight.changed_paths.join(' · ') }],
    { title: '确认配置提交' });
    if (!accepted?.save) return;
    saved = await saveConfigSource(0, workspace, intent as ConfigSourceEditRequest);
    if (saved.edit?.idempotency_key !== intent.idempotency_key) throw new Error('工作区配置保存回执没有原提交身份');
  }
  const state = saved.edit?.state;
  const detail = `${saved.edit?.source_saved ? '来源已保存' : '来源未确认保存'} · ${state}。${saved.status?.last_error ?? saved.status?.reason ?? ''}`;
  if (state === 'rejected' || state === 'recovery_required') {
    notification.error(detail);
  } else {
    notification.info(`${detail} ${scope === 'defaults' ? '默认值只影响之后新建的工作区。'
      : '配置保存与实例启动分开；运行状态以目标 Gateway 回读为准。'}`);
  }
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
    const workspace = scope === 'workspace' ? (this.workspaceTarget ?? await chooseWorkspace(target, this.quickInput)) : undefined;
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

function writeOnlyFields(schema: Record<string, unknown> | undefined, prefix = ''): PrivateParameter[] {
  // TODO：补齐组合 schema 与数组项的秘密位置选择；当前表单枚举对象属性，写入口仍按完整 schema 校验。
  const properties = schema?.properties;
  if (!properties || typeof properties !== 'object' || Array.isArray(properties)) return [];
  return Object.entries(properties as Record<string, unknown>).flatMap(([key, candidate]) => {
    if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) return [];
    const value = candidate as Record<string, unknown>;
    const pointer = `${prefix}/${key.replace(/~/g, '~0').replace(/\//g, '~1')}`;
    return value.writeOnly === true ? [{ pointer, schema: value }] : writeOnlyFields(value, pointer);
  });
}

async function chooseWorkspace(target: ReturnType<typeof managementGateway>, quickInput: IQuickInputService): Promise<GatewayWorkspaceRequestTarget | undefined> {
  const workspaces = (await gateway.workspaces(false)).items.filter(item => item.gateway_id === target.gateway_id);
  const selected = await quickInput.pick(workspaces.map(workspace => ({ label: workspace.name, description: workspace.status,
    detail: workspace.workspace_id, workspace })), { title: '插件管理工作区', placeHolder: '本次只修改所选工作区配置 owner', canPickMany: false });
  return selected ? { gateway_id: target.gateway_id, workspace_id: selected.workspace.workspace_id, gateway_connection_id: target.gateway_connection_id } : undefined;
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
