import { Limiter, raceCancellation, timeout } from '../../upstream/src/vs/base/common/async.js';
import { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
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
import { ISearchService } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import type { IRange } from '../../upstream/src/vs/editor/common/core/range.js';
import { ITextModelService } from '../../upstream/src/vs/editor/common/services/resolverService.js';
import { ScrollType } from '../../upstream/src/vs/editor/common/editorCommon.js';
import { GotoSymbolQuickAccessProvider } from '../../upstream/src/vs/workbench/contrib/codeEditor/browser/quickaccess/gotoSymbolQuickAccess.js';
import { getCodeEditor } from '../../upstream/src/vs/editor/browser/editorBrowser.js';
import { IQuickChatService } from '../../upstream/src/vs/workbench/contrib/chat/browser/chat.js';
import { ASK_QUICK_QUESTION_ACTION_ID } from '../../upstream/src/vs/workbench/contrib/chat/browser/actions/chatQuickInputActions.js';
import { requireGatewayWorkspaceRoute, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { gateway } from '../agentApi.js';
import { ThreadInput } from '../threadInput.js';
import { captureSearchOwner, type SearchInvocationOptions } from './target.js';
import { editorResourceOwner, rankResourcePicks, sameSearchOwner, type ResourcePick } from './quickAccessPicks.js';
import { searchResourceNames, type NameSearchCategory, type NameSearchPage } from './quickAccessQuery.js';
import { chooseSearchOwners, searchOwnerKey } from './quickAccessScope.js';

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
}
interface AccessOptions extends IAnythingQuickAccessProviderRunOptions, SearchInvocationOptions {
  searchGroup?: number;
  searchRestore?: { query: string; activeId?: string };
}

/** 原 Picker 负责交互和取消；本贡献只组织原 owner 的有界候选。 */
export class WorkspaceQuickAccess extends PickerQuickAccessProvider<ResourcePick> {
  private run?: AccessRun;

  constructor(
    @IInstantiationService private readonly instantiation: IInstantiationService,
    @IEditorService private readonly editors: IEditorService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService,
    @IHistoryService private readonly history: IHistoryService,
    @IConfigurationService private readonly configuration: IConfigurationService,
    @ISearchService private readonly search: ISearchService,
    @IQuickInputService private readonly quickInput: IQuickInputService,
    @IContextKeyService private readonly contexts: IContextKeyService,
    @IKeybindingService private readonly keybindings: IKeybindingService,
    @IOpenerService private readonly opener: IOpenerService,
    @INotificationService private readonly notifications: INotificationService,
    @IQuickChatService private readonly quickChat: IQuickChatService,
  ) { super('', { canAcceptInBackground: true, noResultsPick: { id: 'empty', label: '没有匹配的资源' } }); }

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
    picker.placeholder = options.placeholder ?? (options.filesOnly ? '查找文件' : `查找会话、Thread 和文件 · ${run.owners.map(owner => owner.workspaceId).join('、') || '请选择工作区'}`);
    if (!options.filesOnly && !options.fileSearchDelegate && !options.handleAccept && !picker.quickNavigate) {
      const button = { iconClass: ThemeIcon.asClassName(Codicon.filter), tooltip: '选择搜索范围' };
      picker.buttons = [...picker.buttons, button];
      lifetime.add(picker.onDidTriggerButton(selected => { if (selected === button) this.report(this.chooseScope(run)); }));
      lifetime.add(toDisposable(() => { picker.buttons = picker.buttons.filter(item => item !== button); }));
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
      lifetime.add(super.provide(picker, token, options));
      if (run.restoreId) picker.activeItems = picker.items.filter((item): item is ResourcePick => item.type !== 'separator' && item.id === run.restoreId);
    } finally { run.publishing = false; }
    return lifetime;
  }

  protected override _getPicks(original: string, disposables: DisposableStore, token: CancellationToken) {
    const run = this.run!;
    if (run.options.searchRestore?.query !== run.picker.value) run.restoreId = undefined;
    const previousFilter = run.normalized;
    const parsed = run.options.filesOnly ? undefined : extractRangeFromFilter(original, ['@']);
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
    if (previousFilter && !run.options.filesOnly && active?.resource?.scheme === 'boxteam-file' && symbolSegments.length > 1
      && (!(active.label.includes('@') || active.description?.includes('@')) || symbolSegments.length > 2)) {
      return this.symbolPicks(run, active, symbolSegments.at(-1)!.trim(), disposables, token).then(accepted)
        .catch(error => token.isCancellationRequested ? [] : [this.failure('文件符号', error)]);
    }
    const extras = (run.options.additionPicks ?? []).flatMap((pick, index): Pick<ResourcePick>[] => pick.type === 'separator'
      ? [pick] : rankResourcePicks([{ ...pick, id: `addition-${index}` }], filter, run.cache));
    const recent = this.recent(run, filter);
    const fast = accepted([...extras, ...(!filter && run.options.includeHelp ? this.help(run) : []),
      ...(recent.length ? [{ type: 'separator' as const, label: '最近打开' }, ...recent] : [])]);
    if (run.picker.quickNavigate || !filter && !run.options.filesOnly && !run.options.fileSearchDelegate) return fast;
    if (!run.options.filesOnly && !run.options.fileSearchDelegate && !run.options.handleAccept && /^[a-z][a-z0-9+.-]+:\/\//i.test(filter)) {
      return accepted([{ id: 'link:' + filter, label: '打开链接', description: filter,
        accept: mods => this.report(this.opener.open(filter, { fromUserGesture: true, openToSide: mods.alt,
          editorOptions: { pinned: mods.ctrlCmd }, allowContributedOpeners: true })) }]);
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
    if (!run.owners.length) return [...fast, { id: 'scope-error', label: run.error ?? '请先选择明确工作区' }];
    if (this.configuration.getValue<boolean>('search.quickOpen.includeSymbols')) fast.push({ id: 'workspace-symbols-unavailable', label: '工作区符号未启用：当前没有可限定业务根的符号服务' });

    const categories: NameSearchCategory[] = run.options.filesOnly || run.options.handleAccept ? ['文件'] : ['会话', 'Thread', '文件'];
    const branches = categories.flatMap(category => run.owners.map(owner => ({ owner, category,
      key: `${searchOwnerKey(owner)}/${category}`, picks: [] as ResourcePick[],
      label: run.owners.length === 1 ? category : `${owner.workspaceId} · ${category}`,
      status: { id: `status-${searchOwnerKey(owner)}/${category}`, label: `${owner.workspaceId} · ${category} · 正在查询` } as ResourcePick })));
    const seen = new Set(recent.map(pick => pick.id));
    const render = () => {
      if (!alive()) return;
      const items = [...fast];
      for (const category of categories) {
        items.push({ type: 'separator', label: category });
        for (const branch of branches.filter(branch => branch.category === category)) {
          items.push(...branch.picks.filter(pick => !seen.has(pick.id)), branch.status);
        }
      }
      this.publish(run, accepted(items));
    };
    run.picker.busy = true;
    let routes: ReturnType<typeof gateway.workspaceRoutes> | undefined;
    const labels = new Map(run.owners.map(owner => [searchOwnerKey(owner), owner.workspaceId]));
    const resolveTarget = async (owner: WorkspaceOwner) => {
      const snapshot = await (routes ??= timeout(200, token).then(() => gateway.workspaceRoutes(controller.signal)));
      const target = requireGatewayWorkspaceRoute(snapshot.items, snapshot.gatewayId, owner);
      if (target.status !== 'ready') throw new Error(`工作区 ${target.name} 当前不可查询（${target.status}），请明确启动或连接`);
      labels.set(searchOwnerKey(owner), target.name);
      if (alive()) run.picker.placeholder = `查找会话、Thread 和文件 · ${[...labels.values()].join('、')}`;
      return target;
    };
    const limiter = disposables.add(new Limiter<void>(3));
    disposables.add(limiter.onDrained(() => { if (alive()) run.picker.busy = false; }));
    const queue = async (branch: typeof branches[number], cursor?: string) => {
      if (!alive()) return;
      run.picker.busy = true;
      // 释放 Limiter 会丢弃排队任务；等待者仍随原 Picker token 及时结算。
      await raceCancellation(limiter.queue(() => load(branch, cursor)), token);
    };
    const load = async (branch: typeof branches[number], cursor?: string) => {
      if (!alive()) return;
      branch.status = { id: `status-${branch.key}`, label: `${branch.label} · 正在查询` }; render();
      try {
        const resolved = await resolveTarget(branch.owner);
        if (!alive()) return;
        branch.label = run.owners.length === 1 ? branch.category : `${resolved.name} · ${branch.category}`;
        const page = await searchResourceNames(branch.category, filter, branch.owner, resolved, resolved.name,
          controller.signal, token, this.instantiation, this.search, cursor);
        if (!alive()) return;
        const picks = page.items.map(item => item.resource ? this.resourcePick(run, item.resource, item.description, item.title, item.detail)
          : this.sessionPick(run, branch.owner, item.sessionId!, item.title, item.description, item.detail));
        const merged = new Map([...branch.picks, ...picks].map(pick => [pick.id, pick]));
        branch.picks = rankResourcePicks([...merged.values()], filter, run.cache);
        branch.status = this.pageStatus(branch.key, branch.label, page, branch.picks.length, () => queue(branch, page.cursor));
      } catch (error) {
        if (!alive()) return;
        branch.status = { ...this.failure(branch.label, error), id: `status-${branch.key}`,
          buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.refresh), tooltip: '重试此查询' }],
          trigger: async () => { routes = undefined; await queue(branch, cursor); return TriggerAction.NO_ACTION; } };
      } finally { render(); }
    };
    for (const branch of branches) this.report(queue(branch));
    // TODO：文件原 SearchService 的续页披露仍需接入；达到上限时明确要求缩小查询。
    return [...fast, ...branches.map(branch => branch.status)];
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

  private pageStatus(key: string, category: string, page: NameSearchPage, count: number, more: () => Promise<void>): ResourcePick {
    return { id: `status-${key}`, label: `${category} · ${page.limited ?? (page.cursor ? '还有更多结果' : count ? '本次查询完成' : '没有匹配的资源')}`,
      ...(page.cursor ? { buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.more), tooltip: '继续查询此类别' }],
        trigger: async () => { await more(); return TriggerAction.NO_ACTION; } } : {}) };
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
      if (run.picker.activeItems.some(item => item.id === run.restoreId)) run.restoreId = undefined;
    } finally { run.picker.keepScrollPosition = keep; run.publishing = false; }
  }

  private recent(run: AccessRun, filter: string): ResourcePick[] {
    if (run.options.filesOnly || this.configuration.getValue<boolean>('search.quickOpen.includeHistory') === false) return [];
    const picks = new Map<string, ResourcePick>();
    for (const editor of this.history.getHistory()) {
      const resource = editor.resource;
      const owner = resource && editorResourceOwner(resource);
      if (!resource || !owner || !run.owners.some(target => sameSearchOwner(owner, target))) continue;
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
    editor?: EditorInput | IResourceEditorInput): ResourcePick {
    const side = run.options.includeOpenToSideButton !== false;
    const dirty = editor && isEditorInput(editor) && editor.isDirty() && !editor.isSaving();
    const input = editor ?? { resource, label: title };
    return { id: resource.toString(), resource, editor, label: title, description, detail,
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

  private sessionPick(run: AccessRun, owner: WorkspaceOwner, sessionId: string, title: string, description: string, detail: string): ResourcePick {
    const open = async (mods: IKeyMods, background: boolean, side = false) => {
      const group = this.targetGroup(run, mods, side);
      const routeId = await gateway.resolveWorkspace(owner);
      const main = await gateway.mainThread(routeId, sessionId);
      await this.open(run, new ThreadInput({ ...owner, routeId, sessionId, threadId: main.thread_id, title }), mods, background, false, group);
    };
    return { id: `session:${searchOwnerKey(owner)}/${sessionId}`, label: title, description, detail,
      iconClass: ThemeIcon.asClassName(Codicon.commentDiscussion),
      buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.splitHorizontal), tooltip: '在侧边打开' }],
      trigger: (_index, mods) => { this.report(open(mods, false, true)); return TriggerAction.CLOSE_PICKER; },
      accept: (mods, event) => this.report(open(mods, event.inBackground)) };
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

  private async open(run: AccessRun, editor: EditorInput | IResourceEditorInput, mods: IKeyMods, background: boolean, side = false, group = this.targetGroup(run, mods, side), selection?: IRange): Promise<void> {
    const options = { pinned: mods.ctrlCmd || background || this.openPinned(), preserveFocus: background,
      selection: editor.resource?.scheme === 'boxteam-thread' ? undefined : selection ?? run.range };
    if (isEditorInput(editor)) await this.editors.openEditor(editor, options, group);
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
