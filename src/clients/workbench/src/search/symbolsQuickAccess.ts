import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import type { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { IQuickAccessProviderDescriptor } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { SymbolsQuickAccessProvider, type ISymbolQuickPickItem } from '../../upstream/src/vs/workbench/contrib/search/browser/symbolsQuickAccess.js';
import { WorkspaceSymbolProviderRegistry } from '../../upstream/src/vs/workbench/contrib/search/common/search.js';
import { gatewayWorkspaceKey } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceFileSource } from '../workspaceFiles.js';
import type { SearchInvocationOptions } from './target.js';

/** 明确范围只查询完整窗口工作区；原 Provider、评分与接受动作继续由上游持有。 */
export class WorkspaceSymbolsQuickAccess extends SymbolsQuickAccessProvider {
  override provide(picker: Parameters<SymbolsQuickAccessProvider['provide']>[0], token: CancellationToken, options: SearchInvocationOptions = {}) {
    if (options.searchOwners || options.searchOwner || options.searchOwnerError) picker.placeholder = `工作区符号 · ${options.searchOwners?.map(owner => owner.workspaceId).join('、') ?? options.searchOwner?.workspaceId ?? '归属不可用'}`;
    return super.provide(picker, token, options);
  }

  protected override async _getPicks(filter: string, disposables: DisposableStore, token: CancellationToken, options?: SearchInvocationOptions): Promise<ISymbolQuickPickItem[]> {
    if (!options?.searchOwners && !options?.searchOwner && !options?.searchOwnerError) return super._getPicks(filter, disposables, token);
    try {
      if (options.searchOwnerError && !options.searchOwners) throw new Error(options.searchOwnerError);
      const owners = options.searchOwners ?? (options.searchOwner ? [options.searchOwner] : []);
      const selected = new Set(owners.map(gatewayWorkspaceKey));
      const roots = this.workspaceContextService.getWorkspace().folders;
      const windowOwners = new Set(roots.map(folder => gatewayWorkspaceKey(workspaceFileSource(folder.uri))));
      if (!selected.size || !windowOwners.size || selected.size !== windowOwners.size || [...windowOwners].some(owner => !selected.has(owner))) {
        throw new Error('内置符号 Provider 使用完整窗口工作区；请选择全部窗口工作区，部分范围尚不支持');
      }
      const providers = WorkspaceSymbolProviderRegistry.all();
      if (!providers.length) throw new Error('当前窗口没有可用的内置工作区符号 Provider');
      // TODO：分支重试和增量发布仍须接入原 Picker；本批先保留成功结果并披露原失败。
      const failures: ISymbolQuickPickItem[] = [];
      const picks = await this.getSymbolPicks(filter, { providers, onProviderError(_provider, error) {
        failures.push({ label: `工作区符号不可用：${String(error)}` });
      } }, token);
      return token.isCancellationRequested ? [] : [...picks, ...failures];
    } catch (error) {
      return token.isCancellationRequested ? [] : [{ label: `工作区符号不可用：${String(error)}` }];
    }
  }
}

export function selectSymbolSearchDescriptor(descriptor: IQuickAccessProviderDescriptor): IQuickAccessProviderDescriptor {
  return { ...descriptor, ctor: WorkspaceSymbolsQuickAccess };
}
