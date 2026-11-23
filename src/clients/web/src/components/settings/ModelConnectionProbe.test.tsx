import React from 'react';
import { afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { t } from '../../../../shared/core/i18n/index.js';
import { ConfigSourceDocumentDTO, type LlmDiscoverRequest, type LlmTestResultDTO } from '../../types/protocol_generated/boxteam/workspace/v2/public.js';
import { ModelConnectionProbe, type ModelProbeEditor } from './ModelConnectionProbe.js';
import { SecretsClient } from '../../api/secrets.js';

const originalFetch = globalThis.fetch;
const target = { gateway_id: 'gateway_00000000000040008000000000000001', workspace_id: '00000000-0000-4000-8000-000000000001' };
let view: ReactTestRenderer | undefined;
afterEach(() => { act(() => view?.unmount()); view = undefined; globalThis.fetch = originalFetch; });

function setup() {
  const document = ConfigSourceDocumentDTO.fromPartial({ ...target, source_key: 'workspace', writable: true, jsonc: '{}',
    active_revision: 4, active_digest: 'active-four', source_baseline: { workspace: { presence: 'present', file_digest: 'saved' } } });
  const abort = new AbortController();
  const editor: ModelProbeEditor = { target, jsonc: '// 当前来源原文\n{}', version: 10, dirty: true, signal: abort.signal,
    connectionCommit: 0, autoDiscover: true, onAutoDiscover: mock() };
  const onImport = mock(), onApplyMode = mock(), onOpenSource = mock();
  let selectedModelId = 'main';
  const render = () => <ModelConnectionProbe providerId="team" models={['main', 'backup', 'third', 'fourth', 'fifth'].map(id => ({ id, model: `upstream-${id}` }))}
    selectedModelId={selectedModelId} document={document} editor={{ ...editor }} onImport={onImport} onApplyMode={onApplyMode} onOpenSource={onOpenSource} />;
  act(() => { view = create(render()); });
  return { editor, document, abort, onImport, onApplyMode, onOpenSource, update: () => act(() => view!.update(render())),
    selectModel: (id: string) => { selectedModelId = id; act(() => view!.update(render())); } };
}

function respond(handler: (url: URL, init: RequestInit) => Promise<Response> | Response): void {
  globalThis.fetch = Object.assign(async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    const url = new URL(String(input), 'http://127.0.0.1');
    if (url.pathname === '/api/gateway/auth/local-credential') return Response.json({ data: { token: 'probe-ui-test' } });
    return handler(url, init ?? {});
  }, { preconnect: originalFetch.preconnect });
}
const ok = (data: unknown) => Response.json({ code: 0, data, message: 'ok', request_id: 'probe-ui' });
const directory = (input: LlmDiscoverRequest) => ({
  observation: { ...target, mode: input.mode, provider_id: input.provider_id, edit_version: input.edit_version,
    source_key: input.source_key, source_baseline: input.expected_source_baseline,
    active_revision: input.expected_active_revision, active_digest: input.expected_active_digest,
    credential: { source: 'none' }, protocol: 'responses', endpoint: 'https://provider.test/v1', observed_at: '2026-11-17T00:00:00Z' },
  source: 'upstream', models: [{ model: 'upstream-main' }, { model: 'new-model', name: '新模型' }], complete: false,
  error: { category: 'pagination', message: '还有未读取的页' },
});
function button(key: string) { return view!.root.findAllByType('button').find(item => item.children.includes(t(key)))!; }
const flush = () => new Promise<void>(resolve => setTimeout(resolve, 0));
async function click(key: string) { await act(async () => { button(key).props.onClick(); await flush(); }); }

test('打开、同版本刷新和显示名编辑不探测，连接完成编辑只发现一次', async () => {
  const calls: LlmDiscoverRequest[] = [];
  respond((_url, init) => { const input = JSON.parse(String(init.body)); calls.push(input); return ok(directory(input)); });
  const fixture = setup();
  fixture.editor.target = { ...target, gateway_connection_id: 'original-route' };
  fixture.update(); fixture.editor.version++; fixture.update();
  await act(flush);
  expect(calls).toHaveLength(0);
  fixture.editor.connectionCommit = 1;
  await act(async () => { fixture.update(); await flush(); });
  fixture.update(); await act(flush);
  expect(calls).toHaveLength(1);
  expect(calls[0]).toMatchObject({ ...target, mode: 'draft', jsonc: fixture.editor.jsonc,
    expected_source_baseline: JSON.parse(JSON.stringify(fixture.document.source_baseline)) });
  expect(calls[0]).not.toHaveProperty('gateway_connection_id');
  expect(fixture.onImport).not.toHaveBeenCalled();
  fixture.editor.autoDiscover = false; fixture.editor.connectionCommit++;
  fixture.update(); await act(flush);
  expect(calls).toHaveLength(1);
});

test('不完整目录保留差异，搜索多选只有显式选入才修改原草稿', async () => {
  respond((_url, init) => ok(directory(JSON.parse(String(init.body)))));
  const fixture = setup();
  await click('configuration.probe.discover');
  const markup = JSON.stringify(view!.toJSON());
  expect(markup).toContain('还有未读取的页');
  expect(markup).toContain('backup');
  expect(fixture.onImport).not.toHaveBeenCalled();
  const search = view!.root.findAllByType('input').find(item => item.props.type === undefined)!;
  act(() => search.props.onChange({ target: { value: 'new-' } }));
  await click('configuration.probe.selectMatching');
  await click('configuration.probe.importSelected');
  expect(fixture.onImport.mock.calls).toEqual([[[{ model: 'new-model', name: '新模型' }], 10]]);
});

test('编辑时取消旧目录，忽略不遵守取消的迟到响应', async () => {
  let release!: () => void;
  let signal: AbortSignal | undefined;
  respond((_url, init) => new Promise(resolve => {
    signal = init.signal ?? undefined;
    const input = JSON.parse(String(init.body)); release = () => resolve(ok(directory(input)));
  }));
  const fixture = setup();
  await click('configuration.probe.discover');
  fixture.editor.version++; fixture.update();
  expect(signal?.aborted).toBe(true);
  await act(async () => { release(); await flush(); });
  expect(JSON.stringify(view!.toJSON())).not.toContain('新模型');
  expect(fixture.onImport).not.toHaveBeenCalled();
});

test('发现能力逐字段说明来源，保留上游否定声明且不把参考值自动写入配置', async () => {
  respond((_url, init) => {
    const result = directory(JSON.parse(String(init.body)));
    return ok({ ...result, models: [{ model: 'new-model', name: '新模型', metadata: {
      max_input_tokens: 32000, supports_vision: false, supports_function_calling: true,
      supports_reasoning: null, catalog_provider: 'openai',
      sources: { max_input_tokens: 'provider', supports_vision: 'upstream', supports_function_calling: 'litellm' },
    } }] });
  });
  const fixture = setup();
  await click('configuration.probe.discover');
  const markup = JSON.stringify(view!.toJSON());
  expect(markup).toContain(t('configuration.catalog.providerSource'));
  expect(markup).toContain(t('configuration.catalog.capacity', 32000));
  expect(markup).toContain(t('configuration.catalog.upstreamSource'));
  expect(markup).toContain(t('configuration.catalog.unsupported', t('configuration.catalog.vision')));
  expect(markup).toContain(t('configuration.catalog.referenceSource', 'openai'));
  expect(markup).not.toContain(t('configuration.catalog.reasoning'));
  expect(fixture.onImport).not.toHaveBeenCalled();
  await click('configuration.probe.selectMatching');
  await click('configuration.probe.importSelected');
  expect(fixture.onImport.mock.calls[0][0][0].metadata.sources).toEqual({
    max_input_tokens: 'provider', supports_vision: 'upstream', supports_function_calling: 'litellm',
  });
  expect(fixture.editor.jsonc).toBe('// 当前来源原文\n{}');
});

test('批量最多三个在途，停止取消实际请求并且不再派发后续模型', async () => {
  const calls: { model_id: string; signal: AbortSignal }[] = [];
  respond((_url, init) => new Promise((_resolve, reject) => {
    const input = JSON.parse(String(init.body));
    const signal = init.signal!;
    calls.push({ model_id: input.model_id, signal });
    signal.addEventListener('abort', () => reject(signal.reason), { once: true });
  }));
  setup();
  const choices = view!.root.findAllByType('input').filter(item => item.props.type === 'checkbox').slice(1);
  act(() => { for (const choice of choices) choice.props.onChange({ target: { checked: true } }); });
  await click('configuration.probe.testSelected');
  expect(calls.map(call => call.model_id)).toEqual(['main', 'backup', 'third']);
  await click('configuration.probe.stop');
  expect(calls).toHaveLength(3);
  expect(calls.every(call => call.signal.aborted)).toBe(true);
  expect(JSON.stringify(view!.toJSON())).toContain(t('configuration.probe.status.cancelled'));
});

test('active发现不混入草稿，显式版本刷新只读来源而不重测', async () => {
  const calls: { path: string; body: unknown }[] = [];
  const fixture = setup(); fixture.editor.dirty = false; fixture.update();
  respond((url, init) => {
    calls.push({ path: url.pathname, body: init.body ? JSON.parse(String(init.body)) : undefined });
    return ok(init.method === 'POST' ? directory(JSON.parse(String(init.body))) : { ...fixture.document, active_revision: 5, active_digest: 'active-five' });
  });
  await click('configuration.probe.discover');
  expect(calls[0].body).toMatchObject({ mode: 'active', expected_active_revision: 4, expected_active_digest: 'active-four' });
  expect(calls[0].body).not.toHaveProperty('jsonc');
  expect(calls[0].body).not.toHaveProperty('credential_input');
  await click('configuration.probe.refreshVersions');
  expect(calls.map(call => call.path)).toEqual(['/api/v1/config/llm/discover', '/api/v1/config/sources/workspace']);
  expect(JSON.stringify(view!.toJSON())).toContain(t('configuration.probe.stale'));
  expect(fixture.editor.jsonc).toBe('// 当前来源原文\n{}');
});

test('Model临时Key绑定原模型，换模型和取消均清除密码', async () => {
  const fixture = setup();
  const scope = view!.root.findAllByType('select')[1];
  act(() => scope.props.onChange({ target: { value: 'model' } }));
  const password = () => view!.root.findByProps({ type: 'password' });
  act(() => password().props.onChange({ target: { value: 'never-save-this-key' } }));
  fixture.selectModel('backup');
  expect(password().props.value).toBe('');
  act(() => password().props.onChange({ target: { value: 'another-key' } }));
  act(() => fixture.abort.abort());
  expect(password().props.value).toBe('');
});

function modelTestResponse(input: LlmDiscoverRequest & { model_id: string; streaming?: boolean }, credential = { source: 'none' } as object,
  status: LlmTestResultDTO['status'] = 'passed') {
  const source_baseline = Object.fromEntries(Object.entries(input.expected_source_baseline).map(([key, value]) => [key, {
    uri: value.uri ?? null, presence: value.presence, layer_revision: value.layer_revision ?? null,
    layer_digest: value.layer_digest ?? null, source_generation: value.source_generation ?? null,
    file_presence: value.file_presence ?? null, file_digest: value.file_digest ?? null,
  }]));
  const start = { request_id: 'probe-test', observation: { ...directory(input).observation, source_baseline, credential },
    model_ref: { provider_id: input.provider_id, model_id: input.model_id }, streaming: input.streaming ?? true };
  const result = { ...start, status, first_content_ms: 3, elapsed_ms: 5, preview: '完成',
    preview_truncated: false, usage: null, error: null };
  return new Response(`event: start\ndata: ${JSON.stringify(start)}\n\nevent: result\ndata: ${JSON.stringify(result)}\n\n`,
    { headers: { 'Content-Type': 'text/event-stream', 'X-Request-ID': start.request_id } });
}

test('临时Provider Key结束编辑才发现一次，纯焦点变化和关闭自动获取均不请求', async () => {
  const calls: LlmDiscoverRequest[] = [];
  respond((_url, init) => { const input = JSON.parse(String(init.body)); calls.push(input); return ok(directory(input)); });
  const fixture = setup();
  const password = () => view!.root.findByProps({ type: 'password' });
  act(() => { password().props.onChange({ target: { value: 'first' } }); password().props.onChange({ target: { value: 'second' } }); });
  expect(calls).toHaveLength(0);
  await act(async () => { password().props.onBlur(); await flush(); });
  expect(calls).toHaveLength(1);
  expect(calls[0].credential_input).toEqual({ provider_id: 'team', scope: 'provider', value: 'second' });
  await act(async () => { password().props.onBlur(); await flush(); });
  expect(calls).toHaveLength(1);
  fixture.editor.autoDiscover = false; fixture.update();
  act(() => password().props.onChange({ target: { value: 'third' } }));
  await act(async () => { password().props.onBlur(); await flush(); });
  expect(calls).toHaveLength(1);
});

test('明确测试两种模式分别发真实选择且不修改草稿，应用模式才交回原writer', async () => {
  const calls: { streaming: boolean }[] = [];
  respond((_url, init) => { const input = JSON.parse(String(init.body)); calls.push(input); return modelTestResponse(input); });
  const fixture = setup();
  await click('configuration.probe.test.both');
  expect(calls.map(call => call.streaming)).toEqual([true, false]);
  expect(fixture.onApplyMode).not.toHaveBeenCalled();
  expect(JSON.stringify(view!.toJSON())).toContain('probe-test');
  await click('configuration.probe.applyMode');
  expect(fixture.onApplyMode.mock.calls).toEqual([['main', true]]);
});

test('服务端明确取消的终态保留取消结果，不误报失败或自动重测', async () => {
  const calls: string[] = [];
  respond((url, init) => {
    calls.push(url.pathname);
    return modelTestResponse(JSON.parse(String(init.body)), { source: 'none' }, 'cancelled');
  });
  setup();
  await click('configuration.probe.test.configured');
  const markup = JSON.stringify(view!.toJSON());
  expect(markup).toContain(t('configuration.probe.status.cancelled'));
  expect(markup).not.toContain(t('configuration.probe.status.failed'));
  expect(calls).toEqual(['/api/v1/config/llm/test']);
});

test('本窗口密钥回执及显式元数据刷新使旧观察过期，另一Gateway不串线且零重测', async () => {
  const fixture = setup();
  const id = 'sec_' + '1'.repeat(32), calls: string[] = [];
  let revision = 1;
  respond((url, init) => {
    calls.push(`${init.method ?? 'GET'} ${url.pathname}`);
    if (url.pathname.endsWith('/llm/test')) return modelTestResponse(JSON.parse(String(init.body)), { source: 'vault', secret_id: id, revision });
    if (url.pathname.includes('/secrets')) return ok({ gateway_id: new Headers(init.headers).get('X-BoxTeam-Target-Gateway-Id'),
      items: [{ id, revision, name: 'Key', created_at: 'before', updated_at: 'after' }] });
    return ok(fixture.document);
  });
  await click('configuration.probe.test.stream');
  expect(JSON.stringify(view!.toJSON())).not.toContain(t('configuration.probe.stale'));
  revision = 2;
  await act(async () => { await new SecretsClient({ gateway_id: 'another-gateway' }).list(); });
  expect(JSON.stringify(view!.toJSON())).not.toContain(t('configuration.probe.stale'));
  await click('configuration.probe.refreshVersions');
  expect(JSON.stringify(view!.toJSON())).toContain(t('configuration.probe.stale'));
  expect(calls.filter(call => call.includes('/llm/test'))).toHaveLength(1);
  expect(calls).toContain('GET /api/v1/config/sources/workspace');
  expect(calls).toContain('GET /api/gateway/secrets');
  expect(fixture.editor.jsonc).toBe('// 当前来源原文\n{}');
  // 显式重测采用新版本；后续管理回执使这个新观察过期。
  await click('configuration.probe.test.stream');
  expect(JSON.stringify(view!.toJSON())).not.toContain(t('configuration.probe.stale'));
  revision = 3;
  await act(async () => { await new SecretsClient(target).update(id, { expected_revision: 2, name: 'Key' }); });
  expect(JSON.stringify(view!.toJSON())).toContain(t('configuration.probe.stale'));
  expect(calls.filter(call => call.includes('/llm/test'))).toHaveLength(2);
  const after = calls.length;
  act(() => view!.unmount()); view = undefined;
  await new SecretsClient(target).list();
  expect(calls).toHaveLength(after + 1);
});

test('选择时的空密钥目录吸收随后创建回执，新Key测试不会被误判为已删除', async () => {
  setup();
  const id = 'sec_' + '2'.repeat(32), methods: string[] = [];
  respond((url, init) => {
    methods.push(init.method ?? 'GET');
    if (url.pathname.endsWith('/llm/test')) return modelTestResponse(JSON.parse(String(init.body)), { source: 'vault', secret_id: id, revision: 1 });
    return ok(init.method === 'POST' ? { gateway_id: target.gateway_id,
      item: { id, name: '新Key', revision: 1, created_at: 'now', updated_at: 'now' } } : { gateway_id: target.gateway_id, items: [] });
  });
  await act(async () => {
    const client = new SecretsClient(target);
    await client.list();
    await client.create({ id, name: '新Key', value: 'temporary-value' });
  });
  await click('configuration.probe.test.stream');
  expect(JSON.stringify(view!.toJSON())).toContain(t('configuration.probe.status.passed'));
  expect(JSON.stringify(view!.toJSON())).not.toContain(t('configuration.probe.stale'));
  expect(methods).toEqual(['GET', 'POST', 'POST']);
});

test('失败和取消的测试不推荐应用模式，只有成功结果可以修改草稿', async () => {
  let status: LlmTestResultDTO['status'] = 'failed';
  respond((_url, init) => modelTestResponse(JSON.parse(String(init.body)), { source: 'none' }, status));
  const fixture = setup();
  for (const next of ['failed', 'cancelled', 'passed'] as const) {
    status = next;
    await click('configuration.probe.test.configured');
    const actions = view!.root.findAllByType('button').filter(item => item.children.includes(t('configuration.probe.applyMode')));
    expect(actions.length).toBe(next === 'passed' ? 1 : 0);
  }
  expect(fixture.onApplyMode).not.toHaveBeenCalled();
});
