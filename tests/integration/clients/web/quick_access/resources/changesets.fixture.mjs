import { Emitter, Event } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { ResourceQuickAccess } from '../../../../../../src/clients/workbench/src/resources/quickAccess.js';
import { ThreadInput } from '../../../../../../src/clients/workbench/src/threadInput.js';
import { workspaceContext } from '../../../../../../src/clients/workbench/src/workspaceContext.js';
import { gatewayWorkspaceKey } from '../../../../../../src/clients/web/src/api/workspaceIdentity.js';
import { invalidateGatewayToken } from '../../../../../../src/clients/web/src/api/http.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

// 运行真实 Provider 和 HTTP 客户端；EditorService 只记录目标，完整面板／Diff另经正式页面验收。
export async function changesetContract(mode, kind = 'changeset') {
  const previous = workspaceContext.getSnapshot(), changes = new Emitter(), accepts = new Emitter(), itemButtons = new Emitter();
  const cancellation = new CancellationTokenSource(), requests = [], opened = [], errors = [];
  const attachment = { file_id: 'boxteam://workspace/019a0000-0000-7000-8000-000000000001/resources/attachments/att_' + 'a'.repeat(32) + '/original/content',
    directory_uri: 'boxteam://workspace/019a0000-0000-7000-8000-000000000001/resources/attachments/att_' + 'a'.repeat(32), name: '原图片.png', content_type: 'image/png', availability: 'available' };
  const owners = ['a', 'b'].map(letter => ({ gatewayId: `gateway_${letter.repeat(32)}`, workspaceId: '019a0000-0000-7000-8000-000000000001' }));
  const targets = owners.map((owner, index) => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, name: `变更目标 ${index}`, status: 'ready' }));
  workspaceContext.publish({ ...previous, gatewayId: owners[0].gatewayId, workspaces: targets, activeId: gatewayWorkspaceKey(owners[0]), loading: false, error: null });
  const ok = data => Response.json({ code: 0, data, request_id: 'changeset-contract' }, { headers: { 'X-Request-ID': 'changeset-contract' } });
  const origin = new ThreadInput({ ...owners[1], sessionId: 'ses_origin', threadId: 'thr_child', workspaceKey: '', title: '原child' });
  const editors = { activeEditor: mode === 'no-thread' ? undefined : origin,
    async openEditor(input, options, group) { opened.push({ owner: { ...input.owner }, options, group }); } };
  let releaseRead, aborted = false, groupsAdded = 0;
  installSessionApiFetchMock(async ({ path: pathname, url, init, method }) => {
    const query = new URL(url, 'http://127.0.0.1').search;
    const path = pathname.endsWith('/attachments') ? pathname + query : pathname;
    const headers = new Headers(init.headers), gatewayId = headers.get('X-BoxTeam-Target-Gateway-Id'), workspaceId = headers.get('X-BoxTeam-Workspace-Id');
    requests.push({ path, method, gatewayId, workspaceId });
    if (path === '/api/gateway/workspaces') return ok({ items: targets });
    if (path.endsWith('/changesets') || path.includes('/attachments?')) {
      const selected = kind === 'attachment' ? path.includes('/attachments?') : path.endsWith('/changesets');
      if (!selected) return ok(path.endsWith('/changesets') ? { session_id: 'ses_origin', items: [] } : { session_id: 'ses_origin', thread_id: 'thr_child', items: [] });
      if (['close', 'identity', 'freeze'].includes(mode) || mode.startsWith('page-') && query.includes('after=att_')) {
        init.signal.addEventListener('abort', () => { aborted = true; });
        await new Promise(resolve => { releaseRead = resolve; });
      }
      if (mode === 'denied') return Response.json({ detail: '原变更读取权限已撤销', request_id: 'changeset-denied' },
        { status: 403, headers: { 'X-Request-ID': 'changeset-denied' } });
      if (kind === 'attachment') return ok({ session_id: 'ses_origin', thread_id: mode === 'wrong-owner' ? 'thr_wrong' : 'thr_child',
        items: [{ ...attachment, ...(query.includes('after=att_') ? { file_id: attachment.file_id.replace('a'.repeat(32), 'b'.repeat(32)), name: '原图片2.png' } : {}),
          availability: mode === 'unavailable' ? 'unavailable' : 'available' }],
        next_after: mode.startsWith('page') && !new URLSearchParams(path.split('?')[1]).get('after') ? 'att_' + 'a'.repeat(32) : undefined });
      return ok({ session_id: mode === 'wrong-owner' ? 'ses_wrong' : 'ses_origin',
        items: [{ changeset_id: 'turn:chosen', label: '选定任务的更改', change_kind: 'turn', summary: { files: 2 } }] });
    }
    if (path.endsWith('/config/sources')) return ok({ sources: [] });
    if (path.endsWith('/agent-spec')) return ok({ node: { uri: 'AGENTS', resource_shape: 'file', operations: ['read'] } });
    if (path.endsWith('/gateway-skills')) return ok({ gateway_id: gatewayId, workspace_id: workspaceId, available: true, entries: [] });
    if (path.endsWith('/skill-sources') || path.endsWith('/local-skills')) return ok({ workspace_id: workspaceId, entries: [] });
    if (path === '/api/v1/resources') return ok({ workspace_id: workspaceId, items: [] });
    if (path === '/api/v1/mcp/servers') return Response.json({ code: 0, request_id: 'mcp-contract', protocol_version: 1,
      target: { kind: 'workspace', gateway_id: gatewayId, workspace_id: workspaceId }, catalog_revision: `sha256:${'0'.repeat(64)}`,
      catalog_generation: 1, data: [] }, { headers: { 'X-Request-ID': 'mcp-contract' } });
    throw new Error(`非预期变更请求 ${method} ${path}`);
  });
  const picker = { value: kind === 'attachment' ? 'resource 原图片' : 'resource turn:chosen', items: [], activeItems: [], selectedItems: [], buttons: [],
    keyMods: { alt: mode === 'side', ctrlCmd: mode === 'pinned' }, onDidChangeValue: changes.event, onDidAccept: accepts.event,
    onDidTriggerButton: Event.None, onDidTriggerItemButton: itemButtons.event, onDidTriggerSeparatorButton: Event.None, hide() {} };
  const groups = { activeGroup: { id: 7 }, getGroup: id => mode === 'closed-group' ? undefined : { id },
    findGroup: () => undefined, addGroup: () => { groupsAdded++; return { id: 8 }; } };
  const provider = new ResourceQuickAccess({}, {}, groups, { getValue: key => key === 'workbench.editor.openSideBySideDirection' ? 'right' : mode !== 'no-preview' },
    { error: cause => errors.push(String(cause)) }, {}, editors);
  const lifetime = provider.provide(picker, cancellation.token, { searchOwners: mode === 'excluded' ? [owners[0]] : owners });
  const until = async predicate => { for (let i = 0; i < 300; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error('变更合同未收敛'); };
  try {
    if (['close', 'identity', 'freeze'].includes(mode)) {
      await until(() => releaseRead);
      if (mode === 'close') lifetime.dispose();
      else if (mode === 'identity') invalidateGatewayToken(0);
      else { editors.activeEditor = new ThreadInput({ ...origin.owner, sessionId: 'ses_later', threadId: 'thr_later' }); groups.activeGroup = { id: 99 }; }
      releaseRead();
      if (mode !== 'freeze') await new Promise(resolve => setTimeout(resolve, 20));
    }
    if (!['close', 'identity'].includes(mode)) {
      await until(() => !picker.busy && picker.items.length);
      if (mode.startsWith('page') && kind === 'attachment') {
        const more = picker.items.find(item => item.label === '更多附件…');
        itemButtons.fire({ item: more, button: more.buttons[0] });
        itemButtons.fire({ item: more, button: more.buttons[0] });
        if (mode === 'page') await until(() => picker.items.some(item => item.label === '原图片2.png') && !picker.busy);
        else {
          await until(() => releaseRead);
          if (mode === 'page-close') lifetime.dispose();
          else invalidateGatewayToken(0);
          releaseRead(); await new Promise(resolve => setTimeout(resolve, 20));
        }
      } else if (mode === 'browse') {
        const count = requests.length;
        picker.value = kind === 'attachment' ? 'resource 图片' : 'resource 更改'; changes.fire(picker.value); await until(() => !picker.busy);
        if (requests.length !== count) throw new Error('筛选重复发现变更');
      } else if (!['no-thread', 'excluded', 'denied', 'wrong-owner', 'unavailable'].includes(mode)) {
        const pick = picker.items.find(item => item.resourceId === (kind === 'attachment' ? attachment.file_id : 'turn:chosen'));
        picker.activeItems = picker.selectedItems = [pick];
        try { accepts.fire({ inBackground: mode === 'background' }); } catch (cause) { errors.push(String(cause)); }
        await until(() => opened.length || errors.length);
      }
    }
    return { requests, opened, errors, groupsAdded, aborted, owner: origin.owner,
      attachment, rows: picker.items.map(item => ({ id: item.id, label: item.label, detail: item.detail, selectable: !!item.accept })) };
  } finally {
    lifetime.dispose(); cancellation.dispose(true); provider.dispose(); changes.dispose(); accepts.dispose(); itemButtons.dispose();
    unwrapSessionApiFetch(); workspaceContext.publish(previous);
  }
}
