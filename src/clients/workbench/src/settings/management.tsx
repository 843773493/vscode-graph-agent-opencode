import { Action, Separator } from '../../upstream/src/vs/base/common/actions.js';
import { Disposable, DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ToolBar } from '../../upstream/src/vs/base/browser/ui/toolbar/toolbar.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import { IContextMenuService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { IConfigurationService, ConfigurationTarget } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { ILogService } from '../../upstream/src/vs/platform/log/common/log.js';
import { IViewsService } from '../../upstream/src/vs/workbench/services/views/common/viewsService.js';
import { ReactSurface } from '../react/surface.js';
import { productStyles } from '../react/productStyles.js';
import WarmConfirmProvider from '../../../web/src/components/shell/WarmConfirmProvider.js';
import GatewaySettingsContent, { type GatewaySettingsPage } from '../../../web/src/components/workspace/gateway/GatewaySettingsContent.js';
import { BACKGROUND_SETTING } from '../../../web/src/theme/theme.js';
import { readWorkbenchBackground } from '../theme.js';
import { createManagementTargetLifecycle, listManagementTargets } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import { workspaceActionCommands } from '../search/workspaceActions.js';
import { workspaceOwnerFromKey, type GatewayRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { notifyPluginCommandGatewayReconnected } from '../extensions/pluginCommands.js';
import type { SessionNavigation } from '../sessionNavigation.js';
import type { SourceSettings } from './editor.js';
import type { SettingsCategory, SettingsCategoryOptions } from './categories.js';
import { t } from '../../../shared/core/i18n/index.js';
import styles from './management.css?text';
import './management.css';

const pageCategory = { connections: 'connections', credentials: 'connections', runtime: 'runtime', diagnostics: 'observability', theme: 'client' } as const;
const pageTitle = { connections: 'gateway.control.connections', credentials: 'gateway.control.connectionsAndCredentials',
  runtime: 'gateway.control.runtime', diagnostics: 'gateway.control.diagnostics', theme: 'gateway.theme.backgroundImage' } as const;

/** 管理内容与配置树共用同一 Settings Pane；原生工具栏只切换内容，不再创建控制中心。 */
export class SettingsManagementView extends Disposable {
  private readonly surface: ReactSurface;
  private readonly toolbar: ToolBar;
  private readonly actions = this._register(new DisposableStore());
  private lifetime!: ReturnType<typeof createManagementTargetLifecycle>;
  private readonly ready: Promise<void>;
  private category: SettingsCategory = 'client';
  private source?: SourceSettings;
  private page?: GatewaySettingsPage;
  private request: SettingsCategoryOptions = {};
  private dialog?: 'ssh' | 'external-device';
  private revision = 0;
  private paneVisible = true;
  private readonly heading = document.createElement('h2');
  constructor(private readonly element: HTMLElement, toolbarContainer: HTMLElement, private readonly visible: (active: boolean) => void,
    private readonly openGatewayConfiguration: (target: GatewayRequestTarget) => Promise<void>,
    @ICommandService private readonly commands: ICommandService,
    @IConfigurationService private readonly configuration: IConfigurationService,
    @INotificationService private readonly notifications: INotificationService,
    @IViewsService private readonly views: IViewsService,
    @IContextMenuService menus: IContextMenuService,
    @ILogService log: ILogService) {
    super();
    element.className = 'settings-management-view';
    this.heading.className = 'settings-group-title-label settings-group-level-1';
    const body = document.createElement('div'); body.className = 'settings-management-body';
    element.append(this.heading, body);
    toolbarContainer.className = 'settings-management-actions';
    this.toolbar = this._register(new ToolBar(toolbarContainer, menus));
    this.surface = this._register(new ReactSurface(body, [...productStyles, styles], message => log.error(message)));
    this._register({ dispose: workspaceContext.subscribe(() => { if (this.page) this.render(); }) });
    this._register(configuration.onDidChangeConfiguration(event => { if (this.page === 'theme' && event.affectsConfiguration(BACKGROUND_SETTING)) this.render(); }));
    this.ready = workspaceContext.ensureReady().then(() => {
      if (this._store.isDisposed) return;
      this.lifetime = this._register(createManagementTargetLifecycle(t('workbench.settings.reopenSource')));
      this._register(this.lifetime.token.onCancellationRequested(() => { this.hide(); this.toolbar.setActions([]); }));
      this.updateActions(); this.render();
    });
    this.ready.catch(error => { if (!this._store.isDisposed) this.notifications.error(error); });
    this._register({ dispose: () => { element.remove(); toolbarContainer.remove(); } });
  }
  get active(): boolean { return this.page !== undefined; }
  show(category: SettingsCategory, source: SourceSettings | undefined, request: SettingsCategoryOptions = {}, searching = false): void {
    const previousPage = this.category === category ? this.page : undefined;
    if (this.category === category && this.source === source && request.managementPage === undefined && !searching) {
      request = { ...this.request, ...request, managementPage: undefined };
    } else this.dialog = undefined;
    this.category = category; this.source = source; this.request = request;
    const localConnections = category === 'connections' && source?.location.gateway_id === workspaceContext.getSnapshot().gatewayId;
    this.page = searching ? undefined : request.managementPage ?? previousPage ?? (localConnections ? 'connections' : undefined);
    if (this.page && pageCategory[this.page] !== category) this.page = undefined;
    this.updateActions(); this.render();
  }
  hide(): void { this.page = undefined; this.dialog = undefined; this.surface.render(null); this.element.style.display = 'none'; this.visible(false); this.updateActions(); }
  setVisible(visible: boolean): void { this.paneVisible = visible; this.render(); }
  focus(): void { if (!this.surface.focus('button, input, select, summary')) this.toolbar.focus(); }
  private run(action: () => Promise<unknown> | void): void {
    this.ready.then(() => { this.lifetime.check(); return action(); }).catch(error => {
      if (!this._store.isDisposed) this.notifications.error(error instanceof Error ? error : String(error));
    });
  }
  private updateActions(): void {
    if (!this.lifetime) return;
    this.actions.clear();
    const action = (id: string, label: string, icon: string, run: () => Promise<unknown> | void) =>
      this.actions.add(new Action(id, label, ThemeIcon.asClassName(ThemeIcon.fromId(icon)), true, () => this.run(run)));
    const openPage = (page: GatewaySettingsPage) => {
      // 入口连接和设备授权由当前入口 Gateway 持有，不能冒充远端的管理目录。
      if (['connections', 'credentials'].includes(page) && this.source?.location.gateway_id !== this.lifetime.entryGatewayId) {
        return this.commands.executeCommand('boxteam.gateway.' + (page === 'credentials' ? 'credentials' : 'connections'));
      }
      this.page = page; this.dialog = undefined; this.updateActions(); this.render();
    };
    const pages: GatewaySettingsPage[] = this.category === 'connections' ? ['connections', 'credentials']
      : this.category === 'runtime' ? ['runtime'] : this.category === 'observability' ? ['diagnostics'] : this.category === 'client' ? ['theme'] : [];
    const actions = pages.map(page => action('settings.management.' + page, t(pageTitle[page]),
      page === 'connections' ? 'plug' : page === 'credentials' ? 'shield' : page === 'runtime' ? 'pulse' : page === 'diagnostics' ? 'output' : 'file-media', () => openPage(page)));
    if (this.category === 'client') actions.unshift(action('settings.management.colorTheme', t('gateway.theme.selectColorTheme'), 'color-mode',
      () => this.commands.executeCommand('workbench.action.selectTheme')));
    if (this.page === 'connections' || this.page === 'credentials') actions.push(action('settings.management.add',
      t(this.page === 'credentials' ? 'gateway.inbound.addDevice' : 'gateway.connection.addSsh'), 'add', () => {
        this.dialog = this.page === 'credentials' ? 'external-device' : 'ssh'; this.render();
      }));
    const secondary = [];
    if (this.page) {
      actions.push(action('settings.management.refresh', t('gateway.common.refresh'), 'refresh', async () => {
        await this.navigation('refresh'); this.revision++; this.render();
      }));
      secondary.push(action('settings.management.configuration', t('gateway.connections.openConfiguration'), 'settings-gear', () => {
        this.hide();
      }));
    }
    if (this.category === 'observability') secondary.push(action('settings.management.output', t('gateway.settings.openOutput'), 'output',
      () => this.commands.executeCommand('boxteam.output.openLog', { gateway_id: this.source?.location.gateway_id ?? this.lifetime.entryGatewayId,
        gateway_connection_id: this.source?.location.gateway_connection_id, workspace_id: this.source?.location.workspace_id })));
    if (this.category === 'runtime' && this.source?.location.gateway_id === this.lifetime.entryGatewayId) {
      secondary.push(new Separator(), action('settings.management.restartDevelopment', t('gateway.control.restartLocal'), 'debug-restart', () => this.commands.executeCommand('boxteam.gateway.restartDevelopment')));
    }
    this.toolbar.setActions(actions, secondary);
  }
  private async navigation(action: 'refresh' | Parameters<SessionNavigation['runGatewayAction']>[0]): Promise<void> {
    const view = await this.views.openView<SessionNavigation>('boxteam.sessions', false);
    this.lifetime.check();
    if (!view) throw new Error(t('workbench.workspaceAction.navigationUnavailable'));
    await view.runGatewayAction(action, () => this.lifetime.check());
  }
  private render(): void {
    if (!this.lifetime || this.lifetime.signal.aborted) return;
    this.element.style.display = this.page ? '' : 'none'; this.visible(!!this.page);
    if (!this.page || !this.paneVisible) { this.surface.render(null); return; }
    this.heading.textContent = t(pageTitle[this.page]);
    const location = this.source?.location;
    const snapshot = workspaceContext.getSnapshot();
    const workspaces = this.page === 'runtime' ? snapshot.workspaces.filter(workspace => workspace.gateway_id === location?.gateway_id) : snapshot.workspaces;
    this.surface.render(<WarmConfirmProvider><GatewaySettingsContent key={JSON.stringify([this.page, location?.gateway_id, location?.workspace_id])}
      page={this.page} entryGatewayId={this.lifetime.entryGatewayId} workspaces={workspaces} revision={this.revision}
      target={location ?? { gateway_id: this.lifetime.entryGatewayId }}
      dialog={this.dialog} onCloseDialog={() => { this.dialog = undefined; this.render(); }}
      onChanged={() => { this.revision++; this.render(); }}
      onAddRemoteGatewayConnection={payload => this.navigation(payload)} onRefresh={() => this.navigation('refresh')}
      onRemoteGatewayReconnected={notifyPluginCommandGatewayReconnected}
      onOpenConfiguration={async target => {
        this.hide();
        if (target) {
          // 日志保留属于 Gateway；即使 ID 相同，也必须离开当前工作区来源。
          await this.openGatewayConfiguration(target);
        }
      }}
      onOpenLogs={gatewayConnectionId => this.run(async () => {
        const targets = await listManagementTargets(this.lifetime.signal); this.lifetime.check();
        const target = targets.find(item => item.gateway_connection_id === gatewayConnectionId);
        if (!target) throw new Error(t('gateway.connections.ownerUnavailable'));
        await this.commands.executeCommand('boxteam.gateway.diagnostics', target);
      })}
      onOpenLog={target => this.commands.executeCommand('boxteam.output.openLog', target)}
      onStart={id => this.commands.executeCommand(workspaceActionCommands.start, workspaceOwnerFromKey(id))}
      onStop={id => this.commands.executeCommand(workspaceActionCommands.stop, workspaceOwnerFromKey(id))}
      onSafeRestart={(id, request) => this.commands.executeCommand(workspaceActionCommands.safeRestart, workspaceOwnerFromKey(id), request)}
      onForceRestart={(id, request) => this.commands.executeCommand(workspaceActionCommands.forceRestart, workspaceOwnerFromKey(id), request)}
      background={readWorkbenchBackground(this.configuration.getValue(BACKGROUND_SETTING))}
      onUpdateBackground={value => this.configuration.updateValue(BACKGROUND_SETTING, value, {}, ConfigurationTarget.USER)} />
    </WarmConfirmProvider>);
  }
}
