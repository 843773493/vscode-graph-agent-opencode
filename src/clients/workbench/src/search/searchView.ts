import { MessageType } from '../../upstream/src/vs/base/browser/ui/inputbox/inputBox.js';
import { CancellationError } from '../../upstream/src/vs/base/common/errors.js';
import type { IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { SearchView } from '../../upstream/src/vs/workbench/contrib/search/browser/searchView.js';
import type { ISearchModel } from '../../upstream/src/vs/workbench/contrib/search/browser/searchTreeModel/searchTreeCommon.js';
import type { ISearchComplete } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import { SearchWorkspaceContext, scopeFromQuery, type SearchScope } from './scope.js';

/** 保留原视图、全局结果模型和替换服务；只固定其后续查询使用的业务范围。 */
export class ScopedSearchView extends SearchView {
  private scopeGeneration = 0;
  private queryGeneration = 0;
  private pendingScope?: Promise<void>;
  private loadInitialScope!: () => Promise<SearchScope>;
  private scopeError: unknown;

  initializeSearchScope(context: SearchWorkspaceContext, load: () => Promise<SearchScope>, cancellation: IDisposable): void {
    this._register(context);
    this._register(cancellation);
    // 创建／移动视图不发起文件读取；实际查询才需要根，转交模型直接携带自己的范围。
    const query = this.searchResult.query;
    this.loadInitialScope = query ? () => Promise.resolve(scopeFromQuery(query)) : load;
  }

  private bindScope(pending: Promise<SearchScope>): Promise<void> {
    const generation = ++this.scopeGeneration;
    this.scopeError = undefined;
    return pending.then(async scope => {
      if (generation !== this.scopeGeneration) return;
      const context = this.contextService;
      if (!(context instanceof SearchWorkspaceContext)) throw new Error('搜索视图没有通过业务范围 child 创建');
      await context.updateScope(scope);
      if (generation === this.scopeGeneration) this.updateTitleDescription(scope.workspaces.map(target => target.label).join('、'));
    }).catch(error => {
      if (generation !== this.scopeGeneration) return;
      this.scopeError = error;
      this.updateTitleDescription(`搜索范围不可用：${error instanceof Error ? error.message : String(error)}`);
    });
  }

  override triggerQueryChange(options?: Parameters<SearchView['triggerQueryChange']>[0]): void {
    // 原控件同步更新期间不能排入异步查询，否则等待范围后会绕过原 pauseSearching。
    if (this.pauseSearching) return;
    const generation = ++this.queryGeneration;
    const ready = this.pendingScope ??= this.bindScope(this.loadInitialScope());
    void ready.then(() => {
      if (generation !== this.queryGeneration) return;
      if (this.scopeError) {
        this.searchAndReplaceWidget.searchInput?.showMessage({ content: String(this.scopeError), type: MessageType.ERROR });
        return;
      }
      super.triggerQueryChange(options);
    });
  }

  override async replaceSearchModel(model: ISearchModel, results: Promise<ISearchComplete>): Promise<void> {
    const generation = ++this.queryGeneration;
    this.cancelSearch(false);
    this.pendingScope = this.bindScope(Promise.resolve(scopeFromQuery(model.searchResult.query)));
    await this.pendingScope;
    if (generation !== this.queryGeneration) throw new CancellationError();
    if (this.scopeError) throw this.scopeError;
    await super.replaceSearchModel(model, results);
  }

  override dispose(): void { ++this.scopeGeneration; ++this.queryGeneration; super.dispose(); }
}
