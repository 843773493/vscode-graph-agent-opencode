import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import type { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { IQuickAccessProviderDescriptor } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { SymbolsQuickAccessProvider, type ISymbolQuickPickItem } from '../../upstream/src/vs/workbench/contrib/search/browser/symbolsQuickAccess.js';
import { workspaceSymbolProviders } from '../extensions/workspaceSymbols.js';
import type { SearchInvocationOptions } from './target.js';

/** 明确范围只查询完整宿主视图；原 Provider、评分与接受动作继续由上游持有。 */
export class WorkspaceSymbolsQuickAccess extends SymbolsQuickAccessProvider {
  override provide(picker: Parameters<SymbolsQuickAccessProvider['provide']>[0], token: CancellationToken, options: SearchInvocationOptions = {}) {
    if (options.searchOwners || options.searchOwner || options.searchOwnerError) picker.placeholder = `工作区符号 · ${options.searchOwners?.map(owner => owner.workspaceId).join('、') ?? options.searchOwner?.workspaceId ?? '归属不可用'}`;
    return super.provide(picker, token, options);
  }

  protected override _getPicks(filter: string, disposables: DisposableStore, token: CancellationToken, options?: SearchInvocationOptions): Promise<ISymbolQuickPickItem[]> {
    if (!options?.searchOwners && !options?.searchOwner && !options?.searchOwnerError) return super._getPicks(filter, disposables, token);
    try {
      if (options.searchOwnerError && !options.searchOwners) throw new Error(options.searchOwnerError);
      const providers = workspaceSymbolProviders(options.searchOwners ?? (options.searchOwner ? [options.searchOwner] : []));
      return this.getSymbolPicks(filter, { providers }, token);
    } catch (error) {
      return Promise.resolve([{ label: `工作区符号不可用：${String(error)}` }]);
    }
  }
}

export function selectSymbolSearchDescriptor(descriptor: IQuickAccessProviderDescriptor): IQuickAccessProviderDescriptor {
  return { ...descriptor, ctor: WorkspaceSymbolsQuickAccess };
}
