import { afterEach, expect, test } from 'bun:test';
import React from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { useSessionTurnHistory } from './useSessionTurnHistory';
import { createSessionTurnTimeline, upsertTurns } from '../../state/session/turnTimeline';
import { appState, bootstrap, emptyHistoryPageResponse, installWindow, originalFetch,
  restoreTurnHistoryTestGlobals, SESSION_ID, SCOPE_KEY, THREAD_ID, WORKSPACE_KEY, WORKSPACE_OWNER, turnDetail, wait } from './testFixtures';

afterEach(restoreTurnHistoryTestGlobals);

test.each([{ epoch: 1, cachedRevision: 1 }, { epoch: 2, cachedRevision: 8 }])(
  '真实历史入口保留恢复镜像直到代际 $epoch 的权威历史页返回', async ({ epoch, cachedRevision }) => {
  installWindow(9149);
  let state = appState();
  state.turnTimelinesBySession.set(SCOPE_KEY, { ...upsertTurns(createSessionTurnTimeline(SCOPE_KEY), [turnDetail(1)]),
    threadId: THREAD_ID, phase: 'bootstrapping', restoredFromCache: true, projectionEpoch: 1, historyViewRevision: cachedRevision });
  let finishBootstrap!: (value: Response) => void, finishHistory!: (value: Response) => void;
  const pendingBootstrap = new Promise<Response>(resolve => { finishBootstrap = resolve; });
  const pendingHistory = new Promise<Response>(resolve => { finishHistory = resolve; });
  const paths: string[] = [];
  globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
    const path = new URL(String(args[0]), 'http://127.0.0.1:9149').pathname;
    if (path === '/api/gateway/auth/local-credential') return Response.json({ data: { token: 'cached-bootstrap' } });
    paths.push(path);
    if (path.endsWith('/bootstrap')) return pendingBootstrap;
    if (path.endsWith('/history')) return pendingHistory;
    throw new Error(`未声明的请求：${path}`);
  }, { preconnect: originalFetch.preconnect });
  const current = () => state.turnTimelinesBySession.get(SCOPE_KEY)!;
  function Harness() {
    useSessionTurnHistory({ apiPort: 9149, sessionId: SESSION_ID, threadId: THREAD_ID, workspaceId: WORKSPACE_KEY,
      sessionCacheKey: SCOPE_KEY, getCurrentTimeline: current, reloadNonce: 0,
      setState: update => { state = typeof update === 'function' ? update(state) : update; } });
    return null;
  }
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<Harness />); await wait(20); });
    expect(current().orderedTurnIds).toEqual(['job_latest']);
    expect(current().phase).toBe('bootstrapping');
    await act(async () => {
      finishBootstrap(Response.json({ code: 0, message: 'ok', request_id: 'cached-bootstrap', data: { ...bootstrap('ready', epoch), latest_turn: null } }));
      await wait(20);
    });
    expect(paths.some(path => path.endsWith('/history'))).toBe(true);
    expect(current().orderedTurnIds).toEqual(['job_latest']);
    expect(current().phase).toBe('bootstrapping');
    expect(current().restoredFromCache).toBe(true);
    await act(async () => { finishHistory(emptyHistoryPageResponse('cached-history', epoch)); await wait(20); });
    expect(current().orderedTurnIds).toEqual([]);
    expect(current().restoredFromCache).toBeUndefined();
    expect(current().phase).toBe('ready');
  } finally { act(() => renderer.unmount()); }
});

test.each(['bootstrap', 'history'] as const)(
  '%s 失败后保留只读镜像和原错误，明确重试取得权威历史后才退出预览', async failureStage => {
    installWindow(9150);
    let state = appState(), recovering = false;
    state.turnTimelinesBySession.set(SCOPE_KEY, { ...upsertTurns(createSessionTurnTimeline(SCOPE_KEY), [turnDetail(1)]),
      threadId: THREAD_ID, phase: 'bootstrapping', restoredFromCache: true, projectionEpoch: 1, historyViewRevision: 8 });
    const paths: string[] = [];
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      const path = new URL(String(args[0]), 'http://127.0.0.1:9150').pathname;
      if (path === '/api/gateway/auth/local-credential') return Response.json({ data: { token: 'cached-retry' } });
      paths.push(path);
      const headers = new Headers(args[1]?.headers);
      expect(headers.get('X-BoxTeam-Target-Gateway-Id')).toBe(WORKSPACE_OWNER.gateway_id);
      expect(headers.get('X-BoxTeam-Workspace-Id')).toBe(WORKSPACE_OWNER.workspace_id);
      if (!recovering && path.endsWith('/' + failureStage)) {
        return Response.json({ detail: '历史来源暂不可用，请保留原镜像' }, { status: 503 });
      }
      if (path.endsWith('/bootstrap')) return Response.json({ code: 0, message: 'ok', request_id: 'metadata-2',
        data: { ...bootstrap('ready', 2), latest_turn: null } });
      if (path.endsWith('/history')) return emptyHistoryPageResponse('history-2', 2);
      throw new Error(`未声明的请求：${path}`);
    }, { preconnect: originalFetch.preconnect });
    const current = () => state.turnTimelinesBySession.get(SCOPE_KEY)!;
    function Harness({ reloadNonce }: { reloadNonce: number }) {
      useSessionTurnHistory({ apiPort: 9150, sessionId: SESSION_ID, threadId: THREAD_ID, workspaceId: WORKSPACE_KEY,
        sessionCacheKey: SCOPE_KEY, getCurrentTimeline: current, reloadNonce,
        setState: update => { state = typeof update === 'function' ? update(state) : update; } });
      return null;
    }
    let renderer!: ReactTestRenderer;
    try {
      await act(async () => { renderer = create(<Harness reloadNonce={0} />); await wait(20); });
      expect(current().orderedTurnIds).toEqual(['job_latest']);
      expect(current().restoredFromCache).toBe(true);
      expect(current().phase).not.toBe('ready');
      expect(current().error).toContain('历史来源暂不可用');
      expect(current().loadingBefore).toBe(false);
      expect(current().loadingAfter).toBe(false);
      expect(paths.filter(path => path.endsWith('/bootstrap'))).toHaveLength(1);
      expect(paths.filter(path => path.endsWith('/history'))).toHaveLength(failureStage === 'history' ? 1 : 0);
      recovering = true;
      await act(async () => { renderer.update(<Harness reloadNonce={1} />); await wait(20); });
      expect(current().orderedTurnIds).toEqual([]);
      expect(current().restoredFromCache).toBeUndefined();
      expect(current().phase).toBe('ready');
      expect(current().projectionEpoch).toBe(2);
      expect(current().error).toBeNull();
    } finally { act(() => renderer.unmount()); }
  });
