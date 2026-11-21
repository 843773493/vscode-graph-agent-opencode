import { afterEach, expect, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import WarmConfirmProvider from '../../shell/WarmConfirmProvider';
import { DEFAULT_BACKGROUND } from '../../../theme/theme';
import { apiSuccessResponse, installGatewayFetch, restoreGatewayTestEnvironment } from '../../../tests/fixtures/session/gatewayEnvironment';
import GatewaySettingsContent, { type GatewaySettingsContentProps } from './GatewaySettingsContent';
import type { GatewayWorkspace } from '../../../types/backend';

afterEach(restoreGatewayTestEnvironment);
const noop = async () => {};
const props: GatewaySettingsContentProps = { page: 'connections', entryGatewayId: 'gateway_local', target: { gateway_id: 'gateway_local' }, workspaces: [], revision: 0,
  onCloseDialog: noop, onChanged: noop, onAddRemoteGatewayConnection: noop, onRefresh: noop, onRemoteGatewayReconnected: noop,
  onOpenConfiguration: noop, onOpenLogs: noop, onCloseLocation: noop, onOpenLog: noop, background: DEFAULT_BACKGROUND,
  onUpdateBackground: noop, onStart: noop, onStop: noop, onSafeRestart: noop, onForceRestart: noop };
async function mount(overrides: Partial<GatewaySettingsContentProps> = {}) {
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(<WarmConfirmProvider><GatewaySettingsContent {...props} {...overrides} /></WarmConfirmProvider>);
    // lazy 内容与 owner 请求均完成后再断言，不用占位 DOM 冒充管理列表。
    await new Promise(resolve => setTimeout(resolve, 30));
  });
  return renderer;
}

test('健康读取失败仍使用冻结入口读取连接目录，保留 request_id', async () => {
  const paths: string[] = [];
  installGatewayFetch(({ path }) => {
    paths.push(path);
    if (path === '/api/gateway/health') return Response.json({ detail: '健康读取被拒绝', request_id: 'req_denied' },
      { status: 503, headers: { 'X-Request-ID': 'req_denied' } });
    if (path === '/api/gateway/management/gateways') return apiSuccessResponse({ local_gateway_id: 'gateway_local', items: [] });
    return undefined;
  });
  const renderer = await mount();
  try {
    expect(renderer.root.findByProps({ role: 'alert' }).children.join('')).toContain('req_denied');
    expect(paths).toContain('/api/gateway/management/gateways');
    expect(paths).not.toContain('/api/gateway/device-connections');
    expect(JSON.stringify(renderer.toJSON())).toContain('gateway_local');
    expect(renderer.root.findAllByType('nav')).toHaveLength(0);
  } finally { await act(async () => renderer.unmount()); }
});

test('设备访问按需读取；未打开连接页时不追加健康检查', async () => {
  const paths: string[] = [];
  installGatewayFetch(({ path }) => {
    paths.push(path);
    if (path === '/api/gateway/inbound-access') return apiSuccessResponse({ gateway_id: 'gateway_local', peers: [], items: [] });
    if (path === '/api/gateway/device-connections') return apiSuccessResponse({ items: [] });
    return undefined;
  });
  const renderer = await mount({ page: 'credentials' });
  try {
    expect(JSON.stringify(renderer.toJSON())).toContain('暂无已授权的外部设备');
    expect(paths).toContain('/api/gateway/inbound-access');
    expect(paths).not.toContain('/api/gateway/health');
  } finally { await act(async () => renderer.unmount()); }
});

test('工作区运行管理只展示所属完整 Backend 组，操作仍使用原组目标', async () => {
  const workspace = (id: string, generation: string, members: string[]): GatewayWorkspace => ({
    gateway_id: 'gateway_local', workspace_id: id, name: `工作区-${id}`, backend_url: 'http://127.0.0.1:8010',
    connection_kind: 'local', status: 'ready', managed: true, removable: true, runtime_action: 'start_managed_backend',
    services: {}, active: false, system_default: false,
    checked_at: '', backend_generation: generation, affected_workspace_ids: members, parent_workspace: undefined, remote_gateways: [],
  });
  const calls: string[] = [];
  const workspaces = [workspace('a', 'shared', ['a', 'b']), workspace('b', 'shared', ['a', 'b']), workspace('c', 'other', ['c'])];
  const renderer = await mount({ page: 'runtime', target: { gateway_id: 'gateway_local', workspace_id: 'b' }, workspaces,
    onSafeRestart: async id => { calls.push(id); } });
  try {
    const groups = renderer.root.findAllByType('article');
    expect(groups).toHaveLength(1);
    expect(JSON.stringify(groups[0].findByType('h2').children)).toContain('工作区-a');
    expect(JSON.stringify(groups[0].findByType('h2').children)).toContain('工作区-b');
    expect(JSON.stringify(renderer.toJSON())).not.toContain('工作区-c');
    const restart = renderer.root.findAllByType('button').find(button => button.children.join('') === '安全重启')!;
    await act(async () => { await restart.props.onClick(); });
    expect(calls).toEqual([JSON.stringify(['gateway_local', 'a'])]);
    await act(async () => { renderer.update(<WarmConfirmProvider><GatewaySettingsContent {...props} page="runtime" workspaces={workspaces} /></WarmConfirmProvider>); });
    expect(renderer.root.findAllByType('article')).toHaveLength(2);
  } finally { await act(async () => renderer.unmount()); }
});
