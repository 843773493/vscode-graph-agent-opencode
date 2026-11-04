import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey } from "../api/workspaceIdentity";
import type { GatewayWorkspace } from "../types/backend";
import { isRecord, jsonCandidates } from "../utils/jsonDisplay";
import { errorMessage } from "../utils/errorMessage";

export const WORKSPACE_INFORMATION_KIND =
  "boxteam_workspace_information" as const;

export interface WorkspaceInformationDump {
  kind: typeof WORKSPACE_INFORMATION_KIND;
  schema_version: 2;
  generated_at: string;
  workspace: {
    gateway_id: string;
    workspace_id: string;
    parent_workspace: GatewayWorkspace["parent_workspace"];
    child_workspaces: Array<{ gateway_id: string; workspace_id: string }>;
    name: string;
    connection_kind: GatewayWorkspace["connection_kind"];
    backend_url: string;
    active: boolean;
    status: GatewayWorkspace["status"];
    managed: boolean;
    removable: boolean;
    system_default: boolean;
    remote_gateways: GatewayWorkspace["remote_gateways"];
    connection_error: string | null;
  };
}

export function buildWorkspaceInformationDump(
  workspace: GatewayWorkspace,
  workspaces: GatewayWorkspace[],
): WorkspaceInformationDump {
  return {
    kind: WORKSPACE_INFORMATION_KIND,
    schema_version: 2,
    generated_at: new Date().toISOString(),
    workspace: {
      gateway_id: workspace.gateway_id,
      workspace_id: workspace.workspace_id,
      parent_workspace: workspace.parent_workspace ?? null,
      child_workspaces: workspaces
        .filter(
          (candidate) =>
            nullableGatewayWorkspaceKey(candidate.parent_workspace) === gatewayWorkspaceKey(workspace),
        )
        .map(({ gateway_id, workspace_id }) => ({ gateway_id, workspace_id })),
      name: workspace.name,
      connection_kind: workspace.connection_kind,
      backend_url: workspace.backend_url,
      active: workspace.active,
      status: workspace.status,
      managed: workspace.managed,
      removable: workspace.removable,
      system_default: workspace.system_default,
      remote_gateways: workspace.remote_gateways,
      connection_error: workspace.connection_error ?? null,
    },
  };
}

export function formatWorkspaceInformationDump(
  information: WorkspaceInformationDump,
): string {
  return JSON.stringify(information, null, 2);
}

export function extractWorkspaceSelectionFromClipboardText(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) {
    throw new Error("剪贴板内容为空");
  }

  const parseErrors: string[] = [];
  for (const candidate of jsonCandidates(trimmed)) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(candidate);
    } catch (error) {
      parseErrors.push(errorMessage(error));
      continue;
    }
    if (Array.isArray(parsed) && parsed.length === 2 && parsed.every(value => typeof value === "string" && value.trim())) {
      return gatewayWorkspaceKey({ gatewayId: parsed[0] as string, workspaceId: parsed[1] as string });
    }
    if (!isRecord(parsed) || parsed.kind !== WORKSPACE_INFORMATION_KIND) {
      parseErrors.push(`JSON kind 必须为 ${WORKSPACE_INFORMATION_KIND}`);
      continue;
    }
    const workspace = parsed.workspace;
    if (!isRecord(workspace) || typeof workspace.gateway_id !== "string" || typeof workspace.workspace_id !== "string") {
      throw new Error("通用工作区信息缺少 Gateway／UUID 归属");
    }
    return gatewayWorkspaceKey({ gateway_id: workspace.gateway_id, workspace_id: workspace.workspace_id });
  }

  throw new Error(
    "剪贴板内容缺少完整 Gateway／UUID 工作区归属: " +
      parseErrors.join("；"),
  );
}
