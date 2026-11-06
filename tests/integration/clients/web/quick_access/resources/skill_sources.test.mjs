import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/skill_sources.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'skill-sources.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/resources/skill_sources.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  return new URL(request.url).pathname === '/' ? new Response('<!doctype html><title>Skill 来源合同</title>')
    : new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
} });
run.addCleanup('Skill 合同静态服务', () => server.stop(true));
let browser;
beforeAll(async () => { browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] }); run.addCleanup('Chromium', () => browser.close()); });
afterAll(() => run.close());
async function exercise(mode) {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', cause => errors.push(String(cause)));
  try {
    await page.goto(server.url.href);
    const result = await page.evaluate(mode => import('/skill-sources.mjs').then(module => module.skillSourcesContract(mode)), mode);
    await Bun.write(join(run.artifactsDir, `${mode}.json`), JSON.stringify(result, null, 2) + '\n');
    expect(errors).toEqual([]); return result;
  } finally { await page.close(); }
}
const metadata = result => result.requests.filter(item => item.path.startsWith('/api/v1'));
const workspaceReads = result => result.requests.filter(item => item.path === '/api/gateway/workspaces');
const skillRows = result => result.rows.filter(item => item.description?.startsWith('配置 Skill'));

test('同名 Skill 按完整 owner 和原来源 URI 分别保留，浏览只读 metadata', async () => {
  const result = await exercise('browse'), skills = skillRows(result);
  expect(skills).toHaveLength(4); expect(new Set(skills.map(item => item.id)).size).toBe(4);
  expect(skills.map(item => item.description)).toEqual(['配置 Skill · 工作区 0 · 优先级 0', '配置 Skill · 工作区 0 · 优先级 7', '配置 Skill · 工作区 1 · 优先级 0', '配置 Skill · 工作区 1 · 优先级 7']);
  expect(result.requests.every(item => item.method === 'GET')).toBe(true);
  expect(result.opened).toEqual([]); expect(result.references).toEqual([]);
});
test('同次 provide 的连续筛选只发现一次，两种 metadata 均不按字符重拉', async () => {
  const result = await exercise('reuse');
  expect(workspaceReads(result)).toHaveLength(1); expect(metadata(result)).toHaveLength(4);
  expect(result.snapshots.map(rows => rows.length)).toEqual([4, 4, 1, 2, 6]);
  expect(result.snapshots[2][0].id).toBe('no-sources');
  expect(result.snapshots[0].every(item => item.highlights.label.length)).toBe(true);
});
test('发现未完成时修改筛选不取消共享读取，只有最新过滤结果发布', async () => {
  const result = await exercise('filter-pending');
  expect(workspaceReads(result)).toHaveLength(1); expect(metadata(result)).toHaveLength(4);
  expect(result.aborted).toBe(0); expect(result.rows).toHaveLength(4);
  expect(result.rows.every(item => item.label.startsWith('同名'))).toBe(true);
});
test('重新打开重新发现 owner 当前接受的 metadata', async () => {
  const result = await exercise('reopen');
  expect(workspaceReads(result)).toHaveLength(2); expect(metadata(result)).toHaveLength(8);
  expect(result.snapshots[0].filter(item => item.label.startsWith('同名')).every(item => item.label.endsWith('1'))).toBe(true);
  expect(result.snapshots[1].filter(item => item.label.startsWith('同名')).every(item => item.label.endsWith('2'))).toBe(true);
});
for (const mode of ['close', 'identity', 'gateway']) test(`${mode}取消在途 metadata，不发布迟到来源`, async () => {
  const result = await exercise(mode);
  expect(result.aborted).toBeGreaterThan(0); expect(result.references).toEqual([]); expect(result.opened).toEqual([]);
  expect(result.rows.map(item => item.id)).toEqual(mode === 'close' ? [] : ['source-access-invalidated']);
});
test('四个 owner 的两种 metadata 共用真实三并发，第四个请求等待空位', async () => {
  const result = await exercise('parallel');
  expect(result.maximum).toBe(3); expect(result.barrierRequests).toBe(3);
  expect(metadata(result)).toHaveLength(8); expect(result.rows).toHaveLength(12);
});
for (const mode of ['fail-skill', 'fail-agent']) test(`${mode}只报告独立分支失败并保留其他来源`, async () => {
  const result = await exercise(mode), failure = result.rows.filter(item => !item.selectable);
  expect(failure).toHaveLength(1); expect(failure[0].label).toContain('具名来源边界失败');
  expect(failure[0].detail).toBe('request_id: source-failure-id');
  expect(result.rows.filter(item => item.selectable)).toHaveLength(mode === 'fail-skill' ? 4 : 5);
  expect(result.opened).toEqual([]);
});
test('Skill 响应 workspace 不匹配时拒绝该分支，不创建可打开来源', async () => {
  const result = await exercise('mismatch');
  expect(skillRows(result)).toHaveLength(2); expect(result.rows.find(item => !item.selectable).label).toContain('工作区归属不匹配');
  expect(result.references).toEqual([]); expect(result.opened).toEqual([]);
});
test('不可用来源保留原 error_code、优先级和 URI，且不可接受', async () => {
  const result = await exercise('unavailable'), unavailable = result.rows.filter(item => item.label.includes('来源不可用'));
  expect(unavailable).toHaveLength(1); expect(unavailable[0].selectable).toBe(false);
  expect(unavailable[0].description).toContain('优先级 7'); expect(unavailable[0].detail).toContain('source-unavailable');
  expect(unavailable[0].id).toContain('/resources/skills/http_higher/SKILL.md');
});
for (const mode of ['scope-change', 'scope-cancel', 'scope-pending']) test(`${mode}经原范围按钮重开并结束旧发现`, async () => {
  const result = await exercise(mode);
  expect(result.shows).toHaveLength(1); expect(result.shows[0].options.providerOptions.resourceGroup).toBe(7);
  expect(workspaceReads(result)).toHaveLength(2);
  expect(result.shows[0].options.providerOptions.searchOwners).toHaveLength(mode === 'scope-cancel' ? 2 : 1);
  expect(result.rows).toHaveLength(mode === 'scope-cancel' ? 6 : 3);
  if (mode !== 'scope-cancel') expect(result.rows.every(item => item.detail.startsWith(`gateway_${'b'.repeat(32)} ·`))).toBe(true);
  expect(result.opened).toEqual([]);
});
for (const mode of ['open', 'side', 'pinned', 'background', 'freeze']) test(`${mode}只经原 Opener 传完整 owner、冻结组和编辑器选项`, async () => {
  const result = await exercise(mode), parses = result.requests.filter(item => item.path.endsWith('/parse'));
  expect(result.errors).toEqual([]); expect(parses).toHaveLength(1); expect(result.opened).toHaveLength(1);
  expect(parses[0].body.resource_gateway_id).toBe(`gateway_${'b'.repeat(32)}`);
  expect(result.opened[0].source.gatewayId).toBe(`gateway_${'b'.repeat(32)}`);
  expect(result.opened[0].source.uri).toBe(parses[0].body.uri);
  expect(result.opened[0].group).toBe(mode === 'side' ? 8 : 7);
  expect(result.opened[0].options).toMatchObject({ preserveFocus: mode === 'background', pinned: ['pinned', 'background'].includes(mode) });
  expect(result.references).toHaveLength(1); expect(result.released).toBe(1);
});
