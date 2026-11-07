import { afterEach, expect, spyOn, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import * as api from '../../web/src/api/mcpConnections.js';
import * as sources from '../../web/src/api/configSources.js';
import * as http from '../../web/src/api/http.js';
import { addMcpConfiguration } from '../src/mcp/addConfiguration.js';
import { workspaceContext } from '../src/workspaceContext.js';
import { workspaceFileUri } from '../src/workspaceFiles.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import type { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';
import type { IInputOptions, IQuickInputService, IQuickPickItem, QuickPickInput } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { INotificationService, IPromptChoice, IPromptOptions } from '../upstream/src/vs/platform/notification/common/notification.js';
import type { ConfigSourceDocumentDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { GatewayWorkspace } from '../../web/src/types/backend.js';

const workspace = { gateway_id: 'gateway_00000000000040008000000000000001', workspace_id: '00000000-0000-4000-8000-000000000001' };
const target = { kind: 'backend', gateway_id: workspace.gateway_id, backend_id: '00000000-0000-4000-8000-000000000011' };
const installation = { ...target, gateway_connection_id: 'original-connection' };
const route = { ...workspace, gateway_connection_id: installation.gateway_connection_id };
const root = `boxteam://workspace/${workspace.workspace_id}/resources/files/root`;
const folder = workspaceFileUri({ gatewayId: workspace.gateway_id, workspaceId: workspace.workspace_id }, root, root);
const registered: GatewayWorkspace = { ...workspace, backend_id: target.backend_id, name: '原工作区', backend_url: 'http://workspace.test',
  connection_kind: 'local', status: 'ready', active: true, managed: true, removable: true, system_default: false,
  checked_at: '2026-11-07T00:00:00Z', affected_workspace_ids: [], remote_gateways: [], services: {} };
const digest = 'sha256:jcs:v1:' + 'a'.repeat(64);
const document: ConfigSourceDocumentDTO = { ...workspace, source_key: 'workspace_root_mutable_override', layer: 'workspace', writable: true,
  jsonc: '// 保留原来源与注释\r\n{"original":true}\r\n', file_presence: 'present', file_digest: 'original-file', layer_revision: 3,
  layer_digest: 'original-layer', source_generation: 'original-generation', active_revision: 2, active_digest: 'original-active',
  json_schema: {}, effective: {}, source_effective: {}, layers: [], origins: [], status: undefined };
const generated = '// 原 owner 返回的提案\r\n{"mcp":{"servers":{"manual-server":{"enabled":false}}}}\r\n';
const mocks: { mockRestore(): void }[] = [];
afterEach(() => { mocks.splice(0).forEach(mock => mock.mockRestore()); });
type Step = (items: QuickPickInput<IQuickPickItem>[], token: CancellationToken) => IQuickPickItem | undefined;
const choose = (label: string): Step => items => {
  const item = items.find(item => item.type !== 'separator' && item.label === label);
  expect(item).toBeDefined(); return item as IQuickPickItem;
};
const stdio = ['原工作区', 'stdio', '完成参数', '使用目标工作区默认目录', '完成环境变量', '暂不启用此 Server', '确认保存工作区配置'];
const httpSteps = ['原工作区', 'Streamable HTTP', '完成HTTP Header', '暂不启用此 Server', '确认保存工作区配置'];

function prepare(labels: (string | Step)[] = stdio, values: (string | undefined)[] = ['manual-server', '/native/runtime']) {
  const steps = labels.map(item => typeof item === 'string' ? choose(item) : item), events: string[] = [];
  let snapshot = { ...workspaceContext.getSnapshot(), gatewayId: workspace.gateway_id, workspaces: [registered], loading: false, error: null };
  const context = spyOn(workspaceContext, 'getSnapshot').mockImplementation(() => snapshot);
  const refresh = spyOn(workspaceContext, 'refresh').mockResolvedValue();
  const read = spyOn(sources, 'getConfigSourceDocument').mockResolvedValue(document);
  const roots = spyOn(http, 'requestJson').mockResolvedValue({ code: 0, message: 'ok', request_id: 'roots', data: { workspace_uri: root } });
  const inspect = spyOn(api, 'inspectMcpInstallationCandidate').mockImplementation(async (_port, _route, request) => {
    events.push('inspect');
    return { code: 0, message: 'ok', request_id: 'inspect', target, protocol_version: 1, data: {
      source: { kind: 'manual', name: request.source!.name, version: request.source!.version }, manifest_digest: digest,
      platform: { operating_system: 'linux', architecture: 'x86_64' }, variants: [{ ref: { kind: 'manual', index: 0, digest },
        transport: String(request.source!.definition!.transport), configuration_inputs: [] }],
    } };
  });
  const plan = spyOn(api, 'planMcpCandidateConfiguration').mockImplementation(async (_port, _route, request) => {
    events.push('plan');
    return { code: 0, message: 'ok', request_id: 'plan', target, protocol_version: 1, data: {
      source: { kind: 'manual', name: 'manual-server', version: 'user-defined' }, manifest_digest: digest, variant: request.variant,
      platform: { operating_system: 'linux', architecture: 'x86_64' }, plan_digest: digest,
      disposition: request.source!.definition!.transport === 'stdio' ? 'explicit_command' : 'configuration_only',
      download_required: false, parameters: { command: '不得消费的脱敏参数' },
    } };
  });
  const draft = spyOn(api, 'draftMcpCandidateConfiguration').mockImplementation(async (_port, _route, request) => {
    events.push('draft');
    return { source_edit: { ...request.source_edit!, jsonc: generated },
      preflight: { document, candidate: {}, candidate_digest: digest, changed_paths: ['mcp.servers.manual-server'] } };
  });
  const save = spyOn(api, 'saveMcpCandidateConfiguration').mockImplementation(async (_port, _route, request) => {
    events.push('save');
    return { ...document, edit: { idempotency_key: request.source_edit!.idempotency_key, candidate_id: 'source-candidate', state: 'pending',
      file_digest: 'saved-file', source_saved: true, layer_imported: true } };
  });
  const env = spyOn(api, 'writeMcpPrivateEnv').mockImplementation(async (_port, _route, request) => {
    events.push('private-env');
    return { kind: 'mcp_private_env', workspace_id: workspace.workspace_id, server_id: request.server_id,
      input_key: request.input_key, definition_digest: digest, version: 'b'.repeat(32) };
  });
  const header = spyOn(api, 'writeMcpPrivateHeader').mockImplementation(async (_port, _route, request) => {
    events.push('private-header');
    return { kind: 'mcp_private_header', workspace_id: workspace.workspace_id, server_id: request.server_id,
      header_name: request.header_name, endpoint: request.endpoint, version: 'c'.repeat(32) };
  });
  const install = spyOn(api, 'installMcpArtifact');
  mocks.push(context, refresh, read, roots, inspect, plan, draft, save, env, header, install);
  const input = spyOn({ input: async (_options: IInputOptions, _token: CancellationToken): Promise<string | undefined> => {
    events.push('input'); if (!values.length) throw new Error('计划外 input'); return values.shift();
  } }, 'input');
  const picks: { title?: string; prompt?: string }[] = [];
  const quick = { input, pick: async (items: QuickPickInput<IQuickPickItem>[], options: { title?: string; prompt?: string }, token: CancellationToken) => {
    events.push('pick'); picks.push(options); const step = steps.shift(); if (!step) throw new Error('计划外 pick'); return step(items, token);
  } } as unknown as IQuickInputService;
  const messages: string[] = [], prompts: { text: string; choices: IPromptChoice[]; options?: IPromptOptions }[] = [];
  const notifications = { info: (text: string) => messages.push(text), prompt: (_severity: unknown, text: string, choices: IPromptChoice[], options?: IPromptOptions) => {
    prompts.push({ text, choices, options }); queueMicrotask(() => choices[0]!.run()); return { close() { options?.onCancel?.(); } };
  } } as unknown as INotificationService;
  const controller = new AbortController();
  const run = (configUri?: URI | string, owner = installation, assertAccess = () => {}) =>
    addMcpConfiguration(owner, '目标 A', quick, notifications, assertAccess, controller.signal, configUri);
  return { run, events, input, picks, read, roots, inspect, plan, draft, save, env, header, install, messages, prompts, controller,
    update: (change: Partial<typeof snapshot>) => { snapshot = { ...snapshot, ...change }; } };
}

test('手动stdio沿原inspect→workspace plan→draft→唯一PUT，保留原CAS且不伪造制品', async () => {
  const fixture = prepare(); await fixture.run();
  expect(fixture.install).not.toHaveBeenCalled();
  expect(fixture.inspect.mock.calls[0]![1]).toEqual(installation);
  const candidate = fixture.plan.mock.calls[0]![2];
  expect(candidate).toEqual({ target, source: { kind: 'manual', name: 'manual-server', version: 'user-defined',
    definition: { transport: 'stdio', command: '/native/runtime', args: [], env: {} } }, manifest_digest: digest,
    variant: { kind: 'manual', index: 0, digest }, server_id: 'manual-server', inputs: {} });
  expect(fixture.draft.mock.calls[0]![2]).toMatchObject({ target: { kind: 'workspace', ...workspace }, server_id: 'manual-server', enabled: false,
    source_edit: { ...workspace, source_key: document.source_key, jsonc: document.jsonc, base_file_digest: document.file_digest,
      base_layer_revision: document.layer_revision, base_layer_digest: document.layer_digest,
      expected_active_revision: document.active_revision, expected_active_digest: document.active_digest } });
  expect(fixture.save).toHaveBeenCalledTimes(1);
  for (const call of [fixture.draft.mock.calls[0]!, fixture.save.mock.calls[0]!]) {
    expect(call[1]).toEqual(route); expect(call[3]).toBe(fixture.controller.signal);
    expect(call[2]).not.toHaveProperty('managed_launch'); expect(call[2].plan!.candidate).toEqual(candidate);
  }
  const saved = fixture.save.mock.calls[0]![2];
  expect(saved.source_edit!.jsonc).toBe(generated); expect(JSON.stringify(saved)).not.toContain('不得消费的脱敏参数');
  expect(fixture.messages[0]).toContain('source 状态 pending'); expect(fixture.messages[0]).not.toContain('ready');
});

test('command与有序argv分开收集，保留空字符串、引号、重复值、cwd和普通空env', async () => {
  const fixture = prepare(['原工作区', 'stdio', '添加一个参数', '添加一个参数', '添加一个参数', '添加一个参数', '完成参数',
    '指定工作目录', '添加普通环境变量', '完成环境变量', '启用此 Server', '确认保存工作区配置'],
  ['manual-server', '/native/command with spaces', '', '--label="literal quotes"', 'repeat', 'repeat', '${workspaceFolder}/working dir', 'EMPTY_ENV', '']);
  await fixture.run();
  expect(fixture.plan.mock.calls[0]![2].source!.definition).toEqual({ transport: 'stdio', command: '/native/command with spaces',
    args: ['', '--label="literal quotes"', 'repeat', 'repeat'], cwd: '${workspaceFolder}/working dir', env: { EMPTY_ENV: '' } });
  expect(fixture.draft.mock.calls[0]![2].enabled).toBe(true); expect(fixture.env).not.toHaveBeenCalled();
});

test('全部输入及启用选择完成才写私有env，最终inspect和保存只携带引用', async () => {
  let fixture: ReturnType<typeof prepare>;
  fixture = prepare(['原工作区', 'stdio', '完成参数', '指定工作目录', '添加私有环境变量', '添加普通环境变量', '完成环境变量',
    (items) => { expect(fixture.env).not.toHaveBeenCalled(); return choose('暂不启用此 Server')(items, {} as CancellationToken); }, '确认保存工作区配置'],
  ['manual-server', '/native/command', '/native/cwd', 'TOKEN', 'private-env-value', 'PLAIN', 'ordinary']);
  await fixture.run();
  expect(fixture.env).toHaveBeenCalledTimes(1);
  const call = fixture.env.mock.calls[0]!;
  expect(call[1]).toEqual(route); expect(call[2]).toEqual({ target: { kind: 'workspace', ...workspace }, server_id: 'manual-server', input_key: 'TOKEN',
    launch: { command: '/native/command', args: [], cwd: '/native/cwd' }, value: 'private-env-value' });
  expect(call[3]).toBe(fixture.controller.signal);
  const candidate = fixture.inspect.mock.calls[0]![2].source!.definition!;
  expect(candidate.env).toEqual({ PLAIN: 'ordinary', TOKEN: await fixture.env.mock.results[0]!.value });
  expect(fixture.events.indexOf('private-env')).toBeLessThan(fixture.events.indexOf('inspect'));
  expect(JSON.stringify([fixture.plan.mock.calls, fixture.draft.mock.calls, fixture.save.mock.calls, fixture.messages, fixture.picks])).not.toContain('private-env-value');
  expect(fixture.input.mock.calls.find(([options]) => options.title?.includes('TOKEN'))![0].password).toBe(true);
  expect(fixture.picks.at(-1)!.prompt).toContain('取消保存不会删除该版本');
});

test('Streamable HTTP普通/私有Header沿精确endpoint写引用，没有安装或managed_launch', async () => {
  const endpoint = 'https://mcp.example.test/v1?scope=exact';
  const fixture = prepare(['原工作区', 'Streamable HTTP', '添加普通HTTP Header', '添加私有HTTP Header', '完成HTTP Header',
    '暂不启用此 Server', '确认保存工作区配置'], ['manual-server', endpoint, 'X-Empty', '', 'Authorization', 'Bearer private-header-value']);
  await fixture.run();
  expect(fixture.header).toHaveBeenCalledTimes(1); expect(fixture.env).not.toHaveBeenCalled(); expect(fixture.install).not.toHaveBeenCalled();
  const call = fixture.header.mock.calls[0]!;
  expect(call[1]).toEqual(route); expect(call[2]).toEqual({ target: { kind: 'workspace', ...workspace }, server_id: 'manual-server',
    header_name: 'Authorization', endpoint, value: 'Bearer private-header-value' });
  expect(fixture.plan.mock.calls[0]![2].source!.definition).toEqual({ transport: 'streamable_http', url: endpoint,
    headers: { 'X-Empty': '', Authorization: await fixture.header.mock.results[0]!.value } });
  expect(fixture.draft.mock.calls[0]![2]).not.toHaveProperty('managed_launch');
  expect(JSON.stringify([fixture.inspect.mock.calls, fixture.plan.mock.calls, fixture.save.mock.calls])).not.toContain('private-header-value');
});

test.each(['${MCP_ENDPOINT}', 'http://remote.example.test/mcp', 'https://user:pass@example.test/mcp', 'https://example.test/mcp#fragment', ' https://example.test/mcp'])(
  '私有Header不能使用未解析或不合法endpoint %s，零私有写入', async endpoint => {
    const fixture = prepare(['原工作区', 'Streamable HTTP', '添加私有HTTP Header', '完成HTTP Header'], ['manual-server', endpoint, 'Authorization', 'Bearer value']);
    await expect(fixture.run()).rejects.toThrow();
    expect(fixture.header).not.toHaveBeenCalled(); expect(fixture.inspect).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
  },
);

test('无私有Header的目标环境URL表达式原样交候选owner，不在浏览器解析', async () => {
  const fixture = prepare(httpSteps, ['manual-server', '${MCP_ENDPOINT}']); await fixture.run();
  expect(fixture.plan.mock.calls[0]![2].source!.definition!.url).toBe('${MCP_ENDPOINT}'); expect(fixture.header).not.toHaveBeenCalled();
});

test.each(['stdio', 'http'])('收集%s私有值后取消后续输入或启用选择，零私有POST', async transport => {
  const fixture = prepare(transport === 'stdio'
    ? ['原工作区', 'stdio', '完成参数', '使用目标工作区默认目录', '添加私有环境变量', '完成环境变量', () => undefined]
    : ['原工作区', 'Streamable HTTP', '添加私有HTTP Header', '完成HTTP Header', () => undefined],
  ['manual-server', transport === 'stdio' ? '/native/command' : 'https://mcp.example.test', 'TOKEN', 'private-value']);
  await fixture.run();
  expect(fixture.env).not.toHaveBeenCalled(); expect(fixture.header).not.toHaveBeenCalled();
  expect(fixture.inspect).not.toHaveBeenCalled(); expect(fixture.plan).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});

test.each(['jsonc', 'source_effective', 'effective'])('同Server ID来自%s时拒绝Add，不能隐式upsert', async source => {
  const fixture = prepare(['原工作区'], ['manual-server']);
  const existing = { mcp: { servers: { 'manual-server': { enabled: false } } } };
  fixture.read.mockResolvedValueOnce(source === 'jsonc' ? { ...document, jsonc: JSON.stringify(existing) } : { ...document, [source]: existing });
  await expect(fixture.run()).rejects.toThrow('Add 不会覆盖已有配置');
  expect(fixture.inspect).not.toHaveBeenCalled(); expect(fixture.env).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});

test('原来源不可写时不收集定义或写秘密', async () => {
  const fixture = prepare(['原工作区'], ['manual-server']); fixture.read.mockResolvedValueOnce({ ...document, writable: false });
  await expect(fixture.run()).rejects.toThrow('来源不可写'); expect(fixture.input).toHaveBeenCalledTimes(1);
  expect(fixture.env).not.toHaveBeenCalled(); expect(fixture.inspect).not.toHaveBeenCalled();
});

test.each(['file:///native/.mcp.json', 'file:///native/.vscode/mcp.json', 'file:///native/workspace', folder.with({ path: folder.path + '/nested' }).toString()])(
  '明确来源%s不能静默换文件或任选工作区', async explicit => {
    const fixture = prepare([]); await expect(fixture.run(explicit)).rejects.toThrow();
    expect(fixture.input).not.toHaveBeenCalled(); expect(fixture.read).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
  },
);

test('显式文件夹只接受同冻结域的权威工作区根，直接带入而不另选工作区', async () => {
  const fixture = prepare(stdio.slice(1)); await fixture.run(folder);
  expect(fixture.roots).toHaveBeenCalledTimes(1);
  expect(fixture.roots.mock.calls[0]![2]).toMatchObject({ headers: http.gatewayWorkspaceRequestHeaders(route), signal: fixture.controller.signal });
  expect(fixture.picks.some(options => options.title === '选择 MCP 配置工作区')).toBe(false);
  expect(fixture.save.mock.calls[0]![1]).toEqual(route);
});

test.each(['foreign-gateway', 'foreign-backend', 'non-authoritative-root', 'stopped'])('显式folder在%s时拒绝且不降级选择', async state => {
  const fixture = prepare([]);
  if (state === 'foreign-gateway') fixture.update({ workspaces: [{ ...registered, gateway_id: 'other' }] });
  if (state === 'foreign-backend') fixture.update({ workspaces: [{ ...registered, backend_id: 'other' }] });
  if (state === 'non-authoritative-root') fixture.roots.mockResolvedValueOnce({ code: 0, data: { workspace_uri: root + '/other' } });
  if (state === 'stopped') fixture.update({ workspaces: [{ ...registered, status: 'stopped' }] });
  await expect(fixture.run(folder)).rejects.toThrow();
  expect(fixture.input).not.toHaveBeenCalled(); expect(fixture.read).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});

test('输入期间改变焦点、视图目标或原route对象不改投本次目标', async () => {
  const mutable = { ...installation };
  let fixture: ReturnType<typeof prepare>;
  fixture = prepare([...stdio.slice(0, -1), (items, token) => {
    mutable.backend_id = 'another-backend'; mutable.gateway_connection_id = 'another-connection'; fixture.update({ activeId: 'another-focus' });
    return choose('确认保存工作区配置')(items, token);
  }]);
  await fixture.run(undefined, mutable);
  expect(fixture.inspect.mock.calls[0]![1]).toEqual(installation);
  expect(fixture.save.mock.calls[0]![1]).toEqual(route); expect(fixture.save.mock.calls[0]![2].plan!.candidate!.target).toEqual(target);
});

test('启用选择时身份失效取消Quick Input，零私有写入和旧确认', async () => {
  let fixture: ReturnType<typeof prepare>;
  let token: CancellationToken | undefined;
  fixture = prepare(['原工作区', 'stdio', '完成参数', '使用目标工作区默认目录', '添加私有环境变量', '完成环境变量', (items, current) => {
    token = current; fixture.controller.abort(new Error('原身份失效')); return choose('暂不启用此 Server')(items, current);
  }], ['manual-server', '/native/command', 'TOKEN', 'secret']);
  await expect(fixture.run()).rejects.toThrow('原身份失效'); expect(token!.isCancellationRequested).toBe(true);
  expect(fixture.env).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});

test('确认期间工作区停止不保存，不借其它ready工作区', async () => {
  let fixture: ReturnType<typeof prepare>;
  fixture = prepare([...stdio.slice(0, -1), (items, token) => {
    fixture.update({ workspaces: [{ ...registered, status: 'stopped' }, { ...registered, workspace_id: 'other' }] });
    return choose('确认保存工作区配置')(items, token);
  }]);
  await expect(fixture.run()).rejects.toThrow('原目标工作区尚未就绪'); expect(fixture.save).not.toHaveBeenCalled();
});

test('私有写入结果未知保留输入身份、原因及request_id，不重试、不inspect或保存', async () => {
  const fixture = prepare(['原工作区', 'stdio', '完成参数', '使用目标工作区默认目录', '添加私有环境变量', '完成环境变量', '暂不启用此 Server'],
  ['manual-server', '/native/command', 'TOKEN', 'secret']);
  const cause = new http.HttpRequestError(503, 'Unavailable', 'private owner disconnected', '/private-env', 'private-write-request');
  fixture.env.mockRejectedValue(cause);
  const failure = await fixture.run().catch((error: unknown) => error) as Error;
  expect(failure.cause).toBe(cause); expect(failure.message).toContain('TOKEN 写入未确认'); expect(failure.message).toContain('private-write-request');
  expect(fixture.env).toHaveBeenCalledTimes(1); expect(fixture.inspect).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});

test('手动保存响应丢失复用共享原键查询，不重新plan/draft或重放PUT', async () => {
  const fixture = prepare(); fixture.save.mockRejectedValue(new Error('accepted then disconnected'));
  fixture.read.mockResolvedValueOnce(document).mockResolvedValueOnce(document).mockImplementation(async (_port, _route, _source, options) => ({ ...document,
    edit: { idempotency_key: options!.idempotencyKey!, candidate_id: 'original', state: 'pending', file_digest: 'saved', source_saved: true, layer_imported: true } }));
  await fixture.run();
  expect(fixture.save).toHaveBeenCalledTimes(1); expect(fixture.plan).toHaveBeenCalledTimes(1); expect(fixture.draft).toHaveBeenCalledTimes(1);
  const key = fixture.save.mock.calls[0]![2].source_edit!.idempotency_key;
  for (const call of fixture.read.mock.calls.slice(1)) {
    expect(call[1]).toEqual(route); expect(call[3]).toEqual({ idempotencyKey: key, signal: fixture.controller.signal });
  }
  expect(fixture.prompts[0]!.choices[0]!.label).toBe('查询原来源提交'); expect(fixture.messages[0]).toContain(`提交 ${key}`);
});

test('取消最终确认不会保存，已收取私有版本仍如实说明', async () => {
  const fixture = prepare(['原工作区', 'Streamable HTTP', '添加私有HTTP Header', '完成HTTP Header', '暂不启用此 Server', '取消'],
  ['manual-server', 'http://127.0.0.1/mcp', 'Authorization', 'Bearer secret']);
  await fixture.run(); expect(fixture.header).toHaveBeenCalledTimes(1); expect(fixture.save).not.toHaveBeenCalled();
  expect(fixture.picks.at(-1)!.prompt).toContain('私有版本已写入');
});

test('原Quick Input验证env名称、私有值和重复键，普通空值仍合法', async () => {
  const values = ['manual-server', '/native/command', 'TOKEN', 'private-value', 'EMPTY', ''];
  const fixture = prepare(['原工作区', 'stdio', '完成参数', '使用目标工作区默认目录', '添加私有环境变量', '添加普通环境变量',
    '完成环境变量', '暂不启用此 Server', '确认保存工作区配置'], []);
  let keys = 0;
  fixture.input.mockImplementation(async options => {
    if (options.title === '环境变量名称') {
      expect(await options.validateInput!('1INVALID')).toBeDefined(); expect(await options.validateInput!('has space')).toBeDefined();
      if (++keys === 2) expect(await options.validateInput!('TOKEN')).toContain('已存在');
    }
    if (options.title === '私有环境变量值 · TOKEN') {
      expect(options.password).toBe(true); expect(await options.validateInput!('')).toBeDefined();
      expect(await options.validateInput!('not\0allowed')).toBeDefined(); expect(await options.validateInput!(' exact value ')).toBeUndefined();
    }
    if (options.title === '普通环境变量值 · EMPTY') expect(await options.validateInput!('')).toBeUndefined();
    return values.shift();
  });
  await fixture.run(); expect(keys).toBe(2); expect(fixture.env).toHaveBeenCalledTimes(1);
});

test('原Quick Input验证HTTP token、大小写重复header及私有ASCII边界，不修剪原值', async () => {
  const values = ['manual-server', 'https://mcp.example.test', 'Authorization', 'Bearer private', 'X-Empty', ''];
  const fixture = prepare(['原工作区', 'Streamable HTTP', '添加私有HTTP Header', '添加普通HTTP Header', '完成HTTP Header',
    '暂不启用此 Server', '确认保存工作区配置'], []);
  let names = 0;
  fixture.input.mockImplementation(async options => {
    if (options.title === 'HTTP Header名称') {
      for (const invalid of ['', 'has space', '名字', 'x'.repeat(257)]) expect(await options.validateInput!(invalid)).toBeDefined();
      if (++names === 2) expect(await options.validateInput!('authorization')).toContain('已存在');
    }
    if (options.title === '私有HTTP Header值 · Authorization') {
      expect(options.password).toBe(true);
      for (const invalid of ['', ' padded', 'padded ', 'line\nfeed', 'not-ascii-中', 'x'.repeat(8193)]) expect(await options.validateInput!(invalid)).toBeDefined();
      expect(await options.validateInput!('Bearer exact-value')).toBeUndefined();
    }
    if (options.title === '普通HTTP Header值 · X-Empty') expect(await options.validateInput!('')).toBeUndefined();
    return values.shift();
  });
  await fixture.run(); expect(names).toBe(2); expect(fixture.header.mock.calls[0]![2].value).toBe('Bearer private');
});

test('含已收集私有值时取消后续普通字段，不产生提前写入', async () => {
  const fixture = prepare(['原工作区', 'stdio', '完成参数', '使用目标工作区默认目录', '添加私有环境变量', '添加普通环境变量'],
    ['manual-server', '/native/command', 'TOKEN', 'private-value', 'CANCELLED', undefined]);
  await fixture.run();
  expect(fixture.env).not.toHaveBeenCalled(); expect(fixture.header).not.toHaveBeenCalled(); expect(fixture.inspect).not.toHaveBeenCalled();
});

test('工作区根URI来自另一Gateway或显式null时不能进入工作区选择', async () => {
  const fixture = prepare([]);
  await expect(fixture.run(folder.with({ authority: 'different-gateway' }))).rejects.toThrow('精确工作区根');
  await expect(fixture.run(null as unknown as URI)).rejects.toThrow('可核验的工作区根 URI');
  expect(fixture.input).not.toHaveBeenCalled(); expect(fixture.read).not.toHaveBeenCalled();
});

test('目标返回多个manual variant时拒绝，不使用第一个候选', async () => {
  const fixture = prepare(stdio.slice(0, -1));
  fixture.inspect.mockResolvedValueOnce({ code: 0, message: 'ok', request_id: 'inspect', target, protocol_version: 1, data: {
    source: { kind: 'manual', name: 'manual-server', version: 'user-defined' }, manifest_digest: digest,
    platform: { operating_system: 'linux', architecture: 'x86_64' }, variants: [0, 1].map(index => ({
      ref: { kind: 'manual', index, digest }, transport: 'stdio', configuration_inputs: [],
    })),
  } });
  await expect(fixture.run()).rejects.toThrow('唯一可用的手动候选'); expect(fixture.plan).not.toHaveBeenCalled(); expect(fixture.save).not.toHaveBeenCalled();
});

test('原Add DI wrapper冻结MCP管理目标、贯穿同一身份signal并释放订阅，错误经通知结束', async () => {
  const text = await readFile(new URL('../src/mcp/commands.ts', import.meta.url), 'utf8');
  const source = ts.createSourceFile('commands.ts', text, ts.ScriptTarget.Latest, true);
  const node = source.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'addWorkspaceMcpConfiguration');
  if (!node) throw new Error('Add wrapper缺失');
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(node.getText().replace('export ', ''));
  const selected = { ...installation, name: '管理目标', connection_kind: 'local' }, calls: unknown[][] = [], errors: string[] = [];
  let invalidated = () => {}, releaseCount = 0;
  const run = new Function('IQuickInputService', 'INotificationService', 'workspaceContext', 'managementGateway', 'mcpManagementTarget',
    'onGatewayUserSessionInvalidated', 'addMcpConfiguration', 'mcpConfigurationErrorMessage', javascript + '\nreturn addWorkspaceMcpConfiguration;')(
    'quick', 'notifications', { getSnapshot: () => ({ gatewayId: workspace.gateway_id }), subscribe: () => () => { ++releaseCount; } },
    (scope: string) => { expect(scope).toBe('mcp'); return selected; },
    async (owner: object, signal: AbortSignal) => { calls.push([owner, signal]); return { ...owner }; },
    (_port: number, callback: () => void) => { invalidated = callback; return () => { ++releaseCount; }; },
    async (...args: unknown[]) => { calls.push(args); selected.backend_id = 'changed'; invalidated(); (args[4] as () => void)(); },
    (cause: Error) => cause.message,
  ) as (accessor: object, resource: URI) => Promise<void>;
  await run({ get: (id: string) => id === 'quick' ? {} : { error: (text: string) => errors.push(text) } }, folder);
  expect(calls[0]![0]).toMatchObject({ backend_id: target.backend_id });
  expect(calls[1]![0]).toEqual(installation); expect(calls[1]![6]).toBe(folder);
  expect(calls[0]![1]).toBe(calls[1]![5]); expect((calls[1]![5] as AbortSignal).aborted).toBe(true);
  expect(errors).toEqual(['Gateway 或用户身份已改变，请重新添加 MCP 配置']); expect(releaseCount).toBe(2);
});
