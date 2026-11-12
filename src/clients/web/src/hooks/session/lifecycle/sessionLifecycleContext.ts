import type { MutableRefObject } from "react";
import type { Session } from "../../../types/backend";
import type { AppState } from "../../../types/frontend";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import type { SetAppState } from "../../contentViewLoaderTypes";
import type { SessionCatalogOutboxRuntime } from "../sessionCatalogOutboxRuntime";

export type SessionLifecycleContext = {
  onSessionCreated?: (session: Session, workspaceId: string | null) => Promise<void>;
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

  workspaceRequestTargets: readonly GatewayWorkspaceRequestTarget[];
  selectionIntentRef: MutableRefObject<number>;
};
