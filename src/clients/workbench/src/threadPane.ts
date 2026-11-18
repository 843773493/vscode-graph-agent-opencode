import { EditorPane } from '../upstream/src/vs/workbench/browser/parts/editor/editorPane.js';
import { t } from '../../shared/core/i18n/index.js';
import { getSession, listChildThreads } from '../../web/src/api.js';
import { IQuickInputService, type IQuickPickItem } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';
import { onGatewayTokenInvalidated } from '../../web/src/api/http.js';
import { IEditorGroup, IEditorGroupsService } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { IContextMenuService } from '../upstream/src/vs/platform/contextview/browser/contextView.js';
import { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { ITelemetryService } from '../upstream/src/vs/platform/telemetry/common/telemetry.js';
import { IThemeService } from '../upstream/src/vs/platform/theme/common/themeService.js';
import { IStorageService } from '../upstream/src/vs/platform/storage/common/storage.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { ICommandService } from '../upstream/src/vs/platform/commands/common/commands.js';
import { Dimension } from '../upstream/src/vs/base/browser/dom.js';
import { toAction } from '../upstream/src/vs/base/common/actions.js';
import { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';
import { EditorInput } from '../upstream/src/vs/workbench/common/editor/editorInput.js';
import { IEditorOptions } from '../upstream/src/vs/platform/editor/common/editor.js';
import { createEditorOpenError, IEditorOpenContext } from '../upstream/src/vs/workbench/common/editor.js';
import { ThreadInput, UnavailableThreadInput } from './threadInput.js';
import { ChatSurface } from './react/chatView.js';
import { showFileOpenMenu } from './fileOpenMenu.js';
import './thread.css';
import type { LLMRequestLogMatchDTO, TraceMatchDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import type { GatewayWorkspace, Session } from '../../web/src/types/backend.js';

import type { TurnNavigationTarget } from "../../web/src/api/session/sessionTurnHistory.js";

export interface ThreadEditorOptions extends IEditorOptions {
  boxteamSession?: Session;
  boxteamMessage?: TurnNavigationTarget;
  boxteamRequestLog?: LLMRequestLogMatchDTO;
  boxteamTrace?: TraceMatchDTO;
  boxteamLogRecord?: boolean;
}

/** 原版只管理编辑器生命周期，完整会话产品由既有 React 组件呈现。 */
export class ThreadPane extends EditorPane {
  static readonly ID = 'boxteam.editor.threadPane';
  override get minimumWidth(): number { return 280; }
  override get minimumHeight(): number { return 220; }
  private root!: HTMLElement;
  private conversation?: ChatSurface;
  private current?: ThreadInput;
  get threadTarget(): { input: ThreadInput; conversation: ChatSurface } | undefined {
    return this.current && this.conversation ? { input: this.current, conversation: this.conversation } : undefined;
  }

  constructor(group: IEditorGroup,
    @ITelemetryService telemetry: ITelemetryService,
    @IThemeService themes: IThemeService,
    @IStorageService storage: IStorageService,
    @IEditorService private readonly editors: IEditorService,
    @IContextMenuService private readonly menus: IContextMenuService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService,
    @IInstantiationService private readonly instantiation: IInstantiationService,
    @INotificationService private readonly notifications: INotificationService,
    @ICommandService private readonly commands: ICommandService,
    @IQuickInputService private readonly quickInput: IQuickInputService,
  ) { super(ThreadPane.ID, group, telemetry, themes, storage); }

  protected override createEditor(parent: HTMLElement): void {
    this.root = document.createElement('section'); this.root.className = 'boxteam-thread';
    parent.append(this.root);
  }
  override async setInput(input: EditorInput, options: ThreadEditorOptions | undefined, context: IEditorOpenContext, token: CancellationToken): Promise<void> {
    this.conversation?.dispose(); this.conversation = undefined; this.current = undefined;
    await super.setInput(input, options, context, token);
    if (token.isCancellationRequested || this.input !== input) return;
    if (input instanceof UnavailableThreadInput) throw createEditorOpenError(input.reason, [toAction({
      id: 'boxteam.thread.removeUnavailableRecord', label: t('thread.restore.removeRecord'), run: () => this.group.closeEditor(input),
    })], { forceMessage: true });
    if (!(input instanceof ThreadInput)) throw new Error(t('thread.restore.invalidInput'));
    let workspaces: GatewayWorkspace[];
    try { workspaces = (await input.resolveOwner()).workspaces; }
    catch (error) { throw createEditorOpenError(error instanceof Error ? error : String(error), [], { forceMessage: true }); }
    if (token.isCancellationRequested || this.input !== input) return;
    this.current = input;
    if (options?.viewState) input.restoreViewState({ ...input.getViewState(), ...options.viewState });
    this.root.dataset.threadId = input.owner.threadId; this.root.dataset.sessionId = input.owner.sessionId;
    this.root.dataset.workspaceId = input.owner.workspaceId;
    this.conversation = new ChatSurface(this.root, input, this.editors, this.isVisible(), (path, anchor) => showFileOpenMenu(path, anchor, input.owner,
      this.menus, this.editors, this.groups, this.instantiation, this.group), this.notifications, this.commands, workspaces, this.instantiation, this.group.id,
      () => this.chooseThread(input), options?.boxteamSession);
    if (options?.boxteamMessage) this.conversation.revealMessage(options.boxteamMessage);
    if (options?.boxteamRequestLog) this.conversation.revealRequestLog(options.boxteamRequestLog, options.boxteamLogRecord);
    if (options?.boxteamTrace) this.conversation.revealTrace(options.boxteamTrace, options.boxteamLogRecord);
  }
  private async chooseThread(input: ThreadInput): Promise<void> {
    const owner = { ...input.owner };
    type ThreadPick = IQuickPickItem & { threadId: string; title: string };
    const items = Promise.all([getSession(0, owner.sessionId, owner.workspaceKey), listChildThreads(0, owner.sessionId, owner.workspaceKey)])
      .then(([session, children]): ThreadPick[] => {
        if (!session.thread_id || session.session_id !== owner.sessionId || session.workspace_id !== owner.workspaceId) {
          throw new Error(t('workbench.session.mainThreadOwnerMismatch'));
        }
        return [
        { label: t('child.main'), description: session.title, detail: session.thread_id ?? undefined,
          threadId: session.thread_id, title: session.title, picked: owner.threadId === session.thread_id },
        ...children.items.map(thread => ({ label: thread.title ?? thread.thread_id, description: thread.subagent_type ?? thread.role ?? '',
          detail: thread.thread_id, threadId: thread.thread_id, title: thread.title ?? thread.thread_id, picked: owner.threadId === thread.thread_id })),
      ]; });
    // setInput 的令牌在加载结束后就被上游取消；选择器必须拥有自己的生命周期。
    const cancellation = new CancellationTokenSource();
    const editorListener = this.editors.onDidActiveEditorChange(() => cancellation.cancel());
    const releaseCredential = onGatewayTokenInvalidated(0, () => cancellation.cancel());
    try {
      const selected = await this.quickInput.pick(items, { title: t('thread.host.selectThread'), matchOnDescription: true,
        matchOnDetail: true, placeHolder: t('thread.host.selectThreadHint') }, cancellation.token);
      if (!selected || cancellation.token.isCancellationRequested || this.current !== input) return;
      await this.editors.openEditor(new ThreadInput({ ...owner, threadId: selected.threadId, title: selected.title }), { pinned: true }, this.group);
    } finally { editorListener.dispose(); releaseCredential(); cancellation.dispose(); }
  }
  override setOptions(options: ThreadEditorOptions | undefined): void {
    super.setOptions(options);
    if (!this.current) return;
    if (options?.viewState) {
      this.current.restoreViewState({ ...this.current.getViewState(), ...options.viewState });
      this.conversation?.update();
    }
    if (options?.boxteamMessage) this.conversation?.revealMessage(options.boxteamMessage);
    if (options?.boxteamRequestLog) this.conversation?.revealRequestLog(options.boxteamRequestLog, options.boxteamLogRecord);
    if (options?.boxteamTrace) this.conversation?.revealTrace(options.boxteamTrace, options.boxteamLogRecord);
  }
  protected override setEditorVisible(visible: boolean): void {
    super.setEditorVisible(visible); this.conversation?.setVisible(visible);
  }
  override clearInput(): void {
    this.conversation?.dispose(); this.conversation = undefined; this.current = undefined;
    if (this.root) {
      delete this.root.dataset.workspaceId;
      delete this.root.dataset.sessionId;
      delete this.root.dataset.threadId;
    }
    super.clearInput();
  }
  override layout(dimension: Dimension): void { this.root.style.width = `${dimension.width}px`; this.root.style.height = `${dimension.height}px`; }
  override focus(): void { this.conversation?.focus(); }
  override dispose(): void { this.clearInput(); super.dispose(); }
}
