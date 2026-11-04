import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey } from "../../../web/src/api/workspaceIdentity.js";
import { useGatewayAccessHeartbeat } from "../../../web/src/hooks/settings/useGatewayAccessHeartbeat.js";
import { browserGatewayId, getBrowserUiSettings } from "../../../web/src/hooks/settings/browserUiSettings.js";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { getSession } from '../../../web/src/api.js';
import { ensureGatewayUserAccess } from '../../../web/src/api/gateway/userAccess.js';
import { listGatewayWorkspaces } from '../../../web/src/gatewayApi.js';
import { INITIAL_APP_STATE } from '../../../web/src/hooks/app/appStateSeed.js';
import { cloneMaps } from '../../../web/src/state/appStateMaps.js';
import { withFreshGatewayWorkspaceList } from '../../../web/src/state/gatewayWorkspaceState.js';
import { createSerialTaskQueue } from '../../../web/src/hooks/runtime/serialTaskQueue.js';
import { createSessionCatalogOutboxRuntime } from '../../../web/src/hooks/session/sessionCatalogOutboxRuntime.js';
import { useSessionCreationActions } from '../../../web/src/hooks/session/lifecycle/useSessionCreationActions.js';
import { useSessionCatalogActions } from '../../../web/src/hooks/session/lifecycle/useSessionCatalogActions.js';
import { useGatewayWorkspaceActivation } from '../../../web/src/hooks/gatewayWorkspace/useGatewayWorkspaceActivation.js';
import { useGatewayWorkspaceHierarchy } from '../../../web/src/hooks/gatewayWorkspace/useGatewayWorkspaceHierarchy.js';
import { useGatewayWorkspaceMutations } from '../../../web/src/hooks/gatewayWorkspace/useGatewayWorkspaceMutations.js';
import { useGatewayWorkspaceRuntimeLifecycle } from '../../../web/src/hooks/gatewayWorkspace/useGatewayWorkspaceRuntimeLifecycle.js';
import { useWorkspaceRefreshOrchestration } from '../../../web/src/hooks/workspace/useWorkspaceRefreshOrchestration.js';
import { useUiSettingsController } from '../../../web/src/hooks/settings/useUiSettingsController.js';
import { useWorkspaceSessionActivity } from '../../../web/src/hooks/workspace/useWorkspaceSessionActivity.js';
import { useUnreadSessionTracking } from '../../../web/src/hooks/session/useUnreadSessionTracking.js';
import { requireGatewayWorkspaceRequestTarget, type GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { AppState } from '../../../web/src/types/frontend.js';
import type { Session, WebUiSettings } from '../../../web/src/types/backend.js';
import { gateway } from '../agentApi.js';
import { workspaceContext } from '../workspaceContext.js';

/** 导航持有目录镜像；聊天与流式运行状态仍由各个 Editor 的显式 owner 管理。 */
export function useNavigationRuntime(
  onSessionCreated: (session: Session, workspaceKey: string | null) => Promise<void>,
  onError: (message: string) => void,
  refreshSessionOwner: (target: GatewayWorkspaceRequestTarget, signal: AbortSignal) => Promise<void>,
  currentSessionCacheKey: string | null,
) {
  const [state, setState] = useState<AppState>(() => ({ ...cloneMaps(INITIAL_APP_STATE), apiPort: 0 }));
  useEffect(() => {
    workspaceContext.publish({ gatewayId: state.browserViewScope ?? null, workspaces: state.gatewayWorkspaces, activeId: state.activeGatewayWorkspaceId,
      settings: state.uiSettingsLoaded ? state.uiSettings : null, loading: state.isBootstrapping || state.workspaceSwitching,
      error: state.gatewayError });
  }, [state.browserViewScope, state.gatewayWorkspaces, state.activeGatewayWorkspaceId, state.uiSettings, state.uiSettingsLoaded,
    state.isBootstrapping, state.workspaceSwitching, state.gatewayError]);
  const latestStateRef = useRef(state); latestStateRef.current = state;
  const readState = useCallback(() => latestStateRef.current, []);
  const setStatus = useCallback((status: string) => setState(previous => ({ ...previous, status })), []);
  const reportError = useCallback((message: string) => {
    setStatus(message);
    onError(message);
  }, [onError, setStatus]);
  const selectionIntentRef = useRef(0), gatewayListRequestRef = useRef(0);
  const [queue] = useState(createSerialTaskQueue);
  const [catalog] = useState(() => createSessionCatalogOutboxRuntime({ apiPort: 0, onError: reportError, refreshSessionOwner }));
  useEffect(() => { catalog.activatePrincipal(state.gatewayUserAccess); }, [catalog, state.gatewayUserAccess]);
  useEffect(() => () => catalog.dispose(), [catalog]);
  const catalogSnapshot = useSyncExternalStore(catalog.subscribe, catalog.getSnapshot, catalog.getSnapshot);
  const invalidateWorkspaceRefreshes = useCallback(() => { gatewayListRequestRef.current += 1; }, []);
  const refreshSessions = useCallback(async () => {
    const request = ++gatewayListRequestRef.current;
    const [workspaces, access, settings, browserViewScope] = await queue.enqueue(() => Promise.all([
      listGatewayWorkspaces(0), ensureGatewayUserAccess(0), getBrowserUiSettings(0), browserGatewayId(0),
    ]));
    const workspaceKey = nullableGatewayWorkspaceKey(workspaces.active_workspace);
    const sessions = workspaceKey ? await gateway.sessions(workspaceKey) : [];
    if (request !== gatewayListRequestRef.current) return null;
    setState(previous => {
      const next = cloneMaps(withFreshGatewayWorkspaceList(previous, workspaces.items));
      if (workspaceKey) next.sessionsByWorkspace.set(workspaceKey, sessions);
      return { ...next, activeGatewayWorkspaceId: workspaceKey, sessions,
        workspaceName: workspaces.items.find(item => gatewayWorkspaceKey(item) === workspaceKey)?.name ?? '',
        gatewayUserAccess: access, browserViewScope, uiSettings: settings, uiSettingsLoaded: true, isBootstrapping: false };
    });
    return workspaceKey;
  }, [queue]);
  useEffect(() => {
    void refreshSessions().catch(cause => reportError(`导航初始化失败：${String(cause)}`));
    return invalidateWorkspaceRefreshes;
  }, [refreshSessions, invalidateWorkspaceRefreshes, reportError]);
  useGatewayAccessHeartbeat(0, state.gatewayUserAccess !== null, refreshSessions, reportError);
  const refreshGatewayWorkspaceStatuses = useCallback(async () => { await refreshSessions(); }, [refreshSessions]);
  const orchestration = useWorkspaceRefreshOrchestration({
    apiPort: 0, gatewayWorkspaces: state.gatewayWorkspaces, setState, refreshSessions,
    abortCurrentStream: invalidateWorkspaceRefreshes, selectionIntentRef,
  });
  const shared = { apiPort: 0, currentSessionId: state.currentSession?.session_id ?? null, setState,
    gatewayListRequestRef, runGatewayWorkspaceListTask: queue.enqueue, selectionIntentRef };
  const activation = useGatewayWorkspaceActivation({ ...shared, latestStateRef, invalidateWorkspaceRefreshes,
    refreshGatewayWorkspaceStatuses, ...orchestration });
  const lifecycle = useGatewayWorkspaceRuntimeLifecycle({ ...shared, finishWorkspaceRefresh: orchestration.finishWorkspaceRefresh,
    refreshGatewayState: activation.refreshGatewayState });
  const applySettings = useCallback(async (uiSettings: WebUiSettings) => {
    setState(previous => ({ ...previous, uiSettings, uiSettingsLoaded: true }));
  }, []);
  const updateUiSettings = useUiSettingsController({ apiPort: 0, setState,
    applySettings, onError: reportError });
  const mutations = useGatewayWorkspaceMutations({ ...shared, activeGatewayWorkspaceId: state.activeGatewayWorkspaceId,
    abortCurrentStream: invalidateWorkspaceRefreshes, invalidateWorkspaceRefreshes, ...orchestration, updateUiSettings });
  const setWorkspaceParent = useGatewayWorkspaceHierarchy(0, setState, gatewayListRequestRef, queue.enqueue, selectionIntentRef);
  const context = { apiPort: 0, currentSession: state.currentSession, activeGatewayWorkspaceId: state.activeGatewayWorkspaceId,
    currentSessionGatewayWorkspaceId: state.currentSessionWorkspaceId,
    defaultGatewayWorkspaceId: nullableGatewayWorkspaceKey(state.gatewayWorkspaces.find(item => item.system_default)),
    readState, setState, abortCurrentStream: invalidateWorkspaceRefreshes, invalidateAgentState: invalidateWorkspaceRefreshes,
    sessionCatalogOutboxRuntime: catalog, gatewayUserAccess: state.gatewayUserAccess,
    workspaceRequestTargets: state.gatewayWorkspaces, selectionIntentRef, onSessionCreated };
  const creation = useSessionCreationActions(context);
  const sessionActions = useSessionCatalogActions(context);
  const activeSessionRequest = useRef(0);
  useEffect(() => () => { activeSessionRequest.current += 1; }, []);
  const clearActiveSession = useCallback(() => {
    activeSessionRequest.current += 1;
    setState(previous => ({ ...previous, currentSession: null, currentSessionWorkspaceId: null }));
  }, []);
  const setActiveSession = useCallback(async (workspaceKey: string, sessionId: string) => {
    const request = ++activeSessionRequest.current;
    const session = await getSession(0, sessionId, workspaceKey);
    if (request === activeSessionRequest.current) setState(previous => ({ ...previous, currentSession: session, currentSessionWorkspaceId: workspaceKey }));
    return session;
  }, []);
  useUnreadSessionTracking({ unreadSessionKeys: state.unreadSessionKeys, currentSessionCacheKey, setState });
  useWorkspaceSessionActivity({ apiPort: 0, workspaceTarget: state.activeGatewayWorkspaceId
    ? requireGatewayWorkspaceRequestTarget(state.gatewayWorkspaces, state.activeGatewayWorkspaceId) : null,
    currentSessionCacheKey, setState });
  return { state, setState, setStatus, reportError, setActiveSession, clearActiveSession, catalog, catalogSnapshot, selectionIntentRef,
    updateUiSettings, setWorkspaceParent, ...activation, ...orchestration, ...lifecycle, ...mutations, ...creation, ...sessionActions };
}
