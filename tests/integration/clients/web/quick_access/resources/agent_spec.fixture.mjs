import { Emitter, Event } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { CommandsRegistry } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
import { IClipboardService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/clipboard/common/clipboardService.js';
import { ResourceQuickAccess } from '../../../../../../src/clients/workbench/src/resources/quickAccess.js';
import { ResourceOpener } from '../../../../../../src/clients/workbench/src/resourceOpener.js';
import { sourceDocumentSource } from '../../../../../../src/clients/workbench/src/sourceDocuments.js';
import { ThreadInput } from '../../../../../../src/clients/workbench/src/threadInput.js';
import { workspaceFileSource } from '../../../../../../src/clients/workbench/src/workspaceFiles.js';
import { workspaceContext } from '../../../../../../src/clients/workbench/src/workspaceContext.js';
import { gatewayWorkspaceKey } from '../../../../../../src/clients/web/src/api/workspaceIdentity.js';
import { invalidateGatewayToken } from '../../../../../../src/clients/web/src/api/http.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

const owners = ['a', 'b'].map(letter => ({ gatewayId: `gateway_${letter.repeat(32)}`, workspaceId: '019a0000-0000-7000-8000-000000000001' }));
// 装具回执，不代替后端 grammar；两 Gateway 故意返回相同本机简称。
const uri = `boxteam://workspace/${owners[0].workspaceId}/resources/agent-spec/root/AGENTS.md`;

export async function agentSpecContract(mode) {
  const previous = workspaceContext.getSnapshot(), changes = new Emitter(), accepts = new Emitter(), buttons = new Emitter(), itemButtons = new Emitter(), cancellation = new CancellationTokenSource();
  const requests = [], opened = [], references = [], errors = [], shows = [], choices = [], copied = [];
  let releaseRead, releaseParse, aborted = false, released = 0, addedGroups = 0;
  const targets = owners.map((owner, index) => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId,
    name: `来源 ${index}`, status: mode === 'stopped' && index === 1 ? 'stopped' : 'ready' }));
  workspaceContext.publish({ ...previous, gatewayId: owners[0].gatewayId, loading: false, error: null,
    workspaces: targets, activeId: gatewayWorkspaceKey(owners[0]) });
  const ok = data => Response.json({ code: 0, request_id: 'agent-spec-contract', data }, { headers: { 'X-Request-ID': 'agent-spec-contract' } });
  installSessionApiFetchMock(({ path, method, init }) => {
    const headers = new Headers(init?.headers), body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ path, method, body, gateway: headers.get('X-BoxTeam-Target-Gateway-Id') });
    if (path === '/api/gateway/workspaces') return ok({ items: targets, active_workspace: targets[0] });
    if (path === '/api/gateway/config/sources' || path === '/api/v1/config/sources') return ok({ sources: [] });
    if (path === '/api/v1/resources/skill-sources') return ok({ workspace_id: owners[0].workspaceId, entries: [] });
    if (path === '/api/v1/resources/gateway-skills') return ok({ gateway_id: headers.get('X-BoxTeam-Target-Gateway-Id'), workspace_id: owners[0].workspaceId, revision: 'fixed', available: true, entries: [] });
    if (path === '/api/v1/resources/local-skills') return ok({ workspace_id: owners[0].workspaceId, entries: [] });
    if (path === '/api/v1/resources') return ok({ workspace_id: owners[0].workspaceId, items: [] });
    if (path === '/api/v1/mcp/servers') return Response.json({ code: 0, request_id: 'agent-spec-contract', data: [],
      protocol_version: 1, target: { kind: 'workspace', gateway_id: headers.get('X-BoxTeam-Target-Gateway-Id'), workspace_id: owners[0].workspaceId },
      catalog_revision: `sha256:${'0'.repeat(64)}`, catalog_generation: 1 }, { headers: { 'X-Request-ID': 'agent-spec-contract' } });
    if (path === '/api/v1/resources/agent-spec') {
      const result = () => ok({ node: { uri, resource_shape: mode === 'unsupported' ? 'domain_object' : 'file', operations: ['describe', 'read'] },
        entries: [], matches: [], contexts: [], truncated: false, view: 'live' });
      if ((mode === 'close' || mode === 'identity') && !releaseRead) {
        init.signal.addEventListener('abort', () => { aborted = true; });
        return new Promise(resolve => { releaseRead = () => resolve(result()); });
      }
      return result();
    }
    if (path === '/api/gateway/resource-addresses/parse') {
      if (mode === 'open-parse-identity') invalidateGatewayToken(0);
      const owner = owners.find(item => item.gatewayId === body.resource_gateway_id) ?? owners[0];
      const result = () => ok({ gateway_id: ['mismatch', 'copy-mismatch'].includes(mode) ? owners[0].gatewayId : owner.gatewayId,
        scope: 'workspace', scope_id: owner.workspaceId, kind: 'agent-spec', uri,
        reference_uri: `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/agent-spec/root/AGENTS.md` });
      if (['freeze', 'copy-identity', 'copy-gateway'].includes(mode)) return new Promise(resolve => { releaseParse = () => resolve(result()); });
      return result();
    }
    throw new Error(`非预期来源请求: ${method} ${path}`);
  });
  const quick = { quickAccess: { show: (value, options) => shows.push({ value, options }) },
    async pick(items) { choices.push(items.map(item => item.label)); return undefined; } };
  const group = { id: 7 }, groups = { activeGroup: group,
    getGroup: id => mode === 'closed-group' ? undefined : { id },
    findGroup: () => undefined, addGroup() { addedGroups += 1; return { id: 8 }; } };
  const configuration = { getValue: key => key === 'workbench.editor.openSideBySideDirection' ? 'right'
    : key === 'workbench.editor.enablePreview' && mode === 'no-preview' ? false : true };
  const models = { async createModelReference(resource) { references.push(sourceDocumentSource(resource));
    if (mode === 'open-model-identity') invalidateGatewayToken(0);
    if (mode === 'open-model-gateway') workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: owners[1].gatewayId });
    return { object: {}, dispose() { released += 1; } }; } };
  const opener = new ResourceOpener({ registerOpener: () => ({ dispose() {} }) },
    { openEditor: async (input, id) => opened.push({ source: sourceDocumentSource(input.resource), options: input.options, group: id }) },
    groups, configuration, models, {});
  const notifications = { error: cause => errors.push(String(cause)) };
  const picker = { value: 'resource ag', items: [], activeItems: [], selectedItems: [], buttons: [],
    keyMods: { alt: mode === 'side' || mode.startsWith('open-'), ctrlCmd: mode === 'pinned' }, onDidChangeValue: changes.event,
    onDidAccept: accepts.event, onDidTriggerButton: buttons.event, onDidTriggerItemButton: itemButtons.event,
    onDidTriggerSeparatorButton: Event.None, hide() { this.hidden = true; } };
  const commands = { executeCommand(id, ...args) {
    return CommandsRegistry.getCommand(id).handler({ get(service) {
      if (service === IClipboardService) return { writeText: async text => { copied.push(text); } };
      throw new Error(`复制不应读取其它宿主服务：${service}`);
    } }, ...args);
  } };
  const provider = new ResourceQuickAccess(quick, opener, groups, configuration, notifications, commands, {});
  const options = mode === 'no-owner' ? { searchOwnerError: '发起来源未明确' } : { searchOwners: owners };
  const lifetime = provider.provide(picker, cancellation.token, options);
  const until = async predicate => {
    for (let i = 0; i < 200; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error('来源合同未收敛');
  };
  try {
    if (mode === 'close' || mode === 'identity') {
      await until(() => releaseRead);
      if (mode === 'close') lifetime.dispose(); else invalidateGatewayToken(0);
      releaseRead(); await new Promise(resolve => setTimeout(resolve, 20));
    } else {
      await until(() => !picker.busy && picker.items.length > 0);
      if (mode === 'scope-cancel') {
        buttons.fire(picker.buttons[0]); await until(() => shows.length);
      } else if (mode.startsWith('copy')) {
        const item = picker.items[1];
        itemButtons.fire({ item, button: item.buttons[0] });
        if (['copy-identity', 'copy-gateway'].includes(mode)) {
          await until(() => releaseParse);
          if (mode === 'copy-identity') invalidateGatewayToken(0);
          else workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: owners[1].gatewayId });
          releaseParse();
        }
        await until(() => copied.length || errors.length);
      } else if (!['browse', 'stopped', 'unsupported', 'no-owner'].includes(mode)) {
        picker.activeItems = [picker.items[1]];
        picker.selectedItems = picker.activeItems;
        accepts.fire({ inBackground: mode === 'background' });
        if (mode === 'freeze') {
          await until(() => releaseParse);
          groups.activeGroup = { id: 9 };
          workspaceContext.publish({ ...workspaceContext.getSnapshot(), activeId: gatewayWorkspaceKey(owners[0]) });
          lifetime.dispose(); releaseParse();
        }
        await until(() => opened.length || errors.length);
      }
    }
    return { requests, opened, references, errors, shows, choices, copied, hidden: !!picker.hidden, aborted, released, addedGroups,
      rows: picker.items.map(item => ({ id: item.id, label: item.label, highlights: item.highlights, selectable: !!item.accept,
        copy: !!item.buttons?.some(button => button.tooltip === '复制资源 URI') })) };
  } finally {
    lifetime.dispose(); cancellation.dispose(true); provider.dispose(); opener.dispose();
    changes.dispose(); accepts.dispose(); buttons.dispose(); itemButtons.dispose(); unwrapSessionApiFetch(); workspaceContext.publish(previous);
  }
}

/** 真实 Opener／客户端；只替换目录回执和编辑器宿主，验证异步准入期间的身份切换。 */
export async function resourceOpenTargetContract(mode) {
  const previous = workspaceContext.getSnapshot(), owner = owners[0];
  const target = { gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, status: 'ready' };
  const sessionId = 'sess_20261112_000000_lifetime', threadId = 'thread_lifetime_exact';
  const kind = mode.split('-')[0], root = `boxteam://workspace/${owner.workspaceId}/resources/files/workspace`;
  const uri = kind === 'file' ? root + '/same.png'
    : `boxteam://workspace/${owner.workspaceId}/resources/session/${sessionId}${kind === 'thread' ? '/threads/' + threadId : ''}`;
  const requests = [], opened = [], errors = [], inputs = [];
  let addedGroups = 0;
  workspaceContext.publish({ ...previous, gatewayId: owner.gatewayId });
  const ok = data => Response.json({ code: 0, request_id: 'resource-open-target', data }, { headers: { 'X-Request-ID': 'resource-open-target' } });
  installSessionApiFetchMock(({ path, method, init }) => {
    requests.push({ path, method, body: init.body && JSON.parse(String(init.body)) });
    if (path.endsWith('/parse')) return ok({ gateway_id: owner.gatewayId, scope: 'workspace', scope_id: owner.workspaceId,
      kind: kind === 'file' ? 'files' : 'session', uri, reference_uri: uri,
      file_root_uri: kind === 'file' ? root : null, session_id: kind === 'file' ? null : sessionId,
      thread_id: kind === 'thread' ? threadId : null });
    if (path === '/api/gateway/workspaces') {
      if (mode.endsWith('route-identity')) invalidateGatewayToken(0);
      return ok({ items: [target] });
    }
    if (path === `/api/v1/sessions/${sessionId}/main-thread`) {
      if (mode.endsWith('main-identity')) invalidateGatewayToken(0);
      if (mode.endsWith('main-gateway')) workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: owners[1].gatewayId });
      return ok({ session_id: sessionId, thread_id: threadId });
    }
    throw new Error(`资源打开产生非预期请求 ${method} ${path}`);
  });
  const opener = new ResourceOpener({ registerOpener: () => ({ dispose() {} }) }, {
    async openEditor(input, options, group) {
      if (input instanceof ThreadInput) { inputs.push(input); opened.push({ owner: input.owner, group, options }); }
      else opened.push({ source: workspaceFileSource(input.resource), group: options, options: input.options });
    },
  }, { activeGroup: { id: 7 }, getGroup: id => ({ id }), findGroup: () => undefined,
    addGroup: () => { addedGroups++; return { id: 8 }; } }, { getValue: () => 'right' }, {
    createModelReference: async () => { throw new Error('文件或Thread不得先读取只读来源'); },
  }, {});
  try {
    try { await opener.open(uri, { boxteamResourceOwner: owner, openToSide: true, editorOptions: { pinned: true } }); }
    catch (cause) { errors.push(String(cause)); }
    return { requests, opened, errors, addedGroups, owner, threadId };
  } finally {
    for (const input of inputs) input.dispose();
    opener.dispose(); unwrapSessionApiFetch(); workspaceContext.publish(previous);
  }
}
