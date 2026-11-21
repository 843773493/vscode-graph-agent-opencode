import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { RunOnceScheduler } from '../upstream/src/vs/base/common/async.js';
import { joinPath } from '../upstream/src/vs/base/common/resources.js';
import type { URI } from '../upstream/src/vs/base/common/uri.js';
import { VSBuffer } from '../upstream/src/vs/base/common/buffer.js';
import { IFileService } from '../upstream/src/vs/platform/files/common/files.js';
import { Registry } from '../upstream/src/vs/platform/registry/common/platform.js';
import { CommandsRegistry } from '../upstream/src/vs/platform/commands/common/commands.js';
import { MenuId, MenuRegistry } from '../upstream/src/vs/platform/actions/common/actions.js';
import { ContextKeyExpr, IContextKeyService, type IContextKey } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { ThemeIcon } from '../upstream/src/vs/base/common/themables.js';
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { IViewsService } from '../upstream/src/vs/workbench/services/views/common/viewsService.js';
import { Extensions, IOutputChannelRegistry, IOutputService, OUTPUT_VIEW_ID, OutputChannelUpdateMode } from '../upstream/src/vs/workbench/services/output/common/output.js';
import { IWorkbenchEnvironmentService } from '../upstream/src/vs/workbench/services/environment/common/environmentService.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import { getGatewayDiagnostics, type GatewayLogTarget } from '../../web/src/gatewayApi.js';
import { watchPluginRuntimeStatus } from '../../web/src/api/pluginRuntimeStatus.js';
import { onGatewayTokenInvalidated } from '../../web/src/api/http.js';
import type { GatewayDiagnosticLog } from '../../web/src/types/backend.js';
import { t } from '../../shared/core/i18n/index.js';
import { managementGateway, managementWorkspace, selectManagementTarget, listManagementTargets, createManagementTargetLifecycle, onDidChangePanelManagementTarget } from './managementTarget.js';
import { readPluginInstanceLog } from './extensions/pluginEditor.js';
import { workspaceContext } from './workspaceContext.js';

interface LogSource {
  id: string;
  label: string;
  read: (signal: AbortSignal) => Promise<string>;
  text?: string;
  plugin?: boolean;
  workspaceScoped?: boolean;
}

/** 只注册与读取日志；原生 OutputService 唯一管理编辑器、通道、筛选和展示态。 */
class WorkspaceOutput extends Disposable {
  private readonly registry = Registry.as<IOutputChannelRegistry>(Extensions.OutputChannels);
  private readonly sources = new Map<string, LogSource & { resource: URI }>();
  private readonly refresh = this._register(new RunOnceScheduler(() => void this.readSelected(), 3000));
  private scope?: { key: string; abort: AbortController };
  private reading?: AbortController;
  private loading?: Promise<void>;
  private initialized = false;
  private readonly workspaceScopedContext: IContextKey<boolean>;

  constructor(
    @IOutputService private readonly output: IOutputService,
    @IViewsService private readonly views: IViewsService,
    @INotificationService private readonly notifications: INotificationService,
    @IFileService private readonly files: IFileService,
    @IWorkbenchEnvironmentService private readonly environment: IWorkbenchEnvironmentService,
    @IContextKeyService contexts: IContextKeyService,
  ) {
    super();
    this.workspaceScopedContext = contexts.createKey('boxteamOutputWorkspaceScoped', false);
    this._register({ dispose: workspaceContext.subscribe(() => this.updateScope()) });
    this._register(onDidChangePanelManagementTarget(() => this.updateScope(true)));
    this._register({ dispose: onGatewayTokenInvalidated(0, () => this.clearScope()) });
    this._register(views.onDidChangeViewVisibility(event => { if (event.id === OUTPUT_VIEW_ID) this.updateScope(); }));
    this._register(output.onActiveOutputChannel(() => void this.readSelected()));
    this._register(CommandsRegistry.registerCommand('boxteam.output.refresh', () => this.updateScope(true)));
    this._register(CommandsRegistry.registerCommand('boxteam.output.openLog', async (_accessor, request: GatewayLogTarget) => {
      await workspaceContext.ensureReady();
      const lifetime = createManagementTargetLifecycle(t('workbench.settings.reopenSource'));
      try {
        const targets = await listManagementTargets(lifetime.signal); lifetime.check();
        const target = targets.find(item => item.gateway_id === request.gateway_id
          && (!request.gateway_connection_id || item.gateway_connection_id === request.gateway_connection_id));
        if (!target) throw new Error(t('gateway.connections.ownerUnavailable'));
        selectManagementTarget('panel', target, request.workspace_id);
        await this.views.openView(OUTPUT_VIEW_ID, true); lifetime.check();
        this.updateScope();
        const scope = this.scope;
        await this.loading; lifetime.check();
        if (!scope || scope !== this.scope || scope.abort.signal.aborted) throw new Error(t('workbench.workspaceAction.identityChanged'));
        if (request.log_id) {
          const id = `boxteam.log.${encodeURIComponent(target.gateway_id)}.${encodeURIComponent(request.log_id)}`;
          if (!this.sources.has(id)) throw new Error(t('plugins.logMissing'));
          await this.output.showChannel(id);
        }
      } finally { lifetime.dispose(); }
    }));
    this._register(MenuRegistry.appendMenuItem(MenuId.ViewTitle, {
      command: { id: 'boxteam.output.refresh', title: t('bottom.gatewayLog.refreshOutput'), icon: ThemeIcon.fromId('refresh') },
      when: ContextKeyExpr.equals('view', OUTPUT_VIEW_ID), group: '3_output', order: 99,
    }));
    this._register({ dispose: () => this.clearScope() });
    this.updateScope();
  }

  private clearScope(): void {
    this.scope?.abort.abort(); this.scope = undefined;
    this.reading?.abort(); this.reading = undefined;
    this.refresh.cancel();
    const ids = [...this.sources.keys()];
    this.sources.clear();
    this.workspaceScopedContext.reset();
    for (const id of ids) this.registry.removeChannel(id);
  }

  private updateScope(force = false): void {
    const snapshot = workspaceContext.getSnapshot();
    if (!this.views.isViewVisible(OUTPUT_VIEW_ID)) {
      this.scope?.abort.abort();
      this.reading?.abort(); this.refresh.cancel();
      return;
    }
    if (snapshot.loading) return;
    if (snapshot.error) { this.clearScope(); this.notifications.error(snapshot.error); return; }
    try {
      const target = managementGateway('panel');
      const workspace = snapshot.workspaces.some(item => item.gateway_id === target.gateway_id)
        ? managementWorkspace('panel', target) : undefined;
      const diagnosticTarget = { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id, workspace_id: workspace?.workspace_id };
      const key = JSON.stringify([snapshot.gatewayId, target.gateway_id, target.gateway_connection_id, workspace?.workspace_id]);
      if (!force && this.scope?.key === key && !this.scope.abort.signal.aborted) return;
      const previous = this.output.getActiveChannel()?.id;
      // 隐藏只暂停读取；同一范围重开继续使用原通道及模型，避免原生视图恢复时引用悬空。
      if (this.scope?.key !== key) this.clearScope();
      else { this.scope.abort.abort(); this.reading?.abort(); this.refresh.cancel(); }
      const scope = { key, abort: new AbortController() };
      this.scope = scope;
      const idFor = (logId: string) => `boxteam.log.${encodeURIComponent(target.gateway_id)}.${encodeURIComponent(logId)}`;
      const register = async (source: LogSource) => {
        if (scope.abort.signal.aborted) return;
        if (this.sources.has(source.id)) return;
        // 沿原生窗口日志存储保留有界镜像；显式 source 避免 DelegatedOutputChannel
        // 在同一通道注销后重建时重复 createFile。Web／Electron 共用原 FileService。
        const resource = joinPath(this.environment.windowLogsPath, `${source.id}.log`);
        const registered = { ...source, resource };
        this.sources.set(source.id, registered);
        await this.files.writeFile(resource, VSBuffer.fromString(''));
        if (scope.abort.signal.aborted || this.sources.get(source.id) !== registered) return;
        this.registry.registerChannel({ id: source.id, label: source.label, log: true, source: { resource } });
      };
      this.loading = getGatewayDiagnostics(0, { target: diagnosticTarget, tailLines: 300, signal: scope.abort.signal }).then(async diagnostics => {
        if (scope.abort.signal.aborted) return;
        for (const log of diagnostics.logs) {
          const label = log.service === 'workspace_api' ? t('bottom.gatewayLog.workspaceBackend') : log.label;
          await register({ id: idFor(log.log_id), label, workspaceScoped: log.service === 'workspace_api', read: async signal => {
            const result = await getGatewayDiagnostics(0, { target: diagnosticTarget, logId: log.log_id, tailLines: 300, signal });
            const selected = result.logs.find(item => item.log_id === log.log_id);
            if (!selected) throw new Error(t('plugins.logMissing'));
            return this.logText(selected);
          } });
        }
        // 沿原通道选择；首次进入优先实际工作区后端，原生窗口等已选通道不被轮询抢走。
        const initial = previous && this.sources.has(previous) ? previous
          : diagnostics.selected_log_id ? idFor(diagnostics.selected_log_id) : undefined;
        const changeChannel = initial && (!this.initialized || !previous || previous.startsWith('boxteam.log.'))
          && this.output.getActiveChannel()?.id !== initial;
        if (scope.abort.signal.aborted) return;
        if (changeChannel) await this.output.showChannel(initial, true);
        this.initialized = true;
        if (!changeChannel && !scope.abort.signal.aborted) await this.readSelected();
      });
      void this.loading.catch(error => { if (!scope.abort.signal.aborted) this.notifications.error(error); });

      void watchPluginRuntimeStatus({ gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id }, scope.abort.signal, status => {
        if (scope.abort.signal.aborted) return;
        const ids = new Set<string>();
        const registrations: Promise<void>[] = [];
        for (const plugin of status.plugins) {
          if (!plugin.log_id || !plugin.binding || (plugin.binding.workspace_id && plugin.binding.workspace_id !== workspace?.workspace_id)) continue;
          const id = idFor(plugin.log_id); ids.add(id);
          const logId = plugin.log_id;
          registrations.push(register({ id, plugin: true, label: `${plugin.binding.plugin_id} · ${t('bottom.gatewayLog.pluginLogs')}`,
            read: signal => readPluginInstanceLog(target, logId, signal) }));
        }
        // 实例被替换后移除原通道；不把同名插件的新进程冒充旧实例日志。
        for (const [id, source] of this.sources) {
          if (source.plugin && !ids.has(id)) { this.sources.delete(id); this.registry.removeChannel(id); }
        }
        void Promise.all(registrations).catch(error => { if (!scope.abort.signal.aborted) this.notifications.error(error); });
      }).catch(error => { if (!scope.abort.signal.aborted) this.notifications.error(error); });
    } catch (error) { this.clearScope(); this.notifications.error(error instanceof Error ? error : String(error)); }
  }

  private logText(log: GatewayDiagnosticLog): string {
    if (log.status === 'unavailable') throw new Error(log.error ?? t('plugins.logUnreadable'));
    return log.tail;
  }

  private async readSelected(): Promise<void> {
    this.refresh.cancel(); this.reading?.abort();
    const scope = this.scope;
    const source = this.sources.get(this.output.getActiveChannel()?.id ?? '');
    this.workspaceScopedContext.set(source?.workspaceScoped === true);
    if (!scope || scope.abort.signal.aborted || !source || !this.views.isViewVisible(OUTPUT_VIEW_ID)) return;
    const reading = new AbortController(); this.reading = reading;
    try {
      const text = await source.read(reading.signal);
      if (reading.signal.aborted || scope.abort.signal.aborted) return;
      if (source.text !== text) {
        const channel = this.output.getChannel(source.id);
        if (!channel) throw new Error(t('plugins.logMissing'));
        await this.files.writeFile(source.resource, VSBuffer.fromString(text.endsWith('\n') ? text : `${text}\n`));
        if (reading.signal.aborted || scope.abort.signal.aborted) return;
        channel.update(OutputChannelUpdateMode.Replace, 0); source.text = text;
      }
      this.refresh.schedule();
    } catch (error) {
      if (!reading.signal.aborted && !scope.abort.signal.aborted) this.notifications.error(`${source.label}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
}
registerWorkbenchContribution2('boxteam.workspaceOutput', WorkspaceOutput, WorkbenchPhase.AfterRestored);
