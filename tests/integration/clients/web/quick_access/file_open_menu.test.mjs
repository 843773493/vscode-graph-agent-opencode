import { expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/file_open_menu.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'file-open-menu.fixture.mjs');
await build({ stdin: { contents: `
import './tests/harness/js/workbench-locale.ts';
export { showFileOpenMenu } from './src/clients/workbench/src/fileOpenMenu.js';
export { workspaceFileSource } from './src/clients/workbench/src/workspaceFiles.js';
export { GroupDirection } from './src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
export { IConfigurationService } from './src/clients/workbench/upstream/src/vs/platform/configuration/common/configuration.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const owner = { gatewayId: 'gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', workspaceId: '019a0000-0000-7000-8000-000000000001', sessionId: 'file-menu' };
const rootUri = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root`;
const path = `${rootUri}/file.txt`;

// 执行真实菜单 Action；仅组服务、编辑器 writer 和打开方式选择结果为边界装具。
function fixture(direction = 'right', existing = true) {
  const source = { id: 1 }, other = { id: 2 }, side = { id: 3 }, live = new Map([[1, source], [2, other]]);
  const opened = [], located = [], added = [], picking = Promise.withResolvers();
  let menu;
  const groups = { activeGroup: source, getGroup: id => live.get(id),
    findGroup(location, from) { located.push({ location, from }); return existing ? side : undefined; },
    addGroup(from, direction) { added.push({ from, direction }); return side; } };
  const instantiation = { createInstance: () => ({ pick: () => picking.promise }),
    invokeFunction: callback => callback({ get: service => {
      expect(service).toBe(m.IConfigurationService);
      return { getValue: key => { expect(key).toBe('workbench.editor.openSideBySideDirection'); return direction; } };
    } }) };
  m.showFileOpenMenu(path, { x: 3, y: 5 }, owner, { showContextMenu: value => { menu = value; } },
    { openEditor: async (input, target) => { opened.push({ input, target }); } }, groups, instantiation, source);
  groups.activeGroup = other;
  return { source, side, live, opened, located, added, picking,
    action: name => menu.getActions().find(action => action.id === `boxteam.file.${name}`),
    dispose: () => menu.onHide() };
}

for (const [name, pinned] of [['open', true], ['preview', false]]) test(`${name}保留右键发起组和预览选项`, async () => {
  const f = fixture();
  try {
    await f.action(name).run();
    expect(f.opened).toHaveLength(1); expect(f.opened[0].target).toBe(f.source.id);
    expect(f.opened[0].input.options).toEqual({ pinned });
    expect(m.workspaceFileSource(f.opened[0].input.resource)).toEqual({ gatewayId: owner.gatewayId, workspaceId: owner.workspaceId, path, root: rootUri });
    expect(f.located).toEqual([]);
  } finally { f.dispose(); }
});

for (const direction of ['right', 'down']) for (const existing of [true, false]) test(`侧开按${direction}配置从原组${existing ? '查找' : '新建'}，不随活动组`, async () => {
  const f = fixture(direction, existing);
  try {
    await f.action('openSide').run();
    const expected = direction === 'down' ? m.GroupDirection.DOWN : m.GroupDirection.RIGHT;
    expect(f.located).toEqual([{ location: { direction: expected }, from: f.source }]);
    expect(f.added).toEqual(existing ? [] : [{ from: f.source, direction: expected }]);
    expect(f.opened[0].target).toBe(f.side.id); expect(f.opened[0].input.options.pinned).toBe(true);
  } finally { f.dispose(); }
});

test('下方打开固定原组并忽略侧开方向设置', async () => {
  const f = fixture();
  try {
    await f.action('openBelow').run();
    expect(f.located).toEqual([{ location: { direction: m.GroupDirection.DOWN }, from: f.source }]);
    expect(f.opened[0].target).toBe(f.side.id);
  } finally { f.dispose(); }
});

for (const name of ['open', 'preview', 'openSide', 'openBelow', 'openWith']) test(`${name}在发起组关闭后拒绝改投且不新建组`, async () => {
  const f = fixture();
  try {
    const pending = Promise.resolve().then(() => f.action(name).run());
    if (name === 'openWith') await Promise.resolve();
    f.live.delete(f.source.id); f.picking.resolve({ override: 'test-editor', preserveFocus: true });
    await expect(pending).rejects.toThrow('发起打开的编辑器组已关闭');
    expect(f.opened).toEqual([]); expect(f.located).toEqual([]); expect(f.added).toEqual([]);
  } finally { f.picking.resolve(); f.dispose(); }
});

test('打开方式取消零打开，确认后保留原组、编辑器选择和后台选项', async () => {
  for (const options of [undefined, { override: 'test-editor', preserveFocus: true }]) {
    const f = fixture();
    try {
      const pending = f.action('openWith').run(); f.picking.resolve(options); await pending;
      expect(f.opened).toHaveLength(options ? 1 : 0);
      if (options) expect(f.opened[0]).toMatchObject({ target: f.source.id, input: { options: { ...options, pinned: true } } });
    } finally { f.picking.resolve(); f.dispose(); }
  }
});
