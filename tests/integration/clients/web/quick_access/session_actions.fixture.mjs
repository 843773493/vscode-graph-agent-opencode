import '../../../../harness/js/workbench-locale.ts';
import { CommandsRegistry } from '../../../../../src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
import { IQuickInputService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IEditorService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IViewsService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/views/common/viewsService.js';
import { URI } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/uri.js';
import { sessionActionCommands } from '../../../../../src/clients/workbench/src/search/sessionActions.js';
import { ThreadInput } from '../../../../../src/clients/workbench/src/threadInput.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
import { gatewayWorkspaceKey } from '../../../../../src/clients/web/src/api/workspaceIdentity.js';
import { invalidateGatewayToken } from '../../../../../src/clients/web/src/api/http.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';
export { sessionPickerContract, threadPickerContract } from './federated_branches.fixture.mjs';

const owners = ['a', 'b'].map(letter => ({ gatewayId: `gateway_${letter.repeat(32)}`, workspaceId: '019a0000-0000-7000-8000-000000000001' }));
const sessionId = `ses_${'c'.repeat(32)}`;
const until = async predicate => {
  for (let attempt = 0; attempt < 200; attempt++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  throw new Error('会话命令未进入预期屏障');
};

/** 运行真实注册命令、目标解析及 HTTP 客户端；仅 Quick Input／导航 writer 和响应为具名装具。 */
export async function sessionActionContract(mode) {
  const previous = workspaceContext.getSnapshot(), requests = [], calls = [], picks = [], errors = [];
  const targets = owners.map(owner => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, name: '同名工作区', status: mode === 'offline' ? 'offline' : 'ready', connection_kind: 'local' }));
  workspaceContext.publish({ ...previous, gatewayId: owners[0].gatewayId, workspaces: targets, loading: false, error: null,
    activeId: mode === 'scope-cancel' ? null : gatewayWorkspaceKey(owners[0]) });
  let releaseRead, releaseView, releaseWrite;
  const success = data => Response.json({ code: 0, request_id: 'session-action', data }, { headers: { 'X-Request-ID': 'session-action' } });
  installSessionApiFetchMock(({ path, url, method, init }) => {
    const headers = new Headers(init.headers);
    requests.push({ path, method, query: new URL(url, location.origin).search,
      gateway: headers.get('X-BoxTeam-Target-Gateway-Id'), workspace: headers.get('X-BoxTeam-Workspace-Id') });
    if (path === '/api/gateway/workspaces') return success({ items: mode === 'removed' ? [targets[0]] : targets });
    if (path !== `/api/v1/sessions/${sessionId}`) throw new Error(`会话命令出现额外读写：${method} ${path}`);
    if (mode === 'deleted') return Response.json({ request_id: 'session-deleted', detail: '原会话已删除' }, { status: 404, headers: { 'X-Request-ID': 'session-deleted' } });
    const response = () => success({ session_id: mode === 'wrong-session' ? 'other' : sessionId,
      workspace_id: mode === 'wrong-workspace' ? 'other' : owners[0].workspaceId, title: '权威会话标题' });
    return mode.startsWith('late-') || mode === 'mutation' ? new Promise(resolve => { releaseRead = () => resolve(response()); }) : response();
  });
  const origin = new ThreadInput({ ...owners[1], workspaceKey: '旧展示路由', sessionId, threadId: `thr_${'d'.repeat(32)}`, title: 'child 标题' });
  const activeEditor = mode.startsWith('pick-') || mode === 'scope-cancel' ? undefined
    : mode === 'corrupt-origin' ? { resource: URI.from({ scheme: 'boxteam-file', authority: owners[0].gatewayId, path: `/${owners[0].workspaceId}/%/bad` }) } : origin;
  const quick = {
    async pick() { picks.push({ scope: true }); return undefined; },
    quickAccess: { async pick(value, options) {
      picks.push({ value, options });
      if (mode === 'pick-cancel') return undefined;
      return mode === 'pick-folder' ? [{ id: 'folder', label: '目录' }] : [{ sessionTarget: { owner: owners[0], sessionId } }];
    } },
  };
  const view = { async runSessionAction(action, id, title, workspaceKey, assertAccess, source) {
    if (mode === 'late-view') await new Promise(resolve => { releaseView = resolve; });
    assertAccess();
    calls.push({ action, sessionId: id, title, workspaceKey, ...(source ? { source } : {}) });
    if (mode === 'accepted') await new Promise(resolve => { releaseWrite = resolve; });
  } };
  const services = new Map([[IQuickInputService, quick], [IEditorService, { activeEditor }], [IViewsService, { openView: async () => view }]]);
  let synchronous = true;
  const accessor = { get(service) {
    if (!synchronous) throw new Error('异步阶段重新使用了 ServicesAccessor');
    if (!services.has(service)) throw new Error(`会话命令请求了未声明服务 ${service}`);
    return services.get(service);
  } };
  const explicit = { owner: { ...owners[0] }, sessionId, ...(mode === 'fork-explicit' ? { source: { threadId: 'explicit-child', itemId: 'explicit-item', sourceViewId: 'explicit-view', sourceViewRevision: 7 } } : {}) };
  let completed = false;
  try {
    const command = CommandsRegistry.getCommand(sessionActionCommands[mode.startsWith('fork') ? 'fork' : mode === 'rename' ? 'rename' : 'delete']);
    const pending = Promise.resolve(command.handler(accessor,
      ['explicit', 'mutation', 'rename', 'wrong-session', 'wrong-workspace', 'accepted', 'fork-explicit'].includes(mode) ? explicit : undefined))
      .catch(cause => errors.push(String(cause))).finally(() => { completed = true; });
    synchronous = false;
    if (mode.startsWith('late-') || mode === 'mutation') {
      await until(() => releaseRead);
      if (mode === 'late-token') invalidateGatewayToken(0);
      if (mode === 'late-gateway') {
        workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: owners[1].gatewayId });
        workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: owners[0].gatewayId });
      }
      if (mode === 'mutation') { explicit.owner.gatewayId = owners[1].gatewayId; explicit.sessionId = 'mutated'; }
      releaseRead();
      if (mode === 'late-view') { await until(() => releaseView); invalidateGatewayToken(0); releaseView(); }
    }
    let completedBeforeOwner;
    if (mode === 'accepted') {
      await until(() => releaseWrite); completedBeforeOwner = completed;
      invalidateGatewayToken(0); releaseWrite();
    }
    await pending;
    return { requests, calls, picks, errors, completedBeforeOwner };
  } finally { origin.dispose(); unwrapSessionApiFetch(); workspaceContext.publish(previous); }
}
