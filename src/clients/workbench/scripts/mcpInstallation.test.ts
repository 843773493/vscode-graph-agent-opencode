import { afterEach, expect, spyOn, test } from 'bun:test';
import * as api from '../../web/src/api/mcpConnections.js';
import { galleryInstallationSource, publishMcpArtifact } from '../src/mcp/installation.js';
import type { IGalleryMcpServer } from '../upstream/src/vs/platform/mcp/common/mcpManagement.js';
import type { IQuickInputService } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { McpCandidateInputDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';

const workspace = Object.freeze({ gateway_id: 'gateway_00000000000040008000000000000001', workspace_id: '00000000-0000-4000-8000-000000000001' });
const target = { kind: 'backend', gateway_id: workspace.gateway_id, backend_id: '00000000-0000-4000-8000-000000000011' };
const route = Object.freeze({ ...target, gateway_connection_id: 'original-connection' });
const digest = 'sha256:jcs:v1:' + 'a'.repeat(64);
const gallery = { name: 'org.example/same', version: 'release-1',
  installationSource: { endpoint: 'https://source.test', protocol: 'v0.1', entryUrl: 'https://source.test/v0.1/servers/org.example%2Fsame/versions/release-1' },
} as unknown as IGalleryMcpServer;
const artifact = { binding: { backend_id: target.backend_id, installation_id: 'b'.repeat(32), generation: 'c'.repeat(32), artifact_digest: digest },
  source: {}, manifest_digest: digest, variant: { kind: 'package', index: 1, digest }, platform: {}, registry_type: 'pypi', identifier: 'chosen', version: '1.0.0',
  entrypoint: 'explicit-entry', command: '/published/explicit-entry', lock_digest: 'sha256:' + 'e'.repeat(64), installed_distributions: { chosen: '1.0.0' } };
const envelope = <T>(data: T) => ({ code: 0, message: 'ok', data, request_id: 'request', target, protocol_version: 1 });
const mocks: { mockRestore(): void }[] = [];
afterEach(() => { mocks.splice(0).forEach(mock => mock.mockRestore()); });

function prepare(registry: 'pypi' | 'npm' = 'pypi') {
  const capabilities = spyOn(api, 'getMcpInstallationCapabilities').mockResolvedValue(envelope({ platform: {}, pypi_wheel_available: registry === 'pypi',
    npm_available: registry === 'npm', unsupported_registry_types: [], limitations: [] }));
  const variants = [0, 1].map(index => ({
    ref: { kind: 'package', index, digest }, transport: 'stdio', registry_type: registry, identifier: index ? 'chosen' : 'wrong-first', version: '1.0.0',
    configuration_inputs: [] as McpCandidateInputDTO[], configuration_unavailable_reason: undefined as string | undefined }));
  const inspect = spyOn(api, 'inspectMcpInstallationCandidate').mockResolvedValue(envelope({ source: {}, manifest_digest: digest, platform: {}, variants }));
  const published = registry === 'npm' ? { ...artifact, registry_type: registry, command: '/published/node_modules/chosen/bin/server.js',
    runtime_executable: '/target/bin/node', runtime_version: 'v24.22.1' } : artifact;
  const plan = spyOn(api, 'planMcpInstallation').mockResolvedValue(envelope({ source: {}, manifest_digest: digest, variant: artifact.variant, platform: {}, plan_digest: digest,
    disposition: 'requires_artifact', download_required: true, parameters: {} }));
  const install = spyOn(api, 'installMcpArtifact').mockResolvedValue(envelope({ operation_id: 'operation', intent_digest: digest, status: 'succeeded', stage: 'complete', created_at: 1, updated_at: 2, log: '', artifact: published }));
  const input = spyOn({ input: async (_options: { prompt?: string }) => 'explicit-entry' }, 'input');
  mocks.push(capabilities, inspect, plan, install, input);
  const quickInput = { pick: async (items: { variant: { ref: { index: number } } }[]) => items[1], input } as unknown as IQuickInputService;
  return { capabilities, inspect, plan, install, input, quickInput, published, variants };
}

test('原安装工作流使用来源owner给出的精确URI和用户选择，返回发布回执不生成local配置', async () => {
  const { capabilities, inspect, plan, install, quickInput } = prepare();
  const signal = new AbortController().signal;
  const receipt = await publishMcpArtifact(gallery, route, '目标 A', quickInput, { report() {} }, CancellationToken.None, signal);
  expect(receipt.artifact).toEqual(artifact);
  expect(receipt.candidate).toEqual(plan.mock.calls[0]![2]);
  expect(receipt.candidate).not.toHaveProperty('server_id');
  expect(receipt.candidate.inputs).toEqual({});
  expect(receipt.configuration_inputs).toEqual([]);
  expect(plan.mock.calls[0]![2].variant?.index).toBe(1);
  expect(plan.mock.calls[0]![2].source?.entry_url).toBe(galleryInstallationSource(gallery).entry_url);
  expect(install.mock.calls[0]![2]).toMatchObject({ entrypoint: 'explicit-entry', candidate: { target, variant: { index: 1 } } });
  expect(plan.mock.calls[0]![3]).toBe(signal);
  expect(install.mock.calls[0]![3]).toBe(signal);
  expect(receipt).not.toHaveProperty('local');
  for (const call of [capabilities.mock.calls[0]!, inspect.mock.calls[0]!, plan.mock.calls[0]!, install.mock.calls[0]!]) {
    expect(call[1]).toEqual(route); expect(call[1]).not.toHaveProperty('workspace_id');
  }
});

test('冻结显式非首项的配置声明，安装期间不收集工作区输入', async () => {
  const fixture = prepare();
  const descriptor: McpCandidateInputDTO = { key: '--path', description: '原说明', is_required: true, can_skip: false,
    format: 'filepath', default_value: '/original/path', choices: undefined, is_secret: false, is_repeated: false };
  fixture.variants[0]!.configuration_inputs.push({ ...descriptor, key: 'wrong-first' });
  fixture.variants[1]!.configuration_inputs.push(descriptor);
  const expected = structuredClone(descriptor);
  fixture.plan.mockImplementationOnce(async () => {
    descriptor.description = '迟到修改'; descriptor.default_value = '/changed/path';
    fixture.variants[1]!.configuration_inputs.push({ ...descriptor, key: 'late' });
    return envelope({ source: {}, manifest_digest: digest, variant: artifact.variant, platform: {}, plan_digest: digest,
      disposition: 'requires_artifact', download_required: true, parameters: {} });
  });
  const receipt = await publishMcpArtifact(gallery, route, '目标 A', fixture.quickInput, { report() {} }, CancellationToken.None, new AbortController().signal);
  expect(receipt.configuration_inputs).toEqual([expected]);
  expect(fixture.input).toHaveBeenCalledTimes(1);
  expect(fixture.plan.mock.calls[0]![2].inputs).toEqual({});
  expect(fixture.install.mock.calls[0]![2].candidate?.inputs).toEqual({});
});

test('配置声明不可用仍允许发布制品，并保留目标给出的具体原因', async () => {
  const fixture = prepare();
  fixture.variants[1]!.configuration_unavailable_reason = 'secret argument 尚无私有引用能力';
  const receipt = await publishMcpArtifact(gallery, route, '目标 A', fixture.quickInput, { report() {} }, CancellationToken.None, new AbortController().signal);
  expect(receipt.artifact).toEqual(artifact);
  expect(receipt.configuration_unavailable_reason).toBe('secret argument 尚无私有引用能力');
  expect(fixture.install).toHaveBeenCalledTimes(1);
});

test('安装结果未知时不重放原意图，也不生成虚假的已安装回执', async () => {
  const { install, quickInput } = prepare();
  install.mockRejectedValueOnce(new Error('socket disconnected'));
  await expect(publishMcpArtifact(gallery, route, '目标 A', quickInput, { report() {} }, CancellationToken.None, new AbortController().signal)).rejects.toThrow('不能自动重放');
  expect(install).toHaveBeenCalledTimes(1);
});

test('安装已确认running后身份立即失效仍保留原操作ID，不查询、取消或重放', async () => {
  const fixture = prepare(), controller = new AbortController();
  const reason = new Error('原身份已失效');
  const query = spyOn(api, 'getMcpInstallationOperation').mockRejectedValue(new Error('失效身份不能查询'));
  const cancel = spyOn(api, 'cancelMcpInstallationOperation').mockRejectedValue(new Error('失效身份不能取消'));
  mocks.push(query, cancel);
  fixture.install.mockImplementationOnce(async (_port, _route, request) => {
    controller.abort(reason);
    return envelope({ operation_id: request.operation_id, intent_digest: digest, status: 'running', stage: 'resolve',
      created_at: 1, updated_at: 2, log: '' });
  });
  const failure = await publishMcpArtifact(gallery, route, '目标 A', fixture.quickInput, { report() {} }, CancellationToken.None,
    controller.signal).catch((cause: unknown) => cause);
  expect(failure).toBeInstanceOf(Error); expect((failure as Error).cause).toBe(reason);
  expect((failure as Error).message).toContain(fixture.install.mock.calls[0]![2].operation_id);
  expect((failure as Error).message).toContain('原操作可能仍在运行');
  expect(fixture.install).toHaveBeenCalledTimes(1); expect(query).not.toHaveBeenCalled(); expect(cancel).not.toHaveBeenCalled();
});

test('npm目标不依赖PyPI能力，保留明确bin和发布时绑定的Node', async () => {
  const { install, input, quickInput, published } = prepare('npm');
  const receipt = await publishMcpArtifact(gallery, route, '目标 B', quickInput, { report() {} }, CancellationToken.None, new AbortController().signal);
  expect(receipt.artifact).toEqual(published);
  expect(input.mock.calls[0]![0].prompt).toContain('根 package 的 bin 名称');
  expect(install.mock.calls[0]![2]).toMatchObject({ entrypoint: 'explicit-entry', candidate: { target, variant: { index: 1 } } });
});

test.each(['pypi', 'npm'] as const)('选定%s生态不可用时保留目标原因，不转投其它生态', async registry => {
  const { capabilities, inspect, plan, install, quickInput } = prepare(registry);
  capabilities.mockResolvedValue(envelope({ platform: {}, pypi_wheel_available: registry !== 'pypi', npm_available: registry !== 'npm',
    unavailable_reason: '目标 uv 不可用', npm_unavailable_reason: '目标 Node/npm 不可用', unsupported_registry_types: [], limitations: [] }));
  await expect(publishMcpArtifact(gallery, route, '目标 B', quickInput, { report() {} }, CancellationToken.None, new AbortController().signal))
    .rejects.toThrow(registry === 'npm' ? '目标 Node/npm 不可用' : '目标 uv 不可用');
  expect(inspect).toHaveBeenCalledTimes(1);
  expect(plan).not.toHaveBeenCalled();
  expect(install).not.toHaveBeenCalled();
});

test('没有精确目录来源时拒绝展示投影，不在前端猜URI', () => {
  expect(() => galleryInstallationSource({ name: 'same', version: '1' } as IGalleryMcpServer)).toThrow('精确来源');
});
