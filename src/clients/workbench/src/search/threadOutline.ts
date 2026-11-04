import { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { Emitter } from '../../upstream/src/vs/base/common/event.js';
import { escapeIcons } from '../../upstream/src/vs/base/common/iconLabels.js';
import { Disposable, toDisposable, type IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { ITreeRenderer } from '../../upstream/src/vs/base/browser/ui/tree/tree.js';
import type { FuzzyScore } from '../../upstream/src/vs/base/common/filters.js';
import type { IEditorPane } from '../../upstream/src/vs/workbench/common/editor.js';
import { IOutlineService, type IOutline, type IOutlineCreator, type IOutlineListConfig, type OutlineChangeEvent, type OutlineTarget } from '../../upstream/src/vs/workbench/services/outline/browser/outline.js';
import { loadSessionHistory } from '../../../web/src/api/session/sessionTurnHistory.js';
import { ThreadPane } from '../threadPane.js';

interface ThreadOutlineEntry {
  readonly turnId?: string;
  readonly ordinal: number;
  readonly label: string;
}

const PAGE_TURNS = 20;

/** 只保留本次大纲的有界标题引用；正文与定位仍交原历史 owner。 */
class ThreadOutline extends Disposable implements IOutline<ThreadOutlineEntry> {
  readonly outlineKind = 'boxteam-thread';
  private readonly changed = this._register(new Emitter<OutlineChangeEvent>());
  readonly onDidChange = this.changed.event;
  private readonly controller = new AbortController();
  private entries: ThreadOutlineEntry[] = [];
  private cursor?: string;
  private hasBefore = false;
  private loading = false;
  private error?: string;
  private restart = false;
  private readonly continuation: ThreadOutlineEntry = { ordinal: -1, label: '加载更早轮次' };
  private readonly target: NonNullable<ThreadPane['outlineTarget']>;
  readonly config: IOutlineListConfig<ThreadOutlineEntry>;

  constructor(private readonly pane: ThreadPane, token: CancellationToken) {
    super();
    const target = pane.outlineTarget;
    if (!target) throw new Error('Thread 编辑器尚未就绪');
    this.target = target;
    this._register(toDisposable(() => this.controller.abort()));
    this._register(token.onCancellationRequested(() => this.dispose()));
    const compare = (a: ThreadOutlineEntry, b: ThreadOutlineEntry) => a.ordinal - b.ordinal;
    const renderer: ITreeRenderer<ThreadOutlineEntry, FuzzyScore, HTMLElement> = {
      templateId: 'boxteam-thread-outline', renderTemplate: container => container,
      renderElement: (node, _index, container) => { container.textContent = node.element.label; },
      disposeTemplate: () => {},
    };
    this.config = {
      delegate: { getHeight: () => 22, getTemplateId: () => renderer.templateId }, renderers: [renderer],
      comparator: { compareByPosition: compare, compareByType: compare, compareByName: (a, b) => a.label.localeCompare(b.label) },
      options: { multipleSelectionSupport: false, identityProvider: { getId: entry => entry.turnId ?? 'continue' },
        accessibilityProvider: { getAriaLabel: entry => entry.label, getWidgetAriaLabel: () => 'Thread 轮次大纲' },
        keyboardNavigationLabelProvider: { getKeyboardNavigationLabel: entry => entry.label } },
      treeDataSource: { getChildren: element => element === this ? this.visibleEntries : [] },
      breadcrumbsDataSource: { getBreadcrumbElements: () => [] },
      quickPickDataSource: { getQuickPickElements: () => this.visibleEntries.map(entry => ({
        element: entry, label: entry.turnId ? `$(comment-discussion) ${escapeIcons(entry.label)}`
          : `$(history) ${this.error ? '读取大纲失败，重新读取' : this.loading ? '正在读取更早轮次…' : '加载更早轮次'}`,
        description: [this.coverage, !entry.turnId ? this.error : undefined].filter(Boolean).join(' · '),
        ariaLabel: entry.label, acceptInPlace: !entry.turnId,
      })) },
    };
  }
  get uri() { return this.target.input.resource; }
  get isEmpty() { return this.visibleEntries.length === 0; }
  get activeElement() { return undefined; }
  private get visibleEntries() { return this.hasBefore || this.error ? [...this.entries, this.continuation] : this.entries; }
  private get coverage() {
    return `已载入 ${this.entries.length} 轮${this.error ? '，历史读取失败' : this.hasBefore ? '，更早历史未载入' : '，本次历史已覆盖'}`;
  }
  private isTarget() { return this.pane.outlineTarget?.conversation === this.target.conversation; }
  private isCurrent() { return !this.controller.signal.aborted && this.isTarget(); }

  async load(): Promise<void> {
    if (this.loading || !this.isCurrent()) return;
    this.loading = true;
    this.error = undefined;
    this.changed.fire({});
    try {
      const { owner } = this.target.input;
      const page = await loadSessionHistory(0, owner.sessionId, {
        direction: this.cursor && !this.restart ? 'before' : 'tail',
        cursor: this.restart ? undefined : this.cursor, turns: PAGE_TURNS, include: ['user', 'metadata'],
      }, owner.workspaceKey, this.controller.signal, owner.threadId);
      if (!this.isCurrent()) return;
      if (page.thread_id !== owner.threadId) throw new Error('Thread 大纲历史归属不一致');
      if (this.restart) this.entries = [];
      const existing = new Set(this.entries.map(entry => entry.turnId));
      for (const turn of page.summaries ?? []) {
        if (existing.has(turn.turn_id)) continue;
        const title = (turn.user_messages ?? []).map(message => message.preview ?? '').join(' ').replace(/\s+/g, ' ').trim();
        this.entries.push({ turnId: turn.turn_id, ordinal: turn.ordinal, label: `第 ${turn.ordinal} 轮 · ${title || '无文本消息'}` });
      }
      this.entries.sort((a, b) => a.ordinal - b.ordinal);
      this.cursor = page.before_cursor ?? undefined;
      this.hasBefore = page.has_before ?? false;
      if (this.hasBefore && !this.cursor) throw new Error('Thread 大纲缺少更早历史续点');
      this.restart = false;
    } catch (error) {
      if (this.controller.signal.aborted) return;
      this.error = error instanceof Error ? error.message : String(error);
      this.restart = true;
    } finally {
      this.loading = false;
      if (this.isCurrent()) this.changed.fire({});
    }
  }
  reveal(entry: ThreadOutlineEntry): void {
    // 原 Provider 先关闭浮层并释放大纲，再调用已接受动作；只读请求取消不撤回该动作。
    if (!this.isTarget()) return;
    if (!entry.turnId) { void this.load(); return; }
    this.target.conversation.revealTurn(entry.turnId);
  }
  preview(entry: ThreadOutlineEntry): IDisposable {
    return this.isCurrent() && entry.turnId ? this.target.conversation.revealTurn(entry.turnId, true) : Disposable.None;
  }
  captureViewState(): IDisposable { return this.target.conversation.captureViewState(); }
}

export class ThreadOutlineCreator extends Disposable implements IOutlineCreator<ThreadPane, ThreadOutlineEntry> {
  constructor(@IOutlineService outline: IOutlineService) { super(); this._register(outline.registerOutlineCreator(this)); }
  matches(candidate: IEditorPane): candidate is ThreadPane { return candidate instanceof ThreadPane; }
  async createOutline(editor: ThreadPane, _target: OutlineTarget, token: CancellationToken): Promise<IOutline<ThreadOutlineEntry> | undefined> {
    const target = editor.outlineTarget;
    if (!target || token.isCancellationRequested) return undefined;
    const outline = new ThreadOutline(editor, token);
    await outline.load();
    if (token.isCancellationRequested || editor.outlineTarget?.conversation !== target.conversation) { outline.dispose(); return undefined; }
    return outline;
  }
}
