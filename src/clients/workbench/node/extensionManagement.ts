import { createRuntimeLogger } from '../../../shared/serverRuntimeLogging.mjs';
import { ExtensionDownloadRequestService } from './extensionDownload.js';
import { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';
import { DisposableStore } from '../upstream/src/vs/base/common/lifecycle.js';
import { Schemas } from '../upstream/src/vs/base/common/network.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { basename, dirname, joinPath } from '../upstream/src/vs/base/common/resources.js';
import { ConfigurationService } from '../upstream/src/vs/platform/configuration/common/configurationService.js';
import { IConfigurationService } from '../upstream/src/vs/platform/configuration/common/configuration.js';
import { DownloadService } from '../upstream/src/vs/platform/download/common/downloadService.js';
import { IDownloadService } from '../upstream/src/vs/platform/download/common/download.js';
import { ExtensionManagementError, ExtensionManagementErrorCode, IAllowedExtensionsService, IExtensionGalleryService, type InstallOptions, type Metadata } from '../upstream/src/vs/platform/extensionManagement/common/extensionManagement.js';
import { areSameExtensions, ExtensionKey } from '../upstream/src/vs/platform/extensionManagement/common/extensionManagementUtil.js';
import { AllowedExtensionsService } from '../upstream/src/vs/platform/extensionManagement/common/allowedExtensionsService.js';
import { ExtensionGalleryServiceWithNoStorageService } from '../upstream/src/vs/platform/extensionManagement/common/extensionGalleryService.js';
import { IExtensionGalleryManifestService } from '../upstream/src/vs/platform/extensionManagement/common/extensionGalleryManifest.js';
import { ExtensionGalleryManifestService } from '../upstream/src/vs/platform/extensionManagement/common/extensionGalleryManifestService.js';
import { IExtensionsScannerService, toExtensionDescription, type IScannedExtension } from '../upstream/src/vs/platform/extensionManagement/common/extensionsScannerService.js';
import { IExtensionsProfileScannerService } from '../upstream/src/vs/platform/extensionManagement/common/extensionsProfileScannerService.js';
import { ExtensionType } from '../upstream/src/vs/platform/extensions/common/extensions.js';
import { ExtensionManagementService, INativeServerExtensionManagementService, NATIVE_EXTENSION_REUSED_CONTEXT } from '../upstream/src/vs/platform/extensionManagement/node/extensionManagementService.js';
import { ExtensionsProfileScannerService } from '../upstream/src/vs/platform/extensionManagement/node/extensionsProfileScannerService.js';
import { ExtensionSignatureVerificationService, IExtensionSignatureVerificationService } from '../upstream/src/vs/platform/extensionManagement/node/extensionSignatureVerificationService.js';
import { FileService } from '../upstream/src/vs/platform/files/common/fileService.js';
import { IFileService } from '../upstream/src/vs/platform/files/common/files.js';
import { DiskFileSystemProvider } from '../upstream/src/vs/platform/files/node/diskFileSystemProvider.js';
import { SyncDescriptor } from '../upstream/src/vs/platform/instantiation/common/descriptors.js';
import { InstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiationService.js';
import { ServiceCollection } from '../upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { AdapterLogger, ILogService, LogLevel } from '../upstream/src/vs/platform/log/common/log.js';
import { LogService } from '../upstream/src/vs/platform/log/common/logService.js';
import { NullPolicyService } from '../upstream/src/vs/platform/policy/common/policy.js';
import product from '../upstream/src/vs/platform/product/common/product.js';
import { IProductService } from '../upstream/src/vs/platform/product/common/productService.js';
import { IRequestService } from '../upstream/src/vs/platform/request/common/request.js';
import { ITelemetryService } from '../upstream/src/vs/platform/telemetry/common/telemetry.js';
import { NullTelemetryService } from '../upstream/src/vs/platform/telemetry/common/telemetryUtils.js';
import { IUriIdentityService } from '../upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import { UriIdentityService } from '../upstream/src/vs/platform/uriIdentity/common/uriIdentityService.js';
import { IUserDataProfilesService } from '../upstream/src/vs/platform/userDataProfile/common/userDataProfile.js';
import { ServerUserDataProfilesService } from '../upstream/src/vs/platform/userDataProfile/node/userDataProfile.js';
import { ExtensionsScannerService } from '../upstream/src/vs/server/node/extensionsScannerService.js';
import { IServerEnvironmentService, ServerEnvironmentService, type ServerParsedArgs } from '../upstream/src/vs/server/node/serverEnvironmentService.js';

/** 与官方 server 使用相同的安装／扫描 owner，只装配本组件需要的原生服务。 */
export async function createExtensionManagement(directory: string, builtinExtensionsPath: string) {
  const scope = new DisposableStore();
  const consumers = scope.add(new DisposableStore());
  try {
    const services = new ServiceCollection();
    const args: ServerParsedArgs = {
      _: [], 'user-data-dir': `${directory}/data`, 'extensions-dir': `${directory}/extensions`,
      'builtin-extensions-dir': builtinExtensionsPath, 'disable-telemetry': true,
      'accept-server-license-terms': true, help: false, version: false, workspace: '', folder: '', compatibility: '',
    };
    const productService: IProductService = { _serviceBrand: undefined, ...product };
    const environment = new ServerEnvironmentService(args, productService);
    services.set(IProductService, productService);
    services.set(IServerEnvironmentService, environment);
    const diagnostic = createRuntimeLogger('extension-management', 'extensions.native');
    const logger = scope.add(new AdapterLogger({ log: (level, args) => {
      const write = level < LogLevel.Info ? diagnostic.debug : level === LogLevel.Info ? diagnostic.info
        : level === LogLevel.Warning ? diagnostic.warning : diagnostic.error;
      write(...args);
    } }, LogLevel.Trace));
    const log = scope.add(new LogService(logger));
    services.set(ILogService, log);
    const files = scope.add(new FileService(log));
    const disk = scope.add(new DiskFileSystemProvider(log));
    scope.add(files.registerProvider(Schemas.file, disk));
    services.set(IFileService, files);
    if (!(await files.resolve(URI.file(builtinExtensionsPath))).isDirectory) throw new Error('目标扩展运行时的内置扩展目录无效');
    await files.createFolder(URI.file(environment.extensionsPath));
    const identity = scope.add(new UriIdentityService(files));
    services.set(IUriIdentityService, identity);
    const profiles = consumers.add(new ServerUserDataProfilesService(identity, environment, files, log));
    services.set(IUserDataProfilesService, profiles);
    const configuration = consumers.add(new ConfigurationService(profiles.defaultProfile.settingsResource, files, new NullPolicyService(), log));
    services.set(IConfigurationService, configuration);
    await Promise.all([profiles.init(), configuration.initialize()]);
    services.set(IRequestService, new SyncDescriptor(ExtensionDownloadRequestService));
    services.set(IDownloadService, new SyncDescriptor(DownloadService));
    services.set(ITelemetryService, NullTelemetryService);
    services.set(IExtensionGalleryManifestService, new SyncDescriptor(ExtensionGalleryManifestService));
    services.set(IExtensionGalleryService, new SyncDescriptor(ExtensionGalleryServiceWithNoStorageService));
    services.set(IExtensionSignatureVerificationService, new SyncDescriptor(ExtensionSignatureVerificationService));
    services.set(IAllowedExtensionsService, new SyncDescriptor(AllowedExtensionsService));
    services.set(IExtensionsProfileScannerService, new SyncDescriptor(ExtensionsProfileScannerService));
    services.set(IExtensionsScannerService, new SyncDescriptor(ExtensionsScannerService));
    services.set(INativeServerExtensionManagementService, new SyncDescriptor(ExtensionManagementService));
    const instantiation = consumers.add(new InstantiationService(services, true));
    return instantiation.invokeFunction(accessor => {
      const management = accessor.get(INativeServerExtensionManagementService);
      const gallery = accessor.get(IExtensionGalleryService);
      const scanner = accessor.get(IExtensionsScannerService);
      const profileScanner = accessor.get(IExtensionsProfileScannerService);
      const profile = profiles.defaultProfile;
      async function profileMember(profileId: string, extensionId: string, version: string) {
        if (profileId !== profile.id) throw new ExtensionManagementError('扩展操作 profile 与目标默认 profile 不匹配', ExtensionManagementErrorCode.Invalid);
        const members = (await profileScanner.scanProfileExtensions(profile.extensionsResource))
          .filter(member => areSameExtensions(member.identifier, { id: extensionId }));
        if (members.length !== 1 || members[0].version !== version) {
          throw new ExtensionManagementError(`目标 profile 不再包含唯一的 ${extensionId}@${version}，请重新读取库存`, ExtensionManagementErrorCode.InstalledExtensionNotFound);
        }
        const [member] = members;
        const installed = (await management.getInstalled(ExtensionType.User, profile.extensionsResource))
          .filter(extension => areSameExtensions(extension.identifier, member.identifier)
            && extension.manifest.version === version && identity.extUri.isEqual(extension.location, member.location));
        if (installed.length !== 1) throw new ExtensionManagementError(`目标 profile 不再包含唯一的 ${extensionId}@${version}，请重新读取库存`, ExtensionManagementErrorCode.InstalledExtensionNotFound);
        return installed[0];
      }
      async function galleryExtension(extensionId: string, version: string, preRelease?: boolean) {
        const candidates = await gallery.getExtensions([{ id: extensionId, version, preRelease }],
          { targetPlatform: await management.getTargetPlatform() }, CancellationToken.None);
        const matches = candidates.filter(extension => areSameExtensions(extension.identifier, { id: extensionId }) && extension.version === version);
        if (matches.length !== 1) throw new ExtensionManagementError(`目标扩展市场不包含唯一的 ${extensionId}@${version}`, ExtensionManagementErrorCode.InstalledExtensionNotFound);
        return matches[0];
      }
      async function scanProfile() {
        // 不调用 scanAllExtensions 的跨来源 dedup；此库存只承诺同一默认 profile。
        const extensions = await scanner.scanUserExtensions({ profileLocation: profile.extensionsResource, includeInvalid: true });
        const members = await profileScanner.scanProfileExtensions(profile.extensionsResource);
        const byId = new Map(members.map(extension => [extension.identifier.id.toLowerCase(), extension]));
        if (byId.size !== members.length) throw new Error('目标 profile 存在重复扩展 ID，不能静默选择版本');
        if (extensions.length !== members.length || extensions.some(extension => {
          const member = byId.get(extension.identifier.id.toLowerCase());
          return !member || member.version !== extension.manifest.version || !identity.extUri.isEqual(member.location, extension.location);
        })) throw new Error('目标 profile 包含无法解析的已安装扩展');
        return extensions;
      }
      async function describeInventory(extensions: readonly IScannedExtension[]) {
        const entries = await Promise.all(extensions.map(async extension => {
          // 沿原描述转换识别真实 README/CHANGELOG，复用本次扫描对象，不另扫库存。
          const local = await management.describeNativeExtension(extension);
          const assetPath = (resource: URI | undefined) => {
            if (!resource) return null;
            const relative = identity.extUri.relativePath(extension.location, resource);
            if (!relative) throw new Error('扩展描述资源不属于原包根');
            return relative;
          };
          return { source: 'profile' as const, description: toExtensionDescription(extension, false),
            extension_key: ExtensionKey.create(extension).toString(), metadata: extension.metadata ?? {},
            readme_path: assetPath(local.readmeUrl), changelog_path: assetPath(local.changelogUrl),
            is_valid: extension.isValid, validations: extension.validations.map(([severity, message]) => ({ severity, message })),
          };
        }));
        return { profile_id: profile.id, target_platform: await management.getTargetPlatform(), extensions: entries };
      }
      function describePublication(extension: IScannedExtension) {
        const key = ExtensionKey.create(extension).toString(), root = scanner.userExtensionsLocation;
        const location = joinPath(root, key);
        if (!extension.isValid || !identity.extUri.isEqual(dirname(location), root) || basename(location) !== key
          || !identity.extUri.isEqual(extension.location, location)) {
          throw new ExtensionManagementError(`目标 profile 不包含有效的精确扩展包 ${key}`, ExtensionManagementErrorCode.InstalledExtensionNotFound);
        }
        // 安装身份与 profile 资格沿原 scanner；包内路径及字节读取交目标 Python。
        return { extension_id: extension.identifier.id, extension_key: key,
          native_uri: extension.location.toString(), extensions_root_uri: root.toString() };
      }
      async function publishedExtension(profileId: string, extensionId: string, extensionKeyText: string,
        currentProfileMember: boolean) {
        if (profileId !== profile.id) throw new ExtensionManagementError('扩展文件 profile 与目标默认 profile 不匹配', ExtensionManagementErrorCode.Invalid);
        if (!extensionKeyText || /[\\/\x00-\x1f\x7f]/.test(extensionKeyText)) {
          throw new ExtensionManagementError('扩展文件身份不能定位到用户扩展目录的直接子目录', ExtensionManagementErrorCode.Invalid);
        }
        const root = scanner.userExtensionsLocation;
        const location = joinPath(root, extensionKeyText);
        if (!identity.extUri.isEqual(dirname(location), root) || basename(location) !== extensionKeyText) {
          throw new ExtensionManagementError('扩展包身份不能定位到用户扩展目录的直接子目录', ExtensionManagementErrorCode.Invalid);
        }
        const scanned = await scanner.scanExistingExtension(location, ExtensionType.User, { includeInvalid: true });
        // scanner 的空结果可能是缺失目录，也可能是无效 manifest；保留原安装错误归属。
        if (!scanned) await files.stat(location);
        if (!scanned || !scanned.isValid || ExtensionKey.create(scanned).toString() !== extensionKeyText
          || !identity.extUri.isEqual(scanned.location, location)) {
          throw new ExtensionManagementError(`目标 profile 不包含有效的精确扩展包 ${extensionKeyText}`, ExtensionManagementErrorCode.InstalledExtensionNotFound);
        }
        if (!extensionId || !areSameExtensions(scanned.identifier, { id: extensionId })) {
          throw new ExtensionManagementError(`扩展包 ${extensionKeyText} 与请求的逻辑扩展 ID 不匹配`, ExtensionManagementErrorCode.InstalledExtensionNotFound);
        }
        if (currentProfileMember) {
          const members = await profileScanner.scanProfileExtensions(profile.extensionsResource);
          if (!members.some(member => areSameExtensions(member.identifier, scanned.identifier)
            && member.version === scanned.manifest.version && identity.extUri.isEqual(member.location, scanned.location))) {
            throw new ExtensionManagementError(`扩展包 ${extensionKeyText} 不再是默认 profile 当前成员`, ExtensionManagementErrorCode.InstalledExtensionNotFound);
          }
        }
        return describePublication(scanned);
      }
      return {
        ready: management.whenNativeManagementReady,
        async dispose() {
          const failures: unknown[] = [];
          // 先排空写入及消费者，再沿原 watcher 停止 API 关句柄，最后释放 provider。
          for (const close of [() => management.drainNativeManagement(), () => consumers.dispose(),
            () => disk.stopNativeManagementWatchers(), () => scope.dispose()]) {
            try { await close(); } catch (error) { failures.push(error); }
          }
          if (failures.length === 1) throw failures[0];
          if (failures.length) throw new AggregateError(failures, '原生扩展管理释放失败');
        },
        async environment() {
          // bundle 的实际根就是发行组件目录；存储沿原环境/profile，不按客户端或进程代际重建。
          return { app_root_uri: URI.file(import.meta.dirname).toString(),
            global_storage_home_uri: profile.globalStorageHome.toString(),
            workspace_storage_home_uri: environment.workspaceStorageHome.toString(),
            untitled_workspaces_home_uri: environment.untitledWorkspacesHome.toString(),
            profile_id: profile.id, settings_native_uri: profile.settingsResource.toString() };
        },
        async inventory() {
          return describeInventory(await scanProfile());
        },
        async publication(profileId: string, extensionId: string, extensionKey: string, currentProfileMember: boolean) {
          return publishedExtension(profileId, extensionId, extensionKey, currentProfileMember);
        },
        async webPublication(profileId: string, extensionKeys: readonly string[]) {
          if (profile.id !== profileId || !extensionKeys.length || new Set(extensionKeys).size !== extensionKeys.length) {
            throw new ExtensionManagementError('Web 宿主请求不属于目标 profile 当前有效的精确安装版本', ExtensionManagementErrorCode.InstalledExtensionNotFound);
          }
          // 原 profile 扫描已读取并校验每份 manifest；同次准入不再逐包重读 manifest 和整个 profile。
          const members = new Map((await scanProfile()).map(extension => [ExtensionKey.create(extension).toString(), extension]));
          const extensions = extensionKeys.map(key => {
            const extension = members.get(key);
            if (!extension?.isValid) throw new ExtensionManagementError('Web 宿主请求不属于目标 profile 当前有效的精确安装版本', ExtensionManagementErrorCode.InstalledExtensionNotFound);
            return extension;
          });
          const publications = extensions.map(describePublication);
          // TODO(4.4)：Node 真实运行集合与最终引用回收尚未闭合，不能启用全局物理 cleanUp。
          return { inventory: await describeInventory(extensions), publications, environment: await this.environment() };
        },
        async install(vsixPath: string) {
          // 同版本复用、不同版本并存沿原安装器处理；安装不覆盖在用目录或切换运行实例。
          // TODO(4.4): 宿主引用闭包接通后，沿原卸载服务回收不再使用的版本。
          const context = { [NATIVE_EXTENSION_REUSED_CONTEXT]: false };
          const extension = await management.install(URI.file(vsixPath), { profileLocation: profile.extensionsResource, keepExisting: true, context });
          // 在同一串行控制请求内读取发布后的库存，避免并发安装把回执拼成另一版本。
          return { installed: { id: extension.identifier.id, version: extension.manifest.version, profile_id: profile.id,
            reused: context[NATIVE_EXTENSION_REUSED_CONTEXT] },
            inventory: await this.inventory() };
        },
        async installGallery(extensionId: string, version: string, options: InstallOptions) {
          const candidate = await galleryExtension(extensionId, version, options.installPreReleaseVersion);
          const context = { [NATIVE_EXTENSION_REUSED_CONTEXT]: false };
          const extension = await management.installFromGallery(candidate, {
            ...options, profileLocation: profile.extensionsResource, keepExisting: true, context,
          });
          return { installed: { id: extension.identifier.id, version: extension.manifest.version,
            profile_id: profile.id, reused: context[NATIVE_EXTENSION_REUSED_CONTEXT] }, inventory: await this.inventory() };
        },
        async updateMetadata(profileId: string, extensionId: string, version: string, patch: Metadata,
          refreshFromGallery: boolean) {
          const extension = await profileMember(profileId, extensionId, version);
          const metadata: Metadata = {};
          if (refreshFromGallery) {
            const original = await galleryExtension(extensionId, version);
            Object.assign(metadata, { id: original.identifier.uuid, publisherId: original.publisherId,
              publisherDisplayName: original.publisherDisplayName, private: original.private,
              isPreReleaseVersion: original.properties.isPreReleaseVersion,
              hasPreReleaseVersion: original.hasPreReleaseVersion, targetPlatform: original.properties.targetPlatform });
          }
          await management.updateMetadata(extension, { ...metadata, ...patch }, profile.extensionsResource);
          return this.inventory();
        },
        async uninstall(profileId: string, extensionId: string, version: string) {
          const extension = await profileMember(profileId, extensionId, version);
          // 原卸载服务保留依赖／pack 规则；只移除 profile，不请求删除版本目录。
          await management.uninstall(extension, { profileLocation: profile.extensionsResource });
          return { uninstalled: { id: extension.identifier.id, version: extension.manifest.version, profile_id: profile.id },
            inventory: await this.inventory() };
        },
      };
    });
  } catch (error) {
    scope.dispose();
    throw error;
  }
}
