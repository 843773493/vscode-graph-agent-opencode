import { Emitter, Event } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { ResourceQuickAccess } from '../../../../../../src/clients/workbench/src/resources/quickAccess.js';
import { workspaceContext } from '../../../../../../src/clients/workbench/src/workspaceContext.js';
import { gatewayWorkspaceKey } from '../../../../../../src/clients/web/src/api/workspaceIdentity.js';
import { invalidateGatewayToken } from '../../../../../../src/clients/web/src/api/http.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

// 实际 Provider 与 HTTP 客户端；命令宿主边界只捕获冻结参数，真实编辑与保存另由页面验证。
export async function configurationSourcesContract(mode) {
  const previous = workspaceContext.getSnapshot(), changes = new Emitter(), accepts = new Emitter();
  const cancellation = new CancellationTokenSource(), requests = [], commands = [], errors = [];
  const gatewayId = `gateway_${'a'.repeat(32)}`;
  const targets = [1, 2].map(index => ({ gateway_id: gatewayId, workspace_id: `019a0000-0000-7000-8000-00000000000${index}`,
    name: `配置目标 ${index}`, status: 'ready' }));
  const owners = targets.map(target => ({ gatewayId, workspaceId: target.workspace_id }));
  workspaceContext.publish({ ...previous, gatewayId, workspaces: targets, activeId: gatewayWorkspaceKey(owners[0]), loading: false, error: null });
  const ok = data => Response.json({ code: 0, data, request_id: 'configuration-source-contract' }, { headers: { 'X-Request-ID': 'configuration-source-contract' } });
  let releaseRead, aborted = false, maximum = 0, active = 0;
  installSessionApiFetchMock(async ({ path, init, method }) => {
    const headers = new Headers(init.headers), workspaceId = headers.get('X-BoxTeam-Workspace-Id');
    requests.push({ path, method, gatewayId: headers.get('X-BoxTeam-Target-Gateway-Id'), workspaceId });
    active++; maximum = Math.max(maximum, active);
    try {
      if (path === '/api/gateway/workspaces') return ok({ items: targets });
      if (path === '/api/gateway/config/sources' || path === '/api/v1/config/sources') {
        if (mode === 'failure' && workspaceId === owners[0].workspaceId) return Response.json({ detail: '配置读取权限已撤销', request_id: 'source-denied' },
          { status: 403, headers: { 'X-Request-ID': 'source-denied' } });
        if (['close', 'identity'].includes(mode) && !releaseRead) {
          init.signal.addEventListener('abort', () => { aborted = true; });
          await new Promise(resolve => { releaseRead = resolve; });
        }
        // inline 有正式 URI；SQLite 逻辑层无文件 URI，也仍可经原配置 writer 查看。
        return ok({ sources: [{ source_key: 'inline:0', layer: 'inline', precedence: 0, presence: 'present',
          uri: 'boxteam://inline/probe/resources/config/workspace_inline' },
        { source_key: 'mutable', layer: 'user', precedence: 1, presence: 'present' },
        { layer: 'runtime_override', presence: 'present', precedence: 9 }] });
      }
      if (path === '/api/v1/resources/agent-spec') return ok({ node: { uri: 'AGENTS', resource_shape: 'file', operations: ['read'] } });
      if (path === '/api/v1/resources/gateway-skills') return ok({ gateway_id: gatewayId, workspace_id: workspaceId, available: true, entries: [] });
      if (path === '/api/v1/resources/skill-sources' || path === '/api/v1/resources/local-skills') return ok({ workspace_id: workspaceId, entries: [] });
      if (path === '/api/v1/resources') return ok({ workspace_id: workspaceId, items: [] });
      if (path === '/api/v1/mcp/servers') return Response.json({ code: 0, request_id: 'mcp-contract', protocol_version: 1,
        target: { kind: 'workspace', gateway_id: gatewayId, workspace_id: workspaceId }, catalog_revision: `sha256:${'0'.repeat(64)}`,
        catalog_generation: 1, data: [] }, { headers: { 'X-Request-ID': 'mcp-contract' } });
      throw new Error(`非预期来源请求 ${method} ${path}`);
    } finally { active--; }
  });
  const picker = { value: 'resource mutable', items: [], activeItems: [], selectedItems: [], buttons: [],
    keyMods: { alt: mode === 'side', ctrlCmd: mode === 'pinned' }, onDidChangeValue: changes.event, onDidAccept: accepts.event,
    onDidTriggerButton: Event.None, onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None, hide() {} };
  const groups = { activeGroup: { id: 7 } };
  const provider = new ResourceQuickAccess({}, {}, groups, { getValue: () => mode !== 'no-preview' },
    { error: cause => errors.push(String(cause)) }, { executeCommand: async (id, request) => commands.push({ id, request }) }, {});
  const lifetime = provider.provide(picker, cancellation.token, { searchOwners: owners });
  const until = async predicate => { for (let i = 0; i < 300; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error('配置来源合同未收敛'); };
  try {
    if (['close', 'identity'].includes(mode)) {
      await until(() => releaseRead);
      if (mode === 'close') lifetime.dispose(); else invalidateGatewayToken(0);
      releaseRead(); await new Promise(resolve => setTimeout(resolve, 20));
    } else {
      await until(() => !picker.busy && picker.items.length);
      if (mode === 'browse') {
        const count = requests.length;
        picker.value = 'resource inline'; changes.fire(picker.value); await until(() => !picker.busy);
        picker.value = 'resource mutable'; changes.fire(picker.value); await until(() => !picker.busy);
        if (requests.length !== count) throw new Error('逐键过滤重复读取来源');
      } else if (mode !== 'failure') {
        const pick = picker.items.find(item => item.detail?.includes(owners[1].workspaceId));
        picker.activeItems = picker.selectedItems = [pick];
        groups.activeGroup = { id: 99 };
        workspaceContext.publish({ ...workspaceContext.getSnapshot(), activeId: gatewayWorkspaceKey(owners[0]) });
        accepts.fire({ inBackground: mode === 'background' });
        await until(() => commands.length || errors.length);
      }
    }
    return { requests, commands, errors, aborted, maximum, owners,
      rows: picker.items.map(item => ({ id: item.id, label: item.label, detail: item.detail, selectable: !!item.accept })) };
  } finally {
    lifetime.dispose(); cancellation.dispose(true); provider.dispose(); changes.dispose(); accepts.dispose();
    unwrapSessionApiFetch(); workspaceContext.publish(previous);
  }
}
