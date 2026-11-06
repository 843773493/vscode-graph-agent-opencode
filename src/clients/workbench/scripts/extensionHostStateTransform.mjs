import { join } from 'node:path';

export const extensionHostStateSourcePaths = [
  'src/vs/workbench/services/extensions/common/extensions.ts',
  'src/vs/workbench/services/extensions/common/extensionDescriptionRegistry.ts',
  'src/vs/workbench/api/common/extHostExtensionService.ts',
  'src/vs/workbench/services/extensions/common/abstractExtensionService.ts',
  'src/vs/workbench/services/extensions/common/extensionHostManager.ts',
  'src/vs/workbench/services/extensions/common/lazyCreateExtensionHostManager.ts',
  'src/vs/workbench/api/browser/mainThreadExtensionService.ts',
];

/** 浏览器和 Node 宿主共用描述版本、候选验证和原子发布合同。 */
export function transformExtensionHostState(root, path, source, replaceOnce) {
  const paths = extensionHostStateSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[0]) {
    let contents = "import { equals } from '../../../../base/common/objects.js';\n" + source;
    contents = replaceOnce(contents, '\t\tthis._versionId = versionId;\n\t\tthis._allExtensions = allExtensions.slice(0);',
      '\t\tvalidateExtensionHostState(allExtensions, myExtensions);\n\t\tthis._versionId = versionId;\n\t\tthis._allExtensions = allExtensions.slice(0);');
    const start = contents.indexOf('\tpublic set(versionId: number, allExtensions:');
    const end = contents.indexOf('\tpublic containsExtension(', start);
    contents = replaceOnce(contents, contents.slice(start, end), `
  public set(versionId: number, allExtensions: IExtensionDescription[], myExtensions: ExtensionIdentifier[]): IExtensionDescriptionDelta {
    if (versionId < this._versionId) throw new Error('ExtensionHostExtensions: snapshot version ' + versionId + ' precedes ' + this._versionId);
    const next = validateExtensionHostState(allExtensions, myExtensions);
    const old = extensionDescriptionArrayToMap(this._allExtensions);
    const toRemove = this._allExtensions.filter(extension => {
      const replacement = next.get(extension.identifier);
      return !replacement || !extensionDescriptionsEqual(extension, replacement);
    }).map(extension => extension.identifier);
    const toAdd = allExtensions.filter(extension => {
      const previous = old.get(extension.identifier);
      return !previous || !extensionDescriptionsEqual(previous, extension);
    });
    const previousMine = new ExtensionIdentifierSet(this._myExtensions);
    const nextMine = new ExtensionIdentifierSet(myExtensions);
    const myToRemove = this._myExtensions.filter(id => !nextMine.has(id));
    const myToAdd = myExtensions.filter(id => !previousMine.has(id));
    if (versionId === this._versionId && (toRemove.length || toAdd.length || myToRemove.length || myToAdd.length)) {
      throw new Error('ExtensionHostExtensions: conflicting snapshot at version ' + versionId);
    }
    const delta = { versionId, toRemove, toAdd, myToRemove, myToAdd,
      addActivationEvents: ImplicitActivationEvents.createActivationEventsMap(toAdd) };
    // 快照是集合输入；沿发送的同一delta提交，空delta不改变原有排列。
    this.delta(delta);
    return delta;
  }

  public delta(delta: IExtensionDescriptionDelta): IExtensionDescriptionDelta | null {
    if (delta.versionId <= this._versionId) return null;
    extensionDescriptionArrayToMap(delta.toAdd);
    const removed = uniqueExtensionIdentifiers(delta.toRemove);
    const myRemoved = uniqueExtensionIdentifiers(delta.myToRemove);
    uniqueExtensionIdentifiers(delta.myToAdd);
    const all = this._allExtensions.filter(extension => !removed.has(extension.identifier)).concat(delta.toAdd);
    const mine = this._myExtensions.filter(id => !myRemoved.has(id)).concat(delta.myToAdd);
    validateExtensionHostState(all, mine);
    this._commit(delta.versionId, all, mine);
    return delta;
  }

  private _commit(versionId: number, all: readonly IExtensionDescription[], mine: ExtensionIdentifier[]): void {
    this._allExtensions = all.slice();
    this._myExtensions = mine.slice();
    this._versionId = versionId;
    this._myActivationEvents = null;
  }

`);
    contents = replaceOnce(contents, 'function extensionDescriptionArrayToMap(extensions: IExtensionDescription[]):',
      'function extensionDescriptionArrayToMap(extensions: readonly IExtensionDescription[]):');
    contents = replaceOnce(contents, '\t\tresult.set(extension.identifier, extension);',
      "\t\tif (result.has(extension.identifier)) throw new Error('Duplicate extension description: ' + extension.identifier.value);\n\t\tresult.set(extension.identifier, extension);");
    contents = replaceOnce(contents, 'export interface IInternalExtensionService {',
      `export interface IInternalExtensionService {
  // 宿主准入与此内部回调对象同寿命，关闭后不能写入下一代的全局状态。
  dispose(): void;
  getExtension(id: string): Promise<IExtensionDescription | undefined>;`);
    return contents + `
function uniqueExtensionIdentifiers(ids: readonly ExtensionIdentifier[]): ExtensionIdentifierSet {
  const result = new ExtensionIdentifierSet();
  for (const id of ids) {
    if (result.has(id)) throw new Error('Duplicate extension identifier: ' + id.value);
    result.add(id);
  }
  return result;
}

function validateExtensionHostState(all: readonly IExtensionDescription[], mine: ExtensionIdentifier[]): ExtensionIdentifierMap<IExtensionDescription> {
  const descriptions = extensionDescriptionArrayToMap(all);
  uniqueExtensionIdentifiers(mine);
  for (const id of mine) {
    if (!descriptions.has(id)) throw new Error('Host extension is absent from its description snapshot: ' + id.value);
  }
  return descriptions;
}

function extensionDescriptionsEqual(a: IExtensionDescription, b: IExtensionDescription): boolean {
  return a === b || equals(
    { ...a, identifier: ExtensionIdentifier.toKey(a.identifier), extensionLocation: a.extensionLocation.toString() },
    { ...b, identifier: ExtensionIdentifier.toKey(b.identifier), extensionLocation: b.extensionLocation.toString() }
  );
}
`;
  }
  if (path === paths[1]) {
    const start = source.indexOf('\tpublic set(extensionDescriptions:');
    const end = source.indexOf('\tpublic deltaExtensions(', start);
    return replaceOnce(source, source.slice(start, end), `
  public set(extensionDescriptions: IExtensionDescription[]): { versionId: number } {
    const candidate = new ExtensionDescriptionRegistry(this._activationEventsReader, extensionDescriptions, this._requireTarget);
    ExtensionDescriptionRegistry.replaceAll([{ target: this, source: candidate }]);
    candidate.dispose();
    return { versionId: this._versionId };
  }

  // 候选已经完成构建；先替换全部状态，再通知同步读取两份 registry 的消费者。
  public static replaceAll(updates: readonly { target: ExtensionDescriptionRegistry; source: ExtensionDescriptionRegistry }[]): void {
    for (const { target, source } of updates) {
      target._extensionDescriptions = source._extensionDescriptions;
      target._extensionsMap = source._extensionsMap;
      target._extensionsArr = source._extensionsArr;
      target._activationMap = source._activationMap;
      target._versionId++;
    }
    for (const { target } of updates) target._onDidChange.fire(undefined);
  }

`);
  }
  if (path === paths[3]) {
    const start = source.indexOf('\tprivate _acquireInternalAPI(');
    const end = source.indexOf('\tprivate _onWillActivateExtension(', start);
    let contents = replaceOnce(source, source.slice(start, end), `
  private _acquireInternalAPI(extensionHost: IExtensionHost): IInternalExtensionService {
    // TODO(5.6)：其余MainThread异步UI及RPC载荷还须沿各owner迁齐代际和视图投影。
    let active = true;
    const assertActive = () => {
      if (!active) throw new Error('Extension host callback generation is closed');
    };
    const assertOwned = (id: ExtensionIdentifier) => {
      assertActive();
      if (!extensionHost.extensions?.containsExtension(id)) {
        throw new Error('Extension does not belong to the calling host: ' + id.value);
      }
    };
    const assertActivatable = (id: ExtensionIdentifier) => {
      assertActive();
      const extensions = extensionHost.extensions;
      const description = extensions?.allExtensions.find(extension => ExtensionIdentifier.equals(extension.identifier, id));
      if (!extensions || !description || (!extensions.containsExtension(id) &&
          (!(description.main || description.browser) || description.api !== 'none'))) {
        throw new Error('Extension is not activatable from the calling host: ' + id.value);
      }
    };
    return {
      dispose: () => { active = false; },
      getExtension: async (id: string) => {
        assertActive();
        await this._installedExtensionsReady.wait();
        assertActive();
        return extensionHost.extensions?.allExtensions.find(extension => ExtensionIdentifier.equals(extension.identifier, id));
      },
      _activateById: async (extensionId: ExtensionIdentifier, reason: ExtensionActivationReason): Promise<void> => {
        assertActivatable(extensionId);
        await this._activateById(extensionId, reason, extensionHost);
        // 已准入操作可能仍在结算；迟到完成不能重新授权原宿主或重放到新代际。
        assertActive();
      },
      _onWillActivateExtension: extensionId => {
        assertOwned(extensionId);
        this._onWillActivateExtension(extensionId, extensionHost.runningLocation);
      },
      _onDidActivateExtension: (extensionId, codeLoadingTime, activateCallTime, activateResolvedTime, activationReason) => {
        assertOwned(extensionId);
        this._onDidActivateExtension(extensionId, codeLoadingTime, activateCallTime, activateResolvedTime, activationReason);
      },
      _onDidActivateExtensionError: (extensionId, error) => {
        // 原activator也报告合法api:none依赖的激活失败；这条诊断不写它的运行状态。
        assertActivatable(extensionId);
        this._onDidActivateExtensionError(extensionId, error);
      },
      _onExtensionRuntimeError: (extensionId, error) => {
        assertOwned(extensionId);
        this._onExtensionRuntimeError(extensionId, error);
      }
    };
  }

  public async _activateById(extensionId: ExtensionIdentifier, reason: ExtensionActivationReason, source?: IExtensionHost): Promise<void> {
    // 只向实际持有者发送一次；无发起上下文的同ID多实例必须明确消解，不能广播副作用。
    const managers = this._extensionHostManagers.filter(manager => manager.containsExtension(extensionId)
      && (!source?.extensions?.containsExtension(extensionId) || manager.representsRunningLocation(source.runningLocation)));
    if (managers.length !== 1) {
      throw new Error((managers.length ? 'Ambiguous extension ' : 'Unknown extension ') + extensionId.value);
    }
    if (!await managers[0].activate(extensionId, reason)) throw new Error('Extension activation was refused: ' + extensionId.value);
  }

`);
    contents = replaceOnce(contents, `await Promise.all(
\t\t\t\tthis._extensionHostManagers.map(extHostManager => extHostManager.activate(extensionDescription.identifier, { startup: false, extensionId: extensionDescription.identifier, activationEvent: shouldActivateReason }))
\t\t\t);`,
      'await this._activateById(extensionDescription.identifier, { startup: false, extensionId: extensionDescription.identifier, activationEvent: shouldActivateReason });');
    contents = replaceOnce(contents,
      'await extensionHostManager.deltaExtensions({ versionId, toRemove, toAdd, addActivationEvents, myToRemove, myToAdd: myToAdd.map(extension => extension.identifier) });',
      `try {
      await extensionHostManager.deltaExtensions({ versionId, toRemove, toAdd, addActivationEvents, myToRemove, myToAdd: myToAdd.map(extension => extension.identifier) });
    } catch (error) {
      throw await this._stopFailedExtensionHost(extensionHostManager, error);
    }`);
    contents = replaceOnce(contents,
      'extHostManager.start(snapshot.versionId, snapshot.extensions, extensions.map(extension => extension.identifier));',
      `void extHostManager.start(snapshot.versionId, snapshot.extensions, extensions.map(extension => extension.identifier))
        .catch(error => this._stopFailedExtensionHost(extHostManager, error));`);
    contents = replaceOnce(contents, '\tprivate _startOnDemandExtensionHosts(): void {', `
  private async _stopFailedExtensionHost(manager: IExtensionHostManager, error: unknown): Promise<unknown> {
    // 全局registry已经提交；失败代际不能继续消费后续增量，也不能自动重放未知RPC。
    let failure = error;
    try {
      await this._extensionHostManagers.stopOne(manager);
    } catch (cleanupError) {
      failure = new AggregateError([error, cleanupError], 'Extension host update and cleanup failed: ' + toErrorMessage(error));
    }
    this._logService.error('Extension host state update failed: ' + manager.friendyName, failure);
    this._notificationService.error(toErrorMessage(failure));
    return failure;
  }

\tprivate _startOnDemandExtensionHosts(): void {`);
    const stop = `\t\t\tthis._extensionHostManagers.splice(index, 1);
\t\t\tawait extensionHostManager.disconnect();
\t\t\textensionHostManager.dispose();`;
    return replaceOnce(contents, stop, `
      const [manager] = this._extensionHostManagers.splice(index, 1);
      // 先撤销退出订阅，主动结束失败代际不能再被当作崩溃自动重启。
      manager.disposableStore.dispose();
      const failures: unknown[] = [];
      try { await extensionHostManager.disconnect(); } catch (error) { failures.push(error); }
      try { extensionHostManager.dispose(); } catch (error) { failures.push(error); }
      if (failures.length) throw new AggregateError(failures, 'Extension host cleanup failed');`);
  }
  if (path === paths[4] || path === paths[5]) {
    let contents = replaceOnce(source, '\tpublic async disconnect(): Promise<void> {',
      `\tpublic async disconnect(): Promise<void> {
    this._internalExtensionService.dispose();`);
    const dispose = path === paths[4] ? '\tpublic override dispose(): void {' : '\toverride dispose(): void {';
    return replaceOnce(contents, dispose, `${dispose}\n    this._internalExtensionService.dispose();`);
  }
  if (path === paths[6]) {
    let contents = replaceOnce(source, 'ExtensionActivationReason, IExtensionService, IInternalExtensionService',
      'ExtensionActivationReason, IInternalExtensionService');
    contents = replaceOnce(contents, '\t\t@IExtensionService private readonly _extensionService: IExtensionService,\n', '');
    contents = replaceOnce(contents, 'this._extensionService.getExtension(extensionId)', 'this._internalExtensionService.getExtension(extensionId)');
    return replaceOnce(contents, 'this._extensionService.getExtension(extensionId.value)', 'this._internalExtensionService.getExtension(extensionId.value)');
  }
  if (path !== paths[2]) return source;
  let contents = replaceOnce(source, 'asPromise, Barrier, IntervalTimer, timeout', 'asPromise, Barrier, IntervalTimer, Sequencer, timeout');
  contents = replaceOnce(contents, 'MissingExtensionDependency, ActivationKind,', 'ExtensionHostExtensions, MissingExtensionDependency, ActivationKind,');
  contents = replaceOnce(contents, '\tprivate readonly _activationEventsReader: SyncedActivationEventsReader;', `
  private readonly _extensionStateQueue = new Sequencer();
  private _extensionStateVersion: number;
  private _receivedStart = false;
  private readonly _activationEventsReader: SyncedActivationEventsReader;`);
  const initialize = '\t\tthis._activationEventsReader = new SyncedActivationEventsReader(this._initData.extensions.activationEvents);';
  contents = replaceOnce(contents, initialize, `
    const snapshot = this._initData.extensions;
    new ExtensionHostExtensions(snapshot.versionId, snapshot.allExtensions, snapshot.myExtensions);
    this._extensionStateVersion = snapshot.versionId;
${initialize}`);
  contents = replaceOnce(contents, 'this._realPathCache.set(fsPath, this._hostUtils.fsRealpath(fsPath));',
    `this._realPathCache.set(fsPath, this._hostUtils.fsRealpath(fsPath).catch(error => {
      this._realPathCache.delete(fsPath);
      throw error;
    }));`);
  contents = replaceOnce(contents, '\t\t\t\treturn new ExtensionPaths(searchTree);\n\t\t\t});',
    `\t\t\t\treturn new ExtensionPaths(searchTree);
      }, error => {
        this._extensionPathIndex = null;
        throw error;
      });`);
  const start = contents.indexOf('\tpublic async $startExtensionHost(');
  const end = contents.indexOf('\tpublic $activateByEvent(', start);
  contents = replaceOnce(contents, contents.slice(start, end), `
  public async $startExtensionHost(delta: IExtensionDescriptionDelta): Promise<void> {
    await this._extensionStateQueue.queue(async () => {
      if (this._receivedStart || this._started) throw new Error('Extension host is already started');
      if (delta.versionId < this._extensionStateVersion) throw new Error('Extension host start snapshot is stale');
      if (delta.versionId === this._extensionStateVersion &&
          (delta.toAdd.length || delta.toRemove.length || delta.myToAdd.length || delta.myToRemove.length || Object.keys(delta.addActivationEvents).length)) {
        throw new Error('Extension host start conflicts with its initial snapshot');
      }
      await this._applyExtensionsDelta(delta);
      this._receivedStart = true;
    });
    // 激活可能回调 RPC，不能持有状态队列等待扩展执行。
    return this._startExtensionHost();
  }

`);
  const deltaStart = contents.indexOf('\tpublic async $deltaExtensions(');
  const deltaEnd = contents.indexOf('\tpublic async $test_latency(', deltaStart);
  contents = replaceOnce(contents, contents.slice(deltaStart, deltaEnd), `
  public $deltaExtensions(delta: IExtensionDescriptionDelta): Promise<void> {
    return this._extensionStateQueue.queue(async () => {
      if (delta.versionId <= this._extensionStateVersion) return;
      await this._applyExtensionsDelta(delta);
    });
  }

  private async _applyExtensionsDelta(delta: IExtensionDescriptionDelta): Promise<void> {
    const revived = { ...delta, toAdd: delta.toAdd.map(extension =>
      ({ ...extension, extensionLocation: URI.revive(extension.extensionLocation) })) };
    const { globalRegistry, myRegistry, activationEventsReader } = applyExtensionsDelta(
      this._activationEventsReader, this._globalRegistry, this._myRegistry, this._extensionStateVersion, revived);
    try {
      const searchTree = await this._createExtensionPathIndex(myRegistry.getAllExtensionDescriptions());
      const paths = this._extensionPathIndex ? await this._extensionPathIndex : new ExtensionPaths(searchTree);
      // 以下发布段没有 await；事件消费者只能观察到同一版本的完整状态。
      this._activationEventsReader.replace(activationEventsReader);
      paths.setSearchTree(searchTree);
      this._extensionPathIndex = Promise.resolve(paths);
      this._extensionStateVersion = delta.versionId;
      ExtensionDescriptionRegistry.replaceAll([
        { target: this._globalRegistry, source: globalRegistry },
        { target: this._myRegistry, source: myRegistry }
      ]);
    } finally {
      globalRegistry.dispose();
      myRegistry.dispose();
    }
  }

`);
  const helperStart = contents.indexOf('function applyExtensionsDelta(');
  const helperEnd = contents.indexOf('type TelemetryActivationEvent =', helperStart);
  contents = replaceOnce(contents, contents.slice(helperStart, helperEnd), `
function applyExtensionsDelta(reader: SyncedActivationEventsReader, oldGlobal: ExtensionDescriptionRegistry,
  oldMine: ExtensionDescriptionRegistry, version: number, delta: IExtensionDescriptionDelta) {
  const previous = oldGlobal.getAllExtensionDescriptions();
  const mine = oldMine.getAllExtensionDescriptions().map(extension => extension.identifier);
  // 先核 wire 描述和归属，不让 registry 的重复 ID 忽略分支吞掉非法更新。
  const candidate = new ExtensionHostExtensions(version, previous, mine);
  candidate.delta(delta);
  const activationEventsReader = reader.fork(delta);
  const globalRegistry = new ExtensionDescriptionRegistry(activationEventsReader, previous);
  globalRegistry.deltaExtensions(delta.toAdd, delta.toRemove);
  // 保留原 registry 对依赖环及其依赖闭包的过滤语义。
  const myExtensions = filterExtensions(globalRegistry, new ExtensionIdentifierSet(candidate.myExtensions));
  const myRegistry = new ExtensionDescriptionRegistry(activationEventsReader, myExtensions);
  return { globalRegistry, myRegistry, activationEventsReader };
}

`);
  contents = replaceOnce(contents, 'private readonly _map = new ExtensionIdentifierMap<string[]>();',
    'private _map = new ExtensionIdentifierMap<string[]>();');
  const readerMethod = '\tpublic addActivationEvents(activationEvents: { [extensionId: string]: string[] }): void {';
  return replaceOnce(contents, readerMethod, `
  public fork(delta: IExtensionDescriptionDelta): SyncedActivationEventsReader {
    const candidate = new SyncedActivationEventsReader({});
    this._map.forEach((events, id) => candidate._map.set(id, events));
    // 替换为零激活事件时也必须撤销旧版本；wire 的事件表会省略空数组。
    for (const id of delta.toRemove) candidate._map.delete(id);
    for (const extension of delta.toAdd) candidate._map.delete(extension.identifier);
    candidate.addActivationEvents(delta.addActivationEvents);
    return candidate;
  }

  public replace(candidate: SyncedActivationEventsReader): void {
    this._map = candidate._map;
  }

${readerMethod}`);
}
