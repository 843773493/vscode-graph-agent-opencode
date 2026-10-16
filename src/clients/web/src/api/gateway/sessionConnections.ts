import type { CreatableSessionConnectionKind } from "../../types/frontend";
import type { ResourceThreadRef } from "../workspaceResources";
import { createWorkspaceBrowser, createWorkspaceTerminal } from "../workspaceResources";

export interface CreatedSessionConnection {
  kind: CreatableSessionConnectionKind;
  resourceId: string;
}

export async function createSessionConnection(
  port: number,
  workspaceId: string,
  thread: ResourceThreadRef,
  kind: CreatableSessionConnectionKind,
): Promise<CreatedSessionConnection> {
  const resource = kind === "terminal"
    ? await createWorkspaceTerminal(port, workspaceId, thread)
    : await createWorkspaceBrowser(port, workspaceId, thread);
  return { kind, resourceId: resource.resource_id };
}
