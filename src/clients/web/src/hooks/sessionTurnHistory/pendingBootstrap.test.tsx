import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { AppState, ConversationView } from "../../types/frontend";
import type { PendingRequestList } from "../../types/backend";
import { getConversationsForSession, writePendingSnapshot } from "../../state/conversations";
import { cloneMaps } from "../../state/appStateMaps";
import { createSessionTurnTimeline, upsertTurns } from "../../state/session/turnTimeline";
import { TurnActivitySummary } from "../../components/chat/turn/TurnActivitySummary";
import { useSessionJobTurnLoader } from "../session/useSessionTurnTimeline";
import {
  HISTORY_SOURCE_BINDING,
  SESSION_ID,
  THREAD_ID,
  SCOPE_KEY,
  WORKSPACE_ID,
  appState,
  bootstrap,
  emptyHistoryPageResponse,
  installWindow,
  originalFetch,
  restoreTurnHistoryTestGlobals,
  turnDetail,
  wait,
} from "./testFixtures";
import { useSessionTurnHistory } from "./useSessionTurnHistory";

afterEach(() => {
  restoreTurnHistoryTestGlobals();
});

describe("useSessionTurnHistory pending bootstrap", () => {
  test.each(["snapshot_first", "metadata_first", "queued_to_active", "internal_turn",
    "metadata_then_full", "full_then_metadata", "completed_metadata_expand"] as const)(
    "真实活动 Turn reader 在 %s 顺序下读取一次 metadata 并保留 live 内容", async (order) => {
      const apiPort = 9116;
      const jobId = "job_active_metadata";
      const turnId = "turn_active_metadata";
      const threadId = "thr_018f0000000070008000000000000001";
      installWindow(apiPort);
      let resolveMetadata!: (response: Response) => void;
      let resolvePending!: (response: Response) => void;
      let resolveFull!: (response: Response) => void;
      const metadataResponse = new Promise<Response>((resolve) => { resolveMetadata = resolve; });
      const pendingResponse = new Promise<Response>((resolve) => { resolvePending = resolve; });
      const fullResponse = new Promise<Response>((resolve) => { resolveFull = resolve; });
      const requests: Array<{ direction: string; turn_ids?: string[]; include?: string[] }> = [];
      const fullParts = [{ part_id: "part_active_body", kind: "text" as const, projection: "detail" as const,
        status: "completed" as const, source: { message_sequence: 1 }, text: "历史完整正文" }];
      const initialState = appState();
      const live: ConversationView = {
        conversationId: "msg_active_metadata", displayMode: "live", sessionId: SESSION_ID,
        turnId, userMessage: null,
        assistantMessages: [{ message_id: "msg_live_metadata", session_id: SESSION_ID, thread_id: threadId,
          role: "assistant", content: "已收到的实时正文", attachments: [], metadata: {},
          created_at: "2026-10-09T00:00:01Z", updated_at: "2026-10-09T00:00:01Z" }],
        events: [{ event_id: "evt_live_metadata", session_id: SESSION_ID, thread_id: threadId, job_id: jobId,
          type: "text_delta", phase: "text", title: "文本增量", content: "已收到的实时事件",
          timestamp: "2026-10-09T00:00:01Z" }],
        status: "running", jobId, pending: true, source: "pending",
      };
      if (order !== "internal_turn") initialState.pendingConversations.set(SCOPE_KEY, [live]);
      const snapshot: PendingRequestList = {
        session_id: SESSION_ID, thread_id: threadId,
        active_job_id: order === "queued_to_active" ? null : jobId,
        snapshot_version: 2,
        requests: [{ session_id: SESSION_ID, thread_id: threadId, job_id: jobId,
          turn_id: order === "internal_turn" ? null : turnId,
          message_id: "msg_active_metadata", content: "权威用户消息",
          attachments: [], execution_binding: { job_id: jobId,
            turn_id: order === "internal_turn" ? null : turnId,
            admission_idempotency_key: "admission_active_metadata" },
          delivery_policy: "after_turn", enqueue_sequence: 1, position: 0,
          status: "queued", agent_id: "default",
          message_created_at: "2026-10-09T00:00:00Z",
          message_metadata: { updated_at: "2026-10-09T00:00:01Z" },
          created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:00:01Z",
          snapshot_version: 2 }],
      };
      globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "active-turn-metadata-token" } });
        }
        if (path.endsWith("/bootstrap")) {
          return Response.json({ code: 0, message: "ok", request_id: "req_active_bootstrap", data: {
            ...bootstrap("ready", 1), latest_turn: null, active_job_id: snapshot.active_job_id,
            active_job_count: 1, active_jobs: [{ job_id: jobId, message_id: "msg_active_metadata",
              status: order === "queued_to_active" ? "queued" : "running",
              updated_at: "2026-10-09T00:00:01Z", snapshot_version: 2 }],
          } });
        }
        if (path.endsWith("/main-thread/pending-requests")) return pendingResponse;
        if (path.endsWith("/history")) {
          const parameters = JSON.parse(String(args[1]?.body));
          if (parameters.direction === "tail") return emptyHistoryPageResponse("req_active_tail");
          requests.push(parameters);
          return parameters.include?.length === 1 && parameters.include[0] === "metadata"
            ? metadataResponse : fullResponse;
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      }, { preconnect: originalFetch.preconnect });
      let currentState = initialState;
      let updateState!: React.Dispatch<React.SetStateAction<AppState>>;
      let history!: ReturnType<typeof useSessionTurnHistory>;
      function Harness(): React.ReactNode {
        const [state, setState] = React.useState(initialState);
        currentState = state;
        updateState = setState;
        const latest = React.useRef(state);
        latest.current = state;
        const getCurrentTimeline = React.useCallback(() => latest.current.turnTimelinesBySession.get(SCOPE_KEY) ?? null, []);
        history = useSessionTurnHistory({ apiPort, sessionId: SESSION_ID,
          workspaceId: WORKSPACE_ID, sessionCacheKey: SCOPE_KEY, getCurrentTimeline,
          reloadNonce: state.sessionHistoryReloadNonce, setState });
        useSessionJobTurnLoader({ state, loadTurnDetails: history.loadTurnDetails,
          latestStateRef: latest, sessionCacheKey: SCOPE_KEY,
          refreshTurnHistory: history.refreshTurnHistory, setState });
        const conversation = getConversationsForSession(SESSION_ID, state, SCOPE_KEY)[0];
        if (order === "completed_metadata_expand" && conversation?.displayMode === "history") {
          return <TurnActivitySummary conversation={conversation} groups={[]} showRawDetails={false}
            onLoadTurnDetails={history.loadTurnDetails} />;
        }
        return null;
      }
      const metadata = Response.json({ code: 0, message: "ok", request_id: "req_active_metadata", data: {
        thread_id: THREAD_ID,
        ...HISTORY_SOURCE_BINDING,
        projection_epoch: 1, internal_executions: [], items: [{ ...turnDetail(1),
          turn_id: turnId, job_id: jobId,
          status: order === "completed_metadata_expand" ? "completed" : "running",
          root_logical_item_ordinal: 15, activity_stats: { duration_ms: 1000, item_count: 1 },
          final_response: "", response_preview: "", response_parts: [] }],
      } });
      const full = Response.json({ code: 0, message: "ok", request_id: "req_active_full", data: {
        thread_id: THREAD_ID,
        ...HISTORY_SOURCE_BINDING,
        projection_epoch: 1, internal_executions: [], items: [{ ...turnDetail(1),
          turn_id: turnId, job_id: jobId,
          status: order === "completed_metadata_expand" ? "completed" : "running",
          root_logical_item_ordinal: 15, activity_stats: { duration_ms: 1000, item_count: 1 },
          final_response: "历史完整正文", response_preview: "历史完整正文", response_parts: fullParts }],
      } });
      const pending = Response.json({ code: 0, message: "ok", request_id: "req_active_pending", data: snapshot });
      let renderer!: ReactTestRenderer;
      await act(async () => { renderer = create(<Harness />); await wait(60); });
      if (order === "metadata_first") {
        await act(async () => { resolveMetadata(metadata); await wait(40); });
        expect(getConversationsForSession(SESSION_ID, currentState, SCOPE_KEY)[0]?.rootLogicalItemOrdinal).toBe(15);
      }
      await act(async () => { resolvePending(pending); await wait(40); });
      if (order === "internal_turn") {
        expect(requests).toEqual([]);
        expect(currentState.pendingConversations.has(SCOPE_KEY)).toBe(false);
        expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.orderedTurnIds).toEqual([]);
        expect(currentState.activeJobIdsBySession.get(SCOPE_KEY)).toBe(jobId);
        act(() => renderer.unmount());
        return;
      }
      if (order === "queued_to_active") {
        expect(requests).toEqual([]);
        await act(async () => {
          updateState((previous) => {
            const next = cloneMaps(previous);
            writePendingSnapshot(next.pendingConversations, next.activeJobIdsBySession,
              { ...snapshot, active_job_id: jobId, snapshot_version: 3 }, SCOPE_KEY);
            return next;
          });
          await wait(40);
        });
      }
      let fullRequest: Promise<void> | null = null;
      if (order === "metadata_then_full" || order === "full_then_metadata") {
        await act(async () => {
          fullRequest = history.loadTurnDetails([turnId], `full-active:${turnId}`);
          await wait(30);
        });
      }
      if (order === "full_then_metadata") {
        await act(async () => { resolveFull(full); await fullRequest; await wait(30); });
        expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.turnsById[turnId]?.response_parts)
          .toEqual(fullParts);
      }
      if (order !== "metadata_first") {
        await act(async () => { resolveMetadata(metadata); await wait(40); });
      }
      if (order === "metadata_then_full") {
        await act(async () => { resolveFull(full); await fullRequest; await wait(30); });
      }
      if (order === "completed_metadata_expand") {
        expect(getConversationsForSession(SESSION_ID, currentState, SCOPE_KEY)[0]?.displayMode).toBe("history");
        await act(async () => { renderer.root.findByType("button").props.onClick(); await wait(30); });
        expect(requests).toHaveLength(2);
        expect(renderer.root.findByProps({ className: "chat-thinking-preview" }).children.join(""))
          .toBe("耗时 1.0s · Item 1 项");
        expect(renderer.root.findByProps({ className: "chat-working-detail" }).children.join(""))
          .toBe("正在加载中间消息…");
        await act(async () => { resolveFull(full); await wait(40); });
      }
      await act(async () => {
        updateState((previous) => {
          const next = cloneMaps(previous);
          for (const version of [4, 4]) writePendingSnapshot(next.pendingConversations,
            next.activeJobIdsBySession, { ...snapshot, active_job_id: jobId, snapshot_version: version }, SCOPE_KEY);
          return next;
        });
        await wait(40);
      });
      const expectedRequests: typeof requests = [{ direction: "around", turn_ids: [turnId], include: ["metadata"] }];
      if (order === "metadata_then_full" || order === "full_then_metadata") {
        expectedRequests.push({ direction: "around", turn_ids: [turnId] });
      }
      if (order === "completed_metadata_expand") {
        expectedRequests.push({ direction: "around", turn_ids: [turnId], include: ["user", "text",
          "reasoning_detail", "encrypted_reasoning_meta", "tool_summary", "tool_call", "tool_result", "final_response"] });
      }
      expect(requests).toEqual(expectedRequests);
      const projected = getConversationsForSession(SESSION_ID, currentState, SCOPE_KEY)[0]!;
      expect(projected.rootLogicalItemOrdinal).toBe(15);
      expect(projected.turnItemsView).toBe("full");
      if (order === "completed_metadata_expand") {
        expect(projected.assistantMessages?.[0]?.content).toBe("历史完整正文");
        expect(projected.responseParts).toEqual(fullParts);
      } else {
        expect(projected.assistantMessages).toBe(live.assistantMessages);
        expect(projected.events).toBe(live.events);
        expect(projected.responseParts).toBeUndefined();
      }
      if (order === "metadata_then_full" || order === "full_then_metadata") {
        expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.turnsById[turnId]?.response_parts)
          .toEqual(fullParts);
        const fullTurn = currentState.turnTimelinesBySession.get(SCOPE_KEY)?.turnsById[turnId];
        expect(fullTurn && "final_response" in fullTurn ? fullTurn.final_response : null).toBe("历史完整正文");
      }
      expect(currentState.pendingConversations.get(SCOPE_KEY)?.[0]?.rootLogicalItemOrdinal).toBeUndefined();
      act(() => renderer.unmount());
    },
  );

  test.each(["session", "workspace"] as const)("活动 metadata 成功响应待写回时切换 %s 丢弃旧结果", async (scope) => {
    const apiPort = 9116;
    const jobId = "job_late_metadata";
    const turnId = "turn_late_metadata";
    const targetSessionId = scope === "session" ? "ses_target_metadata" : SESSION_ID;
    const targetWorkspaceId = scope === "workspace" ? "workspace_target_metadata" : WORKSPACE_ID;
    const targetScopeKey = `${targetWorkspaceId}::${targetSessionId}`;
    installWindow(apiPort);
    const initialState = appState();
    initialState.pendingConversations.set(SCOPE_KEY, [{ conversationId: turnId,
      displayMode: "live", sessionId: SESSION_ID, turnId, userMessage: null,
      assistantMessages: [], events: [], status: "running", jobId, pending: true, source: "pending" }]);
    const targetTurn = { ...bootstrap("ready", 1).latest_turn!, session_id: targetSessionId,
      turn_id: "turn_target_metadata", job_id: "job_target_metadata", root_logical_item_ordinal: 85 };
    initialState.turnTimelinesBySession.set(targetScopeKey, {
      ...upsertTurns(createSessionTurnTimeline(targetScopeKey), [targetTurn]),
      phase: "ready", projectionEpoch: 1, projectionState: "ready", threadId: THREAD_ID,
    });
    let resolveMetadata!: (response: Response) => void;
    const metadataResponse = new Promise<Response>((resolve) => { resolveMetadata = resolve; });
    const requests: string[][] = [];
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      const path = new URL(String(args[0]), `http://127.0.0.1:${apiPort}`).pathname;
      if (path === "/api/gateway/auth/local-credential") return Response.json({ data: { token: "late-metadata-token" } });
      if (path.endsWith("/bootstrap")) return Response.json({ code: 0, message: "ok", request_id: "req_late_bootstrap", data: {
        ...bootstrap("ready", 1), latest_turn: null, active_job_id: jobId, active_job_count: 1,
      } });
      if (path.endsWith("/main-thread/pending-requests")) return Response.json({ code: 0, message: "ok",
        request_id: "req_late_pending", data: { session_id: SESSION_ID,
          thread_id: "thr_018f0000000070008000000000000001", active_job_id: jobId, requests: [], snapshot_version: 2 } });
      if (path.endsWith("/history")) {
        const parameters = JSON.parse(String(args[1]?.body));
        if (parameters.direction === "tail") return emptyHistoryPageResponse("req_late_tail");
        requests.push(parameters.turn_ids);
        return metadataResponse;
      }
      throw new Error(`测试收到未预期请求: ${path}`);
    }, { preconnect: originalFetch.preconnect });
    let currentState = initialState;
    let updateState!: React.Dispatch<React.SetStateAction<AppState>>;
    let holdWriteBack = false;
    const delayedUpdates: React.SetStateAction<AppState>[] = [];
    function Harness(): React.ReactNode {
      const [state, setState] = React.useState(initialState);
      currentState = state;
      updateState = setState;
      const latest = React.useRef(state);
      latest.current = state;
      const scopeKey = `${state.currentSessionWorkspaceId}::${state.currentSession!.session_id}`;
      const getCurrentTimeline = React.useCallback(() => latest.current.turnTimelinesBySession.get(scopeKey) ?? null, [scopeKey]);
      const setHistoryState = React.useCallback((update: React.SetStateAction<AppState>) => {
        if (holdWriteBack) delayedUpdates.push(update);
        else setState(update);
      }, []);
      const history = useSessionTurnHistory({ apiPort, sessionId: state.currentSession!.session_id,
        workspaceId: state.currentSessionWorkspaceId, sessionCacheKey: scopeKey, getCurrentTimeline,
        reloadNonce: state.sessionHistoryReloadNonce, setState: setHistoryState });
      useSessionJobTurnLoader({ state, loadTurnDetails: history.loadTurnDetails,
        latestStateRef: latest, sessionCacheKey: scopeKey,
        refreshTurnHistory: history.refreshTurnHistory, setState: setHistoryState });
      return null;
    }
    let renderer!: ReactTestRenderer;
    await act(async () => { renderer = create(<Harness />); await wait(60); });
    expect(requests).toEqual([[turnId]]);
    // 真实 reader 已取得成功响应；延后 React updater，复现写回前切 scope。
    holdWriteBack = true;
    await act(async () => { resolveMetadata(Response.json({ code: 0, message: "ok", request_id: "req_late_metadata", data: {
      thread_id: THREAD_ID,
      ...HISTORY_SOURCE_BINDING,
      projection_epoch: 1, items: [{ ...turnDetail(1), turn_id: turnId, job_id: jobId,
        status: "running", root_logical_item_ordinal: 15, final_response: "", response_parts: [] }],
    } })); await wait(40); });
    holdWriteBack = false;
    expect(delayedUpdates).toHaveLength(1);
    await act(async () => { updateState((previous) => ({ ...previous,
      currentSession: { ...previous.currentSession!, session_id: targetSessionId, workspace_id: targetWorkspaceId },
      currentSessionWorkspaceId: targetWorkspaceId })); await wait(40); });
    const previousTimeline = currentState.turnTimelinesBySession.get(SCOPE_KEY);
    const targetBefore = currentState.turnTimelinesBySession.get(targetScopeKey);
    await act(async () => { updateState(delayedUpdates[0]!); await wait(30); });
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)).toBe(previousTimeline);
    expect(currentState.turnTimelinesBySession.get(targetScopeKey)).toBe(targetBefore);
    const visible = getConversationsForSession(targetSessionId, currentState, targetScopeKey);
    expect(visible.map((conversation) => conversation.turnId)).toEqual([targetTurn.turn_id]);
    expect(visible[0]?.rootLogicalItemOrdinal).toBe(85);
    expect(requests).toEqual([[turnId]]);
    act(() => renderer.unmount());
  });

  test("active Job 没有 queued request 时保留乐观 replay 提示", async () => {
    const apiPort = 9113;
    const activeJobId = "job_replay_active";
    installWindow(apiPort);
    let currentState = appState();
    const optimisticReplay: ConversationView = {
      conversationId: "msg_replay_active",
      displayMode: "live",
      sessionId: SESSION_ID,
      userMessage: {
        message_id: "msg_replay_active",
        session_id: SESSION_ID,
        thread_id: "thr_018f0000000070008000000000000001",
        role: "user",
        content: "请重新生成",
        attachments: [],
        metadata: {
          source: "optimistic_replay",
          replay_action: "regenerate",
          job_id: activeJobId,
        },
        created_at: "2026-07-28T00:00:04Z",
        updated_at: "2026-07-28T00:00:04Z",
      },
      assistantMessages: [],
      events: [],
      status: "running",
      jobId: activeJobId,
      pending: true,
      source: "pending",
    };
    currentState.pendingConversations.set(SCOPE_KEY, [optimisticReplay]);
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "turn-history-replay-token" } });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_bootstrap_replay",
            data: {
              ...bootstrap("ready", 1),
              latest_turn: null,
              active_job_id: activeJobId,
              active_jobs: [{
                job_id: activeJobId,
                message_id: "msg_replay_active",
                status: "running",
                updated_at: "2026-07-28T00:00:04Z",
                snapshot_version: 1,
              }],
              active_job_count: 1,
            },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          return emptyHistoryPageResponse("req_history_replay");
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/main-thread/pending-requests`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_pending_replay",
            data: {
              session_id: SESSION_ID,
              active_job_id: null,
              thread_id: "thr_018f0000000070008000000000000001",
              requests: [],
              snapshot_version: 1,
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      useSessionTurnHistory({
        apiPort,
        sessionId: SESSION_ID,
        workspaceId: WORKSPACE_ID,
        sessionCacheKey: SCOPE_KEY,
        getCurrentTimeline: () => currentState.turnTimelinesBySession.get(SCOPE_KEY) ?? null,
        reloadNonce: 0,
        setState: (update) => {
          currentState = typeof update === "function" ? update(currentState) : update;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await wait(60);
    });

    const replay = currentState.pendingConversations.get(SCOPE_KEY)?.[0];
    expect(replay?.userMessage?.metadata?.replay_action).toBe("regenerate");
    expect(replay?.jobId).toBe(activeJobId);
    expect(replay?.activeJobOverlay).toBe(true);
    expect(currentState.activeJobIdsBySession.get(SCOPE_KEY)).toBe(activeJobId);
    act(() => renderer!.unmount());
  });

  test("只有 queued request 时加载真实内容且不误标为 active Job", async () => {
    const apiPort = 9114;
    installWindow(apiPort);
    let currentState = appState();
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "turn-history-queued-token" } });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_bootstrap_queued",
            data: {
              ...bootstrap("ready", 1),
              latest_turn: null,
              active_jobs: [{
                job_id: "job_queued_only",
                message_id: "msg_queued_only",
                status: "queued",
                updated_at: "2026-07-29T00:00:00Z",
                snapshot_version: 1,
              }],
              snapshot_version: 1,
              active_job_count: 1,
            },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          return emptyHistoryPageResponse("req_history_queued");
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/main-thread/pending-requests`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_pending_queued",
            data: {
              session_id: SESSION_ID,
              active_job_id: null,
              thread_id: "thr_018f0000000070008000000000000001",
              requests: [{
                job_id: "job_queued_only",
                message_id: "msg_queued_only",
                session_id: SESSION_ID,
                thread_id: "thr_018f0000000070008000000000000001",
                turn_id: "turn_queued_only",
                execution_binding: { job_id: "job_queued_only", turn_id: "turn_queued_only", admission_idempotency_key: "admission_queued_only" },
                content: "排队消息真实内容",
                attachments: [],
                delivery_policy: "after_turn",
                enqueue_sequence: 1,
                status: "queued",
                position: 1,
                agent_id: "default",
                message_created_at: "2026-07-29T00:00:00Z",
                message_metadata: { updated_at: "2026-07-29T00:00:00Z" },
                created_at: "2026-07-29T00:00:00Z",
                updated_at: "2026-07-29T00:00:00Z",
                snapshot_version: 1,
              }],
              snapshot_version: 1,
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      useSessionTurnHistory({
        apiPort,
        sessionId: SESSION_ID,
        workspaceId: WORKSPACE_ID,
        sessionCacheKey: SCOPE_KEY,
        getCurrentTimeline: () => currentState.turnTimelinesBySession.get(SCOPE_KEY) ?? null,
        reloadNonce: 0,
        setState: (update) => {
          currentState = typeof update === "function" ? update(currentState) : update;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await wait(100);
    });

    expect(currentState.activeJobIdsBySession.has(SCOPE_KEY)).toBe(false);
    expect(currentState.pendingConversations.get(SCOPE_KEY)?.[0]?.userMessage?.content)
      .toBe("排队消息真实内容");
    expect(currentState.pendingConversations.get(SCOPE_KEY)?.[0]?.activeJobOverlay)
      .not.toBe(true);
    act(() => renderer!.unmount());
  });

  test("bootstrap 权威 count 为零时清除旧 queued 缓存", async () => {
    const apiPort = 9115;
    installWindow(apiPort);
    let currentState = appState();
    currentState.pendingConversations.set(SCOPE_KEY, [{
      conversationId: "old-queued",
      displayMode: "live",
      sessionId: SESSION_ID,
      userMessage: null,
      events: [],
      status: "queued",
      jobId: "old-queued-job",
      pending: true,
      source: "pending",
    }]);
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "turn-history-empty-token" } });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_bootstrap_empty_pending",
            data: { ...bootstrap("ready", 1), latest_turn: null },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          return emptyHistoryPageResponse("req_history_empty_pending");
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      useSessionTurnHistory({
        apiPort,
        sessionId: SESSION_ID,
        workspaceId: WORKSPACE_ID,
        sessionCacheKey: SCOPE_KEY,
        getCurrentTimeline: () => currentState.turnTimelinesBySession.get(SCOPE_KEY) ?? null,
        reloadNonce: 0,
        setState: (update) => {
          currentState = typeof update === "function" ? update(currentState) : update;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await wait(30);
    });
    expect(currentState.pendingConversations.has(SCOPE_KEY)).toBe(false);
    act(() => renderer!.unmount());
  });
});
