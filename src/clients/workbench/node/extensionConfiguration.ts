import { VSBuffer } from '../upstream/src/vs/base/common/buffer.js';
import { parse, ParseError } from '../upstream/src/vs/base/common/json.js';
import { dirname, joinPath } from '../upstream/src/vs/base/common/resources.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { IFileService, IFileWriteOptions, FileType } from '../upstream/src/vs/platform/files/common/files.js';
import { DiskFileSystemProvider } from '../upstream/src/vs/platform/files/node/diskFileSystemProvider.js';
import { IUriIdentityService } from '../upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import { IUserDataProfile } from '../upstream/src/vs/platform/userDataProfile/common/userDataProfile.js';
import { isStoredWorkspaceFolder, toWorkspaceFolders } from '../upstream/src/vs/platform/workspaces/common/workspaces.js';
import { IServerEnvironmentService } from '../upstream/src/vs/server/node/serverEnvironmentService.js';

export interface NativeViewFolder { native_uri: string; name: string }

/** 只登记原 profile settings 与明确多根 view；磁盘实现始终复用已安装的原 provider。 */
export class ExtensionConfiguration {
  private readonly views = new Map<string, { resource: URI; roots: readonly URI[] }>();
  constructor(private readonly files: IFileService, private readonly disk: DiskFileSystemProvider,
    private readonly identity: IUriIdentityService, private readonly environment: IServerEnvironmentService,
    private readonly profile: IUserDataProfile) { }

  async initialize(): Promise<void> {
    await this.files.createFolder(dirname(this.profile.settingsResource));
  }

  private native(value: string): URI {
    const resource = URI.parse(value);
    if (resource.scheme !== 'file' || !resource.path.startsWith('/') || resource.query || resource.fragment) {
      throw new Error('配置文件必须使用目标原生绝对 file URI');
    }
    return resource;
  }

  private viewResource(id: string): URI {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id)) {
      throw new Error('多根配置必须使用 owner 生成的稳定工作区视图身份');
    }
    return joinPath(this.environment.untitledWorkspacesHome, id, 'workspace.code-workspace');
  }

  private validateView(resource: URI, bytes: Uint8Array, roots: readonly URI[]): void {
    const errors: ParseError[] = [];
    const value: unknown = parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes), errors);
    if (errors.length || typeof value !== 'object' || value === null || !('folders' in value)
      || !Array.isArray(value.folders) || value.folders.length !== roots.length || !value.folders.every(isStoredWorkspaceFolder)) {
      throw new Error('稳定工作区视图配置损坏，已保留原文件');
    }
    const folders = toWorkspaceFolders(value.folders, resource, this.identity.extUri);
    if (folders.length !== roots.length || folders.some((folder, index) => !this.identity.extUri.isEqual(folder.uri, roots[index]))) {
      throw new Error('配置中的有序工作区根与已准入视图不符，必须重新打开明确视图');
    }
  }

  async prepareView(id: string, folders: readonly NativeViewFolder[]): Promise<{ native_uri: string }> {
    if (folders.length < 2) throw new Error('只有多根视图需要原生 workspace 配置文件');
    const resource = this.viewResource(id), roots = folders.map(folder => this.native(folder.native_uri));
    await this.files.createFolder(dirname(resource));
    if (await this.files.exists(resource)) {
      this.validateView(resource, (await this.files.readFile(resource, { atomic: true })).value.buffer, roots);
    } else {
      await this.files.createFile(resource, VSBuffer.fromString(JSON.stringify({ folders: folders.map((folder, index) => ({
        uri: roots[index].toString(), name: folder.name,
      })) }, null, '\t') + '\n'), { overwrite: false });
    }
    this.views.set(this.identity.extUri.getComparisonKey(resource), { resource, roots });
    return { native_uri: resource.toString() };
  }

  private resource(value: string, parent = false): URI {
    const resource = this.native(value);
    const allowed = [this.profile.settingsResource, ...[...this.views.values()].map(view => view.resource)];
    if (!allowed.some(file => this.identity.extUri.isEqual(resource, file)
      || parent && this.identity.extUri.isEqual(resource, dirname(file)))) {
      throw new Error('配置文件不属于已登记 profile 或工作区视图');
    }
    return resource;
  }

  async stat(value: string) {
    const stat = await this.disk.stat(this.resource(value, true));
    return { type: stat.type, ctime: stat.ctime, mtime: stat.mtime, size: stat.size, permissions: stat.permissions ?? null };
  }

  async read(value: string): Promise<{ content_base64: string }> {
    const content = await this.files.readFile(this.resource(value), { atomic: true });
    return { content_base64: Buffer.from(content.value.buffer).toString('base64') };
  }

  async write(value: string, content: string, options: IFileWriteOptions) {
    const resource = this.resource(value), bytes = Buffer.from(content, 'base64');
    const view = this.views.get(this.identity.extUri.getComparisonKey(resource));
    if (view) {
      if (options.append) throw new Error('工作区视图配置不支持追加写入');
      this.validateView(resource, bytes, view.roots);
    }
    if (!((await this.disk.stat(dirname(resource))).type & FileType.Directory)) throw new Error('配置文件父级不是原生目录');
    // 原 FileService/TextFileService 已执行 modified-since 与 dirty 检查；不另建 hash 或保存基线。
    await this.disk.writeFile(resource, bytes, options);
    return this.stat(value);
  }
}
