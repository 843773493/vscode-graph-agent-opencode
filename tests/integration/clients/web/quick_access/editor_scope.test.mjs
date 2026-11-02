import { describe, expect, test } from 'bun:test';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/editor_scope.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'editor.fixture.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/editor.fixture.ts')], outfile: bundle,
  bundle: true, format: 'esm', platform: 'browser', target: 'es2024', tsconfig: join(root, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const scope = { version: 1, kind: 'workspace', workspaces: [{ gatewayId: 'gateway-a', workspaceId: 'workspace-a',
  rootUri: 'boxteam://workspace/workspace-a/resources/files/root', label: '工作区 A' }] };

describe('原 Search Editor 唯一持久格式（不替代真实 Pane 查询／恢复验收）', () => {
  test('磁盘、backup 和模型 header 保留范围、末尾选项、多行／emoji 与结果正文', () => {
    const config = { ...m.defaultSearchConfig(), scope, query: '🧪\\n\nneedle', isRegexp: true,
      isCaseSensitive: true, matchWholeWord: true, filesToInclude: '**/*.txt', filesToExclude: '**/skip/**',
      contextLines: 7, onlyOpenEditors: true, showIncludesExcludes: true };
    const body = '1 result - 1 file\n\nboxteam-file://gateway-a/workspace-a/root/one.txt:\n  2: 🧪needle\n';
    const header = m.serializeSearchConfiguration(config), serialized = header + '\n' + body;
    const restored = m.parseSerializedSearchEditor(serialized);
    expect(restored.config).toEqual(config);
    expect(restored.text).toBe(body);
    const lines = serialized.split('\n');
    // 真实模型读取入口不能受旧六行上限影响，也不能扫描完整结果正文。
    const read = [];
    const model = { getLineCount: () => lines.length, getLineContent: line => { read.push(line); return lines[line - 1]; } };
    expect(m.extractSearchQueryFromModel(model)).toEqual(config);
    expect(read.at(-1)).toBe(header.split('\n').length);
    expect(read.length).toBeGreaterThan(6);
  });

  test('仅已打开单独开启也被原 Flags 保存；旧结果保留正文且不凭空取得范围', () => {
    const config = { ...m.defaultSearchConfig(), onlyOpenEditors: true, scope: { version: 1, kind: 'native' } };
    expect(m.parseSerializedSearchEditor(m.serializeSearchConfiguration(config) + '\ntext').config).toEqual(config);
    const old = m.parseSerializedSearchEditor('# Query: needle\n\nold result');
    expect(old.config.scope).toBeUndefined();
    expect(old.text).toBe('old result');
  });

  test('损坏或未知范围明确拒绝；展示名不影响同范围复用，另一个 owner 不能复用', () => {
    for (const invalid of [null, {}, { ...scope, version: 2 }, { ...scope, workspaces: [] },
      { ...scope, workspaces: [{ ...scope.workspaces[0], gatewayId: undefined }] }]) {
      expect(() => m.parseSerializedSearchEditor('# Query: needle\n# BoxTeamScope: ' + JSON.stringify(invalid) + '\n\nbody')).toThrow('范围');
    }
    expect(() => m.parseSerializedSearchEditor('# BoxTeamScope: {broken\n\nbody')).toThrow();
    expect(m.searchEditorScopeKey({ ...scope, workspaces: [{ ...scope.workspaces[0], label: '重命名' }] })).toBe(m.searchEditorScopeKey(scope));
    expect(m.searchEditorScopeKey({ ...scope, workspaces: [{ ...scope.workspaces[0], gatewayId: 'gateway-b' }] })).not.toBe(m.searchEditorScopeKey(scope));
    expect(m.searchEditorScopeKey(undefined)).not.toBe(m.searchEditorScopeKey({ version: 1, kind: 'native' }));
  });
});
