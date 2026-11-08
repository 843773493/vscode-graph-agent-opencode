import { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { raceCancellationError } from '../../upstream/src/vs/base/common/async.js';
import { create, toJson } from '@bufbuild/protobuf';
import { Emitter } from '../../upstream/src/vs/base/common/event.js';
import { DisposableStore, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { joinPath } from '../../upstream/src/vs/base/common/resources.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { AbstractExtensionManagementService, type InstallExtensionTaskOptions, type UninstallExtensionTaskOptions } from '../../upstream/src/vs/platform/extensionManagement/common/abstractExtensionManagementService.js';
import { IAllowedExtensionsService, IExtensionGalleryService, InstallOperation, type IGalleryExtension, type ILocalExtension, type InstallExtensionInfo, type InstallExtensionResult, type InstallOptions, type Metadata, type UninstallExtensionInfo } from '../../upstream/src/vs/platform/extensionManagement/common/extensionManagement.js';
import { ExtensionType, type IExtension, type IExtensionIdentifier, type IExtensionManifest, type TargetPlatform, installationTargetKey } from '../../upstream/src/vs/platform/extensions/common/extensions.js';
import { ILogService } from '../../upstream/src/vs/platform/log/common/log.js';
import { IProductService } from '../../upstream/src/vs/platform/product/common/productService.js';
import { ITelemetryService } from '../../upstream/src/vs/platform/telemetry/common/telemetry.js';
import { IUriIdentityService } from '../../upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import { IUserDataProfilesService } from '../../upstream/src/vs/platform/userDataProfile/common/userDataProfile.js';
import { IWebExtensionsScannerService, type IProfileAwareExtensionManagementService } from '../../upstream/src/vs/workbench/services/extensionManagement/common/extensionManagement.js';
import { toExtension } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';
import { IUserDataProfileService } from '../../upstream/src/vs/workbench/services/userDataProfile/common/userDataProfile.js';
import { onGatewayUserSessionInvalidated } from '../../../web/src/api/http.js';
import type { ExtensionInstallationGalleryDTO, ExtensionInstallationInventoryDTO, ExtensionInstallationUninstallDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { ExtensionGalleryInstallOptionsSchema, ExtensionMetadataPatchSchema } from '../../../../workspace-services/protocol/generated/boxteam/gateway/v2/public_pb.js';
import { gateway } from '../agentApi.js';
import { extensionManagementTarget, managementGateway, onDidChangeExtensionManagementTarget } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import { workspaceFileSource } from '../workspaceFiles.js';
import { currentExtensionPackageUri, nativeInventoryDescriptions, type NativeExtensionTarget } from './nativeInventory.js';
import { invalidateMcpProviderInstallations } from '../mcp/providerBridge.js';

type TargetOptions = InstallOptions & { readonly nativeTarget?: ReturnType<typeof managementGateway>; readonly nativeProfileId?: string };
const targetKey = (target: NativeExtensionTarget) => JSON.stringify([installationTargetKey(target), target.gatewayConnectionId]);

/** 原市场入口先冻结展示选择；原生更新沿已安装目标，窗口内置项不提供安装域。 */
export function captureTargetInstallOptions(options: InstallOptions = {}, local?: ILocalExtension,
  target?: ReturnType<typeof managementGateway>): TargetOptions {
  if (local?.installationTarget?.kind === 'backend') {
    if (!local.installationProfileId) throw new Error('原生扩展缺少目标默认 profile');
    return { ...options, nativeTarget: { gateway_id: local.installationTarget.gatewayId, backend_id: local.installationTarget.backendId,
      gateway_connection_id: local.gatewayConnectionId, name: local.installationTarget.gatewayId, connection_kind: local.gatewayConnectionId ? 'remote_gateway' : 'local' },
      nativeProfileId: local.installationProfileId };
  }
  const nativeTarget = target ?? (options as TargetOptions).nativeTarget;
  if (nativeTarget) return { ...options, nativeTarget: { ...nativeTarget } };
  return { ...options, nativeTarget: managementGateway('extensions') };
}

/** 原 Web management 服务仍持有事件与 UI；用户安装事实和任务只由目标原生服务持有。 */
export class WebExtensionManagementService extends AbstractExtensionManagementService implements IProfileAwareExtensionManagementService {
  private readonly profileChanged = this._register(new Emitter<{ readonly added: ILocalExtension[]; readonly removed: ILocalExtension[] }>());
  readonly onDidChangeProfile = this.profileChanged.event;
  get onProfileAwareInstallExtension() { return this.onInstallExtension; }
  get onProfileAwareDidInstallExtensions() { return this.onDidInstallExtensions; }
  get onProfileAwareUninstallExtension() { return this.onUninstallExtension; }
  get onProfileAwareDidUninstallExtension() { return this.onDidUninstallExtension; }
  get onProfileAwareDidUpdateExtensionMetadata() { return this.onDidUpdateExtensionMetadata; }
  private readonly operations = new Set<AbortController>();
  private readonly inventoryReads = new Map<string, { readonly controller: AbortController; readonly result: Promise<ExtensionInstallationInventoryDTO> }>();
  private lastInventory: { readonly target: NativeExtensionTarget; readonly value: ExtensionInstallationInventoryDTO } | undefined;

  constructor(@IExtensionGalleryService gallery: IExtensionGalleryService, @ITelemetryService telemetry: ITelemetryService,
    @ILogService logs: ILogService, @IWebExtensionsScannerService private readonly scanner: IWebExtensionsScannerService,
    @IUserDataProfileService private readonly profiles: IUserDataProfileService, @IProductService product: IProductService,
    @IAllowedExtensionsService allowed: IAllowedExtensionsService, @IUserDataProfilesService allProfiles: IUserDataProfilesService,
    @IUriIdentityService identity: IUriIdentityService) {
    super(gallery, telemetry, identity, logs, product, allowed, allProfiles);
    this._register(onDidChangeExtensionManagementTarget(() => { this.lastInventory = undefined; this.profileChanged.fire({ added: [], removed: [] }); }));
    this._register(profiles.onDidChangeCurrentProfile(e => e.join(Promise.resolve().then(() => {
      this.assertProfile(e.profile.extensionsResource); this.lastInventory = undefined; this.profileChanged.fire({ added: [], removed: [] });
    }))));
    this._register(toDisposable(() => {
      const error = new Error('扩展管理服务已关闭');
      this.invalidateInventory(error);
      for (const operation of this.operations) operation.abort(error);
    }));
  }

  private assertProfile(location = this.profiles.currentProfile.extensionsResource): void {
    if (!this.uriIdentityService.extUri.isEqual(location, this.userDataProfilesService.defaultProfile.extensionsResource)) {
      throw new Error('目标安装域当前只支持原生默认 profile，不能把浏览器的其它 profile 映射到默认 profile');
    }
  }

  private async readTargetOptions(options: InstallOptions = {}): Promise<TargetOptions> {
    const snapshot = workspaceContext.getSnapshot();
    if (!snapshot.gatewayId) {
      if (snapshot.loading) await workspaceContext.refresh();
      const error = workspaceContext.getSnapshot().error;
      if (error) throw new Error(error);
    }
    return captureTargetInstallOptions(options);
  }

  private async withTarget<T>(options: TargetOptions, operation: (target: NativeExtensionTarget, signal: AbortSignal) => Promise<T>): Promise<T> {
    if (this._store.isDisposed) throw new Error('扩展管理服务已关闭');
    this.assertProfile(options.profileLocation);
    const selected = options.nativeTarget ?? managementGateway('extensions'), entry = workspaceContext.getSnapshot().gatewayId;
    const controller = new AbortController(); this.operations.add(controller);
    const invalidate = () => {
      const error = new Error('页面 Gateway 或用户身份已改变，本次扩展管理操作已撤销');
      this.invalidateInventory(error); controller.abort(error);
    };
    const releaseUser = onGatewayUserSessionInvalidated(0, invalidate);
    const releaseContext = workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== entry) invalidate(); });
    try {
      const resolved = await extensionManagementTarget(selected, controller.signal); controller.signal.throwIfAborted();
      return await operation({ kind: 'backend', gatewayId: resolved.gateway_id, backendId: resolved.backend_id,
        gatewayConnectionId: resolved.gateway_connection_id }, controller.signal);
    } finally { releaseUser(); releaseContext(); this.operations.delete(controller); controller.abort(); }
  }

  private requestTarget(target: NativeExtensionTarget) {
    return { protocol_version: 1, gateway_id: target.gatewayId, backend_id: target.backendId, gateway_connection_id: target.gatewayConnectionId };
  }

  private isSelected(target: NativeExtensionTarget): boolean {
    if (!workspaceContext.getSnapshot().gatewayId) return false;
    const selected = managementGateway('extensions');
    return selected.gateway_id === target.gatewayId && selected.backend_id === target.backendId && selected.gateway_connection_id === target.gatewayConnectionId;
  }

  private acceptInventory(value: ExtensionInstallationInventoryDTO, target: NativeExtensionTarget): ILocalExtension[] {
    const descriptions = nativeInventoryDescriptions(value, target);
    invalidateMcpProviderInstallations(target.gatewayId, target.backendId, new Set(descriptions.map(item => item.extensionKey!)));
    const locals = descriptions.map((description, index) => {
      const entry = value.inventory!.extensions[index], metadata = entry.metadata as Metadata;
      const original = toExtension(description), location = currentExtensionPackageUri(target, description.installationProfileId!, description.extensionKey!, description.identifier.value);
      const resource = (path: string | undefined) => path ? joinPath(location, ...path.split('/')) : undefined;
      return { ...toLocalExtension(original, metadata), nativeLocation: original.location, location,
        readmeUrl: resource(entry.readme_path), changelogUrl: resource(entry.changelog_path) };
    });
    if (this.isSelected(target)) this.lastInventory = { target, value };
    return locals;
  }

  private async inventory(target: NativeExtensionTarget, signal: AbortSignal): Promise<ExtensionInstallationInventoryDTO> {
    const key = targetKey(target);
    let read = this.inventoryReads.get(key);
    if (!read) {
      const controller = new AbortController();
      read = { controller, result: gateway.request<ExtensionInstallationInventoryDTO>('/api/gateway/extension-installations/inventory', {
        method: 'POST', body: JSON.stringify(this.requestTarget(target)), signal: controller.signal,
      }) };
      this.inventoryReads.set(key, read);
      const release = () => { if (this.inventoryReads.get(key) === read) this.inventoryReads.delete(key); };
      void read.result.then(release, release);
    }
    const lifetime = new DisposableStore();
    try {
      const value = await raceCancellationError(read.result, CancellationToken.fromAbortSignal(signal, lifetime));
      signal.throwIfAborted(); this.acceptInventory(value, target); return value;
    } finally { lifetime.dispose(); }
  }

  private invalidateInventory(error: Error): void {
    const reads = [...this.inventoryReads.values()];
    this.inventoryReads.clear(); this.lastInventory = undefined;
    for (const read of reads) read.controller.abort(error);
  }

  async getTargetPlatform(options: InstallOptions = {}): Promise<TargetPlatform> {
    return this.withTarget(await this.readTargetOptions(options), async (target, signal) => {
      const value = this.lastInventory && targetKey(this.lastInventory.target) === targetKey(target) ? this.lastInventory.value : await this.inventory(target, signal);
      return value.inventory!.target_platform as TargetPlatform;
    });
  }

  async getInstalled(type?: ExtensionType, profileLocation?: URI): Promise<ILocalExtension[]> {
    this.assertProfile(profileLocation);
    const options = type === ExtensionType.System ? undefined : await this.readTargetOptions({ profileLocation });
    const system = type === ExtensionType.User ? [] : (await this.scanner.scanSystemExtensions()).map(extension => toLocalExtension({ ...extension,
      installationTarget: { kind: 'web', profileId: this.profiles.currentProfile.id } }));
    if (type === ExtensionType.System) return system;
    return this.withTarget(options!, async (target, signal) => {
      const [inventory, custom] = await Promise.all([this.inventory(target, signal), this.scanner.scanCustomBuiltinExtensions()]);
      signal.throwIfAborted();
      return [...system, ...custom.map(extension => toLocalExtension({ ...extension,
        installationTarget: { kind: 'web', profileId: this.profiles.currentProfile.id } })), ...this.acceptInventory(inventory, target)];
    });
  }

  override async installGalleryExtensions(extensions: InstallExtensionInfo[]): Promise<InstallExtensionResult[]> {
    const captured = extensions.map(({ extension, options }) => ({ extension, options: captureTargetInstallOptions(options) }));
    const results: InstallExtensionResult[] = [];
    for (const { extension, options } of captured) {
      const profileLocation = options.profileLocation ?? this.profiles.currentProfile.extensionsResource;
      let result: InstallExtensionResult;
      let installTarget: NativeExtensionTarget | undefined;
      try {
        const local = await this.withTarget(options, async (target, signal) => {
          installTarget = target;
          // TODO: 这些选项还没有目标原生契约，不能静默忽略后显示安装成功。
          if (options.isBuiltin || options.isWorkspaceScoped || options.donotVerifySignature || options.keepExisting || options.downloadExtensionsLocally) throw new Error('目标安装器尚未接入本次请求的非标准安装选项');
          if (this.isSelected(target)) this._onInstallExtension.fire({ identifier: extension.identifier, source: extension, profileLocation });
          const reply = await gateway.request<ExtensionInstallationGalleryDTO>('/api/gateway/extension-installations/install-gallery', {
            method: 'POST', signal, body: JSON.stringify({ ...this.requestTarget(target), extension_id: extension.identifier.id, version: extension.version,
              options: toJson(ExtensionGalleryInstallOptionsSchema, create(ExtensionGalleryInstallOptionsSchema, options), { useProtoFieldName: true }) }),
          });
          signal.throwIfAborted();
          const locals = this.acceptInventory(reply, target), installed = reply.installed;
          const local = installed && locals.find(local => local.identifier.id.toLowerCase() === installed.id.toLowerCase() && local.manifest.version === installed.version
            && local.installationProfileId === installed.profile_id);
          if (!local || !installed || typeof installed.reused !== 'boolean' || options.nativeProfileId && local.installationProfileId !== options.nativeProfileId) throw new Error('目标市场安装回执没有对应的完整原生库存成员');
          return local;
        });
        result = { identifier: local.identifier, local, source: extension, operation: options.operation ?? (local.updated ? InstallOperation.Update : InstallOperation.Install),
          profileLocation, context: options.context, applicationScoped: local.isApplicationScoped };
      } catch (error) {
        result = { identifier: extension.identifier, source: extension, operation: options.operation ?? InstallOperation.Install,
          profileLocation, context: options.context, error: error instanceof Error ? error : new Error(String(error)) };
      }
      results.push(result);
      if (installTarget && this.isSelected(installTarget)) {
        this._onDidInstallExtensions.fire([result]); this.profileChanged.fire({ added: [], removed: [] });
      }
    }
    return results;
  }

  override async uninstallExtensions(extensions: UninstallExtensionInfo[]): Promise<void> {
    const capturedExtensions = extensions.map(({ extension, options }) => ({ extension, options, captured: captureTargetInstallOptions(options, extension),
      profileLocation: options?.profileLocation ?? this.profiles.currentProfile.extensionsResource }));
    for (const { extension, options, captured, profileLocation } of capturedExtensions) {
      await this.withTarget(captured, async (target, signal) => {
        if (options?.remove || options?.versionOnly) throw new Error('目标卸载仅移除精确默认 profile 成员，不支持强制物理删除版本');
        const event = { identifier: extension.identifier, profileLocation, applicationScoped: extension.isApplicationScoped };
        if (this.isSelected(target)) this._onUninstallExtension.fire(event);
        try {
          const reply = await gateway.request<ExtensionInstallationUninstallDTO>('/api/gateway/extension-installations/uninstall', {
            method: 'POST', signal, body: JSON.stringify({ ...this.requestTarget(target), profile_id: extension.installationProfileId,
              extension_id: extension.identifier.id, version: extension.manifest.version }),
          });
          signal.throwIfAborted(); this.acceptInventory(reply, target);
          if (reply.uninstalled?.id.toLowerCase() !== extension.identifier.id.toLowerCase() || reply.uninstalled.version !== extension.manifest.version
            || reply.uninstalled.profile_id !== extension.installationProfileId) throw new Error('目标卸载回执不匹配本次冻结的 profile 成员');
          if (this.isSelected(target)) this._onDidUninstallExtension.fire(event);
        } catch (error) {
          if (this.isSelected(target)) { this._onDidUninstallExtension.fire({ ...event, error: error instanceof Error ? error.message : String(error) }); this.profileChanged.fire({ added: [], removed: [] }); }
          throw error;
        }
        if (this.isSelected(target)) this.profileChanged.fire({ added: [], removed: [] });
      });
    }
  }

  async updateMetadata(local: ILocalExtension, metadata: Partial<Metadata>, profileLocation: URI): Promise<ILocalExtension> {
    const captured = captureTargetInstallOptions({ profileLocation }, local);
    const galleryKeys = new Set(['id', 'publisherDisplayName', 'publisherId', 'isPreReleaseVersion']);
    for (const [key, value] of Object.entries(metadata)) {
      if (Object.hasOwn(ExtensionMetadataPatchSchema.field, key)) {
        if (typeof value !== 'boolean') throw new Error('目标 metadata 选项必须为明确的布尔值：' + key);
      } else if (!galleryKeys.has(key)) throw new Error('目标 metadata 尚未接入该原生字段：' + key);
    }
    return this.withTarget(captured, async (target, signal) => {
      try {
        const reply = await gateway.request<ExtensionInstallationInventoryDTO>('/api/gateway/extension-installations/metadata', {
          method: 'POST', signal, body: JSON.stringify({ ...this.requestTarget(target), profile_id: local.installationProfileId,
            extension_id: local.identifier.id, version: local.manifest.version,
            metadata: toJson(ExtensionMetadataPatchSchema, create(ExtensionMetadataPatchSchema, metadata), { useProtoFieldName: true }),
            refresh_from_gallery: Object.keys(metadata).some(key => galleryKeys.has(key)) }),
        });
        signal.throwIfAborted();
        const updated = this.acceptInventory(reply, target).find(extension => extension.identifier.id.toLowerCase() === local.identifier.id.toLowerCase()
          && extension.manifest.version === local.manifest.version && extension.installationProfileId === local.installationProfileId && extension.extensionKey === local.extensionKey);
        if (!updated) throw new Error('目标 metadata 回执缺少本次精确安装成员');
        if (this.isSelected(target)) this._onDidUpdateExtensionMetadata.fire({ local: updated, profileLocation });
        return updated;
      } catch (error) {
        if (this.isSelected(target)) this.profileChanged.fire({ added: [], removed: [] });
        throw error;
      }
    });
  }

  async install(location: URI, options: InstallOptions = {}): Promise<ILocalExtension> {
    if (location.scheme !== 'boxteam-file') throw new Error('原生 VSIX 安装需要目标工作区的明确文件资源，不能安装浏览器本机路径或 Web 包目录');
    // TODO: 原 VSIX 入口尚未提供安装选项；有意请求其它行为时必须先扩展同一原生契约。
    if (Object.entries(options).some(([key, value]) => !['profileLocation', 'nativeTarget', 'nativeProfileId', 'context', 'operation'].includes(key) && value !== undefined && value !== false)) {
      throw new Error('目标 VSIX 安装尚未接入本次明确请求的安装选项');
    }
    const captured = captureTargetInstallOptions(options), source = workspaceFileSource(location);
    return this.withTarget(captured, async (target, signal) => {
      if (source.gatewayId !== target.gatewayId || source.path === source.root) throw new Error('VSIX 来源不属于已选择的目标工作区文件');
      try {
        const reply = await gateway.request<ExtensionInstallationGalleryDTO>('/api/gateway/extension-installations/install', {
          method: 'POST', signal, body: JSON.stringify({ ...this.requestTarget(target), workspace_ids: [source.workspaceId], source_uri: source.path }),
        });
        signal.throwIfAborted();
        const installed = reply.installed, local = this.acceptInventory(reply, target).find(extension => extension.identifier.id.toLowerCase() === installed?.id.toLowerCase()
          && extension.manifest.version === installed.version && extension.installationProfileId === installed.profile_id);
        if (!local || typeof installed?.reused !== 'boolean' || captured.nativeProfileId && local.installationProfileId !== captured.nativeProfileId) throw new Error('目标 VSIX 安装回执缺少完整原生库存成员');
        return local;
      } finally {
        if (this.isSelected(target)) this.profileChanged.fire({ added: [], removed: [] });
      }
    });
  }

  installFromLocation(location: URI, profileLocation: URI) { return this.install(location, { profileLocation }); }
  async installExtensionsFromProfile(extensions: IExtensionIdentifier[], from: URI, to: URI): Promise<ILocalExtension[]> {
    this.assertProfile(from); this.assertProfile(to);
    return (await this.getInstalled(ExtensionType.User, to)).filter(extension => extensions.some(id => id.id.toLowerCase() === extension.identifier.id.toLowerCase()));
  }
  override async copyExtensions(from: URI, to: URI): Promise<void> { this.assertProfile(from); this.assertProfile(to); }
  protected getCurrentExtensionsManifestLocation() { return this.profiles.currentProfile.extensionsResource; }
  protected createInstallExtensionTask(_manifest: IExtensionManifest, _extension: URI | IGalleryExtension, _options: InstallExtensionTaskOptions): never { throw new Error('目标安装任务只由原生管理服务创建'); }
  protected createUninstallExtensionTask(_extension: ILocalExtension, _options: UninstallExtensionTaskOptions): never { throw new Error('目标卸载任务只由原生管理服务创建'); }
  protected copyExtension(_extension: ILocalExtension, from: URI, to: URI): never { this.assertProfile(from); this.assertProfile(to); throw new Error('默认 profile 不存在跨 profile 复制'); }
  protected moveExtension(_extension: ILocalExtension, from: URI, to: URI): never { this.assertProfile(from); this.assertProfile(to); throw new Error('默认 profile 不存在跨 profile 移动'); }
  protected removeExtension(_extension: ILocalExtension, from: URI): never { this.assertProfile(from); throw new Error('目标 profile 成员只能通过原生卸载入口移除'); }
  protected async deleteExtension(): Promise<void> { throw new Error('目标版本制品不能由浏览器物理删除'); }
  zip(): never { throw new Error('目标扩展尚未提供 VSIX 导出入口'); }
  getManifest(): never { throw new Error('目标 VSIX manifest 由原生安装器解析'); }
  download(): never { throw new Error('目标扩展下载只由原生管理服务执行'); }
  async cleanUp(): Promise<void> { }
}

function toLocalExtension(extension: IExtension, metadata: Metadata = {}): ILocalExtension {
  return { ...extension, identifier: { ...extension.identifier, uuid: metadata.id ?? extension.identifier.uuid },
    isMachineScoped: !!metadata.isMachineScoped, isApplicationScoped: !!metadata.isApplicationScoped,
    publisherId: metadata.publisherId ?? null, publisherDisplayName: metadata.publisherDisplayName ?? extension.publisherDisplayName,
    installedTimestamp: metadata.installedTimestamp, isPreReleaseVersion: !!metadata.isPreReleaseVersion,
    hasPreReleaseVersion: !!metadata.hasPreReleaseVersion, preRelease: extension.preRelease, updated: !!metadata.updated,
    pinned: !!metadata.pinned, forceAutoUpdate: false, private: !!metadata.private, isWorkspaceScoped: false,
    source: metadata.source ?? (extension.identifier.uuid ? 'gallery' : 'resource'), size: metadata.size ?? 0 };
}
