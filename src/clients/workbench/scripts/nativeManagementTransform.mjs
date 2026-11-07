import { dirname, join, relative } from 'node:path';

export const nativeManagementSourcePaths = [
  'src/vs/platform/extensionManagement/node/extensionManagementService.ts',
  'src/vs/platform/extensionManagement/node/extensionsWatcher.ts',
  'src/vs/platform/extensionManagement/node/extensionsManifestCache.ts',
  'src/vs/platform/extensionManagement/node/extensionDownloader.ts',
  'src/vs/platform/extensionManagement/common/extensionsScannerService.ts',
  'src/vs/platform/files/common/diskFileSystemProvider.ts',
];

/** 暴露并结算原服务自己的初始化及异步回调，不复制安装、扫描或清理算法。 */
export function transformNativeManagement(root, path, source, replaceOnce) {
  const matches = name => path === join(root, 'upstream', `src/vs/platform/extensionManagement/${name}.ts`);
  const lifecycleImport = () => {
    const imported = relative(dirname(path), join(root, 'node/extensionManagementLifecycle.js')).replaceAll('\\', '/');
    return `import { NativeManagementLifecycle } from ${JSON.stringify(imported)};\n`;
  };
  if (path === join(root, 'upstream/src/vs/platform/files/common/diskFileSystemProvider.ts')) {
    // 原 non-recursive client 的 dispose 不调用 stop；显式空 watch 沿原算法停止所有实例。
    source = replaceOnce(source, 'private refreshNonRecursiveWatchers(): void {',
      `async stopNativeManagementWatchers(): Promise<void> {
    this.nonRecursiveWatchRequestDelayer.cancel();
    this.universalWatchRequestDelayer.cancel();
    await Promise.all([this.nonRecursiveWatcher?.watch([]), this.universalWatcher?.watch([])]);
  }

  private refreshNonRecursiveWatchers(): void {`);
  } else if (matches('node/extensionManagementService')) {
    source = lifecycleImport() + source;
    source = replaceOnce(source, 'readonly _serviceBrand: undefined;\n\tscanAllUserInstalledExtensions()',
      'readonly _serviceBrand: undefined;\n\treadonly whenNativeManagementReady: Promise<void>;\n\tdrainNativeManagement(): Promise<void>;\n\tscanAllUserInstalledExtensions()');
    source = replaceOnce(source, 'private readonly extensionsScanner: ExtensionsScanner;',
      `private readonly nativeManagementLifecycle = new NativeManagementLifecycle();
  private readonly nativeManagementListeners = this._register(new DisposableStore());
  private readonly extensionsWatcher: ExtensionsWatcher;
  readonly whenNativeManagementReady: Promise<void>;
  private readonly extensionsScanner: ExtensionsScanner;`);
    source = replaceOnce(source, "import { Disposable }", "import { DisposableStore, Disposable }");
    source = replaceOnce(source, 'new ExtensionsManifestCache(userDataProfilesService, fileService, uriIdentityService, this, this.logService)',
      'new ExtensionsManifestCache(userDataProfilesService, fileService, uriIdentityService, this, this.logService, this.nativeManagementLifecycle)');
    source = replaceOnce(source, 'const extensionsWatcher = this._register(new ExtensionsWatcher(this, this.extensionsScannerService, userDataProfilesService, extensionsProfileScannerService, uriIdentityService, fileService, logService));',
      'const extensionsWatcher = this.extensionsWatcher = this._register(new ExtensionsWatcher(this, this.extensionsScannerService, userDataProfilesService, extensionsProfileScannerService, uriIdentityService, fileService, logService, this.nativeManagementLifecycle));');
    source = replaceOnce(source, 'e => this.onDidChangeExtensionsFromAnotherSource(e)',
      'e => this.nativeManagementLifecycle.track(this.onDidChangeExtensionsFromAnotherSource(e))');
    source = replaceOnce(source, 'this.watchForExtensionsNotInstalledBySystem();',
      `this.whenNativeManagementReady = Promise.allSettled([
    extensionsWatcher.whenNativeManagementReady, this.watchForExtensionsNotInstalledBySystem(), this.extensionsDownloader.whenNativeManagementReady,
  ]).then(results => {
    const failures = results.filter(result => result.status === 'rejected').map(result => result.reason);
    if (failures.length) throw new AggregateError(failures, '原生扩展管理初始化失败');
  });
`);
    source = replaceOnce(source, 'private _targetPlatformPromise: Promise<TargetPlatform> | undefined;',
      `async drainNativeManagement(): Promise<void> {
    // 初始化全部落定后撤销外部事件；内部 profile / install 事件继续结清原任务。
    await this.whenNativeManagementReady.then(() => undefined, () => undefined);
    this.nativeManagementListeners.dispose();
    this.extensionsWatcher.stopNativeManagementEvents();
    const failures: unknown[] = [];
    try { await this.nativeManagementLifecycle.drain(); } catch (error) { failures.push(error); }
    try { await this.extensionsScannerService.drainNativeManagement(); } catch (error) { failures.push(error); }
    if (failures.length === 1) throw failures[0];
    if (failures.length) throw new AggregateError(failures, '原生扩展管理收尾失败');
  }

  private _targetPlatformPromise: Promise<TargetPlatform> | undefined;`);
    source = replaceOnce(source, 'this._register(this.fileService.onDidFilesChange(e => this.onDidFilesChange(e)));',
      'this.nativeManagementListeners.add(this.fileService.onDidFilesChange(e => this.nativeManagementLifecycle.track(this.onDidFilesChange(e))));');
    source = replaceOnce(source, 'this.manifestCache.invalidate(profileLocation);', 'await this.manifestCache.invalidate(profileLocation);');
    // 复用结果来自原提取分支，沿 InstallOptions.context 回到同一次 VSIX 请求。
    source = replaceOnce(source, 'type ExtractExtensionResult = { readonly local: ILocalExtension;',
      `export const NATIVE_VSIX_REUSED_CONTEXT = 'boxteam.vsixVersionReused';
const NATIVE_VSIX_PREPARED_CONTEXT = 'boxteam.preparedVSIX';
type NativeVSIXPreparation = { readonly profileLocation: URI; readonly installed: ILocalExtension[]; readonly local: ILocalExtension | undefined };

type ExtractExtensionResult = { readonly local: ILocalExtension; readonly reused: boolean;`);
    source = replaceOnce(source, 'const manifest = await getManifest(path.resolve(location.fsPath));',
      'let manifest = await getManifest(path.resolve(location.fsPath));');
    // 候选仍先经过完整预检；依赖规划与实际发布共用同一次原扫描结果。
    source = replaceOnce(source, 'const results = await this.installExtensions([{ manifest, extension: location, options }]);',
      `if (options.keepExisting === true) {
\t\t\t\tconst profileLocation = options.profileLocation ?? this.userDataProfilesService.defaultProfile.extensionsResource;
\t\t\t\tconst installed = await this.extensionsScanner.scanExtensions(ExtensionType.User, profileLocation, options.productVersion ?? { version: this.productService.version, date: this.productService.date });
\t\t\t\tconst local = await this.extensionsScanner.scanReusableUserExtension(new ExtensionKey({ id: extensionId }, manifest.version), installed);
\t\t\t\tconst prepared: NativeVSIXPreparation = { profileLocation, installed, local };
\t\t\t\t(options.context ??= {})[NATIVE_VSIX_PREPARED_CONTEXT] = prepared;
\t\t\t\tif (local) manifest = local.manifest;
\t\t\t}

\t\t\tconst results = await this.installExtensions([{ manifest, extension: location, options }]);`);
    source = replaceOnce(source, '\t\t\tconst local = await this.extensionsScanner.extractUserExtension(',
      '\t\t\tconst result = await this.extensionsScanner.extractUserExtension(');
    source = replaceOnce(source, 'return { local, verificationStatus };', 'return { ...result, verificationStatus };');
    source = replaceOnce(source, '\t\tconst local = await this.extensionsScanner.extractUserExtension(',
      `\t\tconst prepared = options.context?.[NATIVE_VSIX_PREPARED_CONTEXT] as NativeVSIXPreparation | undefined;
\t\tif (prepared?.local) return { local: prepared.local, reused: true };
\t\treturn this.extensionsScanner.extractUserExtension(`);
    source = replaceOnce(source, '\n\t\treturn { local };\n\t}\n\n\tprivate async collectFiles',
      '\n\t}\n\n\tprivate async collectFiles');
    source = replaceOnce(source, '\tasync extractUserExtension(extensionKey: ExtensionKey, zipPath: string, removeIfExists: boolean, token: CancellationToken): Promise<ILocalExtension> {',
      `\tasync scanReusableUserExtension(extensionKey: ExtensionKey, installed: readonly ILocalExtension[] = []): Promise<ILocalExtension | undefined> {
\t\tconst extensionLocation = URI.file(path.join(this.extensionsScannerService.userExtensionsLocation.fsPath, extensionKey.toString()));
\t\tconst current = installed.find(extension => this.uriIdentityService.extUri.isEqual(extension.location, extensionLocation));
\t\tif (!current && !(await this.fileService.exists(extensionLocation))) return undefined;
\t\tconst local = current ?? await this.scanLocalExtension(extensionLocation, ExtensionType.User);
\t\tif (!local.isValid) {
\t\t\tthrow new ExtensionManagementError(local.validations.filter(([severity]) => severity === Severity.Error).map(([, message]) => message).join('\\n'), ExtensionManagementErrorCode.Invalid);
\t\t}
\t\tif (!extensionKey.equals(ExtensionKey.create(local))) {
\t\t\tthrow new ExtensionManagementError(\`Cannot reuse extension '\${extensionKey.toString()}': the existing extension at '\${extensionLocation.fsPath}' has a different identifier, version or target platform.\`, ExtensionManagementErrorCode.Invalid);
\t\t}
\t\treturn local;
\t}

\tasync extractUserExtension(extensionKey: ExtensionKey, zipPath: string, removeIfExists: boolean, token: CancellationToken): Promise<ExtractExtensionResult> {`);
    source = replaceOnce(source, `\t\tif (await this.fileService.exists(extensionLocation)) {
\t\t\tif (!removeIfExists) {
\t\t\t\ttry {
\t\t\t\t\treturn await this.scanLocalExtension(extensionLocation, ExtensionType.User);
\t\t\t\t} catch (error) {
\t\t\t\t\tthis.logService.warn(\`Error while scanning the existing extension at \${extensionLocation.path}. Deleting the existing extension and extracting it.\`, getErrorMessage(error));
\t\t\t\t}
\t\t\t}`, `\t\tif (!removeIfExists) {
\t\t\tconst local = await this.scanReusableUserExtension(extensionKey);
\t\t\tif (local) return { local, reused: true };
\t\t} else if (await this.fileService.exists(extensionLocation)) {`);
    source = replaceOnce(source, '\t\treturn this.scanLocalExtension(extensionLocation, ExtensionType.User);',
      '\t\treturn { local: await this.scanLocalExtension(extensionLocation, ExtensionType.User), reused: false };');
    source = replaceOnce(source, '\t\tconst installed = await this.extensionsScanner.scanExtensions(ExtensionType.User, this.options.profileLocation, this.options.productVersion);',
      `\t\tconst prepared = this.source instanceof URI && this.options.keepExisting === true
\t\t\t? this.options.context?.[NATIVE_VSIX_PREPARED_CONTEXT] as NativeVSIXPreparation | undefined : undefined;
\t\tconst installed = prepared && this.uriIdentityService.extUri.isEqual(prepared.profileLocation, this.options.profileLocation)
\t\t\t? prepared.installed : await this.extensionsScanner.scanExtensions(ExtensionType.User, this.options.profileLocation, this.options.productVersion);`);
    source = replaceOnce(source, '\t\t\tif (existingExtension) {\n\t\t\t\tif (this.extensionKey.equals(new ExtensionKey(existingExtension.identifier, existingExtension.manifest.version))) {',
      '\t\t\tif (this.options.keepExisting !== true && existingExtension) {\n\t\t\t\tif (this.extensionKey.equals(new ExtensionKey(existingExtension.identifier, existingExtension.manifest.version))) {');
    source = replaceOnce(source, '\t\t\t// Installing a VSIX extension shall replace the existing extension always.\n\t\t\tconst existingWithSameVersion = await this.unsetIfRemoved(this.extensionKey);',
      '\t\t\t// 未指定保留时仍沿原 VSIX 替换语义。\n\t\t\tconst existingWithSameVersion = this.options.keepExisting === true ? undefined : await this.unsetIfRemoved(this.extensionKey);');
    source = replaceOnce(source, '\t\t\tthis._verificationStatus = result.verificationStatus;',
      `\t\t\tthis._verificationStatus = result.verificationStatus;
\t\t\tif (this.source instanceof URI && this.options.context) {
\t\t\t\tthis.options.context[NATIVE_VSIX_REUSED_CONTEXT] = result.reused;
\t\t\t}`);
    source = replaceOnce(source, '\t\ttry {\n\t\t\tawait this.extensionsProfileScannerService.addExtensionsToProfile([[local, metadata]], this.options.profileLocation, !local.isValid);',
      `\t\t// 先完成原目录扫描；复用失败不能提前改写 .obsolete。
\t\tif (this.source instanceof URI && this.options.keepExisting === true) {
\t\t\tawait this.extensionsScanner.unsetExtensionsForRemoval(this.extensionKey);
\t\t}

\t\ttry {
\t\t\tawait this.extensionsProfileScannerService.addExtensionsToProfile([[local, metadata]], this.options.profileLocation, !local.isValid);`);
    // 本地 VSIX 的原 metadata 更新同样可能继续写盘，不能在它完成前退出。
    source = replaceOnce(source, 'this.updateMetadata(local, token);', 'await this.updateMetadata(local, token);');
  } else if (matches('common/abstractExtensionManagementService')) {
    const start = source.indexOf('\tasync uninstallExtensions(');
    const end = source.indexOf('\n\tprivate checkForDependents(', start);
    const uninstall = source.slice(start, end);
    let settled = replaceOnce(uninstall, 'const error = toExtensionManagementError(e);\n\t\t\tfor (const { task } of allTasks) {',
      `const error = toExtensionManagementError(e);
      // 先观察全部原任务，再取消尚未启动的任务；保留原业务错误并等待取消结算。
      const settling = Promise.allSettled(allTasks.map(({ task }) => task.waitUntilTaskIsFinished()));
\t\t\tfor (const { task } of allTasks) {`);
    settled = replaceOnce(settled, '\t\t\tthrow error;\n\t\t} finally {', '\t\t\tawait settling;\n\t\t\tthrow error;\n\t\t} finally {');
    source = replaceOnce(source, uninstall, settled);
  } else if (matches('node/extensionsWatcher')) {
    source = lifecycleImport() + source;
    source = replaceOnce(source, 'combinedDisposable, Disposable, DisposableMap', 'combinedDisposable, Disposable, DisposableMap, DisposableStore');
    source = replaceOnce(source, 'private readonly allExtensions = new Map<string, ResourceSet>;',
      'readonly whenNativeManagementReady: Promise<void>;\n\tprivate readonly nativeManagementListeners = this._register(new DisposableStore());\n\tprivate readonly allExtensions = new Map<string, ResourceSet>;');
    source = replaceOnce(source, 'private readonly logService: ILogService,',
      'private readonly logService: ILogService,\n\t\tprivate readonly nativeManagementLifecycle: NativeManagementLifecycle,');
    source = replaceOnce(source, "this.initialize().then(null, error => logService.error('Error while initializing Extensions Watcher', getErrorMessage(error)));",
      'this.whenNativeManagementReady = this.initialize();');
    // 保留官方 profile 成员变更及 .obsolete 标记，不在这里提前物理清理制品。
    source = replaceOnce(source, 'private registerListeners(): void {',
      'stopNativeManagementEvents(): void { this.nativeManagementListeners.dispose(); }\n\n\tprivate registerListeners(): void {');
    source = replaceOnce(source, 'this._register(this.userDataProfilesService.onDidChangeProfiles(e => this.onDidChangeProfiles(e.added)));',
      'this.nativeManagementListeners.add(this.userDataProfilesService.onDidChangeProfiles(e => this.nativeManagementLifecycle.track(this.onDidChangeProfiles(e.added))));');
    for (const method of ['onAddExtensions', 'onDidAddExtensions', 'onRemoveExtensions', 'onDidRemoveExtensions']) {
      source = replaceOnce(source, `e => this.${method}(e)`, `e => this.nativeManagementLifecycle.track(this.${method}(e))`);
    }
    source = replaceOnce(source, 'this._register(this.fileService.onDidFilesChange(e => this.onDidFilesChange(e)));',
      'this.nativeManagementListeners.add(this.fileService.onDidFilesChange(e => this.onDidFilesChange(e)));');
    source = replaceOnce(source, 'this.onDidExtensionsProfileChange(profile.extensionsResource);',
      'this.nativeManagementLifecycle.track(this.onDidExtensionsProfileChange(profile.extensionsResource));');
    source = replaceOnce(source, '}, error => this.logService.error(error)));', '}));');
    source = replaceOnce(source, 'await this.deleteExtensionsNotInProfiles(extensionsToDelete);\n\t\t\t}\n\t\t} catch (error) {\n\t\t\tthis.logService.error(error);',
      'await this.deleteExtensionsNotInProfiles(extensionsToDelete);\n\t\t\t}\n\t\t} catch (error) {\n\t\t\tthis.logService.error(error);\n\t\t\tthrow error;');
    source = replaceOnce(source, "import { getErrorMessage } from '../../../base/common/errors.js';\n", '');
  } else if (matches('node/extensionsManifestCache')) {
    source = lifecycleImport() + source;
    source = replaceOnce(source, 'private readonly logService: ILogService,',
      'private readonly logService: ILogService,\n\t\tprivate readonly nativeManagementLifecycle: NativeManagementLifecycle,');
    source = replaceOnce(source, 'this.invalidate(r.profileLocation);', 'this.nativeManagementLifecycle.track(this.invalidate(r.profileLocation));');
    source = replaceOnce(source, 'this.invalidate(e.profileLocation);', 'this.nativeManagementLifecycle.track(this.invalidate(e.profileLocation));');
    source = replaceOnce(source, 'this.logService.error(error);', 'this.logService.error(error);\n\t\t\t\tthrow error;');
  } else if (matches('node/extensionDownloader')) {
    source = replaceOnce(source, 'private readonly cleanUpPromise: Promise<void>;', 'private readonly cleanUpPromise: Promise<void>;\n\tget whenNativeManagementReady(): Promise<void> { return this.cleanUpPromise; }');
    source = replaceOnce(source, 'this.logService.error(error);\n\t\t\t\t}\n\t\t\t}\n\n\t\t\tconst folderStat',
      'this.logService.error(error);\n\t\t\t\t\tthrow error;\n\t\t\t\t}\n\t\t\t}\n\n\t\t\tconst folderStat');
    source = replaceOnce(source, '} catch (e) {\n\t\t\tthis.logService.error(e);\n\t\t}\n\t}\n\n\tprivate getName',
      '} catch (e) {\n\t\t\tthis.logService.error(e);\n\t\t\tthrow e;\n\t\t}\n\t}\n\n\tprivate getName');
  } else if (matches('common/extensionsScannerService')) {
    source = lifecycleImport() + source;
    source = replaceOnce(source, 'initializeDefaultProfileExtensions(): Promise<void>;', 'initializeDefaultProfileExtensions(): Promise<void>;\n\tdrainNativeManagement(): Promise<void>;');
    source = replaceOnce(source, 'private _targetPlatformPromise: Promise<TargetPlatform> | undefined;',
      `async drainNativeManagement(): Promise<void> {
    const results = await Promise.allSettled([this.systemExtensionsCachedScanner.drainNativeManagement(), this.userExtensionsCachedScanner.drainNativeManagement()]);
    const failures = results.filter(result => result.status === 'rejected').map(result => result.reason);
    if (failures.length) throw new AggregateError(failures, '原扫描器缓存收尾失败');
  }

  private _targetPlatformPromise: Promise<TargetPlatform> | undefined;`);
    source = replaceOnce(source, 'private readonly pendingValidations = new ResourceMap<ExtensionScannerInput>();',
      'private readonly nativeManagementLifecycle = new NativeManagementLifecycle();\n\tprivate readonly pendingValidations = new ResourceMap<ExtensionScannerInput>();');
    source = replaceOnce(source, 'private async validatePendingCaches(): Promise<void> {',
      `async drainNativeManagement(): Promise<void> {
    this.cacheValidatorThrottler.cancel();
    this.nativeManagementLifecycle.track(this.validatePendingCaches());
    await this.nativeManagementLifecycle.drain();
  }

  private async validatePendingCaches(): Promise<void> {`);
    source = replaceOnce(source, 'await Promise.all(pending.map(([cacheFile, input]) => this.validateCache(cacheFile, input)));',
      `const tasks = pending.map(([cacheFile, input]) => this.validateCache(cacheFile, input));
    for (const task of tasks) this.nativeManagementLifecycle.track(task);
    await Promise.all(tasks);`);
    source = replaceOnce(source, "this.logService.warn('Failed to create default profile extensions manifest in extensions installation folder.', this.userExtensionsLocation.toString(), getErrorMessage(error));",
      "this.logService.warn('Failed to create default profile extensions manifest in extensions installation folder.', this.userExtensionsLocation.toString(), getErrorMessage(error));\n\t\t\t\t\t\t\t\tthrow error;");
    source = replaceOnce(source, 'this.logService.error(error);\n\t\t\t\t} finally {\n\t\t\t\t\tthis.initializeDefaultProfileExtensionsPromise',
      'this.logService.error(error);\n\t\t\t\t\tthrow error;\n\t\t\t\t} finally {\n\t\t\t\t\tthis.initializeDefaultProfileExtensionsPromise');
  }
  return source;
}
