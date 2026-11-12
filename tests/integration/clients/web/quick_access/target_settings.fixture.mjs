import { CommandsRegistry } from '../../../../../src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
import { parse } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/json.js';
import { IQuickInputService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickInput.js';
import { INotificationService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/notification/common/notification.js';
import { defaultAgentSourceCommand } from '../../../../../src/clients/workbench/src/settings/commands.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
import { invalidateGatewayToken } from '../../../../../src/clients/web/src/api/http.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

const target = { gateway_id: 'gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', workspace_id: '019a0000-0000-7000-8000-000000000001' };
const sourceKey = 'workspace_root_mutable_override';
const original = '// 顶部注释\r\n{\r\n  "ignored_paths": ["cache/"], // 保留此注释\r\n  "default_agent": "default",\r\n  "ui": {}\r\n}\r\n';
const snapshot = (state = 'active') => ({ healthy: true, revision: 'active-digest', state, active_revision: 4,
  pending_revision: state === 'active' ? undefined : 5, restart_required: state !== 'active', candidate_ref: state === 'active' ? undefined : 'original+/=candidate',
  applied_paths: [], deferred_paths: state === 'active' ? [] : ['/default_agent'], changed_sections: [], layer_digests: {}, mcp_cleanup: [] });

/** 运行原 Command、JSONC editor 与 API；只有 Quick Input、通知和 HTTP 回执为显式服务边界。 */
export async function targetSettingsContract(mode) {
  const before = workspaceContext.getSnapshot(), requests = [], choices = [], notifications = [], inputs = [];
  const a = { ...target, name: '原目标 A', status: mode === 'offline' ? 'offline' : 'ready' };
  const b = { ...target, gateway_id: 'gateway_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', workspace_id: '019a0000-0000-7000-8000-000000000002', name: '导航 B', status: 'ready' };
  const initial = { ...before, gatewayId: target.gateway_id,
    loading: false, error: null, workspaces: [a, b], activeId: 'B' };
  workspaceContext.publish(initial);
  const sourceText = mode === 'delete-first' ? '{\n  "default_agent": "default", // 第一项注释\n  "ignored_paths": ["cache/"] // 保留此注释\n}\n'
    : mode === 'delete-last' ? '{\n  "ignored_paths": ["cache/"], // 保留此注释\n  "default_agent": "default", // 最后一项注释\n}\n'
    : mode === 'delete-only' ? '{\n  // 唯一项之前注释\n  "default_agent": "default", // 唯一项之后注释\n}\n'
    : mode === 'insert' || mode === 'delete-absent' ? '{\n  "ignored_paths": ["cache/"], // 保留此注释\n}\n'
    : mode === 'insert-inline' ? '{"ignored_paths":["cache/"] /* 保留此注释 */}\n'
    : mode === 'duplicate' ? '{"default_agent":"default","default_agent":"coder"}\n' : original;
  const base = { ...target, source_key: sourceKey, layer: 'workspace', writable: mode !== 'readonly', jsonc: sourceText,
    file_presence: 'present', file_digest: 'file-old', layer_revision: 7, layer_digest: 'layer-old', source_generation: 9,
    active_revision: 4, active_digest: 'active-old', effective: { default_agent: 'default' }, source_effective: { default_agent: 'default' },
    layers: [{ source_key: sourceKey, layer: 'workspace', precedence: 4, writable: true, payload: parse(sourceText) }],
    origins: [{ path: '/default_agent', source_key: sourceKey, shadowed_sources: ['workspace_inline_defaults'] }], status: snapshot(), json_schema: {} };
  let document = structuredClone(base), savedIntent, menus = 0, recovery = 0, queries = 0, commandError;
  if (mode === 'retry' || mode === 'discard' || mode === 'discard-conflict') document.status = snapshot('pending_restart');
  const success = data => Response.json({ code: 0, request_id: 'source-request', data }, { headers: { 'X-Request-ID': 'source-request' } });
  const conflict = () => Response.json({ detail: '原 active CAS 已改变', request_id: 'original-conflict' }, { status: 409, headers: { 'X-Request-ID': 'original-conflict' } });
  const savedDocument = intent => ({ ...document, jsonc: intent.jsonc, file_digest: 'file-saved', layer_revision: 8, layer_digest: 'layer-saved',
    effective: { default_agent: mode === 'pending' ? 'default' : 'reviewer' }, source_effective: { default_agent: 'reviewer' },
    layers: [{ ...base.layers[0], payload: { default_agent: mode.startsWith('delete') ? undefined : 'reviewer' } }],
    status: snapshot(mode === 'pending' ? 'pending_restart' : 'active'),
    edit: { idempotency_key: intent.idempotency_key, candidate_id: 'original-candidate', state: mode === 'pending' ? 'pending_restart' : 'active', source_saved: true, layer_imported: true } });
  installSessionApiFetchMock(({ path, url, method, init }) => {
    const params = Object.fromEntries(new URL(url, location.origin).searchParams), headers = new Headers(init.headers);
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ path, method, params, body, owner: { gateway_id: headers.get('X-BoxTeam-Target-Gateway-Id'), workspace_id: headers.get('X-BoxTeam-Workspace-Id') } });
    if (path === '/api/v1/config/sources') return success({ sources: [{ source_key: sourceKey, layer: 'workspace', presence: 'present', precedence: 4, uri: 'boxteam-source://original/workspace.jsonc' }] });
    if (path.endsWith('/preflight')) {
      const errors = [], payload = parse(body.jsonc, errors);
      requests.at(-1).syntaxErrors = errors;
      requests.at(-1).payload = payload;
      return success({ document, candidate: { default_agent: payload.default_agent ?? 'default' }, candidate_digest: 'candidate-digest', changed_paths: ['/default_agent'] });
    }
    if (path === `/api/v1/config/sources/${sourceKey}` && method === 'PUT') {
      savedIntent = body;
      const count = requests.filter(item => item.method === 'PUT').length;
      if ((mode === 'conflict' || mode === 'conflict-cancel') && count === 1) {
        document = { ...base, jsonc: original.replace('// 顶部注释', '// 并发作者注释'), file_digest: 'file-latest', layer_revision: 8, layer_digest: 'layer-latest', active_revision: 5, active_digest: 'active-latest' };
        return conflict();
      }
      if (mode === 'unknown') throw new TypeError('具名 HTTP 保存回执中断');
      document = savedDocument(body);
      return success(document);
    }
    if (path === `/api/v1/config/sources/${sourceKey}` && method === 'GET') {
      if (params.idempotency_key) {
        queries++;
        if (mode === 'unknown' && queries >= 3) document = savedDocument(savedIntent);
      }
      return success(document);
    }
    if (path === '/api/v1/config/pending/retry' || path === '/api/v1/config/pending/discard') {
      if (mode === 'discard-conflict') return conflict();
      document = { ...document, status: snapshot(path.endsWith('/retry') ? 'pending_restart' : 'active') };
      return success(document.status);
    }
    throw new Error(`设置合同出现额外请求：${method} ${path}`);
  });
  const quickInput = {
    async pick(items, options, token) {
      choices.push({ title: options.title, prompt: options.prompt, placeHolder: options.placeHolder,
        labels: items.map(item => item.label), actions: items.map(item => item.action), requests: requests.length, puts: requests.filter(item => item.method === 'PUT').length });
      if (options.title === '选择业务配置目标') return mode === 'cancel-target' ? undefined : items[0];
      if (options.title === '选择默认 Agent 配置来源') return mode === 'cancel-source' ? undefined : items[0];
      if (options.title === '目标工作区默认 Agent 来源') {
        menus++;
        if (menus === 1) return items.find(item => item.action === (mode === 'readonly' ? 'inspect' : mode.startsWith('discard') ? 'discard' : mode === 'retry' ? 'retry' : mode.startsWith('delete') ? 'remove' : 'set'));
        if (mode === 'unknown' && menus === 2) return items.find(item => item.action === 'query');
        return undefined;
      }
      if (options.title === '确认默认 Agent 来源修改') return mode === 'cancel-confirm' ? items[0] : items.find(item => item.save);
      if (options.title === '确认原 pending 操作') return items.find(item => item.confirm);
      if (options.title === '来源修改未完成：保留本次输入') return items.find(item => item.action === (mode === 'conflict-cancel' ? 'back' : 'rebase'));
      if (options.title === '保存结果未确认：只查询原提交') {
        recovery++;
        return items.find(item => item.action === (recovery === 1 ? 'query' : 'back'));
      }
      if (options.title === '默认 Agent 来源与应用状态') return undefined;
      throw new Error(`设置合同出现额外 Quick Input：${options.title} · cancelled=${token.isCancellationRequested}`);
    },
    async input(options, token) {
      inputs.push({ ...options, validateInput: undefined });
      if (mode === 'focus') workspaceContext.publish({ ...initial, activeId: 'B' });
      if (mode === 'identity') invalidateGatewayToken(0);
      if (mode === 'gateway') workspaceContext.publish({ ...initial, gatewayId: 'gateway_changed' });
      inputs.at(-1).cancelled = token.isCancellationRequested;
      return mode === 'cancel-input' ? undefined : 'reviewer';
    },
  };
  const accessor = { get(service) {
    if (service === IQuickInputService) return quickInput;
    if (service === INotificationService) return { info: message => notifications.push({ kind: 'info', message }), error: message => notifications.push({ kind: 'error', message }) };
    throw new Error(`设置合同请求额外宿主服务：${service}`);
  } };
  try {
    const command = CommandsRegistry.getCommand(defaultAgentSourceCommand);
    if (!command) throw new Error('默认 Agent 来源命令未注册');
    await command.handler(accessor);
  } catch (cause) { commandError = String(cause); }
  finally { unwrapSessionApiFetch(); workspaceContext.publish(before); }
  return { target, sourceKey, original: sourceText, document, requests, choices, notifications, inputs, commandError };
}
