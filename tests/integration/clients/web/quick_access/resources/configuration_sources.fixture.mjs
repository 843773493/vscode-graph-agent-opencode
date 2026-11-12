import { Emitter, Event } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { ResourceOpener } from '../../../../../../src/clients/workbench/src/resourceOpener.js';
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

/** 原 Opener、配置来源 API 与命令分派；HTTP 和编辑器是明确装具边界。 */
export async function configurationUriContract(mode) {
  const previous = workspaceContext.getSnapshot(), gatewayId = `gateway_${'a'.repeat(32)}`;
  const remoteGatewayId = `gateway_${'b'.repeat(32)}`;
  const owner = { gatewayId, workspaceId: '019a0000-0000-7000-8000-000000000001' };
  const uri = 'boxteam://inline/distribution/resources/config/workspace_inline';
  const gatewayUri = 'boxteam://inline/distribution/resources/config/gateway_inline';
  const remoteFixture = {
    gatewayId: remoteGatewayId,
    sourceUri: 'boxteam://inline/source-development-0_0_2/resources/config/gateway_inline',
    referenceUri: `boxteam://${remoteGatewayId}/inline/source-development-0_0_2/resources/config/gateway_inline`,
  };
  const requests = [], commands = [], errors = [], references = [];
  let release, group = { id: 7 }, addedGroups = 0;
  workspaceContext.publish({ ...previous, gatewayId, loading: false, error: null, activeId: gatewayWorkspaceKey(owner),
    workspaces: [{ gateway_id: gatewayId, workspace_id: owner.workspaceId, name: '正式配置目标', status: 'ready' }] });
  const ok = data => Response.json({ code: 0, data, request_id: 'configuration-uri-contract' },
    { headers: { 'X-Request-ID': 'configuration-uri-contract' } });
  installSessionApiFetchMock(async ({ path, method, init }) => {
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    const headers = new Headers(init.headers), workspaceId = headers.get('X-BoxTeam-Workspace-Id');
    requests.push({ path, method, body, gatewayId: headers.get('X-BoxTeam-Target-Gateway-Id'), workspaceId });
    if (path === '/api/gateway/resource-addresses/parse') {
      if (mode === 'parse-identity') invalidateGatewayToken(0);
      if (mode === 'remote') return ok({ uri: body.resource_gateway_id ? remoteFixture.sourceUri : remoteFixture.referenceUri,
        reference_uri: remoteFixture.referenceUri, gateway_id: remoteGatewayId, scope: 'inline', scope_id: 'source-development-0_0_2', kind: 'config' });
      const parsedUri = mode === 'gateway' ? gatewayUri : uri;
      const parsedGatewayId = mode === 'wrong-owner' ? 'gateway-other' : gatewayId;
      return ok({ uri: parsedUri, reference_uri: `boxteam://${parsedGatewayId}/inline/distribution/resources/config/${mode === 'gateway' ? 'gateway_inline' : 'workspace_inline'}`,
      gateway_id: parsedGatewayId, scope: 'inline', scope_id: 'distribution', kind: 'config' });
    }
    if (path.endsWith('/config/sources')) {
      if (mode === 'freeze' && workspaceId) await new Promise(resolve => { release = resolve; });
      if (mode === 'identity' && workspaceId) invalidateGatewayToken(0);
      if (mode === 'denied' && workspaceId) return Response.json({ detail: '原配置来源权限已撤销' }, { status: 403 });
      return ok({ sources: [{ source_key: workspaceId ? 'workspace-formal-owner-key'
        : mode === 'remote' ? 'remote-gateway-formal-owner-key' : 'gateway-formal-owner-key',
      uri: mode === 'missing' ? undefined : mode === 'remote' ? remoteFixture.sourceUri
        : workspaceId || mode === 'ambiguous' ? uri : gatewayUri, layer: 'inline' }] });
    }
    throw new Error(`配置URI产生非预期请求 ${method} ${path}`);
  });
  const opener = new ResourceOpener({ registerOpener: () => ({ dispose() {} }) }, { activeEditor: undefined,
    openEditor: async () => { throw new Error('不得绕过原设置命令直接打开模型'); } },
    { activeGroup: group, getGroup: () => mode === 'closed-group' ? undefined : group,
      findGroup: () => undefined, addGroup: () => { addedGroups++; return { id: 8 }; } },
    { getValue: () => 'right' }, { createModelReference: async resource => { references.push(String(resource)); throw new Error('不得走普通来源正文读取'); } },
    { executeCommand: async (id, request) => { commands.push({ id, request }); } });
  try {
    const opened = opener.open(mode === 'gateway' ? gatewayUri : mode === 'remote' ? remoteFixture.referenceUri : uri, {
      ...(mode === 'remote' ? {} : { boxteamResourceOwner: owner }), boxteamTargetGroup: 7,
      openToSide: mode === 'side', editorOptions: { pinned: mode === 'pinned', preserveFocus: mode === 'background' } });
    if (mode === 'freeze') {
      for (let i = 0; i < 200 && !release; i++) await new Promise(resolve => setTimeout(resolve, 5));
      if (!release) throw new Error('配置目录没有进入受控等待');
      group = { id: 99 };
      workspaceContext.publish({ ...workspaceContext.getSnapshot(), activeId: 'later-active-workspace' });
      release();
    }
    try { await opened; } catch (error) { errors.push(String(error)); }
    return { owner, remoteFixture, requests, commands, errors, references, addedGroups };
  } finally { opener.dispose(); unwrapSessionApiFetch(); workspaceContext.publish(previous); }
}
