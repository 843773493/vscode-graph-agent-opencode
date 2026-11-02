import { raceCancellationError } from '../../upstream/src/vs/base/common/async.js';
import { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { ResourceSet } from '../../upstream/src/vs/base/common/map.js';
import { IModelService } from '../../upstream/src/vs/editor/common/services/model.js';
import { IFileService } from '../../upstream/src/vs/platform/files/common/files.js';
import { InstantiationType, registerSingleton } from '../../upstream/src/vs/platform/instantiation/common/extensions.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ILogService } from '../../upstream/src/vs/platform/log/common/log.js';
import { ITelemetryService } from '../../upstream/src/vs/platform/telemetry/common/telemetry.js';
import { IUriIdentityService } from '../../upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IExtensionService } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';
// 基类模块先完成原 singleton 登记，再登记唯一项目实现，发生于 Workbench create 之前。
import { RemoteSearchService } from '../../upstream/src/vs/workbench/services/search/browser/searchService.js';
import { DEFAULT_MAX_SEARCH_RESULTS, FileMatch, IAITextQuery, IFileQuery, ISearchComplete, ISearchProgressItem, ISearchService, ITextQuery, SearchProviderType } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import { editorMatchesToTextSearchResults, getTextSearchMatchWithModelContext } from '../../upstream/src/vs/workbench/services/search/common/searchHelpers.js';
import { WorkspaceSearchProvider } from './fileProvider.js';
import { businessQuery, fileScheme, openResources, resourceInQuery, validateQuery, validateTextQuery } from './query.js';

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
    @IUriIdentityService uriIdentityService: IUriIdentityService,
  ) {
    super(businessModels, businessEditors, telemetryService, logService, extensionService, fileService, instantiationService, uriIdentityService);
    this.businessProvider = this._register(instantiationService.createInstance(WorkspaceSearchProvider));
    this._register(this.registerSearchResultProvider(fileScheme, SearchProviderType.file, this.businessProvider));
    this._register(this.registerSearchResultProvider(fileScheme, SearchProviderType.text, this.businessProvider));
  }

  override aiTextSearch(query: IAITextQuery, token?: CancellationToken, onProgress?: (item: ISearchProgressItem) => void): Promise<ISearchComplete> {
    if (businessQuery(query)) return Promise.reject(new Error('当前业务范围未启用 AI／语义搜索'));
    return super.aiTextSearch(query, token, onProgress);
  }

  override fileSearch(query: IFileQuery, token?: CancellationToken): Promise<ISearchComplete> {
    if (businessQuery(query)) validateQuery(query);
    return super.fileSearch(query, token);
  }

  override textSearchSplitSyncAsync(query: ITextQuery, token = CancellationToken.None,
    onProgress?: (item: ISearchProgressItem) => void, notebookFilesToIgnore?: ResourceSet,
    asyncNotebookFilesToIgnore?: Promise<ResourceSet>): { syncResults: ISearchComplete; asyncResults: Promise<ISearchComplete> } {
    if (!businessQuery(query)) return super.textSearchSplitSyncAsync(query, token, onProgress, notebookFilesToIgnore, asyncNotebookFilesToIgnore);
    validateTextQuery(query);
    if (token.isCancellationRequested) throw new CancellationError();
    const open = new ResourceSet(openResources(this.businessEditors, query));
    const covered = new ResourceSet(), results: FileMatch[] = [];
    const maximum = query.maxResults ?? DEFAULT_MAX_SEARCH_RESULTS;
    let count = 0, limitHit = false;
    for (const model of this.businessModels.getModels()) {
      if (token.isCancellationRequested) throw new CancellationError();
      // 在 findMatches 和首次 progress 之前限制完整身份；零命中模型也覆盖磁盘正文。
      if (!open.has(model.uri) || !resourceInQuery(model.uri, query)) continue;
      covered.add(model.uri);
      if (notebookFilesToIgnore?.has(model.uri)) continue;
      const matches = model.findMatches(query.contentPattern.pattern, false, !!query.contentPattern.isRegExp,
        !!query.contentPattern.isCaseSensitive, query.contentPattern.isWordMatch ? query.contentPattern.wordSeparators ?? null : null,
        false, maximum - count + 1);
      if (matches.length > maximum - count) limitHit = true;
      const accepted = matches.slice(0, maximum - count);
      count += accepted.length;
      if (accepted.length) {
        const match = new FileMatch(model.uri);
        match.results = getTextSearchMatchWithModelContext(editorMatchesToTextSearchResults(accepted, model, query.previewOptions), model, query);
        results.push(match);
        if (!token.isCancellationRequested) onProgress?.(match);
      }
    }
    const syncResults: ISearchComplete = { results, messages: [], limitHit };
    const asyncResults = (async () => {
      const ignored = await raceCancellationError(asyncNotebookFilesToIgnore ?? Promise.resolve(new ResourceSet()), token);
      for (const resource of notebookFilesToIgnore ?? []) covered.add(resource);
      for (const resource of ignored) covered.add(resource);
      return this.businessProvider.textSearch({ ...query, maxResults: maximum - count }, onProgress, token, covered);
    })();
    return { syncResults, asyncResults };
  }
}

registerSingleton(ISearchService, WorkspaceSearchService, InstantiationType.Delayed);
