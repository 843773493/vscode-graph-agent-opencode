import { raceCancellationError } from '../../upstream/src/vs/base/common/async.js';
import { VSBuffer } from '../../upstream/src/vs/base/common/buffer.js';
import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError, isCancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { Emitter, Event } from '../../upstream/src/vs/base/common/event.js';
import { Disposable, DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import { OperatingSystem, language } from '../../upstream/src/vs/base/common/platform.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import type { IMessagePassingProtocol } from '../../upstream/src/vs/base/parts/ipc/common/ipc.js';
import { ISocket, PersistentProtocol, SocketCloseEvent, SocketCloseEventType } from '../../upstream/src/vs/base/parts/ipc/common/ipc.net.js';
import { installationTargetKey } from '../../upstream/src/vs/platform/extensions/common/extensions.js';
import { ILogService } from '../../upstream/src/vs/platform/log/common/log.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IProductService } from '../../upstream/src/vs/platform/product/common/productService.js';
import { ITelemetryService } from '../../upstream/src/vs/platform/telemetry/common/telemetry.js';
import { isLoggingOnly } from '../../upstream/src/vs/platform/telemetry/common/telemetryUtils.js';
import { IWorkbenchEnvironmentService } from '../../upstream/src/vs/workbench/services/environment/common/environmentService.js';
import type { ExtensionDescriptionRegistrySnapshot } from '../../upstream/src/vs/workbench/services/extensions/common/extensionDescriptionRegistry.js';
import { IExtensionHostInitData, MessageType, UIKind, createMessageOfType, isMessageOfType } from '../../upstream/src/vs/workbench/services/extensions/common/extensionHostProtocol.js';
import type { RemoteRunningLocation } from '../../upstream/src/vs/workbench/services/extensions/common/extensionRunningLocation.js';
import type { ExtensionRunningLocationTracker } from '../../upstream/src/vs/workbench/services/extensions/common/extensionRunningLocationTracker.js';
import { ExtensionHostExtensions, ExtensionHostStartup, IExtensionHost } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';
import { IDefaultLogLevelsService } from '../../upstream/src/vs/workbench/services/log/common/defaultLogLevels.js';
import { getGatewayToken } from '../../../web/src/api/http.js';
import type { ExtensionHostConnectionDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { TargetUriTransformer } from './targetUris.js';
import { targetConfigurationServices } from './configuration.js';
import type { TargetExtensionContext } from './targetContext.js';

/** 原 IExtensionHost 的目标侧传输；Gateway 只转发原 PersistentProtocol 字节。 */
export class TargetExtensionHost extends Disposable implements IExtensionHost {
  readonly pid = null;
  readonly remoteAuthority: string;
  readonly startup = ExtensionHostStartup.EagerAutoStart;
  remoteOS: OperatingSystem | undefined;
  extensions: ExtensionHostExtensions | null = null;
  binding: ExtensionHostConnectionDTO | undefined;
  targetContext: TargetExtensionContext | undefined;
  uriTransformer: TargetUriTransformer | undefined;
  customerInstantiationService: IInstantiationService | undefined;
  private readonly configurationLifetime = this._register(new DisposableStore());
  private readonly startupCancellation = this._register(new CancellationTokenSource());
  private readonly exited = this._register(new Emitter<[number, string | null]>());
  readonly onExit = this.exited.event;
  private socket: WebSocket | undefined;
  private protocol: PersistentProtocol | undefined;
  private closing = false;
  private startPromise: Promise<IMessagePassingProtocol> | undefined;

  constructor(readonly runningLocation: RemoteRunningLocation,
    private readonly runningLocations: ExtensionRunningLocationTracker,
    private readonly snapshot: () => Promise<ExtensionDescriptionRegistrySnapshot>,
    @IWorkbenchEnvironmentService private readonly environment: IWorkbenchEnvironmentService,
    @IProductService private readonly product: IProductService,
    @ILogService private readonly logs: ILogService,
    @ITelemetryService private readonly telemetry: ITelemetryService,
    @IDefaultLogLevelsService private readonly defaultLogs: IDefaultLogLevelsService,
    @INotificationService private readonly notifications: INotificationService,
    @IInstantiationService private readonly instantiation: IInstantiationService,
  ) {
    super();
    this.remoteAuthority = installationTargetKey(runningLocation.installationTarget);
  }
  start(): Promise<IMessagePassingProtocol> {
    return this.startPromise ??= this.connect().catch(error => {
      this.configurationLifetime.dispose();
      this.socket?.close();
      if (!(this.closing && isCancellationError(error))) this.notifications.error(error instanceof Error ? error : String(error));
      throw error;
    });
  }
  private async connect(): Promise<IMessagePassingProtocol> {
    if (this.closing) throw new CancellationError();
    const snapshot = await raceCancellationError(this.snapshot(), this.startupCancellation.token);
    if (this.closing) throw new CancellationError();
    const all = this.runningLocations.getHostExtensions(snapshot.extensions, this.runningLocation);
    const mine = this.runningLocations.filterByRunningLocation(all, this.runningLocation);
    this.extensions = new ExtensionHostExtensions(snapshot.versionId, all, mine.map(extension => extension.identifier));
    // 认证可由其它窗口消费者共享；只取消本宿主等待，迟到结果不能创建新通道。
    const token = await raceCancellationError(getGatewayToken(0), this.startupCancellation.token);
    if (this.closing) throw new CancellationError();
    const url = new URL('/api/gateway/extension-hosts/channel', location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    url.searchParams.set('token', token);
    const socket = this.socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    const request = { protocol_version: 1, gateway_id: this.runningLocation.installationTarget.gatewayId,
      backend_id: this.runningLocation.installationTarget.backendId, view_id: this.runningLocation.viewId,
      workspace_ids: [...this.runningLocation.workspaceIds] };
    const binding = await new Promise<ExtensionHostConnectionDTO>((resolve, reject) => {
      const timer = setTimeout(() => { socket.close(); reject(new Error('等待 Node 宿主绑定超时')); }, 30_000);
      const cleanup = () => {
        clearTimeout(timer); cancellation.dispose(); socket.removeEventListener('open', onOpen);
        socket.removeEventListener('message', onMessage); socket.removeEventListener('close', onClose);
      };
      const cancellation = this.startupCancellation.token.onCancellationRequested(() => { cleanup(); reject(new CancellationError()); });
      const onOpen = () => socket.send(JSON.stringify(request));
      const onClose = (event: CloseEvent) => { cleanup(); reject(new Error('Node 宿主绑定前关闭：' + event.code + ' ' + event.reason)); };
      const onMessage = (event: MessageEvent<unknown>) => {
        try {
          if (typeof event.data !== 'string') throw new Error('Node 宿主绑定不是 JSON 文本');
          const value = JSON.parse(event.data) as ExtensionHostConnectionDTO;
          const initialization = value.initialization;
          if (value.protocol_version !== 1 || value.gateway_id !== request.gateway_id || value.backend_id !== request.backend_id
            || value.view_id !== request.view_id || JSON.stringify(value.workspace_ids) !== JSON.stringify(request.workspace_ids)
            || !value.client_id || !value.generation || !value.request_id || !initialization?.environment || !initialization.workspace
            || !initialization.workspace.id || !initialization.workspace.name || initialization.parent_pid <= 0
            || !['linux', 'windows', 'macos'].includes(initialization.os)
            || JSON.stringify(initialization.workspace.folders.map(folder => folder.workspace_id)) !== JSON.stringify(request.workspace_ids)) {
            throw new Error('Node 宿主绑定与请求目标、工作区集合或初始化环境不匹配');
          }
          this.binding = value;
          this.targetContext = { kind: 'node', connection: value };
          this.remoteOS = initialization.os === 'windows' ? OperatingSystem.Windows : initialization.os === 'macos' ? OperatingSystem.Macintosh : OperatingSystem.Linux;
          this.uriTransformer = new TargetUriTransformer(value, () => this.extensions?.allExtensions ?? []);
          // 同一消息回调中交给原协议，避免 Ready 字节先于二进制监听器到达。
          this.protocol = this._register(new PersistentProtocol({ socket: this._register(new GatewaySocket(socket)) }));
          cleanup(); resolve(value);
        } catch (error) { cleanup(); reject(error); }
      };
      socket.addEventListener('open', onOpen, { once: true });
      socket.addEventListener('message', onMessage);
      socket.addEventListener('close', onClose);
    });
    if (this.closing) throw new CancellationError();
    const protocol = this.protocol!;
    this._register(protocol.onSocketClose(event => {
      this.configurationLifetime.dispose();
      if (!this.closing) {
        // 通道已退出；原 owner 后续 disconnect 只结清资源，不再向死通道发送 Terminate。
        this.closing = true;
        this.startupCancellation.cancel();
        this.exited.fire([1, event && 'reason' in event ? event.reason : 'extension_host_channel_closed']);
      }
    }));
    const configuration = await raceCancellationError(
      targetConfigurationServices(this.instantiation, this.targetContext!, this.configurationLifetime), this.startupCancellation.token);
    if (this.closing) throw new CancellationError();
    if (this.configurationLifetime.isDisposed) throw new Error('Node 宿主在配置初始化时关闭');
    this.customerInstantiationService = configuration;
    await new Promise<void>((resolve, reject) => {
      const listeners = new DisposableStore();
      const timer = setTimeout(() => finish(new Error('Node 宿主原初始化握手超时')), 60_000);
      const finish = (error?: Error) => { clearTimeout(timer); listeners.dispose(); error ? reject(error) : resolve(); };
      let initialized = false;
      listeners.add(this.startupCancellation.token.onCancellationRequested(() => finish(new CancellationError())));
      listeners.add(protocol.onSocketClose(() => finish(new Error('Node 宿主在初始化时断开'))));
      listeners.add(protocol.onMessage(message => {
        if (isMessageOfType(message, MessageType.Ready) && !initialized) {
          initialized = true;
          protocol.send(VSBuffer.fromString(JSON.stringify(this.createInitData(binding))));
        } else if (isMessageOfType(message, MessageType.Initialized) && initialized) finish();
        else finish(new Error('Node 宿主初始化收到意外消息'));
      }));
    });
    if (this.closing) throw new CancellationError();
    return protocol;
  }
  private createInitData(binding: ExtensionHostConnectionDTO): IExtensionHostInitData {
    const initialization = binding.initialization!, environment = initialization.environment!, workspace = initialization.workspace!;
    return {
      commit: this.product.commit, version: this.product.version, quality: this.product.quality, date: this.product.date,
      isTargetExtensionHost: true,
      parentPid: initialization.parent_pid,
      environment: { isExtensionDevelopmentDebug: false, appRoot: URI.parse(environment.app_root_uri), appName: this.product.nameLong,
        appHost: this.product.embedderIdentifier || 'web', appUriScheme: this.product.urlProtocol, appLanguage: language,
        isExtensionTelemetryLoggingOnly: isLoggingOnly(this.product, this.environment),
        globalStorageHome: URI.parse(environment.global_storage_home_uri), workspaceStorageHome: URI.parse(environment.workspace_storage_home_uri),
        extensionLogLevel: this.defaultLogs.defaultLogLevels.extensions, isSessionsWindow: this.environment.isSessionsWindow },
      workspace: { id: workspace.id, name: workspace.name, transient: false },
      remote: { isRemote: true, authority: this.remoteAuthority, connectionData: null },
      consoleForward: { includeStack: false, logNative: false }, extensions: this.extensions!.toSnapshot(),
      telemetryInfo: { sessionId: this.telemetry.sessionId, machineId: this.telemetry.machineId, sqmId: this.telemetry.sqmId,
        devDeviceId: this.telemetry.devDeviceId ?? this.telemetry.machineId, firstSessionDate: this.telemetry.firstSessionDate,
        msftInternal: this.telemetry.msftInternal },
      remoteExtensionTips: this.product.remoteExtensionTips, virtualWorkspaceExtensionTips: this.product.virtualWorkspaceExtensionTips,
      logLevel: this.logs.getLevel(), loggers: [], logsLocation: URI.parse(initialization.logs_location_uri),
      autoStart: true, uiKind: UIKind.Web,
    };
  }
  getInspectPort(): undefined { return undefined; }
  async enableInspectPort(): Promise<boolean> { return false; }
  async disconnect(): Promise<void> {
    if (this.closing) return;
    this.closing = true;
    this.startupCancellation.cancel();
    this.configurationLifetime.dispose();
    try {
      if (this.protocol) {
        this.protocol.send(createMessageOfType(MessageType.Terminate));
        this.protocol.sendDisconnect();
        await this.protocol.drain();
      }
    } finally { this.socket?.close(); }
  }
  override dispose(): void { this.closing = true; this.startupCancellation.cancel(); this.socket?.close(); super.dispose(); }
}

class GatewaySocket extends Disposable implements ISocket {
  private readonly data = this._register(new Emitter<VSBuffer>());
  readonly onData = this.data.event;
  private readonly close = this._register(new Emitter<SocketCloseEvent>());
  readonly onClose = this.close.event;
  readonly onEnd = Event.None;
  constructor(private readonly socket: WebSocket) {
    super();
    const message = (event: MessageEvent<unknown>) => {
      if (!(event.data instanceof ArrayBuffer)) { socket.close(1002, 'Expected binary host protocol'); return; }
      this.data.fire(VSBuffer.wrap(new Uint8Array(event.data)));
    };
    const close = (event: CloseEvent) => this.close.fire({ type: SocketCloseEventType.WebSocketCloseEvent,
      code: event.code, reason: event.reason, wasClean: event.wasClean, event });
    socket.addEventListener('message', message); socket.addEventListener('close', close);
    this._register({ dispose: () => { socket.removeEventListener('message', message); socket.removeEventListener('close', close); socket.close(); } });
  }
  write(buffer: VSBuffer): void {
    if (this.socket.readyState !== WebSocket.OPEN) throw new Error('Node 宿主通道已关闭，拒绝后续写入');
    this.socket.send(buffer.buffer as Uint8Array<ArrayBuffer>);
  }
  end(): void { this.socket.close(); }
  async drain(): Promise<void> {
    const deadline = Date.now() + 5_000;
    while (this.socket.bufferedAmount && this.socket.readyState === WebSocket.OPEN) {
      if (Date.now() >= deadline) throw new Error('Node 宿主通道写入排空超时');
      await new Promise(resolve => setTimeout(resolve, 10));
    }
  }
  traceSocketEvent(): void { }
}
