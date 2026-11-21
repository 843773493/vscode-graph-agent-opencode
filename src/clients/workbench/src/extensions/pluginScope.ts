import { t } from '../../../shared/core/i18n/index.js';
import { Disposable, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { Emitter } from '../../upstream/src/vs/base/common/event.js';
import { DeferredPromise, raceTimeout } from '../../upstream/src/vs/base/common/async.js';
import { Action } from '../../upstream/src/vs/base/common/actions.js';
import type { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { DEFAULT_API_REQUEST_TIMEOUT_MS } from '../../../web/src/api/http.js';
import { watchPluginRuntimeStatus } from '../../../web/src/api/pluginRuntimeStatus.js';
import type { PluginBinding, PluginInventoryResultDTO, PluginRuntimeStatusDTO, PluginTarget } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { createManagementTargetLifecycle } from '../managementTarget.js';
import { gateway } from '../agentApi.js';
import { manageConfig, readPluginBindings, writeOnlyFields, type Scope } from '../settings/pluginConfiguration.js';

export type PackageManifest = NonNullable<PluginInventoryResultDTO['packages'][number]['manifest']>;
export type PackagePlugin = PackageManifest['plugins'][number];
export const pluginError = (error: unknown) => error instanceof Error ? error.message : String(error);
export function pluginStateLabel(state: string): string {
  switch (state) {
    case 'stopped': return t('plugins.state.stopped');
    case 'starting': return t('plugins.state.starting');
    case 'ready': return t('plugins.state.ready');
    case 'stopping': return t('plugins.state.stopping');
    case 'failed': return t('plugins.state.failed');
    case 'waiting': return t('plugins.state.waiting');
    default: return state;
  }
}

/** 列表与详情各持有自己的固定范围；只观察原状态流，配置期望仍读原来源 owner。 */
export class PluginScope extends Disposable {
  private readonly changed = this._register(new Emitter<void>());
  readonly onDidChange = this.changed.event;
  readonly lifecycle = createManagementTargetLifecycle(t('plugins.operationTargetChanged'));
  private readonly controller = new AbortController();
  private revision = 0;
  bindings: readonly PluginBinding[] = [];
  runtime: PluginRuntimeStatusDTO | undefined;
  error: string | undefined;
  constructor(readonly scope: Scope, readonly target: PluginTarget, readonly workspace: GatewayWorkspaceRequestTarget | undefined,
    private readonly notifications: INotificationService) {
    super();
    this._register(toDisposable(() => { this.revision++; this.controller.abort(); this.lifecycle.dispose(); }));
    this.lifecycle.signal.addEventListener('abort', () => this.controller.abort(this.lifecycle.signal.reason), { once: true });
  }
  async load(): Promise<void> {
    if (this.scope === 'defaults') return this.refreshBindings();
    const first = new DeferredPromise<void>();
    this.controller.signal.addEventListener('abort', () => void first.cancel(), { once: true });
    const observe = (async () => {
      void watchPluginRuntimeStatus(this.target, this.controller.signal, snapshot => {
        if (this.controller.signal.aborted) return;
        const previous = this.runtime;
        this.runtime = snapshot;
        if (previous && JSON.stringify(previous.configured_bindings.filter(binding => binding.workspace_id === this.workspace?.workspace_id))
          !== JSON.stringify(snapshot.configured_bindings.filter(binding => binding.workspace_id === this.workspace?.workspace_id))) {
          void this.refreshBindings().catch(error => { if (!this.controller.signal.aborted) this.notifications.error(pluginError(error)); });
        }
        this.changed.fire();
        void first.complete();
      }).catch(error => {
        if (this.controller.signal.aborted) return;
        if (!first.isSettled) void first.error(error);
        else { this.error = pluginError(error); this.changed.fire(); this.notifications.error(this.error); }
      });
      if (await raceTimeout(first.p.then(() => true), DEFAULT_API_REQUEST_TIMEOUT_MS) !== true) {
        this.controller.abort();
        throw new Error(t('plugins.runtimeSnapshotTimeout'));
      }
    })();
    await Promise.all([observe, this.refreshBindings()]);
  }
  async refreshBindings(): Promise<void> {
    const revision = ++this.revision;
    const bindings = await readPluginBindings(this.scope, this.workspace ?? this.target);
    if (revision !== this.revision || this.controller.signal.aborted) return;
    this.bindings = bindings;
    this.changed.fire();
  }
  binding(packageId: string, pluginId: string): PluginBinding | undefined {
    return this.bindings.find(item => item.package_id === packageId && item.plugin_id === pluginId && (item.workspace_id ?? undefined) === this.workspace?.workspace_id);
  }
  slot(packageId: string, pluginId: string): PluginRuntimeStatusDTO['plugins'][number] | undefined {
    return this.runtime?.plugins.find(item => item.binding?.package_id === packageId && item.binding.plugin_id === pluginId
      && (item.binding.workspace_id ?? undefined) === this.workspace?.workspace_id);
  }
  configurationLabel(packageId: string, pluginId: string): string {
    const binding = this.binding(packageId, pluginId);
    return binding ? `${binding.enabled ? t('plugins.enabled') : t('plugins.disabled')} @${binding.package_version}` : t('plugins.unbound');
  }
  actualLabel(status: ReturnType<PluginScope['slot']>, runtime: boolean): string {
    return status?.instance ? t('plugins.actualVersion', pluginStateLabel(status.state), status.instance.package_version)
      : runtime ? t('plugins.actualNotReady', status ? pluginStateLabel(status.state) : t('plugins.notRunning')) : t('plugins.declarative');
  }
  label(packageId: string, pluginId: string, runtime: boolean): string {
    return `${this.configurationLabel(packageId, pluginId)} · ${this.actualLabel(this.slot(packageId, pluginId), runtime)}`;
  }
}

/** 同一动作供原侧栏菜单和详情工具栏使用，写入仍只有原配置流程。 */
export function pluginActions(state: PluginScope, manifest: PackageManifest, plugin: PackagePlugin,
  quickInput: IQuickInputService, notifications: INotificationService): Action[] {
  const { scope, target, workspace, lifecycle } = state;
  const binding = state.binding(manifest.package_id, plugin.id), status = state.slot(manifest.package_id, plugin.id);
  const otherVersion = binding && binding.package_version !== manifest.version;
  const value = { package_id: manifest.package_id, plugin_id: plugin.id, package_version: manifest.version, enabled: true };
  const actions: Action[] = [];
  const action = (id: string, label: string, run: () => Promise<unknown>) => {
    const item = new Action(id, label, undefined, true, async () => {
      item.enabled = false;
      try { lifecycle.check(); await run(); lifecycle.check(); }
      catch (error) { notifications.error(pluginError(error)); }
      finally {
        item.enabled = true;
        if (!lifecycle.signal.aborted) await state.refreshBindings().catch(error => notifications.error(pluginError(error)));
      }
    });
    actions.push(item);
  };
  const configure = (enabled: boolean | null | 'preserve', parameters = false, secret?: ReturnType<typeof writeOnlyFields>[number]) =>
    manageConfig(scope, target, workspace, quickInput, notifications, value, enabled, parameters, secret, lifecycle.check, lifecycle.token, lifecycle.signal);
  action('plugin.toggle', otherVersion ? t('plugins.switchVersion') : scope === 'defaults'
    ? binding?.enabled ? t('plugins.disableOnCreate') : t('plugins.enableOnCreate')
    : binding?.enabled ? t('plugins.disableScope') : t('plugins.enableScope'), () => configure(otherVersion ? 'preserve' : !binding?.enabled));
  if (binding && !otherVersion) action('plugin.unbind', scope === 'defaults' ? t('plugins.removeFromDefaults') : t('plugins.unbindScope'), () => configure(null));
  if (scope !== 'defaults' && !otherVersion) {
    action('plugin.parameters', t('plugins.editParametersJson'), () => configure(null, true));
    for (const field of writeOnlyFields(plugin.contributes?.configuration)) {
      action(`plugin.secret.${field.pointer}`, field.pointer ? t('plugins.manageSecret', field.pointer) : t('plugins.manageSecretLocation'), () => configure(null, false, field));
    }
    if (status?.revision && plugin.runtime && binding?.enabled && status.binding?.package_version === manifest.version) {
      action('plugin.restart', t('plugins.restartInstance'), () => gateway.request('/api/gateway/service-plugins/restart', {
        method: 'POST', body: JSON.stringify({ ...target, package_id: manifest.package_id, package_version: manifest.version,
          plugin_id: plugin.id, revision: status.revision, ...(workspace ? { workspace_id: workspace.workspace_id } : {}) }),
      }));
    }
  }
  return actions;
}
