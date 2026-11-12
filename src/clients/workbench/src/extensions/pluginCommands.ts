import { Disposable, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { Emitter } from '../../upstream/src/vs/base/common/event.js';
import { IContextKeyService } from '../../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IProgressService, ProgressLocation } from '../../upstream/src/vs/platform/progress/common/progress.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IEditorGroupsService } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../../upstream/src/vs/workbench/common/contributions.js';
import { onGatewayTokenInvalidated, DEFAULT_API_REQUEST_TIMEOUT_MS } from '../../../web/src/api/http.js';
import type { PluginContributionTarget } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/plugin_commands.js';
import { gatewayWorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceContext } from '../workspaceContext.js';
import { captureSearchOwner } from '../search/target.js';
import { PluginCommandContributions, pluginGatewayContextKey, pluginWorkspaceContextKey, pluginTargetKey,
  type FrozenPluginCommand, type PluginCommandOwner } from './pluginCommandRegistry.js';
import { callPluginCommand, watchPluginCommands } from '../../../web/src/api/pluginCommands.js';

const remoteGatewayReconnected = new Emitter<string>();

export function notifyPluginCommandGatewayReconnected(connectionId: string): void {
  remoteGatewayReconnected.fire(connectionId);
}

/** 与搜索共用发起编辑器归属；空窗口允许明确的入口 Gateway。 */
export function capturePluginCommandOwner(editors: IEditorService,
  input: IEditorService['activeEditor'] | null = editors.activeEditor): PluginCommandOwner {
  const snapshot = workspaceContext.getSnapshot();
  if (!input && !snapshot.activeId && snapshot.gatewayId && !snapshot.loading && !snapshot.error) return { gatewayId: snapshot.gatewayId };
  return captureSearchOwner(editors, input);
}

class PluginCommands extends Disposable {
  private readonly contributions: PluginCommandContributions;
  private readonly streams = new Map<string, { target: PluginContributionTarget; abort: AbortController }>();
  private readonly calls = new Set<AbortController>();

  constructor(@IEditorService editors: IEditorService, @IEditorGroupsService groups: IEditorGroupsService, @IContextKeyService context: IContextKeyService,
    @INotificationService notifications: INotificationService) {
    super();
    this.contributions = this._register(new PluginCommandContributions(accessor => capturePluginCommandOwner(accessor.get(IEditorService)),
      (accessor, source, args) => this.execute(accessor, source, args)));
    const gatewayKey = context.createKey<string>(pluginGatewayContextKey, ''), workspaceKey = context.createKey<string>(pluginWorkspaceContextKey, '');
    const scoped = new Map<IContextKeyService, { gateway: ReturnType<typeof context.createKey<string>>; workspace: ReturnType<typeof context.createKey<string>> }>();
    const update = () => {
      let owner: PluginCommandOwner | undefined;
      try { owner = capturePluginCommandOwner(editors); } catch { /* 未明确归属时不显示其它目标的贡献。 */ }
      gatewayKey.set(owner?.gatewayId ?? ''); workspaceKey.set(owner?.workspaceId ?? '');
      for (const group of groups.groups) {
        const service = group.scopedContextKeyService;
        let keys = scoped.get(service);
        if (!keys) { keys = { gateway: service.createKey<string>(pluginGatewayContextKey, ''), workspace: service.createKey<string>(pluginWorkspaceContextKey, '') }; scoped.set(service, keys); }
        let groupOwner: PluginCommandOwner | undefined;
        try { groupOwner = capturePluginCommandOwner(editors, group.activeEditor); } catch { /* 没有合法归属时清除该组投影。 */ }
        keys.gateway.set(groupOwner?.gatewayId ?? ''); keys.workspace.set(groupOwner?.workspaceId ?? '');
      }
      for (const [service, keys] of scoped) if (!groups.groups.some(group => group.scopedContextKeyService === service)) {
        keys.gateway.reset(); keys.workspace.reset(); scoped.delete(service);
      }
      const snapshot = workspaceContext.getSnapshot(), targets = new Map<string, PluginContributionTarget>();
      if (!snapshot.loading && !snapshot.error && snapshot.gatewayId) {
        // 同一 Gateway 的远端连接不唯一时，不按编辑器顺序挑选一个管理路由。
        const ambiguousRoutes = new Set<string>();
        const addTarget = (target: PluginContributionTarget) => {
          const key = pluginTargetKey(target), current = targets.get(key);
          if (ambiguousRoutes.has(key)) return;
          if (current && current.gateway_connection_id !== target.gateway_connection_id) {
            targets.delete(key); ambiguousRoutes.add(key); return;
          }
          targets.set(key, target);
        };
        addTarget({ gateway_id: snapshot.gatewayId });
        const owners = new Map<string, PluginCommandOwner>();
        const addOwner = (owner: PluginCommandOwner) =>
          owners.set(JSON.stringify([owner.gatewayId, owner.workspaceId ?? null]), owner);
        try { addOwner(capturePluginCommandOwner(editors)); } catch { /* 没有当前明确 owner 时仅订阅入口 Gateway。 */ }
        const visibleEditors = new Set<NonNullable<IEditorService['activeEditor']>>();
        if (editors.activeEditor) visibleEditors.add(editors.activeEditor);
        for (const group of groups.groups) if (group.activeEditor) visibleEditors.add(group.activeEditor);
        for (const input of visibleEditors) {
          try { addOwner(capturePluginCommandOwner(editors, input)); } catch { /* 无法确定来源时不发起订阅。 */ }
        }
        for (const owner of owners.values()) {
          if (!owner.workspaceId) continue;
          const matches = snapshot.workspaces.filter(workspace => workspace.gateway_id === owner.gatewayId
            && workspace.workspace_id === owner.workspaceId);
          if (matches.length !== 1) continue;
          const workspace = matches[0];
          const routeOwner = gatewayWorkspaceOwner(workspace);
          let route: PluginContributionTarget;
          if (workspace.connection_kind === 'local' && routeOwner.gatewayId === snapshot.gatewayId) {
            route = { gateway_id: routeOwner.gatewayId };
          } else if (workspace.connection_kind === 'remote_gateway' && !workspace.connection_error) {
            if (routeOwner.gatewayId === snapshot.gatewayId) continue;
            const connections = workspace.remote_gateways.filter(item => item.gateway_id === routeOwner.gatewayId);
            if (connections.length !== 1) continue;
            route = { gateway_id: routeOwner.gatewayId, gateway_connection_id: connections[0].gateway_connection_id };
          } else continue;
          const target = { ...route, workspace_id: routeOwner.workspaceId };
          addTarget(route); addTarget(target);
        }
      }
      for (const [key, stream] of this.streams) {
        if (JSON.stringify(targets.get(key)) !== JSON.stringify(stream.target)) {
          stream.abort.abort(); this.contributions.remove(stream.target); this.streams.delete(key);
        }
      }
      for (const [key, target] of targets) if (!this.streams.has(key)) {
        const abort = new AbortController(), stream = { target, abort }; this.streams.set(key, stream);
        void watchPluginCommands(target, abort.signal, snapshot => {
          if (this.streams.get(key) !== stream || abort.signal.aborted) return;
          try { this.contributions.replace(target, snapshot); } catch (error) { notifications.error(error); }
        }).catch(error => {
          if (abort.signal.aborted) return;
          this.contributions.remove(target); notifications.error(error);
          // 保留失败槽位，普通焦点／导航不重试；窗口刷新重新订阅。
        });
      }
    };
    this._register(toDisposable(workspaceContext.subscribe(update)));
    this._register(editors.onDidActiveEditorChange(update));
    this._register(editors.onDidVisibleEditorsChange(update));
    this._register(groups.onDidAddGroup(update));
    this._register(groups.onDidRemoveGroup(update));
    this._register(remoteGatewayReconnected.event(connectionId => {
      let retired = false;
      for (const [key, stream] of this.streams) {
        if (stream.target.gateway_connection_id !== connectionId) continue;
        stream.abort.abort();
        this.contributions.remove(stream.target);
        this.streams.delete(key);
        retired = true;
      }
      if (retired) update();
    }));
    this._register(toDisposable(onGatewayTokenInvalidated(0, () => {
      for (const stream of this.streams.values()) { stream.abort.abort(); this.contributions.remove(stream.target); }
      this.streams.clear();
      for (const call of this.calls) call.abort(new Error('Gateway 身份已失效，原命令结果未知'));
      update();
    })));
    this._register(toDisposable(() => {
      for (const stream of this.streams.values()) stream.abort.abort(); this.streams.clear();
      for (const call of this.calls) call.abort(new Error('窗口已关闭，原命令结果未知'));
      gatewayKey.reset(); workspaceKey.reset();
      for (const keys of scoped.values()) { keys.gateway.reset(); keys.workspace.reset(); } scoped.clear();
    }));
    update();
  }

  private execute(accessor: ServicesAccessor, source: FrozenPluginCommand, args: unknown[]): Promise<unknown> {
    const progress = accessor.get(IProgressService), notifications = accessor.get(INotificationService);
    const abort = new AbortController(); this.calls.add(abort);
    const timer = setTimeout(() => abort.abort(new Error('插件命令期限已到，未取得终态则结果未知')), DEFAULT_API_REQUEST_TIMEOUT_MS);
    let cancel: (() => Promise<void>) | undefined, started = false, cancelled = false;
    const sendCancel = () => { if (started && cancel) void cancel().catch(error => notifications.error(error)); };
    return progress.withProgress({ location: ProgressLocation.Notification, title: source.command.title, cancellable: true }, reporter =>
      callPluginCommand({ ...source.target, package_id: source.publication.binding!.package_id,
        package_version: source.publication.binding!.package_version, plugin_id: source.publication.binding!.plugin_id,
        instance_id: source.publication.instance_id!, registration_id: source.registration!.registration_id,
        command_id: source.command.id }, args, { signal: abort.signal, onIdentity: value => { cancel = value; },
        onStarted: () => { started = true; if (cancelled) sendCancel(); },
        onProgress: (_completed, _total, message) => reporter.report({ message }),
      }), () => { if (!cancelled) { cancelled = true; sendCancel(); } })
      .finally(() => { clearTimeout(timer); this.calls.delete(abort); });
  }
}
registerWorkbenchContribution2('boxteam.pluginCommands', PluginCommands, WorkbenchPhase.AfterRestored);
