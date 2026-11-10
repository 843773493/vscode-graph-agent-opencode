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
const document = value => ({ jsonc: JSON.stringify(wrap(value)), effective: wrap(value) });

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
  const doc = { jsonc: JSON.stringify(wrap({})), effective: wrap({ entries: [{ label: 'lower' }] }) };
  expect(() => privateParameterLocation(doc, binding, '/entries/0/token')).toThrow('完整数组');
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
