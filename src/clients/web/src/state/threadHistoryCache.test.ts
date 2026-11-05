import 'fake-indexeddb/auto';
import { describe, expect, spyOn, test } from 'bun:test';
import { ThreadHistoryCache, type ThreadHistoryOwner } from './threadHistoryCache';
import { applyTurnBootstrap, applyTurnHistoryPage, beginTurnBootstrap, createSessionTurnTimeline, upsertTurns } from './session/turnTimeline';
import { bootstrap, HISTORY_SOURCE_BINDING, SESSION_ID, THREAD_ID, WORKSPACE_ID, turnDetail } from '../hooks/sessionTurnHistory/testFixtures';

const owner = (gatewayId: string): ThreadHistoryOwner => ({ gatewayId, workspaceId: WORKSPACE_ID, sessionId: SESSION_ID, threadId: THREAD_ID });
const failure = (error: unknown) => { throw error; };
function history(scopeKey = 'scope-a') {
  return { ...upsertTurns(createSessionTurnTimeline(scopeKey), [turnDetail(1)]),
    threadId: THREAD_ID, phase: 'ready' as const, projectionEpoch: 1, historyViewRevision: 1,
    beforeCursor: 'real-before-cursor', hasBefore: true, eventCursor: 'old-live-cursor' };
}

describe('独立于 Tab 的会话历史镜像', () => {
  test('释放后可从数据库恢复正文、详情与真实分页窗口，但不能复活实时游标和活动 Job', async () => {
    const target = owner('history-reopen');
    const cache = new ThreadHistoryCache();
    cache.remember(target, history(), failure);
    expect((await cache.read(target, 'new-scope'))?.scopeKey).toBe('new-scope');
    await cache.flush(target);
    const restored = await new ThreadHistoryCache().read(target, 'reloaded-scope');
    expect(restored?.turnsById.job_latest).toEqual(turnDetail(1));
    expect(restored?.beforeCursor).toBe('real-before-cursor');
    expect(restored?.restoredFromCache).toBe(true);
    expect(restored?.phase).toBe('bootstrapping');
    expect(restored?.activeJobs).toEqual([]);
    expect(restored?.eventCursor).toBeNull();
    expect(restored?.loadingDetailIds).toEqual([]);
  });

  test('原生退出会同时提交多个待写快照并等待已经开始的事务', async () => {
    const cache = new ThreadHistoryCache();
    const first = owner('history-native-close-a'), second = owner('history-native-close-b');
    cache.remember(first, history(), failure);
    const started = cache.flush(first);
    cache.remember(second, history(), failure);
    await cache.flushAll();
    const restored = new ThreadHistoryCache();
    expect((await restored.read(first, 'fresh'))?.turnsById.job_latest).toEqual(turnDetail(1));
    expect((await restored.read(second, 'fresh'))?.turnsById.job_latest).toEqual(turnDetail(1));
    await started;
  });

  test('关闭提交失败会拒绝退出，并保留待写快照供重试', async () => {
    const cache = new ThreadHistoryCache(), target = owner('history-native-retry');
    const errors: unknown[] = [];
    cache.remember(target, history(), error => errors.push(error));
    const fault = spyOn(IDBDatabase.prototype, 'transaction').mockImplementationOnce(() => {
      throw new Error('测试磁盘写入失败');
    });
    try {
      await expect(cache.flushAll()).rejects.toThrow('会话历史缓存尚未保存');
      expect(errors).toHaveLength(1);
    } finally { fault.mockRestore(); }
    expect(await new ThreadHistoryCache().read(target, 'fresh')).toBeNull();
    await cache.flushAll();
    expect((await new ThreadHistoryCache().read(target, 'fresh'))?.turnsById.job_latest).toEqual(turnDetail(1));
  });

  test('不同 Gateway、工作区、Session 和 Thread 均不能读取彼此正文', async () => {
    const target = owner('history-isolation');
    const cache = new ThreadHistoryCache();
    cache.remember(target, history(), failure);
    await cache.flush(target);
    for (const other of [{ ...target, gatewayId: 'other' }, { ...target, workspaceId: '00000000-0000-4000-8000-000000000002' },
      { ...target, sessionId: 'other' }, { ...target, threadId: 'other' }]) {
      expect(await cache.read(other, 'same-scope')).toBeNull();
    }
  });

  test('跨实例的迟到低版本写入不能覆盖已经保存的较新历史', async () => {
    const target = owner('history-revision');
    const newer = new ThreadHistoryCache(), older = new ThreadHistoryCache();
    newer.remember(target, { ...history(), projectionEpoch: 2, historyViewRevision: 8 }, failure);
    older.remember(target, history(), failure);
    await newer.flush(target);
    await older.flush(target);
    const restored = await new ThreadHistoryCache().read(target, 'scope');
    expect(restored?.projectionEpoch).toBe(2);
    expect(restored?.historyViewRevision).toBe(8);
  });

  test('删除会话会清除内存、待提交写入和数据库镜像', async () => {
    const target = owner('history-delete');
    const cache = new ThreadHistoryCache();
    cache.remember(target, history(), failure);
    await cache.flush(target);
    cache.remember(target, { ...history(), historyViewRevision: 2 }, failure);
    await cache.remove(target);
    await cache.flush(target);
    expect(await cache.read(target, 'scope')).toBeNull();
    expect(await new ThreadHistoryCache().read(target, 'scope')).toBeNull();
  });

  test('有限保留最近 64 个 Thread，不无限堆积正文', async () => {
    const cache = new ThreadHistoryCache();
    for (let index = 0; index < 66; index++) {
      const target = { ...owner('history-limit'), sessionId: `session-${index}` };
      cache.remember(target, history(), failure);
      await cache.flush(target);
    }
    const reload = new ThreadHistoryCache();
    expect(await reload.read({ ...owner('history-limit'), sessionId: 'session-0' }, 'scope')).toBeNull();
    expect(await reload.read({ ...owner('history-limit'), sessionId: 'session-65' }, 'scope')).not.toBeNull();
  });

  test('恢复镜像保持可见；bootstrap 不把它标记为已同步，权威空历史清除所有旧 Turn', async () => {
    const target = owner('history-authoritative');
    const cache = new ThreadHistoryCache();
    cache.remember(target, history(), failure);
    await cache.flush(target);
    const restored = beginTurnBootstrap((await cache.read(target, 'scope'))!, 1);
    const syncedMetadata = applyTurnBootstrap(restored, 1, { ...bootstrap('ready', 1), latest_turn: null });
    expect(syncedMetadata.orderedTurnIds).toEqual(['job_latest']);
    expect(syncedMetadata.phase).toBe('bootstrapping');
    const synced = applyTurnHistoryPage(syncedMetadata, { thread_id: THREAD_ID, ...HISTORY_SOURCE_BINDING,
      projection_epoch: 1, items: [], internal_executions: [], has_before: false, before_cursor: null });
    expect(synced.orderedTurnIds).toEqual([]);
    expect(synced.restoredFromCache).toBeUndefined();
    expect(synced.phase).toBe('ready');
    expect(synced.eventCursor).toBe('event-1');
  });
  test('迟到的较低历史版本不能把已恢复的较新正文清空或标记为已同步', () => {
    const cached = { ...history(), restoredFromCache: true as const, historyViewRevision: 8 };
    const stale = applyTurnHistoryPage(cached, { thread_id: THREAD_ID, ...HISTORY_SOURCE_BINDING,
      source_view_revision: 7, projection_epoch: 1, items: [], internal_executions: [] });
    expect(stale.orderedTurnIds).toEqual(['job_latest']);
    expect(stale.restoredFromCache).toBe(true);
    expect(stale.error).toContain('版本落后');
  });
});
