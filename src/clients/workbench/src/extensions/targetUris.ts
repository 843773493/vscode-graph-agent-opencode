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

class TargetNativeUris extends ExtUri {
  constructor(private readonly windows: boolean) { super(() => windows); }
  override resolvePath(base: URI, path: string): URI {
    if (base.scheme !== 'file') return super.resolvePath(base, path);
    return URI.file(this.windows ? toSlashes(win32.resolve(originalFSPath(base), path)) : posix.resolve(base.path, path));
  }
}

/** 只反转已认证 mount 根的 typed URI；相同原生路径对应多个身份时不任选一个。 */
export class TargetUriTransformer implements IURITransformer {
  readonly folders: readonly { uri: URI; native: URI; name: string; index: number }[];
  private readonly nativeHomes: readonly URI[];
  private readonly windows: boolean;
  private readonly configurations: readonly { native: URI; uri: URI }[];
  private readonly viewConfiguration: URI | undefined;
  constructor(binding: ExtensionHostConnectionDTO, private readonly extensions: () => readonly IExtensionDescription[]) {
    const initialization = binding.initialization;
    if (!initialization?.workspace || !initialization.environment) throw new Error('Node 宿主缺少初始化目录与环境');
    this.windows = initialization.os === 'windows';
    const settings = initialization.environment.remote_settings;
    if (!settings) throw new Error('Node 宿主缺少目标原生 profile 设置资源');
    this.viewConfiguration = initialization.workspace.configuration ? URI.parse(initialization.workspace.configuration.resource_uri) : undefined;
    this.configurations = [settings, initialization.workspace.configuration].filter(value => !!value).map(value => ({
      native: this.native(value.native_uri), uri: URI.parse(value.resource_uri),
    }));
    this.folders = initialization.workspace.folders.map((folder, index) => ({ index, name: folder.name,
      uri: workspaceFileUri({ gatewayId: binding.gateway_id, workspaceId: folder.workspace_id }, folder.resource_uri, folder.resource_uri),
      native: this.native(folder.native_uri),
    }));
    this.nativeHomes = [initialization.environment.app_root_uri, initialization.environment.global_storage_home_uri,
      initialization.environment.workspace_storage_home_uri, initialization.logs_location_uri].map(value => this.native(value));
  }
  private native(value: string): URI {
    const uri = URI.parse(value);
    if (uri.scheme !== 'file' || !uri.path.startsWith('/') || uri.query || uri.fragment) throw new Error('宿主位置不是目标原生绝对 file URI');
    return uri;
  }
  /** 原生 file 路径使用目标平台；逻辑工作区的 Gateway、工作区与根身份始终严格。 */
  private nativePath(uri: URI): string {
    return this.windows ? toSlashes(win32.normalize(originalFSPath(uri))) : posix.normalize(uri.path);
  }
  private inside(resource: URI, base: URI): boolean {
    if (resource.scheme !== base.scheme) return false;
    if (resource.scheme !== 'file') return resource.authority === base.authority && extUri.isEqualOrParent(resource, base);
    const sameAuthority = this.windows ? equalsIgnoreCase(resource.authority, base.authority) : resource.authority === base.authority;
    return sameAuthority && resource.query === base.query && resource.fragment === base.fragment
      && isPathEqualOrParent(this.nativePath(resource), this.nativePath(base), this.windows, true);
  }
  private isHostResource(uri: URI): boolean {
    return [...this.nativeHomes, ...this.extensions().map(extension => extension.extensionLocation), ...this.configurations.map(value => value.native)].some(base => this.inside(uri, base));
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
  if (!host.binding) return true;
  if (!(host.uriTransformer instanceof TargetUriTransformer)) throw new Error('Node 宿主缺少已绑定 URI transformer');
  return host.uriTransformer.ownsWorkspaceResource(uri);
}

/** 文件事件只投影当前宿主视图的工作区和已绑定配置文件。 */
export function hostOwnsFileEventResource(host: IExtensionHost, uri: URI, parent = false): boolean {
  if (!host.binding) return true;
  if (!(host.uriTransformer instanceof TargetUriTransformer)) throw new Error('Node 宿主缺少已绑定 URI transformer');
  return host.uriTransformer.ownsWorkspaceResource(uri) || host.uriTransformer.ownsConfigurationResource(uri, parent);
}

/** 诊断的文档链接可以指向该宿主扩展内的帮助文件；业务文档仍须属于 workspace view。 */
export function hostAllowsResourceLink(host: IExtensionHost, uri: URI): boolean {
  if (!host.binding) return true;
  if (!(host.uriTransformer instanceof TargetUriTransformer)) throw new Error('Node 宿主缺少已绑定 URI transformer');
  return host.uriTransformer.allowsOutgoingResource(uri);
}

/** boxteam 的配置文件服务是窗口共享 provider，文件 RPC 必须先核当前宿主的精确资源。 */
export function assertHostConfigurationFile(host: IExtensionHost, uri: URI, parent = false): void {
  if (!host.binding || uri.scheme !== 'boxteam') return;
  if (!(host.uriTransformer instanceof TargetUriTransformer) || !host.uriTransformer.ownsConfigurationResource(uri, parent)) {
    throw new Error('配置文件不属于发起 Node 宿主的明确 profile 或工作区视图：' + uri.toString());
  }
}
