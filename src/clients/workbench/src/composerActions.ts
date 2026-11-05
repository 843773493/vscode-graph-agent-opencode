import { URI } from '../upstream/src/vs/base/common/uri.js';
import { getCodeEditor } from '../upstream/src/vs/editor/browser/editorBrowser.js';
import { Action2, MenuId, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { ContextKeyExpr } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import type { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { EditorResourceAccessor, SideBySideEditor } from '../upstream/src/vs/workbench/common/editor.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import type { IAnythingQuickAccessProviderRunOptions } from '../upstream/src/vs/workbench/contrib/search/common/search.js';
import { onGatewayUserSessionInvalidated } from '../../web/src/api/http.js';
import { ATTACH_RESOURCE_URI, PASTE_SELECTED_CONTENT, resourceReference, supportsResourceReference } from './resourceActions.js';
import { ThreadPane } from './threadPane.js';
import { ThreadInput } from './threadInput.js';
import type { SearchInvocationOptions } from './search/target.js';
import { workspaceContext } from './workspaceContext.js';

const resourceContext = ContextKeyExpr.or(ContextKeyExpr.equals('resourceScheme', 'boxteam-file'), ContextKeyExpr.equals('resourceScheme', 'boxteam-source'));

/** 接收者来自发起编辑器，或明确选择的可见 Thread；后续焦点不参与归属。 */
async function appendToThread(accessor: ServicesAccessor, content: () => Promise<string | undefined>): Promise<void> {
  const editors = accessor.get(IEditorService), quick = accessor.get(IQuickInputService);
  const active = editors.activeEditorPane;
  const targets = editors.visibleEditorPanes.filter(pane => pane instanceof ThreadPane)
    .flatMap(pane => { const target = pane.threadTarget; return target ? [{ pane, target }] : []; });
  if (!targets.length) throw new Error('请先打开接收资源的 Thread');
  const originating = active instanceof ThreadPane ? targets.find(item => item.pane === active) : undefined;
  if (active instanceof ThreadPane && !originating) throw new Error('发起 Thread 尚未就绪');
  const gatewayId = workspaceContext.getSnapshot().gatewayId;
  if (!gatewayId) throw new Error('尚未取得发起 Gateway 身份');
  let invalidated = false;
  const release = onGatewayUserSessionInvalidated(0, () => { invalidated = true; });
  try {
    const selected = originating ?? await quick.pick(targets.map(item => ({ ...item, label: item.target.input.owner.title,
        description: `${item.target.input.owner.gatewayId} · ${item.target.input.owner.workspaceId}`,
        detail: `${item.target.input.owner.sessionId} / ${item.target.input.owner.threadId} · 编辑器组 ${item.pane.group.id}` })),
      { title: '选择接收草稿的 Thread', placeHolder: '只加入所选草稿，不发送消息或启动执行' });
    if (!selected) return;
    const target = selected.target;
    if (selected.pane.threadTarget?.conversation !== target.conversation) throw new Error('接收 Thread 已关闭或改变，请重新选择');
    const draft = target.conversation.captureDraft();
    const text = await content();
    if (text === undefined) return;
    if (invalidated || workspaceContext.getSnapshot().gatewayId !== gatewayId) throw new Error('Gateway 或用户身份已改变，请重新选择来源与接收者');
    if (selected.pane.threadTarget?.conversation !== target.conversation) throw new Error('接收 Thread 已关闭或改变，请重新选择');
    await draft.append(text);
  } finally { release(); }
}

registerAction2(class AttachResourceReference extends Action2 {
  constructor() {
    super({ id: ATTACH_RESOURCE_URI, title: { value: '将资源 URI 加入聊天草稿…', original: 'Attach Resource URI to Chat…' },
      f1: true, precondition: ContextKeyExpr.or(resourceContext, ContextKeyExpr.equals('activeEditor', ThreadPane.ID)),
      menu: [{ id: MenuId.EditorTitleContext, group: '2_copy', when: resourceContext },
        { id: MenuId.EditorContext, group: '9_cutcopypaste', when: resourceContext }] });
  }
  async run(accessor: ServicesAccessor, supplied?: URI): Promise<void> {
    if (supplied !== undefined && !URI.isUri(supplied)) throw new Error('引用需要明确的资源 URI');
    const editors = accessor.get(IEditorService), quick = accessor.get(IQuickInputService);
    const active = editors.activeEditor;
    const resource = supplied ?? EditorResourceAccessor.getCanonicalUri(active, { supportSideBySide: SideBySideEditor.PRIMARY });
    if (resource && supportsResourceReference(resource)) {
      await appendToThread(accessor, () => resourceReference(resource));
      return;
    }
    if (supplied || !(active instanceof ThreadInput)) throw new Error('此资源尚未提供规范引用能力');
    const providerOptions: SearchInvocationOptions & IAnythingQuickAccessProviderRunOptions = { searchOwner: { gatewayId: active.owner.gatewayId, workspaceId: active.owner.workspaceId },
      filesOnly: true, includeOpenToSideButton: false, placeholder: '选择要加入发起 Thread 草稿的文件 · 不打开文件' };
    await appendToThread(accessor, async () => {
      const selected = await quick.quickAccess.pick('', { preserveValue: true, canPickMany: true, enabledProviderPrefixes: [''], providerOptions });
      if (!selected?.length) return;
      const sources = selected.map(pick => {
        const source = 'resource' in pick && URI.isUri(pick.resource) ? pick.resource : undefined;
        if (!source || !supportsResourceReference(source)) throw new Error('所选文件没有可用的规范资源引用');
        return source;
      });
      const references: string[] = [];
      for (const source of sources) references.push(await resourceReference(source));
      return references.join('\n');
    });
  }
});

registerAction2(class PasteSelectedContent extends Action2 {
  constructor() {
    const context = ContextKeyExpr.and(resourceContext, ContextKeyExpr.has('editorHasSelection'));
    super({ id: PASTE_SELECTED_CONTENT, title: { value: '将选定正文粘贴到聊天草稿…', original: 'Paste Selected Content to Chat…' },
      f1: true, precondition: context, menu: { id: MenuId.EditorContext, group: '9_cutcopypaste', when: context } });
  }
  async run(accessor: ServicesAccessor): Promise<void> {
    const editor = getCodeEditor(accessor.get(IEditorService).activeTextEditorControl);
    const model = editor?.getModel(), selection = editor?.getSelection();
    if (!model || !supportsResourceReference(model.uri) || !selection || selection.isEmpty()) throw new Error('请先在支持的资源编辑器中选择正文');
    // 在任何 Quick Input 及异步步骤前取得实际正文；来源后来变化不替换这份选择。
    const text = model.getValueInRange(selection);
    await appendToThread(accessor, async () => text);
  }
});
