import { isEqualOrParent as isPathEqualOrParent, toSlashes } from '../../upstream/src/vs/base/common/extpath.js';
import { posix, win32 } from '../../upstream/src/vs/base/common/path.js';
import { extUri, joinPath, originalFSPath, relativePath } from '../../upstream/src/vs/base/common/resources.js';
import { equalsIgnoreCase } from '../../upstream/src/vs/base/common/strings.js';
import { URI, UriComponents } from '../../upstream/src/vs/base/common/uri.js';
import type { IURITransformer } from '../../upstream/src/vs/base/common/uriIpc.js';
import type { IExtensionDescription } from '../../upstream/src/vs/platform/extensions/common/extensions.js';
import type { ExtensionHostConnectionDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { workspaceFileUri } from '../workspaceFiles.js';
import type { IExtensionHost } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';

/** 只反转已认证 mount 根的 typed URI；相同原生路径对应多个身份时不任选一个。 */
export class TargetUriTransformer implements IURITransformer {
  readonly folders: readonly { uri: URI; native: URI; name: string; index: number }[];
  private readonly nativeHomes: readonly URI[];
  private readonly windows: boolean;
  constructor(binding: ExtensionHostConnectionDTO, private readonly extensions: () => readonly IExtensionDescription[]) {
    const initialization = binding.initialization;
    if (!initialization?.workspace || !initialization.environment) throw new Error('Node 宿主缺少初始化目录与环境');
    this.windows = initialization.os === 'windows';
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
    return [...this.nativeHomes, ...this.extensions().map(extension => extension.extensionLocation)].some(base => this.inside(uri, base));
  }
  ownsWorkspaceResource(uri: URI): boolean { return this.folders.some(folder => this.inside(uri, folder.uri)); }
  allowsOutgoingResource(uri: URI): boolean {
    if (uri.scheme === 'boxteam-file') return this.ownsWorkspaceResource(uri);
    return uri.scheme !== 'file' || this.isHostResource(uri) || this.folders.some(folder => this.inside(uri, folder.native));
  }
  transformIncoming(value: UriComponents): UriComponents {
    const uri = URI.revive(value);
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

/** 诊断的文档链接可以指向该宿主扩展内的帮助文件；业务文档仍须属于 workspace view。 */
export function hostAllowsResourceLink(host: IExtensionHost, uri: URI): boolean {
  if (!host.binding) return true;
  if (!(host.uriTransformer instanceof TargetUriTransformer)) throw new Error('Node 宿主缺少已绑定 URI transformer');
  return host.uriTransformer.allowsOutgoingResource(uri);
}
