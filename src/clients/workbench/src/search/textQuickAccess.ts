import { t } from '../../../shared/core/i18n/index.js';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Disposable, DisposableStore, MutableDisposable, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import { IConfigurationService } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { ServiceCollection } from '../../upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { DefaultQuickAccessFilterValue, IQuickAccessProvider, IQuickAccessProviderDescriptor } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import { TextSearchQuickAccess } from '../../upstream/src/vs/workbench/contrib/search/browser/quickTextSearch/textSearchQuickAccess.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { TextSearchCompleteMessageType } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import { SearchWorkspaceContext } from './scope.js';
import { captureSearchOwner, loadSearchScope, type SearchInvocationOptions } from './target.js';

// TODO：沿原分支 cause 区分远端取消及单分支重试；普通信息／警告已交同次完成结果展示。
function searchFailure(error: unknown) { return [{ label: t('quickAccess.files.failed', error instanceof Error ? error.message : String(error)) }]; }

/** 原版 Provider 负责搜索；业务拒绝留在当前浮层，不把异步失败变成无匹配。 */
class ScopedTextSearch extends TextSearchQuickAccess {
  protected override async _getPicks(pattern: string, disposables: DisposableStore, token: CancellationToken) {
    try {
      const pendingPicks = super._getPicks(pattern, disposables, token);
      const completion = this.currentAsyncSearch;
      const picks = await pendingPicks;
      if (picks && !Array.isArray(picks) && 'additionalPicks' in picks) {
        return { ...picks, additionalPicks: Promise.all([picks.additionalPicks, completion]).then(([additional, complete]) => {
          if (token.isCancellationRequested) return [];
          const notices = complete.messages.map(message => ({ label: message.text,
            iconClass: ThemeIcon.asClassName(message.type === TextSearchCompleteMessageType.Information ? Codicon.info : Codicon.warning) }));
          if (complete.limitHit) notices.push({ label: t('quickAccess.queryLimited', t('quickAccess.moreResults')),
            iconClass: ThemeIcon.asClassName(Codicon.warning) });
          // 沿原 Picker 的数组／active 形状追加纯文本提示，接受提示不执行动作。
          return 'items' in additional ? { ...additional, items: [...additional.items, ...notices] } : [...additional, ...notices];
        }).catch(error => token.isCancellationRequested ? [] : searchFailure(error)) };
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
