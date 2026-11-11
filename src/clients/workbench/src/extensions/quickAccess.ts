import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { matchesFuzzy } from '../../upstream/src/vs/base/common/filters.js';
import { DisposableStore, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { PickerQuickAccessProvider, type IPickerQuickAccessItem } from '../../upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.js';
import type { IQuickPick } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IViewsService } from '../../upstream/src/vs/workbench/services/views/common/viewsService.js';
import type { PluginInventoryResultDTO, PluginTarget } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { gateway } from '../agentApi.js';
import { managementGateway } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import type { ServicePluginPackagesView } from './servicePluginManagement.js';

interface PluginPick extends IPickerQuickAccessItem { id: string }
interface PluginQuery {
  target: PluginTarget;
  entryGatewayId: string | null;
  controller: AbortController;
  invalidated: boolean;
  inventory?: Promise<PluginInventoryResultDTO>;
}

/** 原 ext 两个前缀只消费自有库存和原管理动作，不维护另一份安装模型。 */
export class ManagePluginQuickAccessProvider extends PickerQuickAccessProvider<PluginPick> {
  static readonly PREFIX: string = 'ext ';
  protected query?: PluginQuery;

  constructor(@ICommandService protected readonly commands: ICommandService,
    @INotificationService protected readonly notifications: INotificationService,
    @IViewsService private readonly views: IViewsService,
  ) { super(new.target.PREFIX, { noResultsPick: { id: 'no-plugin', label: '目标 Gateway 没有匹配的已安装插件' } }); }

  override provide(picker: IQuickPick<PluginPick, { useSeparators: true }>, token: CancellationToken) {
    const lifetime = new DisposableStore(), access = new CancellationTokenSource(token);
    const target = managementGateway('extensions');
    const query: PluginQuery = { target: { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id },
      entryGatewayId: workspaceContext.getSnapshot().gatewayId, controller: new AbortController(), invalidated: false };
    this.query = query;
    picker.placeholder = `${this instanceof InstallPluginQuickAccessProvider ? '安装自有插件包' : '查找已安装插件'} · ${target.name} · ${target.gateway_id}`;
    const invalidate = () => {
      query.invalidated = true;
      access.cancel();
      query.controller.abort();
      picker.activeItems = picker.selectedItems = [];
      picker.items = [{ id: 'plugin-target-invalidated', label: 'Gateway 或访问凭据已改变，请重新查找插件' }];
    };
    lifetime.add(toDisposable(onGatewayTokenInvalidated(0, invalidate)));
    lifetime.add(toDisposable(workspaceContext.subscribe(() => {
      if (workspaceContext.getSnapshot().gatewayId !== query.entryGatewayId) invalidate();
    })));
    lifetime.add(access.token.onCancellationRequested(() => query.controller.abort()));
    lifetime.add(toDisposable(() => { access.dispose(true); query.controller.abort(); if (this.query === query) this.query = undefined; }));
    lifetime.add(super.provide(picker, access.token));
    return lifetime;
  }

  protected check(query: PluginQuery): void {
    if (query.invalidated || workspaceContext.getSnapshot().gatewayId !== query.entryGatewayId) {
      throw new Error('原 Gateway 或访问凭据已改变，请重新查找插件');
    }
  }

  protected report(action: Promise<unknown>): void {
    void action.catch(error => this.notifications.error(error instanceof Error ? error.message : String(error)));
  }

  protected override async _getPicks(filter: string, _disposables: DisposableStore, token: CancellationToken): Promise<PluginPick[]> {
    const query = this.query!;
    try {
      this.check(query);
      const inventory = await (query.inventory ??= gateway.request<PluginInventoryResultDTO>('/api/gateway/service-plugins/inventory', {
        method: 'POST', body: JSON.stringify(query.target), signal: query.controller.signal,
      }));
      if (token.isCancellationRequested || query.invalidated) return [];
      return inventory.packages.flatMap(pkg => (pkg.manifest?.plugins ?? []).flatMap(plugin => {
        const manifest = pkg.manifest!;
        const scope = plugin.instance_scope;
        if (scope !== 'gateway' && scope !== 'workspace') throw new Error(`插件范围无效：${manifest.package_id}/${plugin.id}`);
        const label = `${plugin.name} · ${manifest.package_id}/${plugin.id}@${manifest.version}`;
        const highlights = matchesFuzzy(filter, label);
        if (filter && !highlights) return [];
        return [{ id: `${manifest.package_id}/${plugin.id}@${manifest.version}`, label,
          description: `${plugin.instance_scope === 'gateway' ? 'Gateway' : '工作区'} · ${plugin.runtime ?? '纯声明'}`,
          detail: `${query.target.gateway_id}${query.target.gateway_connection_id ? ` · 连接 ${query.target.gateway_connection_id}` : ''}`,
          highlights: { label: highlights ?? undefined },
          accept: () => this.report((async () => {
            this.check(query);
            const view = await this.views.openView<ServicePluginPackagesView>('boxteam.views.extensionPackages', true);
            this.check(query);
            if (!view) throw new Error('插件包管理视图当前不可用');
            await view.openPlugin(query.target, manifest.package_id, manifest.version, plugin.id, scope);
          })()),
        }];
      }));
    } catch (error) {
      if (token.isCancellationRequested || query.controller.signal.aborted) return [];
      return [{ id: 'plugin-inventory-failed', label: `插件库存不可用：${error instanceof Error ? error.message : String(error)}` }];
    }
  }
}

export class InstallPluginQuickAccessProvider extends ManagePluginQuickAccessProvider {
  static override readonly PREFIX = 'ext install ';

  protected override async _getPicks(_filter: string, _disposables: DisposableStore, _token: CancellationToken): Promise<PluginPick[]> {
    const query = this.query!;
    this.check(query);
    return [{ id: 'install-plugin-package', label: '安装自有插件包…', detail: `${query.target.gateway_id} · ZIP、开发目录或已配置的自有来源`,
      accept: () => {
        this.check(query);
        this.report(this.commands.executeCommand('boxteam.plugins.installPackage', { ...query.target }));
      } }];
  }
}
