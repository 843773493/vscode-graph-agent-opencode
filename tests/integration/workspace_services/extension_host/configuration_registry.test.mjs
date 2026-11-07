import { afterAll, afterEach, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const testFile = 'tests/integration/workspace_services/extension_host/configuration_registry.test.mjs';
const run = await TestRunContext.fromTestFile(testFile).prepare(), root = resolve('src/clients/workbench');
const bundle = join(run.artifactsDir, 'configuration-registry.fixture.mjs');
const sources = [testFile, 'src/clients/workbench/scripts/sourceTransform.mjs',
  'src/clients/workbench/scripts/extensions/configurationRegistryTransform.mjs',
  'src/clients/workbench/upstream/src/vs/platform/configuration/common/configurationRegistry.ts',
  'src/clients/workbench/upstream/src/vs/platform/configuration/common/configurations.ts'];
const fingerprint = async () => Object.fromEntries(await Promise.all(sources.map(async path => [path,
  createHash('sha256').update(await readFile(path)).digest('hex')])));
const fingerprints = await fingerprint(), transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
await build({ stdin: { loader: 'ts', resolveDir: root, contents: `
export { Registry } from './upstream/src/vs/platform/registry/common/platform.js';
export { Extensions, ConfigurationScope, validateProperty } from './upstream/src/vs/platform/configuration/common/configurationRegistry.js';
export { DefaultConfiguration } from './upstream/src/vs/platform/configuration/common/configurations.js';
export { NullLogService } from './upstream/src/vs/platform/log/common/log.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'node', target: 'es2024',
  plugins: [buildSourcePlugin(root, transform)],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(bundle), registry = m.Registry.as(m.Extensions.Configuration);
const cleanups = [], observations = [];
let sequence = 0;
afterEach(() => { for (const cleanup of cleanups.splice(0).reverse()) cleanup(); });
afterAll(async () => {
  const fingerprintsAfter = await fingerprint();
  await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ upstream: pinnedUpstreamCommit,
    fingerprints, fingerprintsAfter, observations,
    boundary: '真实原ConfigurationRegistry、JSON schema注册与DefaultConfiguration订阅；目标来源用具名A/B配置贡献建立，不运行Host，不代替配置授权、scanner、完整设置窗口或Gateway验收。',
  }, null, 2));
  await run.close();
  expect(fingerprintsAfter).toEqual(fingerprints);
});

function fixture({ withPolicy = false } = {}) {
  const prefix = `registryProjection${++sequence}`, key = suffix => `${prefix}.${suffix}`;
  const a = { id: `fixture.${prefix}.a`, displayName: '目标 A', configurationOnly: true };
  const b = { id: `fixture.${prefix}.b`, displayName: '目标 B' };
  const builtin = { id: `fixture.${prefix}.builtin`, displayName: '窗口内置', isBuiltin: true };
  const language = `${prefix}-shared`, languageA = `${prefix}-a`, languageB = `${prefix}-b`;
  const common = { overrides: {
    [key('value')]: 'common', [key('object')]: { common: true },
    [`[${language}]`]: { [key('value')]: 'common-language', [key('object')]: { common: true } },
  } };
  const experiment = { source: 'experiments', overrides: { [key('experiment')]: 'treatment' } };
  const builtinDefaults = { source: builtin, overrides: { [key('builtinValue')]: 'builtin-default', [key('builtinCore')]: 'builtin-core-default' } };
  const defaults = source => ({ source, overrides: {
    [key('value')]: source === a ? 'A' : 'B',
    [key('object')]: { shared: source === a ? 'A' : 'B', [source === a ? 'a' : 'b']: true },
    [key('array')]: [source === a ? 'A' : 'B'], [key('locked')]: 'extension-default',
    [`[${language}]`]: { [key('value')]: source === a ? 'A-language' : 'B-language',
      [key('object')]: { shared: source === a ? 'A' : 'B', [source === a ? 'a' : 'b']: true } },
    [`[${source === a ? languageA : languageB}]`]: { [key('value')]: source === a ? 'A-only' : 'B-only' },
    [key(source === a ? 'hiddenA' : 'hiddenB')]: source === a ? 'A-hidden' : 'B-hidden',
  } });
  const defaultsA = defaults(a), defaultsB = defaults(b);
  const node = { id: prefix, properties: {
    [key('value')]: { type: 'string', default: 'base', scope: m.ConfigurationScope.LANGUAGE_OVERRIDABLE },
    [key('object')]: { type: 'object', default: { base: true, shared: 'base' }, scope: m.ConfigurationScope.LANGUAGE_OVERRIDABLE },
    [key('array')]: { type: 'array', items: { type: 'string' }, default: ['base'] },
    [key('locked')]: { type: 'string', default: 'base-locked', disallowConfigurationDefault: true },
    [key('experiment')]: { type: 'string', default: 'base-experiment' },
    [key('builtinCore')]: { type: 'string', default: 'base-core' },
  } };
  const extensionNode = source => ({ id: source.id, extensionInfo: source, properties: {
    [key(source === a ? 'ownA' : 'ownB')]: { type: 'string', default: source === a ? 'A-schema' : 'B-schema', enum: ['first', 'second'],
      scope: m.ConfigurationScope.RESOURCE, restricted: true, markdownDescription: '原扩展说明',
      ...(withPolicy ? { policy: { name: `${prefix}${source === a ? 'A' : 'B'}Policy` } } : {}) },
    [key(source === a ? 'hiddenA' : 'hiddenB')]: { type: 'string', default: 'hidden-base', included: false, experiment: { mode: 'startup' } },
  } });
  const builtinNode = { id: builtin.id, extensionInfo: builtin, properties: {
    [key('builtinValue')]: { type: 'string', default: 'base-builtin' },
  } };
  const nodeA = extensionNode(a), nodeB = extensionNode(b), nodes = [node, nodeA, nodeB, builtinNode];
  const registrations = [common, experiment, builtinDefaults, defaultsA, defaultsB];
  registry.registerConfigurations(nodes);
  registry.registerDefaultConfigurations(registrations);
  cleanups.push(() => { registry.deregisterDefaultConfigurations(registrations); registry.deregisterConfigurations(nodes); });
  const sourceFilter = owner => source => typeof source !== 'object' || source.isBuiltin || source.id === owner.id;
  return { key, a, b, language, languageA, languageB, defaultsA, defaultsB, nodeA, nodeB,
    filterA: sourceFilter(a), filterB: sourceFilter(b),
    removeDefaults(item) { registry.deregisterDefaultConfigurations([item]); registrations.splice(registrations.indexOf(item), 1); },
    removeNode(item) { registry.deregisterConfigurations([item]); nodes.splice(nodes.indexOf(item), 1); },
    replaceNode(previous, next) {
      registry.deltaConfiguration({ removedConfigurations: [previous], addedConfigurations: [next] });
      nodes.splice(nodes.indexOf(previous), 1, next);
    },
  };
}

test('同一原 registry 按目标过滤 schema，同时从原始贡献恢复内置键和对象默认值', () => {
  const f = fixture(), a = registry.getConfigurationProperties(f.filterA), b = registry.getConfigurationProperties(f.filterB);
  expect(a[f.key('ownA')]).toMatchObject({ default: 'A-schema', source: f.a,
    scope: m.ConfigurationScope.RESOURCE, restricted: true, markdownDescription: '原扩展说明' });
  expect(a[f.key('ownB')]).toBeUndefined(); expect(b[f.key('ownA')]).toBeUndefined();
  expect(b[f.key('ownB')].default).toBe('B-schema');
  expect(a[f.key('value')].default).toBe('A'); expect(b[f.key('value')].default).toBe('B');
  expect(a[f.key('object')].default).toEqual({ base: true, common: true, shared: 'A', a: true });
  expect(b[f.key('object')].default).toEqual({ base: true, common: true, shared: 'B', b: true });
  expect(a[f.key('object')].defaultValueSource).toBeInstanceOf(Map);
  expect(a[f.key('object')].defaultValueSource.get(`${f.key('object')}.shared`)).toEqual(f.a);
  expect(a[f.key('array')].default).toEqual(['A']); expect(b[f.key('array')].default).toEqual(['B']);
  expect(a[f.key('locked')].default).toBe('base-locked');
  expect(a[f.key('experiment')].default).toBe('treatment');
  expect(a[f.key('experiment')].defaultValueSource).toBe('experiments');
  expect(a[f.key('builtinValue')]).toMatchObject({ default: 'builtin-default', source: { isBuiltin: true } });
  expect(b[f.key('builtinValue')].default).toBe('builtin-default');
  expect(a[f.key('builtinCore')].default).toBe('builtin-core-default');
  expect(b[f.key('builtinCore')].default).toBe('builtin-core-default');
  observations.push('A/B内置键、对象合并、原来源Map、数组、disallowConfigurationDefault与窗口内置扩展贡献');
});

test('语言默认贡献按来源重建，另一目标的独有语言节点不会进入投影', () => {
  const f = fixture(), a = registry.getConfigurationProperties(f.filterA), b = registry.getConfigurationProperties(f.filterB);
  expect(a[`[${f.language}]`].default).toEqual({ [f.key('value')]: 'A-language',
    [f.key('object')]: { common: true, shared: 'A', a: true } });
  expect(b[`[${f.language}]`].default).toEqual({ [f.key('value')]: 'B-language',
    [f.key('object')]: { common: true, shared: 'B', b: true } });
  expect(a[`[${f.language}]`].source).toEqual(f.a);
  expect(a[`[${f.language}]`].defaultDefaultValue).toEqual(a[`[${f.language}]`].default);
  expect(a[`[${f.languageA}]`].default).toEqual({ [f.key('value')]: 'A-only' });
  expect(a[`[${f.languageB}]`]).toBeUndefined(); expect(b[`[${f.languageA}]`]).toBeUndefined();
  observations.push('共享语言对象原合并、筛后defaultDefaultValue和独有语言节点');
});

test('没有目标时可排除 configurationOnly，同时无参查询保留原窗口全注册结果', () => {
  const f = fixture(), window = registry.getConfigurationProperties();
  const runtime = registry.getConfigurationProperties(source => typeof source !== 'object' || !source.configurationOnly);
  expect(runtime[f.key('ownA')]).toBeUndefined(); expect(runtime[f.key('ownB')].default).toBe('B-schema');
  expect(runtime[f.key('object')].default).toEqual({ base: true, common: true, shared: 'B', b: true });
  expect(runtime[`[${f.languageA}]`]).toBeUndefined();
  expect(window[f.key('ownA')].source.configurationOnly).toBe(true);
  expect(window[f.key('object')].default).toEqual({ base: true, common: true, shared: 'B', a: true, b: true });
  expect(window).toBe(registry.getConfigurationProperties());
  expect(registry.getConfigurationProperties(() => true)).toEqual(window);
  const common = registry.getConfigurationProperties(source => typeof source !== 'object');
  expect(common[f.key('value')].default).toBe('common');
  expect(common[f.key('object')].default).toEqual({ base: true, shared: 'base', common: true });
  expect(common[`[${f.language}]`].default[f.key('value')]).toBe('common-language');
  observations.push('configurationOnly窗口过滤、内置来源保留与无参原map身份');
});

test('投影包含同目标隐藏实验 schema，修改派生结果不改变原注册或触发事件', () => {
  const f = fixture(), window = registry.getConfigurationProperties();
  const before = structuredClone(registry.getConfigurationDefaultsOverrides());
  let changes = 0;
  const events = [registry.onDidUpdateConfiguration(() => changes++), registry.onDidSchemaChange(() => changes++)];
  try {
    const a = registry.getConfigurationProperties(f.filterA), hidden = registry.getExcludedConfigurationProperties(f.filterA);
    expect(hidden[f.key('hiddenA')].default).toBe('A-hidden'); expect(hidden[f.key('hiddenB')]).toBeUndefined();
    expect(hidden[f.key('hiddenA')].experiment.mode).toBe('startup');
    a[f.key('object')].default.base = false; a[f.key('object')].defaultValueSource.get(`${f.key('object')}.shared`).id = 'changed';
    a[f.key('array')].default.push('changed'); a[f.key('ownA')].enum.push('changed');
    a[f.key('ownA')].source.id = 'changed'; a[`[${f.language}]`].default[f.key('object')].common = false;
    expect(registry.getConfigurationProperties()).toBe(window);
    expect(window[f.key('object')].default.base).toBe(true); expect(window[f.key('array')].default).toEqual(['B']);
    expect(window[f.key('ownA')].enum).toEqual(['first', 'second']); expect(f.defaultsA.source.id).toBe(f.a.id);
    expect(registry.getConfigurationDefaultsOverrides()).toEqual(before); expect(changes).toBe(0);
    expect(registry.getConfigurationProperties(f.filterA)[f.key('array')].default).toEqual(['A']);
  } finally { for (const event of events) event.dispose(); }
  observations.push('隐藏experiment同源过滤、派生对象隔离、原默认贡献不变且零事件');
});

test('原删除入口立即更新目标投影，移除 schema 后不保留旧目标成员', () => {
  const f = fixture();
  f.removeDefaults(f.defaultsB);
  const a = registry.getConfigurationProperties(f.filterA), b = registry.getConfigurationProperties(f.filterB);
  expect(a[f.key('value')].default).toBe('A'); expect(b[f.key('value')].default).toBe('common');
  expect(b[f.key('object')].default).toEqual({ base: true, shared: 'base', common: true });
  expect(b[`[${f.languageB}]`]).toBeUndefined();
  expect(b[`[${f.language}]`].default).toEqual({ [f.key('value')]: 'common-language', [f.key('object')]: { common: true } });
  f.removeNode(f.nodeB);
  expect(registry.getConfigurationProperties(f.filterB)[f.key('ownB')]).toBeUndefined();
  expect(registry.getExcludedConfigurationProperties(f.filterB)[f.key('hiddenB')]).toBeUndefined();
  expect(registry.getConfigurationProperties()[f.key('value')].default).toBe('A');
  observations.push('原deregister默认贡献、语言节点和schema均无旧目标残留');
});

test('原 DefaultConfiguration 无参消费继续订阅全窗口默认值和语言变化', async () => {
  const f = fixture(), configuration = new m.DefaultConfiguration(new m.NullLogService());
  try {
    await configuration.initialize();
    expect(configuration.configurationModel.getValue(f.key('value'))).toBe('B');
    expect(configuration.configurationModel.getOverrideValue(f.key('value'), f.language)).toBe('B-language');
    f.removeDefaults(f.defaultsB);
    expect(configuration.configurationModel.getValue(f.key('value'))).toBe('A');
    expect(configuration.configurationModel.getOverrideValue(f.key('value'), f.language)).toBe('A-language');
    f.removeDefaults(f.defaultsA);
    expect(configuration.configurationModel.getValue(f.key('value'))).toBe('common');
    expect(configuration.configurationModel.getOverrideValue(f.key('value'), f.language)).toBe('common-language');
  } finally { configuration.dispose(); }
  observations.push('原DefaultConfiguration初始化、注册事件与语言/普通默认删除');
});

test('显式替换复用同设置的原 policy，原 delta 一次发布新 schema 且删除旧 policy', () => {
  const f = fixture({ withPolicy: true }), property = f.key('ownA');
  const oldPolicy = f.nodeA.properties[property].policy.name;
  const samePolicy = { type: 'number', default: 7, scope: m.ConfigurationScope.MACHINE, policy: { name: oldPolicy } };
  expect(m.validateProperty(property, samePolicy, f.a.id)).toContain('already registered');
  expect(m.validateProperty(property, samePolicy, f.a.id, true)).toBeNull();
  let events = 0;
  const listener = registry.onDidUpdateConfiguration(() => events++);
  try {
    const replacement = { id: f.a.id, extensionInfo: f.a, properties: { [property]: samePolicy } };
    f.replaceNode(f.nodeA, replacement);
    expect(registry.getConfigurationProperties(f.filterA)[property]).toMatchObject({ type: 'number', default: 7,
      scope: m.ConfigurationScope.MACHINE, policy: { name: oldPolicy } });
    expect(registry.getPolicyConfigurations().get(oldPolicy)).toBe(property);
    expect(events).toBe(1);
    const newPolicy = `${oldPolicy}New`, next = { ...samePolicy, default: 11, policy: { name: newPolicy } };
    expect(m.validateProperty(property, next, f.a.id, true)).toBeNull();
    f.replaceNode(replacement, { id: f.a.id, extensionInfo: f.a, properties: { [property]: next } });
    expect(registry.getPolicyConfigurations().has(oldPolicy)).toBe(false);
    expect(registry.getPolicyConfigurations().get(newPolicy)).toBe(property);
    expect(registry.getConfigurationProperties(f.filterA)[property].default).toBe(11);
    expect(events).toBe(2);
  } finally { listener.dispose(); }
  observations.push('同设置policy显式替换、单delta事件、新schema与旧policy清理');
});

test('替换参数不豁免其它设置占用的 policy、双声明或保留语言属性', () => {
  const f = fixture({ withPolicy: true }), property = f.key('ownA'), other = f.key('ownB');
  const otherPolicy = f.nodeB.properties[other].policy.name;
  const candidate = { type: 'string', default: 'candidate', policy: { name: otherPolicy } };
  expect(m.validateProperty(property, candidate, f.a.id, true)).toContain('already registered with');
  expect(m.validateProperty(f.key('new'), candidate, f.a.id, true)).toContain('already registered with');
  expect(m.validateProperty(property, { ...candidate, policyReference: { name: otherPolicy } }, f.a.id, true)).toContain("both 'policy' and 'policyReference'");
  expect(m.validateProperty(`[${f.language}]`, { type: 'object' }, f.a.id, true)).toContain('Use \'configurationDefaults\'');
  expect(m.validateProperty(other, { type: 'string' }, f.a.id)).toContain('already registered');
  expect(registry.getPolicyConfigurations().get(otherPolicy)).toBe(other);
  expect(registry.getConfigurationProperties()[property].default).toBe('A-schema');
  observations.push('替换时其它policy owner、双policy声明、语言保留键和普通重复仍拒绝');
});
