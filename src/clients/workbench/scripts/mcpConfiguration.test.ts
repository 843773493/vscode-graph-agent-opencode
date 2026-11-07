import { afterEach, expect, spyOn, test } from 'bun:test';
import * as api from '../../web/src/api/mcpConnections.js';
import * as sources from '../../web/src/api/configSources.js';
import { HttpRequestError } from '../../web/src/api/http.js';
import { offerMcpArtifactConfiguration } from '../src/mcp/configuration.js';
import type { PublishedMcpArtifact } from '../src/mcp/installation.js';
import type { ConfigSourceDocumentDTO, McpCandidateInputDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { IInputOptions, IQuickInputService, IQuickPickItem, QuickPickInput } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';
import type { INotificationService, IPromptChoice, IPromptOptions } from '../upstream/src/vs/platform/notification/common/notification.js';
import { workspaceContext } from '../src/workspaceContext.js';
import type { GatewayWorkspace } from '../../web/src/types/backend.js';

const workspace = { gateway_id: 'gateway_00000000000040008000000000000001', workspace_id: '00000000-0000-4000-8000-000000000001' };
const target = { kind: 'backend', gateway_id: workspace.gateway_id, backend_id: '00000000-0000-4000-8000-000000000011' };
const route = { ...target, gateway_connection_id: 'original-connection' };
const workspaceRoute = { ...workspace, gateway_connection_id: route.gateway_connection_id };
const registered: GatewayWorkspace = { ...workspace, backend_id: target.backend_id, name: '原工作区', backend_url: 'http://workspace.test',
  connection_kind: 'local', status: 'ready', active: true, managed: true, removable: true, system_default: false,
  checked_at: '2026-11-07T00:00:00Z', affected_workspace_ids: [], remote_gateways: [], services: {} };
const digest = 'sha256:jcs:v1:' + 'a'.repeat(64);
const published: PublishedMcpArtifact = { artifact: {
  binding: { backend_id: target.backend_id, installation_id: 'b'.repeat(32), generation: 'c'.repeat(32), artifact_digest: digest },
  source: { kind: 'gallery', name: 'original/server', version: 'release-1', endpoint: 'https://original.test' }, manifest_digest: digest,
  variant: { kind: 'package', index: 1, digest }, platform: { operating_system: 'linux', architecture: 'x86_64' }, registry_type: 'pypi',
  identifier: 'chosen', version: '1.0.0', entrypoint: 'chosen', command: '/original/chosen', lock_digest: digest,
  installed_distributions: { chosen: '1.0.0' }, state: 'published',
}, candidate: { target, source: { kind: 'gallery', name: 'original/server', version: 'release-1', endpoint: 'https://original.test',
  protocol: 'v0.1', entry_url: 'https://original.test/exact' }, manifest_digest: digest, variant: { kind: 'package', index: 1, digest }, inputs: {} }, configuration_inputs: [] };
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
const inputDefinition = (key: string, fields: Partial<McpCandidateInputDTO> = {}): McpCandidateInputDTO => ({
  key, is_required: true, can_skip: false, format: 'string', choices: undefined, is_secret: false, is_repeated: false, ...fields,
});

function prepare(steps: Step[] = [choose('暂不启用此 Server'), choose('确认保存工作区配置')], notificationChoices: (boolean | 'hold')[] = [true]) {
  let snapshot = { ...workspaceContext.getSnapshot(), gatewayId: workspace.gateway_id, workspaces: [registered], loading: false, error: null };
  const context = spyOn(workspaceContext, 'getSnapshot').mockImplementation(() => snapshot);
  const refresh = spyOn(workspaceContext, 'refresh').mockResolvedValue();
  steps = [choose('原工作区'), ...steps];
  const read = spyOn(sources, 'getConfigSourceDocument').mockResolvedValue(document);
  const plan = spyOn(api, 'planMcpCandidateConfiguration').mockResolvedValue({ code: 0, message: 'ok', request_id: 'plan', target, protocol_version: 1,
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
  const write = spyOn(api, 'writeMcpPrivateEnv').mockImplementation(async (_port, _route, request) => ({ kind: 'mcp_private_env',
    workspace_id: workspace.workspace_id, server_id: request.server_id, input_key: request.input_key, definition_digest: digest, version: 'f'.repeat(32) }));
  mocks.push(context, refresh, read, plan, draft, save, write);
  const input = spyOn({ input: async (_options: IInputOptions, _token: CancellationToken): Promise<string | undefined> => 'server-a' }, 'input');
  const quick = { input, pick: async (items: QuickPickInput<IQuickPickItem>[]) => {
    const step = steps.shift(); if (!step) throw new Error('出现计划外 Quick Input'); return step(items);
  } } as unknown as IQuickInputService;
  const messages: string[] = [], warnings: string[] = [], prompts: { text: string; choices: IPromptChoice[]; options?: IPromptOptions }[] = [];
  let closed = 0;
  const notifications = { info: (text: string) => messages.push(text), warn: (text: string) => warnings.push(text),
    prompt: (_severity: unknown, text: string, choices: IPromptChoice[], options?: IPromptOptions) => {
      prompts.push({ text, choices, options });
      const selected = notificationChoices.shift();
      if (selected !== 'hold') queueMicrotask(() => selected ? choices[0]!.run() : options?.onCancel?.());
      return { close() { ++closed; options?.onCancel?.(); } };
    },
  } as unknown as INotificationService;
  const controller = new AbortController();
  const run = (owner = route, original = published, assertAccess = () => {}) =>
    offerMcpArtifactConfiguration(original, owner, '目标 A', quick, notifications, assertAccess, controller.signal);
  return { run, read, refresh, plan, draft, save, write, input, messages, warnings, prompts, controller, closed: () => closed,
    update: (change: Partial<typeof snapshot>) => { snapshot = { ...snapshot, ...change }; } };
}

test.each([false, true])('发布后只有明确配置、Server ID、启用选择及确认才保存；enabled=%j', async enabled => {
  const fixture = prepare([choose(enabled ? '启用此 Server' : '暂不启用此 Server'), choose('确认保存工作区配置')]);
  await fixture.run();
  expect(fixture.prompts[0]!.choices[0]!.label).toBe('选择工作区配置');
  expect(fixture.refresh).toHaveBeenCalledTimes(1);
  expect(fixture.plan.mock.calls[0]![1]).toEqual(workspaceRoute);
  expect(fixture.plan.mock.calls[0]![2]).toEqual({ ...published.candidate, server_id: 'server-a' });
  const draft = fixture.draft.mock.calls[0]!;
  expect(draft[1]).toEqual(workspaceRoute);
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
  const mutableRoute = { ...route }, original = structuredClone(published);
  const fixture = prepare([choose('暂不启用此 Server'), items => {
    mutableRoute.backend_id = 'new-backend'; mutableRoute.gateway_connection_id = 'new-connection';
    fixture.update({ activeId: 'new-focus' }); original.artifact.binding!.generation = 'another-generation'; original.candidate.variant!.index = 0;
    return choose('确认保存工作区配置')(items);
  }]);
  await fixture.run(mutableRoute, original);
  const save = fixture.save.mock.calls[0]!;
  expect(save[1]).toEqual(workspaceRoute); expect(save[2].managed_launch).toEqual(published.artifact.binding);
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
  await expect(fixture.run(route, published, () => { throw new Error('原目标尚未就绪'); })).rejects.toThrow('原目标尚未就绪');
  expect(fixture.prompts).toEqual([]); expect(fixture.read).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});

test('发布通知取消时不刷新或读取任何工作区', async () => {
  const cancelled = prepare([], [false]);
  await cancelled.run();
  expect(cancelled.refresh).not.toHaveBeenCalled(); expect(cancelled.read).not.toHaveBeenCalled();
});

test('未知及异主安装域不能被任选为配置目标', async () => {
  const fixture = prepare();
  fixture.update({ workspaces: [{ ...registered, backend_id: undefined }, { ...registered, backend_id: 'another-backend' },
    { ...registered, gateway_id: 'another-gateway' }] });
  await expect(fixture.run()).rejects.toThrow('没有已确认归属的工作区');
  expect(fixture.input).not.toHaveBeenCalled(); expect(fixture.read).not.toHaveBeenCalled(); expect(fixture.plan).not.toHaveBeenCalled();
});

test('选中工作区停止后拒绝配置，不按新焦点改投另一工作区', async () => {
  const fixture = prepare([choose('暂不启用此 Server'), items => {
    fixture.update({ activeId: 'new-focus', workspaces: [{ ...registered, status: 'stopped' },
      { ...registered, workspace_id: 'another-workspace' }] });
    return choose('确认保存工作区配置')(items);
  }]);
  await expect(fixture.run()).rejects.toThrow('原目标工作区尚未就绪');
  expect(fixture.plan.mock.calls[0]![1]).toEqual(workspaceRoute);
  expect(fixture.save).not.toHaveBeenCalled();
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
    expect(request[1]).toEqual(workspaceRoute); expect(request[2]).toBe(document.source_key);
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

test('真实配置入口保留重复顺序、格式原串、默认选择和可选空串／跳过', async () => {
  const original = { ...published, configuration_inputs: [
    inputDefinition('--tag', { choices: ['red', 'blue'], is_repeated: true }),
    inputDefinition('--port', { format: 'number', default_value: '8123' }),
    inputDefinition('--number', { format: 'number' }),
    inputDefinition('--boolean', { format: 'boolean' }),
    inputDefinition('--directory', { format: 'filepath', is_required: false, can_skip: true }),
    inputDefinition('--optional', { is_required: false, can_skip: true }),
    inputDefinition('--empty', { is_required: false, can_skip: true }),
    inputDefinition('label', { is_required: false, can_skip: false }),
    inputDefinition('MCP_PLAIN_VALUE', { default_value: 'normal', choices: ['normal', 'fast'], is_required: false, can_skip: true }),
    inputDefinition('MCP_EMPTY_VALUE', { is_required: false, can_skip: true }),
  ] };
  const fixture = prepare([choose('启用此 Server'),
    choose('添加一个值'), choose('red'), choose('添加一个值'), choose('red'), choose('添加一个值'), choose('blue'), choose('完成输入'),
    choose('使用默认值'), choose('true'), choose('输入值'), choose('跳过此输入'), choose('输入值'),
    choose('输入值'), choose('fast'), choose('输入值'), choose('确认保存工作区配置')]);
  for (const value of ['server-a', ' 1.50 ', '/native target/with spaces', '', '', '']) fixture.input.mockResolvedValueOnce(value);
  await fixture.run(route, original);
  const expected = { '--tag': ['red', 'red', 'blue'], '--number': ' 1.50 ', '--boolean': 'true',
    '--directory': '/native target/with spaces', '--empty': '', label: '', MCP_PLAIN_VALUE: 'fast', MCP_EMPTY_VALUE: '' };
  for (const call of [fixture.plan.mock.calls[0]![2], fixture.draft.mock.calls[0]![2].plan!.candidate!, fixture.save.mock.calls[0]![2].plan!.candidate!]) {
    expect(call.inputs).toEqual(expected);
    expect(call.inputs).not.toHaveProperty('--port'); expect(call.inputs).not.toHaveProperty('--optional');
  }
  expect(fixture.write).not.toHaveBeenCalled();
  const required = fixture.input.mock.calls.find(([options]) => options.title === '配置 MCP 输入 --number')![0];
  expect(await required.validateInput!('')).toBe('此输入不能为空字符串');
  const template = fixture.input.mock.calls.find(([options]) => options.title === '配置 MCP 输入 label')![0];
  expect(await template.validateInput!('')).toBeUndefined();
});

test('所有输入完成后才写私有env，后续候选只携带精确引用', async () => {
  const original = { ...published, configuration_inputs: [inputDefinition('FIRST_SECRET', { is_secret: true }),
    inputDefinition('--after-secret'), inputDefinition('LAST_SECRET', { is_secret: true, is_required: false, can_skip: true })] };
  const fixture = prepare([choose('暂不启用此 Server'), choose('输入值'), choose('确认保存工作区配置')]);
  let entered = 0;
  const values = ['server-a', 'first-private-value', 'ordinary-value', 'last-private-value'];
  fixture.input.mockImplementation(async () => {
    expect(fixture.write).not.toHaveBeenCalled();
    const value = values[entered++];
    if (entered > 1) expect(fixture.read).toHaveBeenCalledTimes(1);
    return value;
  });
  await fixture.run(route, original);
  expect(entered).toBe(4); expect(fixture.write).toHaveBeenCalledTimes(2);
  const expectedInputs: Record<string, unknown> = { '--after-secret': 'ordinary-value' };
  for (const [index, key] of ['FIRST_SECRET', 'LAST_SECRET'].entries()) {
    const request = fixture.write.mock.calls[index]!;
    expect(request[1]).toEqual(workspaceRoute); expect(request[3]).toBe(fixture.controller.signal);
    expect(request[2]).toMatchObject({ target: { kind: 'workspace', ...workspace }, server_id: 'server-a', input_key: key,
      value: index ? 'last-private-value' : 'first-private-value', candidate: { ...published.candidate, server_id: 'server-a' } });
    expect(request[2].candidate?.inputs).toEqual(expectedInputs);
    expectedInputs[key] = { kind: 'mcp_private_env', workspace_id: workspace.workspace_id, server_id: 'server-a',
      input_key: key, definition_digest: digest, version: 'f'.repeat(32) };
  }
  for (const key of ['FIRST_SECRET', 'LAST_SECRET']) {
    const options = fixture.input.mock.calls.find(([options]) => options.title === `配置 MCP 输入 ${key}`)![0];
    expect(options.password).toBe(true); expect(options).not.toHaveProperty('value');
  }
  for (const call of [fixture.plan.mock.calls[0]![2], fixture.draft.mock.calls[0]![2], fixture.save.mock.calls[0]![2]]) {
    expect(JSON.stringify(call)).not.toContain('first-private-value'); expect(JSON.stringify(call)).not.toContain('last-private-value');
  }
  expect(fixture.plan.mock.calls[0]![2].inputs).toEqual(expectedInputs);
  expect(fixture.draft.mock.calls[0]![2].plan?.candidate?.inputs).toEqual(expectedInputs);
  expect(fixture.save.mock.calls[0]![2].plan?.candidate?.inputs).toEqual(expectedInputs);
  expect(original.candidate.inputs).toEqual({});
});

test.each(['text', 'optional', 'repeated'])('收集%s输入时取消，已输入的秘密不写私有env或候选配置', async stage => {
  const next = stage === 'optional' ? inputDefinition('--next', { is_required: false, can_skip: true })
    : inputDefinition('--next', { is_repeated: stage === 'repeated' });
  const original = { ...published, configuration_inputs: [inputDefinition('TOKEN', { is_secret: true }), next] };
  const fixture = prepare(stage === 'text' ? [choose('暂不启用此 Server')] : [choose('暂不启用此 Server'), () => undefined]);
  fixture.input.mockResolvedValueOnce('server-a').mockResolvedValueOnce('collected-private-value').mockResolvedValueOnce(undefined);
  await fixture.run(route, original);
  expect(fixture.write).not.toHaveBeenCalled(); expect(fixture.plan).not.toHaveBeenCalled();
  expect(fixture.draft).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});

test.each(['identity', 'stopped'])('收集结束时%s失效阻止已收集秘密写入原工作区', async kind => {
  const original = { ...published, configuration_inputs: [inputDefinition('TOKEN', { is_secret: true }), inputDefinition('--last')] };
  const fixture = prepare([choose('暂不启用此 Server')]);
  fixture.input.mockResolvedValueOnce('server-a').mockResolvedValueOnce('collected-private-value').mockImplementationOnce(async (_options, token) => {
    if (kind === 'identity') {
      fixture.controller.abort(new Error('原身份已失效')); expect(token.isCancellationRequested).toBe(true);
    } else fixture.update({ workspaces: [{ ...registered, status: 'stopped' }] });
    return 'last-value';
  });
  await expect(fixture.run(route, original)).rejects.toThrow(kind === 'identity' ? '原身份已失效' : '原目标工作区尚未就绪');
  expect(fixture.write).not.toHaveBeenCalled(); expect(fixture.plan).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});

test('私有env写入失败保留原错误，不重试、不生成候选计划或保存', async () => {
  const original = { ...published, configuration_inputs: [inputDefinition('TOKEN', { is_secret: true })] };
  const fixture = prepare([choose('暂不启用此 Server')]);
  fixture.input.mockResolvedValueOnce('server-a').mockResolvedValueOnce('private-value');
  const cause = new HttpRequestError(409, 'Conflict', '私有定义已变化', '/candidate-plans/private-env', 'private-write-request');
  fixture.write.mockRejectedValueOnce(cause);
  await expect(fixture.run(route, original)).rejects.toBe(cause);
  expect(fixture.write).toHaveBeenCalledTimes(1); expect(fixture.plan).not.toHaveBeenCalled();
  expect(fixture.draft).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});

test('配置声明不可用明确显示原因，不按空输入读取工作区或生成计划', async () => {
  const fixture = prepare();
  await fixture.run(route, { ...published, configuration_unavailable_reason: 'secret argument 尚无私有引用能力' });
  expect(fixture.warnings).toEqual(['MCP 制品 chosen 1.0.0 已在 目标 A 发布。工作区配置不可用：secret argument 尚无私有引用能力']);
  expect(fixture.prompts).toEqual([]); expect(fixture.refresh).not.toHaveBeenCalled(); expect(fixture.read).not.toHaveBeenCalled();
  expect(fixture.write).not.toHaveBeenCalled(); expect(fixture.plan).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});

test('原source不可写时不开始参数收集或私有env写入', async () => {
  const original = { ...published, configuration_inputs: [inputDefinition('TOKEN', { is_secret: true })] };
  const fixture = prepare([choose('暂不启用此 Server')]);
  fixture.read.mockResolvedValueOnce({ ...document, writable: false });
  await expect(fixture.run(route, original)).rejects.toThrow('原工作区配置来源不可写');
  expect(fixture.input).toHaveBeenCalledTimes(1); expect(fixture.write).not.toHaveBeenCalled(); expect(fixture.plan).not.toHaveBeenCalled();
});

test('原published描述符在Server ID输入期间变化不替换本次冻结输入', async () => {
  const original = { ...published, configuration_inputs: [inputDefinition('--choice', { choices: ['original-choice'] })] };
  const fixture = prepare([choose('暂不启用此 Server'), choose('original-choice'), choose('确认保存工作区配置')]);
  fixture.input.mockImplementationOnce(async () => {
    original.configuration_inputs[0]!.key = '--changed'; original.configuration_inputs[0]!.choices![0] = 'changed-choice';
    return 'server-a';
  });
  await fixture.run(route, original);
  expect(fixture.plan.mock.calls[0]![2].inputs).toEqual({ '--choice': 'original-choice' });
});

test('真实HTTP可选字段为null时仍收集输入，不虚构默认值或配置不可用', async () => {
  const original = { ...published, configuration_unavailable_reason: null,
    configuration_inputs: [{ ...inputDefinition('label', { is_required: false, can_skip: false }),
      description: null, default_value: null, choices: null }] } as unknown as PublishedMcpArtifact;
  const fixture = prepare([choose('暂不启用此 Server'), choose('确认保存工作区配置')]);
  fixture.input.mockResolvedValueOnce('server-a').mockResolvedValueOnce('');
  await fixture.run(route, original);
  expect(fixture.warnings).toEqual([]); expect(fixture.save).toHaveBeenCalledTimes(1);
  expect(fixture.plan.mock.calls[0]![2].inputs).toEqual({ label: '' });
  expect(fixture.input.mock.calls[1]![0].prompt).not.toContain('null');
});

test('必需choices不提供空字符串选项', async () => {
  const original = { ...published, configuration_inputs: [inputDefinition('--choice', { choices: ['', 'valid'] })] };
  const fixture = prepare([choose('暂不启用此 Server'), items => {
    expect(items.filter(item => item.type !== 'separator').map(item => item.label)).toEqual(['valid']);
    return choose('valid')(items);
  }, choose('确认保存工作区配置')]);
  await fixture.run(route, original);
  expect(fixture.plan.mock.calls[0]![2].inputs).toEqual({ '--choice': 'valid' });
});
