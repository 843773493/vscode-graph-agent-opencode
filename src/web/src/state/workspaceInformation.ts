import type { GatewayWorkspace } from "../types/backend";

export const WORKSPACE_INFORMATION_KIND =
  "boxteam_workspace_information" as const;

export interface WorkspaceInformationDump {
  kind: typeof WORKSPACE_INFORMATION_KIND;
  schema_version: 1;
  generated_at: string;
  workspace: {
    id: string;
    parent_workspace_id: string | null;
    child_workspace_ids: string[];
    name: string;
    root_path: string;
    connection_kind: GatewayWorkspace["connection_kind"];
    backend_url: string;
    active: boolean;
    status: GatewayWorkspace["status"];
    managed: boolean;
    removable: boolean;
    system_default: boolean;
    remote: GatewayWorkspace["remote"];
    connection_error: string | null;
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isWorkspaceId(value: unknown): value is string {
  return typeof value === "string" && /^gw_[A-Za-z0-9_-]+$/.test(value);
}

function jsonCandidates(text: string): string[] {
  const candidates = [text];
  const fencePattern = /```(?:json)?\s*([\s\S]*?)```/gi;
  for (const match of text.matchAll(fencePattern)) {
    const fencedJson = match[1]?.trim();
    if (fencedJson) {
      candidates.push(fencedJson);
    }
  }
  return [...new Set(candidates)];
}

export function buildWorkspaceInformationDump(
  workspace: GatewayWorkspace,
  workspaces: GatewayWorkspace[],
): WorkspaceInformationDump {
  return {
    kind: WORKSPACE_INFORMATION_KIND,
    schema_version: 1,
    generated_at: new Date().toISOString(),
    workspace: {
      id: workspace.workspace_id,
      parent_workspace_id: workspace.parent_workspace_id ?? null,
      child_workspace_ids: workspaces
        .filter(
          (candidate) =>
            candidate.parent_workspace_id === workspace.workspace_id,
        )
        .map((candidate) => candidate.workspace_id),
      name: workspace.name,
      root_path: workspace.root_path,
      connection_kind: workspace.connection_kind,
      backend_url: workspace.backend_url,
      active: workspace.active,
      status: workspace.status,
      managed: workspace.managed,
      removable: workspace.removable,
      system_default: workspace.system_default,
      remote: workspace.remote,
      connection_error: workspace.connection_error ?? null,
    },
  };
}

export function formatWorkspaceInformationDump(
  information: WorkspaceInformationDump,
): string {
  return JSON.stringify(information, null, 2);
}

export function extractWorkspaceIdFromClipboardText(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) {
    throw new Error("剪贴板内容为空");
  }
  if (isWorkspaceId(trimmed)) {
    return trimmed;
  }

  const parseErrors: string[] = [];
  for (const candidate of jsonCandidates(trimmed)) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(candidate);
    } catch (error) {
      parseErrors.push(error instanceof Error ? error.message : String(error));
      continue;
    }
    if (!isRecord(parsed) || parsed.kind !== WORKSPACE_INFORMATION_KIND) {
      parseErrors.push(`JSON kind 必须为 ${WORKSPACE_INFORMATION_KIND}`);
      continue;
    }
    const workspace = parsed.workspace;
    if (!isRecord(workspace) || !isWorkspaceId(workspace.id)) {
      throw new Error("通用工作区信息缺少有效 workspace.id");
    }
    return workspace.id;
  }

  throw new Error(
    "剪贴板内容既不是工作区 ID，也不是有效的通用工作区信息 JSON: " +
      parseErrors.join("；"),
  );
}
