import { afterEach, expect, spyOn, test } from 'bun:test';
import * as api from '../../web/src/api/mcpConnections.js';
import * as sources from '../../web/src/api/configSources.js';
import { HttpRequestError } from '../../web/src/api/http.js';
import { offerMcpArtifactConfiguration } from '../src/mcp/configuration.js';
import type { PublishedMcpArtifact } from '../src/mcp/installation.js';
import type { ConfigSourceDocumentDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { IQuickInputService, IQuickPickItem, QuickPickInput } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { INotificationService, IPromptChoice, IPromptOptions } from '../upstream/src/vs/platform/notification/common/notification.js';

const workspace = { gateway_id: 'gateway_00000000000040008000000000000001', workspace_id: '00000000-0000-4000-8000-000000000001' };
const target = { kind: 'backend', gateway_id: workspace.gateway_id, backend_id: '00000000-0000-4000-8000-000000000011' };
const digest = 'sha256:jcs:v1:' + 'a'.repeat(64);
const published: PublishedMcpArtifact = { artifact: {
  binding: { backend_id: target.backend_id, installation_id: 'b'.repeat(32), generation: 'c'.repeat(32), artifact_digest: digest },
  source: { kind: 'gallery', name: 'original/server', version: 'release-1', endpoint: 'https://original.test' }, manifest_digest: digest,
  variant: { kind: 'package', index: 1, digest }, platform: { operating_system: 'linux', architecture: 'x86_64' }, registry_type: 'pypi',
  identifier: 'chosen', version: '1.0.0', entrypoint: 'chosen', command: '/original/chosen', lock_digest: digest,
  installed_distributions: { chosen: '1.0.0' }, state: 'published',
}, candidate: { target, source: { kind: 'gallery', name: 'original/server', version: 'release-1', endpoint: 'https://original.test',
  protocol: 'v0.1', entry_url: 'https://original.test/exact' }, manifest_digest: digest, variant: { kind: 'package', index: 1, digest }, inputs: {} } };
const document: ConfigSourceDocumentDTO = { ...workspace, source_key: 'workspace_root_mutable_override', layer: 'workspace', writable: true,
  jsonc: '// 原 source 与占位\r\n{"private":"***"}\r\n', file_presence: 'present', file_digest: 'original-file', layer_revision: 3,
  layer_digest: 'original-layer', source_generation: 'original-generation', active_revision: 2, active_digest: 'original-active',
  json_schema: {}, effective: {}, source_effective: {}, layers: [], origins: [], status: undefined };
const generated = '// 后端生成的提案\r\n{"mcp":{"servers":{"server-a":{"enabled":false}}}}\r\n';
const mocks: { mockRestore(): void }[] = [];
afterEach(() => { mocks.splice(0).forEach(mock => mock.mockRestore()); });
type Step = (items: QuickPickInput<IQuickPickItem>[]) => IQuickPickItem | undefined;
const choose = (label: string): Step => items => {
  const choice = items.find(item => item.type !== 'separator' && item.label === label);
  expect(choice).toBeDefined();
  return choice as IQuickPickItem;
};

function prepare(steps: Step[] = [choose('暂不启用此 Server'), choose('确认保存工作区配置')], notificationChoices: (boolean | 'hold')[] = [true]) {
  const read = spyOn(sources, 'getConfigSourceDocument').mockResolvedValue(document);
  const plan = spyOn(api, 'planMcpInstallation').mockResolvedValue({ code: 0, message: 'ok', request_id: 'plan', target, protocol_version: 1,
    data: { source: published.artifact.source, manifest_digest: digest, variant: published.candidate.variant,
      platform: published.artifact.platform, plan_digest: digest, disposition: 'requires_artifact', download_required: false,
      parameters: { command: '此脱敏投影不得进入保存', arguments: ['wrong'] } } });
  const draft = spyOn(api, 'draftMcpCandidateConfiguration').mockImplementation(async (_port, _route, request) => ({
    source_edit: { ...request.source_edit!, jsonc: generated }, preflight: { document, candidate: {}, candidate_digest: digest, changed_paths: ['mcp.servers.server-a'] },
  }));
  const save = spyOn(api, 'saveMcpCandidateConfiguration').mockImplementation(async (_port, _route, request) => ({
    ...document, edit: { idempotency_key: request.source_edit!.idempotency_key, candidate_id: 'source-candidate', state: 'pending',
      file_digest: 'saved-file', source_saved: true, layer_imported: true },
  }));
  mocks.push(read, plan, draft, save);
  const input = spyOn({ input: async (): Promise<string | undefined> => 'server-a' }, 'input');
  const quick = { input, pick: async (items: QuickPickInput<IQuickPickItem>[]) => {
    const step = steps.shift(); if (!step) throw new Error('出现计划外 Quick Input'); return step(items);
  } } as unknown as IQuickInputService;
  const messages: string[] = [], prompts: { text: string; choices: IPromptChoice[]; options?: IPromptOptions }[] = [];
  let closed = 0;
  const notifications = { info: (text: string) => messages.push(text),
    prompt: (_severity: unknown, text: string, choices: IPromptChoice[], options?: IPromptOptions) => {
      prompts.push({ text, choices, options });
      const selected = notificationChoices.shift();
      if (selected !== 'hold') queueMicrotask(() => selected ? choices[0]!.run() : options?.onCancel?.());
      return { close() { ++closed; options?.onCancel?.(); } };
    },
  } as unknown as INotificationService;
  const controller = new AbortController();
  const run = (route = workspace, original = published, assertAccess = () => {}) =>
    offerMcpArtifactConfiguration(original, route, '目标 A', quick, notifications, assertAccess, controller.signal);
  return { run, read, plan, draft, save, input, messages, prompts, controller, closed: () => closed };
}

test.each([false, true])('发布后只有明确配置、Server ID、启用选择及确认才保存；enabled=%j', async enabled => {
  const fixture = prepare([choose(enabled ? '启用此 Server' : '暂不启用此 Server'), choose('确认保存工作区配置')]);
  await fixture.run();
  expect(fixture.prompts[0]!.choices[0]!.label).toBe('配置此工作区');
  expect(fixture.plan.mock.calls[0]![2]).toEqual({ ...published.candidate, server_id: 'server-a' });
  const draft = fixture.draft.mock.calls[0]!;
  expect(draft[1]).toEqual(workspace);
  expect(draft[2]).toMatchObject({ server_id: 'server-a', enabled, managed_launch: published.artifact.binding,
    source_edit: { ...workspace, source_key: document.source_key, jsonc: document.jsonc, base_file_digest: document.file_digest,
      base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
      expected_active_revision: document.active_revision, expected_active_digest: document.active_digest } });
  expect(fixture.save).toHaveBeenCalledTimes(1);
  const intent = fixture.save.mock.calls[0]![2];
  expect(intent.source_edit?.jsonc).toBe(generated);
  expect(intent.plan).toEqual(draft[2].plan);
  expect(intent).not.toHaveProperty('enabled');
  expect(JSON.stringify(intent)).not.toContain('此脱敏投影');
  expect(fixture.read.mock.calls[0]![3]?.signal).toBe(fixture.controller.signal);
  for (const request of [fixture.plan.mock.calls[0]!, draft, fixture.save.mock.calls[0]!]) expect(request[3]).toBe(fixture.controller.signal);
  expect(fixture.messages[0]).toContain('source 状态 pending');
  expect(fixture.messages[0]).not.toContain('ready');
});

test.each(['notification', 'server-id', 'enablement', 'confirmation'])('取消 %s 阶段不保存', async stage => {
  const fixture = prepare(stage === 'confirmation' ? [choose('暂不启用此 Server'), () => undefined] : [() => undefined],
    [stage !== 'notification']);
  if (stage === 'server-id') fixture.input.mockResolvedValue(undefined);
  await fixture.run();
  if (stage === 'notification') expect(fixture.input).not.toHaveBeenCalled();
  if (stage === 'confirmation') expect(fixture.draft).toHaveBeenCalledTimes(1);
  else expect(fixture.read).not.toHaveBeenCalled();
  expect(fixture.save).not.toHaveBeenCalled();
});

test('焦点与原对象改变不改投原Gateway、workspace、variant、Backend或generation', async () => {
  const route = { ...workspace }, original = structuredClone(published);
  const fixture = prepare([choose('暂不启用此 Server'), items => {
    route.workspace_id = 'new-focus'; original.artifact.binding!.generation = 'another-generation'; original.candidate.variant!.index = 0;
    return choose('确认保存工作区配置')(items);
  }]);
  await fixture.run(route, original);
  const save = fixture.save.mock.calls[0]!;
  expect(save[1]).toEqual(workspace); expect(save[2].managed_launch).toEqual(published.artifact.binding);
  expect(save[2].plan?.candidate?.variant?.index).toBe(1); expect(save[2].target).toEqual({ kind: 'workspace', ...workspace });
});

test('确认前身份失效中止原信号和Quick Input，不提交旧确认', async () => {
  const fixture = prepare([choose('暂不启用此 Server'), items => {
    fixture.controller.abort(new Error('原身份已失效')); return choose('确认保存工作区配置')(items);
  }]);
  await expect(fixture.run()).rejects.toThrow('原身份已失效');
  expect(fixture.save).not.toHaveBeenCalled();
});

test('原目标未就绪在配置入口失败，不启动目标或读取来源', async () => {
  const fixture = prepare();
  await expect(fixture.run(workspace, published, () => { throw new Error('原目标尚未就绪'); })).rejects.toThrow('原目标尚未就绪');
  expect(fixture.prompts).toEqual([]); expect(fixture.read).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});

test('保存结果未知只回读原source并提供原键查询，不重放PUT', async () => {
  const fixture = prepare(undefined, [true, true, true]);
  fixture.save.mockRejectedValue(new Error('accepted then disconnected'));
  fixture.read.mockResolvedValueOnce(document).mockResolvedValueOnce(document).mockRejectedValueOnce(new Error('query unavailable'))
    .mockImplementation(async (_port, _route, _source, options) => ({ ...document,
      edit: { idempotency_key: options!.idempotencyKey!, candidate_id: 'original-source-candidate', state: 'active', file_digest: 'saved', source_saved: true, layer_imported: true } }));
  await fixture.run();
  expect(fixture.save).toHaveBeenCalledTimes(1);
  const key = fixture.save.mock.calls[0]![2].source_edit!.idempotency_key;
  expect(fixture.read).toHaveBeenCalledTimes(4);
  for (const request of fixture.read.mock.calls.slice(1)) {
    expect(request[1]).toEqual(workspace); expect(request[2]).toBe(document.source_key);
    expect(request[3]).toEqual({ idempotencyKey: key, signal: fixture.controller.signal });
  }
  expect(fixture.prompts[1]!.choices[0]!.label).toBe('查询原来源提交');
  expect(fixture.prompts[2]!.text).toContain('query unavailable');
  expect(fixture.messages[0]).toContain(`提交 ${key}`); expect(fixture.messages[0]).toContain('source 状态 active');
});

test('保存被拒绝且回读失败仍显示原错误及request_id，不重建提案或保存', async () => {
  const fixture = prepare();
  const cause = new HttpRequestError(409, 'Conflict', '原CAS已过期', '/candidate-configurations', 'original-save-request');
  fixture.save.mockRejectedValue(cause);
  fixture.read.mockResolvedValueOnce(document).mockRejectedValueOnce(new Error('source read failed'));
  const failure = await fixture.run().catch((error: unknown) => error);
  expect(failure).toBeInstanceOf(Error); expect((failure as Error).cause).toBe(cause);
  expect((failure as Error).message).toContain('original-save-request'); expect((failure as Error).message).toContain('source read failed');
  expect(fixture.save).toHaveBeenCalledTimes(1); expect(fixture.plan).toHaveBeenCalledTimes(1); expect(fixture.prompts).toHaveLength(1);
});

test('身份失效关闭尚未选择的发布通知，不留下可恢复的旧确认', async () => {
  const fixture = prepare([], ['hold']);
  const running = fixture.run(); const reason = new Error('root Gateway changed'); fixture.controller.abort(reason);
  await expect(running).rejects.toBe(reason); expect(fixture.closed()).toBe(1);
  expect(fixture.read).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});
