import type {
  GatewayWorkspace,
  SessionInformationSnapshot,
} from "../../types/backend";
import { isRecord, jsonCandidates } from "../../utils/jsonDisplay";
import { errorMessage } from "../../utils/errorMessage";

export const SESSION_INFORMATION_KIND = "session_diagnostic_snapshot" as const;

interface LocalSessionConnection {
  kind: "local";
  backend_url: string;
  managed: boolean;
  connection_error: string | null;
}

interface RemoteGatewaySessionConnection {
  kind: "remote_gateway";
  gateway_connection_id: string;
  gateway_id: string;
  remote_workspace_id: string;
  gateway_proxy_url: string;
  managed: boolean;
  connection_error: string | null;
}

type SessionConnection =
  | LocalSessionConnection
  | RemoteGatewaySessionConnection;

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
    title_truncated: boolean;
    created_at: string;
    updated_at: string;
    storage_path: string;
  };
  relations: SessionInformationSnapshot["relations"];
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
  if (!workspace.remote) {
    throw new Error("远程 Gateway 工作区信息缺少连接摘要");
  }

  return {
    kind: "remote_gateway",
    gateway_connection_id: workspace.remote.gateway_connection_id,
    gateway_id: workspace.remote.gateway_id,
    remote_workspace_id: workspace.remote.remote_workspace_id,
    gateway_proxy_url: workspace.backend_url,
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
    schema_version: information.schema_version ?? 2,
    generated_at: information.generated_at,
    session: {
      id: information.session.session_id,
      title: information.session.title,
      agent_id: information.session.current_agent_id,
      backend_workspace_id: information.session.workspace_id,
      parent_session_id: information.session.parent_session_id ?? null,
      title_truncated: information.session.title_truncated ?? false,
      created_at: information.session.created_at,
      updated_at: information.session.updated_at,
      storage_path: information.storage_path,
    },
    relations: information.relations,
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
    resources: information.resources,
    recent_errors: information.recent_errors ?? [],
  };
}

export function formatSessionInformationDump(
  information: SessionInformationDump,
): string {
  return JSON.stringify(information, null, 2);
}

// 与后端唯一 canonical 验证器同口径（OpenSpec 2.1：ses_ + 32 位小写
// hex + UUIDv4 version/variant 位），不保留宽松旧形态。
const SESSION_ID_PATTERN = /^ses_[0-9a-f]{32}$/;

function isSessionId(value: unknown): value is string {
  if (typeof value !== "string" || !SESSION_ID_PATTERN.test(value)) {
    return false;
  }
  const payload = value.slice(4);
  return payload[12] === "4" && "89ab".includes(payload[16] ?? "");
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
      const message = errorMessage(error);
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
