import { browserViewKey, readBrowserView, updateBrowserViewCache } from "./browserViewStore";
import type { SessionTurnTimeline } from "./session/turnTimeline";

export interface ThreadHistoryOwner {
  gatewayId: string;
  workspaceId: string;
  sessionId: string;
  threadId: string;
}

interface HistorySnapshot {
  version: 1;
  owner: ThreadHistoryOwner;
  timeline: SessionTurnTimeline;
  savedAt: number;
  bytes: number;
}
interface CacheWriteFailure { error: unknown }
const MAX_ENTRIES = 64;
const MAX_BYTES = 16 * 1024 * 1024;

function snapshotKey(owner: ThreadHistoryOwner): string {
  return browserViewKey(owner.gatewayId, "thread-history", owner.workspaceId, owner.sessionId, owner.threadId);
}
function indexKey(owner: ThreadHistoryOwner): string {
  return browserViewKey(owner.gatewayId, "thread-history-v1");
}
function isOlder(left: SessionTurnTimeline, right: SessionTurnTimeline): boolean {
  if (left.projectionEpoch !== right.projectionEpoch) return (left.projectionEpoch ?? -1) < (right.projectionEpoch ?? -1);
  return (left.historyViewRevision ?? -1) < (right.historyViewRevision ?? -1);
}

/** 与 EditorPane 生命周期分离的历史镜像；正文只保存服务端已经物化的窗口。 */
export class ThreadHistoryCache {
  private readonly memory = new Map<string, HistorySnapshot>();
  private readonly pending = new Map<string, { snapshot: HistorySnapshot; timer: ReturnType<typeof setTimeout>; onError: (error: unknown) => void }>();
  private readonly writes = new Set<Promise<CacheWriteFailure | undefined>>();

  async read(owner: ThreadHistoryOwner, scopeKey: string): Promise<SessionTurnTimeline | null> {
    const key = snapshotKey(owner);
    const cached = this.memory.get(key) ?? await readBrowserView<HistorySnapshot>(key);
    // 读取数据库期间可能已有更晚的后端快照到达，不能用迟到读取覆盖它。
    const snapshot = this.memory.get(key) ?? cached;
    if (!snapshot) return null;
    if (snapshot.version !== 1 || snapshotKey(snapshot.owner) !== key || snapshot.timeline.threadId !== owner.threadId) {
      throw new Error("浏览器历史缓存版本或 Thread 归属不一致");
    }
    this.rememberSnapshot(key, snapshot);
    return { ...snapshot.timeline, scopeKey, restoredFromCache: true };
  }

  remember(owner: ThreadHistoryOwner, timeline: SessionTurnTimeline, onError: (error: unknown) => void): void {
    if (timeline.phase !== "ready" || timeline.projectionState !== "ready" || timeline.restoredFromCache || timeline.error) return;
    if (timeline.threadId !== owner.threadId) throw new Error("保存历史缓存时 Thread 归属不一致");
    const key = snapshotKey(owner);
    const previous = this.memory.get(key);
    if (previous && isOlder(timeline, previous.timeline)) return;
    if (previous && previous.timeline.turnsById === timeline.turnsById
      && previous.timeline.internalExecutions === timeline.internalExecutions
      && previous.timeline.beforeCursor === timeline.beforeCursor && previous.timeline.afterCursor === timeline.afterCursor
      && previous.timeline.historyViewRevision === timeline.historyViewRevision && previous.timeline.projectionEpoch === timeline.projectionEpoch) return;
    const snapshot: HistorySnapshot = { version: 1, owner, savedAt: Date.now(), bytes: 0,
      timeline: { ...timeline, phase: "bootstrapping", restoredFromCache: true, generation: 0,
        activeJobs: [], eventCursor: null, loadingBefore: false, loadingAfter: false, loadingDetailIds: [], error: null } };
    snapshot.bytes = new TextEncoder().encode(JSON.stringify(snapshot)).byteLength;
    this.rememberSnapshot(key, snapshot);
    const pending = this.pending.get(key);
    if (pending) clearTimeout(pending.timer);
    this.pending.set(key, { snapshot, onError, timer: setTimeout(() => { void this.flush(owner); }, 500) });
  }

  /** Tab 释放及 pagehide 时提交；不依赖卸载时仍能发起网络请求。 */
  async flush(owner: ThreadHistoryOwner): Promise<void> {
    // 普通页面生命周期由 onError 展示失败；原生退出通过 flushAll 等待并阻止错误退出。
    await this.writePending(snapshotKey(owner));
  }

  async flushAll(): Promise<void> {
    while (this.pending.size || this.writes.size) {
      const writes = [...this.pending.keys()].map(key => this.writePending(key));
      const results = await Promise.allSettled([...writes, ...this.writes]);
      const errors = results.flatMap(result => result.status === 'rejected' ? [result.reason] : result.value ? [result.value.error] : []);
      if (errors.length) throw new AggregateError(errors, '会话历史缓存尚未保存，请重试关闭窗口');
    }
  }

  private writePending(key: string): Promise<CacheWriteFailure | undefined> {
    const pending = this.pending.get(key);
    if (!pending) return Promise.resolve(undefined);
    clearTimeout(pending.timer);
    this.pending.delete(key);
    let write!: Promise<CacheWriteFailure | undefined>;
    write = (async () => {
      try {
        await updateBrowserViewCache<HistorySnapshot>(key, indexKey(pending.snapshot.owner), previous => {
          if (previous && (isOlder(pending.snapshot.timeline, previous.timeline)
            || (!isOlder(previous.timeline, pending.snapshot.timeline) && previous.savedAt > pending.snapshot.savedAt))) return previous;
          return pending.snapshot;
        }, { entries: MAX_ENTRIES, bytes: MAX_BYTES });
      } catch (error) {
        // 保留失败快照，允许用户在原生窗口中重试关闭，而不是把失败伪装成已持久化。
        if (this.memory.get(key) === pending.snapshot && !this.pending.has(key)) this.pending.set(key, pending);
        console.error("保存会话历史缓存失败", error);
        pending.onError(error);
        return { error };
      } finally {
        this.writes.delete(write);
      }
    })();
    this.writes.add(write);
    return write;
  }

  async remove(owner: ThreadHistoryOwner): Promise<void> {
    const key = snapshotKey(owner);
    const pending = this.pending.get(key);
    if (pending) clearTimeout(pending.timer);
    this.pending.delete(key);
    this.memory.delete(key);
    await updateBrowserViewCache<HistorySnapshot>(key, indexKey(owner), () => undefined, { entries: MAX_ENTRIES, bytes: MAX_BYTES });
  }

  private rememberSnapshot(key: string, snapshot: HistorySnapshot): void {
    this.memory.delete(key);
    this.memory.set(key, snapshot);
    let bytes = [...this.memory.values()].reduce((sum, entry) => sum + entry.bytes, 0);
    for (const [entryKey, entry] of this.memory) {
      if (this.memory.size <= MAX_ENTRIES && bytes <= MAX_BYTES) break;
      this.memory.delete(entryKey);
      bytes -= entry.bytes;
    }
  }
}

export const threadHistoryCache = new ThreadHistoryCache();
