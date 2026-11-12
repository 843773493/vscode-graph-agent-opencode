import { Emitter, Event } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { CommandsRegistry } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
import { IQuickInputService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IDialogService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/dialogs/common/dialogs.js';
import { INotificationService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/notification/common/notification.js';
import { IEditorService } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorService.js';
import { AutomationQuickAccess } from '../../../../../../src/clients/workbench/src/automation/quickAccess.js';
import { taskActionCommand } from '../../../../../../src/clients/workbench/src/automation/actions.js';
import { ThreadInput } from '../../../../../../src/clients/workbench/src/threadInput.js';
import { workspaceContext } from '../../../../../../src/clients/workbench/src/workspaceContext.js';
import { gatewayWorkspaceKey } from '../../../../../../src/clients/web/src/api/workspaceIdentity.js';
import { invalidateGatewayToken } from '../../../../../../src/clients/web/src/api/http.js';
import { availableTaskActions } from '../../../../../../src/clients/web/src/hooks/persistentTasks/taskActions.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

const owners = ['gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'gateway_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb']
  .map(gatewayId => ({ gatewayId, workspaceId: '019a0000-0000-7000-8000-000000000001' }));
const record = index => ({
  task_id: 'same-task-id', workspace_id: owners[index].workspaceId, revision: 7, definition_revision: 2,
  application_type: 'automation', application_version: '1', lifecycle: 'active', phase: 'waiting_trigger', recovering: false,
  created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z',
  configuration: { name: 'Needle 自动化', trigger: { type_id: 'builtin.manual', version: '1', timezone: 'UTC' },
    condition: { type_id: 'builtin.always', version: '1' }, context: { type_id: 'builtin.literal', version: '1', content: '仅检查' },
    action: { type_id: 'builtin.runtime_source', version: '1',
      target_uri: `boxteam://workspace/${owners[index].workspaceId}/resources/session/session/threads/thread`, delivery_mode: 'context_only', delivery_policy: 'after_turn' },
    policies: { completion: 'continuous', misfire: 'skip', match_mode: 'each_match' } },
});

/** 运行真实 Provider、Command 与 API；仅宿主 Picker／Editor／Dialog 和 HTTP 响应为装具。 */
export async function automationContract(mode) {
  const before = workspaceContext.getSnapshot(), changes = new Emitter(), accepts = new Emitter(), buttons = new Emitter(), cancellation = new CancellationTokenSource();
  const requests = [], errors = [], choices = [], opened = [], notices = [], confirmations = [], shows = [];
  const targets = owners.map((owner, index) => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, name: `工作区 ${index}`, status: 'ready' }));
  const initial = { ...before, gatewayId: 'control-gateway', loading: false, error: null, workspaces: targets,
    activeId: mode === 'owner-cancel' ? null : gatewayWorkspaceKey(owners[0]) };
  workspaceContext.publish(initial);
  let records = owners.map((_, index) => record(index)), releaseList, releaseWrite, releaseChoice, releaseRead, aborted = false;
  if (mode === 'resume') records[0].lifecycle = 'paused';
  if (mode === 'unsupported-edit') records[0].configuration.trigger.version = '999';
  const success = data => Response.json({ code: 0, request_id: 'task-contract', data }, { headers: { 'X-Request-ID': 'task-contract' } });
  installSessionApiFetchMock(({ path, method, init }) => {
    const headers = new Headers(init?.headers), index = headers.get('X-BoxTeam-Target-Gateway-Id') === owners[1].gatewayId ? 1 : 0;
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ path, method, owner: headers.get('X-BoxTeam-Target-Gateway-Id'), body });
    if (path === '/api/gateway/workspaces') {
      const response = () => success({ items: targets, active_workspace: targets[0] });
      if (mode === 'freeze') return new Promise(resolve => { releaseList = () => resolve(response()); });
      return response();
    }
    if (path === '/api/v1/persistent-tasks' && method === 'GET') {
      if ((mode === 'pending-identity' || mode === 'pending-close') && index === 0) {
        init.signal.addEventListener('abort', () => { aborted = true; });
        return new Promise(resolve => { releaseRead = () => resolve(success({ items: [records[index]], runtime: { state: 'running', recovering_tasks: 0 } })); });
      }
      return success({ items: [records[index]], runtime: { state: 'running', recovering_tasks: 0 } });
    }
    if (path.endsWith('/control') || path.endsWith('/checks')) {
      if (mode === 'conflict') {
        records[index] = { ...records[index], revision: 8, lifecycle: 'paused' };
        return Response.json({ request_id: 'stale-task', detail: '持久任务 revision 已变化' },
          { status: 409, headers: { 'X-Request-ID': 'stale-task' } });
      }
      const response = () => {
        if (body.action) records[index] = { ...records[index], revision: body.expected_revision + 1,
          lifecycle: { pause: 'paused', resume: 'active', terminate: 'terminated' }[body.action] };
        return success(body.action ? records[index] : { task_id: records[index].task_id, run_id: 'trial-run', check_only: body.check_only });
      };
      if (mode === 'accepted-close' || mode === 'accepted-terminate-close') {
        init.signal.addEventListener('abort', () => { aborted = true; });
        return new Promise(resolve => { releaseWrite = () => resolve(response()); });
      }
      return response();
    }
    throw new Error(`非预期任务请求：${method} ${path}`);
  });
  const quick = { quickAccess: { show: (value, options) => shows.push({ value, options }) }, async pick(items, options) {
    choices.push({ items: items.map(item => ({ label: item.label, action: item.action })), title: options.title });
    if (mode === 'owner-cancel' || mode === 'action-cancel') return undefined;
    if (mode === 'identity') return new Promise(resolve => { releaseChoice = () => resolve(items.find(item => item.action === 'pause')); });
    return items[0];
  } };
  const origin = new ThreadInput({ ...owners[1], workspaceKey: gatewayWorkspaceKey(owners[1]), sessionId: 'session', threadId: 'thread', title: '发起Thread' });
  const editors = { activeEditor: mode === 'origin' ? origin : undefined, openEditor: async (input, options) => opened.push({ owner: input.owner, task: input.task, options }) };
  const notifications = { error: cause => errors.push(String(cause)), info: message => notices.push(message),
    status: message => { notices.push(message); return { close() { notices.push('pending-closed'); } }; } };
  const commands = { executeCommand(id, ...args) {
    let valid = true;
    const accessor = { get(service) {
      if (!valid) throw new Error('异步访问了 ServicesAccessor');
      if (service === IQuickInputService) return quick;
      if (service === IEditorService) return editors;
      if (service === INotificationService) return notifications;
      if (service === IDialogService) return { confirm: async options => { confirmations.push(options); return { confirmed: mode !== 'terminate-cancel' }; } };
      throw new Error(`额外服务：${service}`);
    } };
    try { return CommandsRegistry.getCommand(id).handler(accessor, ...args); }
    finally { valid = false; }
  } };
  const picker = { value: 'automation Needle', items: [], activeItems: [], selectedItems: [], buttons: [],
    keyMods: { alt: false, ctrlCmd: false }, onDidChangeValue: changes.event, onDidAccept: accepts.event, onDidTriggerButton: buttons.event,
    onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None, hide() { this.hidden = true; } };
  const provider = new AutomationQuickAccess(quick, commands, notifications);
  const lifetime = provider.provide(picker, cancellation.token, { searchOwners: owners });
  const until = async predicate => {
    for (let n = 0; n < 200; n++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error('任务合同未收敛');
  };
  let result;
  try {
    if (mode === 'pending-identity' || mode === 'pending-close') {
      await until(() => releaseRead);
      if (mode === 'pending-identity') invalidateGatewayToken(0);
      else { lifetime.dispose(); cancellation.cancel(); }
      releaseRead();
      await new Promise(resolve => setTimeout(resolve, 20));
      return { requests, aborted, rows: picker.items.map(item => ({ id: item.id, label: item.label })) };
    }
    if (mode === 'freeze') {
      // Provider 本身的目录查询也暂停；先释放它，再单独冻结命令的目标。
      await until(() => releaseList); releaseList();
    }
    await until(() => !picker.busy && picker.items.length === 2);
    const rows = picker.items.map(item => ({ id: item.id, label: item.label, detail: item.detail }));
    if (mode === 'unsupported-edit') return { requests, rows };
    releaseList = undefined;
    requests.length = 0;
    if (mode === 'browse') {
      picker.activeItems = [picker.items[1]]; cancellation.cancel();
    } else if (mode === 'identity' || mode === 'action-cancel') {
      picker.selectedItems = [picker.items[1]]; accepts.fire({ inBackground: false });
      if (mode === 'identity') { await until(() => releaseChoice); invalidateGatewayToken(0); releaseChoice(); }
      await until(() => errors.length || shows.length);
    } else {
      const action = ['pause', 'resume', 'check', 'checkOnly', 'details', 'history', 'edit'].includes(mode) ? mode
        : mode.startsWith('terminate') || mode === 'accepted-terminate-close' ? 'terminate' : 'pause';
      const supplied = mode === 'origin' || mode === 'owner-cancel' ? undefined : { owner: { ...owners[0] }, task: structuredClone(records[0]) };
      const operation = commands.executeCommand(taskActionCommand(action), supplied).catch(cause => { errors.push(String(cause)); return false; });
      if (mode === 'freeze') {
        await until(() => releaseList);
        supplied.owner.gatewayId = owners[1].gatewayId; supplied.task.revision = 99;
        workspaceContext.publish({ ...initial, activeId: gatewayWorkspaceKey(owners[1]) }); releaseList();
      }
      if (mode === 'accepted-close' || mode === 'accepted-terminate-close') {
        await until(() => releaseWrite); lifetime.dispose(); cancellation.cancel();
        workspaceContext.publish({ ...initial, activeId: gatewayWorkspaceKey(owners[1]) });
        releaseWrite();
      }
      result = await operation;
    }
    return { requests, errors, choices, opened, notices, confirmations, shows, rows, result, aborted,
      goalActions: availableTaskActions({ ...record(0), application_type: 'goal', configuration: null,
        goal: { session_id: 'session', objective: 'Goal' } }) };
  } finally {
    lifetime.dispose(); cancellation.dispose(); changes.dispose(); accepts.dispose(); buttons.dispose();
    unwrapSessionApiFetch(); workspaceContext.publish(before);
  }
}
