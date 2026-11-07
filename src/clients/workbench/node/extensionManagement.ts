import { format } from 'node:util';
import { DisposableStore } from '../upstream/src/vs/base/common/lifecycle.js';
import { Schemas } from '../upstream/src/vs/base/common/network.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { basename, dirname, joinPath } from '../upstream/src/vs/base/common/resources.js';
import { ConfigurationService } from '../upstream/src/vs/platform/configuration/common/configurationService.js';
import { IConfigurationService } from '../upstream/src/vs/platform/configuration/common/configuration.js';
import { DownloadService } from '../upstream/src/vs/platform/download/common/downloadService.js';
import { IDownloadService } from '../upstream/src/vs/platform/download/common/download.js';
import { ExtensionManagementError, ExtensionManagementErrorCode, IAllowedExtensionsService, IExtensionGalleryService } from '../upstream/src/vs/platform/extensionManagement/common/extensionManagement.js';
import { areSameExtensions, ExtensionKey } from '../upstream/src/vs/platform/extensionManagement/common/extensionManagementUtil.js';
import { AllowedExtensionsService } from '../upstream/src/vs/platform/extensionManagement/common/allowedExtensionsService.js';
import { ExtensionGalleryServiceWithNoStorageService } from '../upstream/src/vs/platform/extensionManagement/common/extensionGalleryService.js';
import { IExtensionGalleryManifestService } from '../upstream/src/vs/platform/extensionManagement/common/extensionGalleryManifest.js';
import { ExtensionGalleryManifestService } from '../upstream/src/vs/platform/extensionManagement/common/extensionGalleryManifestService.js';
import { IExtensionsScannerService, toExtensionDescription } from '../upstream/src/vs/platform/extensionManagement/common/extensionsScannerService.js';
import { IExtensionsProfileScannerService } from '../upstream/src/vs/platform/extensionManagement/common/extensionsProfileScannerService.js';
import { ExtensionType } from '../upstream/src/vs/platform/extensions/common/extensions.js';
import { createFileSystemProviderError, FileOperationResult, FileSystemProviderErrorCode, FileType, TooLargeFileOperationError } from '../upstream/src/vs/platform/files/common/files.js';
import { ExtensionManagementService, INativeServerExtensionManagementService, NATIVE_VSIX_REUSED_CONTEXT } from '../upstream/src/vs/platform/extensionManagement/node/extensionManagementService.js';
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
import { RequestService } from '../upstream/src/vs/platform/request/node/requestService.js';
import { ITelemetryService } from '../upstream/src/vs/platform/telemetry/common/telemetry.js';
import { NullTelemetryService } from '../upstream/src/vs/platform/telemetry/common/telemetryUtils.js';
import { IUriIdentityService } from '../upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import { UriIdentityService } from '../upstream/src/vs/platform/uriIdentity/common/uriIdentityService.js';
import { IUserDataProfilesService } from '../upstream/src/vs/platform/userDataProfile/common/userDataProfile.js';
import { ServerUserDataProfilesService } from '../upstream/src/vs/platform/userDataProfile/node/userDataProfile.js';
import { ExtensionsScannerService } from '../upstream/src/vs/server/node/extensionsScannerService.js';
import { IServerEnvironmentService, ServerEnvironmentService, type ServerParsedArgs } from '../upstream/src/vs/server/node/serverEnvironmentService.js';
import { ExtensionConfiguration } from './extensionConfiguration.js';

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
    const logger = scope.add(new AdapterLogger({ log: (level, args) => process.stderr.write(`[${level}] ${format(...args)}\n`) }, LogLevel.Info));
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
    services.set(IRequestService, new SyncDescriptor(RequestService, ['remote']));
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
      const scanner = accessor.get(IExtensionsScannerService);
      const profileScanner = accessor.get(IExtensionsProfileScannerService);
      const profile = profiles.defaultProfile;
      const maximumPublishedFileSize = 8 * 1024 * 1024;
      async function publishedExtensionFile(profileId: string, extensionKeyText: string, relativeSegments: string[]) {
        if (profileId !== profile.id) throw new ExtensionManagementError('扩展文件 profile 与目标默认 profile 不匹配', ExtensionManagementErrorCode.Invalid);
        if (!extensionKeyText || /[\\/\x00-\x1f\x7f]/.test(extensionKeyText)) {
          throw new ExtensionManagementError('扩展文件身份不能定位到用户扩展目录的直接子目录', ExtensionManagementErrorCode.Invalid);
        }
        if (!Array.isArray(relativeSegments) || relativeSegments.some(segment =>
          typeof segment !== 'string' || !segment || segment === '.' || segment === '..' || /[\\/\0]/.test(segment))) {
          throw createFileSystemProviderError('扩展文件必须使用有效的包内相对路径段', FileSystemProviderErrorCode.NoPermissions);
        }
        const root = scanner.userExtensionsLocation;
        const location = joinPath(root, extensionKeyText);
        if (!identity.extUri.isEqual(dirname(location), root) || basename(location) !== extensionKeyText) {
          throw new ExtensionManagementError('扩展包身份不能定位到用户扩展目录的直接子目录', ExtensionManagementErrorCode.Invalid);
        }
        const [realExtensionsRoot, realRoot] = await Promise.all([files.realpath(root), files.realpath(location)]);
        if (!realExtensionsRoot || !realRoot || !identity.extUri.isEqual(dirname(realRoot), realExtensionsRoot)
          || basename(realRoot) !== extensionKeyText) {
          throw createFileSystemProviderError('扩展包目录符号链接越出用户扩展目录', FileSystemProviderErrorCode.NoPermissions);
        }
        const scanned = await scanner.scanExistingExtension(location, ExtensionType.User, { includeInvalid: true });
        if (!scanned || !scanned.isValid || ExtensionKey.create(scanned).toString() !== extensionKeyText
          || !identity.extUri.isEqual(scanned.location, location)) {
          throw new ExtensionManagementError(`目标 profile 不包含有效的精确扩展包 ${extensionKeyText}`, ExtensionManagementErrorCode.InstalledExtensionNotFound);
        }
        const file = joinPath(location, ...relativeSegments);
        const realFile = await files.realpath(file);
        if (!realFile || !identity.extUri.isEqualOrParent(realFile, realRoot)) {
          throw createFileSystemProviderError('扩展文件符号链接越出已发布包', FileSystemProviderErrorCode.NoPermissions);
        }
        return file;
      }
      const configurationFiles = new ExtensionConfiguration(files, disk, identity, environment, profile);
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
          await configurationFiles.initialize();
          // bundle 的实际根就是发行组件目录；存储沿原环境/profile，不按客户端或进程代际重建。
          return { app_root_uri: URI.file(import.meta.dirname).toString(),
            global_storage_home_uri: profile.globalStorageHome.toString(),
            workspace_storage_home_uri: environment.workspaceStorageHome.toString(),
            profile_id: profile.id, settings_native_uri: profile.settingsResource.toString() };
        },
        configurationFiles,
        async inventory() {
          // 不调用 scanAllExtensions 的跨来源 dedup；此库存只承诺同一默认 profile。
          const extensions = await scanner.scanUserExtensions({ profileLocation: profile.extensionsResource, includeInvalid: true });
          const members = await profileScanner.scanProfileExtensions(profile.extensionsResource);
          const byId = new Map(members.map(extension => [extension.identifier.id.toLowerCase(), extension]));
          if (byId.size !== members.length) throw new Error('目标 profile 存在重复扩展 ID，不能静默选择版本');
          if (extensions.length !== members.length || extensions.some(extension => {
            const member = byId.get(extension.identifier.id.toLowerCase());
            return !member || member.version !== extension.manifest.version || !identity.extUri.isEqual(member.location, extension.location);
          })) throw new Error('目标 profile 包含无法解析的已安装扩展');
          return { profile_id: profile.id, extensions: extensions.map(extension => ({
            source: 'profile' as const, description: toExtensionDescription(extension, false),
            is_valid: extension.isValid, validations: extension.validations.map(([severity, message]) => ({ severity, message })),
          })) };
        },
        async extensionFileStat(profileId: string, extensionKey: string, relativeSegments: string[]) {
          const file = await publishedExtensionFile(profileId, extensionKey, relativeSegments);
          const stat = await disk.stat(file);
          return { type: stat.type, ctime: stat.ctime, mtime: stat.mtime, size: stat.size, permissions: stat.permissions ?? null };
        },
        async extensionFileRead(profileId: string, extensionKey: string, relativeSegments: string[]) {
          const file = await publishedExtensionFile(profileId, extensionKey, relativeSegments);
          const stat = await disk.stat(file);
          if ((stat.type & FileType.File) === 0) throw createFileSystemProviderError('扩展包读取目标不是普通文件', FileSystemProviderErrorCode.FileIsADirectory);
          if (stat.size > maximumPublishedFileSize) {
            throw new TooLargeFileOperationError(`扩展包文件超过私有读取上限 ${maximumPublishedFileSize} 字节`, FileOperationResult.FILE_TOO_LARGE, stat.size);
          }
          // TODO(扩展资源流式读取接入后)：移除私有控制帧的 8 MiB 限制并复用原 FileService 流。
          const content = await files.readFile(file, { limits: { size: maximumPublishedFileSize } });
          return { content_base64: Buffer.from(content.value.buffer).toString('base64') };
        },
        async install(vsixPath: string) {
          // 同版本复用、不同版本并存沿原安装器处理；安装不覆盖在用目录或切换运行实例。
          // TODO(4.4): 宿主引用闭包接通后，沿原卸载服务回收不再使用的版本。
          const context = { [NATIVE_VSIX_REUSED_CONTEXT]: false };
          const extension = await management.install(URI.file(vsixPath), { profileLocation: profile.extensionsResource, keepExisting: true, context });
          // 在同一串行控制请求内读取发布后的库存，避免并发安装把回执拼成另一版本。
          return { installed: { id: extension.identifier.id, version: extension.manifest.version, profile_id: profile.id,
            reused: context[NATIVE_VSIX_REUSED_CONTEXT] },
            inventory: await this.inventory() };
        },
        async uninstall(profileId: string, extensionId: string, version: string) {
          if (profileId !== profile.id) throw new ExtensionManagementError('扩展卸载 profile 与目标默认 profile 不匹配', ExtensionManagementErrorCode.Invalid);
          const members = (await profileScanner.scanProfileExtensions(profile.extensionsResource))
            .filter(member => areSameExtensions(member.identifier, { id: extensionId }));
          if (members.length !== 1 || members[0].version !== version) {
            throw new ExtensionManagementError(`目标 profile 不再包含唯一的 ${extensionId}@${version}，请重新读取库存`, ExtensionManagementErrorCode.InstalledExtensionNotFound);
          }
          const [member] = members;
          const installed = (await management.getInstalled(ExtensionType.User, profile.extensionsResource))
            .filter(extension => areSameExtensions(extension.identifier, member.identifier)
              && extension.manifest.version === version && identity.extUri.isEqual(extension.location, member.location));
          if (installed.length !== 1) throw new ExtensionManagementError(`目标 profile 的 ${extensionId}@${version} 无法解析为原安装成员`, ExtensionManagementErrorCode.InstalledExtensionNotFound);
          const [extension] = installed;
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
