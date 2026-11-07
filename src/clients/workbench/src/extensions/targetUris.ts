import { isEqualOrParent as isPathEqualOrParent, toSlashes } from '../../upstream/src/vs/base/common/extpath.js';
import { posix, win32 } from '../../upstream/src/vs/base/common/path.js';
import { dirname, ExtUri, extUri, joinPath, originalFSPath, relativePath } from '../../upstream/src/vs/base/common/resources.js';
import { equalsIgnoreCase } from '../../upstream/src/vs/base/common/strings.js';
import { URI, UriComponents } from '../../upstream/src/vs/base/common/uri.js';
import type { IURITransformer } from '../../upstream/src/vs/base/common/uriIpc.js';
import type { IExtensionDescription } from '../../upstream/src/vs/platform/extensions/common/extensions.js';
import type { ExtensionHostConnectionDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { workspaceFileUri } from '../workspaceFiles.js';
import type { IExtensionHost } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';
import { WorkspaceFolder } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import { IStoredWorkspaceFolder, toWorkspaceFolders } from '../../upstream/src/vs/platform/workspaces/common/workspaces.js';
import type { IWorkspaceEditDto } from '../../upstream/src/vs/workbench/api/common/extHost.protocol.js';
import { targetOperatingSystem, targetSettings, targetWorkspace, type TargetConfigurationContext } from './targetContext.js';

class TargetNativeUris extends ExtUri {
  constructor(private readonly windows: boolean) { super(() => windows); }
  override resolvePath(base: URI, path: string): URI {
    if (base.scheme !== 'file') return super.resolvePath(base, path);
    return URI.file(this.windows ? toSlashes(win32.resolve(originalFSPath(base), path)) : posix.resolve(base.path, path));
  }
}

/** Node 与 Web 共用业务归属和原配置 parser；Web 的 typed URI 始终保持 typed URI。 */
export class TargetWorkspaceResources {
  readonly folders: readonly { uri: URI; native: URI; name: string; index: number }[];
  protected readonly windows: boolean;
  protected readonly configurations: readonly { native: URI; uri: URI }[];
  private readonly viewConfiguration: URI | undefined;
  constructor(context: TargetConfigurationContext) {
    const workspace = targetWorkspace(context), settings = targetSettings(context);
    this.windows = targetOperatingSystem(context) === 'windows';
    this.viewConfiguration = workspace.configuration ? URI.parse(workspace.configuration.resource_uri) : undefined;
    this.configurations = [settings, workspace.configuration].filter(value => !!value).map(value => ({
      native: this.native(value.native_uri), uri: URI.parse(value.resource_uri),
    }));
    this.folders = workspace.folders.map((folder, index) => ({ index, name: folder.name,
      uri: workspaceFileUri({ gatewayId: context.connection.gateway_id, workspaceId: folder.workspace_id }, folder.resource_uri, folder.resource_uri),
      native: this.native(folder.native_uri),
    }));
  }
  protected native(value: string): URI {
    const uri = URI.parse(value);
    if (uri.scheme !== 'file' || !uri.path.startsWith('/') || uri.query || uri.fragment) throw new Error('宿主位置不是目标原生绝对 file URI');
    return uri;
  }
  /** 原生 file 路径使用目标平台；逻辑工作区的 Gateway、工作区与根身份始终严格。 */
  protected nativePath(uri: URI): string {
    return this.windows ? toSlashes(win32.normalize(originalFSPath(uri))) : posix.normalize(uri.path);
  }
  protected inside(resource: URI, base: URI): boolean {
    if (resource.scheme !== base.scheme) return false;
    if (resource.scheme !== 'file') return resource.authority === base.authority && extUri.isEqualOrParent(resource, base);
    const sameAuthority = this.windows ? equalsIgnoreCase(resource.authority, base.authority) : resource.authority === base.authority;
    return sameAuthority && resource.query === base.query && resource.fragment === base.fragment
      && isPathEqualOrParent(this.nativePath(resource), this.nativePath(base), this.windows, true);
  }
  ownsWorkspaceResource(uri: URI): boolean { return this.folders.some(folder => this.inside(uri, folder.uri)); }
  ownsConfigurationResource(uri: URI, parent = false): boolean {
    return this.configurations.some(resource => extUri.isEqual(uri, resource.uri) || parent && extUri.isEqual(uri, dirname(resource.uri)));
  }
  /** 原文件夹 parser 处理 raw metadata 和去重；目标平台解释原生路径，再核冻结的有序根。 */
  workspaceFolders(stored: IStoredWorkspaceFolder[], configuration: URI): WorkspaceFolder[] {
    const resource = this.configurations.find(value => extUri.isEqual(value.uri, configuration) && extUri.isEqual(configuration, this.viewConfiguration));
    if (!resource) throw new Error('工作区配置资源不属于当前目标视图');
    const native = toWorkspaceFolders(stored, resource.native, new TargetNativeUris(this.windows));
    if (native.length !== this.folders.length || native.some((folder, index) =>
      !this.inside(folder.uri, this.folders[index].native) || !this.inside(this.folders[index].native, folder.uri))) {
      throw new Error('目标视图的工作区集合已变化，请明确重新挂接并载入宿主');
    }
    return native.map((folder, index) => new WorkspaceFolder({ uri: this.folders[index].uri, name: folder.name, index }, folder.raw));
  }
}

/** 只有 Node 原协议反转已认证的 typed URI；相同原生路径对应多个身份时不任选一个。 */
export class TargetUriTransformer extends TargetWorkspaceResources implements IURITransformer {
  private readonly nativeHomes: readonly URI[];
  constructor(binding: ExtensionHostConnectionDTO, private readonly extensions: () => readonly IExtensionDescription[]) {
    super({ kind: 'node', connection: binding });
    const initialization = binding.initialization;
    if (!initialization?.environment) throw new Error('Node 宿主缺少初始化环境');
    this.nativeHomes = [initialization.environment.app_root_uri, initialization.environment.global_storage_home_uri,
      initialization.environment.workspace_storage_home_uri, initialization.logs_location_uri].map(value => this.native(value));
  }
  private isHostResource(uri: URI): boolean {
    return [...this.nativeHomes, ...this.extensions().map(extension => extension.extensionLocation), ...this.configurations.map(value => value.native)].some(base => this.inside(uri, base));
  }
  allowsOutgoingResource(uri: URI): boolean {
    if (uri.scheme === 'boxteam-file') return this.ownsWorkspaceResource(uri);
    return uri.scheme !== 'file' || this.isHostResource(uri) || this.folders.some(folder => this.inside(uri, folder.native));
  }
  transformIncoming(value: UriComponents): UriComponents {
    const uri = URI.revive(value);
    const configuration = this.configurations.find(resource => this.inside(uri, resource.native) && this.inside(resource.native, uri));
    if (configuration) return configuration.uri.toJSON();
    const configurationParent = this.configurations.find(resource => this.inside(uri, dirname(resource.native)) && this.inside(dirname(resource.native), uri));
    if (configurationParent) return dirname(configurationParent.uri).toJSON();
    if (uri.scheme === 'boxteam-file' && !this.ownsWorkspaceResource(uri)) {
      throw new Error('Node 返回的文件不属于已绑定工作区视图：' + uri.toString());
    }
    if (uri.scheme !== 'file') return value;
    const candidates = this.folders.filter(folder => this.inside(uri, folder.native)).sort((a, b) => b.native.path.length - a.native.path.length);
    const first = candidates[0];
    if (!first) {
      if (this.isHostResource(uri)) return value;
      throw new Error('Node 返回的文件超出已绑定工作区／扩展／存储位置：' + uri.toString());
    }
    if (candidates.some(folder => folder !== first && folder.native.path.length === first.native.path.length)) {
      throw new Error('原生文件路径对应多个业务工作区，缺少唯一请求上下文：' + uri.toString());
    }
    const suffix = this.windows ? toSlashes(win32.relative(this.nativePath(first.native), this.nativePath(uri))) : posix.relative(first.native.path, uri.path);
    return joinPath(first.uri, suffix).with({ query: uri.query, fragment: uri.fragment }).toJSON();
  }
  transformOutgoing(value: UriComponents): UriComponents {
    const uri = URI.revive(value);
    const configuration = this.configurations.find(resource => extUri.isEqual(uri, resource.uri));
    if (configuration) return configuration.native.toJSON();
    const configurationParent = this.configurations.find(resource => extUri.isEqual(uri, dirname(resource.uri)));
    if (configurationParent) return dirname(configurationParent.native).toJSON();
    if (uri.scheme === 'boxteam-file') {
      const folder = this.folders.find(folder => this.inside(uri, folder.uri));
      if (!folder) throw new Error('文件不属于接收 Node 宿主的明确工作区视图：' + uri.toString());
      const suffix = relativePath(folder.uri, uri);
      if (suffix === undefined) throw new Error('无法映射目标工作区文件位置');
      return folder.native.with({ path: posix.join(folder.native.path, suffix), query: uri.query, fragment: uri.fragment }).toJSON();
    }
    if (!this.allowsOutgoingResource(uri)) throw new Error('不能向目标 Node 宿主发送未绑定的本机文件：' + uri.toString());
    return value;
  }
  transformOutgoingURI(uri: URI): URI { return URI.revive(this.transformOutgoing(uri)); }
  transformOutgoingScheme(scheme: string): string { return scheme === 'boxteam-file' ? 'file' : scheme; }
}

export function hostOwnsWorkspaceResource(host: IExtensionHost, uri: URI): boolean {
  return !host.targetContext || hostResources(host).ownsWorkspaceResource(uri);
}

const resources = new WeakMap<IExtensionHost, TargetWorkspaceResources>();
export function hostResources(host: IExtensionHost): TargetWorkspaceResources {
  if (!host.targetContext) throw new Error('宿主没有已认证的目标业务 context');
  if (host.targetContext.kind === 'node') {
    if (!(host.uriTransformer instanceof TargetUriTransformer)) throw new Error('Node 宿主缺少已绑定 URI transformer');
    return host.uriTransformer;
  }
  let value = resources.get(host);
  if (!value) { value = new TargetWorkspaceResources(host.targetContext); resources.set(host, value); }
  return value;
}

/** 文件事件只投影当前宿主视图的工作区和已绑定配置文件。 */
export function hostOwnsFileEventResource(host: IExtensionHost, uri: URI, parent = false): boolean {
  if (!host.targetContext) return true;
  const resources = hostResources(host);
  return resources.ownsWorkspaceResource(uri) || resources.ownsConfigurationResource(uri, parent);
}

/** 编辑先核整批所有端点，不能先写合法首项再发现跨视图后项。 */
export function assertHostWorkspaceEdit(host: IExtensionHost, edit: IWorkspaceEditDto): void {
  for (const value of edit.edits) {
    const resources = ['resource' in value ? value.resource : undefined,
      'oldResource' in value ? value.oldResource : undefined, 'newResource' in value ? value.newResource : undefined];
    for (const raw of resources) {
      if (raw && !hostOwnsFileEventResource(host, URI.revive(raw))) throw new Error('扩展编辑超出发起宿主工作区视图：' + URI.revive(raw).toString());
    }
  }
}

/** 诊断的文档链接可以指向该宿主扩展内的帮助文件；业务文档仍须属于 workspace view。 */
export function hostAllowsResourceLink(host: IExtensionHost, uri: URI): boolean {
  if (!host.targetContext) return true;
  if (host.targetContext.kind === 'node') return (hostResources(host) as TargetUriTransformer).allowsOutgoingResource(uri);
  if (uri.scheme === 'boxteam-file' || uri.scheme === 'boxteam') return hostOwnsFileEventResource(host, uri);
  return uri.scheme !== 'file';
}

/** 窗口共享 FileService 必须在执行前核目标文件／配置归属；Web 不接收 Node 原生 file URI。 */
export function assertHostFileResource(host: IExtensionHost, uri: URI, parent = false): void {
  if (!host.targetContext) return;
  if (uri.scheme === 'boxteam-file' && !hostResources(host).ownsWorkspaceResource(uri)
    || uri.scheme === 'file' && host.targetContext.kind === 'web') throw new Error('文件不属于发起目标宿主的明确工作区视图：' + uri.toString());
  if (uri.scheme === 'boxteam' && !hostResources(host).ownsConfigurationResource(uri, parent)) {
    throw new Error('配置文件不属于发起目标宿主的明确 profile 或工作区视图：' + uri.toString());
  }
}
