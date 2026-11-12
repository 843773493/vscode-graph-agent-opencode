import { useSessionViewState } from "./useSessionViewState";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { threadHistoryCache } from "../../state/threadHistoryCache";
import type { SessionTurnTimeline } from "../../state/session/turnTimeline";
import { listAgents } from "../../api";
import type { Agent, GatewayWorkspace, Session, WebUiSettings } from "../../types/backend";
import { INITIAL_APP_STATE } from "../app/appStateSeed";
import { cloneMaps } from "../../state/appStateMaps";
import { getConversationsForSession, messageStreamTurnIdForSession } from "../../state/conversations";
import { useSessionTurnHistory } from "../sessionTurnHistory/useSessionTurnHistory";
import { useSessionEventStream } from "../sessionEventStream/useSessionEventStream";
import { useSessionMessageStream } from "./useSessionMessageStream";
import { useSessionJobTurnLoader, useSessionTurnTimeline } from "./useSessionTurnTimeline";
import { useSessionRunActions } from "./useSessionRunActions";
import { useSessionLifecycleActions } from "./useSessionLifecycleActions";
import { useSessionGoalController } from "./useSessionGoalController";
import { useContentViewLoader } from "../content/useContentViewLoader";
import { useContentViewEffects } from "../content/useContentViewEffects";
import { useSessionTraceHistory } from "../sessionTraceHistory/useSessionTraceHistory";
import { createSessionCatalogOutboxRuntime } from "./sessionCatalogOutboxRuntime";
import { useComposerStateProjection } from "../composer/useComposerStateProjection";
import { useUiSettingsController } from "../settings/useUiSettingsController";
import type { AppState } from "../../types/frontend";

export interface ThreadSessionOwner {
  gatewayId: string;
  workspaceKey: string;
  workspaceId: string;
  sessionId: string;
  threadId: string;
}
export interface ThreadSessionSeed {
  history?: SessionTurnTimeline | null;
  browserViewScope: string;
  session: Session;
  agents: Agent[];
  workspaces: GatewayWorkspace[];
  settings: WebUiSettings;
}

/** 一个固定 owner 的会话实例；所有写入、历史与订阅均复用原 Web 链路。 */
export function useThreadSessionRuntime({ owner, seed, active, onSessionCreated, onError }: {
  owner: ThreadSessionOwner;
  seed: ThreadSessionSeed;
  active: boolean;
  onSessionCreated: (session: Session, workspaceId: string | null) => Promise<void>;
  onError: (message: string) => void;
}) {
  const scopeKey = JSON.stringify([owner.workspaceKey, owner.sessionId, owner.threadId]);
  const [state, setState] = useState<AppState>(() => ({
    ...cloneMaps(INITIAL_APP_STATE), apiPort: 0, isBootstrapping: false,
    currentSession: seed.session, currentSessionWorkspaceId: owner.workspaceKey,
    activeGatewayWorkspaceId: owner.workspaceKey, agents: seed.agents,
    browserViewScope: seed.browserViewScope, gatewayWorkspaces: seed.workspaces,
    uiSettings: seed.settings, uiSettingsLoaded: true,
    turnTimelinesBySession: new Map(seed.history ? [[scopeKey, seed.history]] : []),
  }));
  const latestStateRef = useRef(state);
  latestStateRef.current = state;
  const readState = useCallback(() => latestStateRef.current, []);
  const setStatus = useCallback((status: string) => setState(previous => ({ ...previous, status })), []);
  const views = useSessionViewState({ host: { apiPort: 0, currentWorkspaceId: owner.workspaceKey,
    currentSessionId: owner.sessionId, threadId: owner.threadId, scopeKey,
    sessionViewStates: state.sessionViewStates, expandDetails: state.expandDetails }, setState, onError });
  useEffect(() => { void views.loadSessionViewState(owner.workspaceKey, owner.sessionId); }, [views.loadSessionViewState, owner.workspaceKey, owner.sessionId]);
  const selectionIntentRef = useRef(0);
  const [catalogRuntime] = useState(() => createSessionCatalogOutboxRuntime({ apiPort: 0, onError }));
  useEffect(() => {
    return () => catalogRuntime.dispose();
  }, [catalogRuntime]);
  const { currentTurnTimeline: timeline, getCurrentTurnTimeline } = useSessionTurnTimeline(state, latestStateRef, scopeKey);
  const cacheOwner = useMemo(() => ({ gatewayId: owner.gatewayId, workspaceId: owner.workspaceId,
    sessionId: owner.sessionId, threadId: owner.threadId }), [owner.gatewayId, owner.workspaceId, owner.sessionId, owner.threadId]);
  useLayoutEffect(() => {
    if (timeline) threadHistoryCache.remember(cacheOwner, timeline, error => onError(`保存历史缓存失败：${String(error)}`));
  }, [cacheOwner, timeline, onError]);
  useEffect(() => {
    const flush = () => { void threadHistoryCache.flush(cacheOwner); };
    window.addEventListener("pagehide", flush);
    return () => { window.removeEventListener("pagehide", flush); flush(); };
  }, [cacheOwner]);
  const history = useSessionTurnHistory({
    apiPort: 0, sessionId: owner.sessionId, threadId: owner.threadId,
    workspaceId: owner.workspaceKey, sessionCacheKey: scopeKey, getCurrentTimeline: getCurrentTurnTimeline,
    reloadNonce: state.sessionHistoryReloadNonce, setState,
  });
  const activeJobId = state.activeJobIdsBySession.get(scopeKey) ?? null;
  const loadTerminalJobTurn = useSessionJobTurnLoader({
    state, loadTurnDetails: history.loadTurnDetails, latestStateRef,
    sessionCacheKey: scopeKey, refreshTurnHistory: history.refreshTurnHistory, setState,
  });
  const { abortCurrentStream } = useSessionEventStream({
    apiPort: active ? 0 : null, sessionId: owner.sessionId, threadId: owner.threadId,
    workspaceId: owner.workspaceKey, workspaceRequestTarget: null, sessionCacheKey: scopeKey,
    activeJobId, timelineReady: timeline?.phase === "ready" && timeline.projectionState === "ready",
    initialEventCursor: timeline?.eventCursor ?? null, refreshTurnHistory: history.refreshTurnHistory,
    loadTerminalJobTurn, setState,
  });
  useSessionMessageStream({
    apiPort: active ? 0 : null, sessionId: owner.sessionId, threadId: owner.threadId,
    turnId: messageStreamTurnIdForSession(owner.sessionId, state, scopeKey),
    workspaceId: owner.workspaceKey, sessionCacheKey: scopeKey, setState,
  });
  const wasActive = useRef(active);
  useEffect(() => {
    if (active && !wasActive.current) history.refreshTurnHistory();
    wasActive.current = active;
  }, [active, history.refreshTurnHistory]);
  const content = useContentViewLoader({ apiPort: 0, currentSession: state.currentSession,
    currentSessionGatewayWorkspaceId: owner.workspaceKey, setState, displayChangesInChat: false });
  useContentViewEffects({ contentView: state.contentView, sessionId: owner.sessionId,
    refreshLLMRequestLogs: content.refreshLLMRequestLogs, refreshSessionChanges: content.refreshSessionChanges });
  const traceHistory = state.sessionTraceHistoryBySession.get(scopeKey) ?? null;
  const traces = useSessionTraceHistory({ apiPort: 0, currentSession: state.currentSession,
    workspaceId: owner.workspaceKey, threadId: owner.threadId, scopeKey,
    active: active && state.contentView === "events", history: traceHistory, setState });
  const receivedEvents = useMemo(() => [
    ...(traceHistory?.items ?? []).map(event => ({ id: `initial_load:${event.event_id}`, kind: "trace" as const,
      sessionId: owner.sessionId, receivedAt: event.timestamp, source: "initial_load" as const, event })),
    ...(state.eventQueuesBySession.get(scopeKey) ?? []),
  ], [traceHistory?.items, state.eventQueuesBySession, scopeKey, owner.sessionId]);
  const lifecycle = useSessionLifecycleActions({
    apiPort: 0, currentSession: state.currentSession,
    activeGatewayWorkspaceId: owner.workspaceKey, currentSessionGatewayWorkspaceId: owner.workspaceKey,
    defaultGatewayWorkspaceId: owner.workspaceKey, readState, setState, abortCurrentStream,
    invalidateAgentState: content.invalidateAgentState, sessionCatalogOutboxRuntime: catalogRuntime, workspaceRequestTargets: seed.workspaces,
    selectionIntentRef, onSessionCreated,
  });
  const actions = useSessionRunActions({
    apiPort: 0, currentSession: state.currentSession, currentThreadId: owner.threadId,
    activeGatewayWorkspaceId: owner.workspaceKey, currentSessionGatewayWorkspaceId: owner.workspaceKey,
    currentSessionCacheKey: scopeKey, defaultGatewayWorkspaceId: owner.workspaceKey,
    contentView: state.contentView, setState, refreshAgentStateSnapshot: content.refreshAgentStateSnapshot,
    createSession: lifecycle.createSession,
  });
  const goal = useSessionGoalController({
    apiPort: 0, currentSessionId: owner.sessionId, currentMainThreadId: state.currentSession?.thread_id ?? null,
    currentWorkspaceId: owner.workspaceKey, setState,
  });
  const refreshAgents = useCallback(async (workspaceKey: string) => {
    if (workspaceKey !== owner.workspaceKey) throw new Error("Agent 刷新请求与会话工作区不一致");
    const agents = await listAgents(0, workspaceKey);
    setState(previous => ({ ...previous, agents }));
  }, [owner.workspaceKey]);
  const applySettings = useCallback(async (uiSettings: WebUiSettings) => {
    setState(previous => ({ ...previous, uiSettings, uiSettingsLoaded: true }));
  }, []);
  const updateUiSettings = useUiSettingsController({
    apiPort: 0, setState, applySettings, onError,
  });
  const { composerValue } = useComposerStateProjection({
    state, currentSessionCacheKey: scopeKey, latestStateRef, setStatus,
    ...actions, ...goal, switchAgent: lifecycle.switchAgent, switchModel: lifecycle.switchModel,
    refreshAgents, setWorkspaceDefaultAgent: lifecycle.setWorkspaceDefaultAgent,
    setWorkspaceDefaultProvider: lifecycle.setWorkspaceDefaultProvider,
    switchContentView: content.switchContentView, createSession: lifecycle.createSession, renameSession: lifecycle.renameSession, updateUiSettings,
  });
  const conversations = useMemo(() => getConversationsForSession(owner.sessionId, state, scopeKey), [owner.sessionId, state, scopeKey]);
  return { state, views, setStatus, timeline, conversations, composerValue, history, actions, lifecycle, selectionIntentRef, content, traces, traceHistory, receivedEvents, scopeKey };
}
