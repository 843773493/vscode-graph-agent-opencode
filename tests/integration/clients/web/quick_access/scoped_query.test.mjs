import { describe, expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform, transformedSourcePaths } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/scoped_query.test.mjs').prepare();
const workbench = resolve('src/clients/workbench');
const transform = sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []);
const bundle = join(run.artifactsDir, 'query.fixture.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/query.fixture.ts')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  tsconfig: join(workbench, 'tsconfig.jsonc'), plugins: [buildSourcePlugin(workbench, transform)], logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const owner = { gatewayId: 'gateway-a', workspaceId: 'workspace-a' };
const root = 'boxteam://workspace/workspace-a/resources/files/root';
const scope = { version: 1, workspaces: [{ ...owner, rootUri: root, label: '工作区 A' }] };
const uri = (name = '', target = owner) => m.workspaceFileUri(target, root + (name ? '/' + name : ''), root);

function fixture(context = new m.SearchWorkspaceContext(scope), groupService = { groups: [] }) {
  const configuration = { editor: { wordSeparators: ' /' }, files: { encoding: 'utf8', exclude: {} },
    search: { useIgnoreFiles: false, useGlobalIgnoreFiles: false, useParentIgnoreFiles: false, followSymlinks: true } };
  const services = new m.ServiceCollection(
    [m.IConfigurationService, { getValue: () => configuration }], [m.IWorkspaceContextService, context],
    [m.IEditorGroupsService, groupService], [m.ILogService, new m.NullLogService()],
    [m.IPathService, { userHome: () => m.URI.parse('file:///home/native') }],
    [m.IUriIdentityService, { extUri: m.extUri, onDidChange: m.Event.None }],
  );
  const instantiation = new m.InstantiationService(services, true);
  return { context, instantiation, builder: instantiation.createInstance(m.QueryBuilder) };
}

describe('生产 sourceTransform 与原 QueryBuilder 范围接线', () => {
  test('显式转接保留原归属及接收者，新入口独立捕获，不隐式继承旧错误或接受动作', () => {
    const editors = { activeEditor: new m.ThreadInput({ ...owner, sessionId: 'session-a', threadId: 'thread-a', title: 'A', routeId: '' }) };
    const accept = () => {}, additions = [{ label: '原附件' }];
    const initial = m.prepareSearchInvocation({ providerOptions: { handleAccept: accept, additionPicks: additions, includeHelp: true },
      enabledProviderPrefixes: ['', '?', '%'] }, editors);
    const other = { ...owner, workspaceId: 'workspace-b' };
    editors.activeEditor = new m.ThreadInput({ ...other, sessionId: 'session-b', threadId: 'thread-b', title: 'B', routeId: '' });
    const transfer = m.prepareSearchInvocation({ ...initial, preserveValue: true,
      providerOptions: { ...initial.providerOptions, includeHelp: false } }, editors);
    expect(transfer.providerOptions.searchOwner).toEqual(owner);
    expect(transfer.providerOptions.handleAccept).toBe(accept);
    expect(transfer.providerOptions.additionPicks).toBe(additions);
    expect(transfer.providerOptions.includeHelp).toBe(false);
    expect(transfer.enabledProviderPrefixes).toEqual(['', '?', '%']);
    const fresh = m.prepareSearchInvocation(undefined, editors);
    expect(fresh.providerOptions.searchOwner).toEqual(other);
    expect(fresh.providerOptions.handleAccept).toBeUndefined();
    expect(fresh.enabledProviderPrefixes).toBeUndefined();
    const failed = { providerOptions: { searchOwnerError: '发起身份已失效' } };
    const continued = m.prepareSearchInvocation(failed, editors);
    expect(continued.providerOptions.searchOwnerError).toBe('发起身份已失效');
    expect(continued.providerOptions.searchOwner).toBeUndefined();
    expect(fresh.providerOptions.searchOwnerError).toBeUndefined();
    const selected = [owner, other];
    const multi = m.prepareSearchInvocation({ providerOptions: { searchOwners: selected } }, { activeEditor: undefined });
    expect(multi.providerOptions.searchOwners).toBe(selected);
    expect(multi.providerOptions.searchOwner).toBeUndefined();
    expect(multi.providerOptions.searchOwnerError).toBeUndefined();
  });

  test('预览恢复等待关闭时可释放 Provider，迟到恢复不清除下一次定位状态', async () => {
    for (const dispose of [true, false]) {
      let release;
      const closed = new Promise(resolve => { release = resolve; });
      const original = { id: 'original' }, next = { id: 'next' }, restored = [];
      const preview = { isDirty: () => false };
      const group = { isTransient: editor => editor === preview, closeEditor: () => closed,
        openEditor: async editor => { restored.push(editor.id); } };
      const editors = { activeEditorPane: { input: original, group, getControl: () => undefined },
        openEditor: async () => ({ input: preview, group }) };
      const state = new m.PickerEditorState(editors, { groups: [group] });
      state.set();
      await state.openTransientEditor({ resource: m.URI.parse('workbench:/preview') });
      const restoring = state.restore();
      if (dispose) state.dispose();
      else { editors.activeEditorPane.input = next; state.set(); }
      release();
      await restoring;
      expect(restored).toEqual(['original']);
      await state.restore();
      expect(restored).toEqual(dispose ? ['original'] : ['original', 'next']);
      state.dispose();
    }
  });

  test('固定提交及每个锚点缺失、重复均失败，实际上游原件保持不变', async () => {
    expect(() => sourceTransform(workbench, { commit: 'other' }, [])).toThrow('上游版本');
    for (const path of transformedSourcePaths) {
      const absolute = join(workbench, 'upstream', path), original = await readFile(absolute, 'utf8');
      const generated = await transform(absolute, original);
      expect(generated).not.toBe(original);
      await expect(transform(absolute, '')).rejects.toThrow('恰好一次');
      await expect(transform(absolute, original + original)).rejects.toThrow('恰好一次');
      expect(await readFile(absolute, 'utf8')).toBe(original);
    }
  });

  test('真实 child 构建只使用明确根，在查询前排除跨 Gateway、跨工作区及原生 extras', () => {
    const f = fixture();
    try {
      const a = uri('same.txt'), b = uri('same.txt', { ...owner, gatewayId: 'gateway-b' });
      const c = uri('same.txt', { ...owner, workspaceId: 'workspace-c' });
      const query = f.builder.text({ pattern: 'Needle' }, [uri()], {
        extraFileResources: [a, b, c, m.URI.parse('workbench:/same.txt')], isSmartCase: true,
      });
      expect(query.folderQueries.map(folder => folder.folder.toString())).toEqual([uri().toString()]);
      expect(query.extraFileResources.map(resource => resource.toString())).toEqual([a.toString()]);
      expect(query.contentPattern.isCaseSensitive).toBe(true);
      expect(f.context.getWorkspaceFolder(b)).toBeNull();
      expect(() => f.builder.text({ pattern: 'needle' }, [b])).toThrow('超出');
    } finally { f.instantiation.dispose(); f.context.dispose(); }
  });

  test('仅已打开保留明确根，不进入按 fsPath 枚举全部编辑器的旧分支', () => {
    const f = fixture(undefined, { get groups() { throw new Error('不应读取其它组资源'); } });
    try {
      const query = f.builder.text({ pattern: 'needle' }, [uri()], { onlyOpenEditors: true, includePattern: 'src/*.ts' });
      expect(query.onlyOpenEditors).toBe(true);
      expect(query.folderQueries.map(folder => folder.folder.toString())).toEqual([uri().toString()]);
      expect(query.includePattern).toEqual({ 'src/*.ts': true });
    } finally { f.instantiation.dispose(); f.context.dispose(); }
  });

  test('显式 search path 不得越出所选根，原生 context 的仅已打开分支保持原行为', () => {
    const f = fixture();
    try {
      expect(() => f.builder.text({ pattern: 'needle' }, [uri()], { includePattern: '/elsewhere', expandPatterns: true })).toThrow('超出');
    } finally { f.instantiation.dispose(); f.context.dispose(); }
    const nativeRoot = m.URI.parse('workbench:/Workspace'), nativeFile = m.URI.joinPath(nativeRoot, 'one.txt');
    const native = {
      getWorkspace: () => ({ id: 'native', folders: [] }), getWorkbenchState: () => 1,
      getWorkspaceFolder: () => null, isInsideWorkspace: () => false };
    const n = fixture(native, { groups: [{ editors: [{ resource: nativeFile }] }] });
    try {
      const query = n.builder.file([nativeRoot], { onlyOpenEditors: true });
      expect(query.folderQueries.map(folder => folder.folder.toString())).toEqual([nativeRoot.toString()]);
      expect(query.folderQueries[0].includePattern).toEqual({ 'one.txt': true });
    } finally { n.instantiation.dispose(); }
  });

  test('结果转交后 child 随原结果模型释放，关闭 Picker 不提前销毁', () => {
    const context = new m.SearchWorkspaceContext(scope), resultLifetime = new m.DisposableStore();
    let disposed = 0;
    context.ownChild({ dispose() { disposed++; } });
    context.transferTo({ ownSearchContext(child) { resultLifetime.add(child); } });
    context.dispose();
    expect(disposed).toBe(0);
    resultLifetime.dispose();
    expect(disposed).toBe(1);
    const cancelled = new m.SearchWorkspaceContext(scope);
    cancelled.ownChild({ dispose() { disposed++; } }); cancelled.dispose();
    expect(disposed).toBe(2);
  });

  test('范围变更发出原事件，旧 join 迟到不覆盖新范围；查询可反解原稳定根', async () => {
    const context = new m.SearchWorkspaceContext();
    let release;
    const waiting = new Promise(resolve => { release = resolve; });
    const changing = context.onWillChangeWorkspaceFolders(event => {
      if (event.changes.added[0].name === '旧等待') event.join(waiting);
    });
    const changed = [];
    const subscription = context.onDidChangeWorkspaceFolders(event => changed.push(event));
    try {
      const unbound = fixture(context);
      try { expect(() => unbound.builder.text({ pattern: 'needle' }, [])).toThrow('明确'); }
      finally { unbound.instantiation.dispose(); }
      const old = context.updateScope({ ...scope, workspaces: [{ ...scope.workspaces[0], label: '旧等待' }] });
      await context.updateScope(scope);
      release(); await old;
      expect(context.getWorkspace().folders[0].name).toBe('工作区 A');
      expect(changed).toHaveLength(1);
      expect(m.scopeFromQuery({ folderQueries: [{ folder: uri('nested'), folderName: '工作区 A' }] })).toEqual(scope);
      expect(() => m.scopeFromQuery({ folderQueries: [{ folder: m.URI.parse('workbench:/Workspace') }] })).toThrow('稳定');
    } finally { changing.dispose(); subscription.dispose(); context.dispose(); }
  });
});
