import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError, isCancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { Disposable, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ExtensionIdentifier } from '../../upstream/src/vs/platform/extensions/common/extensions.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { McpServerLaunch, McpServerTransportType } from '../../upstream/src/vs/workbench/contrib/mcp/common/mcpTypes.js';
import type { ExtHostMcpShape } from '../../upstream/src/vs/workbench/api/common/extHost.protocol.js';
import type { IExtensionHost } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';
import type { Proxied } from '../../upstream/src/vs/workbench/services/extensions/common/proxyIdentifier.js';
import { getGatewayToken, onGatewayUserSessionInvalidated } from '../../../web/src/api/http.js';
import type { ExtensionMcpDefinitionChannelDTO, McpProviderCancelDTO, McpProviderDefinitionDTO, McpProviderLaunchDTO,
  McpProviderResolveDTO, McpProviderResolvedDTO, McpProviderSnapshotDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import type { McpProviderDefinitionRefDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { targetConfigurationRequest } from '../extensions/targetContext.js';

export interface LiveMcpProviderDefinition {
  readonly reference: McpProviderDefinitionRefDTO;
  readonly definition: McpProviderDefinitionDTO;
  readonly workspaceIds: readonly string[];
  readonly gatewayConnectionId: string | undefined;
  readonly check: () => void;
}

const liveBridges = new Set<TargetMcpDefinitions>();

/** 原宿主实时 metadata 的展示镜像；不以安装库存或持久 launch 恢复离线候选。 */
export function liveMcpProviderDefinitions(gatewayId: string, backendId: string, workspaceId: string): LiveMcpProviderDefinition[] {
  return [...liveBridges].flatMap(bridge => bridge.definitions()).filter(item => item.reference.gateway_id === gatewayId
    && item.reference.backend_id === backendId && item.workspaceIds.includes(workspaceId));
}

/** 原安装 owner 的完整回执即时撤销已卸载版本；读取与保存都不另建安装账本。 */
export function invalidateMcpProviderInstallations(gatewayId: string, backendId: string, extensionKeys: ReadonlySet<string>): void {
  for (const bridge of liveBridges) bridge.invalidateInstallations(gatewayId, backendId, extensionKeys);
}

export class TargetMcpDefinitions extends Disposable {
  private readonly snapshots = new Map<string, McpProviderSnapshotDTO>();
  private readonly pending = new Map<string, { providerRef: string; cancellation: CancellationTokenSource }>();
  private socket: WebSocket | undefined;
  private connecting: Promise<void> | undefined;
  private bound = false;
  private closed = false;
  private readonly context;

  constructor(private readonly host: IExtensionHost, private readonly proxy: Proxied<ExtHostMcpShape>, private readonly notifications: INotificationService) {
    super();
    this.context = host.targetContext;
    this._register(host.onExit(() => this.dispose()));
    this._register(toDisposable(onGatewayUserSessionInvalidated(0, () => this.dispose())));
    liveBridges.add(this);
  }

  private checkSnapshot(snapshot: McpProviderSnapshotDTO): void {
    const context = this.context;
    if (this.closed || !context || this.host.targetContext !== context) throw new CancellationError();
    const extensions = this.host.extensions;
    const extension = extensions?.allExtensions.find(item => ExtensionIdentifier.equals(item.identifier, snapshot.extension_id)
      && item.extensionKey === snapshot.extension_key && extensions.containsExtension(item.identifier));
    if (!extension || extension.installationTarget?.kind !== 'backend'
      || extension.installationTarget.gatewayId !== context.connection.gateway_id || extension.installationTarget.backendId !== context.connection.backend_id
      || !extension.contributes?.mcpServerDefinitionProviders?.some(item => ExtensionIdentifier.toKey(extension.identifier) + '/' + item.id === snapshot.collection_id)
      || new Set(snapshot.definitions.map(item => item.definition_ref)).size !== snapshot.definitions.length) {
      throw new Error('Provider 快照不属于原宿主的精确 VSIX 安装实例与 manifest 声明');
    }
  }

  upsert(snapshot: McpProviderSnapshotDTO): void {
    this.checkSnapshot(snapshot);
    this.snapshots.set(snapshot.provider_ref, snapshot);
    if (this.bound) this.send(snapshot);
    else void (this.connecting ??= this.connect()).catch(() => {
      this.notifications.error('动态 MCP Provider 控制通道不可用；原来源引用保留，请重新载入目标宿主后选择定义');
      this.dispose();
    });
    if (snapshot.error_code) this.notifications.error(`扩展 ${snapshot.extension_id} 的 MCP Provider 提供定义失败：${snapshot.error_code}`);
  }

  invalidate(providerRef: string): void {
    this.snapshots.delete(providerRef);
    for (const pending of this.pending.values()) if (pending.providerRef === providerRef) pending.cancellation.cancel();
    if (this.bound && !this.closed) this.send({ kind: 'invalidate', provider_ref: providerRef });
  }

  invalidateInstallations(gatewayId: string, backendId: string, extensionKeys: ReadonlySet<string>): void {
    if (this.context?.connection.gateway_id !== gatewayId || this.context.connection.backend_id !== backendId) return;
    for (const snapshot of this.snapshots.values()) if (!extensionKeys.has(snapshot.extension_key)) this.invalidate(snapshot.provider_ref);
  }

  definitions(): LiveMcpProviderDefinition[] {
    const context = this.context;
    if (this.closed || !this.bound || !context) return [];
    return [...this.snapshots.values()].flatMap(snapshot => snapshot.definitions.map(definition => {
      const check = () => {
        this.checkSnapshot(snapshot);
        if (!this.bound || this.snapshots.get(snapshot.provider_ref) !== snapshot) throw new CancellationError();
        if (context.kind === 'web' && definition.transport === 'stdio') throw new Error('Web Provider 没有目标原生进程上下文，不能启动 stdio');
      };
      return { definition, workspaceIds: [...context.connection.workspace_ids], gatewayConnectionId: context.gatewayConnectionId, check,
        reference: { gateway_id: context.connection.gateway_id, backend_id: context.connection.backend_id, client_id: context.connection.client_id,
          view_id: context.connection.view_id, host_generation: context.connection.generation, host_kind: context.kind,
          extension_id: snapshot.extension_id, extension_key: snapshot.extension_key, collection_id: snapshot.collection_id,
          provider_ref: snapshot.provider_ref, definition_ref: definition.definition_ref } };
    }));
  }

  private async connect(): Promise<void> {
    const context = this.context;
    if (this.closed || !context) throw new CancellationError();
    const token = await getGatewayToken(0);
    if (this.closed) throw new CancellationError();
    const url = new URL('/api/gateway/extension-hosts/mcp-definitions', location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    url.searchParams.set('token', token);
    const socket = this.socket = new WebSocket(url);
    this._register(toDisposable(() => socket.close()));
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('等待 Provider 控制通道超时')), 30_000);
      socket.addEventListener('open', () => socket.send(JSON.stringify({ ...targetConfigurationRequest(context), host_kind: context.kind })), { once: true });
      socket.addEventListener('close', () => {
        clearTimeout(timer);
        if (!this.closed) this.notifications.error('原 MCP Provider 宿主控制通道已离线；未启动定义已失效');
        this.dispose();
        reject(new Error('Provider 控制通道关闭'));
      }, { once: true });
      socket.addEventListener('message', event => {
        if (typeof event.data !== 'string') { socket.close(1003, 'named_json_required'); return; }
        if (!this.bound) {
          try {
            const binding = JSON.parse(event.data) as ExtensionMcpDefinitionChannelDTO;
            const expected = context.connection;
            if (binding.protocol_version !== 1 || binding.gateway_id !== expected.gateway_id || binding.backend_id !== expected.backend_id
              || binding.client_id !== expected.client_id || binding.view_id !== expected.view_id || binding.generation !== expected.generation
              || binding.host_kind !== context.kind || !binding.request_id
              || JSON.stringify(binding.workspace_ids) !== JSON.stringify(expected.workspace_ids) || this.closed) throw new Error('Provider 控制通道身份不匹配');
            this.bound = true;
            clearTimeout(timer);
            for (const snapshot of this.snapshots.values()) { this.checkSnapshot(snapshot); this.send(snapshot); }
            resolve();
          } catch (error) { clearTimeout(timer); reject(error); socket.close(1008, 'provider_binding_mismatch'); }
          return;
        }
        void this.receive(event.data).catch(() => socket.close(1008, 'provider_control_failed'));
      });
    });
  }

  private send(frame: object): void {
    if (this.closed || !this.bound || this.socket?.readyState !== WebSocket.OPEN) throw new CancellationError();
    this.socket.send(JSON.stringify(frame));
  }

  private async receive(raw: string): Promise<void> {
    const request = JSON.parse(raw) as McpProviderResolveDTO | McpProviderCancelDTO;
    if (!request.request_id || typeof request.request_id !== 'string') throw new Error('Provider 控制请求缺少原请求身份');
    if (request.kind === 'cancel') { this.pending.get(request.request_id)?.cancellation.cancel(); return; }
    if (request.kind !== 'resolve' || !('provider_ref' in request) || !('definition_ref' in request)
      || typeof request.provider_ref !== 'string' || !request.provider_ref || typeof request.definition_ref !== 'string' || !request.definition_ref
      || this.pending.has(request.request_id)) throw new Error('Provider 控制请求不是正式 resolve／cancel');
    const snapshot = this.snapshots.get(request.provider_ref);
    const definition = snapshot?.definitions.find(item => item.definition_ref === request.definition_ref);
    if (!snapshot || !definition) { this.send({ kind: 'resolved', request_id: request.request_id, error_code: 'definition_invalidated' }); return; }
    const cancellation = new CancellationTokenSource();
    this.pending.set(request.request_id, { providerRef: request.provider_ref, cancellation });
    let response: McpProviderResolvedDTO;
    try {
      this.checkSnapshot(snapshot);
      const serialized = await this.proxy.$resolveMcpLaunch(request.provider_ref, request.definition_ref, cancellation.token);
      this.checkSnapshot(snapshot);
      if (cancellation.token.isCancellationRequested || this.snapshots.get(request.provider_ref) !== snapshot) throw new CancellationError();
      if (!serialized) response = { kind: 'resolved', request_id: request.request_id, error_code: 'resolve_undefined' };
      else {
        const launch = McpServerLaunch.fromSerialized(serialized);
        let value: McpProviderLaunchDTO;
        if (launch.type === McpServerTransportType.Stdio) {
          if (this.context?.kind !== 'node' || definition.transport !== 'stdio' || launch.envFile || launch.sandbox) {
            this.send({ kind: 'resolved', request_id: request.request_id, error_code: 'unsupported_launch' }); return;
          }
          value = { transport: 'stdio', command: launch.command, args: [...launch.args], cwd: launch.cwd,
            env: Object.fromEntries(Object.entries(launch.env).filter(([, item]) => item !== null).map(([key, item]) => [key, String(item)])),
            unset_env: Object.entries(launch.env).filter(([, item]) => item === null).map(([key]) => key), headers: {} };
        } else {
          if (definition.transport !== 'streamable_http' || launch.authentication) {
            this.send({ kind: 'resolved', request_id: request.request_id, error_code: 'unsupported_launch' }); return;
          }
          value = { transport: 'streamable_http', url: launch.uri.toString(true), headers: Object.fromEntries(launch.headers), args: [], env: {}, unset_env: [] };
        }
        // 正式 launch 的互斥字段按实际传输发送，秘密只活在这次连接的内存中。
        const launchFrame = value.transport === 'stdio'
          ? { transport: value.transport, command: value.command!, args: value.args, env: value.env, unset_env: value.unset_env, ...(value.cwd === undefined ? {} : { cwd: value.cwd }) }
          : { transport: value.transport, url: value.url!, headers: value.headers };
        this.send({ kind: 'resolved', request_id: request.request_id, launch: launchFrame });
        return;
      }
    } catch (error) {
      response = { kind: 'resolved', request_id: request.request_id, error_code: isCancellationError(error) ? 'definition_invalidated' : 'resolve_failed' };
    } finally { this.pending.delete(request.request_id); cancellation.dispose(); }
    if (!this.closed) this.send(response);
  }

  override dispose(): void {
    if (this.closed) return;
    this.closed = true;
    this.bound = false;
    liveBridges.delete(this);
    this.snapshots.clear();
    for (const request of this.pending.values()) request.cancellation.cancel();
    super.dispose();
  }
}
