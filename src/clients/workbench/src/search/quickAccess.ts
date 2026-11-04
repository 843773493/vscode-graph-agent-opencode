import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import type { FuzzyScorerCache } from '../../upstream/src/vs/base/common/fuzzyScorer.js';
import { DisposableStore, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { basename } from '../../upstream/src/vs/base/common/resources.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IConfigurationService } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import { IContextKeyService } from '../../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IKeybindingService } from '../../upstream/src/vs/platform/keybinding/common/keybinding.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IOpenerService } from '../../upstream/src/vs/platform/opener/common/opener.js';
import { PickerQuickAccessProvider, TriggerAction, type Pick } from '../../upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.js';
import { DefaultQuickAccessFilterValue, Extensions, IQuickAccessRegistry, type IQuickAccessProviderDescriptor } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { IQuickInputService, QuickInputHideReason, type IKeyMods, type IQuickPick } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { Registry } from '../../upstream/src/vs/platform/registry/common/platform.js';
import { PickerEditorState } from '../../upstream/src/vs/workbench/browser/quickaccess.js';
import { isEditorInput } from '../../upstream/src/vs/workbench/common/editor.js';
import type { EditorInput } from '../../upstream/src/vs/workbench/common/editor/editorInput.js';
import type { IResourceEditorInput } from '../../upstream/src/vs/platform/editor/common/editor.js';
import { extractRangeFromFilter, type IAnythingQuickAccessProviderRunOptions } from '../../upstream/src/vs/workbench/contrib/search/common/search.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { GroupDirection, IEditorGroupsService } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { IHistoryService } from '../../upstream/src/vs/workbench/services/history/common/history.js';
import { IViewsService } from '../../upstream/src/vs/workbench/services/views/common/viewsService.js';
import type { IRange } from '../../upstream/src/vs/editor/common/core/range.js';
import { ITextModelService } from '../../upstream/src/vs/editor/common/services/resolverService.js';
import { ScrollType } from '../../upstream/src/vs/editor/common/editorCommon.js';
import { GotoSymbolQuickAccessProvider } from '../../upstream/src/vs/workbench/contrib/codeEditor/browser/quickaccess/gotoSymbolQuickAccess.js';
import { getCodeEditor } from '../../upstream/src/vs/editor/browser/editorBrowser.js';
import { IQuickChatService } from '../../upstream/src/vs/workbench/contrib/chat/browser/chat.js';
import { ASK_QUICK_QUESTION_ACTION_ID } from '../../upstream/src/vs/workbench/contrib/chat/browser/actions/chatQuickInputActions.js';
import type { WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { onGatewayUserSessionInvalidated } from '../../../web/src/api/http.js';
import { gateway } from '../agentApi.js';
import { workspaceContext } from '../workspaceContext.js';
import { ThreadInput } from '../threadInput.js';
import { captureSearchOwner, type SearchInvocationOptions } from './target.js';
import { editorResourceOwner, rankResourcePicks, sameSearchOwner, type ResourcePick } from './quickAccessPicks.js';
import type { NameSearchCategory, ContentSearchCategory, SearchCategory } from './quickAccessQuery.js';
import { queryFederatedSearchBranches } from './quickAccessFederation.js';
import { chooseSearchOwners, searchOwnerKey } from './quickAccessScope.js';
import type { SessionNavigation } from '../sessionNavigation.js';
import type { ResourceOpenOptions } from '../resourceOpener.js';
import type { ThreadEditorOptions } from '../threadPane.js';
import type { TurnMessageMatchDTO, LLMRequestLogMatchDTO, TraceMatchDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';

interface AccessRun {
  picker: IQuickPick<ResourcePick, { useSeparators: true }>;
  owner?: WorkspaceOwner;
  owners: readonly WorkspaceOwner[];
  error?: string;
  group: number;
  editorState: PickerEditorState;
  range?: IRange;
  normalized?: string;
  options: AccessOptions;
  cache: FuzzyScorerCache;
  symbols?: GotoSymbolQuickAccessProvider;
  globalPick?: ResourcePick;
  restoreId?: string;
  publishing?: boolean;
  invalidated?: boolean;
}
interface AccessOptions extends IAnythingQuickAccessProviderRunOptions, SearchInvocationOptions {
  contentCategory?: ContentSearchCategory;
  searchGroup?: number;
  searchRestore?: { query: string; activeId?: string };
}

/** 原 Picker 负责交互和取消；本贡献只组织原 owner 的有界候选。 */
export class WorkspaceQuickAccess extends PickerQuickAccessProvider<ResourcePick> {
  static readonly PREFIX: string = '';
  private run?: AccessRun;
  protected get category(): NameSearchCategory | undefined { return undefined; }
  protected get contentMode(): boolean { return false; }

  constructor(
    @IInstantiationService private readonly instantiation: IInstantiationService,
    @IEditorService private readonly editors: IEditorService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService,
    @IHistoryService private readonly history: IHistoryService,
    @IConfigurationService private readonly configuration: IConfigurationService,
    @IQuickInputService private readonly quickInput: IQuickInputService,
    @IContextKeyService private readonly contexts: IContextKeyService,
    @IKeybindingService private readonly keybindings: IKeybindingService,
    @IOpenerService private readonly opener: IOpenerService,
    @INotificationService private readonly notifications: INotificationService,
    @IQuickChatService private readonly quickChat: IQuickChatService,
    @IViewsService private readonly views: IViewsService,
  ) { super(new.target.PREFIX, { canAcceptInBackground: true, shouldSkipTrimPickFilter: new.target.PREFIX === 'content ', noResultsPick: { id: 'empty', label: '没有匹配的资源' } }); }

  get defaultFilterValue() {
    return this.configuration.getValue<boolean>('workbench.quickOpen.preserveInput') ? DefaultQuickAccessFilterValue.LAST : undefined;
  }

  override provide(picker: AccessRun['picker'], token: CancellationToken, options: AccessOptions = {}) {
    const lifetime = new DisposableStore(), editorState = lifetime.add(this.instantiation.createInstance(PickerEditorState));
    const run: AccessRun = { picker, group: options.searchGroup ?? this.groups.activeGroup.id, owners: [], editorState, options, cache: {},
      restoreId: options.searchRestore?.query === picker.value ? options.searchRestore.activeId : undefined };
    // Controller 在 provide 之后才 show，不能等异步请求后再读取焦点。
    if (!options.fileSearchDelegate) {
      try {
        if (options.searchOwnerError && !options.searchOwners) throw new Error(options.searchOwnerError);
        run.owner = options.searchOwner ?? (options.searchOwners ? undefined : captureSearchOwner(this.editors));
        run.owners = options.searchOwners ?? (run.owner ? [run.owner] : []);
      }
      catch (error) { run.error = String(error); }
    }
    this.run = run;
    const access = new CancellationTokenSource(token);
    lifetime.add(toDisposable(() => access.dispose(true)));
    lifetime.add(toDisposable(() => { picker.busy = false; }));
    const invalidate = () => {
      if (run.invalidated) return;
      run.invalidated = true;
      access.cancel();
      run.globalPick = undefined;
      picker.activeItems = picker.selectedItems = [];
      picker.items = [{ id: 'identity-invalidated', label: 'Gateway 或用户身份已改变，请重新打开快速访问' }];
      picker.busy = false;
    };
    lifetime.add(toDisposable(onGatewayUserSessionInvalidated(0, invalidate)));
    const gatewayId = workspaceContext.getSnapshot().gatewayId;
    lifetime.add(toDisposable(workspaceContext.subscribe(() => {
      const current = workspaceContext.getSnapshot().gatewayId;
      if (gatewayId && current && gatewayId !== current) invalidate();
    })));
    picker.placeholder = options.placeholder ?? (this.contentMode ? `搜索正文 · ${options.contentCategory ?? '请选择类别'} · ${run.owners.map(owner => owner.workspaceId).join('、')}` : options.filesOnly ? '查找文件' : `查找${this.category ?? '会话、Thread 和文件'} · ${run.owners.map(owner => owner.workspaceId).join('、') || '请选择工作区'}`);
    if (!options.filesOnly && !options.fileSearchDelegate && !options.handleAccept && !picker.quickNavigate) {
      const button = { iconClass: ThemeIcon.asClassName(Codicon.filter), tooltip: '选择搜索范围' };
      picker.buttons = [...picker.buttons, button];
      lifetime.add(picker.onDidTriggerButton(selected => { if (selected === button) this.report(this.chooseScope(run)); }));
      lifetime.add(toDisposable(() => { picker.buttons = picker.buttons.filter(item => item !== button); }));
      if (this.contentMode) {
        const categories = { iconClass: ThemeIcon.asClassName(Codicon.listSelection), tooltip: '选择正文类别' };
        picker.buttons = [...picker.buttons, categories];
        lifetime.add(picker.onDidTriggerButton(selected => {
          if (selected === categories) {
            const query = picker.value;
            picker.hide();
            const providerOptions: AccessOptions = { ...run.options, contentCategory: undefined, searchGroup: run.group, searchOwners: run.owners };
            this.quickInput.quickAccess.show(query, { preserveValue: true, providerOptions });
          }
        }));
        lifetime.add(toDisposable(() => { picker.buttons = picker.buttons.filter(item => item !== categories); }));
      }

    }
    lifetime.add(picker.onDidHide(event => {
      if (event.reason === QuickInputHideReason.Gesture) this.report(editorState.restore());
    }));
    lifetime.add(picker.onDidChangeActive(() => {
      const control = getCodeEditor(this.editors.activeTextEditorControl), pick = picker.activeItems[0];
      if (!run.publishing && pick?.accept) run.restoreId = undefined;
      if (control) run.symbols?.clearDecorations(control);
      if (!pick?.range) { if (pick?.resource) run.globalPick = pick; return; }
      if (!control || control.getModel()?.uri.toString() !== pick.resource?.toString()) return;
      control.revealRangeInCenter(pick.range.selection, ScrollType.Smooth);
      run.symbols?.addDecorations(control, pick.range.decoration);
    }));
    lifetime.add(toDisposable(() => {
      const control = this.editors.activeTextEditorControl;
      if (control) run.symbols?.clearDecorations(control);
    }));
    lifetime.add(toDisposable(() => { if (this.run === run) this.run = undefined; }));
    run.publishing = true;
    try {
      lifetime.add(super.provide(picker, access.token, options));
      if (run.restoreId) picker.activeItems = picker.items.filter((item): item is ResourcePick => item.type !== 'separator' && item.id === run.restoreId);
    } finally { run.publishing = false; }
    return lifetime;
  }

  protected override _getPicks(original: string, disposables: DisposableStore, token: CancellationToken) {
    const run = this.run!;
    if (run.invalidated) return [{ id: 'identity-invalidated', label: 'Gateway 或用户身份已改变，请重新打开快速访问' }];
    if (run.options.searchRestore?.query !== run.picker.value) run.restoreId = undefined;
    const previousFilter = run.normalized;
    const openLink = !this.contentMode && !this.category && !run.options.filesOnly && !run.options.fileSearchDelegate
      && !run.options.handleAccept && /^[a-z][a-z0-9+.-]+:\/\//i.test(original);
    const resourceLink = openLink || /^boxteam:/i.test(original);
    if (this.contentMode && !run.options.contentCategory) return this.contentCategories(run, original);
    const parsed = this.contentMode || this.category || run.options.filesOnly || resourceLink ? undefined : extractRangeFromFilter(original, ['@']);
    run.range = parsed?.range;
    const filter = parsed?.filter ?? original;
    if (run.normalized === filter && original !== filter && !run.picker.busy) return null;
    run.normalized = filter;
    const controller = new AbortController();
    disposables.add(toDisposable(() => controller.abort()));
    disposables.add(token.onCancellationRequested(() => controller.abort()));
    const alive = () => !token.isCancellationRequested && !controller.signal.aborted && this.run === run;
    const accepted = (items: Pick<ResourcePick>[]) => run.options.filter ? items.filter(run.options.filter) : items;
    const active = run.globalPick;
    const symbolSegments = original.split('@');
    if (!this.contentMode && !this.category && !resourceLink && previousFilter && !run.options.filesOnly && active?.resource?.scheme === 'boxteam-file' && symbolSegments.length > 1
      && (!(active.label.includes('@') || active.description?.includes('@')) || symbolSegments.length > 2)) {
      return this.symbolPicks(run, active, symbolSegments.at(-1)!.trim(), disposables, token).then(accepted)
        .catch(error => token.isCancellationRequested ? [] : [this.failure('文件符号', error)]);
    }
    const extras = (run.options.additionPicks ?? []).flatMap((pick, index): Pick<ResourcePick>[] => pick.type === 'separator'
      ? [pick] : rankResourcePicks([{ ...pick, id: `addition-${index}` }], filter, run.cache));
    const recent = this.contentMode ? [] : this.recent(run, filter);
    const fast = accepted([...extras, ...(!filter && run.options.includeHelp ? this.help(run) : []),
      ...(recent.length ? [{ type: 'separator' as const, label: '最近打开' }, ...recent] : [])]);
    if (run.picker.quickNavigate || !filter && !run.options.filesOnly && !run.options.fileSearchDelegate && !this.contentMode) return fast;
    if (openLink) {
      return accepted([{ id: 'link:' + filter, label: '打开链接', description: filter,
        accept: (mods, event) => {
          this.assertAccess(run);
          const options: ResourceOpenOptions = { fromUserGesture: true, allowContributedOpeners: true,
            boxteamTargetGroup: run.group, openToSide: mods.alt || this.openPinned() && mods.ctrlCmd,
            editorOptions: { pinned: mods.ctrlCmd || event.inBackground || this.openPinned(), preserveFocus: event.inBackground } };
          this.report(this.opener.open(filter, options));
        } }]);
    }
    if (run.options.fileSearchDelegate) {
      run.picker.busy = true;
      void run.options.fileSearchDelegate.search(filter, token).then(result => {
        if (!alive()) return;
        const picks = result.results.map(item => this.resourcePick(run, item.resource,
          run.options.fileSearchDelegate?.getItemDescription?.(item.resource)));
        this.publish(run, accepted([...fast, ...rankResourcePicks(picks, filter, run.cache)]));
      }).catch(error => { if (alive()) this.publish(run, [...fast, this.failure('文件', error)]); })
        .finally(() => { if (alive()) run.picker.busy = false; });
      return fast;
    }
    if (this.contentMode && !filter) return [{ id: 'content-query-empty', label: '输入要搜索的正文（区分大小写的字面量）' }];
    if (!run.owners.length) return [...fast, { id: 'scope-error', label: run.error ?? '请先选择明确工作区' }];
    if (!this.contentMode && !this.category && this.configuration.getValue<boolean>('search.quickOpen.includeSymbols')) fast.push({ id: 'workspace-symbols-unavailable', label: '工作区符号未启用：当前没有可限定业务根的符号服务' });

    const categories: SearchCategory[] = this.contentMode ? [run.options.contentCategory!] : this.category ? [this.category]
      : run.options.filesOnly || run.options.handleAccept ? ['文件'] : ['会话', 'Thread', '文件'];
    const seen = new Set(recent.map(pick => pick.id));
    const branches = queryFederatedSearchBranches(run.owners, categories, filter, disposables, token, run.cache,
      (item, owner) => item.kind === 'resource' ? this.resourcePick(run, item.resource, item.description, item.title, item.detail, undefined, item.message, item.trace)
        : item.kind === 'session' ? this.sessionPick(run, owner, item.sessionId, item.title, item.description, item.detail, item.requestLog)
          : this.folderPick(run, owner, item.nodeId, item.title, item.description, item.detail),
      branches => {
        if (!alive()) return;
        const items = [...fast];
        for (const category of categories) {
          items.push({ type: 'separator', label: category });
          for (const branch of branches.filter(branch => branch.category === category)) {
            items.push(...branch.picks.filter(pick => !seen.has(pick.id)), branch.status);
          }
        }
        this.publish(run, accepted(items));
      }, value => { if (alive()) run.picker.busy = value; },
      label => { if (alive()) run.picker.placeholder = `${this.contentMode ? '搜索正文 · ' + run.options.contentCategory : '查找' + (this.category ?? '会话、Thread 和文件')} · ${label}`; }, this.instantiation);
    return [...fast, ...branches.map(branch => branch.status)];
  }

  private contentCategories(run: AccessRun, filter: string): ResourcePick[] {
    const select = (prefix: string, category?: ContentSearchCategory) => {
      this.assertAccess(run);
      run.picker.hide();
      const providerOptions: AccessOptions = { ...run.options, contentCategory: category, searchOwners: run.owners, searchGroup: run.group };
      this.quickInput.quickAccess.show(prefix + filter, { preserveValue: true, providerOptions });
    };
    // TODO：工作区运行日志及 Gateway 自有诊断日志仍待各原 owner 的独立查询与权限切片。
    return [
      { id: 'content-category-message', label: '会话消息', description: '用户输入和助手正文 · main / child', accept: () => select('content ', '会话消息') },
      { id: 'content-category-tool', label: '工具结果', description: 'canonical 结果及不可变完整输出 · 不重放工具', accept: () => select('content ', '工具结果') },
      { id: 'content-category-file', label: '文件', description: '转到 % 文件内容搜索，保留所选工作区', accept: () => select('%') },
      { id: 'content-category-request', label: 'LLM 请求记录', description: 'Session 级原请求／响应 · 精确记录定位', accept: () => select('content ', 'LLM 请求记录') },
      { id: 'content-category-trace', label: 'Trace', description: '工作区原事件 · 精确 Thread／记录定位', accept: () => select('content ', 'Trace') },
      { id: 'content-category-pending-logs', label: '运行日志 · 尚未接入' },
    ];
  }

  private failure(category: string, error: unknown): ResourcePick {
    return { id: `error-${category}`, label: `${category}查询失败：${error instanceof Error ? error.message : String(error)}` };
  }

  private async symbolPicks(run: AccessRun, active: ResourcePick, filter: string, disposables: DisposableStore, token: CancellationToken): Promise<Pick<ResourcePick>[]> {
    run.editorState.set();
    await run.editorState.openTransientEditor({ resource: active.resource!, options: { preserveFocus: true, revealIfOpened: true } }, run.group);
    if (token.isCancellationRequested) return [];
    const reference = disposables.add(await this.instantiation.invokeFunction(accessor => accessor.get(ITextModelService).createModelReference(active.resource!)));
    if (token.isCancellationRequested) return [];
    run.symbols ??= this.instantiation.createInstance(GotoSymbolQuickAccessProvider);
    const picks = await run.symbols.getSymbolPicks(reference.object.textEditorModel, filter, { extraContainerLabel: active.label }, disposables, token);
    if (token.isCancellationRequested) return [];
    return picks.map(pick => pick.type === 'separator' ? pick : {
      ...pick, attach: undefined, id: `${active.id}:symbol:${pick.index}`, resource: active.resource,
      trigger: (_index, mods) => { this.report(this.open(run, { resource: active.resource! }, mods, false, true, undefined, pick.range?.selection)); return TriggerAction.CLOSE_PICKER; },
      accept: (mods, event) => this.report(this.open(run, { resource: active.resource! }, mods, event.inBackground, false, undefined, pick.range?.selection)),
    });
  }

  private publish(run: AccessRun, items: Pick<ResourcePick>[]): void {
    const active = run.picker.activeItems[0], keep = run.picker.keepScrollPosition;
    const targets = items.filter((item): item is ResourcePick => item.type !== 'separator' && !!item.accept);
    run.publishing = true;
    try {
      run.picker.keepScrollPosition = true;
      run.picker.items = items;
      // 恢复按身份等待原目标；目标消失时清空确认，不能改投新占行对象。
      const activeId = run.restoreId ?? (active?.accept ? active.id : undefined);
      run.picker.activeItems = activeId ? targets.filter(item => item.id === activeId) : targets.slice(0, 1);
      if (activeId && !run.picker.activeItems.length) run.restoreId = activeId;
      if (run.picker.activeItems.some(item => item.id === run.restoreId)) run.restoreId = undefined;
    } finally { run.picker.keepScrollPosition = keep; run.publishing = false; }
  }

  private recent(run: AccessRun, filter: string): ResourcePick[] {
    if (this.category === '会话' || run.options.filesOnly || this.configuration.getValue<boolean>('search.quickOpen.includeHistory') === false) return [];
    const picks = new Map<string, ResourcePick>();
    for (const editor of this.history.getHistory()) {
      const resource = editor.resource;
      const owner = resource && editorResourceOwner(resource);
      if (!resource || !owner || !run.owners.some(target => sameSearchOwner(owner, target))) continue;
      if (this.category === 'Thread' && resource.scheme !== 'boxteam-thread') continue;
      if (run.options.handleAccept && resource.scheme !== 'boxteam-file') continue;
      const description = resource.scheme === 'boxteam-file' ? `${owner.workspaceId} · ${resource.path.split('/').slice(3, -1).join('/')}`
        : isEditorInput(editor) ? editor.getDescription() : editor.description;
      const pick = this.resourcePick(run, resource, description,
        isEditorInput(editor) ? editor.getName() : editor.label, undefined, editor);
      if (!picks.has(pick.id)) picks.set(pick.id, pick);
    }
    return rankResourcePicks([...picks.values()], filter, run.cache,
      this.configuration.getValue<string>('search.quickOpen.history.filterSortOrder') === 'recency');
  }

  private resourcePick(run: AccessRun, resource: URI, description?: string, title = basename(resource), detail?: string,
    editor?: EditorInput | IResourceEditorInput, message?: TurnMessageMatchDTO, trace?: TraceMatchDTO): ResourcePick {
    const side = run.options.includeOpenToSideButton !== false;
    const dirty = editor && isEditorInput(editor) && editor.isDirty() && !editor.isSaving();
    const input = editor ?? { resource, label: title, options: message ? { boxteamMessage: message } as ThreadEditorOptions : trace ? { boxteamTrace: trace } as ThreadEditorOptions : undefined };
    return { id: message ? `${resource.toString()}#${message.reference!.item_id}/${message.reference!.start_codepoint}`
      : trace ? `${resource.toString()}#trace/${trace.reference!.event_id}/${trace.reference!.start_codepoint}` : resource.toString(), resource, editor, label: title, description, detail,
      ariaLabel: `${title} ${description ?? ''}${dirty ? '，未保存' : ''}`,
      iconClass: ThemeIcon.asClassName(resource.scheme === 'boxteam-thread' ? Codicon.commentDiscussion : Codicon.file),
      buttons: [...(side ? [{ iconClass: ThemeIcon.asClassName(Codicon.splitHorizontal), tooltip: '在侧边打开' }] : []),
        ...(editor ? [{ iconClass: ThemeIcon.asClassName(dirty ? Codicon.circleFilled : Codicon.close), tooltip: '从最近打开中移除', alwaysVisible: !!dirty }] : [])],
      trigger: (index, mods) => {
        if (side && index === 0) { this.report(this.open(run, input, mods, false, true)); return TriggerAction.CLOSE_PICKER; }
        if (editor) { this.history.removeFromHistory(editor); return TriggerAction.REMOVE_ITEM; }
        return TriggerAction.NO_ACTION;
      },
      accept: (mods, event) => this.report(this.open(run, input, mods, event.inBackground)),
    };
  }

  private sessionPick(run: AccessRun, owner: WorkspaceOwner, sessionId: string, title: string, description: string, detail: string, requestLog?: LLMRequestLogMatchDTO): ResourcePick {
    const open = async (mods: IKeyMods, background: boolean, side = false) => {
      this.assertAccess(run);
      const group = this.targetGroup(run, mods, side);
      const routeId = await gateway.resolveWorkspace(owner);
      const main = await gateway.mainThread(routeId, sessionId);
      await this.open(run, new ThreadInput({ ...owner, routeId, sessionId, threadId: main.thread_id, title }), mods, background, false, group, undefined, requestLog);
    };
    return { id: requestLog ? `request:${searchOwnerKey(owner)}/${sessionId}/${requestLog.reference!.file_name}/${requestLog.reference!.start_codepoint}` : `session:${searchOwnerKey(owner)}/${sessionId}`, label: title, description, detail,
      iconClass: ThemeIcon.asClassName(Codicon.commentDiscussion),
      buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.splitHorizontal), tooltip: '在侧边打开' }],
      trigger: (_index, mods) => { this.report(open(mods, false, true)); return TriggerAction.CLOSE_PICKER; },
      accept: (mods, event) => this.report(open(mods, event.inBackground)) };
  }

  private folderPick(run: AccessRun, owner: WorkspaceOwner, nodeId: string, label: string, description: string, detail: string): ResourcePick {
    return { id: `folder:${searchOwnerKey(owner)}/${nodeId}`, label, description, detail,
      iconClass: ThemeIcon.asClassName(Codicon.folder),
      accept: (_mods, event) => this.report((async () => {
        this.assertAccess(run);
        const target = await gateway.resolveWorkspaceTarget(owner);
        this.assertAccess(run);
        const view = await this.views.openView<SessionNavigation>('boxteam.sessions', !event.inBackground);
        if (!view) throw new Error('会话导航视图不可用');
        await view.revealFolder(target, nodeId, !event.inBackground);
      })()) };
  }

  private async chooseScope(run: AccessRun): Promise<void> {
    const query = run.picker.value, activeId = run.picker.activeItems[0]?.id;
    let selected: WorkspaceOwner[] | undefined;
    try { selected = await chooseSearchOwners(this.quickInput, run.owners); }
    finally {
      // 新的独立 Quick Input 已经打开时，不抢回焦点或改写其查询。
      if (!this.quickInput.currentQuickInput) {
        const providerOptions: AccessOptions = { ...run.options, searchGroup: run.group, searchOwners: selected ?? run.owners,
          searchRestore: selected ? undefined : { query, activeId } };
        this.quickInput.quickAccess.show(query, { preserveValue: true, providerOptions });
      }
    }
  }

  private targetGroup(run: AccessRun, mods: IKeyMods, side = false): number {
    const group = this.groups.getGroup(run.group);
    if (!group) throw new Error('发起快速访问的编辑器组已关闭');
    const pinned = this.openPinned();
    if (!(side || mods.alt || pinned && mods.ctrlCmd)) return group.id;
    const direction = this.configuration.getValue<string>('workbench.editor.openSideBySideDirection') === 'down' ? GroupDirection.DOWN : GroupDirection.RIGHT;
    return (this.groups.findGroup({ direction }, group) ?? this.groups.addGroup(group, direction)).id;
  }

  private openPinned(): boolean {
    return this.configuration.getValue<boolean>('workbench.editor.enablePreviewFromQuickOpen') === false
      || this.configuration.getValue<boolean>('workbench.editor.enablePreview') === false;
  }

  private assertAccess(run: AccessRun): void {
    if (run.invalidated) throw new Error('Gateway 或用户身份已改变，请重新选择目标');
  }

  private async open(run: AccessRun, editor: EditorInput | IResourceEditorInput, mods: IKeyMods, background: boolean, side = false, group = this.targetGroup(run, mods, side), selection?: IRange, requestLog?: LLMRequestLogMatchDTO): Promise<void> {
    this.assertAccess(run);
    const options = { pinned: mods.ctrlCmd || background || this.openPinned(), preserveFocus: background,
      selection: editor.resource?.scheme === 'boxteam-thread' ? undefined : selection ?? run.range };
    if (isEditorInput(editor)) await this.editors.openEditor(editor, requestLog ? { ...options, boxteamRequestLog: requestLog } as ThreadEditorOptions : options, group);
    else await this.editors.openEditor({ ...editor, options: { ...editor.options, ...options } }, group);
  }

  private help(run: AccessRun): ResourcePick[] {
    const picks = Registry.as<IQuickAccessRegistry>(Extensions.Quickaccess).getQuickAccessProviders(this.contexts)
      .flatMap(provider => provider.helpEntries.filter(entry => entry.commandCenterOrder !== undefined).map(entry => ({ provider, entry })))
      .sort((left, right) => left.entry.commandCenterOrder! - right.entry.commandCenterOrder!)
      .map(({ provider, entry }): ResourcePick & { order: number } => ({ id: 'help:' + provider.prefix, label: entry.commandCenterLabel ?? entry.description,
        order: entry.commandCenterOrder!,
        description: entry.prefix ?? provider.prefix, ariaLabel: `${entry.commandCenterLabel ?? entry.description}, ${entry.description}`,
        keybinding: entry.commandId ? this.keybindings.lookupKeybinding(entry.commandId) : undefined,
        accept: () => {
          const providerOptions: AccessOptions = { ...run.options, searchOwner: run.owner,
            includeHelp: provider.prefix ? run.options.includeHelp : false };
          this.quickInput.quickAccess.show(provider.prefix, { preserveValue: true, providerOptions });
        } }));
    if (this.quickChat.enabled) picks.push({ id: 'quick-chat', label: '打开快速聊天', order: 30,
      keybinding: this.keybindings.lookupKeybinding(ASK_QUICK_QUESTION_ACTION_ID), accept: () => this.quickChat.toggle() });
    return picks.sort((left, right) => left.order - right.order);
  }

  private report(operation: Promise<unknown>): void { void operation.catch(error => this.notifications.error(error)); }
}

export function selectDefaultSearchDescriptor(descriptor: IQuickAccessProviderDescriptor): IQuickAccessProviderDescriptor {
  return { ...descriptor, ctor: WorkspaceQuickAccess, placeholder: '查找会话、Thread 和文件，或粘贴链接' };
}
