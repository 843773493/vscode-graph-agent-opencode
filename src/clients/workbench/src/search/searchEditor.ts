import { Button } from '../../upstream/src/vs/base/browser/ui/button/button.js';
import { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { isCancellationError } from '../../upstream/src/vs/base/common/errors.js';
import type { IMarkdownString } from '../../upstream/src/vs/base/common/htmlContent.js';
import { ServiceCollection } from '../../upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { defaultButtonStyles } from '../../upstream/src/vs/platform/theme/browser/defaultStyles.js';
import { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import type { IEditorOptions } from '../../upstream/src/vs/platform/editor/common/editor.js';
import { createEditorOpenError, type IEditorOpenContext } from '../../upstream/src/vs/workbench/common/editor.js';
import { SearchEditor } from '../../upstream/src/vs/workbench/contrib/searchEditor/browser/searchEditor.js';
import { SearchEditorInput } from '../../upstream/src/vs/workbench/contrib/searchEditor/browser/searchEditorInput.js';
import type { SearchConfiguration } from '../../upstream/src/vs/workbench/contrib/searchEditor/browser/constants.js';
import { QueryBuilder, type ITextQueryBuilderOptions } from '../../upstream/src/vs/workbench/services/search/common/queryBuilder.js';
import type { IPatternInfo, ITextQuery } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { gatewayWorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { gateway } from '../agentApi.js';
import { workspaceContext } from '../workspaceContext.js';
import { parseSearchEditorScope, searchEditorScopeKey, type SearchEditorScope } from './editorScope.js';
import { SearchWorkspaceContext, type SearchScope } from './scope.js';
import { captureSearchOwner, loadSearchScope } from './target.js';

/** 发起时捕获，恢复／factory 入口不得调用它来猜历史范围。 */
export function captureNewSearchEditorScope(editors: IEditorService, supplied?: SearchEditorScope): Promise<SearchEditorScope> {
  const explicit = parseSearchEditorScope(supplied);
  if (explicit) return Promise.resolve(explicit);
  if (editors.activeEditor instanceof SearchEditorInput) {
    const existing = parseSearchEditorScope(editors.activeEditor.tryReadConfigSync()?.scope);
    if (existing) return Promise.resolve(existing);
  }
  const owner = captureSearchOwner(editors);
  return loadSearchScope([owner], new AbortController().signal).then(scope => ({ ...scope, kind: 'workspace' }));
}

/** 原 Pane／Input／模型不变；每个 Pane 的 QueryBuilder 只观察这个输入的范围。 */
export class ScopedSearchEditor extends SearchEditor {
  private readonly searchContext = this._register(new SearchWorkspaceContext());
  private readonly scopedInstantiation = this._register(this.instantiationService.createChild(
    new ServiceCollection([IWorkspaceContextService, this.searchContext])));
  private scopeButton?: Button;
  private scopeGeneration = 0;
  private inputGeneration = 0;
  private scopeKey: string | undefined;
  private pendingScope = Promise.resolve();
  private scopeFailure: unknown;
  private scopeRequest?: AbortController;

  protected override createEditor(parent: HTMLElement): void {
    super.createEditor(parent);
    this.scopeButton = this._register(new Button(this.queryEditorContainer, { ...defaultButtonStyles, secondary: true }));
    this.scopeButton.element.classList.add('boxteam-search-scope');
    this.scopeButton.label = '历史结果 · 请选择搜索范围';
    this._register(this.scopeButton.onDidClick(() => { void this.chooseScope().catch(error => this.reportFailure(error)); }));
  }

  private reportFailure(error: unknown): void {
    if (!isCancellationError(error)) this.instantiationService.invokeFunction(accessor => accessor.get(INotificationService).error(error instanceof Error ? error : String(error)));
  }

  protected override getReadonlyConfiguration(isReadonly: boolean | IMarkdownString | undefined) {
    return super.getReadonlyConfiguration(!this.scopeKey || this.scopeFailure !== undefined ? true : isReadonly);
  }

  private bindScope(scope: SearchEditorScope | undefined): Promise<void> {
    const key = searchEditorScopeKey(scope);
    if (key === this.scopeKey) return this.pendingScope;
    this.cancelInputSearch();
    const generation = ++this.scopeGeneration;
    this.scopeRequest?.abort();
    const controller = this.scopeRequest = new AbortController();
    this.scopeKey = key;
    this.scopeFailure = new Error('正在核验搜索范围');
    if (this.input) this.updateReadonly(this.input);
    if (this.scopeButton) this.scopeButton.label = '正在核验搜索范围…';
    this.pendingScope = (async () => {
      if (!scope) throw new Error('历史结果没有明确范围，请先选择搜索工作区');
      let resolved = scope;
      if (scope.kind === 'workspace') {
        // 只重解已登记路由；不启动、连接目标，也不使用现在的配置推测旧根。
        const workspaces = await Promise.all(scope.workspaces.map(async target => {
          const current = await gateway.resolveWorkspaceTarget(target, controller.signal);
          if (current.status !== 'ready') throw new Error(`工作区 ${current.name} 当前不可查询（${current.status}）`);
          return { ...target, label: current.name };
        }));
        if (generation !== this.scopeGeneration) return;
        resolved = { ...scope, workspaces };
      }
      await this.searchContext.updateScope(resolved.kind === 'workspace' ? resolved : { version: 1, workspaces: [] });
      if (generation !== this.scopeGeneration) return;
      this.scopeFailure = undefined;
      if (this.input) this.updateReadonly(this.input);
      if (this.scopeButton) this.scopeButton.label = resolved.kind === 'native' ? '搜索范围 · 原生虚拟工作区'
        : `搜索范围 · ${resolved.workspaces.map(target => target.label).join('、')}`;
    })().catch(error => {
      if (generation !== this.scopeGeneration) return;
      controller.abort();
      this.scopeFailure = error;
      if (this.input) this.updateReadonly(this.input);
      if (this.scopeButton) this.scopeButton.label = `历史结果 · ${error instanceof Error ? error.message : String(error)}`;
    });
    return this.pendingScope;
  }

  override async setInput(input: SearchEditorInput, options: IEditorOptions | undefined, context: IEditorOpenContext, token: CancellationToken): Promise<void> {
    const generation = ++this.inputGeneration;
    this.cancelInputSearch();
    this.invalidateScope();
    try {
      const { configurationModel } = await input.resolveModels();
      if (token.isCancellationRequested || generation !== this.inputGeneration) return;
      await this.bindScope(parseSearchEditorScope(configurationModel.config.scope));
      if (token.isCancellationRequested || generation !== this.inputGeneration) return;
      await super.setInput(input, options, context, token);
    } catch (error) {
      if (token.isCancellationRequested || generation !== this.inputGeneration) return;
      throw createEditorOpenError(error instanceof Error ? error : String(error), [], { forceMessage: true });
    }
  }

  override setSearchConfig(config: Partial<Readonly<SearchConfiguration>>): void {
    const paused = this.pauseSearching;
    this.pauseSearching = true;
    try {
      super.setSearchConfig(config);
      void this.bindScope(parseSearchEditorScope(this.input instanceof SearchEditorInput ? this.input.tryReadConfigSync()?.scope : undefined));
    } finally { this.pauseSearching = paused; }
  }

  override async triggerSearch(options?: Parameters<SearchEditor['triggerSearch']>[0]): Promise<void> {
    if (this.pauseSearching) return;
    const input = this.input, generation = this.scopeGeneration;
    await this.pendingScope;
    if (input !== this.input || generation !== this.scopeGeneration) return;
    try {
      if (this.scopeFailure) throw this.scopeFailure;
      await super.triggerSearch(options);
    } catch (error) { this.reportFailure(error); }
  }

  protected override buildTextQuery(content: IPatternInfo, options: ITextQueryBuilderOptions, config: SearchConfiguration): ITextQuery {
    const scope = parseSearchEditorScope(config.scope);
    if (!scope || this.scopeFailure) throw this.scopeFailure ?? new Error('请先选择明确搜索范围');
    return scope.kind === 'native' ? super.buildTextQuery(content, options, config)
      : this.scopedInstantiation.createInstance(QueryBuilder).text(content, this.searchContext.getWorkspace().folders.map(folder => folder.uri), options);
  }

  private async chooseScope(): Promise<void> {
    const input = this.input, generation = this.inputGeneration;
    const snapshot = workspaceContext.getSnapshot();
    if (snapshot.loading || snapshot.error || !snapshot.gatewayId) throw new Error(snapshot.error ?? '工作区登记信息尚未加载');
    const current = input instanceof SearchEditorInput ? parseSearchEditorScope(input.tryReadConfigSync()?.scope) : undefined;
    const picks = snapshot.workspaces.map(target => {
      const owner = gatewayWorkspaceOwner(target);
      return { label: target.name, description: target.status, target: owner, picked: current?.kind === 'workspace'
        && current.workspaces.some(item => item.workspaceId === owner.workspaceId && item.gatewayId === owner.gatewayId) };
    });
    const selected = await this.instantiationService.invokeFunction(accessor => accessor.get(IQuickInputService).pick(picks,
      { canPickMany: true, title: '选择搜索工作区', placeHolder: '仅查询选定的已登记工作区；不会自动启动或连接' }));
    if (!selected?.length || !(input instanceof SearchEditorInput) || this.input !== input || generation !== this.inputGeneration) return;
    this.cancelInputSearch();
    const scopeGeneration = ++this.scopeGeneration;
    this.scopeRequest?.abort();
    const controller = this.scopeRequest = new AbortController();
    this.scopeKey = undefined;
    this.scopeFailure = new Error('正在核验新的搜索范围');
    this.updateReadonly(input);
    if (this.scopeButton) this.scopeButton.label = '正在核验新的搜索范围…';
    const data = await input.resolveModels();
    if (this.input !== input || generation !== this.inputGeneration || scopeGeneration !== this.scopeGeneration) return;
    data.resultsModel.setValue('');
    input.setMatchRanges([]);
    input.setDirty(true);
    let selectedScope: SearchScope;
    try {
      selectedScope = await loadSearchScope(selected.map(item => item.target), controller.signal);
    } catch (error) {
      controller.abort();
      if (this.input !== input || generation !== this.inputGeneration || scopeGeneration !== this.scopeGeneration) return;
      this.scopeFailure = error;
      if (this.scopeButton) this.scopeButton.label = `搜索范围不可用 · ${error instanceof Error ? error.message : String(error)}`;
      throw error;
    }
    if (this.input !== input || generation !== this.inputGeneration || scopeGeneration !== this.scopeGeneration) return;
    const scope: SearchEditorScope = { ...selectedScope, kind: 'workspace' };
    this.setSearchConfig({ scope });
    await this.pendingScope;
    if (this.scopeFailure) throw this.scopeFailure;
    await this.triggerSearch();
  }

  private invalidateScope(): void {
    ++this.scopeGeneration;
    this.scopeRequest?.abort();
    this.scopeKey = undefined;
    this.scopeFailure = new Error('搜索范围尚未绑定');
    this.pendingScope = this.searchContext.updateScope({ version: 1, workspaces: [] });
  }

  override clearInput(): void { ++this.inputGeneration; this.invalidateScope(); super.clearInput(); }
  override dispose(): void { ++this.inputGeneration; this.cancelInputSearch(); this.invalidateScope(); super.dispose(); }
}
