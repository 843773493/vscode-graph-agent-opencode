import '../../../../harness/js/workbench-locale.ts';
import { CommandsRegistry, ICommandService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
import { INotificationService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/notification/common/notification.js';
import { IQuickInputService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickInput.js';
import { defaultAgentSourceCommand } from '../../../../../src/clients/workbench/src/settings/commands.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

const ownerA = { gateway_id: 'gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', workspace_id: '019a0000-0000-7000-8000-000000000001' };
const ownerB = { gateway_id: ownerA.gateway_id, workspace_id: '019a0000-0000-7000-8000-000000000002' };
const sourceKey = 'workspace_root_mutable_override';
const original = '// 顶部注释\r\n{\r\n  "ignored_paths": ["cache/"], // 保留此注释\r\n  "default_agent": "default",\r\n  "ui": {}\r\n}\r\n';
const pending = { healthy: true, revision: 'active-digest', state: 'pending_restart', active_revision: 4,
  pending_revision: 5, restart_required: true, candidate_ref: 'original+/=candidate', applied_paths: [],
  deferred_paths: ['/default_agent'], changed_sections: [], layer_digests: {}, mcp_cleanup: [] };

/** 运行真实默认 Agent 来源命令和来源 API；命令交接 Agent 表单由真实页面验收。 */
export async function targetSettingsContract(mode) {
  const before = workspaceContext.getSnapshot(), requests = [], choices = [], notifications = [];
  const a = { ...ownerA, name: '原目标 A', status: mode === 'offline' ? 'offline' : 'ready' };
  const b = { ...ownerB, name: '导航 B', status: 'ready' };
  const initial = { ...before, gatewayId: ownerA.gateway_id, loading: false, error: null,
    workspaces: [a, b], activeId: ownerB.workspace_id };
  workspaceContext.publish(initial);
  let document = { gateway_id: ownerA.gateway_id, workspace_id: ownerA.workspace_id, source_key: sourceKey,
    layer: 'workspace', writable: true, jsonc: original, file_presence: 'present', file_digest: 'file-old',
    layer_revision: 7, layer_digest: 'layer-old', source_generation: 9, active_revision: 4, active_digest: 'active-old',
    effective: { default_agent: 'default' }, source_effective: { default_agent: 'default' },
    layers: [{ source_key: sourceKey, layer: 'workspace', precedence: 4, writable: true, payload: { default_agent: 'default' } }],
    origins: [{ path: '/default_agent', source_key: sourceKey, shadowed_sources: ['workspace_inline_defaults'] }], status: pending,
    edit: { idempotency_key: 'original-edit', candidate_id: 'original-candidate', candidate_ref: pending.candidate_ref,
      state: 'candidate_validated', source_saved: true, layer_imported: true }, json_schema: {} };
  let menuCount = 0;
  installSessionApiFetchMock(({ path, url, method, init }) => {
    const params = Object.fromEntries(new URL(url, location.origin).searchParams), headers = new Headers(init.headers);
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ path, method, params, body, owner: { gateway_id: headers.get('X-BoxTeam-Target-Gateway-Id'),
      workspace_id: headers.get('X-BoxTeam-Workspace-Id') } });
    const response = data => Response.json({ code: 0, request_id: 'source-request', data },
      { headers: { 'X-Request-ID': 'source-request' } });
    if (path === '/api/v1/config/sources') return response({ sources: [{ source_key: sourceKey, layer: 'workspace', presence: 'present', precedence: 4 }] });
    if (path === `/api/v1/config/sources/${sourceKey}`) return response(document);
    if (path === '/api/v1/config/pending/retry') return response(document.status);
    if (path === '/api/v1/config/pending/discard') {
      if (mode === 'discard-conflict') return Response.json({ detail: 'active CAS 已改变', request_id: 'pending-conflict' },
        { status: 409, headers: { 'X-Request-ID': 'pending-conflict' } });
      document = { ...document, status: { ...document.status, state: 'discarded', pending_revision: undefined,
        restart_required: false, candidate_ref: undefined } };
      return response(document.status);
    }
    throw new Error(`默认 Agent 命令出现额外请求：${method} ${path}`);
  });
  const quick = {
    async pick(items, options, token) {
      choices.push({ title: options.title, prompt: options.prompt, placeholder: options.placeHolder,
        labels: items.map(item => item.label), details: items.map(item => item.detail), actions: items.map(item => item.action), requests: requests.length });
      if (options.title === '选择业务配置目标') return mode === 'cancel-target' ? undefined : items.find(item => item.owner.workspaceId === ownerA.workspace_id);
      if (options.title === '选择默认 Agent 配置来源') return mode === 'cancel-source' ? undefined : items.find(item => item.source_key === sourceKey);
      if (options.title === '默认 Agent 来源与应用状态') {
        if (items.every(item => item.action === undefined)) return undefined;
        menuCount++;
        if (menuCount > 1) return undefined;
        if (mode === 'inspect-pending') return items.find(item => item.action === 'inspect');
        if (mode === 'retry-pending') return items.find(item => item.action === 'retry');
        if (mode === 'discard-pending' || mode === 'discard-conflict') return items.find(item => item.action === 'discard');
        return undefined;
      }
      if (options.title === '确认原 pending 操作') return items.find(item => item.confirm);
      throw new Error(`默认 Agent 命令出现额外 Quick Input：${options.title} · cancelled=${token.isCancellationRequested}`);
    },
  };
  const commandCalls = [];
  let accessorActive = true, accessorEntered = false;
  const accessor = { get(service) {
    if (!accessorActive) throw new Error('目标方法返回后不能继续使用 ServicesAccessor');
    if (!accessorEntered) {
      accessorEntered = true;
      queueMicrotask(() => { accessorActive = false; });
    }
    if (service === IQuickInputService) return quick;
    if (service === INotificationService) return { info: message => notifications.push({ kind: 'info', message }),
      error: message => notifications.push({ kind: 'error', message }) };
    if (service === ICommandService) return { executeCommand: (id, ...args) => { commandCalls.push({ id, args }); } };
    throw new Error(`默认 Agent 命令请求额外宿主服务：${service}`);
  } };
  let commandError;
  try {
    const command = CommandsRegistry.getCommand(defaultAgentSourceCommand);
    if (!command) throw new Error('默认 Agent 来源命令未注册');
    const explicit = mode.includes('pending') || mode === 'discard-conflict'
      ? { version: 2, ...ownerA, source_key: sourceKey, name: '原目标 A' } : undefined;
    await command.handler(accessor, explicit);
  } catch (error) { commandError = String(error); }
  finally { unwrapSessionApiFetch(); workspaceContext.publish(before); }
  return { ownerA, ownerB, sourceKey, document, requests, choices, notifications, commandCalls, commandError };
}
