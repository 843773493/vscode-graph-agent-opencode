import { t } from '../../../shared/core/i18n/index.js';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { isCancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { escapeIcons } from '../../upstream/src/vs/base/common/iconLabels.js';
import { Disposable, DisposableStore, MutableDisposable, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ResourceSet } from '../../upstream/src/vs/base/common/map.js';
import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import { IConfigurationService } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ServiceCollection } from '../../upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { TriggerAction, type IPickerQuickAccessItem } from '../../upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.js';
import { DefaultQuickAccessFilterValue, IQuickAccessProvider, IQuickAccessProviderDescriptor } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import { MAX_FILES_SHOWN, TextSearchQuickAccess } from '../../upstream/src/vs/workbench/contrib/search/browser/quickTextSearch/textSearchQuickAccess.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { TextSearchCompleteMessageType, type ISearchComplete } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { errorDiagnostic, errorMessage, errorSummary } from '../../../web/src/utils/errorMessage.js';
import { failureSummary } from '../../../shared/core/failurePresentation.js';
import { copyTextToClipboard } from '../../../web/src/utils/clipboard.js';
import { WorkspaceSearchBranchError, workspaceSearchBranchErrors } from './fileProvider.js';
import { SearchWorkspaceContext } from './scope.js';
import { captureSearchOwner, loadSearchScope, type SearchInvocationOptions } from './target.js';
import { workspaceContext } from '../workspaceContext.js';

function searchFailure(error: unknown): IPickerQuickAccessItem[] {
  const branches = workspaceSearchBranchErrors(error);
  const copy = (failure: unknown) => ({ buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.copy), tooltip: t('shell.copyDiagnostic') }],
    trigger: async () => { await copyTextToClipboard(errorDiagnostic(failure)); return TriggerAction.NO_ACTION; } });
  return branches ? branches.map(branch => ({ ...copy(branch), label: escapeIcons(failureSummary(t(branch.cause.branch!.state === 'cancelled' ? 'quickAccess.files.cancelled' : 'quickAccess.files.failed',
    branch.cause.branch!.message ?? '未提供错误原因'))), excludeFromResultCount: true,
    iconClass: ThemeIcon.asClassName(branch.cause.branch!.state === 'cancelled' ? Codicon.info : Codicon.error) }))
    : [{ ...copy(error), label: escapeIcons(failureSummary(t(isCancellationError(error) ? 'quickAccess.files.cancelled' : 'quickAccess.files.failed', errorMessage(error)))), excludeFromResultCount: true,
      iconClass: ThemeIcon.asClassName(isCancellationError(error) ? Codicon.info : Codicon.error) }];
}

/** 原版 Provider 负责搜索；业务拒绝留在当前浮层，不把异步失败变成无匹配。 */
class ScopedTextSearch extends TextSearchQuickAccess {
  private picker?: Parameters<TextSearchQuickAccess['provide']>[0];

  override provide(picker: Parameters<TextSearchQuickAccess['provide']>[0], token: CancellationToken, options?: SearchInvocationOptions) {
    this.picker = picker;
    const lifetime = super.provide(picker, token, options);
    return toDisposable(() => { lifetime.dispose(); if (this.picker === picker) this.picker = undefined; });
  }

  protected override async _getPicks(pattern: string, disposables: DisposableStore, token: CancellationToken) {
    try {
      const pendingPicks = super._getPicks(pattern, disposables, token);
      const completion = this.currentAsyncSearch, model = this.searchModel, picker = this.picker!;
      const syncMatches = model.searchResult.matches(false), syncResources = new ResourceSet(syncMatches.map(match => match.resource));
      const messages: ISearchComplete['messages'] = [];
      let failures: { error: unknown; retryFailure?: unknown }[] = [], limited = false, retrying = false;
      const current = () => !token.isCancellationRequested && this.picker === picker && this.searchModel === model;
      const addMessages = (complete: Pick<ISearchComplete, 'messages' | 'limitHit'>) => {
        for (const message of complete.messages) if (!messages.includes(message)) messages.push(message);
        limited ||= !!complete.limitHit;
      };
      const notices = (): IPickerQuickAccessItem[] => {
        const items: IPickerQuickAccessItem[] = messages.map(message => ({ label: escapeIcons(message.text), excludeFromResultCount: true,
          iconClass: ThemeIcon.asClassName(message.type === TextSearchCompleteMessageType.Information ? Codicon.info : Codicon.warning) }));
        if (limited) items.push({ label: t('quickAccess.queryLimited', t('quickAccess.moreResults')), excludeFromResultCount: true, iconClass: ThemeIcon.asClassName(Codicon.warning) });
        for (const failure of failures) {
          const branch = failure.error instanceof WorkspaceSearchBranchError ? failure.error : undefined;
          items.push({ ...searchFailure(failure.error)[0],
            detail: failure.retryFailure === undefined ? undefined : escapeIcons(errorSummary(failure.retryFailure)),
            buttons: [...(branch?.retry && !failure.retryFailure && !retrying ? [{ iconClass: ThemeIcon.asClassName(Codicon.refresh), tooltip: t('quickAccess.files.retry') }] : []),
              { iconClass: ThemeIcon.asClassName(Codicon.copy), tooltip: t('shell.copyDiagnostic') }],
            trigger: async (index = 0) => {
              if (index === (branch?.retry && !failure.retryFailure && !retrying ? 1 : 0)) {
                await copyTextToClipboard(errorDiagnostic(failure.retryFailure ?? failure.error)); return TriggerAction.NO_ACTION;
              }
              if (!branch?.retry) return TriggerAction.NO_ACTION;
              if (!current() || retrying || failure.retryFailure || !failures.includes(failure)) return TriggerAction.NO_ACTION;
              retrying = true; picker.busy = true; publish();
              try {
                const complete = await branch.retry!(token, item => {
                  if (!current() || !('resource' in item)) return;
                  model.searchResult.add([item], model.id(), false); publish();
                });
                if (!current()) return TriggerAction.NO_ACTION;
                failures = failures.filter(item => item !== failure); addMessages(complete);
                if (!failures.length) this.currentAsyncSearch = Promise.resolve({ ...complete, messages, limitHit: limited });
              } catch (error) {
                if (!current()) return TriggerAction.NO_ACTION;
                const branches = workspaceSearchBranchErrors(error);
                if (branches) {
                  failures.splice(failures.indexOf(failure), 1, ...branches.map(error => ({ error })));
                  for (const branch of branches) addMessages(branch);
                } else failure.retryFailure = error;
              } finally {
                retrying = false;
                if (current()) { picker.busy = false; publish(); }
              }
              return TriggerAction.NO_ACTION;
            },
          });
        }
        return items;
      };
      const publish = () => {
        if (!current()) return;
        const active = picker.activeItems[0]?.match;
        const items = [...this._getPicksFromMatches(model.searchResult.matches(false)), ...notices()];
        model.searchResult.toggleHighlights(true);
        picker.items = items;
        if (active) picker.activeItems = items.filter((item): item is (typeof picker.activeItems)[number] => 'match' in item && item.match === active);
      };
      const picks = await pendingPicks;
      if (picks && !Array.isArray(picks) && 'additionalPicks' in picks) {
        return { ...picks, additionalPicks: Promise.all([picks.additionalPicks, completion]).then(([additional, complete]) => {
          if (!current()) return [];
          addMessages(complete);
          // 沿原 Picker 的数组／active 形状追加纯文本提示，接受提示不执行动作。
          return 'items' in additional ? { ...additional, items: [...additional.items, ...notices()] } : [...additional, ...notices()];
        }).catch(error => {
          if (!current()) return [];
          const branches = workspaceSearchBranchErrors(error);
          failures = (branches ?? [error]).map(error => ({ error }));
          for (const branch of branches ?? []) addMessages(branch);
          const matches = model.searchResult.matches(false).filter(match => !syncResources.has(match.resource));
          const partial = syncMatches.length < MAX_FILES_SHOWN ? this._getPicksFromMatches(matches, MAX_FILES_SHOWN - syncMatches.length) : [];
          if (partial.length) model.searchResult.toggleHighlights(true);
          return [...partial, ...notices()];
        }) };
      }
      return picks ?? [];
    } catch (error) { return token.isCancellationRequested ? [] : searchFailure(error); }
  }
}

/** 原 Provider 每次展示使用独立 child；Picker、匹配、预览和接受仍由原实现负责。 */
export class WorkspaceTextQuickAccess extends Disposable implements IQuickAccessProvider {
  private readonly current = this._register(new MutableDisposable<DisposableStore>());

  constructor(
    @IInstantiationService private readonly instantiation: IInstantiationService,
    @IEditorService private readonly editors: IEditorService,
    @IConfigurationService private readonly configuration: IConfigurationService,
  ) { super(); }

  get defaultFilterValue() {
    return this.configuration.getValue<boolean>('search.quickAccess.preserveInput') ? DefaultQuickAccessFilterValue.LAST : undefined;
  }

  provide(picker: Parameters<TextSearchQuickAccess['provide']>[0], token: CancellationToken, options?: SearchInvocationOptions) {
    const operation = new DisposableStore();
    this.current.value = operation;
    const cancellation = new CancellationTokenSource(token), controller = new AbortController();
    operation.add(toDisposable(() => cancellation.dispose(true)));
    operation.add(cancellation.token.onCancellationRequested(() => controller.abort()));
    const invalidate = () => {
      operation.clear();
      picker.activeItems = picker.selectedItems = [];
      picker.buttons = [];
      picker.items = [{ label: t('quickAccess.identityChangedReopen'), excludeFromResultCount: true }];
      picker.busy = false;
    };
    operation.add(toDisposable(onGatewayTokenInvalidated(0, invalidate)));
    const gatewayId = workspaceContext.getSnapshot().gatewayId;
    operation.add(toDisposable(workspaceContext.subscribe(() => {
      if (gatewayId && workspaceContext.getSnapshot().gatewayId !== gatewayId) invalidate();
    })));
    picker.busy = true;
    const fail = (error: unknown) => {
      if (cancellation.token.isCancellationRequested) return;
      controller.abort();
      picker.busy = false;
      picker.items = searchFailure(error);
    };
    // 同步捕获发生在原 Controller 的 picker.show() 之前。
    let owners: readonly ReturnType<typeof captureSearchOwner>[];
    try {
      if (options?.searchOwnerError && !options.searchOwners) throw new Error(options.searchOwnerError);
      owners = options?.searchOwners ?? [options?.searchOwner ?? captureSearchOwner(this.editors)];
    }
    catch (error) { fail(error); return operation; }
    void loadSearchScope(owners, controller.signal).then(scope => {
      if (cancellation.token.isCancellationRequested) return;
      const context = operation.add(new SearchWorkspaceContext(scope));
      const child = context.ownChild(this.instantiation.createChild(new ServiceCollection([IWorkspaceContextService, context])));
      const provider = operation.add(child.createInstance(ScopedTextSearch));
      picker.placeholder = t('quickAccess.files.placeholder', scope.workspaces.map(target => target.label).join(t('quickAccess.listSeparator')));
      picker.busy = false;
      operation.add(provider.provide(picker, cancellation.token, options));
    }).catch(fail);
    return operation;
  }
}

export function selectTextSearchDescriptor(descriptor: IQuickAccessProviderDescriptor): IQuickAccessProviderDescriptor {
  return { ...descriptor, ctor: WorkspaceTextQuickAccess };
}
