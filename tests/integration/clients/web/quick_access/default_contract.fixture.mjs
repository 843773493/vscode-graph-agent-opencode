import { WorkspaceQuickAccess } from '../../../../../src/clients/workbench/src/search/quickAccess.js';
import { ThreadInput } from '../../../../../src/clients/workbench/src/threadInput.js';
import { PickerEditorState } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/browser/quickaccess.js';
import { Event, Emitter } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { workspaceFileUri } from '../../../../../src/clients/workbench/src/workspaceFiles.js';

const owner = { gatewayId: 'gateway-a', workspaceId: 'workspace-a' };
const root = 'boxteam://workspace/workspace-a/resources/files/root';
const uri = name => workspaceFileUri(owner, root + '/' + name, root);

/** 仅替原 Picker 事件和宿主服务；实际 Provider、取消及接受逻辑均来自生产源码。 */
function defaultAccess(options, history = []) {
  const changes = new Emitter(), accepts = new Emitter(), cancellation = new CancellationTokenSource();
  const picker = { value: '', items: [], activeItems: [], selectedItems: [], keyMods: { alt: false, ctrlCmd: false },
    onDidHide: Event.None, onDidChangeActive: Event.None, onDidChangeValue: changes.event, onDidAccept: accepts.event,
    onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None, hide() { this.hidden = true; } };
  const groups = { groups: [], activeGroup: { id: 1 } }, editors = { openEditor() { throw new Error('只选择调用方不应打开编辑器'); } };
  const provider = new WorkspaceQuickAccess({ createInstance: () => new PickerEditorState(editors, groups) }, editors, groups,
    { getHistory: () => history }, { getValue: () => undefined }, {}, {}, {}, {}, {},
    { open() { throw new Error('delegate 不应被 URI opener 抢占'); } }, { error(error) { throw error; } }, { enabled: false });
  const lifetime = provider.provide(picker, cancellation.token, options);
  return { picker, accepts, change(value) { picker.value = value; changes.fire(); },
    dispose() { cancellation.dispose(true); lifetime.dispose(); provider.dispose(); changes.dispose(); accepts.dispose(); } };
}

export async function delegateContract() {
  const calls = [], observed = {};
  const f = defaultAccess({ includeOpenToSideButton: false, fileSearchDelegate: {
    search(pattern, token) { return new Promise(resolve => calls.push({ pattern, token, resolve })); },
    getItemDescription: () => '调用方目录',
  } });
  try {
    observed.initial = calls.map(call => call.pattern);
    f.change('https://example.test/file');
    observed.patterns = calls.map(call => call.pattern);
    observed.cancelledPrevious = calls[0].token.isCancellationRequested;
    f.change('delegate');
    calls[2].resolve({ results: [{ resource: uri('delegate.txt') }], messages: [] });
    await Promise.resolve(); await Promise.resolve();
    observed.picks = f.picker.items.map(item => ({ label: item.label, description: item.description, buttons: item.buttons }));
    calls[0].resolve({ results: [{ resource: uri('old.txt') }], messages: [] });
    calls[1].resolve({ results: [], messages: [] });
    await Promise.resolve(); await Promise.resolve();
    observed.afterLate = f.picker.items.map(item => item.label);
    f.change('cancel');
  } finally { f.dispose(); }
  observed.cancelledOnDispose = calls.at(-1).token.isCancellationRequested;
  calls.at(-1).resolve({ results: [], messages: [] });
  return observed;
}

export function selectContract() {
  const received = [], observed = {};
  const f = defaultAccess({ searchOwner: owner, additionPicks: [{ label: '附件来源', kind: 'contextPick' }, { label: '隐藏项' }],
    filter: item => item.label !== '隐藏项', handleAccept: (item, background) => received.push({ label: item.label, kind: item.kind, background }) },
  [{ resource: uri('history.txt') }, new ThreadInput({ ...owner, routeId: '', sessionId: 'session-a', threadId: 'thread-a', title: '历史 Thread' })]);
  try {
    observed.labels = f.picker.items.filter(item => item.type !== 'separator').map(item => item.label);
    f.picker.activeItems = [f.picker.items[0]];
    f.accepts.fire({ inBackground: true });
    observed.backgroundKeptOpen = !f.picker.hidden;
    f.accepts.fire({ inBackground: false });
    observed.foregroundClosed = f.picker.hidden;
    observed.received = received;
    return observed;
  } finally { f.dispose(); }
}
