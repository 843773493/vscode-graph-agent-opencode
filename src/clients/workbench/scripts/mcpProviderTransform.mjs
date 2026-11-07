import { join } from 'node:path';

export const mcpProviderSourcePaths = [
  'src/vs/workbench/api/common/extHostMcp.ts',
  'src/vs/workbench/api/browser/mainThreadMcp.ts',
  'src/vs/workbench/api/common/extHost.protocol.ts',
  'src/vs/workbench/api/node/extHostMcpNode.ts',
];

function section(source, from, to, replacement, replaceOnce) {
  const begin = source.indexOf(from), end = source.indexOf(to, begin + from.length);
  if (begin < 0 || end < 0) throw new Error(`原 MCP Provider 转换边界缺失：${from}`);
  return replaceOnce(source, source.slice(begin, end), replacement);
}

/** 原对象和取消仍在 ExtHost；MainThread 不注册第二 MCP delegate／client。 */
export function transformMcpProviders(root, path, source, replaceOnce) {
  const relative = mcpProviderSourcePaths.find(item => path === join(root, 'upstream', item));
  if (!relative) return source;
  const publicTypes = JSON.stringify(join(root, '../web/src/types/protocol_generated/boxteam/gateway/v2/public.js'));
  let contents = source;
  if (relative.endsWith('/extHost.protocol.ts')) {
    contents = replaceOnce(contents, '$resolveMcpLaunch(collectionId: string, label: string): Promise<McpServerLaunch.Serialized | undefined>;',
      '$resolveMcpLaunch(providerRef: string, definitionRef: string, token: CancellationToken): Promise<McpServerLaunch.Serialized | undefined>;');
    contents = replaceOnce(contents, '$upsertMcpCollection(collection: McpCollectionDefinition.FromExtHost, servers: McpServerDefinition.Serialized[]): void;',
      '$upsertMcpCollection(snapshot: McpProviderSnapshotDTO): void;');
    contents = replaceOnce(contents, 'import { McpCollectionDefinition, McpConnectionState,', 'import { McpConnectionState,');
    return `import type { McpProviderSnapshotDTO } from ${publicTypes};\n` + contents;
  }
  if (relative.endsWith('/extHostMcpNode.ts')) {
    // 原 Node MCP stdio／HTTP client 已迁至 Workspace Owner，不保留未使用的启动实现。
    return `import { ExtHostMcpService } from '../common/extHostMcp.js';
export class NodeExtHostMpcService extends ExtHostMcpService {}
`;
  }
  if (relative.endsWith('/extHostMcp.ts')) {
    contents = section(contents, '\tprivate readonly _unresolvedMcpServers = new Map<', '\n\t// MCP server definitions synced',
      '\tprivate readonly _unresolvedMcpServers = new Map<string, McpProviderRegistration>();\n', replaceOnce);
    contents = section(contents, '\tasync $resolveMcpLaunch(', '\n\t/** {@link vscode.lm.startMcpGateway}', `
  async $resolveMcpLaunch(providerRef: string, definitionRef: string, token: CancellationToken): Promise<McpServerLaunch.Serialized | undefined> {
    const registration = this._unresolvedMcpServers.get(providerRef);
    if (!registration) throw new CancellationError();
    return registration.resolve(definitionRef, token);
  }

  public registerMcpConfigurationProvider(extension: IExtensionDescription, id: string, provider: vscode.McpServerDefinitionProvider): IDisposable {
    const metadata = extension.contributes?.mcpServerDefinitionProviders?.find(item => item.id === id);
    if (!metadata) throw new Error('MCP Provider 必须在原 VSIX manifest 中声明：' + id);
    if (!extension.extensionKey || extension.installationTarget?.kind !== 'backend') throw new Error('动态 MCP Provider 需要明确的目标 VSIX 安装实例');
    const store = new DisposableStore();
    const registration = store.add(new McpProviderRegistration(provider, {
      extension_id: extension.identifier.value, extension_key: extension.extensionKey,
      collection_id: extensionPrefixedIdentifier(extension.identifier, id),
    }, snapshot => this._proxy.$upsertMcpCollection(snapshot), reference => this._proxy.$deleteMcpCollection(reference),
      item => serverDataValidation.validateOrThrow(item), item => Convert.McpServerDefinition.from(item),
      error => this._logService.error('VSIX MCP provide 失败：' + extension.identifier.value + ' / ' + (error instanceof Error ? error.name : typeof error))));
    this._unresolvedMcpServers.set(registration.providerRef, registration);
    store.add(toDisposable(() => this._unresolvedMcpServers.delete(registration.providerRef)));
    this._initialProviderPromises.add(registration.ready);
    void registration.ready.then(() => this._initialProviderPromises.delete(registration.ready));
    return this._register(store);
  }
`, replaceOnce);
    const api = contents.slice(contents.indexOf('export const IExtHostMpcService'), contents.indexOf('const serverDataValidation'));
    const validation = contents.slice(contents.indexOf('const serverDataValidation'), contents.indexOf('export class ExtHostMcpService'));
    const registration = contents.slice(contents.indexOf('  async $resolveMcpLaunch('), contents.indexOf('\n\t/** {@link vscode.lm.startMcpGateway}'));
    return `import type * as vscode from 'vscode';
import { CancellationToken } from '../../../base/common/cancellation.js';
import { CancellationError } from '../../../base/common/errors.js';
import type { Event } from '../../../base/common/event.js';
import { Disposable, DisposableStore, IDisposable, toDisposable } from '../../../base/common/lifecycle.js';
import type { URI } from '../../../base/common/uri.js';
import { vArray, vNumber, vObj, vObjAny, vOptionalProp, vString } from '../../../base/common/validation.js';
import type { IExtensionDescription } from '../../../platform/extensions/common/extensions.js';
import { createDecorator } from '../../../platform/instantiation/common/instantiation.js';
import { ILogService } from '../../../platform/log/common/log.js';
import { extensionPrefixedIdentifier, McpServerLaunch } from '../../contrib/mcp/common/mcpTypes.js';
import { ExtHostMcpShape, MainContext, MainThreadMcpShape } from './extHost.protocol.js';
import { IExtHostRpcService } from './extHostRpcService.js';
import * as Convert from './extHostTypeConverters.js';
import { McpProviderRegistration } from ${JSON.stringify(join(root, 'src/mcp/providerRegistration.js'))};
${api}
${validation}
export class ExtHostMcpService extends Disposable implements IExtHostMpcService {
  private readonly _proxy: MainThreadMcpShape;
  private readonly _initialProviderPromises = new Set<Promise<void>>();
  private readonly _unresolvedMcpServers = new Map<string, McpProviderRegistration>();
  constructor(@IExtHostRpcService rpc: IExtHostRpcService, @ILogService private readonly _logService: ILogService) {
    super(); this._proxy = rpc.getProxy(MainContext.MainThreadMcp);
  }
  get onDidChangeMcpServerDefinitions(): Event<void> { throw this.unavailable(); }
  get mcpServerDefinitions(): readonly vscode.McpServerDefinition[] { throw this.unavailable(); }
  async $waitForInitialCollectionProviders(): Promise<void> { await Promise.all(this._initialProviderPromises); }
${registration}
  async startMcpGateway(): Promise<never> { throw this.unavailable(); }
  async $substituteVariables(): Promise<never> { throw this.unavailable(); }
  async $expandEnvironmentVariables(): Promise<never> { throw this.unavailable(); }
  async $resolveMcpPolicyVariables(): Promise<never> { throw this.unavailable(); }
  $startMcp(): never { throw this.unavailable(); }
  $stopMcp(): never { throw this.unavailable(); }
  $sendMessage(): never { throw this.unavailable(); }
  $onDidChangeMcpServerDefinitions(): never { throw this.unavailable(); }
  $onDidChangeGatewayServers(): never { throw this.unavailable(); }
  private unavailable(): Error { return new Error('此 MCP client／目录 API 未接入目标 Workspace Owner；动态 Provider 仅提供原定义'); }
}
`;
  }
  // 原 Provider→Registry delegate→ExtHost MCP client 链在此物理下线。
  return `import { Disposable } from '../../../base/common/lifecycle.js';
import { INotificationService } from '../../../platform/notification/common/notification.js';
import { extHostNamedCustomer, IExtHostContext } from '../../services/extensions/common/extHostCustomers.js';
import { ExtHostContext, MainContext, MainThreadMcpShape } from '../common/extHost.protocol.js';
import type { McpProviderSnapshotDTO } from ${publicTypes};
import { TargetMcpDefinitions } from ${JSON.stringify(join(root, 'src/mcp/providerBridge.js'))};

@extHostNamedCustomer(MainContext.MainThreadMcp)
export class MainThreadMcp extends Disposable implements MainThreadMcpShape {
  private readonly definitions: TargetMcpDefinitions;
  constructor(context: IExtHostContext, @INotificationService notifications: INotificationService) {
    super();
    this.definitions = this._register(new TargetMcpDefinitions(context.extensionHost, context.getProxy(ExtHostContext.ExtHostMcp), notifications));
  }
  $upsertMcpCollection(snapshot: McpProviderSnapshotDTO): void { this.definitions.upsert(snapshot); }
  $deleteMcpCollection(providerRef: string): void { this.definitions.invalidate(providerRef); }
  async $checkMcpServerAllowed(): Promise<never> { throw this.unavailable(); }
  $onDidChangeState(): never { throw this.unavailable(); }
  $onDidPublishLog(): never { throw this.unavailable(); }
  $onDidReceiveMessage(): never { throw this.unavailable(); }
  async $getTokenFromServerMetadata(): Promise<never> { throw this.unavailable(); }
  async $getTokenForProviderId(): Promise<never> { throw this.unavailable(); }
  $logMcpAuthSetup(): never { throw this.unavailable(); }
  async $startMcpGateway(): Promise<never> { throw this.unavailable(); }
  $disposeMcpGateway(): never { throw this.unavailable(); }
  private unavailable(): Error { return new Error('MCP 连接及授权归目标工作区的原 MCP Owner'); }
}
`;
}
