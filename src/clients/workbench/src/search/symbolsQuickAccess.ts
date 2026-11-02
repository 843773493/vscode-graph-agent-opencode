import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import type { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { IQuickAccessProviderDescriptor } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { SymbolsQuickAccessProvider, type ISymbolQuickPickItem } from '../../upstream/src/vs/workbench/contrib/search/browser/symbolsQuickAccess.js';
import type { SearchInvocationOptions } from './target.js';

/** 原全局符号服务没有目录参数；不能先访问所有 Provider 再过滤业务工作区。 */
export class WorkspaceSymbolsQuickAccess extends SymbolsQuickAccessProvider {
  override provide(picker: Parameters<SymbolsQuickAccessProvider['provide']>[0], token: CancellationToken, options: SearchInvocationOptions = {}) {
    if (options.searchOwners || options.searchOwner || options.searchOwnerError) picker.placeholder = `工作区符号 · ${options.searchOwners?.map(owner => owner.workspaceId).join('、') ?? options.searchOwner?.workspaceId ?? '归属不可用'}`;
    return super.provide(picker, token, options);
  }

  protected override _getPicks(filter: string, disposables: DisposableStore, token: CancellationToken, options?: SearchInvocationOptions): Promise<ISymbolQuickPickItem[]> {
    if (!options?.searchOwners && !options?.searchOwner && !options?.searchOwnerError) return super._getPicks(filter, disposables, token);
    // TODO：接入能在查询前限定完整业务根的符号服务后，再开放业务工作区符号结果。
    return Promise.resolve([{ label: options.searchOwnerError && !options.searchOwners ? `工作区符号不可用：${options.searchOwnerError}`
      : '工作区符号未启用：当前没有可限定业务根的符号服务' }]);
  }
}

export function selectSymbolSearchDescriptor(descriptor: IQuickAccessProviderDescriptor): IQuickAccessProviderDescriptor {
  return { ...descriptor, ctor: WorkspaceSymbolsQuickAccess };
}
