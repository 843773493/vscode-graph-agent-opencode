import type { MutableRefObject } from "react";
import type { GatewayUserAccess, Session } from "../../../types/backend";
import type { AppState } from "../../../types/frontend";
import type { GatewayWorkspaceForkRoute } from "../../../api/workspaceIdentity";
import type { SetAppState } from "../../contentViewLoaderTypes";
import type { SessionCatalogOutboxRuntime } from "../sessionCatalogOutboxRuntime";

export type SessionLifecycleContext = {
  apiPort: number;
  currentSession: Session | null;
  activeGatewayWorkspaceId: string | null;
  currentSessionGatewayWorkspaceId: string | null;
  defaultGatewayWorkspaceId: string | null;
  readState: () => AppState;
  setState: SetAppState;
  abortCurrentStream: () => void;
  invalidateAgentState: () => void;
  sessionCatalogOutboxRuntime?: SessionCatalogOutboxRuntime;
  gatewayUserAccess?: GatewayUserAccess | null;
  workspaceRequestTargets: readonly GatewayWorkspaceForkRoute[];
  selectionIntentRef: MutableRefObject<number>;
};
