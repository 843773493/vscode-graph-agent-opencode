import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from 'typescript';
import { sourceTransform, pinnedUpstreamCommit } from './sourceTransform.mjs';
import { Range } from '../upstream/src/vs/editor/common/core/range.js';
import { groupBy } from '../upstream/src/vs/base/common/arrays.js';
import { compare } from '../upstream/src/vs/base/common/strings.js';
import { CancellationToken, CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import { gatewayWorkspaceKey } from '../../web/src/api/workspaceIdentity.js';

const root = resolve('src/clients/workbench');
const path = join(root, 'upstream/src/vs/workbench/contrib/search/common/search.ts');
const transformed = await sourceTransform(root, { commit: pinnedUpstreamCommit }, [])(path, await readFile(path, 'utf8'));
const parsed = ts.createSourceFile(path, transformed, ts.ScriptTarget.Latest, true);
const declarations = parsed.statements.filter(node => (ts.isModuleDeclaration(node) || ts.isClassDeclaration(node) || ts.isFunctionDeclaration(node)) && node.name && ['WorkspaceSymbolProviderRegistry', 'WorkspaceSymbolItem', 'getWorkspaceSymbols'].includes(node.name.getText(parsed)));
// 执行原聚合实现；只去掉模块导出，不复制聚合、去重、Provider 与取消逻辑。
const aggregate = new Bun.Transpiler({ loader: 'ts' }).transformSync(declarations.map(node => node.getText(parsed).replace(/^export /, '')).join('\n'));
interface Symbol { name: string; kind: number; location: { uri: URI; range?: Range }; }
interface Provider { provideWorkspaceSymbols(query: string, token: CancellationToken): Promise<Symbol[]>; }
interface Pick { label: string; }
const original = new Function('Range', 'groupBy', 'compare', 'CancellationToken', 'onUnexpectedExternalError', aggregate + '\nreturn { WorkspaceSymbolProviderRegistry, getWorkspaceSymbols };')(
  Range, groupBy, compare, CancellationToken, (error: unknown) => { throw error; },
) as { WorkspaceSymbolProviderRegistry: { all(): Provider[]; register(provider: Provider): { dispose(): void } };
  getWorkspaceSymbols(query: string, token: CancellationToken, providers: Provider[], onError: (provider: Provider, error: unknown) => void): Promise<{ symbol: Symbol; provider: Provider }[]> };
const scopePath = join(root, 'src/search/symbolsQuickAccess.ts');
const scopeSource = ts.createSourceFile(scopePath, await readFile(scopePath, 'utf8'), ts.ScriptTarget.Latest, true);
const owner = scopeSource.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'WorkspaceSymbolsQuickAccess')! as ts.ClassDeclaration;
const method = owner.members.find(node => ts.isMethodDeclaration(node) && node.name.getText(scopeSource) === '_getPicks')!;
const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(`class Scope { ${method.getText(scopeSource).replace('override ', '')} }`);
const Scope = new Function('WorkspaceSymbolProviderRegistry', 'gatewayWorkspaceKey', 'workspaceFileSource', javascript + '\nreturn Scope;')(
  original.WorkspaceSymbolProviderRegistry, gatewayWorkspaceKey,
  (uri: URI) => { const [, workspaceId] = uri.path.split('/'); return { gatewayId: uri.authority, workspaceId }; },
) as new () => { workspaceContextService: object; getSymbolPicks: (query: string, options: { providers: Provider[]; onProviderError: (provider: Provider, error: unknown) => void }, token: CancellationToken) => Promise<Pick[]>;
  _getPicks(query: string, disposable: object, token: CancellationToken, options: object): Promise<Pick[]> };
const a = { gatewayId: 'g', workspaceId: 'a' }, b = { gatewayId: 'g', workspaceId: 'b' };
function fixture() {
  const scope = new Scope();
  scope.workspaceContextService = { getWorkspace: () => ({ folders: ['a', 'b'].map(id => ({ uri: URI.parse(`boxteam-file://g/${id}/root`) })) }) };
  scope.getSymbolPicks = async (query, options, token) => (await original.getWorkspaceSymbols(query, token, options.providers, options.onProviderError)).map(item => ({ label: item.symbol.name }));
  return scope;
}

test('完整窗口范围才调用原Registry；部分、空或其它工作区范围零Provider调用', async () => {
  let calls = 0;
  const provider = { provideWorkspaceSymbols: async () => { calls++; return [{ name: 'builtin', kind: 1, location: { uri: URI.parse('boxteam-file://g/a/root/main.ts') } }]; } };
  const registration = original.WorkspaceSymbolProviderRegistry.register(provider);
  try {
    const scope = fixture();
    for (const owners of [[a], [], [a, { ...b, gatewayId: 'other' }]]) {
      expect((await scope._getPicks('', {}, CancellationToken.None, { searchOwners: owners }))[0]!.label).toContain('完整窗口工作区');
    }
    expect(calls).toBe(0);
    expect(await scope._getPicks('', {}, CancellationToken.None, { searchOwners: [b, a, a] })).toEqual([{ label: 'builtin' }]);
    expect(calls).toBe(1);
  } finally { registration.dispose(); }
});

test('原聚合保留成功候选及Provider关联，失败可见，取消不发布迟到错误', async () => {
  const symbol = { name: 'good', kind: 1, location: { uri: URI.parse('boxteam-file://g/a/root/main.ts') } };
  const good = { provideWorkspaceSymbols: async () => [symbol] };
  const failure = { provideWorkspaceSymbols: async (): Promise<Symbol[]> => { throw new Error('language server failed'); } };
  const registrations = [good, failure].map(provider => original.WorkspaceSymbolProviderRegistry.register(provider));
  try {
    const items = await original.getWorkspaceSymbols('', CancellationToken.None, [good], () => {});
    expect(items[0]!.symbol).toBe(symbol); expect(items[0]!.provider).toBe(good);
    const picks = await fixture()._getPicks('', {}, CancellationToken.None, { searchOwners: [a, b] });
    expect(picks).toEqual([{ label: 'good' }, { label: '工作区符号不可用：Error: language server failed' }]);
    const cancelled = new CancellationTokenSource(); cancelled.cancel();
    expect(await fixture()._getPicks('', {}, cancelled.token, { searchOwners: [a, b] })).toEqual([]);
    cancelled.dispose();
  } finally { registrations.forEach(registration => registration.dispose()); }
  expect((await fixture()._getPicks('', {}, CancellationToken.None, { searchOwners: [a, b] }))[0]!.label).toContain('没有可用的内置');
});
