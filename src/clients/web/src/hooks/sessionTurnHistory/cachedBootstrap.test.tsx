import { afterEach, expect, test } from 'bun:test';
import React from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { useSessionTurnHistory } from './useSessionTurnHistory';
import { createSessionTurnTimeline, upsertTurns } from '../../state/session/turnTimeline';
import { appState, bootstrap, emptyHistoryPageResponse, installWindow, originalFetch,
  restoreTurnHistoryTestGlobals, SESSION_ID, SCOPE_KEY, THREAD_ID, WORKSPACE_ID, turnDetail, wait } from './testFixtures';

afterEach(restoreTurnHistoryTestGlobals);

test('真实历史入口先保留恢复镜像，等待 bootstrap 与 history，权威空页移除旧正文', async () => {
  installWindow(9149);
  let state = appState();
  state.turnTimelinesBySession.set(SCOPE_KEY, { ...upsertTurns(createSessionTurnTimeline(SCOPE_KEY), [turnDetail(1)]),
    threadId: THREAD_ID, phase: 'bootstrapping', restoredFromCache: true, projectionEpoch: 1, historyViewRevision: 1 });
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
    useSessionTurnHistory({ apiPort: 9149, sessionId: SESSION_ID, threadId: THREAD_ID, workspaceId: WORKSPACE_ID,
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
      finishBootstrap(Response.json({ code: 0, message: 'ok', request_id: 'cached-bootstrap', data: { ...bootstrap('ready', 1), latest_turn: null } }));
      await wait(20);
    });
    expect(paths.some(path => path.endsWith('/history'))).toBe(true);
    expect(current().orderedTurnIds).toEqual(['job_latest']);
    expect(current().phase).toBe('bootstrapping');
    await act(async () => { finishHistory(emptyHistoryPageResponse('cached-history')); await wait(20); });
    expect(current().orderedTurnIds).toEqual([]);
    expect(current().restoredFromCache).toBeUndefined();
    expect(current().phase).toBe('ready');
  } finally { act(() => renderer.unmount()); }
});
