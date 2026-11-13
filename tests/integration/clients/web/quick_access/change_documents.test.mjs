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
import './tests/harness/js/workbench-locale.ts';
export { ChangeDocuments } from './src/clients/workbench/src/changeFiles.js';
export { SourceDocuments, sourceDocumentUri } from './src/clients/workbench/src/sourceDocuments.js';
export { changeFileUri, changeFileSource, normalizeReadonlyInput, ReadonlyDocumentSerializer, UnavailableReadonlyDocumentInput } from './src/clients/workbench/src/readonlyDocuments.js';
export { ThreadInput, ThreadSerializer, UnavailableThreadInput } from './src/clients/workbench/src/threadInput.js';
export { setLocaleResources } from './src/clients/shared/core/i18n/index.js';
export { default as english } from './src/clients/shared/core/i18n/messages/en.json';
export { default as chinese } from './src/clients/shared/core/i18n/messages/zh-cn.json';
export { TextResourceEditorInput } from '${upstream}workbench/common/editor/textResourceEditorInput.js';
export { EditorGroupModel } from '${upstream}workbench/common/editor/editorGroupModel.js';
export { EditorExtensions, EditorsOrder } from '${upstream}workbench/common/editor.js';
export { Registry } from '${upstream}platform/registry/common/platform.js';
export { ITextEditorService } from '${upstream}workbench/services/textfile/common/textEditorService.js';
export { gateway } from './src/clients/workbench/src/agentApi.js';
export { workspaceContext } from './src/clients/workbench/src/workspaceContext.js';
export { invalidateGatewayToken } from './src/clients/web/src/api/http.js';
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
m.setLocaleResources('zh-cn', m.chinese);
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
  const state = { latest: 'edit-1', fail: false, kind: 'modify', missing: false, paused: undefined, routePaused: undefined, responseSession: owner.sessionId };
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
  const sources = new m.SourceDocuments(resolver, models, languages);
  const routes = spyOn(m.gateway, 'resolveWorkspaceTarget').mockImplementation(async target => {
    if (state.routePaused) await state.routePaused.promise;
    return { gateway_id: target.gatewayId, workspace_id: target.workspaceId, connection_kind: 'local', status: 'ready' };
  });
  m.installSessionApiFetchMock(async ({ path, init }) => {
    const headers = new Headers(init.headers), gatewayId = headers.get('X-BoxTeam-Target-Gateway-Id'), workspaceId = headers.get('X-BoxTeam-Workspace-Id');
    const target = `${gatewayId}/${workspaceId}`;
    calls.push({ path, gatewayId, workspaceId });
    if (state.paused) await state.paused.promise;
    if (state.fail) throw new Error('原更改读取失败');
    if (path === '/api/v1/resources/navigation') return Response.json({ code: 0, message: 'ok', request_id: 'source-' + calls.length,
      data: { node: { uri: JSON.parse(init.body).uri, resource_shape: 'file' }, content: target + ' source',
        media_type: 'text', content_encoding: 'utf-8', truncated: false } });
    return Response.json({ code: 0, message: 'ok', request_id: 'changes-' + calls.length,
      data: { session_id: state.responseSession, changeset_id: decodeURIComponent(path.split('/').at(-1)), files: [{ ...file,
        file_path: file.file_path.replace(owner.gatewayId, gatewayId).replace(owner.workspaceId, workspaceId),
        latest_edit_id: state.latest, kind: state.kind, before_preview: state.missing ? null : target + ' before ' + state.latest,
        after_preview: state.missing ? null : target + ' after ' + state.latest, diff_text: 'patch ' + state.latest }] },
    }, { headers: { 'X-Request-ID': 'changes-' + calls.length } });
  });
  const read = async resource => { const ref = await resolver.createModelReference(resource); refs.push(ref); return ref; };
  try { await callback({ read, state, calls, texts, created, destroyed, routes, documents }); }
  finally {
    state.paused?.resolve();
    state.routePaused?.resolve();
    for (const ref of refs) ref.dispose();
    await Promise.resolve(); await Promise.resolve();
    documents.dispose(); sources.dispose(); resolver.dispose(); instantiation.dispose();
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

test('公开Diff层级地址经原URI对象往返保留源文件编码，不展开结构斜杠', () => {
  const prefix = `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/changes/ses_${'a'.repeat(32)}/all`;
  for (const path of ['%E4%B8%AD%E6%96%87%20%25%3A%F0%9F%98%80.txt', '%252e%252e.txt', 'folder/file%3F%23.txt']) {
    const reference = `${prefix}/${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root/${path}`;
    expect(m.URI.parse(reference).toString()).toBe(reference);
    expect(m.URI.revive(m.URI.parse(reference).toJSON()).toString()).toBe(reference);
  }
  receipt.checks.push('公开层级URI经过实际上游URI对象及JSON恢复保留中文／空格／字面百分号／保留字符');
});

test('旧展示引用一次迁为同一模型身份，保留原输入；缺失身份和未知恢复格式保留错误记录', () => {
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
  expect(serializer.deserialize({}, JSON.stringify({ version: 2 })).reason).toContain('不支持的只读文档恢复版本');
  expect(serializer.deserialize({}, JSON.stringify({ version: 1, resource: m.URI.parse('untitled:/example').toJSON() })).reason).toContain('不是项目资源');
  receipt.checks.push('旧引用收敛同一身份且不改原件；只写新格式；未知版本／无稳定身份保留为错误记录');
});

test('损坏引用保留原件且重复恢复不嵌套，不调用文件／路由服务', () => {
  const serializer = new m.ReadonlyDocumentSerializer();
  const records = [
    ['{"version":1,', 'JSON 损坏'], ['null', '必须是对象'], ['[]', '必须是对象'],
    [JSON.stringify({ version: 9, name: '未来版本' }), '不支持的只读文档恢复版本'],
    [JSON.stringify({ version: 1 }), '缺少资源身份'],
    [JSON.stringify({ version: 1, resource: { scheme: 'boxteam-source', path: '/missing-owner' } }), '缺少完整归属'],
    [JSON.stringify({ version: 1, resource: uri().with({ path: '/bad-change' }).toJSON() }), '缺少稳定身份'],
    [JSON.stringify({ version: 1, resource: uri().with({ path: '/%FF' }).toJSON() }), 'URI error'],
  ];
  for (const [serialized, reason] of records) {
    let input = serializer.deserialize({}, serialized);
    const original = serialized === '{"version":1,' ? serialized : JSON.parse(serialized);
    for (let round = 0; round < 3; round++) {
      expect(input).toBeInstanceOf(m.UnavailableReadonlyDocumentInput);
      expect(input.resource).toBeUndefined(); expect(input.stored).toEqual(original);
      expect(input.reason).toContain(reason); expect(input.getName()).toBeTruthy();
      expect(serializer.canSerialize(input)).toBe(true);
      input = serializer.deserialize({}, serializer.serialize(input));
    }
  }
  receipt.checks.push('坏JSON／未知版本／无owner／非法编码的原件保留、三次恢复不嵌套、无服务访问');
});

test('原编辑器组保留坏记录及相邻Thread，移除一条后其余顺序和原件仍能恢复', () => {
  const instantiation = new m.InstantiationService(new m.ServiceCollection(), true);
  const registry = m.Registry.as(m.EditorExtensions.EditorFactory);
  const registrations = [registry.registerEditorSerializer(m.TextResourceEditorInput.ID, m.ReadonlyDocumentSerializer),
    registry.registerEditorSerializer(m.ThreadInput.ID, m.ThreadSerializer)];
  instantiation.invokeFunction(accessor => registry.start(accessor));
  const configuration = { onDidChangeConfiguration: m.Event.None, getValue: key => key.endsWith('openPositioning') ? 'right' : true };
  const thread = new m.ThreadInput({ ...owner, workspaceKey: '', threadId: 'exact-child', title: '正常Thread' });
  const stored = { id: 17, editors: [
    { id: m.TextResourceEditorInput.ID, value: '{"version":1,' },
    { id: thread.typeId, value: new m.ThreadSerializer().serialize(thread) },
    { id: m.TextResourceEditorInput.ID, value: JSON.stringify({ version: 9, name: '未来只读文档' }) },
  ], mru: [0, 1, 2], sticky: 0 };
  const group = new m.EditorGroupModel(stored, instantiation, configuration);
  let restored;
  try {
    expect(group.count).toBe(3); expect(group.activeEditor).toBeInstanceOf(m.UnavailableReadonlyDocumentInput);
    const inputs = group.getEditors(m.EditorsOrder.SEQUENTIAL);
    expect(inputs[1].owner).toMatchObject({ ...owner, threadId: 'exact-child' });
    expect(inputs[2].getName()).toBe('未来只读文档');
    expect(group.serialize().editors).toHaveLength(3);
    group.closeEditor(inputs[0]);
    restored = new m.EditorGroupModel(group.serialize(), instantiation, configuration);
    expect(restored.count).toBe(2);
    expect(restored.getEditors(m.EditorsOrder.SEQUENTIAL).map(input => input.getName())).toEqual(['正常Thread', '未来只读文档']);
    expect(restored.getEditors(m.EditorsOrder.SEQUENTIAL)[1].stored).toEqual({ version: 9, name: '未来只读文档' });
    receipt.checks.push('真实EditorGroupModel恢复3条含坏记录；关闭仅移除指定展示记录，余项及精确child保留');
  } finally {
    restored?.dispose(); group.dispose(); thread.dispose();
    for (const registration of registrations) registration.dispose();
    instantiation.dispose();
  }
});

test('Thread 双语恢复错误保留原件和诊断参数，已存原因与用户标题不随语言重写', () => {
  const serializer = new m.ThreadSerializer();
  const title = '用户标题 /home/alice/{0}', reason = '原始原因 C:\\work\\{0}';
  const original = { version: 9, title, message: '用户正文 /tmp/{0}' };
  const badJson = '{"title":"/home/alice/{0}",';
  try {
    for (const [locale, catalog, expectedTitle, expectedError] of [
      ['zh-cn', m.chinese, '无法恢复的 Thread', 'Thread 编辑器恢复 JSON 损坏：'],
      ['en', m.english, 'Unrestorable Thread', 'The Thread editor recovery JSON is corrupt: '],
    ]) {
      m.setLocaleResources(locale, catalog);
      const invalid = serializer.deserialize({}, badJson);
      expect(invalid).toBeInstanceOf(m.UnavailableThreadInput);
      expect(invalid.stored).toBe(badJson);
      expect(invalid.getName()).toBe(expectedTitle);
      expect(invalid.reason).toStartWith(expectedError);
      let syntaxMessage;
      try { JSON.parse(badJson); } catch (error) { syntaxMessage = error.message; }
      expect(invalid.reason.slice(expectedError.length)).toBe(syntaxMessage);
      const future = serializer.deserialize({}, JSON.stringify(original));
      expect(future.stored).toEqual(original);
      expect(future.getName()).toBe(title);
      expect(future.reason).toBe(locale === 'en'
        ? 'Unsupported Thread editor recovery version: 9' : '不支持的 Thread 编辑器恢复版本：9');
      const stored = serializer.deserialize({}, JSON.stringify({ version: 2, unavailable: original, title, reason }));
      const restored = serializer.deserialize({}, serializer.serialize(stored));
      expect(restored.stored).toEqual(original);
      expect(restored.reason).toBe(reason);
      expect(restored.getName()).toBe(title);
      expect(restored.resource).toBeUndefined();
    }
  } finally { m.setLocaleResources('zh-cn', m.chinese); }
  receipt.checks.push('真实ThreadSerializer双语错误／原JSON参数保留；用户标题和已存原因不翻译、不建立新恢复状态');
});

test('Thread 切换启动语言不改变精确身份、分屏展示态或有效序列化结构', () => {
  const serializer = new m.ThreadSerializer();
  const source = new m.ThreadInput({ ...owner, workspaceKey: 'native-route', threadId: 'child-exact', title: '标题 /tmp/{0}' });
  source.restoreViewState({ resourcesVisible: true, resourceTab: 'changes', resourceRatio: 0.35,
    selectedChangesetId: 'turn:stable', filePreviewLayout: {}, fileTreeExpandedPaths: ['/tmp/{0}'] });
  const serialized = serializer.serialize(source);
  const restored = [];
  try {
    for (const [locale, catalog] of [['zh-cn', m.chinese], ['en', m.english]]) {
      m.setLocaleResources(locale, catalog);
      const input = serializer.deserialize({}, serialized);
      const split = input.copy();
      restored.push(input, split);
      expect(input).toBeInstanceOf(m.ThreadInput);
      expect(input.resource.toString()).toBe(source.resource.toString());
      expect(input.owner).toEqual({ ...source.owner, workspaceKey: '' });
      expect(serializer.serialize(input)).toBe(serialized);
      split.fileTreeExpandedPaths.push('/another');
      expect(input.fileTreeExpandedPaths).toEqual(['/tmp/{0}']);
      expect(split.owner).toEqual(input.owner);
      expect(split.getName()).toBe('标题 /tmp/{0}');
    }
  } finally {
    m.setLocaleResources('zh-cn', m.chinese);
    source.dispose();
    for (const input of restored) input.dispose();
  }
  receipt.checks.push('真实ThreadInput／serializer在两语保持同一稳定URI和JSON；copy独立展示态，用户内容不变');
});

test('Thread 在途owner解析共用读取，凭据或Gateway改变后拒绝迟到路由，重开重新解析', async () => {
  const before = m.workspaceContext.getSnapshot();
  try {
    for (const kind of ['credential', 'gateway', 'gateway-returned']) {
      m.workspaceContext.publish({ ...before, gatewayId: owner.gatewayId });
      const input = new m.ThreadInput({ ...owner, workspaceKey: 'old-route', threadId: 'exact-child', title: '原历史' });
      const pending = Promise.withResolvers();
      const routes = spyOn(m.gateway, 'resolveWorkspace').mockImplementation(() => pending.promise);
      try {
        const first = input.resolveOwner(), second = input.resolveOwner();
        const settled = Promise.allSettled([first, second]);
        expect(routes).toHaveBeenCalledTimes(1);
        if (kind === 'credential') m.invalidateGatewayToken(0);
        else {
          m.workspaceContext.publish({ ...before, gatewayId: otherGateway });
          if (kind === 'gateway-returned') m.workspaceContext.publish({ ...before, gatewayId: owner.gatewayId });
        }
        pending.resolve('late-route');
        const results = await settled;
        for (const result of results) {
          expect(result.status).toBe('rejected');
          expect(result.reason.message).toContain('Gateway 或访问凭据已改变');
        }
        expect(input.owner.workspaceKey).toBe('old-route');
        expect(routes.mock.calls[0][1].aborted).toBe(true);
        routes.mockResolvedValue('current-route');
        expect(await input.resolveOwner()).toBe(input.owner);
        expect(input.owner.workspaceKey).toBe('current-route');
        expect(routes).toHaveBeenCalledTimes(2);
      } finally { pending.resolve('cleanup'); routes.mockRestore(); input.dispose(); }
    }
  } finally { m.workspaceContext.publish(before); }
  receipt.checks.push('真实ThreadInput共用在途解析；凭据／Gateway／切回均拒绝迟到路由且不写owner；重开重新解析');
});

test('Thread 启动时首次Gateway发布及同身份登记更新仍沿原路由解析', async () => {
  const before = m.workspaceContext.getSnapshot();
  m.workspaceContext.publish({ ...before, gatewayId: null });
  const input = new m.ThreadInput({ ...owner, workspaceKey: '', threadId: 'exact-child', title: '恢复原记录' });
  const pending = Promise.withResolvers();
  const routes = spyOn(m.gateway, 'resolveWorkspace').mockImplementation(() => pending.promise);
  try {
    const resolving = input.resolveOwner();
    m.workspaceContext.publish({ ...before, gatewayId: owner.gatewayId });
    m.workspaceContext.publish({ ...before, gatewayId: owner.gatewayId, activeId: 'another-workspace' });
    pending.resolve('new-route');
    expect(await resolving).toBe(input.owner);
    expect(input.owner.workspaceKey).toBe('new-route');
    expect(routes).toHaveBeenCalledTimes(1);
    const serialized = new m.ThreadSerializer().serialize(input);
    expect(serialized).not.toContain('new-route');
    expect(JSON.parse(serialized).target).toEqual({ ...owner, threadId: 'exact-child' });
  } finally { pending.resolve('cleanup'); routes.mockRestore(); input.dispose(); m.workspaceContext.publish(before); }
  receipt.checks.push('初次Gateway发布和同身份登记更新不废弃恢复；实际新路由仍不进入序列化');
});

test('有效来源恢复只交原工厂，工厂本身失败保留原错误而不转为坏记录', () => {
  const serializer = new m.ReadonlyDocumentSerializer();
  const resource = m.URI.from({ scheme: 'boxteam-source', authority: owner.gatewayId,
    path: `/${owner.workspaceId}/${encodeURIComponent(file.file_path)}/说明.md` });
  const calls = [], restored = {};
  const instantiation = { invokeFunction: callback => callback({ get: service => {
    expect(service).toBe(m.ITextEditorService);
    return { createTextEditor: input => { calls.push(input); return restored; } };
  } }) };
  const serialized = JSON.stringify({ version: 1, resource: resource.toJSON(), name: '原来源', description: '原展示信息' });
  expect(serializer.deserialize(instantiation, serialized)).toBe(restored);
  expect(calls).toEqual([{ resource, label: '原来源', description: '原展示信息' }]);
  const original = new Error('原输入工厂故障');
  expect(() => serializer.deserialize({ invokeFunction() { throw original; } }, serialized)).toThrow(original);
  receipt.checks.push('有效引用交原TextEditorService；下游基础设施错误不被误判为恢复记录损坏');
});

test('只读来源与更改向原历史交付稳定引用而无模型正文，其他文本输入保持原行为', () => {
  const resources = [uri(), m.URI.from({ scheme: 'boxteam-source', authority: owner.gatewayId,
    path: `/${owner.workspaceId}/${encodeURIComponent(file.file_path)}/说明.md` }), m.URI.parse('unrelated:/original')];
  for (const resource of resources) {
    const input = new m.TextResourceEditorInput(resource, '历史标签', '原来源', undefined, '不应进入历史的正文', {}, {}, {},
      { onDidChangeFileSystemProviderRegistrations: m.Event.None, onDidChangeFileSystemProviderCapabilities: m.Event.None },
      { onDidChangeFormatters: m.Event.None }, { onDidChangeReadonly: m.Event.None }, {}, { onDidChange: m.Event.None });
    try {
      const untyped = input.toUntyped();
      if (resource.scheme === 'unrelated') expect(untyped).toBeUndefined();
      else {
        expect(untyped).toEqual({ resource, label: '历史标签', description: '原来源', options: { override: input.editorId } });
        expect(JSON.stringify(untyped)).not.toContain('不应进入历史的正文');
      }
    } finally { input.dispose(); }
  }
  receipt.checks.push('原输入公开toUntyped只交项目稳定引用；不序列化正文、不改变其它scheme');
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

for (const kind of ['change', 'source']) for (const phase of ['route', 'read']) for (const invalidation of ['credential', 'gateway-returned']) {
  test(`只读恢复${kind}在${phase}中${invalidation}后，迟到正文不创建模型且可重新读取`, async () => {
    const previous = m.workspaceContext.getSnapshot();
    m.workspaceContext.publish({ ...previous, gatewayId: owner.gatewayId });
    try { await fixture(async f => {
      const barrier = Promise.withResolvers();
      f.state[phase === 'route' ? 'routePaused' : 'paused'] = barrier;
      const resource = kind === 'change' ? uri() : m.sourceDocumentUri(owner,
        `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/skills/skill-a/source.md`);
      const pending = f.read(resource).then(() => ({ resolved: true }), error => ({ error: String(error) }));
      await new Promise(resolve => setTimeout(resolve, 0));
      expect(f.routes.mock.calls).toHaveLength(1); expect(f.calls).toHaveLength(phase === 'read' ? 1 : 0);
      if (invalidation === 'credential') m.invalidateGatewayToken(0);
      else {
        m.workspaceContext.publish({ ...previous, gatewayId: otherGateway });
        m.workspaceContext.publish({ ...previous, gatewayId: owner.gatewayId });
      }
      barrier.resolve();
      const result = await pending;
      expect(result).not.toHaveProperty('resolved');
      expect(result.error).toContain('Gateway 或访问凭据已改变');
      expect(f.texts.size).toBe(0); expect(f.created).toEqual([]);
      expect(f.calls).toHaveLength(phase === 'read' ? 1 : 0);
      f.state.routePaused = f.state.paused = undefined;
      const current = await f.read(resource);
      expect(current.object.textEditorModel.getValue()).toContain(ownerKey);
      expect(f.created).toHaveLength(1);
    }); } finally { m.workspaceContext.publish(previous); }
  });
}

for (const kind of ['change', 'source']) test(`只读恢复${kind}允许首次Gateway发布与同Gateway活动工作区切换，已加载模型继续复用`, async () => {
  const previous = m.workspaceContext.getSnapshot();
  m.workspaceContext.publish({ ...previous, gatewayId: null });
  try { await fixture(async f => {
    f.state.routePaused = Promise.withResolvers();
    const resource = kind === 'change' ? uri() : m.sourceDocumentUri(owner,
      `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/skills/skill-a/source.md`);
    const pending = f.read(resource);
    await new Promise(resolve => setTimeout(resolve, 0));
    m.workspaceContext.publish({ ...previous, gatewayId: owner.gatewayId });
    m.workspaceContext.publish({ ...previous, gatewayId: owner.gatewayId, activeId: 'another-workspace' });
    f.state.routePaused.resolve();
    const first = await pending;
    m.invalidateGatewayToken(0);
    const loaded = await f.read(resource);
    expect(loaded.object).toBe(first.object); expect(f.created).toHaveLength(1); expect(f.calls).toHaveLength(1);
  }); } finally { m.workspaceContext.publish(previous); }
});

test('旧更改打开作用域失效后，重开读取当前版本且旧作用域结清不打断新并发读取', async () => {
  await fixture(async f => {
    const entered = Promise.withResolvers(), oldRelease = Promise.withResolvers(), newEntered = Promise.withResolvers(), newRelease = Promise.withResolvers();
    const source = m.changeFileSource(uri());
    const old = f.documents.withFile(source, async () => { entered.resolve(); await oldRelease.promise; })
      .then(() => ({ resolved: true }), error => ({ error: String(error) }));
    let current;
    try {
      await entered.promise;
      m.invalidateGatewayToken(0); f.state.latest = 'edit-2';
      current = f.documents.withFile(source, async file => { newEntered.resolve(); await newRelease.promise; return file.latest_edit_id; });
      await newEntered.promise; expect(f.calls).toHaveLength(2);
      oldRelease.resolve(); expect((await old).error).toContain('Gateway 或访问凭据已改变');
      const after = await f.read(uri('after', owner, 'edit-2'));
      expect(after.object.textEditorModel.getValue()).toContain('after edit-2'); expect(f.calls).toHaveLength(2);
      newRelease.resolve(); expect(await current).toBe('edit-2');
    } finally {
      oldRelease.resolve(); newRelease.resolve();
      await Promise.allSettled([old, current]);
    }
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

test('规范Diff打开作用域顺序解析前后侧仅读一次，结清后重读当前版本', async () => {
  await fixture(async f => {
    const source = m.changeFileSource(uri());
    const open = () => f.documents.withFile(source, async current => {
      const before = await f.read(uri('before', owner, current.latest_edit_id));
      const after = await f.read(uri('after', owner, current.latest_edit_id));
      return { before, after };
    });
    const first = await open();
    expect(first.before.object.textEditorModel.getValue()).toContain('before edit-1');
    expect(first.after.object.textEditorModel.getValue()).toContain('after edit-1');
    expect(f.calls).toHaveLength(1);
    f.state.latest = 'edit-2';
    const next = await open();
    expect(next.before.object.textEditorModel.getValue()).toContain('before edit-2');
    expect(next.after.object.textEditorModel.getValue()).toContain('after edit-2');
    expect(first.after.object.textEditorModel.getValue()).toContain('after edit-1');
    expect(f.calls).toHaveLength(2);
    await expect(f.documents.withFile(source, () => { throw new Error('打开消费者失败'); })).rejects.toThrow('打开消费者失败');
    await open();
    expect(f.calls).toHaveLength(4);
    receipt.checks.push('公开Diff打开作用域顺序建两侧共用1次原响应；下次读当前版本；消费者失败后释放');
  });
});

test('并发Diff作用域中首个读取者结束后，跟随者解析第二侧仍复用同一读取', async () => {
  await fixture(async f => {
    f.state.paused = Promise.withResolvers();
    const source = m.changeFileSource(uri());
    const ownerRelease = Promise.withResolvers();
    const followerFirstRead = Promise.withResolvers();
    const followerContinue = Promise.withResolvers();
    const first = f.documents.withFile(source, async () => {
      await ownerRelease.promise;
      return 'first scope closed';
    });
    const follower = f.documents.withFile(source, async current => {
      const before = await f.read(uri('before', owner, current.latest_edit_id));
      followerFirstRead.resolve();
      await followerContinue.promise;
      const after = await f.read(uri('after', owner, current.latest_edit_id));
      return { before, after };
    });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.calls).toHaveLength(1);
    f.state.paused.resolve();
    await followerFirstRead.promise;
    ownerRelease.resolve();
    await expect(first).resolves.toBe('first scope closed');
    expect(f.calls).toHaveLength(1);
    followerContinue.resolve();
    const { before, after } = await follower;
    expect(before.object.textEditorModel.getValue()).toContain('before edit-1');
    expect(after.object.textEditorModel.getValue()).toContain('after edit-1');
    expect(f.calls).toHaveLength(1);
    f.state.latest = 'edit-2';
    await f.documents.withFile(source, current => current.latest_edit_id);
    expect(f.calls).toHaveLength(2);
    receipt.checks.push('固定首个作用域先结清、跟随者再解析第二侧；并发两侧只发1次HTTP，全部结清后重新读取当前revision');
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

test('英文只读恢复与更改失败使用产品摘要，服务原错和记录原件保持', async () => {
  m.setLocaleResources('en', m.english);
  try {
    await fixture(async f => {
      f.state.latest = 'edit-2';
      await expect(f.read(uri())).rejects.toThrow('This change version has changed; reopen it from the session changes list');
      expect(f.texts.size).toBe(0);
      f.state.fail = true;
      await expect(f.read(uri())).rejects.toThrow('原更改读取失败');
      expect(f.calls).toHaveLength(2);
    });
    const stored = { version: 99, name: '用户来源 {0}', resource: uri().toJSON() };
    const unavailable = new m.ReadonlyDocumentSerializer().deserialize({}, JSON.stringify(stored));
    expect(unavailable.reason).toBe('Unsupported read-only document restore version: 99');
    expect(unavailable.getName()).toBe('用户来源 {0}');
    expect(unavailable.stored).toEqual(stored);
    unavailable.dispose();
    receipt.checks.push('英文产品摘要；未知服务原错、用户标题和坏记录原件保留，零错误模型');
  } finally { m.setLocaleResources('zh-cn', m.chinese); }
});

afterAll(async () => { await writeFile(join(run.artifactsDir, 'change-documents.json'), JSON.stringify(receipt, null, 2) + '\n'); });
