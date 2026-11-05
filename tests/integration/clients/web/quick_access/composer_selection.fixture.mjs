import '../../../../../src/clients/workbench/src/composerActions.js';
import { Event } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CommandsRegistry } from '../../../../../src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';
import { IQuickInputService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IEditorService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorService.js';
import { ThreadPane } from '../../../../../src/clients/workbench/src/threadPane.js';
import { ThreadInput } from '../../../../../src/clients/workbench/src/threadInput.js';
import { workspaceFileUri } from '../../../../../src/clients/workbench/src/workspaceFiles.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
import { ATTACH_RESOURCE_URI } from '../../../../../src/clients/workbench/src/resourceActions.js';

/** 实际 Composer 命令与 URI 客户端；HTTP 回执、宿主 Pane 和 writer 为具名服务装具。 */
export async function composerResourceSelectionContract(mode) {
  const originalFetch = globalThis.fetch, before = workspaceContext.getSnapshot();
  const owner = { gatewayId: 'gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', workspaceId: '019a0000-0000-7000-8000-000000000001' };
  const root = `boxteam://workspace/${owner.workspaceId}/resources/files/root`;
  const files = ['alpha.txt', 'beta.txt'].map(name => ({ label: name, resource: workspaceFileUri(owner, `${root}/${name}`, root) }));
  const requests = [], writes = [[], []], events = [], second = Promise.withResolvers(), ready = Promise.withResolvers();
  let options;
  class Pane extends ThreadPane {
    constructor(index) {
      super({ id: index + 1 }, {}, { getColorTheme: () => ({}), onDidColorThemeChange: Event.None }, { onWillSaveState: Event.None });
      const input = new ThreadInput({ ...owner, workspaceKey: '', sessionId: `session-${index}`, threadId: `thread-${index}`, title: `Thread ${index}` });
      this.target = { input, conversation: { captureDraft() {
        events.push(`capture:${index}`);
        return { async append(text) { events.push(`append:${index}`); writes[index].push(text); } };
      } } };
    }
    get threadTarget() { return this.target; }
  }
  const panes = [new Pane(0), new Pane(1)];
  const editors = { activeEditor: panes[0].threadTarget.input, activeEditorPane: panes[0], visibleEditorPanes: panes };
  const quick = { quickAccess: { async pick(_value, supplied) {
    options = supplied; events.push('pick');
    return mode === 'cancel' ? [] : mode === 'unsupported' ? [files[0], { label: '不支持的来源' }] : files;
  } } };
  const accessor = { get(service) {
    if (service === IEditorService) return editors;
    if (service === IQuickInputService) return quick;
    throw new Error('Composer 合同出现未声明的宿主服务');
  } };
  workspaceContext.publish({ ...before, gatewayId: owner.gatewayId });
  globalThis.fetch = async (url, init) => {
    const path = new URL(String(url), location.origin).pathname;
    if (path === '/api/gateway/auth/local-credential') return Response.json({ data: { token: 'composer-selection-test' } });
    if (path !== '/api/gateway/resource-addresses/parse') throw new Error(`引用出现额外读取：${path}`);
    const request = JSON.parse(init.body); requests.push(request); events.push(`parse:${requests.length}`);
    if (requests.length === 2) { ready.resolve(); await second.promise; }
    if (mode === 'failure' && requests.length === 2) return Response.json({ detail: '第二项地址解析失败' }, { status: 409 });
    return Response.json({ code: 0, message: 'ok', request_id: 'composer-selection-contract', data: { gateway_id: owner.gatewayId, scope: 'workspace', scope_id: owner.workspaceId,
      reference_uri: `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root/${requests.length === 1 ? 'alpha' : 'beta'}.txt` } });
  };
  try {
    const pending = CommandsRegistry.getCommand(ATTACH_RESOURCE_URI).handler(accessor).then(() => ({}), error => ({ failure: String(error) }));
    let beforeSecondWrites;
    if (mode === 'success' || mode === 'failure') {
      await Promise.race([ready.promise, pending.then(outcome => { throw new Error(`命令在第二项屏障前结束：${JSON.stringify({ outcome, requests, events })}`); })]);
      beforeSecondWrites = structuredClone(writes);
      editors.activeEditor = panes[1].threadTarget.input; editors.activeEditorPane = panes[1];
      second.resolve();
    }
    const outcome = await pending;
    return { requests, writes, beforeSecondWrites, events, outcome, canPickMany: options.canPickMany };
  } finally {
    second.resolve(); globalThis.fetch = originalFetch; workspaceContext.publish(before);
    for (const pane of panes) { pane.threadTarget.input.dispose(); pane.dispose(); }
  }
}
