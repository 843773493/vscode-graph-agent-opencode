import type {
  GatewayWorkspace,
  SessionInformationSnapshot,
} from "../../types/backend";

export const SESSION_INFORMATION_KIND = "boxteam_session_information" as const;

interface LocalSessionConnection {
  kind: "local";
  backend_url: string;
  managed: boolean;
  connection_error: string | null;
}

interface SshSessionConnection {
  kind: "ssh";
  host: string;
  port: number;
  username: string;
  remote_backend_host: string;
  remote_backend_port: number;
  tunnel_backend_url: string;
  managed: boolean;
  connection_error: string | null;
}

type SessionConnection = LocalSessionConnection | SshSessionConnection;

export interface SessionInformationDump {
  kind: typeof SESSION_INFORMATION_KIND;
  schema_version: number;
  generated_at: string;
  session: {
    id: string;
    title: string;
    agent_id: string;
    backend_workspace_id: string;
    parent_session_id: string | null;
    child_session_ids: string[];
    created_at: string;
    updated_at: string;
    storage_path: string;
  };
  workspace: {
    id: string;
    backend_workspace_id: string;
    name: string;
    root_path: string;
    active: boolean;
    status: GatewayWorkspace["status"];
    connection: SessionConnection;
  };
  execution: SessionInformationSnapshot["execution"];
  trace: SessionInformationSnapshot["trace"];
  resources: SessionInformationSnapshot["resources"];
  recent_errors: SessionInformationSnapshot["recent_errors"];
}

function normalizedPath(path: string): string {
  const trimmed = path.trim();
  if (trimmed === "/") {
    return trimmed;
  }
  return trimmed.replace(/[\\/]+$/, "");
}

function requiredRemoteString(
  remote: Record<string, unknown>,
  field: string,
): string {
  const value = remote[field];
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`SSH 工作区信息缺少 ${field}`);
  }
  return value;
}

function requiredRemotePort(
  remote: Record<string, unknown>,
  field: string,
): number {
  const value = remote[field];
  if (!Number.isInteger(value) || typeof value !== "number" || value <= 0) {
    throw new Error(`SSH 工作区信息缺少有效 ${field}`);
  }
  return value;
}

function buildConnection(workspace: GatewayWorkspace): SessionConnection {
  const common = {
    managed: workspace.managed,
    connection_error: workspace.connection_error ?? null,
  };
  if (workspace.connection_kind === "local") {
    return {
      kind: "local",
      backend_url: workspace.backend_url,
      ...common,
    };
  }

  return {
    kind: "ssh",
    host: requiredRemoteString(workspace.remote, "host"),
    port: requiredRemotePort(workspace.remote, "port"),
    username: requiredRemoteString(workspace.remote, "username"),
    remote_backend_host: requiredRemoteString(
      workspace.remote,
      "remote_backend_host",
    ),
    remote_backend_port: requiredRemotePort(
      workspace.remote,
      "remote_backend_port",
    ),
    tunnel_backend_url: workspace.backend_url,
    ...common,
  };
}

export function buildSessionInformationDump(
  information: SessionInformationSnapshot,
  gatewayWorkspace: GatewayWorkspace,
): SessionInformationDump {
  if (information.kind !== SESSION_INFORMATION_KIND) {
    throw new Error(`不支持的会话信息 kind: ${information.kind}`);
  }
  if (information.session.workspace_id !== information.workspace.workspace_id) {
    throw new Error(
      `会话信息中的工作区 ID 不一致: session=${information.session.workspace_id}, ` +
        `workspace=${information.workspace.workspace_id}`,
    );
  }
  if (
    normalizedPath(information.workspace.root_path) !==
    normalizedPath(gatewayWorkspace.root_path)
  ) {
    throw new Error(
      `Gateway 与工作区后端路径不一致: gateway=${gatewayWorkspace.root_path}, ` +
        `backend=${information.workspace.root_path}`,
    );
  }

  return {
    kind: information.kind,
    schema_version: information.schema_version ?? 1,
    generated_at: information.generated_at,
    session: {
      id: information.session.session_id,
      title: information.session.title,
      agent_id: information.session.current_agent_id,
      backend_workspace_id: information.session.workspace_id,
      parent_session_id: information.session.parent_session_id ?? null,
      child_session_ids: information.child_session_ids ?? [],
      created_at: information.session.created_at,
      updated_at: information.session.updated_at,
      storage_path: information.storage_path,
    },
    workspace: {
      id: gatewayWorkspace.workspace_id,
      backend_workspace_id: information.workspace.workspace_id,
      name: gatewayWorkspace.name,
      root_path: information.workspace.root_path,
      active: gatewayWorkspace.active,
      status: gatewayWorkspace.status,
      connection: buildConnection(gatewayWorkspace),
    },
    execution: information.execution,
    trace: information.trace,
    resources: information.resources ?? [],
    recent_errors: information.recent_errors ?? [],
  };
}

export function formatSessionInformationDump(
  information: SessionInformationDump,
): string {
  return JSON.stringify(information, null, 2);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isSessionId(value: unknown): value is string {
  return typeof value === "string" && /^ses_[A-Za-z0-9_-]+$/.test(value);
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

export function extractSessionIdFromClipboardText(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) {
    throw new Error("剪贴板内容为空");
  }
  if (isSessionId(trimmed)) {
    return trimmed;
  }

  const parseErrors: string[] = [];
  for (const candidate of jsonCandidates(trimmed)) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(candidate);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      parseErrors.push(message);
      continue;
    }
    if (!isRecord(parsed) || parsed.kind !== SESSION_INFORMATION_KIND) {
      parseErrors.push(`JSON kind 必须为 ${SESSION_INFORMATION_KIND}`);
      continue;
    }
    const session = parsed.session;
    if (!isRecord(session) || !isSessionId(session.id)) {
      throw new Error("通用会话信息缺少有效 session.id");
    }
    return session.id;
  }

  throw new Error(
    "剪贴板内容既不是会话 ID，也不是有效的通用会话信息 JSON: " +
      parseErrors.join("；"),
  );
}
