import { Emitter, Event } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { ResourceQuickAccess } from '../../../../../../src/clients/workbench/src/resources/quickAccess.js';
import { ResourceOpener } from '../../../../../../src/clients/workbench/src/resourceOpener.js';
import { sourceDocumentSource } from '../../../../../../src/clients/workbench/src/sourceDocuments.js';
import { workspaceContext } from '../../../../../../src/clients/workbench/src/workspaceContext.js';
import { gatewayWorkspaceKey } from '../../../../../../src/clients/web/src/api/workspaceIdentity.js';
import { invalidateGatewayUserSession } from '../../../../../../src/clients/web/src/api/http.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

const owners = ['a', 'b'].map(letter => ({ gatewayId: `gateway_${letter.repeat(32)}`, workspaceId: '019a0000-0000-7000-8000-000000000001' }));
// 装具回执，不代替后端 grammar；两 Gateway 故意返回相同本机简称。
const uri = `boxteam://workspace/${owners[0].workspaceId}/resources/agent-spec/root/AGENTS.md`;

export async function agentSpecContract(mode) {
  const previous = workspaceContext.getSnapshot(), changes = new Emitter(), accepts = new Emitter(), buttons = new Emitter(), cancellation = new CancellationTokenSource();
  const requests = [], opened = [], references = [], errors = [], shows = [], choices = [];
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
    if (path === '/api/v1/resources/skill-sources') return ok({ workspace_id: owners[0].workspaceId, entries: [] });
    if (path === '/api/v1/resources/local-skills') return ok({ workspace_id: owners[0].workspaceId, entries: [] });
    if (path === '/api/v1/resources') return ok({ workspace_id: owners[0].workspaceId, items: [] });
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
      const owner = owners.find(item => item.gatewayId === body.resource_gateway_id) ?? owners[0];
      const result = () => ok({ gateway_id: mode === 'mismatch' ? owners[0].gatewayId : owner.gatewayId,
        scope: 'workspace', scope_id: owner.workspaceId, kind: 'agent-spec', uri,
        reference_uri: `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/agent-spec/root/AGENTS.md` });
      if (mode === 'freeze') return new Promise(resolve => { releaseParse = () => resolve(result()); });
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
    return { object: {}, dispose() { released += 1; } }; } };
  const opener = new ResourceOpener({ registerOpener: () => ({ dispose() {} }) },
    { openEditor: async (input, id) => opened.push({ source: sourceDocumentSource(input.resource), options: input.options, group: id }) },
    groups, configuration, models);
  const notifications = { error: cause => errors.push(String(cause)) };
  const picker = { value: 'resource ag', items: [], activeItems: [], selectedItems: [], buttons: [],
    keyMods: { alt: mode === 'side', ctrlCmd: mode === 'pinned' }, onDidChangeValue: changes.event,
    onDidAccept: accepts.event, onDidTriggerButton: buttons.event, onDidTriggerItemButton: Event.None,
    onDidTriggerSeparatorButton: Event.None, hide() { this.hidden = true; } };
  const provider = new ResourceQuickAccess(quick, opener, groups, configuration, notifications, {}, {});
  const options = mode === 'no-owner' ? { searchOwnerError: '发起来源未明确' } : { searchOwners: owners };
  const lifetime = provider.provide(picker, cancellation.token, options);
  const until = async predicate => {
    for (let i = 0; i < 200; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error('来源合同未收敛');
  };
  try {
    if (mode === 'close' || mode === 'identity') {
      await until(() => releaseRead);
      if (mode === 'close') lifetime.dispose(); else invalidateGatewayUserSession(0);
      releaseRead(); await new Promise(resolve => setTimeout(resolve, 20));
    } else {
      await until(() => !picker.busy && picker.items.length > 0);
      if (mode === 'scope-cancel') {
        buttons.fire(picker.buttons[0]); await until(() => shows.length);
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
    return { requests, opened, references, errors, shows, choices, aborted, released, addedGroups,
      rows: picker.items.map(item => ({ id: item.id, label: item.label, highlights: item.highlights, selectable: !!item.accept })) };
  } finally {
    lifetime.dispose(); cancellation.dispose(true); provider.dispose(); opener.dispose();
    changes.dispose(); accepts.dispose(); buttons.dispose(); unwrapSessionApiFetch(); workspaceContext.publish(previous);
  }
}
