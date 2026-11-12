import '../../../../../harness/js/workbench-locale.ts';
import { Emitter, Event } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { CommandsRegistry, ICommandService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
import { IClipboardService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/clipboard/common/clipboardService.js';
import { IEditorService } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorService.js';
import { PickerEditorState } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/browser/quickaccess.js';
import { WorkspaceQuickAccess } from '../../../../../../src/clients/workbench/src/search/quickAccess.js';
import { ThreadInput } from '../../../../../../src/clients/workbench/src/threadInput.js';
import { resourceReference } from '../../../../../../src/clients/workbench/src/resourceActions.js';
import { workspaceContext } from '../../../../../../src/clients/workbench/src/workspaceContext.js';
import { installSessionApiFetchMock, unwrapSessionApiFetch } from '../../../../../../src/clients/web/src/api/session/sessionApiFetchMock.js';
import { composerResourceSelectionContract } from '../composer_selection.fixture.mjs';

const gatewayA = `gateway_${'a'.repeat(32)}`, gatewayB = `gateway_${'b'.repeat(32)}`;
const workspaceId = '019a0000-0000-7000-8000-000000000001';
const sessionId = `ses_${'c'.repeat(32)}`, mainThreadId = `thr_${'d'.repeat(32)}`, childThreadId = `thr_${'e'.repeat(32)}`;
const endpoint = '/api/gateway/resource-addresses/thread';
const threadUri = ({ gatewayId, workspaceId, sessionId, threadId }) =>
  `boxteam://${gatewayId}/workspace/${workspaceId}/resources/session/${sessionId}/threads/${threadId}`;
const responseAddress = body => ({ uri: `boxteam://workspace/${body.workspace_id}/resources/session/${body.session_id}/threads/${body.thread_id}`,
  reference_uri: threadUri({ gatewayId: body.resource_gateway_id, workspaceId: body.workspace_id,
    sessionId: body.session_id, threadId: body.thread_id }), gateway_id: body.resource_gateway_id,
  scope: 'workspace', scope_id: body.workspace_id, kind: 'session', session_id: body.session_id,
  thread_id: body.thread_id, file_root_uri: null });
const ok = data => Response.json({ code: 0, message: 'ok', request_id: 'thread-reference-contract', data },
  { headers: { 'X-Request-ID': 'thread-reference-contract' } });
const owner = (gatewayId, threadId) => ({ gatewayId, workspaceId, sessionId, threadId, workspaceKey: '', title: threadId });

/** 真实 resourceReference、真实最近打开 Provider 和命令；仅 HTTP 回执及 Workbench 宿主为装具。 */
export async function threadReferenceContract(mode) {
  const previous = workspaceContext.getSnapshot(), requests = [], clipboard = [], errors = [], commandIds = [];
  let releaseAddress;
  workspaceContext.publish({ ...previous, gatewayId: gatewayA, loading: false, error: null });
  installSessionApiFetchMock(({ path, method, init }) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ path, method, body });
    if (path !== endpoint) throw new Error(`Thread 引用不得读取或打开资源: ${method} ${path}`);
    if (mode === 'identity-changed') return new Promise(resolve => { releaseAddress = () => resolve(ok(responseAddress(body))); });
    const address = responseAddress(body);
    if (mode === 'wrong-gateway') address.gateway_id = gatewayA;
    if (mode === 'wrong-scope') address.scope_id = `${workspaceId.slice(0, -1)}2`;
    if (mode === 'wrong-kind') address.kind = 'files';
    if (mode === 'wrong-session') address.session_id = `ses_${'f'.repeat(32)}`;
    if (mode === 'wrong-thread') address.thread_id = `thr_${'f'.repeat(32)}`;
    return ok(address);
  });
  try {
    if (mode === 'identity-changed') {
      const pending = resourceReference(new ThreadInput(owner(gatewayB, childThreadId)).resource).then(
        value => ({ value }), cause => ({ error: String(cause) }));
      for (let attempt = 0; attempt < 200 && !releaseAddress; attempt++) await new Promise(resolve => setTimeout(resolve, 5));
      if (!releaseAddress) throw new Error('Thread 地址请求未进入身份屏障');
      workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: gatewayB });
      releaseAddress();
      const outcome = await pending;
      return { requests, outcome, error: outcome.error, clipboard, errors };
    }

    if (mode === 'quick-access-copy') {
      const input = new ThreadInput(owner(gatewayA, childThreadId));
      const changes = new Emitter(), buttons = new Emitter(), cancellation = new CancellationTokenSource();
      const groups = { activeGroup: { id: 7 }, groups: [], getGroup: () => undefined };
      const editors = { activeEditor: input, activeEditorPane: undefined, visibleEditorPanes: [], activeTextEditorControl: undefined };
      const commandPromises = [];
      const accessor = { get(service) {
        if (service === IClipboardService) return { writeText: async value => clipboard.push(value) };
        if (service === IEditorService) return editors;
        if (service === ICommandService) return { executeCommand(id, resource) {
          commandIds.push(id);
          const command = CommandsRegistry.getCommand(id);
          if (!command) throw new Error(`未注册命令 ${id}`);
          const pending = Promise.resolve(command.handler(accessor, resource));
          commandPromises.push(pending);
          return pending;
        } };
        throw new Error('Thread Quick Access 合同请求了未声明的宿主服务');
      } };
      const instantiation = { createInstance(ctor) {
        if (ctor === PickerEditorState) return new PickerEditorState(editors, groups);
        throw new Error(`Thread Quick Access 合同请求了未声明的实例 ${ctor.name}`);
      }, invokeFunction(callback) { return callback(accessor); } };
      const picker = { value: '', items: [], activeItems: [], selectedItems: [], buttons: [], keyMods: { alt: false, ctrlCmd: false },
        onDidChangeValue: changes.event, onDidChangeActive: Event.None, onDidAccept: Event.None, onDidHide: Event.None,
        onDidTriggerButton: Event.None, onDidTriggerItemButton: buttons.event, onDidTriggerSeparatorButton: Event.None, hide() {} };
      const provider = new WorkspaceQuickAccess(instantiation, editors, groups, { getHistory: () => [input], removeFromHistory() {} },
        { getValue: () => undefined }, {}, {}, {},
        { open: async () => { throw new Error('Thread 复制按钮不得打开资源'); } }, { error: cause => errors.push(String(cause)) }, {}, {});
      const lifetime = provider.provide(picker, cancellation.token);
      try {
        for (let attempt = 0; attempt < 200 && !picker.items.some(candidate => candidate.id === input.resource.toString()); attempt++) {
          await new Promise(resolve => setTimeout(resolve, 5));
        }
        const item = picker.items.find(candidate => candidate.id === input.resource.toString());
        if (!item) throw new Error(`最近打开没有发布 Thread 候选: ${JSON.stringify(picker.items.map(candidate => candidate.id))}`);
        const copyButton = item.buttons?.find(button => button.tooltip === '复制资源 URI');
        if (!copyButton) throw new Error('最近打开 Thread 候选缺少既有“复制资源 URI”按钮');
        buttons.fire({ item, button: copyButton });
        await Promise.all(commandPromises);
        return { requests, clipboard, errors, commandIds, buttonLabels: item.buttons.map(button => button.tooltip),
          expected: threadUri({ gatewayId: gatewayA, workspaceId, sessionId, threadId: childThreadId }) };
      } finally { lifetime.dispose(); cancellation.dispose(true); provider.dispose(); changes.dispose(); buttons.dispose(); input.dispose(); }
    }

    const identities = mode === 'wrong-gateway' || mode === 'wrong-scope' || mode === 'wrong-kind'
      || mode === 'wrong-session' || mode === 'wrong-thread'
      ? [owner(gatewayB, childThreadId)]
      : [owner(gatewayA, mainThreadId), owner(gatewayA, childThreadId), owner(gatewayB, mainThreadId), owner(gatewayB, childThreadId)];
    const results = [];
    for (const identity of identities) {
      try { results.push({ reference: await resourceReference(new ThreadInput(identity).resource) }); }
      catch (cause) { results.push({ error: String(cause) }); }
    }
    return { requests, results, errors, clipboard,
      expected: identities.map(identity => threadUri(identity)), mainThreadId, childThreadId };
  } finally {
    unwrapSessionApiFetch();
    workspaceContext.publish(previous);
  }
}

export { composerResourceSelectionContract };
