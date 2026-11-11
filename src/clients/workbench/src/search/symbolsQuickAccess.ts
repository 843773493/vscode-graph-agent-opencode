import { CancellationTokenSource, type CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { toDisposable, type DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import { pieceToQuery, prepareQuery } from '../../upstream/src/vs/base/common/fuzzyScorer.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import { TriggerAction } from '../../upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.js';
import type { IQuickAccessProviderDescriptor } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { SymbolsQuickAccessProvider, type ISymbolQuickPickItem } from '../../upstream/src/vs/workbench/contrib/search/browser/symbolsQuickAccess.js';
import { coalesceWorkspaceSymbols, getWorkspaceSymbols, WorkspaceSymbolProviderRegistry, type IWorkspaceSymbolProvider, type WorkspaceSymbolItem } from '../../upstream/src/vs/workbench/contrib/search/common/search.js';
import { gatewayWorkspaceKey } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceFileSource } from '../workspaceFiles.js';
import type { SearchInvocationOptions } from './target.js';

/** 明确范围只查询完整窗口工作区；原 Provider、评分与接受动作继续由上游持有。 */
export class WorkspaceSymbolsQuickAccess extends SymbolsQuickAccessProvider {
  private openToken?: CancellationToken;
  private picker?: Parameters<SymbolsQuickAccessProvider['provide']>[0];

  override provide(picker: Parameters<SymbolsQuickAccessProvider['provide']>[0], token: CancellationToken, options: SearchInvocationOptions = {}) {
    if (options.searchOwners || options.searchOwner || options.searchOwnerError) picker.placeholder = `工作区符号 · ${options.searchOwners?.map(owner => owner.workspaceId).join('、') ?? options.searchOwner?.workspaceId ?? '归属不可用'}`;
    this.picker = picker;
    this.openToken = token;
    const access = new CancellationTokenSource(token);
    const lifetime = super.provide(picker, access.token, options);
    return toDisposable(() => {
      access.dispose(true);
      lifetime.dispose();
      if (this.picker === picker) this.picker = undefined;
    });
  }

  protected override _getPicks(filter: string, disposables: DisposableStore, token: CancellationToken, options?: SearchInvocationOptions) {
    if (token.isCancellationRequested) return [];
    if (!options?.searchOwners && !options?.searchOwner && !options?.searchOwnerError) return super._getPicks(filter, disposables, token);
    const picker = this.picker!;
    disposables.add(toDisposable(() => { if (this.picker === picker) picker.busy = false; }));
    let initial: ISymbolQuickPickItem[] = [];
    void this.getScopedSymbolPicks(filter, options, token, (picks, busy) => {
      initial = picks;
      if (token.isCancellationRequested || this.picker !== picker) return;
      const active = picker.activeItems[0];
      picker.busy = busy;
      picker.items = picks;
      if (active?.symbol) picker.activeItems = picks.filter(pick => pick.symbol === active.symbol);
    }, { openToken: this.openToken });
    return initial;
  }

  /** 两个入口共用范围准入、Provider 结果、失败重试与原评分／打开逻辑。 */
  async getScopedSymbolPicks(filter: string, options: SearchInvocationOptions, token: CancellationToken,
    progress: (picks: ISymbolQuickPickItem[], busy: boolean) => void,
    pickOptions?: { skipLocal?: boolean; editorGroup?: (side: boolean) => number; openToken?: CancellationToken }): Promise<void> {
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
      const results = new Map<IWorkspaceSymbolProvider, readonly WorkspaceSymbolItem[]>();
      const failures = new Map<IWorkspaceSymbolProvider, unknown>();
      const pending = new Set(providers);
      const target = owners.map(gatewayWorkspaceKey).join('、');
      const prepared = prepareQuery(filter);
      const symbolQuery = prepared.values && prepared.values.length > 1 ? pieceToQuery(prepared.values[0]) : prepared;
      const publish = () => {
        if (token.isCancellationRequested) return;
        const picks = this.toSymbolPicks(prepared, pickOptions, token, coalesceWorkspaceSymbols([...results.values()].flat()));
        for (const [provider, error] of failures) picks.push({
          label: `工作区符号不可用：${String(error)}`,
          description: `${target} · Provider ${providers.indexOf(provider) + 1}`,
          buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.refresh), tooltip: '仅重试此符号 Provider' }],
          trigger: async () => {
            if (!token.isCancellationRequested && failures.has(provider)) await query([provider]);
            return TriggerAction.NO_ACTION;
          },
        });
        if (!picks.length) picks.push({ label: pending.size ? '正在查询工作区符号…' : '没有匹配的工作区符号' });
        progress(picks, pending.size > 0);
      };
      const query = async (selected: readonly IWorkspaceSymbolProvider[]) => {
        if (token.isCancellationRequested) return [];
        for (const provider of selected) { failures.delete(provider); pending.add(provider); }
        publish();
        const onProviderError = (provider: IWorkspaceSymbolProvider, error: unknown) => {
          failures.set(provider, error); pending.delete(provider); publish();
        };
        const onProviderResult = (provider: IWorkspaceSymbolProvider, items: readonly WorkspaceSymbolItem[]) => {
          results.set(provider, items); pending.delete(provider); publish();
        };
        await getWorkspaceSymbols(symbolQuery.original, token, selected, onProviderError, onProviderResult);
        return [];
      };
      publish();
      await this.delayer.trigger(() => query(providers));
    } catch (error) {
      if (!token.isCancellationRequested) progress([{ label: `工作区符号不可用：${String(error)}` }], false);
    }
  }
}

export function selectSymbolSearchDescriptor(descriptor: IQuickAccessProviderDescriptor): IQuickAccessProviderDescriptor {
  return { ...descriptor, ctor: WorkspaceSymbolsQuickAccess };
}
