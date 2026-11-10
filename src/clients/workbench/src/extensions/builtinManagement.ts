import { Event } from '../../upstream/src/vs/base/common/event.js';
import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ExtensionType, TargetPlatform, type IExtension } from '../../upstream/src/vs/platform/extensions/common/extensions.js';
import type { ILocalExtension, IExtensionsControlManifest } from '../../upstream/src/vs/platform/extensionManagement/common/extensionManagement.js';
import { ExtensionInstallLocation, IWebExtensionsScannerService, type IExtensionManagementServer, type IExtensionManagementServerService, type IWorkbenchExtensionManagementService, type IProfileAwareExtensionManagementService } from '../../upstream/src/vs/workbench/services/extensionManagement/common/extensionManagement.js';

/** 原 Workbench 只读枚举产品内置贡献；用户插件由自有包管理 owner 处理。 */
export class WebExtensionManagementService extends Disposable implements IProfileAwareExtensionManagementService {
  declare readonly _serviceBrand: undefined;
  readonly preferPreReleases = false;
  readonly onInstallExtension = Event.None;
  readonly onDidInstallExtensions = Event.None;
  readonly onUninstallExtension = Event.None;
  readonly onDidUninstallExtension = Event.None;
  readonly onDidUpdateExtensionMetadata = Event.None;
  readonly onProfileAwareDidInstallExtensions = Event.None;
  readonly onProfileAwareDidUninstallExtension = Event.None;
  readonly onProfileAwareDidUpdateExtensionMetadata = Event.None;
  readonly onDidChangeProfile = Event.None;

  constructor(@IWebExtensionsScannerService private readonly scanner: IWebExtensionsScannerService) { super(); }

  async getInstalled(type?: ExtensionType): Promise<ILocalExtension[]> {
    const [system, custom] = await Promise.all([
      type === ExtensionType.User ? [] : this.scanner.scanSystemExtensions(),
      type === ExtensionType.System ? [] : this.scanner.scanCustomBuiltinExtensions({ skipInvalidExtensions: true }),
    ]);
    return [...system, ...custom].map(extension => ({ ...extension,
      isWorkspaceScoped: false, isMachineScoped: false, isApplicationScoped: true,
      publisherId: null, isPreReleaseVersion: extension.preRelease, hasPreReleaseVersion: extension.preRelease,
      private: false, updated: false, pinned: false, forceAutoUpdate: false, source: 'resource', size: 0,
    }));
  }

  async getTargetPlatform(): Promise<TargetPlatform> { return TargetPlatform.WEB; }
  async getExtensionsControlManifest(): Promise<IExtensionsControlManifest> { return { malicious: [], deprecated: {}, search: [] }; }
  // 内置内容随产品交付，无用户安装目录需要清理。
  async cleanUp(): Promise<void> { }

  readonly canInstall = unsupported;
  readonly zip = unsupported;
  readonly getManifest = unsupported;
  readonly install = unsupported;
  readonly installFromGallery = unsupported;
  readonly installGalleryExtensions = unsupported;
  readonly installFromLocation = unsupported;
  readonly installExtensionsFromProfile = unsupported;
  readonly uninstall = unsupported;
  readonly uninstallExtensions = unsupported;
  readonly toggleApplicationScope = unsupported;
  readonly copyExtensions = unsupported;
  readonly updateMetadata = unsupported;
  readonly resetPinnedStateForAllUserExtensions = unsupported;
  readonly download = unsupported;
  readonly registerParticipant = unsupported;
}

function unsupported(): never {
  throw new Error('用户 VSIX／Gallery 安装已退役，请使用插件包管理入口');
}

/** 原工作台消费者仍读取内置清单，不再装配工作区安装库存或用户安装任务。 */
export class ExtensionManagementService extends WebExtensionManagementService implements IWorkbenchExtensionManagementService {
  readonly onDidEnableExtensions = Event.None;
  readonly getExtensions = unsupported;
  getInstalledWorkspaceExtensionLocations() { return []; }
  async getInstalledWorkspaceExtensions(): Promise<ILocalExtension[]> { return []; }
  readonly getInstallableServers = unsupported;
  readonly installVSIX = unsupported;
  readonly installResourceExtension = unsupported;
  readonly updateFromGallery = unsupported;
  readonly requestPublisherTrust = unsupported;
  readonly isPublisherTrusted = unsupported;
  readonly getTrustedPublishers = unsupported;
  readonly trustPublishers = unsupported;
  readonly untrustPublishers = unsupported;
}

/** 原 enablement／设置消费者的只读接缝，不创建 Remote Agent 安装通道。 */
export class ExtensionManagementServerService extends Disposable implements IExtensionManagementServerService {
  declare readonly _serviceBrand: undefined;
  readonly localExtensionManagementServer = null;
  readonly remoteExtensionManagementServer = null;
  readonly webExtensionManagementServer: IExtensionManagementServer;
  constructor(@IWebExtensionsScannerService scanner: IWebExtensionsScannerService) {
    super();
    this.webExtensionManagementServer = { id: 'web', label: '产品内置扩展',
      extensionManagementService: this._register(new WebExtensionManagementService(scanner)) };
  }
  getExtensionManagementServer(extension: IExtension): IExtensionManagementServer {
    if (!extension.isBuiltin) throw new Error('用户 Extension Host 已退役，不能取得用户扩展安装目标');
    return this.webExtensionManagementServer;
  }
  getExtensionInstallLocation(extension: IExtension): ExtensionInstallLocation {
    this.getExtensionManagementServer(extension);
    return ExtensionInstallLocation.Web;
  }
}
