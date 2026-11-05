import { Emitter, Event } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { URI } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/uri.js';
import { CommandsRegistry } from '../../../../../src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
import { IQuickInputService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IEditorService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IViewsService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/views/common/viewsService.js';
import { WorkspaceControlQuickAccess } from '../../../../../src/clients/workbench/src/search/workspaceQuickAccess.js';
import { workspaceActionCommands } from '../../../../../src/clients/workbench/src/search/workspaceActions.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
import { ThreadInput } from '../../../../../src/clients/workbench/src/threadInput.js';
import { gatewayWorkspaceKey } from '../../../../../src/clients/web/src/api/workspaceIdentity.js';
import { invalidateGatewayUserSession } from '../../../../../src/clients/web/src/api/http.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

const owners = ['gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'gateway_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb']
  .map(gatewayId => ({ gatewayId, workspaceId: '019a0000-0000-7000-8000-000000000001' }));
const controlGateway = 'gateway_control';

/** 浏览器运行原 Provider／Command／HTTP 客户端；仅 Picker、视图 writer 边界和网络响应为装具。 */
export async function workspaceControlContract(mode) {
  const before = workspaceContext.getSnapshot(), changes = new Emitter(), accepts = new Emitter(), buttons = new Emitter(), cancellation = new CancellationTokenSource();
  const requests = [], listUrls = [], calls = [], shows = [], choices = [], errors = [];
  const targets = owners.map((owner, index) => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, name: 'Needle 工作区',
    status: index ? 'offline' : 'ready', connection_kind: index ? 'remote_gateway' : 'local', managed: true, affected_workspace_ids: [owner.workspaceId] }));
  const initial = { ...before, gatewayId: controlGateway, workspaces: targets,
    activeId: mode.startsWith('command-empty') ? null : gatewayWorkspaceKey(owners[0]),
    loading: mode === 'command-loading', error: mode === 'command-control-error' ? '原控制面读取失败' : null };
  const origin = new ThreadInput({ ...owners[1], workspaceKey: gatewayWorkspaceKey(owners[1]), sessionId: 'origin-session', threadId: 'origin-thread', title: '发起编辑器' });
  workspaceContext.publish(initial);
  const success = data => Response.json({ code: 0, request_id: 'control-request', data }, { headers: { 'X-Request-ID': 'control-request' } });
  const hit = index => ({ owner: { gateway_id: owners[index].gatewayId, workspace_id: owners[index].workspaceId }, name: targets[index].name,
    navigation_node_id: `node-${index}`, breadcrumb_names: ['Gateway 导航', targets[index].name], breadcrumb_node_ids: ['folder', `node-${index}`] });
  const page = (index, complete = true) => ({ gateway_id: controlGateway, query: 'Needle', items: [hit(index)], folders: [], complete,
    ...(complete ? {} : { cursor: 'original-control-cursor' }) });
  let releaseDiscovery, releaseList, releaseChoice, aborted = false;
  installSessionApiFetchMock(({ path, url, method, init }) => {
    const params = Object.fromEntries(new URL(url, location.origin).searchParams);
    requests.push({ path, params, method });
    if (path === '/api/gateway/search/workspaces') {
      init.signal.addEventListener('abort', () => { aborted = true; }, { once: true });
      if (mode === 'late' && params.query === 'slow') return new Promise(resolve => { releaseDiscovery = () => resolve(success(page(0))); });
      if (mode === 'failure' && requests.filter(item => item.path === path).length === 1) return Response.json({ request_id: 'discovery-rejected', detail: '原控制面读取拒绝' },
        { status: 409, headers: { 'X-Request-ID': 'discovery-rejected' } });
      if (mode === 'late' || mode === 'action-start' || mode === 'action-reconnect' || params.cursor) return success(page(1));
      return success(page(0, mode !== 'browse'));
    }
    if (path === '/api/gateway/workspaces') {
      const requestUrl = new URL(url, location.origin);
      listUrls.push(requestUrl.pathname + requestUrl.search);
      const result = () => success({ items: mode === 'removed' ? [targets[0]] : targets, active_workspace: { gateway_id: owners[1].gatewayId, workspace_id: owners[1].workspaceId } });
      if (mode === 'command-mutation' || mode === 'command-origin') return new Promise(resolve => { releaseList = () => resolve(result()); });
      return result();
    }
    throw new Error(`工作区合同出现非控制面读取：${method} ${path}`);
  });
  const quickInput = {
    quickAccess: { show(value, options) { shows.push({ value, options }); } },
    async pick(items, options) {
      choices.push({ items: items.map(item => ({ label: item.label, action: item.action, owner: item.owner })), options });
      if (mode === 'scope-cancel' || mode === 'action-cancel') return undefined;
      if (mode === 'scope-confirm') return [items[1]];
      if (mode.startsWith('command-empty')) return mode.endsWith('cancel') ? undefined : items[1];
      const selected = items.find(item => item.action === mode.replace('action-', ''));
      if (mode === 'action-identity' || mode === 'action-gateway') return new Promise(resolve => { releaseChoice = () => resolve(items.find(item => item.action === 'activate')); });
      return selected;
    },
  };
  const view = { async runWorkspaceAction(action, workspaceKey, assertAccess) { assertAccess(); calls.push({ action, workspaceKey }); } };
  let activeEditor = mode === 'command-origin' || mode === 'command-priority' ? origin : undefined;
  if (mode.startsWith('command-corrupt-')) {
    const kind = mode.slice('command-corrupt-'.length);
    activeEditor = { resource: URI.from({ scheme: `boxteam-${kind}`, authority: owners[1].gatewayId,
      path: `/${owners[1].workspaceId}/${kind === 'file' ? '%' : kind === 'source' ? '%/source.txt' : 'session'}` }) };
  } else if (mode === 'command-empty-editor') activeEditor = { resource: URI.from({ scheme: 'untitled', path: '/空文档' }) };
  const accessor = { get(service) {
    if (service === IQuickInputService) return quickInput;
    if (service === IEditorService) return { activeEditor };
    if (service === IViewsService) return { openView: async () => view };
    throw new Error(`工作区命令请求了额外宿主服务：${service}`);
  } };
  const commands = { executeCommand(id, ...args) {
    const command = CommandsRegistry.getCommand(id);
    if (!command) throw new Error(`工作区命令未注册：${id}`);
    let valid = true;
    const invocation = { get(service) {
      if (!valid) throw new Error('服务访问器只在同步命令入口有效');
      return accessor.get(service);
    } };
    try { return command.handler(invocation, ...args); }
    finally { valid = false; }
  } };
  const picker = { value: mode === 'late' ? 'workspace slow' : 'workspace Needle', items: [], activeItems: [], selectedItems: [], buttons: [],
    keyMods: { alt: false, ctrlCmd: false }, onDidChangeValue: changes.event, onDidAccept: accepts.event, onDidTriggerButton: buttons.event,
    onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None, hide() { this.hidden = true; } };
  const provider = new WorkspaceControlQuickAccess(quickInput, commands, { getValue: () => true }, { error: error => errors.push(String(error)) });
  const lifetime = provider.provide(picker, cancellation.token, { searchOwners: [owners[0]] });
  const until = async predicate => {
    for (let count = 0; count < 200; count++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error('工作区合同条件未收敛');
  };
  let retained, activeRetained;
  try {
    if (mode === 'late') {
      await until(() => releaseDiscovery);
      picker.value = 'workspace fast'; changes.fire();
      await until(() => !picker.busy && picker.items[0]?.id === gatewayWorkspaceKey(owners[1]));
      const current = picker.items[0]; releaseDiscovery();
      await new Promise(resolve => setTimeout(resolve, 20)); retained = picker.items[0] === current;
    } else {
      await until(() => !picker.busy && picker.items.length);
      if (mode === 'browse') {
        const original = picker.items[0]; picker.activeItems = [original];
        await picker.items.find(item => item.id === 'workspace-discovery-more').trigger();
        retained = picker.items[0] === original; activeRetained = picker.activeItems[0] === original;
      } else if (mode === 'failure') {
        const failed = { label: picker.items[0].label, detail: picker.items[0].detail };
        await picker.items[0].trigger();
        choices.push({ failed });
      } else if (mode.startsWith('scope-')) {
        picker.activeItems = [picker.items[0]];
        buttons.fire(picker.buttons[0]); await until(() => shows.length || errors.length);
      } else if (mode.startsWith('action-')) {
        picker.selectedItems = [picker.items[0]]; accepts.fire({ inBackground: false });
        if (mode === 'action-identity' || mode === 'action-gateway') {
          await until(() => releaseChoice);
          if (mode === 'action-identity') invalidateGatewayUserSession(0);
          else workspaceContext.publish({ ...initial, gatewayId: 'changed-control-gateway' });
          releaseChoice();
        }
        await until(() => calls.length || shows.length || errors.length);
      } else if (mode.startsWith('command-') || mode === 'removed') {
        const owner = { ...owners[1] };
        const explicit = mode === 'command-priority' ? owners[0] : mode === 'command-mutation' || mode === 'removed' ? owner : undefined;
        const action = mode === 'command-current' || mode === 'command-priority' ? 'activate' : 'start';
        const operation = commands.executeCommand(workspaceActionCommands[action], explicit);
        if (mode === 'command-mutation' || mode === 'command-origin') {
          await until(() => releaseList); Object.assign(owner, owners[0]); Object.assign(origin.owner, owners[0]);
          workspaceContext.publish({ ...initial, activeId: gatewayWorkspaceKey(owners[0]) }); releaseList();
        }
        await operation.catch(error => errors.push(String(error)));
      }
    }
    return { requests, listUrls, calls, shows, choices, errors, retained, activeRetained, aborted, busy: picker.busy,
      items: picker.items.map(item => ({ id: item.id, label: item.label, description: item.description, detail: item.detail })),
      hidden: picker.hidden ?? false, preserveInput: provider.defaultFilterValue };
  } finally {
    lifetime.dispose(); cancellation.dispose(true); provider.dispose(); changes.dispose(); accepts.dispose(); buttons.dispose();
    origin.dispose(); unwrapSessionApiFetch(); workspaceContext.publish(before);
  }
}
