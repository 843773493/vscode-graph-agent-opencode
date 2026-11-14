import { t } from '../../../shared/core/i18n/index.js';
import { raceCancellationError } from '../../upstream/src/vs/base/common/async.js';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { DisposableStore, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { FileAccess } from '../../upstream/src/vs/base/common/network.js';
import { ResourceSet } from '../../upstream/src/vs/base/common/map.js';
import type { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IModelService } from '../../upstream/src/vs/editor/common/services/model.js';
import { FindMatch, type ITextModel } from '../../upstream/src/vs/editor/common/model.js';
import { Range } from '../../upstream/src/vs/editor/common/core/range.js';
import { IFileService } from '../../upstream/src/vs/platform/files/common/files.js';
import { InstantiationType, registerSingleton } from '../../upstream/src/vs/platform/instantiation/common/extensions.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ILogService } from '../../upstream/src/vs/platform/log/common/log.js';
import { ITelemetryService } from '../../upstream/src/vs/platform/telemetry/common/telemetry.js';
import { IUriIdentityService } from '../../upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import { IWebWorkerService } from '../../upstream/src/vs/platform/webWorker/browser/webWorkerService.js';
import { WebWorkerDescriptor } from '../../upstream/src/vs/platform/webWorker/browser/webWorkerDescriptor.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IExtensionService } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';
// 基类模块先完成原 singleton 登记，再登记唯一项目实现，发生于 Workbench create 之前。
import { RemoteSearchService } from '../../upstream/src/vs/workbench/services/search/browser/searchService.js';
import { DEFAULT_MAX_SEARCH_RESULTS, FileMatch, IAITextQuery, IFileQuery, ISearchComplete, ISearchProgressItem, ISearchService, ITextQuery, SearchProviderType } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import { editorMatchesToTextSearchResults, getTextSearchMatchWithModelContext } from '../../upstream/src/vs/workbench/services/search/common/searchHelpers.js';
import { WorkspaceSearchProvider } from './fileProvider.js';
import { businessQuery, canonicalFileQuery, fileScheme, openResources, requiresCanonicalFileQuery, resourceInQuery, validateTextQuery } from './query.js';
import type { OpenModelSearchWorker } from './openModelSearchWorker.js';

export class WorkspaceSearchService extends RemoteSearchService {
  private readonly businessProvider: WorkspaceSearchProvider;

  constructor(
    @IModelService private readonly businessModels: IModelService,
    @IEditorService private readonly businessEditors: IEditorService,
    @ITelemetryService telemetryService: ITelemetryService,
    @ILogService logService: ILogService,
    @IExtensionService extensionService: IExtensionService,
    @IFileService fileService: IFileService,
    @IInstantiationService instantiationService: IInstantiationService,
    @IUriIdentityService private readonly businessIdentity: IUriIdentityService,
    @IWebWorkerService private readonly businessWorkers: IWebWorkerService,
  ) {
    super(businessModels, businessEditors, telemetryService, logService, extensionService, fileService, instantiationService, businessIdentity);
    this.businessProvider = this._register(instantiationService.createInstance(WorkspaceSearchProvider));
    this._register(this.registerSearchResultProvider(fileScheme, SearchProviderType.file, this.businessProvider));
    this._register(this.registerSearchResultProvider(fileScheme, SearchProviderType.text, this.businessProvider));
  }

  override aiTextSearch(query: IAITextQuery, token?: CancellationToken, onProgress?: (item: ISearchProgressItem) => void): Promise<ISearchComplete> {
    if (businessQuery(query)) return Promise.reject(new Error(t('quickAccess.search.aiUnavailable')));
    return super.aiTextSearch(query, token, onProgress);
  }

  override fileSearch(query: IFileQuery, token?: CancellationToken): Promise<ISearchComplete> {
    if (businessQuery(query)) return this.businessProvider.fileSearch(query, token);
    return super.fileSearch(query, token);
  }

  override textSearchSplitSyncAsync(query: ITextQuery, token = CancellationToken.None,
    onProgress?: (item: ISearchProgressItem) => void, notebookFilesToIgnore?: ResourceSet,
    asyncNotebookFilesToIgnore?: Promise<ResourceSet>): { syncResults: ISearchComplete; asyncResults: Promise<ISearchComplete> } {
    if (!businessQuery(query)) return super.textSearchSplitSyncAsync(query, token, onProgress, notebookFilesToIgnore, asyncNotebookFilesToIgnore);
    const operation = this.businessProvider.beginSearch(token), source = operation.source;
    token = source.token;
    const lifetime = this._register(new DisposableStore());
    lifetime.add(operation);
    const deadlineAt = Date.now() + 10000;
    let timedOut = false;
    if (query.contentPattern.isRegExp) {
      const timer = setTimeout(() => { timedOut = true; source.cancel(); }, Math.max(0, deadlineAt - Date.now()));
      lifetime.add(toDisposable(() => clearTimeout(timer)));
    }
    const search = (canonical: ITextQuery) => this.textSearchCanonical(canonical, operation, lifetime, onProgress,
      notebookFilesToIgnore, asyncNotebookFilesToIgnore, deadlineAt);
    try {
      const split = requiresCanonicalFileQuery(query) ? { syncResults: { results: [], messages: [], limitHit: false },
        asyncResults: canonicalFileQuery(query, token, deadlineAt).then(async canonical => {
          operation.validate();
          const split = search(canonical), remote = await split.asyncResults;
          return { ...remote, results: [...split.syncResults.results, ...remote.results],
            messages: [...split.syncResults.messages, ...remote.messages], limitHit: split.syncResults.limitHit || remote.limitHit };
        }) } : search(query);
      return { ...split, asyncResults: split.asyncResults.then(result => { operation.validate(); return result; },
        error => { operation.validate(); if (timedOut) throw new Error(t('quickAccess.files.deadlineExceeded'), { cause: error });
          throw error; }).finally(() => this._store.delete(lifetime)) };
    } catch (error) {
      this._store.delete(lifetime);
      operation.validate();
      throw error;
    }
  }

  private textSearchCanonical(query: ITextQuery, operation: ReturnType<WorkspaceSearchProvider['beginSearch']>, lifetime: DisposableStore,
    onProgress: ((item: ISearchProgressItem) => void) | undefined,
    notebookFilesToIgnore: ResourceSet | undefined, asyncNotebookFilesToIgnore: Promise<ResourceSet> | undefined, deadlineAt: number
  ): { syncResults: ISearchComplete; asyncResults: Promise<ISearchComplete> } {
    const identity = this.businessIdentity.extUri;
    const source = operation.source, token = source.token;
    operation.validate();
    query = validateTextQuery(query, identity);
    if (token.isCancellationRequested) throw new CancellationError();
    if (this._store.isDisposed) throw new Error(t('quickAccess.files.disposed'));
    const key = (resource: URI) => identity.getComparisonKey(resource);
    const open = new ResourceSet(openResources(this.businessEditors, query, identity), key);
    const covered = new ResourceSet(key), results: FileMatch[] = [];
    const ignoredModels = new ResourceSet([...(notebookFilesToIgnore ?? [])], key);
    const maximum = query.maxResults ?? DEFAULT_MAX_SEARCH_RESULTS;
    let count = 0, limitHit = false;
    const modelVersions = new Map<ReturnType<typeof this.businessModels.getModels>[number], number>();
    const selectedModels: ITextModel[] = [];
    const validateModels = () => {
      const current = new Map(this.businessModels.getModels().map(model => [key(model.uri), model]));
      if ([...modelVersions].some(([model, version]) => model.isDisposed() || current.get(key(model.uri)) !== model || model.getVersionId() !== version)) {
        throw new Error(t('quickAccess.files.modelsChanged'));
      }
    };
    const watchRetryModels = (retrySource: CancellationTokenSource) => {
      validateModels();
      const watch = new DisposableStore();
      for (const model of modelVersions.keys()) {
        watch.add(model.onDidChangeContent(() => retrySource.cancel()));
        watch.add(model.onWillDispose(() => retrySource.cancel()));
      }
      return { dispose: () => watch.dispose(), validate: validateModels };
    };
    const acceptMatches = (model: ITextModel, matches: FindMatch[]) => {
      if (token.isCancellationRequested) throw new CancellationError();
      if (matches.length > maximum - count) limitHit = true;
      const accepted = matches.slice(0, maximum - count);
      count += accepted.length;
      if (accepted.length) {
        const match = new FileMatch(model.uri);
        match.results = getTextSearchMatchWithModelContext(editorMatchesToTextSearchResults(accepted, model, query.previewOptions), model, query);
        results.push(match);
        onProgress?.(match);
      }
    };
    const separators = query.contentPattern.isWordMatch ? query.contentPattern.wordSeparators ?? null : null;
    try {
      for (const model of this.businessModels.getModels()) {
        if (token.isCancellationRequested) throw new CancellationError();
        // 在 findMatches 和首次 progress 之前限制完整身份；零命中模型也覆盖磁盘正文。
        if (!open.has(model.uri) || !resourceInQuery(model.uri, query, identity) || covered.has(model.uri)) continue;
        covered.add(model.uri);
        modelVersions.set(model, model.getVersionId());
        if (ignoredModels.has(model.uri)) continue;
        // 位置及负命中都属于这一代模型；变更／释放后停止网络、续页及迟到 progress。
        lifetime.add(model.onDidChangeContent(() => source.cancel()));
        lifetime.add(model.onWillDispose(() => source.cancel()));
        if (query.contentPattern.isRegExp) selectedModels.push(model);
        else {
          acceptMatches(model, model.findMatches(query.contentPattern.pattern, false, false,
            !!query.contentPattern.isCaseSensitive, separators, false, maximum - count + 1));
          if (limitHit) break;
        }
      }
      let pendingModels: Promise<void> | undefined;
      if (query.contentPattern.isRegExp && selectedModels.length) {
        const worker = lifetime.add(this.businessWorkers.createWorkerClient<OpenModelSearchWorker>(new WebWorkerDescriptor({
          esmModuleLocation: () => FileAccess.asBrowserUri('boxteam/openModelSearchWorker.js'), label: 'open-model-search',
        })));
        lifetime.add(token.onCancellationRequested(() => worker.dispose()));
        pendingModels = (async () => {
          for (const model of selectedModels) {
            if (token.isCancellationRequested) throw new CancellationError();
            const ranges = await raceCancellationError(worker.proxy.$findMatches(model.getValue(), query.contentPattern.pattern,
              !!query.contentPattern.isCaseSensitive, separators, maximum - count + 1), token);
            validateModels();
            acceptMatches(model, ranges.map(range => new FindMatch(Range.lift(range), null)));
            if (limitHit) break;
          }
        })();
      }
      if (token.isCancellationRequested) throw new CancellationError();
      const syncResults: ISearchComplete = { results: [...results], messages: [], limitHit };
      const asyncResults = (async () => {
        try {
          await pendingModels;
          // 本地已取得超限哨兵，无剩余输出或完整性信息需要远端补充。
          if (limitHit) return { results: results.slice(syncResults.results.length), messages: [], limitHit: true };
          const ignored = await raceCancellationError(asyncNotebookFilesToIgnore ?? Promise.resolve(new ResourceSet()), token);
          for (const resource of ignoredModels) covered.add(resource);
          for (const resource of ignored) covered.add(resource);
          const remote = await this.businessProvider.textSearch({ ...query, maxResults: maximum - count }, onProgress, token, covered, { deadlineAt, operation, watchRetryModels });
          return { ...remote, results: [...results.slice(syncResults.results.length), ...remote.results] };
        } catch (error) {
          validateModels();
          throw error;
        }
      })();
      return { syncResults, asyncResults };
    } catch (error) {
      validateModels();
      throw error;
    }
  }
}

registerSingleton(ISearchService, WorkspaceSearchService, InstantiationType.Delayed);
