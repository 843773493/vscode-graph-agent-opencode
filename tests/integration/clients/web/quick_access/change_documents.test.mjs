import { afterAll, expect, spyOn, test } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/change_documents.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'change-documents.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
await build({ stdin: { contents: `
export { ChangeDocuments } from './src/clients/workbench/src/changeFiles.js';
export { changeFileUri, changeFileSource, normalizeReadonlyInput, ReadonlyDocumentSerializer } from './src/clients/workbench/src/readonlyDocuments.js';
export { gateway } from './src/clients/workbench/src/agentApi.js';
export { installSessionApiFetchMock, unwrapSessionApiFetch } from './src/clients/web/src/api/session/sessionApiFetchMock.js';
export { TextModelResolverService } from '${upstream}workbench/services/textmodelResolver/common/textModelResolverService.js';
export { ITextFileService } from '${upstream}workbench/services/textfile/common/textfiles.js';
export { IFileService } from '${upstream}platform/files/common/files.js';
export { ILanguageDetectionService } from '${upstream}workbench/services/languageDetection/common/languageDetectionWorkerService.js';
export { IAccessibilityService } from '${upstream}platform/accessibility/common/accessibility.js';
export { ILanguageService } from '${upstream}editor/common/languages/language.js';
export { IModelService } from '${upstream}editor/common/services/model.js';
export { InstantiationService } from '${upstream}platform/instantiation/common/instantiationService.js';
export { ServiceCollection } from '${upstream}platform/instantiation/common/serviceCollection.js';
export { URI } from '${upstream}base/common/uri.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const owner = { gatewayId: 'gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', workspaceId: '019a0000-0000-7000-8000-000000000001', sessionId: 'session-change' };
const otherGateway = 'gateway_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', otherWorkspace = '019a0000-0000-7000-8000-000000000002';
const ownerKey = `${owner.gatewayId}/${owner.workspaceId}`;
const file = { file_path: `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root/中文%20%25.txt`, latest_edit_id: 'edit-1' };
const uri = (phase = 'before', target = owner, revision = 'edit-1', changeset = 'all') =>
  m.changeFileUri(target, changeset, { ...file, file_path: file.file_path.replace(owner.gatewayId, target.gatewayId)
    .replace(owner.workspaceId, target.workspaceId), latest_edit_id: revision }, phase);
const receipt = { boundary: '真实内容Provider、TextModelResolverService及TextResourceEditorModel；仅HTTP、路由、底层文本存取为计数装具', checks: [] };

async function fixture(callback) {
  const texts = new Map(), created = [], destroyed = [], calls = [], refs = [];
  const state = { latest: 'edit-1', fail: false, kind: 'modify', missing: false, paused: undefined, responseSession: owner.sessionId };
  const models = {
    onModelRemoved: m.Event.None,
    getModel(resource) { return texts.get(resource.toString()) ?? null; },
    createModel(content, _language, resource) {
      if (texts.has(resource.toString())) throw new Error('重复创建同一文本模型');
      const disposing = new m.Emitter();
      const model = { uri: resource, getValue: () => content, onWillDispose: disposing.event,
        dispose() { disposing.fire(); disposing.dispose(); } };
      texts.set(resource.toString(), model); created.push(resource.toString());
      return model;
    },
    destroyModel(resource) {
      const model = texts.get(resource.toString());
      if (model) { texts.delete(resource.toString()); destroyed.push(resource.toString()); model.dispose(); }
    },
  };
  const files = { hasProvider: () => false, async activateProvider() { throw new Error('只读快照不应激活文件Provider'); } };
  const languages = { createByFilepathOrFirstLine: () => ({}) };
  const services = new m.ServiceCollection([m.IModelService, models], [m.ILanguageService, languages], [m.IFileService, files],
    [m.ITextFileService, { files: { resolve() { throw new Error('只读快照不应走文件stat/raw链'); } } }],
    [m.ILanguageDetectionService, {}], [m.IAccessibilityService, {}]);
  const instantiation = new m.InstantiationService(services, true);
  const resolver = new m.TextModelResolverService(instantiation, files, { getElements: () => ({ past: [], future: [] }) },
    models, { asCanonicalUri: resource => resource });
  const documents = new m.ChangeDocuments(resolver, models, languages);
  const routes = spyOn(m.gateway, 'resolveWorkspaceTarget').mockImplementation(async target => ({
    gateway_id: target.gatewayId, workspace_id: target.workspaceId, connection_kind: 'local', status: 'ready',
  }));
  m.installSessionApiFetchMock(async ({ path, init }) => {
    const headers = new Headers(init.headers), gatewayId = headers.get('X-BoxTeam-Target-Gateway-Id'), workspaceId = headers.get('X-BoxTeam-Workspace-Id');
    const target = `${gatewayId}/${workspaceId}`;
    calls.push({ path, gatewayId, workspaceId });
    if (state.paused) await state.paused.promise;
    if (state.fail) throw new Error('原更改读取失败');
    return Response.json({ code: 0, message: 'ok', request_id: 'changes-' + calls.length,
      data: { session_id: state.responseSession, changeset_id: decodeURIComponent(path.split('/').at(-1)), files: [{ ...file,
        file_path: file.file_path.replace(owner.gatewayId, gatewayId).replace(owner.workspaceId, workspaceId),
        latest_edit_id: state.latest, kind: state.kind, before_preview: state.missing ? null : target + ' before ' + state.latest,
        after_preview: state.missing ? null : target + ' after ' + state.latest, diff_text: 'patch ' + state.latest }] },
    }, { headers: { 'X-Request-ID': 'changes-' + calls.length } });
  });
  const read = async resource => { const ref = await resolver.createModelReference(resource); refs.push(ref); return ref; };
  try { await callback({ read, state, calls, texts, created, destroyed, routes }); }
  finally {
    state.paused?.resolve();
    for (const ref of refs) ref.dispose();
    await Promise.resolve(); await Promise.resolve();
    documents.dispose(); resolver.dispose(); instantiation.dispose();
    routes.mockRestore(); m.unwrapSessionApiFetch();
  }
}

test('同一快照不受选择键／标题／Thread改变影响，owner、范围、revision和阶段独立', () => {
  const original = uri();
  expect(uri('before', { ...owner, workspaceKey: 'old', threadId: 'child', title: '旧标题' }).toString()).toBe(original.toString());
  expect(m.changeFileSource(m.URI.parse(original.toString()))).toEqual({ owner, changesetId: 'all',
    filePath: file.file_path, editId: 'edit-1', phase: 'before' });
  for (const other of [uri('after'), uri('patch'), uri('before', owner, 'edit-2'), uri('before', owner, 'edit-1', 'turn:one'),
    uri('before', { ...owner, gatewayId: otherGateway }), uri('before', { ...owner, workspaceId: otherWorkspace }),
    uri('before', { ...owner, sessionId: 'session-other' })]) expect(other.toString()).not.toBe(original.toString());
  expect(() => m.changeFileSource(original.with({ query: 'route=old' }))).toThrow('过时路由');
  receipt.checks.push('稳定身份往返；版本／阶段／Gateway／Workspace／Session隔离；旧不完整记录明确失败');
});

test('旧展示引用一次迁为同一模型身份，保留原输入；缺失身份和未知恢复格式明确拒绝', () => {
  const source = { owner: { ...owner, routeId: 'retired-route', title: '旧显示名', threadId: 'old-child' },
    changesetId: 'all', filePath: file.file_path, editId: file.latest_edit_id, phase: 'after' };
  const old = { resource: m.URI.from({ scheme: 'boxteam-change', authority: owner.workspaceId,
    path: '/session-change/all/after/file.txt', query: JSON.stringify(source) }), forceFile: true, label: '旧标签' };
  const migrated = m.normalizeReadonlyInput(old);
  expect(migrated.resource.toString()).toBe(uri('after').toString());
  expect(migrated.forceFile).toBeUndefined(); expect(migrated.label).toBe('旧标签'); expect(old.forceFile).toBe(true);
  expect(JSON.parse(old.resource.query)).toEqual(source);
  expect(m.normalizeReadonlyInput(migrated).resource.toString()).toBe(migrated.resource.toString());
  const invalid = { ...old, resource: old.resource.with({ query: JSON.stringify({ ...source, owner: { workspaceId: owner.workspaceId } }) }) };
  expect(() => m.normalizeReadonlyInput(invalid)).toThrow('缺少可核验的稳定身份');
  const serializer = new m.ReadonlyDocumentSerializer();
  expect(() => serializer.deserialize({}, JSON.stringify({ version: 2 }))).toThrow('不支持的只读文档恢复版本');
  expect(() => serializer.deserialize({}, JSON.stringify({ version: 1, resource: m.URI.parse('untitled:/example').toJSON() }))).toThrow('不是项目资源');
  receipt.checks.push('旧引用收敛同一身份且不改原件；只写新格式；未知版本／无稳定身份明确拒绝');
});

test('并发两侧与多引用只读一次变更集，释放一份仍可用，最后释放后重开真实读取', async () => {
  await fixture(async f => {
    f.state.paused = Promise.withResolvers();
    const pending = [f.read(uri()), f.read(uri()), f.read(uri('after'))];
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.calls).toHaveLength(1);
    expect(f.texts.size).toBe(0);
    f.state.paused.resolve();
    const [a, split, after] = await Promise.all(pending);
    expect(a.object).toBe(split.object); expect(a.object).not.toBe(after.object);
    expect(a.object.isReadonly()).toBe(true);
    expect(a.object.textEditorModel.getValue()).toBe(`${ownerKey} before edit-1`);
    expect(after.object.textEditorModel.getValue()).toBe(`${ownerKey} after edit-1`);
    expect(f.created).toHaveLength(2);
    expect(f.calls).toEqual([{ path: '/api/v1/sessions/session-change/changesets/all', gatewayId: owner.gatewayId, workspaceId: owner.workspaceId }]);
    a.dispose(); await Promise.resolve();
    expect(f.texts.size).toBe(2); expect(split.object.textEditorModel.getValue()).toContain('before');
    split.dispose(); after.dispose(); await Promise.resolve(); await Promise.resolve();
    expect(f.texts.size).toBe(0); expect(f.destroyed).toHaveLength(2);
    await f.read(uri()); expect(f.calls).toHaveLength(2);
    receipt.checks.push('真实resolver并发共享／只读／最后引用释放；前后侧共用1次HTTP；关闭后重读');
  });
});

test('旧模型保持原快照，新版本和另一owner分别读取；旧引用重开不退回最新', async () => {
  await fixture(async f => {
    const old = await f.read(uri('after'));
    f.state.latest = 'edit-2';
    const next = await f.read(uri('after', owner, 'edit-2'));
    const foreign = await f.read(uri('after', { ...owner, workspaceId: otherWorkspace }, 'edit-2'));
    const remote = await f.read(uri('after', { ...owner, gatewayId: otherGateway }, 'edit-2'));
    expect(old.object.textEditorModel.getValue()).toBe(`${ownerKey} after edit-1`);
    expect(next.object.textEditorModel.getValue()).toBe(`${ownerKey} after edit-2`);
    expect(foreign.object.textEditorModel.getValue()).toBe(`${owner.gatewayId}/${otherWorkspace} after edit-2`);
    expect(remote.object.textEditorModel.getValue()).toBe(`${otherGateway}/${owner.workspaceId} after edit-2`);
    expect(remote.object).not.toBe(next.object);
    expect(f.calls.map(({ gatewayId, workspaceId }) => ({ gatewayId, workspaceId }))).toEqual([
      { gatewayId: owner.gatewayId, workspaceId: owner.workspaceId }, { gatewayId: owner.gatewayId, workspaceId: owner.workspaceId },
      { gatewayId: owner.gatewayId, workspaceId: otherWorkspace }, { gatewayId: otherGateway, workspaceId: owner.workspaceId },
    ]);
    old.dispose(); await Promise.resolve(); await Promise.resolve();
    await expect(f.read(uri('after'))).rejects.toThrow('此更改版本已变化');
    expect(f.texts.size).toBe(3);
    receipt.checks.push('保留旧模型；新revision／不同工作区／同UUID不同Gateway不混用；过时重开明确失败');
  });
});

test('读取失败不缓存，可重试；创建／删除的缺失侧为空，普通缺正文明确失败', async () => {
  await fixture(async f => {
    f.state.fail = true;
    await expect(f.read(uri())).rejects.toThrow('原更改读取失败');
    f.state.fail = false; await f.read(uri()); expect(f.calls).toHaveLength(2);
    f.state.kind = 'create'; f.state.missing = true;
    const empty = await f.read(uri('before', owner, 'edit-1', 'turn:create'));
    expect(empty.object.textEditorModel.getValue()).toBe('');
    await expect(f.read(uri('after', owner, 'edit-1', 'turn:create'))).rejects.toThrow('后端未提供更改预览');
    f.state.kind = 'delete';
    const deleted = await f.read(uri('after', owner, 'edit-1', 'turn:delete'));
    expect(deleted.object.textEditorModel.getValue()).toBe('');
    receipt.checks.push('失败后重试无负缓存；创建／删除空侧与正文缺失区分');
  });
});

test('停止目标不读取，错误Session响应不创建模型，原原因继续传播', async () => {
  await fixture(async f => {
    f.routes.mockResolvedValueOnce({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, status: 'stopped' });
    await expect(f.read(uri())).rejects.toThrow('工作区当前不可用');
    expect(f.calls).toHaveLength(0);
    f.state.responseSession = 'foreign-session';
    await expect(f.read(uri())).rejects.toThrow('更改响应 owner 不匹配');
    expect(f.texts.size).toBe(0);
    receipt.checks.push('停止目标零读取；错误Session不产生模型／fallback');
  });
});

afterAll(async () => { await writeFile(join(run.artifactsDir, 'change-documents.json'), JSON.stringify(receipt, null, 2) + '\n'); });
