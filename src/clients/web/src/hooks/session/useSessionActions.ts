import type { GatewayUserAccess, Session } from "../../types/backend";
import type { AppState, ConversationContentView } from "../../types/frontend";
import type { SetAppState } from "../contentViewLoaderTypes";
import type { GatewayWorkspaceForkRoute } from "../../api/workspaceIdentity";
import type { MutableRefObject } from "react";
import { useSessionLifecycleActions } from "./useSessionLifecycleActions";
import { useSessionRunActions } from "./useSessionRunActions";
import type { SessionCatalogOutboxRuntime } from "./sessionCatalogOutboxRuntime";

export function useSessionActions({
  apiPort,
  currentSession,
  activeGatewayWorkspaceId,
  currentSessionGatewayWorkspaceId,
  workspaceRequestTargets,
  currentSessionCacheKey,
  defaultGatewayWorkspaceId,
  contentView,
  readState,
  setState,
  abortCurrentStream,
  invalidateAgentState,
  refreshAgentStateSnapshot,
  sessionCatalogOutboxRuntime,
  gatewayUserAccess,
  selectionIntentRef,
}: {
  apiPort: number;
  currentSession: Session | null;
  activeGatewayWorkspaceId: string | null;
  currentSessionGatewayWorkspaceId: string | null;
  workspaceRequestTargets: readonly GatewayWorkspaceForkRoute[];
  currentSessionCacheKey: string | null;
  defaultGatewayWorkspaceId: string | null;
  contentView: ConversationContentView;
  readState: () => AppState;
  setState: SetAppState;
  abortCurrentStream: () => void;
  invalidateAgentState: () => void;
  refreshAgentStateSnapshot: (sessionId: string) => Promise<void>;
  sessionCatalogOutboxRuntime: SessionCatalogOutboxRuntime;
  gatewayUserAccess: GatewayUserAccess | null;
  selectionIntentRef: MutableRefObject<number>;
}) {
  const lifecycleActions = useSessionLifecycleActions({
    apiPort,
    currentSession,
    defaultGatewayWorkspaceId,
    activeGatewayWorkspaceId,
    currentSessionGatewayWorkspaceId,
    workspaceRequestTargets,
    readState,
    setState,
    abortCurrentStream,
    invalidateAgentState,
    sessionCatalogOutboxRuntime,
    gatewayUserAccess,
    selectionIntentRef,
  });
  const runActions = useSessionRunActions({
    apiPort,
    currentSession,
    activeGatewayWorkspaceId,
    currentSessionGatewayWorkspaceId,
    currentSessionCacheKey,
    defaultGatewayWorkspaceId,
    contentView,
    setState,
    refreshAgentStateSnapshot,
    createSession: lifecycleActions.createSession,
  });

  return {
    ...lifecycleActions,
    ...runActions,
  };
}
