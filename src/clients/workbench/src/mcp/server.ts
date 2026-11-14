import { t, formatDate, formatNumber } from '../../../shared/core/i18n/index.js';
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
  ready: t('workbench.mcp.status.ready'), disabled: t('workbench.mcp.status.disabled'), failed: t('workbench.mcp.status.failed'), authorization_required: t('workbench.mcp.status.authorizationRequired'), unavailable: t('workbench.mcp.status.unavailable'),
  pending: t('workbench.mcp.status.pending'), waiting_user: t('workbench.mcp.status.waitingUser'), verifying: t('workbench.mcp.status.verifying'),
  succeeded: t('workbench.mcp.status.succeeded'), expired: t('workbench.mcp.status.expired'), cancelled: t('workbench.mcp.status.cancelled'),
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
    if (this.catalog.snapshot.error || !this.data) return { state: McpConnectionState.Kind.Error, message: this.catalog.snapshot.error ?? t('workbench.mcp.missingService') };
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
    return status ? statusLabels[status] ?? status : t('workbench.mcp.removed');
  }
  get description(): string { return t('workbench.mcp.description', this.catalog.label, this.data?.transport ?? t('workbench.mcp.unknownTransport'), this.statusLabel, formatNumber(this.data?.tools.length ?? 0)); }
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
    if (!this.canConnect) throw new Error(t('workbench.mcp.cannotConnect'));
    await this.catalog.act(this.name, 'connect');
    return this.connectionState.get();
  }
  async stop(): Promise<void> {
    if (!this.canStop) throw new Error(t('workbench.mcp.cannotStop'));
    await this.catalog.act(this.name, pendingMcpOperation(this.operation) ? 'cancel' : 'disconnect');
  }
  authorize(): Promise<void> { return this.catalog.act(this.name, 'authorize'); }
  observe(): IDisposable { return this.watch(); }
  showOutput(): Promise<void> { return this.openDetails(this); }
  async *resources(): AsyncIterable<IMcpResource[]> { throw new Error(t('workbench.mcp.resourcesUnavailable')); }
  async resourceTemplates(): Promise<IMcpResourceTemplate[]> { throw new Error(t('workbench.mcp.templatesUnavailable')); }
  async getManifest(): Promise<never> { throw new Error(t('workbench.mcp.manifestUnavailable')); }
  async getReadme(token: CancellationToken): Promise<string> {
    if (token.isCancellationRequested) throw new CancellationError();
    const document = new MarkdownString();
    const line = (label: string, value: string) => { document.appendText(t('workbench.mcp.detailLine', label, value)); document.appendMarkdown('\n\n'); };
    document.appendMarkdown(t('workbench.mcp.detailsHeading'));
    line(t('workbench.mcp.serviceLabel'), this.name);
    line('Gateway', this.catalog.target.gateway_id);
    line(t('workbench.mcp.workspaceLabel'), this.catalog.target.workspace_id);
    if (this.catalog.target.backend_id) line('Backend', this.catalog.target.backend_id);
    if (this.catalog.target.gateway_connection_id) line(t('workbench.mcp.gatewayConnectionLabel'), this.catalog.target.gateway_connection_id);
    line(t('workbench.mcp.statusLabel'), this.statusLabel);
    line(t('workbench.mcp.transportLabel'), this.data?.transport ?? t('workbench.mcp.removed'));
    const snapshot = this.catalog.snapshot;
    if (snapshot.actionError) line(t('workbench.mcp.managementErrorLabel'), snapshot.actionError);
    if (snapshot.catalog) { line(t('workbench.mcp.catalogRevisionLabel'), snapshot.catalog.catalog_revision); line(t('workbench.mcp.catalogGenerationLabel'), String(snapshot.catalog.catalog_generation)); }
    if (this.data?.connection_ref) line(t('workbench.mcp.connectionLabel'), this.data.connection_ref);
    if (this.data?.service) {
      const source = this.data.service;
      line(t('workbench.mcp.pluginServiceLabel'), `${source.package_id}@${source.package_version} · ${source.plugin_id} · ${source.service_id}`);
      line('Gateway', source.gateway_id);
      line(t('workbench.mcp.pluginScopeLabel'), source.workspace_id ? t('workbench.mcp.workspaceScope', source.workspace_id) : 'Gateway');
    }
    if (this.data?.unavailable_reason) line(t('workbench.mcp.sourceUnavailableLabel'), this.data.unavailable_reason);
    if (this.operation) {
      line(t('workbench.mcp.operationLabel'), `${this.operation.action} · ${statusLabels[this.operation.status] ?? this.operation.status} · ${this.operation.stage}`);
      line(t('workbench.mcp.operationIdLabel'), this.operation.operation_id);
      if (this.operation.error_code) line(t('workbench.mcp.errorLabel'), this.operation.error_code);
      if (pendingMcpOperation(this.operation)) line(t('workbench.mcp.deadlineLabel'), formatDate(this.operation.deadline * 1000, { dateStyle: 'short', timeStyle: 'medium' }));
    }
    document.appendMarkdown(t('workbench.mcp.toolsHeading'));
    for (const tool of this.data?.tools ?? []) line(tool.remote_name, tool.description);
    document.appendText(t('workbench.mcp.toolsNote'));
    document.appendMarkdown('\n\n');
    document.appendText(t('workbench.mcp.managementSupport'));
    return document.value;
  }
  dispose(): void { /* 对象只是后端镜像；关闭详情不关闭目标连接。 */ }
}
