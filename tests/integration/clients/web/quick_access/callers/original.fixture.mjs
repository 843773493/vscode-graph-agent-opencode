import * as q from '../query.fixture.ts';
import { PickerHost } from './picker.fixture.mjs';
import { WorkspaceQuickAccess } from '../../../../../../src/clients/workbench/src/search/quickAccess.js';
import { EmptyFileEditorSearch } from '../../../../../../src/clients/workbench/upstream/src/vs/sessions/contrib/editor/browser/emptyFileEditorSearch.js';
import { AttachContextAction } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/chat/browser/actions/chatContextActions.js';
import { QuickAccessController } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/browser/quickAccess.js';
import { Registry } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/registry/common/platform.js';
import { Extensions } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { Codicon } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/codicons.js';
import { IQuickInputService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IEditorService } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IHistoryService } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/services/history/common/history.js';
import { IContextKeyService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/contextkey/common/contextkey.js';
import { IKeybindingService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/keybinding/common/keybinding.js';
import { IOpenerService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/opener/common/opener.js';
import { INotificationService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/notification/common/notification.js';
import { IViewsService } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/services/views/common/viewsService.js';
import { ISearchService } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/services/search/common/search.js';
import { IFileService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/files/common/files.js';
import { IAccessibilityService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/accessibility/common/accessibility.js';
import { IAccessibleViewService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/accessibility/browser/accessibleView.js';
import { IChatWidgetService, IQuickChatService } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/chat/browser/chat.js';
import { IChatContextPickService } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/chat/browser/attachments/chatContextPickService.js';
import { IChatSessionsService } from '../../../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/chat/common/chatSessionsService.js';
import { ICommandService } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/commands/common/commands.js';

const owner = { gatewayId: 'gateway-a', workspaceId: 'workspace-a' };
const root = 'boxteam://workspace/workspace-a/resources/files/root';
const uri = name => q.workspaceFileUri(owner, root + (name ? '/' + name : ''), root);
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

function fixture(items = []) {
  const lifetime = new q.DisposableStore(), host = lifetime.add(new PickerHost());
  const calls = [], cleared = [], attachments = [], otherAttachments = [];
  const context = lifetime.add(new q.SearchWorkspaceContext({ version: 1, workspaces: [{ ...owner, rootUri: root, label: 'A' }] }));
  const widget = { inputPart: {}, viewContext: {}, attachmentModel: { addContext: (...values) => attachments.push(...values) } };
  const widgets = { lastFocusedWidget: { inputPart: {}, attachmentModel: { addContext: (...values) => otherAttachments.push(...values) } } };
  const groups = { groups: [], activeGroup: { id: 1 } };
  const fail = () => { throw new Error('只选择调用方不应打开编辑器或访问项目 Composer'); };
  const editors = { activeEditor: new q.ThreadInput({ ...owner, routeId: '', sessionId: 'session-a', threadId: 'thread-a', title: 'A' }), openEditor: fail };
  const configuration = { editor: { wordSeparators: ' /' }, files: { encoding: 'utf8', exclude: {} }, search: { useIgnoreFiles: false } };
  const services = new q.ServiceCollection(
    [q.IConfigurationService, { getValue: key => typeof key === 'string' ? key.split('.').reduce((value, part) => value?.[part], configuration) : configuration }],
    [q.IWorkspaceContextService, context], [q.IEditorGroupsService, groups], [q.ILogService, new q.NullLogService()],
    [q.IPathService, { userHome: () => q.URI.parse('file:///home/native') }],
    [q.IUriIdentityService, { extUri: q.extUri, onDidChange: q.Event.None }],
    [IQuickInputService, host], [IEditorService, editors],
    [IHistoryService, { getHistory: () => [{ resource: uri('history.txt') }, { resource: q.URI.parse('forbidden:/hidden.txt') }] }],
    [IContextKeyService, { contextMatchesRules: () => true }], [IKeybindingService, { lookupKeybinding: () => undefined }],
    [IOpenerService, { open: fail }], [INotificationService, { error: error => { throw error; } }],
    [IQuickChatService, { enabled: false, open: fail }], [IViewsService, {}], [IFileService, {}],
    [IAccessibilityService, { isScreenReaderOptimized: () => false }], [IAccessibleViewService, {}],
    [ISearchService, { schemeHasFileSearchProvider: () => true,
      fileSearch: (query, token) => new Promise(resolve => calls.push({ query, token, resolve })),
      clearCache: async key => { cleared.push(key); } }],
    [IChatWidgetService, widgets], [IChatContextPickService, { items }],
    [IChatSessionsService, { getContentProviderSchemes: () => ['forbidden'] }], [ICommandService, { executeCommand: fail }],
  );
  const instantiation = lifetime.add(new q.InstantiationService(services, true));
  const registry = Registry.as(Extensions.Quickaccess);
  lifetime.add(registry.registerQuickAccessProvider({ prefix: '', ctor: WorkspaceQuickAccess, helpEntries: [] }));
  const blocked = [];
  lifetime.add(registry.registerQuickAccessProvider({ prefix: '%', helpEntries: [], ctor: class {
    provide() { blocked.push('%'); return { dispose() {} }; }
  } }));
  const controller = lifetime.add(instantiation.createInstance(QuickAccessController));
  host.connect(controller);
  return { lifetime, host, instantiation, calls, cleared, widget, widgets, attachments, otherAttachments, blocked };
}

export async function emptyEditorContract() {
  const f = fixture(), container = document.body.appendChild(document.createElement('div'));
  const search = f.instantiation.createInstance(EmptyFileEditorSearch, container);
  const workspace = { folders: [{ name: 'A', workingDirectory: uri('') }] };
  try {
    search.setWorkspace(workspace); search.show();
    const first = f.host.currentQuickInput;
    await tick();
    const initial = f.calls[0];
    first.value = 'needle';
    await tick();
    const changed = f.calls[1];
    initial.resolve({ results: [{ resource: uri('late.txt') }], messages: [] });
    changed.resolve({ results: [{ resource: uri('dir/needle.txt') }], messages: [] });
    await tick();
    const labels = first.items.map(item => item.label);
    const resultPick = first.items.find(item => item.label === 'needle.txt');
    const options = f.host.shows[0].options;
    first.hide();
    await tick();
    const hiddenCleared = [...f.cleared];
    const hiddenCancelled = changed.token.isCancellationRequested;
    search.show(); await tick();
    const reopened = f.calls[2];
    search.setWorkspace(undefined); await tick();
    const switchedCancelled = reopened.token.isCancellationRequested;
    search.setWorkspace(workspace); search.show(); await tick();
    const final = f.calls[3];
    search.dispose(); await tick();
    return { patterns: f.calls.map(call => call.query.filePattern), roots: changed.query.folderQueries.map(folder => folder.folder.toString()),
      filesOnly: options.providerOptions.filesOnly, sideButton: options.providerOptions.includeOpenToSideButton,
      cancelledPrevious: initial.token.isCancellationRequested, labels, description: resultPick.description, buttons: resultPick.buttons,
      hiddenCleared, cacheKeys: f.calls.map(call => call.query.cacheKey), cleared: f.cleared,
      hiddenCancelled, switchedCancelled, disposedCancelled: final.token.isCancellationRequested, input: container.querySelector('input')?.value };
  } finally {
    search.dispose(); f.lifetime.dispose(); container.remove();
    for (const call of f.calls) call.resolve({ results: [], messages: [] });
  }
}

export async function chatCallerContract(mode) {
  let configDisposed = 0, receiver;
  const attachment = { id: 'native-chat-value', name: '原 Chat 附件', kind: 'generic', value: 'value' };
  const value = { type: 'valuePick', label: '原附件', icon: Codicon.file, asAttachment: widget => { receiver = widget; return attachment; } };
  const child = { type: 'pickerPick', label: '二级附件', icon: Codicon.folder, asPicker: widget => {
    receiver = widget;
    return { placeholder: '原二级选择器', picks: Promise.resolve([{ label: '二级值', asAttachment: () => attachment }]), dispose() { configDisposed++; } };
  } };
  const f = fixture([value, child]);
  const action = new AttachContextAction();
  try {
    await f.instantiation.invokeFunction(accessor => action.run(accessor, { widget: f.widget }));
    const first = f.host.currentQuickInput;
    const visible = first.items.filter(item => item.type !== 'separator').map(item => item.label);
    const prefixes = f.host.shows[0].options.enabledProviderPrefixes;
    if (mode === 'prefix') {
      first.value = '%forbidden'; await tick();
      return { blocked: f.blocked, visible, prefixes, attachments: f.attachments, otherAttachments: f.otherAttachments };
    }
    first.accept(first.items.find(item => item.label === (mode === 'value' ? '原附件' : '二级附件')));
    await tick();
    if (mode === 'back' || mode === 'cancel' || mode === 'child') {
      const secondary = f.host.currentQuickInput;
      if (mode === 'cancel') secondary.hide();
      else secondary.accept(secondary.items.find(item => item.label === (mode === 'back' ? 'Go back ↩' : '二级值')));
      await tick();
      if (mode === 'back') {
        const returned = f.host.currentQuickInput;
        f.widgets.lastFocusedWidget = { inputPart: {}, attachmentModel: { addContext: (...values) => f.otherAttachments.push(...values) } };
        returned.accept(returned.items.find(item => item.label === '原附件'));
        await tick();
      }
    }
    return { visible, prefixes, shows: f.host.shows.length, configDisposed, sameReceiver: receiver === f.widget,
      attachments: f.attachments, otherAttachments: f.otherAttachments };
  } finally { f.lifetime.dispose(); }
}
