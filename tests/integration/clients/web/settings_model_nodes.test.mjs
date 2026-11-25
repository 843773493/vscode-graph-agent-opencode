import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { parse, parseTree } from '../../../../src/clients/workbench/upstream/src/vs/base/common/json.js';
import { applyEdits, setProperty } from '../../../../src/clients/workbench/upstream/src/vs/base/common/jsonEdit.js';
import { t } from '../../../../src/clients/shared/core/i18n/index.js';
import { definitionObject, definitionValue, localProfilePath } from '../../../../src/clients/web/src/components/settings/ConfigurationEntities.js';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/settings_model_nodes.test.mjs').prepare();
const paths = ['src/clients/workbench/src/settings/entities.tsx', 'src/clients/workbench/src/settings/editor.ts'];
const sources = await Promise.all(paths.map(path => readFile(path, 'utf8')));
const printer = ts.createPrinter();
const methods = (index, name, names) => {
  const file = ts.createSourceFile(paths[index], sources[index], ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const owner = file.statements.find(item => item.name?.text === name);
  return names.map(name => {
    const method = owner.members.find(item => item.name?.text === name);
    if (!method) throw new Error(`实际源码缺少 ${name}`);
    return printer.printNode(ts.EmitHint.Unspecified, method, file);
  }).join('\n');
};
const compile = source => ts.transpile(source, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
let uuid = 0;
const Editor = new Function('definitionObject', 'definitionValue', 'localProfilePath', 'generateUuid', 't', compile(`class Editor {
${methods(0, 'ConfigurationEntityView', ['nodeKey', 'rememberNode', 'nodeReady', 'prepareNodes', 'prepareNode', 'editNode', 'editNodeFields', 'commitConnection', 'createModelNode', 'copyNode', 'copyProvider'])}
}
`) + '\nreturn Editor;')(definitionObject, definitionValue, localProfilePath, () => `uuid-${++uuid}`, t);
const Source = new Function('parseTree', 'applyEdits', 'setProperty', 't', compile(`class Source {
${methods(1, 'SourceSettings', ['editDocumentPaths'])}
}
`) + '\nreturn Source;')(parseTree, applyEdits, setProperty, t);

afterAll(() => writeFile(join(run.artifactsDir, 'source-fingerprint.json'), JSON.stringify({ files: paths.map((path, index) => ({
  path, sha256: createHash('sha256').update(sources[index]).digest('hex'),
})) }, null, 2) + '\n'));

const uri = 'boxteam://user/local/resources/config/workspace';
const nodeId = source => `${source}|${uri}#/profiles/original`;
function fixture({ shared = false, owner = 'workspace', document, modelId, parentRef = 'context:workspace' } = {}) {
  let raw = JSON.stringify(document ?? { config_version: 4, extends: 'user:workspace_local', settings: { workspace: { llm: { providers: {
    service: { config_ref: '#/profiles/original', models: { main: { model: 'upstream', config_ref: '#/profiles/original' } } },
    sibling: { config_ref: '#/profiles/original', models: {} },
  } } } }, profiles: { original: { extends: parentRef, settings: { model_options: { endpoint: 'https://old.example/v1', api_key: 'secret:sec_test' } } } } });
  let version = 1;
  const history = [], selections = [];
  const source = new Source();
  const editor = new Editor();
  const selector = { provider_id: 'service', ...(modelId ? { model_id: modelId } : {}) };
  const inspection = { provider_id: 'service', model_ref: selector, node_id: nodeId(owner), shared, config_ref: '#/profiles/original',
    chain: [{ node_id: 'parent', source_key: 'user', source_uri: uri, definition_path: '', reference: 'context:workspace' },
      { node_id: nodeId(owner), parent_id: 'parent', source_key: owner, source_uri: uri, definition_path: '/profiles/original', reference: 'user:workspace#/profiles/original' }],
    options: { endpoint: 'https://old.example/v1', custom_llm_provider: 'openai' },
    local_settings: { model_options: { endpoint: 'https://old.example/v1', api_key: 'secret:sec_test' } },
  };
  Object.assign(source, { document: { workspace_id: 'workspace-test', writable: true, source_key: 'workspace', source_baseline: { workspace: { uri } } }, readDraft: () => parse(raw),
    model: { textEditorModel: { getValue: () => raw, getVersionId: () => version, getFullModelRange: () => ({}),
      pushEditOperations(_selection, edits) { history.push(raw); raw = edits[0].text; version++; if (!editor.editingNode) editor.nodeTargets.clear(); } } },
    async preflightDraft(_signal, selection) { selections.push(selection); return { candidate: parse(raw).settings.workspace,
      provider_options: inspection, model_options: inspection }; },
  });
  Object.assign(editor, { source, nodeTargets: new Map(), connectionEdits: new Map(), connectionCommits: new Map(),
    lifetime: { signal: new AbortController().signal }, check() {}, render() {}, input: async () => '复制服务' });
  source.inspectOptions = (...args) => source.preflightDraft(...args);
  return { editor, source, selector, inspection, history, selections, get value() { return parse(raw); },
    externalEdit(edit) { raw = JSON.stringify(edit(parse(raw))); version++; editor.nodeTargets.clear(); },
    undo() { raw = history.pop(); version++; editor.nodeTargets.clear(); } };
}

test('共享节点首写创建私有子节点和精确目录引用，一次undo恢复整个操作', async () => {
  const state = fixture({ shared: true }); const before = state.value;
  await state.editor.prepareNodes(state.selector, new AbortController().signal);
  state.editor.editNode(state.selector, ['model_options', 'endpoint'], 'https://private.example/v1');
  const value = state.value, reference = value.settings.workspace.llm.providers.service.config_ref;
  expect(reference).not.toBe('#/profiles/original');
  const created = value.profiles[reference.slice('#/profiles/'.length)];
  expect(created).toEqual({ extends: 'user:workspace#/profiles/original', settings: { model_options: { endpoint: 'https://private.example/v1' } } });
  expect(value.profiles.original).toEqual(before.profiles.original);
  expect(value.settings.workspace.llm.providers.sibling).toEqual(before.settings.workspace.llm.providers.sibling);
  expect(value.settings.workspace.llm.providers.service.models).toEqual(before.settings.workspace.llm.providers.service.models);
  expect(state.history).toHaveLength(1); state.undo(); expect(state.value).toEqual(before);
});

test('外部Model首写只改变指定Model引用，连续编辑复用私有节点并顺序保留最后值', async () => {
  const state = fixture({ owner: 'user', modelId: 'main' });
  await state.editor.prepareNodes(state.selector, new AbortController().signal);
  state.editor.editNode(state.selector, ['model_options', 'endpoint'], 'https://one.example/v1');
  state.editor.editNode(state.selector, ['model_options', 'endpoint'], 'https://two.example/v1');
  const provider = state.value.settings.workspace.llm.providers.service;
  expect(provider.config_ref).toBe('#/profiles/original');
  expect(provider.models.main.config_ref).not.toBe('#/profiles/original');
  const profile = state.value.profiles[provider.models.main.config_ref.slice('#/profiles/'.length)];
  expect(profile.settings.model_options.endpoint).toBe('https://two.example/v1');
  expect(state.selections).toEqual([{ provider_id: 'service', model_id: 'main' }]);
  expect(Object.keys(state.value.profiles)).toHaveLength(2);
});

test('本地独占节点直接修改，任意外部原文变化后必须重新取得共享身份', async () => {
  const state = fixture();
  await state.editor.prepareNodes(state.selector, new AbortController().signal);
  state.editor.editNode(state.selector, ['model_options', 'endpoint'], 'https://local.example/v1');
  expect(Object.keys(state.value.profiles)).toEqual(['original']);
  state.externalEdit(value => value); state.inspection.shared = true;
  await state.editor.prepareNodes(state.selector, new AbortController().signal);
  state.editor.editNode(state.selector, ['model_options', 'api_key'], '');
  expect(state.selections).toHaveLength(2);
  expect(state.value.profiles.original.settings.model_options.api_key).toBe('secret:sec_test');
  expect(state.value.settings.workspace.llm.providers.service.config_ref).not.toBe('#/profiles/original');
});

test('父边可连续编辑，独立streaming写入同一Model节点', async () => {
  const state = fixture({ modelId: 'main' });
  await state.editor.prepareNodes(state.selector, new AbortController().signal);
  state.editor.editNode(state.selector, ['extends'], 'user:');
  await state.editor.prepareNodes(state.selector, new AbortController().signal);
  state.editor.editNode(state.selector, ['extends'], 'user:workspace#/profiles/base');
  await state.editor.prepareNodes(state.selector, new AbortController().signal);
  state.editor.editNode(state.selector, ['model_options', 'api_mode', 'streaming'], false);
  expect(state.value.profiles.original.extends).toBe('user:workspace#/profiles/base');
  expect(state.value.profiles.original.settings.model_options.api_mode.streaming).toBe(false);
  expect(state.selections).toHaveLength(1);
});

test.each([undefined, 'main'])('目录自动获取只跟随Provider连接编辑，参数和Model覆盖不触发：%s', async modelId => {
  const state = fixture({ modelId });
  await state.editor.prepareNodes(state.selector, new AbortController().signal);
  for (const [path, value] of [
    [['model_options', 'request_options', 'parameters', 'temperature'], 0],
    [['model_options', 'api_mode', 'streaming'], false],
    [['model_options', 'api_mode', 'model_info', 'supports_reasoning'], false],
    [['model_options', 'api_mode', 'supports_reasoning'], {}],
  ]) state.editor.editNode(state.selector, path, value);
  state.editor.commitConnection('service');
  expect(state.editor.connectionCommits.get('service') ?? 0).toBe(0);
  for (const [path, value] of [
    [['model_options', 'endpoint'], 'https://next.example/v1'],
    [['model_options', 'api_mode', 'protocol'], 'responses'],
    [['model_options', 'request_options', 'default_headers'], { 'X-Catalog': 'next' }],
    [['network', 'proxy'], { mode: 'direct' }],
  ]) state.editor.editNode(state.selector, path, value);
  state.editor.commitConnection('service');
  expect(state.editor.connectionCommits.get('service') ?? 0).toBe(modelId === undefined ? 4 : 0);
  const committed = state.editor.connectionCommits.get('service');
  state.editor.commitConnection('service');
  expect(state.editor.connectionCommits.get('service')).toBe(committed);
});

test('新Model有独立稳定profile并显式继承Provider上下文，不复制effective选项', () => {
  const state = fixture();
  state.editor.createModelNode('service', 'new-model', { model: 'new-upstream' });
  const model = state.value.settings.workspace.llm.providers.service.models['new-model'];
  expect(model.model).toBe('new-upstream');
  expect(state.value.profiles[model.config_ref.slice('#/profiles/'.length)]).toEqual({ extends: 'context:provider/service', settings: {} });
  expect(state.history).toHaveLength(1);
});

test('未填完整连接的新Provider增加Model后仍可同步补端点，不依赖失败预检解锁', () => {
  const state = fixture();
  state.editor.rememberNode({ provider_id: 'service' }, ['profiles', 'original'], {});
  state.source.inspectOptions = async () => { throw new Error('Provider尚未填完连接'); };
  state.editor.createModelNode('service', 'new-model', { model: 'new-upstream' });
  expect(state.editor.nodeReady({ provider_id: 'service' })).toBe(true);
  state.editor.editNode({ provider_id: 'service' }, ['model_options', 'endpoint'], 'https://ready.example/v1');
  expect(state.value.profiles.original.settings.model_options.endpoint).toBe('https://ready.example/v1');
});

test('复制节点只复制本地定义并保留明确父边，禁止复制脱敏占位符', () => {
  const state = fixture({ parentRef: 'user:workspace#/profiles/custom' });
  expect(state.editor.copyNode(state.inspection)).toEqual({ extends: 'user:workspace#/profiles/custom', settings: state.inspection.local_settings });
  state.inspection.local_settings.model_options.api_key = 'boxteam-redacted:/profiles/original/settings/model_options/api_key';
  expect(() => state.editor.copyNode(state.inspection)).toThrow(t('configuration.node.copySecret'));
});

test('复制Provider为每个Model创建独立节点，明确外部父边不因解析成同节点而改写', async () => {
  const state = fixture({ parentRef: 'context:provider/service' });
  state.inspection.local_settings = { model_options: { endpoint: 'https://local.example/v1' } };
  const modelInspection = { ...state.inspection, node_id: 'model-node', chain: [
    ...state.inspection.chain, { node_id: 'model-node', parent_id: state.inspection.node_id, source_key: 'user', source_uri: uri, definition_path: '/profiles/model', reference: 'user:workspace#/profiles/model' },
  ] };
  state.source.preflightDraft = async (_signal, selector) => ({ candidate: state.value.settings.workspace,
    provider_options: state.inspection, model_options: selector.model_id ? modelInspection : undefined });
  await state.editor.copyProvider('service');
  const providers = state.value.settings.workspace.llm.providers;
  const copyId = Object.keys(providers).find(id => !['service', 'sibling'].includes(id));
  expect(copyId).toBeDefined();
  const copy = providers[copyId], copiedProvider = state.value.profiles[copy.config_ref.slice('#/profiles/'.length)];
  expect(copy.config_ref).not.toBe(providers.service.config_ref);
  expect(copy.models.main.config_ref).not.toBe(providers.service.models.main.config_ref);
  expect(copiedProvider.settings).toEqual(state.inspection.local_settings);
  const copiedModel = state.value.profiles[copy.models.main.config_ref.slice('#/profiles/'.length)];
  expect(copiedModel.extends).toBe('user:workspace#/profiles/original');
  expect(copiedModel.extends).not.toBe(`context:provider/${copyId}`);
  expect(state.history).toHaveLength(1);
});

test('节点预检失败向调用者抛出且不写入，后续明确编辑仍可继续', async () => {
  const state = fixture(), before = state.value;
  const preflight = state.source.preflightDraft;
  state.source.preflightDraft = async () => { throw new Error('来源基线已变化'); };
  await expect(state.editor.prepareNodes(state.selector, new AbortController().signal)).rejects.toThrow('来源基线已变化');
  expect(() => state.editor.editNode(state.selector, ['model_options', 'endpoint'], 'https://failed.example/v1')).toThrow();
  expect(state.value).toEqual(before); expect(state.history).toHaveLength(0);
  state.source.preflightDraft = preflight;
  await state.editor.prepareNodes(state.selector, new AbortController().signal);
  state.editor.editNode(state.selector, ['model_options', 'endpoint'], 'https://retry.example/v1');
  expect(state.value.profiles.original.settings.model_options.endpoint).toBe('https://retry.example/v1');
});

test('准备完成后应用同步落入原文，紧接着保存读取的正文包含完整私有节点', async () => {
  const state = fixture({ owner: 'user', modelId: 'main' });
  await state.editor.prepareNodes(state.selector, new AbortController().signal);
  const result = state.editor.editNode(state.selector, ['model_options', 'request_options', 'parameters', 'temperature'], 0.25);
  // 不等待微任务；这就是原文本保存入口在同一次用户操作后看到的正文。
  expect(result).toBeUndefined();
  const saved = JSON.parse(state.source.model.textEditorModel.getValue());
  const ref = saved.settings.workspace.llm.providers.service.models.main.config_ref;
  expect(saved.profiles[ref.slice('#/profiles/'.length)].settings.model_options.request_options.parameters.temperature).toBe(0.25);
  expect(saved.profiles.original.settings.model_options.endpoint).toBe('https://old.example/v1');
  expect(state.history).toHaveLength(1);
});

test('准备尚未完成或原文已变时不能编辑，旧回执不会重新开放旧目标', async () => {
  const state = fixture({ shared: true });
  let complete;
  state.source.inspectOptions = () => new Promise(resolve => { complete = resolve; });
  const prepared = state.editor.prepareNodes(state.selector, new AbortController().signal);
  expect(() => state.editor.editNode(state.selector, ['model_options', 'endpoint'], 'wrong')).toThrow();
  state.externalEdit(value => value);
  complete({ provider_options: state.inspection });
  await expect(prepared).rejects.toThrow(t('configuration.entities.definitionChanged', 'service'));
  expect(state.editor.nodeReady(state.selector)).toBe(false);
  expect(state.history).toHaveLength(0);
});


test('复制直接继承Provider的Model只保留上下文边，不复制Provider定义', () => {
  const state = fixture({ modelId: 'main' });
  state.inspection.config_ref = 'context:provider/service';
  expect(state.editor.copyNode(state.inspection)).toEqual({ extends: 'context:provider/service', settings: {} });
});
