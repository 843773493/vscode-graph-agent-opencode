import { URI } from '../upstream/src/vs/base/common/uri.js';
import { t } from '../../shared/core/i18n/index.js';
import actionTitles from '../../shared/core/i18n/actionTitles.en.json';
import { getCodeEditor, isDiffEditor } from '../upstream/src/vs/editor/browser/editorBrowser.js';
import { Action2, MenuId, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { ContextKeyExpr } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import type { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService, type IQuickPickItem } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { EditorResourceAccessor, SideBySideEditor } from '../upstream/src/vs/workbench/common/editor.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import type { IAnythingQuickAccessProviderRunOptions } from '../upstream/src/vs/workbench/contrib/search/common/search.js';
import { onGatewayTokenInvalidated } from '../../web/src/api/http.js';
import { ATTACH_RESOURCE_URI, ATTACH_SOURCE_URIS, PASTE_SELECTED_CONTENT, resourceReference, supportsResourceReference, supportsChatResourceReference, type ResourceReferenceOwner } from './resourceActions.js';
import { ThreadPane } from './threadPane.js';
import { ThreadInput } from './threadInput.js';
import type { SearchInvocationOptions } from './search/target.js';
import { chooseSearchOwners } from './search/quickAccessScope.js';
import type { ResourceAccessOptions } from './resources/quickAccess.js';
import { workspaceContext } from './workspaceContext.js';

/** 仅持有本次交互的原 Pane／草稿 owner；关闭或切换后原句柄不可复用。 */
export interface ComposerResourceTarget {
  pane: ThreadPane;
  target: ThreadPane['threadTarget'];
}

const referenceContext = ContextKeyExpr.or(...['boxteam-file', 'boxteam-source'].map(scheme => ContextKeyExpr.equals('resourceScheme', scheme)));
const contentContext = ContextKeyExpr.or(referenceContext, ContextKeyExpr.equals('resourceScheme', 'boxteam-change'));

/** 接收者来自发起编辑器，或明确选择的可见 Thread；后续焦点不参与归属。 */
async function appendToThread(accessor: ServicesAccessor, content: (assertRecipient: () => void) => Promise<string | undefined>, recipient?: ComposerResourceTarget | null): Promise<void> {
  const editors = accessor.get(IEditorService), quick = accessor.get(IQuickInputService);
  const active = editors.activeEditorPane;
  const targets = editors.visibleEditorPanes.filter(pane => pane instanceof ThreadPane)
    .flatMap(pane => { const target = pane.threadTarget; return target ? [{ pane, target }] : []; });
  if (!targets.length) throw new Error(t('workbench.composer.openRecipientFirst'));
  const originating = recipient === undefined ? (active instanceof ThreadPane ? targets.find(item => item.pane === active) : undefined) : recipient;
  if (recipient === undefined && active instanceof ThreadPane && !originating) throw new Error(t('workbench.composer.originNotReady'));
  const gatewayId = workspaceContext.getSnapshot().gatewayId;
  if (!gatewayId) throw new Error(t('workbench.composer.gatewayMissing'));
  let invalidated = false;
  const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
  const releaseGateway = workspaceContext.subscribe(() => {
    if (workspaceContext.getSnapshot().gatewayId !== gatewayId) invalidated = true;
  });
  try {
    const selected = originating ?? await quick.pick(targets.map(item => ({ ...item, label: item.target.input.owner.title,
        description: `${item.target.input.owner.gatewayId} · ${item.target.input.owner.workspaceId}`,
        detail: t('workbench.composer.recipientDetail', item.target.input.owner.sessionId, item.target.input.owner.threadId, item.pane.group.id) })),
      { title: t('workbench.composer.chooseRecipient'), placeHolder: t('workbench.composer.chooseRecipientHint') });
    if (!selected) return;
    const target = selected.target;
    if (!target) throw new Error(t('workbench.composer.originNotReady'));
    const assertRecipient = () => {
      if (invalidated || workspaceContext.getSnapshot().gatewayId !== gatewayId) throw new Error(t('workbench.composer.accessChanged'));
      if (selected.pane.threadTarget?.conversation !== target.conversation) throw new Error(t('workbench.composer.recipientChanged'));
    };
    assertRecipient();
    const draft = target.conversation.captureDraft();
    const text = await content(assertRecipient);
    if (text === undefined) return;
    assertRecipient();
    await draft.append(text);
  } finally { release(); releaseGateway(); }
}

/** 全部来源先确认能力，再逐项解析；任何失败均不追加部分草稿。 */
async function selectedReferences(selected: IQuickPickItem[] | undefined, assertRecipient: () => void): Promise<string | undefined> {
  if (!selected?.length) return;
  const sources = selected.map(pick => {
    const source = pick as IQuickPickItem & { resource?: URI; resourceOwner?: ResourceReferenceOwner };
    if (!URI.isUri(source.resource) || !(supportsChatResourceReference(source.resource) || source.resource.scheme === 'boxteam' && source.resourceOwner)) {
      throw new Error(t('workbench.composer.sourceReferenceUnavailable'));
    }
    return { resource: source.resource, owner: source.resourceOwner };
  });
  const references: string[] = [];
  for (const source of sources) {
    assertRecipient();
    references.push(await resourceReference(source.resource, source.owner, 'chat'));
  }
  return references.join('\n');
}

registerAction2(class AttachResourceReference extends Action2 {
  constructor() {
    super({ id: ATTACH_RESOURCE_URI, title: { value: t('workbench.resource.attachToChat'), original: actionTitles['workbench.resource.attachToChat'] },
      f1: true, precondition: ContextKeyExpr.or(referenceContext, ContextKeyExpr.equals('activeEditor', ThreadPane.ID)),
      menu: [{ id: MenuId.EditorTitleContext, group: '2_copy', when: referenceContext },
        { id: MenuId.EditorContext, group: '9_cutcopypaste', when: referenceContext }] });
  }
  async run(accessor: ServicesAccessor, supplied?: URI, sourceOwner?: ResourceReferenceOwner, recipient?: ComposerResourceTarget | null): Promise<void> {
    if (supplied !== undefined && !URI.isUri(supplied)) throw new Error(t('workbench.composer.resourceUriRequired'));
    const editors = accessor.get(IEditorService), quick = accessor.get(IQuickInputService);
    const active = editors.activeEditor;
    const resource = supplied ?? EditorResourceAccessor.getCanonicalUri(active, { supportSideBySide: SideBySideEditor.PRIMARY });
    if (resource && (supportsChatResourceReference(resource) || resource.scheme === 'boxteam' && sourceOwner) && (supplied || !(active instanceof ThreadInput))) {
      await appendToThread(accessor, () => resourceReference(resource, sourceOwner, 'chat'), recipient);
      return;
    }
    if (supplied || !(active instanceof ThreadInput)) throw new Error(t('workbench.composer.referenceUnavailable'));
    const providerOptions: SearchInvocationOptions & IAnythingQuickAccessProviderRunOptions = { searchOwner: { gatewayId: active.owner.gatewayId, workspaceId: active.owner.workspaceId },
      filesOnly: true, includeOpenToSideButton: false, placeholder: t('workbench.composer.chooseFiles') };
    await appendToThread(accessor, async assertRecipient => {
      const selected = await quick.quickAccess.pick('', { preserveValue: true, canPickMany: true, enabledProviderPrefixes: [''], providerOptions });
      return selectedReferences(selected, assertRecipient);
    });
  }
});

registerAction2(class AttachSourceReferences extends Action2 {
  constructor() {
    super({ id: ATTACH_SOURCE_URIS, title: { value: t('workbench.resource.selectSourcesForChat'), original: actionTitles['workbench.resource.selectSourcesForChat'] },
      f1: true, precondition: ContextKeyExpr.equals('activeEditor', ThreadPane.ID) });
  }
  async run(accessor: ServicesAccessor): Promise<void> {
    const input = accessor.get(IEditorService).activeEditor;
    if (!(input instanceof ThreadInput)) throw new Error(t('workbench.composer.selectSourcesFromThread'));
    const quick = accessor.get(IQuickInputService);
    await appendToThread(accessor, async assertRecipient => {
      let owners = [{ gatewayId: input.owner.gatewayId, workspaceId: input.owner.workspaceId }];
      while (true) {
        const selectedOwners = await chooseSearchOwners(quick, owners);
        if (!selectedOwners) return;
        owners = selectedOwners;
        assertRecipient();
        let back = false;
        const providerOptions: ResourceAccessOptions = { searchOwners: owners,
          resourceThread: { owner: { gatewayId: input.owner.gatewayId, workspaceId: input.owner.workspaceId },
            session_id: input.owner.sessionId, thread_id: input.owner.threadId }, resourceReferencesOnly: true,
          resourceReferenceBack: () => { back = true; } };
        const selected = await quick.quickAccess.pick('resource ', {
          preserveValue: true, canPickMany: true, enabledProviderPrefixes: ['resource '], providerOptions,
        });
        assertRecipient();
        if (!back) return selectedReferences(selected, assertRecipient);
      }
    });
  }
});

registerAction2(class PasteSelectedContent extends Action2 {
  constructor() {
    const context = ContextKeyExpr.and(contentContext, ContextKeyExpr.has('editorHasSelection'));
    super({ id: PASTE_SELECTED_CONTENT, title: { value: t('workbench.resource.pasteSelectionToChat'), original: actionTitles['workbench.resource.pasteSelectionToChat'] },
      f1: true, precondition: context, menu: { id: MenuId.EditorContext, group: '9_cutcopypaste', when: context } });
  }
  async run(accessor: ServicesAccessor): Promise<void> {
    const control = accessor.get(IEditorService).activeTextEditorControl;
    const editor = isDiffEditor(control)
      ? (control.getOriginalEditor().hasTextFocus() ? control.getOriginalEditor() : control.getModifiedEditor())
      : getCodeEditor(control);
    const model = editor?.getModel(), selection = editor?.getSelection();
    if (!model || !supportsResourceReference(model.uri) || !selection || selection.isEmpty()) throw new Error(t('workbench.composer.selectContentFirst'));
    // 在任何 Quick Input 及异步步骤前取得实际正文；来源后来变化不替换这份选择。
    const text = model.getValueInRange(selection);
    await appendToThread(accessor, async () => text);
  }
});
