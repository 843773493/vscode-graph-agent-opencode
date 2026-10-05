import type { CreatableSessionConnectionKind } from "../../types/frontend";
import { createWorkspaceBrowser, createWorkspaceTerminal } from "../workspaceResources";

export interface CreatedSessionConnection {
  kind: CreatableSessionConnectionKind;
  resourceId: string;
}

export async function createSessionConnection(
  port: number,
  workspaceId: string,
  sessionId: string,
  kind: CreatableSessionConnectionKind,
): Promise<CreatedSessionConnection> {
  const resource = kind === "terminal"
    ? await createWorkspaceTerminal(port, workspaceId, sessionId)
    : await createWorkspaceBrowser(port, workspaceId, sessionId);
  return { kind, resourceId: resource.resource_id };
}
