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
    boundary: '真实原ConfigurationRegistry、JSON schema注册与DefaultConfiguration订阅；配置来源用具名A/B贡献建立，不运行Host，不代替配置授权、scanner、完整设置窗口或Gateway验收。',
  }, null, 2));
  await run.close();
  expect(fingerprintsAfter).toEqual(fingerprints);
});

function fixture({ withPolicy = false } = {}) {
  const prefix = `configurationRegistry${++sequence}`, key = suffix => `${prefix}.${suffix}`;
  const a = { id: `fixture.${prefix}.a`, displayName: '配置 A' };
  const b = { id: `fixture.${prefix}.b`, displayName: '配置 B' };
  const language = `${prefix}-shared`;
  const common = { overrides: { [key('value')]: 'common', [`[${language}]`]: { [key('value')]: 'common-language' } } };
  const defaults = source => ({ source, overrides: {
    [key('value')]: source === a ? 'A' : 'B',
    [`[${language}]`]: { [key('value')]: source === a ? 'A-language' : 'B-language' },
  } });
  const defaultsA = defaults(a), defaultsB = defaults(b);
  const node = { id: prefix, properties: {
    [key('value')]: { type: 'string', default: 'base', scope: m.ConfigurationScope.LANGUAGE_OVERRIDABLE },
  } };
  const extensionNode = source => ({ id: source.id, extensionInfo: source, properties: {
    [key(source === a ? 'ownA' : 'ownB')]: { type: 'string', default: source === a ? 'A-schema' : 'B-schema', enum: ['first', 'second'],
      scope: m.ConfigurationScope.RESOURCE, restricted: true, markdownDescription: '原扩展说明',
      ...(withPolicy ? { policy: { name: `${prefix}${source === a ? 'A' : 'B'}Policy` } } : {}) },
  } });
  const nodeA = extensionNode(a), nodeB = extensionNode(b), nodes = [node, nodeA, nodeB];
  const registrations = [common, defaultsA, defaultsB];
  registry.registerConfigurations(nodes);
  registry.registerDefaultConfigurations(registrations);
  cleanups.push(() => { registry.deregisterDefaultConfigurations(registrations); registry.deregisterConfigurations(nodes); });
  return { key, a, b, language, defaultsA, defaultsB, nodeA, nodeB,
    removeDefaults(item) { registry.deregisterDefaultConfigurations([item]); registrations.splice(registrations.indexOf(item), 1); },
    replaceNode(previous, next) {
      registry.deltaConfiguration({ removedConfigurations: [previous], addedConfigurations: [next] });
      nodes.splice(nodes.indexOf(previous), 1, next);
    },
  };
}

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
    expect(registry.getConfigurationProperties()[property]).toMatchObject({ type: 'number', default: 7,
      scope: m.ConfigurationScope.MACHINE, policy: { name: oldPolicy } });
    expect(registry.getPolicyConfigurations().get(oldPolicy)).toBe(property);
    expect(events).toBe(1);
    const newPolicy = `${oldPolicy}New`, next = { ...samePolicy, default: 11, policy: { name: newPolicy } };
    expect(m.validateProperty(property, next, f.a.id, true)).toBeNull();
    f.replaceNode(replacement, { id: f.a.id, extensionInfo: f.a, properties: { [property]: next } });
    expect(registry.getPolicyConfigurations().has(oldPolicy)).toBe(false);
    expect(registry.getPolicyConfigurations().get(newPolicy)).toBe(property);
    expect(registry.getConfigurationProperties()[property].default).toBe(11);
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
