import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { parseTree, findNodeAtLocation } from '../../../../src/clients/workbench/upstream/src/vs/base/common/json.js';
import { MarkerSeverity } from '../../../../src/clients/workbench/upstream/src/vs/platform/markers/common/markers.js';
import { HttpRequestError } from '../../../../src/clients/web/src/api/http.js';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/settings_source_diagnostics.test.mjs').prepare();
const path = 'src/clients/workbench/src/settings/sourceEditor.ts';
const source = await readFile(path, 'utf8');
const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
const owner = parsed.statements.find(node => node.name?.text === 'ConfigurationSourceFiles');
const printer = ts.createPrinter();
const methods = ['preflight', 'showValidation', 'clearValidation'].map(name => {
  const method = owner.members.find(node => node.name?.text === name);
  if (!method) throw new Error(`实际来源编辑器缺少 ${name}`);
  return printer.printNode(ts.EmitHint.Unspecified, method, parsed);
}).join('\n');
const Files = new Function('parseTree', 'findNodeAtLocation', 'MarkerSeverity', 'HttpRequestError', 'validationMarkerOwner', 't',
  ts.transpile(`class Files { ${methods} }`, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None }) + '\nreturn Files;')(
  parseTree, findNodeAtLocation, MarkerSeverity, HttpRequestError, 'boxteam.configSourceValidation', key => key);
afterAll(() => writeFile(join(run.artifactsDir, 'source-fingerprint.json'), JSON.stringify({ path,
  sha256: createHash('sha256').update(source).digest('hex') }, null, 2) + '\n'));

function fixture() {
  const resource = {}, uri = 'boxteam://user/local/resources/config/workspace';
  let jsonc = '{\n  "emoji": "😀",\n  "field.with.dot": [{"value": false}, {"value": 7}]\n}', version = 3;
  const publications = [], removals = [];
  const model = { getVersionId: () => version, getValue: () => jsonc, getPositionAt(offset) {
    const lines = jsonc.slice(0, offset).split('\n'); return { lineNumber: lines.length, column: lines.at(-1).length + 1 };
  } };
  const files = new Files();
  const edit = { sourceKey: 'workspace', baseline: {}, document: { nodes: [{ source_key: 'parent', source_uri: uri + '/parent' },
    { source_key: 'workspace', source_uri: uri }] },
    owner: { intent: (_key, text) => ({ jsonc: text }), preflight: async () => ({}) } };
  Object.assign(files, { edit: () => edit, models: { getModel: key => key === resource ? model : undefined },
    markers: { changeOne: (owner, target, values) => publications.push({ owner, target, values }), remove: (_owner, targets) => removals.push(targets) } });
  const error = (issues, sourceUri = uri) => new HttpRequestError(422, 'Unprocessable Content', {
    code: 'unknown_error', message: '字段类型不正确', details: { source_uri: sourceUri, issues },
  }, '/api/v1/config/sources/workspace/preflight', 'request-original');
  return { files, resource, model, edit, publications, removals, error, get jsonc() { return jsonc; }, get version() { return version; },
    change(text) { jsonc = text; version++; } };
}
const issue = (path, path_exact = true) => ({ path, path_exact, code: 'type', message: '必须是整数' });

test('精确字段段和数组坐标定位原JSONC，不拆含点号键，坐标采用原模型UTF16语义', () => {
  const f = fixture();
  f.files.showValidation(f.resource, f.jsonc, f.model, f.version, f.error([issue(['field.with.dot', 1, 'value'])]));
  expect(f.publications).toHaveLength(1);
  const marker = f.publications[0].values[0], offset = f.jsonc.indexOf('7');
  expect(marker).toMatchObject({ severity: MarkerSeverity.Error, message: '必须是整数', code: 'type', modelVersionId: 3,
    startLineNumber: 3, startColumn: f.model.getPositionAt(offset).column, endColumn: f.model.getPositionAt(offset + 1).column });
});

test('脱敏或未声明精确、union伪段和缺失字段不猜位置；错误仍由原调用者持有', () => {
  const f = fixture(), error = f.error([issue(['field.with.dot', 1, 'value'], false),
    { path: ['field.with.dot', 0, 'value'], message: '未声明精确' }, issue(['union-label', 'value']), issue(['missing'])]);
  f.files.showValidation(f.resource, f.jsonc, f.model, f.version, error);
  expect(f.publications[0].values).toEqual([]);
  expect(error.message).toBe('字段类型不正确');
});

test('其他来源的精确路径不绑定到当前文档，数组重排后的迟到诊断也不定位', () => {
  const f = fixture(), original = f.jsonc, version = f.version;
  f.files.showValidation(f.resource, original, f.model, version, f.error([issue(['field.with.dot', 1, 'value'])], 'boxteam://user/other/resources/config/workspace'));
  f.change(original.replace('[{"value": false}, {"value": 7}]', '[{"value": 7}, {"value": false}]'));
  f.files.showValidation(f.resource, original, f.model, version, f.error([issue(['field.with.dot', 1, 'value'])]));
  expect(f.publications).toHaveLength(0);
});

test('原预检入口保留同一个HTTP错误并发布精确诊断，后续成功清除原markers', async () => {
  const f = fixture(), error = f.error([issue(['field.with.dot', 1, 'value'])]);
  f.edit.owner.preflight = async () => { throw error; };
  await expect(f.files.preflight(f.resource, f.jsonc)).rejects.toBe(error);
  expect(f.publications[0].values).toHaveLength(1);
  f.edit.owner.preflight = async () => ({ accepted: true });
  await expect(f.files.preflight(f.resource, f.jsonc)).resolves.toEqual({ accepted: true });
  expect(f.removals).toEqual([[f.resource]]);
});

test('预检在途编辑使旧诊断失效，URI不变也不能发布到新草稿', async () => {
  const f = fixture(); let reject;
  f.edit.owner.preflight = () => new Promise((_resolve, rejectPromise) => { reject = rejectPromise; });
  const pending = f.files.preflight(f.resource, f.jsonc);
  f.change(f.jsonc + '\n');
  reject(f.error([issue(['field.with.dot', 1, 'value'])]));
  await expect(pending).rejects.toBeInstanceOf(HttpRequestError);
  expect(f.publications).toHaveLength(0);
});


test('同URI同version和正文的重建模型不接受旧实例诊断', () => {
  const f = fixture();
  f.files.models.getModel = () => ({ ...f.model });
  f.files.showValidation(f.resource, f.jsonc, f.model, f.version, f.error([issue(['field.with.dot', 1, 'value'])]));
  expect(f.publications).toHaveLength(0);
});
