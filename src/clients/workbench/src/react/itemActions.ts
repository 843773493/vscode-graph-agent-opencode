import { t } from '../../../shared/core/i18n/index.js';
import type { ChatItemMenuAction, ChatItemMenuRequest, ChatItemTarget } from '../../../web/src/components/chat/itemContextMenu.js';
import type { ThreadItemInspectionDTO, ThreadItemInspectionRequest } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { ThreadOwner } from '../agentApi.js';
import { gateway } from '../agentApi.js';
import { workspaceContext } from '../workspaceContext.js';
import { contextContentUri, itemContentUri } from '../itemDocuments.js';
import { resourceReference } from '../resourceActions.js';
import { ThreadInput } from '../threadInput.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import type { ResourceOpenOptions } from '../resourceOpener.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IClipboardService } from '../../upstream/src/vs/platform/clipboard/common/clipboardService.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IEditorGroupsService } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { IOpenerService } from '../../upstream/src/vs/platform/opener/common/opener.js';
import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';

export function inspectItem(owner: ThreadOwner, request: ThreadItemInspectionRequest, signal?: AbortSignal): Promise<ThreadItemInspectionDTO> {
  return gateway.request(`/api/v1/sessions/${encodeURIComponent(owner.sessionId)}/threads/${encodeURIComponent(owner.threadId)}/items/inspect`,
    { method: 'POST', signal, body: JSON.stringify(request) }, owner.workspaceKey);
}

/** 只在菜单接受后查询；请求／Trace 交回发起 Tab 原有定位链路。 */
export class ChatItemActions extends Disposable {
  constructor(
    @IQuickInputService private readonly quick: IQuickInputService,
    @IClipboardService private readonly clipboard: IClipboardService,
    @IEditorService private readonly editors: IEditorService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService,
    @IOpenerService private readonly opener: IOpenerService,
  ) { super(); }

  private async run(groupId: number, action: (signal: AbortSignal, check: () => void) => Promise<void>): Promise<void> {
    const gatewayId = workspaceContext.getSnapshot().gatewayId, controller = new AbortController();
    const invalidate = () => controller.abort(new Error(t('quickAccess.resource.identityChanged')));
    const releaseToken = onGatewayTokenInvalidated(0, invalidate);
    const releaseGateway = workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== gatewayId) invalidate(); });
    const check = () => {
      controller.signal.throwIfAborted();
      if (this._store.isDisposed) throw new Error(t('chat.itemMenu.sourceGone'));
      if (!this.groups.getGroup(groupId)) throw new Error(t('quickAccess.resource.invokingGroupClosed'));
    };
    try { check(); await action(controller.signal, check); }
    finally { releaseToken(); releaseGateway(); }
  }

  private async selectContextSource(sources: NonNullable<ChatItemTarget['contextSources']>) {
    return sources.length === 1 ? sources[0] : (await this.quick.pick(sources.map(value => ({
      label: value.name, description: value.reference.contribution_id ?? value.reference.item_id, value,
    })), { placeHolder: t('chat.itemMenu.chooseItem') }))?.value;
  }

  /** 菜单与详情中的全文按钮共用同一打开链路和发起编辑器组。 */
  openContent(owner: ThreadOwner, groupId: number, target: ChatItemTarget): Promise<void> {
    return this.run(groupId, async (signal, check) => {
      if (target.contextSources) {
        const source = await this.selectContextSource(target.contextSources);
        check();
        if (source) await this.editors.openEditor({ resource: contextContentUri(owner, source.reference),
          options: { pinned: true } }, groupId);
        return;
      }
      let itemId = target.itemIds?.at(-1)?.id;
      if (target.itemIds && target.itemIds.length > 1) {
        const pick = await this.quick.pick(target.itemIds.map((item, index) => ({ label: `${index + 1}. ${item.kind}`, description: item.id, item })),
          { placeHolder: t('chat.itemMenu.chooseItem') });
        check();
        if (!pick) return;
        itemId = pick.item.id;
      }
      const result = await inspectItem(owner, { operation: 'content_metadata', item_id: itemId,
        message_id: itemId ? undefined : target.messageId }, signal);
      check();
      const variants = result.contents.flatMap((content, index) => content.uri ? [{ label: content.name, index, preview: false }]
        : content.language === 'markdown' ? [
          { label: t('chat.itemMenu.previewMarkdown'), description: content.name, index, preview: true },
          { label: t('chat.itemMenu.sourceMarkdown'), description: content.name, index, preview: false },
        ] : [{ label: content.name, description: content.language, index, preview: false }]);
      const pick = variants.length === 1 ? variants[0] : await this.quick.pick(variants, { placeHolder: t('chat.itemMenu.chooseContent') });
      check();
      if (!pick) return;
      const content = result.contents[pick.index];
      if (content.uri) {
        const options: ResourceOpenOptions = { openExternal: false, boxteamResourceOwner: owner, boxteamTargetGroup: groupId };
        await this.opener.open(content.uri, options); return;
      }
      const resource = itemContentUri(owner, result.location!.item_id, pick.index, content);
      await this.editors.openEditor({ resource, options: { pinned: true,
        override: content.language === 'markdown' ? pick.preview ? 'vscode.markdown.preview.editor' : 'default' : undefined } }, groupId);
    });
  }

  actions(request: ChatItemMenuRequest, thread: ThreadOwner, groupId: number,
    navigate: (result: ThreadItemInspectionDTO, owner: ThreadOwner) => void): ChatItemMenuAction[] {
    const target = request.target;
    const owner = target ? { ...thread, sessionId: target.sessionId, threadId: target.threadId } : thread;
    const boundary = target?.itemIds?.at(-1)?.id;
    const enabled = Boolean(boundary || target?.messageId);
    const sources = target?.contextSources;
    if (sources) return [
      { id: 'openContextContent', label: t('chat.contextNotice.open'),
        tooltip: t('chat.contextNotice.openHint'), enabled: sources.length > 0,
        run: () => this.openContent(owner, groupId, { contextSources: sources }) },
      { id: 'copyContextContent', label: t('chat.itemMenu.copy'),
        tooltip: t('chat.contextNotice.copyHint'), enabled: sources.length > 0,
        run: () => this.run(groupId, async (signal, check) => {
          const source = await this.selectContextSource(sources);
          check();
          if (!source) return;
          const result = await inspectItem(owner, { operation: 'content', content_ref: source.reference }, signal);
          check();
          await this.clipboard.writeText(result.contents.map(content => content.content ?? '').join('\n\n'));
        }) },
    ];
    const inspect = (operation: ThreadItemInspectionRequest['operation'], signal: AbortSignal, itemId = boundary) => inspectItem(owner,
      { operation, item_id: itemId, message_id: itemId ? undefined : target?.messageId }, signal);
    return [
      { id: 'copyItemUri', label: t('chat.itemMenu.copyUri'), tooltip: t('chat.itemMenu.copyUriHint'), enabled,
        run: () => this.run(groupId, async (signal, check) => {
          const { location } = await inspect('location', signal);
          check();
          const input = new ThreadInput(owner);
          try {
            const uri = await resourceReference(input.resource, undefined, 'copy', location!.item_id);
            check();
            await this.clipboard.writeText(uri);
          } finally { input.dispose(); }
        }) },
      { id: 'openItemContent', label: t('chat.itemMenu.openContent'), tooltip: t('chat.itemMenu.openContentHint'), enabled,
        run: () => this.openContent(owner, groupId, target ?? {}) },
      ...(['request', 'trace'] as const).map(operation => ({ id: `locateItem${operation}`, enabled,
        label: t(operation === 'request' ? 'chat.itemMenu.openRequest' : 'chat.itemMenu.openTrace'),
        tooltip: t(operation === 'request' ? 'chat.itemMenu.openRequestHint' : 'chat.itemMenu.openTraceHint'),
        run: () => this.run(groupId, async (signal, check) => { const result = await inspect(operation, signal); check(); navigate(result, owner); }),
      })),
    ];
  }
}
