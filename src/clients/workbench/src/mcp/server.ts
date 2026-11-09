import { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { MarkdownString } from '../../upstream/src/vs/base/common/htmlContent.js';
import { CancellationError } from '../../upstream/src/vs/base/common/errors.js';
import type { IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { derived, observableValue } from '../../upstream/src/vs/base/common/observable.js';
import Severity from '../../upstream/src/vs/base/common/severity.js';
import { ContributionEnablementState } from '../../upstream/src/vs/workbench/contrib/chat/common/enablement.js';
import {
  IMcpServer, IMcpTool, IMcpPrompt, IMcpServerConnection, IMcpResource, IMcpResourceTemplate,
  IWorkbenchMcpServer, McpConnectionState, McpServerCacheState, McpServerEnablementState,
  McpServerInstallState,
} from '../../upstream/src/vs/workbench/contrib/mcp/common/mcpTypes.js';
import { WorkspaceMcpCatalog, pendingMcpOperation } from './catalog.js';

const statusLabels: Readonly<Record<string, string>> = {
  ready: '连接可用', disabled: '配置已禁用', failed: '原连接不可用', authorization_required: '需要授权', unavailable: '原服务不可用',
  pending: '已排队', waiting_user: '等待用户授权', verifying: '正在验证连接与目录',
  succeeded: '操作已完成', expired: '已过期', cancelled: '已取消',
};

/** 原列表、详情与生命周期命令共享此对象；全部业务状态从同一后端快照派生。 */
export class WorkspaceMcpServer implements IMcpServer, IWorkbenchMcpServer {
  readonly local = undefined;
  readonly gallery = undefined;
  readonly installable = undefined;
  readonly config = undefined;
  readonly readmeUrl = undefined;
  readonly installState = McpServerInstallState.Uninstalled;
  readonly codicon = 'plug';
  private readonly revision = observableValue(this, 0);
  private fingerprint = '';
  readonly connection = observableValue<IMcpServerConnection | undefined>(this, undefined);
  // 管理合同只公布工具摘要；Workbench 不注册可执行工具，Agent 仍消费 McpCatalogOwner 的完整目录。
  readonly tools = observableValue<readonly IMcpTool[]>(this, []);
  readonly prompts = observableValue<readonly IMcpPrompt[]>(this, []);
  readonly capabilities = observableValue(this, 0);
  readonly serverMetadata: IMcpServer['serverMetadata'] = observableValue(this, undefined);
  readonly enablement = derived(this, reader => {
    this.revision.read(reader);
    return this.data?.enabled ? ContributionEnablementState.EnabledWorkspace : ContributionEnablementState.DisabledWorkspace;
  });
  readonly connectionState = derived<McpConnectionState>(this, reader => {
    this.revision.read(reader);
    if (this.catalog.snapshot.error || !this.data) return { state: McpConnectionState.Kind.Error, message: this.catalog.snapshot.error ?? '此服务已不在目标工作区目录中' };
    if (pendingMcpOperation(this.operation)) return { state: McpConnectionState.Kind.Starting };
    if (this.data.status === 'ready') return { state: McpConnectionState.Kind.Running };
    if (this.data.status === 'failed') return { state: McpConnectionState.Kind.Error, message: this.operation?.error_code ?? this.statusLabel };
    return { state: McpConnectionState.Kind.Stopped, reason: this.data.status === 'authorization_required' ? 'needs-user-interaction' : undefined };
  });
  readonly cacheState = derived(this, reader => this.connectionState.read(reader).state === McpConnectionState.Kind.Running ? McpServerCacheState.Live : McpServerCacheState.Unknown);
  readonly definition: IMcpServer['definition'];
  readonly collection: IMcpServer['collection'];

  constructor(readonly catalog: WorkspaceMcpCatalog, readonly name: string,
    private readonly openDetails: (server: WorkspaceMcpServer) => Promise<void>, private readonly watch: () => IDisposable) {
    this.definition = { id: JSON.stringify([catalog.key, name]), label: name };
    this.collection = { id: catalog.key, label: `${catalog.label} · ${catalog.target.gateway_id}`, order: 0 };
  }

  get id(): string { return this.definition.id; }
  get label(): string { return this.name; }
  get data() { return this.catalog.server(this.name); }
  get operation() { return this.catalog.operation(this.name); }
  get isPending(): boolean { return pendingMcpOperation(this.operation); }
  get statusLabel(): string {
    if (this.catalog.snapshot.error) return this.catalog.snapshot.error;
    const status = this.operation && (pendingMcpOperation(this.operation) || ['cancelled', 'expired', 'failed'].includes(this.operation.status)) ? this.operation.status : this.data?.status;
    return status ? statusLabels[status] ?? status : '服务已移除';
  }
  get description(): string { return `${this.catalog.label} · ${this.data?.transport ?? '未知传输'} · ${this.statusLabel} · ${this.data?.tools.length ?? 0} 个已知工具`; }
  get runtimeStatus() {
    return { state: this.data?.enabled ? McpServerEnablementState.Enabled : McpServerEnablementState.DisabledWorkspace,
      message: { severity: this.catalog.snapshot.error || this.catalog.snapshot.actionError || this.operation?.error_code ? Severity.Error : Severity.Info,
        text: new MarkdownString().appendText(this.catalog.snapshot.actionError ?? this.description) } };
  }
  get canConnect(): boolean { return !this.catalog.isBusy && Boolean(this.data?.enabled) && !this.catalog.snapshot.error && !this.isPending && this.data?.status !== 'ready'; }
  get canStop(): boolean { return !this.catalog.isBusy && !this.catalog.snapshot.error && (this.isPending || this.data?.status === 'ready'); }
  get canAuthorize(): boolean { return !this.catalog.isBusy && !this.catalog.snapshot.error && this.operation?.status === 'waiting_user'; }
  changed(): boolean {
    const fingerprint = JSON.stringify([this.data, this.operation, this.catalog.isBusy, this.catalog.snapshot.error, this.catalog.snapshot.actionError, this.catalog.snapshot.catalog?.catalog_revision, this.catalog.snapshot.catalog?.catalog_generation]);
    if (fingerprint === this.fingerprint) return false;
    this.fingerprint = fingerprint;
    this.revision.set(this.revision.get() + 1, undefined);
    return true;
  }
  readDefinitions(): ReturnType<IMcpServer['readDefinitions']> { return observableValue(this, { server: undefined, collection: undefined }); }
  async start(): Promise<McpConnectionState> {
    if (!this.catalog.snapshot.error && this.data?.enabled && (this.isPending || this.data.status === 'ready')) return this.connectionState.get();
    if (!this.canConnect) throw new Error('此目标服务当前不可发起连接，请先查看原操作状态');
    await this.catalog.act(this.name, 'connect');
    return this.connectionState.get();
  }
  async stop(): Promise<void> {
    if (!this.canStop) throw new Error('此目标服务没有可断开连接或可取消操作');
    await this.catalog.act(this.name, pendingMcpOperation(this.operation) ? 'cancel' : 'disconnect');
  }
  authorize(): Promise<void> { return this.catalog.act(this.name, 'authorize'); }
  observe(): IDisposable { return this.watch(); }
  showOutput(): Promise<void> { return this.openDetails(this); }
  async *resources(): AsyncIterable<IMcpResource[]> { throw new Error('Workbench MCP 资源浏览尚未接入项目 owner'); }
  async resourceTemplates(): Promise<IMcpResourceTemplate[]> { throw new Error('Workbench MCP 资源模板尚未接入项目 owner'); }
  async getManifest(): Promise<never> { throw new Error('此工作区连接对象未提供制品 manifest，请使用目标制品管理入口'); }
  async getReadme(token: CancellationToken): Promise<string> {
    if (token.isCancellationRequested) throw new CancellationError();
    const document = new MarkdownString();
    const line = (label: string, value: string) => { document.appendText(`${label}：${value}`); document.appendMarkdown('\n\n'); };
    document.appendMarkdown('## MCP 服务\n\n');
    line('服务', this.name);
    line('Gateway', this.catalog.target.gateway_id);
    line('工作区', this.catalog.target.workspace_id);
    if (this.catalog.target.backend_id) line('Backend', this.catalog.target.backend_id);
    if (this.catalog.target.gateway_connection_id) line('Gateway 连接', this.catalog.target.gateway_connection_id);
    line('状态', this.statusLabel);
    line('传输', this.data?.transport ?? '服务已移除');
    const snapshot = this.catalog.snapshot;
    if (snapshot.actionError) line('管理请求错误', snapshot.actionError);
    if (snapshot.catalog) { line('目录 revision', snapshot.catalog.catalog_revision); line('目录 generation', String(snapshot.catalog.catalog_generation)); }
    if (this.data?.connection_ref) line('连接', this.data.connection_ref);
    if (this.data?.service) {
      const source = this.data.service;
      line('插件服务', `${source.package_id}@${source.package_version} · ${source.plugin_id} · ${source.service_id}`);
      line('Gateway', source.gateway_id);
      line('工作区', source.workspace_id);
    }
    if (this.data?.unavailable_reason) line('来源不可用', this.data.unavailable_reason);
    if (this.operation) {
      line('操作', `${this.operation.action} · ${statusLabels[this.operation.status] ?? this.operation.status} · ${this.operation.stage}`);
      line('操作 ID', this.operation.operation_id);
      if (this.operation.error_code) line('错误', this.operation.error_code);
      if (pendingMcpOperation(this.operation)) line('期限', new Date(this.operation.deadline * 1000).toLocaleString());
    }
    document.appendMarkdown('### 目标工具摘要\n\n');
    for (const tool of this.data?.tools ?? []) line(tool.remote_name, tool.description);
    document.appendText('工具执行仍使用此工作区的 Agent MCP owner；新工具在允许的上下文边界生效。插件服务由原安装域管理，连接及工具目录由工作区 owner 管理。');
    return document.value;
  }
  dispose(): void { /* 对象只是后端镜像；关闭详情不关闭目标连接。 */ }
}
