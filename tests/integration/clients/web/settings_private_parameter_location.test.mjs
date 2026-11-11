import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { parse, parseTree } from '../../../../src/clients/workbench/upstream/src/vs/base/common/json.js';
import { applyEdits, setProperty } from '../../../../src/clients/workbench/upstream/src/vs/base/common/jsonEdit.js';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/settings_private_parameter_location.test.mjs').prepare();
const path = 'src/clients/workbench/src/settings/pluginConfiguration.ts';
const source = await readFile(path, 'utf8');
const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
const names = ['privateParameterLocation', 'updateJsonc', 'containsWriteOnly', 'writeOnlyFields', 'sourceBindings', 'bindingsAt', 'bindingJsonc'];
const code = names.map(name => {
  const node = parsed.statements.find(item => item.name?.text === name);
  if (!node) throw new Error(`实际源码缺少 ${name}`);
  return ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, '');
}).join('\n');
const factory = new Function('parse', 'parseTree', 'applyEdits', 'setProperty', 'sameBinding',
  `${ts.transpile(code, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None })}\nreturn { privateParameterLocation, updateJsonc, writeOnlyFields, bindingJsonc };`);
const { privateParameterLocation, updateJsonc, writeOnlyFields, bindingJsonc } = factory(parse, parseTree, applyEdits, setProperty,
  (a, b) => a.package_id === b.package_id && a.plugin_id === b.plugin_id);
afterAll(() => writeFile(join(run.artifactsDir, 'source-fingerprint.json'), JSON.stringify({ path,
  sha256: createHash('sha256').update(source).digest('hex'),
}, null, 2) + '\n'));

const binding = { package_id: 'probe.package', plugin_id: 'probe', package_version: '1.0.0' };
const wrap = value => ({ plugins: { configuration: { values: { [binding.package_id]: { [binding.plugin_id]: value } } } } });
const document = value => ({ jsonc: JSON.stringify(wrap(value)), source_effective: wrap(value) });

test('数组秘密引用按原 JSONC 数字索引保存和清除，保留兄弟及转义字段名', () => {
  const doc = document({ entries: [{ 'a/b~c': 'old-reference', label: 'first' }, { label: 'sibling' }] });
  const location = privateParameterLocation(doc, binding, '/entries/0/a~1b~0c');
  expect(location.path.slice(-3)).toEqual(['entries', 0, 'a/b~c']);
  expect(location.current).toBe('old-reference');
  const reference = { kind: 'plugin_private_file', parameter_pointer: '/entries/0/a~1b~0c', version: 'new' };
  const saved = parse(updateJsonc(doc.jsonc, location.path, reference));
  const value = saved.plugins.configuration.values[binding.package_id][binding.plugin_id];
  expect(value.entries).toEqual([{ 'a/b~c': reference, label: 'first' }, { label: 'sibling' }]);
  const cleared = parse(updateJsonc(JSON.stringify(saved), location.path, undefined));
  expect(cleared.plugins.configuration.values[binding.package_id][binding.plugin_id].entries)
    .toEqual([{ label: 'first' }, { label: 'sibling' }]);
});

test('对象的数字键仍是字符串，数组只接受已有的规范索引', () => {
  expect(privateParameterLocation(document({ entries: { '01': {} } }), binding, '/entries/01/token').path.slice(-2))
    .toEqual(['01', 'token']);
  for (const index of ['01', '-1', '١', '2', '-']) {
    expect(() => privateParameterLocation(document({ entries: [{}, {}] }), binding, `/entries/${index}/token`))
      .toThrow('已有数组项');
  }
  for (const pointer of ['', 'entries/0/token', '/entries/0/~2']) {
    expect(() => privateParameterLocation(document({}), binding, pointer)).toThrow('JSON Pointer');
  }
});

test('下层数组必须由用户明确整体覆盖，不能误建对象并丢失兄弟项', () => {
  const doc = { jsonc: JSON.stringify(wrap({})), source_effective: wrap({ entries: [{ label: 'lower' }] }) };
  expect(() => privateParameterLocation(doc, binding, '/entries/0/token')).toThrow('完整数组');
});

test('pending 数组结构沿来源合并解析，不借 active 的旧类型决定编辑路径', () => {
  const doc = { jsonc: JSON.stringify(wrap({})),
    source_effective: wrap({ entries: [{}] }), effective: wrap({ entries: {} }) };
  expect(() => privateParameterLocation(doc, binding, '/entries/0/token')).toThrow('完整数组');
  const objectSource = { ...doc, source_effective: wrap({ entries: {} }), effective: wrap({ entries: [{}] }) };
  expect(privateParameterLocation(objectSource, binding, '/entries/0/token').path.slice(-3)).toEqual(['entries', '0', 'token']);
});

test('参数入口按来源期望版本校验，pending 换版与解绑不受旧 active 绑定阻挡', async () => {
  const node = parsed.statements.find(item => item.name?.text === 'manageConfig');
  const manageCode = ts.transpile(ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, ''),
    { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
  const payload = entries => ({ plugins: { runtime: { entries } } });
  const current = { ...binding, package_version: '2.0.0', enabled: false };
  const doc = { writable: true, source_effective: payload([current]),
    effective: payload([{ ...binding, enabled: true }]), jsonc: '{}' };
  let parameterPrompts = 0;
  const owner = { async list() { return { sources: [] }; }, async read() { return doc; } };
  const manage = new Function('ConfigurationSourceOwner', 'chooseSource', 'bindingsAt', 'sameBinding', 'parse',
    `${manageCode}\nreturn manageConfig;`)(class { constructor() { return owner; } }, async () => ({ source_key: 'workspace' }),
    (value, keys) => keys.reduce((current, key) => current[key], value),
    (a, b) => a.package_id === b.package_id && a.plugin_id === b.plugin_id, parse);
  const edit = version => manage('workspace', { gateway_id: 'gateway-a' }, { gateway_id: 'gateway-a', workspace_id: 'workspace-a' },
    { async pick() { parameterPrompts++; return undefined; } }, {}, { ...binding, package_version: version },
    null, true, undefined, () => {}, {}, new AbortController().signal);
  await edit('2.0.0');
  expect(parameterPrompts).toBe(1);
  await expect(edit('1.0.0')).rejects.toThrow('当前绑定使用 2.0.0');
  expect(parameterPrompts).toBe(1);
  doc.source_effective = payload([]);
  await edit('2.0.0');
  expect(parameterPrompts).toBe(2);
});

test('复杂 schema 只给具名位置入口，前端不解析分支或建立第二份 schema', () => {
  const schema = { type: 'object', properties: { direct: { type: 'string', writeOnly: true },
    entries: { type: 'array', items: { $ref: '#/$defs/entry' } } },
  $defs: { entry: { allOf: [{ properties: { token: { type: 'integer', writeOnly: true } } }] } } };
  expect(writeOnlyFields(schema)).toEqual([{ pointer: '/direct', schema: schema.properties.direct }, { pointer: '', schema: {} }]);
  expect(writeOnlyFields({ properties: { ordinary: { type: 'string' } } })).toEqual([]);
});

test('显式切版只修改所选来源，保留本层开关及兄弟，不物化其它层', () => {
  for (const path of [['plugins', 'runtime', 'entries'], ['plugins', 'workspace_defaults', 'entries']]) {
    for (const enabled of [false, true]) {
      const sibling = { ...binding, plugin_id: 'sibling', package_version: '4.0.0', enabled: true };
      const payload = entries => ({ plugins: { [path[1]]: { entries } } });
      const doc = { jsonc: JSON.stringify(payload([{ ...binding, package_version: '1.0.0', enabled }, sibling])),
        source_effective: payload([{ ...binding, package_version: '9.0.0', enabled: !enabled }]) };
      const value = parse(bindingJsonc(doc, path, { ...binding, package_version: '2.0.0', enabled: true }, 'preserve'));
      expect(value.plugins[path[1]].entries).toEqual([{ ...binding, package_version: '2.0.0', enabled }, sibling]);
      expect(() => bindingJsonc({ ...doc, jsonc: JSON.stringify(payload([sibling])) }, path, binding, 'preserve'))
        .toThrow('所选来源没有此插件绑定');
    }
  }
  expect(() => bindingJsonc({ jsonc: '{}', source_effective: {} }, ['plugins', 'runtime', 'entries'], binding, 'preserve'))
    .toThrow('所选来源没有此插件绑定');
});

test('单插件开关和解绑不能覆盖选择来源期间已经切换的版本', () => {
  for (const path of [['plugins', 'runtime', 'entries'], ['plugins', 'workspace_defaults', 'entries']]) {
    const changed = { ...binding, package_version: '3.0.0', enabled: true };
    const payload = { plugins: { [path[1]]: { entries: [changed] } } };
    const doc = { jsonc: JSON.stringify(payload), source_effective: payload };
    for (const enabled of [true, false, null]) {
      expect(() => bindingJsonc(doc, path, binding, enabled)).toThrow('插件绑定版本已变化');
    }
    const value = parse(bindingJsonc(doc, path, { ...binding, package_version: '2.0.0' }, 'preserve'));
    expect(value.plugins[path[1]].entries).toEqual([{ ...binding, package_version: '2.0.0', enabled: true }]);
  }
});


test('三类管理范围只读取原正式来源的完整合并期望，不先列目录或混入 active 绑定', async () => {
  const node = parsed.statements.find(item => item.name?.text === 'readPluginBindings');
  const readCode = ts.transpile(ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, ''),
    { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
  const calls = [];
  const desired = { ...binding, package_version: '2.0.0', enabled: false };
  const defaults = { ...binding, package_version: '3.0.0', enabled: true };
  const readBindings = new Function('ConfigurationSourceOwner', 'bindingsAt', `${readCode}\nreturn readPluginBindings;`)(class {
    constructor(target) { this.target = target; }
    async read(source) {
      calls.push({ target: this.target, source });
      return { effective: { plugins: { runtime: { entries: [{ ...binding, enabled: true }] } } },
        source_effective: { plugins: { runtime: { entries: [desired] }, workspace_defaults: { entries: [defaults] } } } };
    }
  }, (value, keys) => keys.reduce((current, key) => current[key], value));
  const gateway = { gateway_id: 'gateway-a', gateway_connection_id: 'relay-b' };
  const workspace = { ...gateway, workspace_id: 'workspace-a' };
  expect(await readBindings('gateway', gateway)).toEqual([desired]);
  expect(await readBindings('workspace', workspace)).toEqual([{ ...desired, workspace_id: 'workspace-a' }]);
  expect(await readBindings('defaults', gateway)).toEqual([defaults]);
  expect(calls).toEqual([{ target: gateway, source: 'inline:0' }, { target: workspace, source: 'inline' },
    { target: gateway, source: 'inline:0' }]);
});

test('批量开关读取来源后拒绝已换版项，继续提交未变化的兄弟且不隐式切版', async () => {
  const node = parsed.statements.find(item => item.name?.text === 'setPluginBindings');
  const batchCode = ts.transpile(ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, ''),
    { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
  for (const scope of ['gateway', 'workspace', 'defaults']) {
    const path = ['plugins', scope === 'defaults' ? 'workspace_defaults' : 'runtime', 'entries'];
    const changed = { ...binding, package_version: '3.0.0', enabled: true };
    const sibling = { ...binding, plugin_id: 'sibling', package_version: '2.0.0', enabled: true };
    const payload = { plugins: { [path[1]]: { entries: [changed, sibling] } } };
    const document = { writable: true, jsonc: JSON.stringify(payload), source_effective: payload };
    const submitted = [], preflights = [];
    const owner = { async list() { return { sources: [] }; }, async read() { return document; },
      intent(_key, jsonc) { return { jsonc }; }, async preflight(intent) { preflights.push(intent); } };
    const batch = new Function('ConfigurationSourceOwner', 'chooseSource', 'bindingsAt', 'sameBinding', 'bindingJsonc', 'sourceBaseline', 'saveSourceEdit', 'sourceStatus',
      `${batchCode}\nreturn setPluginBindings;`)(class { constructor() { return owner; } }, async () => ({ source_key: 'source' }),
      (value, keys) => keys.reduce((current, key) => current[key], value),
      (a, b) => a.package_id === b.package_id && a.plugin_id === b.plugin_id, bindingJsonc, () => ({}),
      async (_owner, intent) => { submitted.push(parse(intent.jsonc)); return { document }; }, () => 'active');
    const target = { gateway_id: 'gateway-a' }, workspace = scope === 'workspace' ? { ...target, workspace_id: 'workspace-a' } : undefined;
    const results = await batch(scope, target, workspace, {}, [binding, sibling], false, () => {}, {}, new AbortController().signal);
    expect(results[0].configuration).toContain('未提交');
    expect(results[0].configuration).toContain('版本已变化');
    expect(results[1].configuration).toBe('active');
    expect(preflights).toHaveLength(1); expect(submitted).toHaveLength(1);
    expect(submitted[0].plugins[path[1]].entries).toEqual([changed, { ...sibling, enabled: false }]);
  }
});
