import { Disposable, DisposableStore, MutableDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
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
import { IQuickInputService, type IQuickPickItem } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';
import { ICodeEditorService } from '../upstream/src/vs/editor/browser/services/codeEditorService.js';
import { ITextModelService } from '../upstream/src/vs/editor/common/services/resolverService.js';
import { IViewsService } from '../upstream/src/vs/workbench/services/views/common/viewsService.js';
import { IStatusbarService, StatusbarAlignment, type IStatusbarEntryAccessor } from '../upstream/src/vs/workbench/services/statusbar/browser/statusbar.js';
import { Extensions, IOutputChannelRegistry, IOutputService, OUTPUT_VIEW_ID, OutputChannelUpdateMode } from '../upstream/src/vs/workbench/services/output/common/output.js';
import { IWorkbenchEnvironmentService } from '../upstream/src/vs/workbench/services/environment/common/environmentService.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import { getGatewayDiagnostics, locateGatewayDiagnosticLog, queryGatewayDiagnosticLogs, type GatewayLogTarget } from '../../web/src/gatewayApi.js';
import { watchPluginRuntimeStatus } from '../../web/src/api/pluginRuntimeStatus.js';
import { DEFAULT_API_REQUEST_TIMEOUT_MS, onGatewayTokenInvalidated } from '../../web/src/api/http.js';
import { errorSummary } from '../../web/src/utils/errorMessage.js';
import type { GatewayDiagnosticLog } from '../../web/src/types/backend.js';
import type { GatewayDiagnosticLogLocateRequest, GatewayDiagnosticLogQueryScopeDTO } from '../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { t } from '../../shared/core/i18n/index.js';
import { managementGateway, managementWorkspace, selectManagementTarget, listManagementTargets, createManagementTargetLifecycle, onDidChangePanelManagementTarget } from './managementTarget.js';
import { workspaceContext } from './workspaceContext.js';

interface LogSource {
  id: string;
  label: string;
  target: GatewayLogTarget;
  descriptor: GatewayDiagnosticLog;
  queryScope?: GatewayDiagnosticLogQueryScopeDTO;
  read: (signal: AbortSignal) => Promise<GatewayDiagnosticLog>;
  text?: string;
  status?: string;
  located?: boolean;
  plugin?: boolean;
  workspaceScoped?: boolean;
}

/** Gateway 返回码点；仅在已核验的原行内转换为 Monaco 的 UTF-16 列。 */
export function diagnosticLogSelection(text: string, start: number, end: number) {
  const points = Array.from(text);
  if (text.includes('\n') || text.includes('\r') || start < 0 || end <= start || end > points.length) throw new Error(t('plugins.logOwnerMismatch'));
  return { startLineNumber: 1, startColumn: points.slice(0, start).join('').length + 1,
    endLineNumber: 1, endColumn: points.slice(0, end).join('').length + 1 };
}

/** 只消费原诊断能力；原生 OutputService 管理通道、模型和展示态。 */
class WorkspaceOutput extends Disposable {
  private readonly registry = Registry.as<IOutputChannelRegistry>(Extensions.OutputChannels);
  private readonly sources = new Map<string, LogSource & { resource: URI }>();
  private readonly refresh = this._register(new RunOnceScheduler(() => void this.readSelected(), 3000));
  private readonly status = this._register(new MutableDisposable<IStatusbarEntryAccessor>());
  private scope?: { key: string; abort: AbortController };
  private reading?: AbortController;
  private navigation?: { abort: AbortController; channelId?: string; target: GatewayLogTarget; opening: boolean };
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
    @IQuickInputService private readonly quickInput: IQuickInputService,
    @ICodeEditorService private readonly codeEditors: ICodeEditorService,
    @ITextModelService private readonly models: ITextModelService,
    @IStatusbarService private readonly statusbar: IStatusbarService,
  ) {
    super();
    this.workspaceScopedContext = contexts.createKey('boxteamOutputWorkspaceScoped', false);
    this._register({ dispose: workspaceContext.subscribe(() => this.updateScope()) });
    this._register(onDidChangePanelManagementTarget(() => this.updateScope(true)));
    this._register({ dispose: onGatewayTokenInvalidated(0, () => this.clearScope()) });
    this._register(views.onDidChangeViewVisibility(event => { if (event.id === OUTPUT_VIEW_ID) this.updateScope(); }));
    this._register(output.onActiveOutputChannel(id => {
      if (this.navigation?.channelId && this.navigation.channelId !== id) this.navigation.abort.abort();
      if (!this.navigation) void this.readSelected();
    }));
    this._register(CommandsRegistry.registerCommand('boxteam.output.refresh', () => {
      const source = this.sources.get(this.output.getActiveChannel()?.id ?? '');
      if (source) return this.openLog(source.target, true);
      this.updateScope(true);
    }));
    this._register(CommandsRegistry.registerCommand('boxteam.output.openLog', (_accessor, request: GatewayLogTarget) => this.openLog(request)));
    this._register(MenuRegistry.appendMenuItem(MenuId.ViewTitle, {
      command: { id: 'boxteam.output.refresh', title: t('bottom.gatewayLog.refreshOutput'), icon: ThemeIcon.fromId('refresh') },
      when: ContextKeyExpr.equals('view', OUTPUT_VIEW_ID), group: '3_output', order: 99,
    }));
    this._register({ dispose: () => this.clearScope() });
    this.updateScope();
  }

  private idFor(gatewayId: string, logId: string): string {
    return `boxteam.log.${encodeURIComponent(gatewayId)}.${encodeURIComponent(logId)}`;
  }

  private showStatus(source?: LogSource): void {
    this.status.clear();
    if (!source?.status || !this.views.isViewVisible(OUTPUT_VIEW_ID)) return;
    this.status.value = this.statusbar.addEntry({ name: t('gateway.settings.openOutput'), text: `$(output) ${source.status}`,
      ariaLabel: `${source.label}: ${source.status}`, tooltip: `${source.label}: ${source.status}` }, 'boxteam.output.status', StatusbarAlignment.RIGHT, 1);
  }

  private clearScope(): void {
    this.scope?.abort.abort(); this.scope = undefined;
    this.reading?.abort(); this.reading = undefined;
    this.navigation?.abort.abort();
    this.refresh.cancel(); this.status.clear();
    const ids = [...this.sources.keys()];
    this.sources.clear(); this.workspaceScopedContext.reset();
    for (const id of ids) this.registry.removeChannel(id);
  }

  private updateScope(force = false): void {
    const snapshot = workspaceContext.getSnapshot();
    if (!this.views.isViewVisible(OUTPUT_VIEW_ID)) {
      this.scope?.abort.abort(); this.reading?.abort();
      if (this.navigation && !this.navigation.opening) this.navigation.abort.abort();
      this.refresh.cancel(); this.status.clear(); return;
    }
    if (snapshot.loading) return;
    if (snapshot.error) { this.clearScope(); if (!this.navigation) this.notifications.error(snapshot.error); return; }
    try {
      const target = managementGateway('panel');
      const workspace = snapshot.workspaces.some(item => item.gateway_id === target.gateway_id)
        ? managementWorkspace('panel', target) : undefined;
      const diagnosticTarget = { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id, workspace_id: workspace?.workspace_id };
      const key = JSON.stringify([snapshot.gatewayId, target.gateway_id, target.gateway_connection_id, workspace?.workspace_id]);
      if (!force && this.scope?.key === key && !this.scope.abort.signal.aborted) return;
      const previous = this.output.getActiveChannel()?.id;
      if (this.scope?.key !== key) {
        const navigation = this.navigation;
        if (navigation && !navigation.opening && (navigation.target.gateway_id !== target.gateway_id
          || navigation.target.gateway_connection_id && navigation.target.gateway_connection_id !== target.gateway_connection_id
          || navigation.target.workspace_id && navigation.target.workspace_id !== workspace?.workspace_id)) navigation.abort.abort();
        // 显式打开自身会更改面板目标；原操作继续核验冻结的目标。
        this.navigation = undefined; this.clearScope(); this.navigation = navigation;
      } else { this.scope.abort.abort(); this.reading?.abort(); this.refresh.cancel(); }
      const scope = { key, abort: new AbortController() }; this.scope = scope;
      const register = async (log: GatewayDiagnosticLog, plugin = false, queryable = true) => {
        if (scope.abort.signal.aborted) return;
        const id = this.idFor(target.gateway_id, log.log_id);
        const existing = this.sources.get(id);
        if (existing) { existing.plugin ||= plugin; return; }
        const logTarget = { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id,
          workspace_id: log.source === 'workspace' ? log.workspace_id ?? workspace?.workspace_id : undefined, log_id: log.log_id };
        const resource = joinPath(this.environment.windowLogsPath, `${id}.log`);
        const source: LogSource & { resource: URI } = { id, resource, target: logTarget, descriptor: log, plugin,
          label: log.service === 'workspace_api' ? t('bottom.gatewayLog.workspaceBackend') : log.label,
          workspaceScoped: log.source === 'workspace',
          queryScope: !queryable ? undefined : log.source === 'gateway' ? { source: log.source, workspace_ids: [] }
            : log.source === 'workspace' && logTarget.workspace_id ? { source: log.source, workspace_ids: [logTarget.workspace_id] } : undefined,
          read: async signal => {
            const result = await getGatewayDiagnostics(0, { target: logTarget, logId: log.log_id, tailLines: 300, signal });
            const selected = result.logs.find(item => item.log_id === log.log_id);
            if (result.gateway_id !== target.gateway_id || result.selected_log_id !== log.log_id || !selected
              || selected.source !== log.source || selected.workspace_id !== log.workspace_id) throw new Error(t('plugins.logOwnerMismatch'));
            return selected;
          } };
        this.sources.set(id, source);
        await this.files.writeFile(resource, VSBuffer.fromString(''));
        if (scope.abort.signal.aborted || this.sources.get(id) !== source) return;
        this.registry.registerChannel({ id, label: source.label, log: true, source: { resource } });
      };
      const requestedLog = this.navigation?.target.location?.reference?.log_id ?? this.navigation?.target.log_id;
      const discoveryTarget = requestedLog ? { ...target, workspace_id: this.navigation?.target.workspace_id } : diagnosticTarget;
      this.loading = getGatewayDiagnostics(0, { target: discoveryTarget, includeTail: false,
        logId: requestedLog, signal: scope.abort.signal }).then(async diagnostics => {
        if (scope.abort.signal.aborted) return;
        if (diagnostics.gateway_id !== target.gateway_id) throw new Error(t('plugins.logOwnerMismatch'));
        for (const log of diagnostics.logs) await register(log, false, log.source === 'gateway'
          || diagnostics.workspaces.some(item => item.workspace_id === log.workspace_id && item.connection_kind === 'local'));
        const initial = previous && this.sources.has(previous) ? previous
          : diagnostics.selected_log_id ? this.idFor(target.gateway_id, diagnostics.selected_log_id) : undefined;
        if (scope.abort.signal.aborted) return;
        if (this.navigation) {
          if (!this.navigation.target.log_id && !this.navigation.target.location && diagnostics.selected_log_id)
            await this.output.showChannel(this.idFor(target.gateway_id, diagnostics.selected_log_id), true);
        } else {
          if (initial && (!this.initialized || !previous || previous.startsWith('boxteam.log.'))
            && this.output.getActiveChannel()?.id !== initial) await this.output.showChannel(initial, true);
          else await this.readSelected();
        }
        this.initialized = true;
      });
      void this.loading.catch(error => { if (!scope.abort.signal.aborted && !this.navigation) this.notifications.error(error instanceof Error ? error : String(error)); });
      let runtimeVersion = 0;
      void watchPluginRuntimeStatus({ gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id }, scope.abort.signal, status => {
        if (scope.abort.signal.aborted) return;
        const version = ++runtimeVersion;
        const ids = new Set<string>();
        const registrations: Promise<void>[] = [];
        for (const plugin of status.plugins) {
          if (!plugin.log_id || !plugin.binding || (plugin.binding.workspace_id && plugin.binding.workspace_id !== workspace?.workspace_id)) continue;
          const id = this.idFor(target.gateway_id, plugin.log_id); ids.add(id);
          const existing = this.sources.get(id);
          if (existing) {
            if (existing.descriptor.source !== 'gateway') throw new Error(t('plugins.logOwnerMismatch'));
            existing.plugin = true; continue;
          }
          const logId = plugin.log_id;
          registrations.push(getGatewayDiagnostics(0, { target, logId, includeTail: false, signal: scope.abort.signal }).then(async result => {
            if (scope.abort.signal.aborted || version !== runtimeVersion) return;
            const log = result.logs.find(item => item.log_id === logId);
            if (result.gateway_id !== target.gateway_id || result.selected_log_id !== logId || !log || log.source !== 'gateway') throw new Error(t('plugins.logOwnerMismatch'));
            await register(log, true);
          }));
        }
        for (const [id, source] of this.sources) if (source.plugin && !ids.has(id)) {
          if (this.navigation?.channelId === id) this.navigation.abort.abort();
          if (this.output.getActiveChannel()?.id === id) { this.reading?.abort(); this.refresh.cancel(); this.status.clear(); }
          this.sources.delete(id); this.registry.removeChannel(id);
        }
        void Promise.all(registrations).catch(error => { if (!scope.abort.signal.aborted) this.notifications.error(error instanceof Error ? error : String(error)); });
      }).catch(error => { if (!scope.abort.signal.aborted) this.notifications.error(error instanceof Error ? error : String(error)); });
    } catch (error) {
      this.loading = Promise.reject(error);
      void this.loading.catch(() => {}); // 命令 await 原失败；背景发现只在此交付一次。
      this.clearScope(); if (!this.navigation) this.notifications.error(error instanceof Error ? error : String(error));
    }
  }

  private async writeText(source: LogSource & { resource: URI }, text: string, signal: AbortSignal): Promise<void> {
    if (source.text === text) return;
    const channel = this.output.getChannel(source.id);
    if (!channel) throw new Error(t('plugins.logMissing'));
    const reference = await this.models.createModelReference(channel.uri);
    try {
      signal.throwIfAborted();
      await this.files.writeFile(source.resource, VSBuffer.fromString(text));
      signal.throwIfAborted();
      const model = reference.object.textEditorModel;
      const expected = text.replace(/\r\n/g, '\n');
      await new Promise<void>((resolve, reject) => {
        const resources = new DisposableStore();
        const finish = (error?: unknown) => {
          resources.dispose(); clearTimeout(deadline); signal.removeEventListener('abort', cancel);
          error ? reject(error) : resolve();
        };
        const cancel = () => finish(signal.reason);
        const check = () => { if (model.getValue().replace(/\r\n/g, '\n') === expected) finish(); };
        const deadline = setTimeout(() => finish(new Error(t('bottom.gatewayLog.modelTimeout'))), DEFAULT_API_REQUEST_TIMEOUT_MS);
        signal.addEventListener('abort', cancel, { once: true });
        resources.add(model.onDidChangeContent(check));
        resources.add(model.onWillDispose(() => finish(new Error(t('plugins.logMissing')))));
        channel.update(OutputChannelUpdateMode.Replace, 0); check();
      });
      signal.throwIfAborted(); source.text = text;
    } finally { reference.dispose(); }
  }

  private async readSelected(report = true): Promise<void> {
    this.refresh.cancel(); this.reading?.abort();
    const scope = this.scope;
    const source = this.sources.get(this.output.getActiveChannel()?.id ?? '');
    this.workspaceScopedContext.set(source?.workspaceScoped === true); this.showStatus(source);
    if (!scope || scope.abort.signal.aborted || !source || source.located || !this.views.isViewVisible(OUTPUT_VIEW_ID)) return;
    const reading = new AbortController(); this.reading = reading;
    const cancel = () => reading.abort();
    const navigation = this.navigation;
    scope.abort.signal.addEventListener('abort', cancel, { once: true });
    navigation?.abort.signal.addEventListener('abort', cancel, { once: true });
    try {
      const log = await source.read(reading.signal);
      if (reading.signal.aborted) return;
      if (log.status === 'unavailable') throw new Error(log.error ?? t('plugins.logUnreadable'));
      await this.writeText(source, log.tail, reading.signal);
      source.status = log.status === 'empty' ? t('gateway.diagnostics.emptyLog')
        : log.truncated ? t('gateway.diagnostics.latestTailOnly') : t('bottom.gatewayLog.loaded');
      this.showStatus(source); this.refresh.schedule();
    } catch (error) {
      if (reading.signal.aborted) return;
      source.status = t(source.text === undefined ? 'gateway.diagnostics.readFailed' : 'gateway.diagnostics.refreshFailedWithSnapshot', errorSummary(error));
      this.showStatus(source);
      if (report) this.notifications.error(error instanceof Error ? error : String(error)); else throw error;
    } finally {
      scope.abort.signal.removeEventListener('abort', cancel); navigation?.abort.signal.removeEventListener('abort', cancel);
    }
  }

  private async openLog(request: GatewayLogTarget, refresh = false): Promise<void> {
    await workspaceContext.ensureReady();
    this.navigation?.abort.abort(); this.reading?.abort(); this.refresh.cancel();
    const navigation = { abort: new AbortController(), channelId: undefined as string | undefined, target: { ...request }, opening: true }; this.navigation = navigation;
    const lifetime = createManagementTargetLifecycle(t('workbench.settings.reopenSource'));
    const cancel = () => navigation.abort.abort(); lifetime.signal.addEventListener('abort', cancel, { once: true });
    const check = () => { lifetime.check(); navigation.abort.signal.throwIfAborted(); };
    try {
      check();
      const targets = (await listManagementTargets(lifetime.signal)).filter(item => item.gateway_id === request.gateway_id
        && (!request.gateway_connection_id || item.gateway_connection_id === request.gateway_connection_id));
      check();
      if (targets.length !== 1) throw new Error(t('gateway.connections.ownerUnavailable'));
      const target = targets[0];
      if (request.location?.reference && (request.location.reference.gateway_id !== request.gateway_id
        || request.log_id && request.log_id !== request.location.reference.log_id)) throw new Error(t('plugins.logOwnerMismatch'));
      selectManagementTarget('panel', target, request.workspace_id);
      await this.views.openView(OUTPUT_VIEW_ID, true); check(); navigation.opening = false;
      this.updateScope(refresh || !!request.log_id || !!request.location); await this.loading; check();
      const scope = this.scope;
      if (!scope || scope.abort.signal.aborted) throw new Error(t('workbench.workspaceAction.identityChanged'));
      const logId = request.location?.reference?.log_id ?? request.log_id;
      const id = logId ? this.idFor(request.gateway_id, logId) : undefined;
      const source = id ? this.sources.get(id) : this.sources.get(this.output.getActiveChannel()?.id ?? '');
      if (!source) throw new Error(t('plugins.logMissing'));
      navigation.channelId = source.id;
      await this.output.showChannel(source.id, true); check();
      if (request.location) await this.locate(source, request.location, navigation.abort.signal);
      else {
        source.located = false;
        await this.readSelected(false); check(); this.refresh.cancel();
        if (request.request_id) await this.findRequest(source, request.request_id, navigation.abort.signal);
      }
      check();
    } finally {
      lifetime.signal.removeEventListener('abort', cancel); lifetime.dispose();
      if (this.navigation === navigation) { this.navigation = undefined; if (!navigation.abort.signal.aborted) this.refresh.schedule(); }
    }
  }

  private async locate(source: LogSource & { resource: URI }, location: GatewayDiagnosticLogLocateRequest, signal: AbortSignal): Promise<void> {
    if (!source.queryScope || !location.scope || !location.reference || location.scope.source !== source.descriptor.source
      || source.descriptor.source === 'workspace' && !location.scope.workspace_ids.includes(source.target.workspace_id!)) throw new Error(t('plugins.logOwnerMismatch'));
    const result = await locateGatewayDiagnosticLog(0, location, { gatewayConnectionId: source.target.gateway_connection_id, signal });
    signal.throwIfAborted();
    if (!result.reference || !result.source || (Object.keys(location.reference) as (keyof typeof location.reference)[])
      .some(key => result.reference![key] !== location.reference![key])
      || result.source.log_id !== source.descriptor.log_id || result.source.source !== source.descriptor.source) throw new Error(t('plugins.logOwnerMismatch'));
    const selection = diagnosticLogSelection(result.text, result.reference.start_codepoint, result.reference.end_codepoint);
    source.located = true; this.refresh.cancel();
    await this.writeText(source, result.text, signal);
    await this.reveal(source, selection, signal);
    source.status = t('bottom.gatewayLog.located'); this.showStatus(source);
  }

  private async reveal(source: LogSource, selection: ReturnType<typeof diagnosticLogSelection>, signal: AbortSignal): Promise<void> {
    const channel = this.output.getChannel(source.id);
    if (!channel) throw new Error(t('plugins.logMissing'));
    const reference = await this.models.createModelReference(channel.uri);
    try {
      signal.throwIfAborted();
      const model = reference.object.textEditorModel;
      const editor = this.codeEditors.listCodeEditors().find(editor => editor.getModel() === model);
      if (!editor) throw new Error(t('plugins.logMissing'));
      editor.setSelection(selection); editor.revealRangeInCenter(selection);
    } finally { reference.dispose(); }
  }

  private async findRequest(source: LogSource & { resource: URI }, query: string, signal: AbortSignal): Promise<void> {
    if (!query || query.includes('\n') || query.includes('\r') || query.length > 512) throw new Error(t('plugins.logOwnerMismatch'));
    const picks: (IQuickPickItem & { line: number; column: number })[] = [];
    for (const [line, text] of (source.text ?? '').split(/\r?\n/).entries()) {
      let offset = 0, index: number;
      while (picks.length < 100 && (index = text.indexOf(query, offset)) !== -1) {
        picks.push({ label: text.slice(0, 200), description: `${line + 1}:${index + 1}`, line: line + 1, column: index + 1 }); offset = index + query.length;
      }
      if (picks.length === 100) break;
    }
    const token = new CancellationTokenSource();
    const cancel = () => token.cancel(); signal.addEventListener('abort', cancel, { once: true });
    try {
      if (picks.length) {
        const chosen = await this.quickInput.pick(picks, { title: t('bottom.gatewayLog.candidates'), matchOnDescription: true }, token.token);
        signal.throwIfAborted();
        if (chosen) {
          source.located = true;
          await this.reveal(source, { startLineNumber: chosen.line, startColumn: chosen.column,
            endLineNumber: chosen.line, endColumn: chosen.column + query.length }, signal);
          source.status = t('bottom.gatewayLog.located'); this.showStatus(source);
        }
        return;
      }
      if (!source.queryScope) { source.status = t('bottom.gatewayLog.tailOnly'); this.showStatus(source); return; }
      let cursor: string | undefined;
      do {
        const page = await queryGatewayDiagnosticLogs(0, source.target, { scope: source.queryScope, query, cursor, limit: 50 }, signal);
        signal.throwIfAborted();
        if (page.gateway_id !== source.target.gateway_id || page.query !== query || page.scope?.source !== source.queryScope.source
          || page.scope.workspace_ids.length !== source.queryScope.workspace_ids.length
          || page.scope.workspace_ids.some(id => !source.queryScope!.workspace_ids.includes(id))) throw new Error(t('plugins.logOwnerMismatch'));
        const matches = page.items.filter(item => item.reference && item.reference.log_id === source.target.log_id && item.reference.gateway_id === source.target.gateway_id
          && item.source?.source === source.descriptor.source && item.source.log_id === source.target.log_id);
        source.status = page.complete ? t('bottom.gatewayLog.queryComplete') : t('bottom.gatewayLog.queryIncomplete', page.limited_reason ?? ''); this.showStatus(source);
        const choices: (IQuickPickItem & { location?: GatewayDiagnosticLogLocateRequest; more?: boolean })[] = matches.map(item => ({
          label: item.preview, description: `${item.source!.label} · UTF-8 ${item.reference!.line_start} · ${item.reference!.start_codepoint}`,
          location: { scope: source.queryScope, reference: item.reference },
        }));
        if (!page.complete && page.cursor) choices.push({ label: t('bottom.gatewayLog.continueQuery'), more: true });
        if (!choices.length) return;
        const chosen = await this.quickInput.pick(choices, { title: t('bottom.gatewayLog.candidates'), placeHolder: source.status }, token.token);
        signal.throwIfAborted();
        if (!chosen) return;
        if (chosen.location) { await this.locate(source, chosen.location, signal); return; }
        cursor = chosen.more ? page.cursor : undefined;
      } while (cursor);
    } finally { signal.removeEventListener('abort', cancel); token.dispose(); }
  }
}
registerWorkbenchContribution2('boxteam.workspaceOutput', WorkspaceOutput, WorkbenchPhase.AfterRestored);
