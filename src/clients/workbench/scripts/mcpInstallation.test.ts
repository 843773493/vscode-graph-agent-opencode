import { afterEach, expect, spyOn, test } from 'bun:test';
import * as api from '../../web/src/api/mcpConnections.js';
import { galleryInstallationSource, publishMcpArtifact } from '../src/mcp/installation.js';
import type { IGalleryMcpServer } from '../upstream/src/vs/platform/mcp/common/mcpManagement.js';
import type { IQuickInputService } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';

const workspace = Object.freeze({ gateway_id: 'gateway_00000000000040008000000000000001', workspace_id: '00000000-0000-4000-8000-000000000001' });
const target = { kind: 'backend', gateway_id: workspace.gateway_id, backend_id: '00000000-0000-4000-8000-000000000011' };
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

function prepare() {
  mocks.push(spyOn(api, 'getMcpInstallationCapabilities').mockResolvedValue(envelope({ platform: {}, pypi_wheel_available: true, unsupported_registry_types: [], limitations: [] })));
  mocks.push(spyOn(api, 'inspectMcpInstallationCandidate').mockResolvedValue(envelope({ source: {}, manifest_digest: digest, platform: {}, variants: [0, 1].map(index => ({
    ref: { kind: 'package', index, digest }, transport: 'stdio', registry_type: 'pypi', identifier: index ? 'chosen' : 'wrong-first', version: '1.0.0' })) })));
  const plan = spyOn(api, 'planMcpInstallation').mockResolvedValue(envelope({ source: {}, manifest_digest: digest, variant: artifact.variant, platform: {}, plan_digest: digest,
    disposition: 'requires_artifact', download_required: true, parameters: {} }));
  const install = spyOn(api, 'installMcpArtifact').mockResolvedValue(envelope({ operation_id: 'operation', intent_digest: digest, status: 'succeeded', stage: 'complete', created_at: 1, updated_at: 2, log: '', artifact }));
  mocks.push(plan, install);
  const quickInput = { pick: async (items: { variant: { ref: { index: number } } }[]) => items[1], input: async () => 'explicit-entry' } as unknown as IQuickInputService;
  return { plan, install, quickInput };
}

test('原安装工作流使用来源owner给出的精确URI和用户选择，返回发布回执不生成local配置', async () => {
  const { plan, install, quickInput } = prepare();
  const receipt = await publishMcpArtifact(gallery, workspace, '目标 A', quickInput, { report() {} }, CancellationToken.None);
  expect(receipt).toEqual(artifact);
  expect(plan.mock.calls[0]![2].variant?.index).toBe(1);
  expect(plan.mock.calls[0]![2].source?.entry_url).toBe(galleryInstallationSource(gallery).entry_url);
  expect(install.mock.calls[0]![2]).toMatchObject({ entrypoint: 'explicit-entry', candidate: { target, variant: { index: 1 } } });
  expect(receipt).not.toHaveProperty('local');
});

test('安装结果未知时不重放原意图，也不生成虚假的已安装回执', async () => {
  const { install, quickInput } = prepare();
  install.mockRejectedValueOnce(new Error('socket disconnected'));
  await expect(publishMcpArtifact(gallery, workspace, '目标 A', quickInput, { report() {} }, CancellationToken.None)).rejects.toThrow('不能自动重放');
  expect(install).toHaveBeenCalledTimes(1);
});

test('没有精确目录来源时拒绝展示投影，不在前端猜URI', () => {
  expect(() => galleryInstallationSource({ name: 'same', version: '1' } as IGalleryMcpServer)).toThrow('精确来源');
});
