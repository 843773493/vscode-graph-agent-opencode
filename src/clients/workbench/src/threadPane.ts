import { EditorPane } from '../upstream/src/vs/workbench/browser/parts/editor/editorPane.js';
import { IEditorGroup, IEditorGroupsService } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { IContextMenuService } from '../upstream/src/vs/platform/contextview/browser/contextView.js';
import { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { ITelemetryService } from '../upstream/src/vs/platform/telemetry/common/telemetry.js';
import { IThemeService } from '../upstream/src/vs/platform/theme/common/themeService.js';
import { IStorageService } from '../upstream/src/vs/platform/storage/common/storage.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { Dimension } from '../upstream/src/vs/base/browser/dom.js';
import { CancellationToken } from '../upstream/src/vs/base/common/cancellation.js';
import { EditorInput } from '../upstream/src/vs/workbench/common/editor/editorInput.js';
import { IEditorOptions } from '../upstream/src/vs/platform/editor/common/editor.js';
import { IEditorOpenContext } from '../upstream/src/vs/workbench/common/editor.js';
import { ThreadInput, UnavailableThreadInput } from './threadInput.js';
import { ChatSurface } from './react/chatView.js';
import { showFileOpenMenu } from './fileOpenMenu.js';
import './thread.css';
import type { TurnMessageMatchDTO, LLMRequestLogMatchDTO, TraceMatchDTO } from '../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';

export interface ThreadEditorOptions extends IEditorOptions {
  boxteamMessage?: TurnMessageMatchDTO;
  boxteamRequestLog?: LLMRequestLogMatchDTO;
  boxteamTrace?: TraceMatchDTO;
}

/** 原版只管理编辑器生命周期，完整会话产品由既有 React 组件呈现。 */
export class ThreadPane extends EditorPane {
  static readonly ID = 'boxteam.editor.threadPane';
  private root!: HTMLElement;
  private content!: HTMLElement;
  private toggle!: HTMLButtonElement;
  private conversation?: ChatSurface;
  private current?: ThreadInput;
  get outlineTarget(): { input: ThreadInput; conversation: ChatSurface } | undefined {
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
  ) { super(ThreadPane.ID, group, telemetry, themes, storage); }

  protected override createEditor(parent: HTMLElement): void {
    this.root = document.createElement('section'); this.root.className = 'boxteam-thread';
    const toolbar = document.createElement('header'); toolbar.className = 'boxteam-thread-toolbar';
    this.toggle = document.createElement('button'); this.toggle.type = 'button';
    this.toggle.className = 'codicon codicon-layout-sidebar-right';
    this.toggle.title = '切换会话资源'; this.toggle.setAttribute('aria-label', '切换会话资源');
    this.toggle.onclick = () => {
      if (!this.current) return;
      this.current.resourcesVisible = !this.current.resourcesVisible;
      this.toggle.setAttribute('aria-expanded', String(this.current.resourcesVisible));
      this.conversation?.update();
    };
    toolbar.append(this.toggle);
    this.content = document.createElement('div'); this.content.className = 'boxteam-thread-body';
    this.root.append(toolbar, this.content); parent.append(this.root);
  }
  override async setInput(input: EditorInput, options: ThreadEditorOptions | undefined, context: IEditorOpenContext, token: CancellationToken): Promise<void> {
    this.conversation?.dispose(); this.conversation = undefined; this.current = undefined;
    await super.setInput(input, options, context, token);
    if (token.isCancellationRequested || this.input !== input) return;
    if (input instanceof UnavailableThreadInput) throw new Error(input.reason);
    if (!(input instanceof ThreadInput)) throw new Error('ThreadPane 只接受 ThreadInput');
    await input.resolveOwner();
    if (token.isCancellationRequested || this.input !== input) return;
    this.current = input;
    if (options?.viewState) input.restoreViewState({ ...input.getViewState(), ...options.viewState });
    this.root.dataset.threadId = input.owner.threadId; this.root.dataset.sessionId = input.owner.sessionId;
    this.root.dataset.workspaceId = input.owner.workspaceId;
    this.toggle.setAttribute('aria-expanded', String(input.resourcesVisible));
    this.conversation = new ChatSurface(this.content, input, this.editors, this.isVisible(), () => {
      this.toggle.setAttribute('aria-expanded', String(input.resourcesVisible));
    }, (path, anchor) => showFileOpenMenu(path, anchor, input.owner,
      this.menus, this.editors, this.groups, this.instantiation, this.group), this.notifications);
    if (options?.boxteamMessage) this.conversation.revealMessage(options.boxteamMessage);
    if (options?.boxteamRequestLog) this.conversation.revealRequestLog(options.boxteamRequestLog);
    if (options?.boxteamTrace) this.conversation.revealTrace(options.boxteamTrace);
  }
  override setOptions(options: ThreadEditorOptions | undefined): void {
    super.setOptions(options);
    if (!this.current) return;
    if (options?.viewState) {
      this.current.restoreViewState({ ...this.current.getViewState(), ...options.viewState });
      this.toggle.setAttribute('aria-expanded', String(this.current.resourcesVisible));
      this.conversation?.update();
    }
    if (options?.boxteamMessage) this.conversation?.revealMessage(options.boxteamMessage);
    if (options?.boxteamRequestLog) this.conversation?.revealRequestLog(options.boxteamRequestLog);
    if (options?.boxteamTrace) this.conversation?.revealTrace(options.boxteamTrace);
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
