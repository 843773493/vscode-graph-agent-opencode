import '../../../../../../src/clients/workbench/src/composerActions.js';
import { Emitter, Event } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { CommandsRegistry } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
import { IQuickInputService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IEditorService } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorService.js';
import { ThreadPane } from '../../../../../../src/clients/workbench/src/threadPane.js';
import { ThreadInput } from '../../../../../../src/clients/workbench/src/threadInput.js';
import { ResourceQuickAccess } from '../../../../../../src/clients/workbench/src/resources/quickAccess.js';
import { ATTACH_SOURCE_URIS } from '../../../../../../src/clients/workbench/src/resourceActions.js';
import { workspaceContext } from '../../../../../../src/clients/workbench/src/workspaceContext.js';
import { gatewayWorkspaceKey } from '../../../../../../src/clients/web/src/api/workspaceIdentity.js';
import { invalidateGatewayToken } from '../../../../../../src/clients/web/src/api/http.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';

/** 同时运行真实来源 Provider、资源命令与 URI 客户端；只替换 HTTP、Picker 与原草稿 writer 边界。 */
export async function sourceReferenceSelectionContract(mode) {
  const before = workspaceContext.getSnapshot(), requests = [], drafts = [[], []], events = [], errors = [], selectedItems = [];
  const owners = ['a', 'b'].map(letter => ({ gatewayId: `gateway_${letter.repeat(32)}`, workspaceId: '019a0000-0000-7000-8000-000000000001' }));
  const targets = owners.map((owner, index) => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, name: `来源 ${index}`, status: 'ready' }));
  workspaceContext.publish({ ...before, gatewayId: owners[0].gatewayId, loading: false, error: null, workspaces: targets, activeId: gatewayWorkspaceKey(owners[0]) });
  const ok = data => Response.json({ code: 0, data, request_id: 'source-selection-contract' }, { headers: { 'X-Request-ID': 'source-selection-contract' } });
  const uri = (owner, tail) => `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/${tail}`;
  const attachment = { file_id: uri(owners[0], `attachments/att_${'c'.repeat(32)}/original/content`), name: '附件.pdf', content_type: 'application/pdf', availability: 'available' };
  const ready = Promise.withResolvers(), second = Promise.withResolvers();
  let parseCount = 0, pickerOptions, scopeOptions, beforeWrites;
  class Pane extends ThreadPane {
    constructor(index) {
      super({ id: index + 7 }, {}, { getColorTheme: () => ({}), onDidColorThemeChange: Event.None }, { onWillSaveState: Event.None });
      this.target = { input: new ThreadInput({ ...owners[0], workspaceKey: '', sessionId: 'ses_origin', threadId: `thr_${index}`, title: `Thread ${index}` }),
        conversation: { captureDraft() { events.push(`capture:${index}`); return { append: async text => drafts[index].push(text) }; } } };
    }
    get threadTarget() { return this.target; }
  }
  const panes = [new Pane(0), new Pane(1)];
  const editors = { activeEditor: panes[0].target.input, activeEditorPane: panes[0], visibleEditorPanes: panes };
  const changes = new Emitter(), itemButtons = new Emitter(), buttons = new Emitter(), cancellation = new CancellationTokenSource();
  let sourceVisits = 0;
  const picker = { value: 'resource ', items: [], activeItems: [], selectedItems: [], buttons: [], keyMods: { alt: false, ctrlCmd: false },
    onDidChangeValue: changes.event, onDidAccept: Event.None, onDidTriggerButton: buttons.event,
    onDidTriggerItemButton: itemButtons.event, onDidTriggerSeparatorButton: Event.None, hide() {} };
  const until = async predicate => {
    for (let i = 0; i < 300; i++) { if (predicate()) return; await new Promise(resolve => setTimeout(resolve, 5)); }
    throw new Error('来源选择合同未收敛');
  };
  installSessionApiFetchMock(async ({ path, init, method }) => {
    const headers = new Headers(init.headers), body = init.body && JSON.parse(String(init.body));
    const owner = owners.find(item => item.gatewayId === headers.get('X-BoxTeam-Target-Gateway-Id'));
    requests.push({ path, method, body, gatewayId: owner?.gatewayId, workspaceId: headers.get('X-BoxTeam-Workspace-Id') });
    if (path === '/api/gateway/workspaces') return ok({ items: targets });
    if (path.endsWith('/config/sources')) return ok({ sources: [{ source_key: 'inline:0', layer: 'inline', presence: 'present', precedence: 0,
      uri: 'boxteam://inline/probe/resources/config/gateway_inline' }, { source_key: 'logical', layer: 'user', presence: 'present', precedence: 1 }] });
    if (path.endsWith('/agent-spec')) return ok({ node: { uri: uri(owner, 'agent-spec/root/AGENTS.md'), resource_shape: 'file', operations: ['read'] } });
    if (path.endsWith('/skill-sources') || path.endsWith('/local-skills')) return ok({ workspace_id: owner.workspaceId, entries: [] });
    if (path.endsWith('/gateway-skills')) return ok({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, entries: [], available: true });
    if (path.endsWith('/attachments')) return ok({ session_id: 'ses_origin', thread_id: 'thr_0', items: [attachment] });
    if (path === '/api/gateway/resource-addresses/parse') {
      parseCount++;
      if (mode === 'success' || mode === 'failure' || mode === 'identity-parse') {
        if (parseCount === 2) { ready.resolve(); await second.promise; }
      }
      if (mode === 'failure' && parseCount === 2) return Response.json({ detail: '第二项原地址拒绝' }, { status: 409 });
      const sourceGateway = body.resource_gateway_id;
      const scoped = body.uri.startsWith('boxteam://inline/');
      return ok({ uri: body.uri, gateway_id: sourceGateway, scope: scoped ? 'inline' : 'workspace',
        scope_id: scoped ? 'probe' : owners[0].workspaceId, kind: scoped ? 'config' : body.uri.includes('/agent-spec/') ? 'agent-spec' : 'attachments',
        reference_uri: scoped ? body.uri.replace('boxteam://', `boxteam://${sourceGateway}/`) : body.uri });
    }
    throw new Error(`来源只选择不得读取或操作其它资源：${method} ${path}`);
  });
  let provider, lifetime;
  const quick = {
    backButton: { tooltip: 'Back' },
    async pick(items, options) {
      scopeOptions = { title: options.title, canPickMany: options.canPickMany, owners: items.map(item => item.owner) };
      events.push('scope');
      if (mode === 'back-cancel' && sourceVisits) return;
      if (mode === 'scope-cancel') return;
      if (mode === 'scope-identity') invalidateGatewayToken(0);
      return mode === 'only-b' ? [items[1]] : items;
    },
    quickAccess: { async pick(value, options) {
      pickerOptions = options;
      events.push('sources');
      sourceVisits++;
      lifetime?.dispose();
      lifetime = provider.provide(picker, cancellation.token, options.providerOptions);
      await until(() => !picker.busy && picker.items.length);
      if (mode === 'back-cancel') { buttons.fire(quick.backButton); return []; }
      editors.activeEditor = panes[1].target.input; editors.activeEditorPane = panes[1];
      if (mode === 'source-cancel') return [];
      if (mode === 'source-closed') panes[0].target = undefined;
      if (mode === 'source-identity') invalidateGatewayToken(0);
      const chosen = [picker.items.find(item => item.label === 'AGENTS.md' && item.resourceOwner.gatewayId === owners[1].gatewayId),
        picker.items.find(item => item.label === 'inline:0.jsonc' && item.description.includes('Gateway') && item.resourceOwner.gatewayId === owners[1].gatewayId)];
      if (mode === 'unsupported') chosen.push(picker.items.find(item => item.label === 'logical.jsonc'));
      else if (mode !== 'only-b') chosen.push(picker.items.find(item => item.label === attachment.name));
      if (chosen.some(item => !item)) throw new Error('来源选择缺少预期的原 Provider 候选');
      selectedItems.push(...chosen.map(item => ({ label: item.label, disabled: item.disabled, buttons: item.buttons?.length, owner: item.resourceOwner })));
      return chosen;
    } },
  };
  const accessor = { get(service) {
    if (service === IEditorService) return editors;
    if (service === IQuickInputService) return quick;
    throw new Error('来源引用请求未声明的宿主服务');
  } };
  provider = new ResourceQuickAccess(quick, { open: async () => { throw new Error('只选择不得打开'); } }, { activeGroup: { id: 7 } },
    { getValue: () => true }, { error: cause => errors.push(String(cause)) }, { executeCommand: async () => { throw new Error('只选择不得触发其它命令'); } }, editors);
  try {
    const pending = CommandsRegistry.getCommand(ATTACH_SOURCE_URIS).handler(accessor).then(() => ({}), cause => ({ error: String(cause) }));
    if (['success', 'failure', 'identity-parse'].includes(mode)) {
      await Promise.race([ready.promise, pending.then(outcome => { throw new Error(`第二项解析前结束：${JSON.stringify(outcome)}`); })]);
      beforeWrites = structuredClone(drafts);
      if (mode === 'identity-parse') invalidateGatewayToken(0);
      second.resolve();
    }
    const outcome = await pending;
    return { requests, drafts, beforeWrites, events, errors, selectedItems, outcome, pickerOptions, scopeOptions,
      scopeButtons: picker.buttons.length, rows: picker.items.map(item => ({ label: item.label, disabled: item.disabled })), owners };
  } finally {
    second.resolve(); lifetime?.dispose(); provider.dispose(); cancellation.dispose(true); changes.dispose(); itemButtons.dispose(); buttons.dispose();
    for (const pane of panes) { pane.target?.input.dispose(); pane.dispose(); }
    unwrapSessionApiFetch(); workspaceContext.publish(before);
  }
}
