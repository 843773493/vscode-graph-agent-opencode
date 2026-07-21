import type {
  Agent,
  AgentStateMessages,
  APIResponse,
  AttachmentRef,
  CursorPage,
  DeleteSessionResult,
  InterruptSessionResult,
  LLMRequestLogRecord,
  Message,
  MessageReplayAccepted,
  MessageReplayRequest,
  MessageRunAccepted,
  PendingRequestKind,
  MessageRunRequest,
  Session,
  SessionInformationSnapshot,
  SessionChangesSummary,
  SessionChangeset,
  SessionChangesetList,
  SessionFileChange,
  SessionCompactResult,
  SessionFileReviewResult,
  SessionResourceAction,
  SessionResourceControlResult,
  SessionResourceKind,
  SessionResourceList,
  SessionUpdateRequest,
  TraceEvent,
  WorkspaceFileContent,
  WorkspaceFileList,
  WorkspaceInfo,
  WorkspaceFileUpdateRequest,
} from "./types/backend";
import type {
  ToolCatalogItem,
  ToolSelectionChange,
  ToolTestRun,
  ToolTestRunList,
} from "./types/toolTesting";

export const DEFAULT_BACKEND_HOST = "127.0.0.1";
export const DEFAULT_BACKEND_PORT = 8014;
export const DEFAULT_AGENT_ID = "default";
export const DEFAULT_SESSION_TITLE = "新会话";
const AGENT_STATE_TIMEOUT_MS = 10000;
const SESSION_HISTORY_TIMEOUT_MS = 10000;

type RequestJsonInit = RequestInit & {
  timeoutMs?: number;
};

function normalizeHeaders(headers: HeadersInit | undefined): Record<string, string> {
  if (!headers) {
    return {};
  }
  if (headers instanceof Headers) {
    return Object.fromEntries(headers.entries());
  }
  if (Array.isArray(headers)) {
    return Object.fromEntries(headers);
  }
  return headers;
}

export function workspaceHeader(workspaceId?: string | null): Record<string, string> {
  return workspaceId ? { "X-BoxTeam-Workspace-Id": workspaceId } : {};
}

function getBaseUrl(port: number): string {
  if (typeof window !== "undefined" && window.location.port !== String(port)) {
    return "";
  }

  return `http://${DEFAULT_BACKEND_HOST}:${port}`;
}

const gatewayTokenByPort = new Map<number, Promise<string>>();

function gatewayToken(port: number): Promise<string> {
  const existing = gatewayTokenByPort.get(port);
  if (existing) {
    return existing;
  }
  const pending = fetch(
    `${getBaseUrl(port)}/api/gateway/auth/local-credential`,
  )
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`获取 Gateway 本地凭据失败: HTTP ${response.status}`);
      }
      const payload = await response.json() as APIResponse<{ token: string }>;
      const token = payload.data?.token;
      if (!token) {
        throw new Error("Gateway 本地凭据响应缺少 token");
      }
      return token;
    })
    .catch((error) => {
      gatewayTokenByPort.delete(port);
      throw error;
    });
  gatewayTokenByPort.set(port, pending);
  return pending;
}

export async function requestJson<T>(
  port: number,
  path: string,
  init?: RequestJsonInit,
): Promise<T> {
  const { timeoutMs, signal, headers, ...fetchInit } = init ?? {};
  const controller = timeoutMs ? new AbortController() : null;
  const timeoutErrorMessage = `请求超时: ${path}`;
  let timeoutId: number | null = null;
  const timeoutPromise = timeoutMs
    ? new Promise<Response>((_, reject) => {
        timeoutId = window.setTimeout(() => {
          controller?.abort();
          reject(new Error(timeoutErrorMessage));
        }, timeoutMs);
      })
    : null;

  try {
    const localToken = await gatewayToken(port);
    const fetchPromise = fetch(`${getBaseUrl(port)}${path}`, {
      ...fetchInit,
      headers: {
        "Content-Type": "application/json",
        "X-Local-Token": localToken,
        ...normalizeHeaders(headers),
      },
      signal: signal ?? controller?.signal,
    });
    const response = timeoutPromise
      ? await Promise.race([fetchPromise, timeoutPromise])
      : await fetchPromise;

    if (!response.ok) {
      const errorBody = await response.clone().json().catch(() => null) as {
        detail?: string;
        message?: string;
      } | null;
      const detail = errorBody?.detail ?? errorBody?.message;
      throw new Error(
        `请求失败 ${response.status} ${response.statusText}: ${detail ?? path}`,
      );
    }

    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof Error && error.message === timeoutErrorMessage) {
      throw error;
    }
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error(timeoutErrorMessage);
    }
    throw error;
  } finally {
    if (timeoutId !== null) {
      window.clearTimeout(timeoutId);
    }
  }
}

export async function getSessionAttachmentBlob(
  port: number,
  sessionId: string,
  fileId: string,
  workspaceId?: string | null,
): Promise<Blob> {
  const localToken = await gatewayToken(port);
  const query = new URLSearchParams({ file_id: fileId });
  const response = await fetch(
    `${getBaseUrl(port)}/api/v1/sessions/${encodeURIComponent(sessionId)}/attachments/content?${query}`,
    {
      headers: {
        "X-Local-Token": localToken,
        ...workspaceHeader(workspaceId),
      },
    },
  );
  if (!response.ok) {
    const payload = await response.clone().json().catch(() => null) as {
      detail?: string;
    } | null;
    throw new Error(
      `读取消息附件失败: ${payload?.detail ?? `HTTP ${response.status}`}`,
    );
  }
  return response.blob();
}

export async function getToolCatalog(
  port: number,
  agentId: string,
  workspaceId?: string | null,
): Promise<ToolCatalogItem[]> {
  const query = new URLSearchParams({ agent_id: agentId });
  return unwrapApiData(
    await requestJson<APIResponse<ToolCatalogItem[]>>(
      port,
      `/api/v1/tools?${query.toString()}`,
      { headers: workspaceHeader(workspaceId) },
    ),
  );
}

export async function updateToolSelection(
  port: number,
  agentId: string,
  changes: ToolSelectionChange[],
  workspaceId?: string | null,
): Promise<ToolCatalogItem[]> {
  return unwrapApiData(
    await requestJson<APIResponse<ToolCatalogItem[]>>(
      port,
      "/api/v1/tools/selection",
      {
        method: "PATCH",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify({
          agent_id: agentId,
          changes,
        }),
      },
    ),
  );
}

export async function startToolTest(
  port: number,
  toolId: string,
  agentId: string,
  workspaceId?: string | null,
): Promise<ToolTestRun> {
  return unwrapApiData(
    await requestJson<APIResponse<ToolTestRun>>(
      port,
      `/api/v1/tools/${encodeURIComponent(toolId)}/tests`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify({
          agent_id: agentId,
          provider_ids: [],
        }),
      },
    ),
  );
}

export async function getToolTestRun(
  port: number,
  runId: string,
  workspaceId?: string | null,
): Promise<ToolTestRun> {
  return unwrapApiData(
    await requestJson<APIResponse<ToolTestRun>>(
      port,
      `/api/v1/tools/tests/${encodeURIComponent(runId)}`,
      { headers: workspaceHeader(workspaceId) },
    ),
  );
}

export async function listToolTestRuns(
  port: number,
  workspaceId?: string | null,
): Promise<ToolTestRun[]> {
  const result = unwrapApiData(
    await requestJson<APIResponse<ToolTestRunList>>(
      port,
      "/api/v1/tools/tests?limit=50",
      { headers: workspaceHeader(workspaceId) },
    ),
  );
  return result.items;
}

export function unwrapApiData<T>(response: APIResponse<T>): T {
  if (typeof response.request_id !== "string" || !response.request_id) {
    throw new Error("后端响应缺少 request_id");
  }
  if (response.data == null) {
    throw new Error(
      `后端响应缺少 data 字段: ${response.message || "unknown message"}`,
    );
  }

  return response.data;
}

function normalizeSessionChangesSummary(
  summary: Partial<SessionChangesSummary> | null | undefined,
): SessionChangesSummary {
  return {
    files: summary?.files ?? 0,
    additions: summary?.additions ?? 0,
    deletions: summary?.deletions ?? 0,
  };
}

function normalizeSessionFileChange(file: SessionFileChange): SessionFileChange {
  return {
    ...file,
    additions: file.additions ?? 0,
    deletions: file.deletions ?? 0,
    reviewed: file.reviewed ?? false,
    tool_call_ids: file.tool_call_ids ?? [],
    turn_ids: file.turn_ids ?? [],
  };
}

function normalizeSessionChangesetList(
  value: SessionChangesetList,
): SessionChangesetList {
  return {
    ...value,
    items: value.items.map((item) => ({
      ...item,
      is_default: item.is_default ?? false,
      summary: normalizeSessionChangesSummary(item.summary),
    })),
  };
}

function normalizeSessionChangeset(value: SessionChangeset): SessionChangeset {
  return {
    ...value,
    status: value.status ?? "ready",
    summary: normalizeSessionChangesSummary(value.summary),
    files: (value.files ?? []).map(normalizeSessionFileChange),
  };
}

function normalizePageResult<T>(value: unknown): CursorPage<T> {
  if (!value || typeof value !== "object") {
    return { items: [] };
  }

  const record = value as {
    items?: T[];
    next_cursor?: string | null;
    has_more?: boolean;
  };
  return {
    items: Array.isArray(record.items) ? record.items : [],
    next_cursor: record.next_cursor ?? null,
    has_more:
      typeof record.has_more === "boolean" ? record.has_more : undefined,
  };
}

export async function getWorkspace(
  port: number,
  workspaceId?: string | null,
): Promise<WorkspaceInfo> {
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceInfo>>(
      port,
      "/api/v1/workspace",
      workspaceId ? { headers: workspaceHeader(workspaceId) } : undefined,
    ),
  );
}

export async function getWorkspaceFiles(
  port: number,
  path: string = "",
  workspaceId?: string | null,
): Promise<WorkspaceFileList> {
  const query = new URLSearchParams();
  if (path) {
    query.set("path", path);
  }

  const suffix = query.toString();
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceFileList>>(
      port,
      `/api/v1/workspace/files${suffix ? `?${suffix}` : ""}`,
      workspaceId ? { headers: workspaceHeader(workspaceId) } : undefined,
    ),
  );
}

export async function getWorkspaceFileContent(
  port: number,
  path: string,
  workspaceId?: string | null,
): Promise<WorkspaceFileContent> {
  const query = new URLSearchParams({ path });
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceFileContent>>(
      port,
      `/api/v1/workspace/files/content?${query.toString()}`,
      workspaceId ? { headers: workspaceHeader(workspaceId) } : undefined,
    ),
  );
}

export async function updateWorkspaceFileContent(
  port: number,
  path: string,
  payload: WorkspaceFileUpdateRequest,
  workspaceId?: string | null,
): Promise<WorkspaceFileContent> {
  const query = new URLSearchParams({ path });
  return unwrapApiData(
    await requestJson<APIResponse<WorkspaceFileContent>>(
      port,
      `/api/v1/workspace/files/content?${query.toString()}`,
      {
        method: "PUT",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function listSessions(
  port: number,
  workspaceId?: string | null,
): Promise<CursorPage<Session>> {
  const data = await requestJson<APIResponse<CursorPage<Session>>>(
    port,
    "/api/v1/sessions",
    workspaceId
      ? {
          headers: workspaceHeader(workspaceId),
        }
      : undefined,
  );
  return normalizePageResult<Session>(unwrapApiData(data));
}

export async function getSession(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<Session> {
  return unwrapApiData(
    await requestJson<APIResponse<Session>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      workspaceId ? { headers: workspaceHeader(workspaceId) } : undefined,
    ),
  );
}

export async function getSessionInformation(
  port: number,
  sessionId: string,
  workspaceId: string,
): Promise<SessionInformationSnapshot> {
  return unwrapApiData(
    await requestJson<APIResponse<SessionInformationSnapshot>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/information`,
      { headers: workspaceHeader(workspaceId) },
    ),
  );
}

export async function listAgents(
  port: number,
  workspaceId?: string | null,
): Promise<Agent[]> {
  return unwrapApiData(
    await requestJson<APIResponse<Agent[]>>(
      port,
      "/api/v1/agents",
      workspaceId ? { headers: workspaceHeader(workspaceId) } : undefined,
    ),
  );
}

export async function createSession(
  port: number,
  title: string = DEFAULT_SESSION_TITLE,
  workspaceId?: string | null,
): Promise<Session> {
  return unwrapApiData(
    await requestJson<APIResponse<Session>>(port, "/api/v1/sessions", {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ title }),
    }),
  );
}

export async function forkSessionContext(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<Session> {
  return unwrapApiData(
    await requestJson<APIResponse<Session>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/fork-context`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
      },
    ),
  );
}

export async function updateSession(
  port: number,
  sessionId: string,
  payload: SessionUpdateRequest,
  workspaceId?: string | null,
): Promise<Session> {
  return unwrapApiData(
    await requestJson<APIResponse<Session>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "PATCH",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function deleteSession(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<DeleteSessionResult> {
  return unwrapApiData(
    await requestJson<APIResponse<DeleteSessionResult>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      { method: "DELETE", headers: workspaceHeader(workspaceId) },
    ),
  );
}

export function updateSessionAgent(
  port: number,
  sessionId: string,
  agentId: string,
  workspaceId?: string | null,
): Promise<Session> {
  return updateSession(port, sessionId, { agent_id: agentId }, workspaceId);
}

export async function compactSessionContext(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<SessionCompactResult> {
  return unwrapApiData(
    await requestJson<APIResponse<SessionCompactResult>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/compact`,
      { method: "POST", headers: workspaceHeader(workspaceId) },
    ),
  );
}

export async function listMessages(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<CursorPage<Message>> {
  const data = await requestJson<APIResponse<CursorPage<Message>>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
    {
      headers: workspaceHeader(workspaceId),
      timeoutMs: SESSION_HISTORY_TIMEOUT_MS,
    },
  );
  return normalizePageResult<Message>(unwrapApiData(data));
}

export async function getAgentStateMessages(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<AgentStateMessages> {
  const data = await requestJson<APIResponse<AgentStateMessages>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/agent-state/messages`,
    {
      headers: workspaceHeader(workspaceId),
      timeoutMs: AGENT_STATE_TIMEOUT_MS,
    },
  );
  return unwrapApiData(data);
}

export async function getLLMRequestLogs(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<LLMRequestLogRecord[]> {
  const data = await requestJson<APIResponse<LLMRequestLogRecord[]>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/llm-request-logs`,
    { headers: workspaceHeader(workspaceId) },
  );
  return unwrapApiData(data);
}

export async function getSessionResources(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<SessionResourceList> {
  const data = await requestJson<APIResponse<SessionResourceList>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/resources`,
    { headers: workspaceHeader(workspaceId) },
  );
  return unwrapApiData(data);
}

export async function getSessionChangesets(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<SessionChangesetList> {
  const data = await requestJson<APIResponse<SessionChangesetList>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/changesets`,
    { headers: workspaceHeader(workspaceId) },
  );
  return normalizeSessionChangesetList(unwrapApiData(data));
}

export async function getSessionChangeset(
  port: number,
  sessionId: string,
  changesetId: string,
  workspaceId?: string | null,
): Promise<SessionChangeset> {
  const data = await requestJson<APIResponse<SessionChangeset>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/changesets/${encodeURIComponent(changesetId)}`,
    { headers: workspaceHeader(workspaceId) },
  );
  return normalizeSessionChangeset(unwrapApiData(data));
}

export async function reviewSessionChangeFile(
  port: number,
  sessionId: string,
  changesetId: string,
  filePath: string,
  reviewed: boolean,
  workspaceId?: string | null,
): Promise<SessionFileReviewResult> {
  const data = await requestJson<APIResponse<SessionFileReviewResult>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/changesets/${encodeURIComponent(changesetId)}/review`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ file_path: filePath, reviewed }),
    },
  );
  return unwrapApiData(data);
}

export async function controlSessionResource(
  port: number,
  sessionId: string,
  kind: SessionResourceKind,
  resourceId: string,
  action: SessionResourceAction,
  workspaceId?: string | null,
): Promise<SessionResourceControlResult> {
  const data = await requestJson<APIResponse<SessionResourceControlResult>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/resources/${encodeURIComponent(kind)}/${encodeURIComponent(resourceId)}/control`,
    {
      method: "POST",
      headers: workspaceHeader(workspaceId),
      body: JSON.stringify({ action }),
    },
  );
  return unwrapApiData(data);
}

export async function sendMessage(
  port: number,
  sessionId: string,
  payload: MessageRunRequest,
  workspaceId?: string | null,
): Promise<MessageRunAccepted> {
  const accepted = unwrapApiData(
    await requestJson<APIResponse<MessageRunAccepted>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  );
  if (typeof accepted.message_id !== "string" || !accepted.message_id) {
    throw new Error("发送消息响应缺少 message_id");
  }
  if (typeof accepted.job_id !== "string" || !accepted.job_id) {
    throw new Error("发送消息响应缺少 job_id");
  }
  return accepted;
}

export async function sendUserMessage(
  port: number,
  sessionId: string,
  content: string,
  agentId: string = DEFAULT_AGENT_ID,
  attachments: AttachmentRef[] = [],
  workspaceId?: string | null,
  queue?: PendingRequestKind | null,
): Promise<MessageRunAccepted> {
  const payload: MessageRunRequest = {
    message: {
      role: "user",
      content,
      attachments,
      metadata: {},
    },
    run: {
      mode: "single_agent",
      agent_id: agentId,
      response_mode: "stream",
      async: true,
      queue: queue ?? undefined,
    },
  };

  return sendMessage(port, sessionId, payload, workspaceId);
}

export async function replayMessageTurn(
  port: number,
  sessionId: string,
  messageId: string,
  payload: MessageReplayRequest,
  workspaceId?: string | null,
): Promise<MessageReplayAccepted> {
  return unwrapApiData(
    await requestJson<APIResponse<MessageReplayAccepted>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/replay`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function interruptSession(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
): Promise<InterruptSessionResult> {
  return unwrapApiData(
    await requestJson<APIResponse<InterruptSessionResult>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/interrupt`,
      { method: "POST", headers: workspaceHeader(workspaceId) },
    ),
  );
}

export async function getSessionTraces(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
  afterEventId?: string | null,
): Promise<TraceEvent[]> {
  const query = afterEventId
    ? `?${new URLSearchParams({ after_event_id: afterEventId }).toString()}`
    : "";
  const result = await requestJson<APIResponse<TraceEvent[]>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/traces${query}`,
    {
      headers: workspaceHeader(workspaceId),
      timeoutMs: SESSION_HISTORY_TIMEOUT_MS,
    },
  );
  return unwrapApiData(result);
}

export interface SessionStreamEvent {
  event_id: string;
  part_id?: string | null;
  session_id: string;
  job_id: string;
  step_id: string | null;
  agent_id: string | null;
  timestamp: string;
  type: string;
  payload?: Record<string, unknown>;
  /** 后端 DTO 格式可能将真实事件数据嵌套在 raw 中 */
  raw?: Record<string, unknown>;
}

export class TraceCursorGoneError extends Error {
  readonly status = 410;

  constructor(readonly eventId: string) {
    super(`Trace 事件游标已失效: ${eventId}`);
    this.name = "TraceCursorGoneError";
  }
}

function parseSseBlock(block: string): SessionStreamEvent | null {
  let eventType = "message";
  let eventId = "";
  const dataLines: string[] = [];

  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) {
      continue;
    }

    if (line.startsWith("event:")) {
      eventType = line.slice(6).trim();
      continue;
    }

    if (line.startsWith("id:")) {
      eventId = line.slice(3).trim();
      continue;
    }

    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  const data = dataLines.join("\n");
  if (!data) {
    return null;
  }

  if (eventType !== "trace") {
    throw new Error(`SSE 事件类型错误: ${eventType}`);
  }
  if (!eventId) {
    throw new Error("SSE trace 缺少 id 行");
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(
      `SSE trace JSON 无法解析: event_id=${eventId} error=${detail}`,
    );
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`SSE trace data 必须是对象: event_id=${eventId}`);
  }
  const payload = parsed as Partial<SessionStreamEvent>;
  if (payload.event_id !== eventId) {
    throw new Error(
      `SSE trace event_id 不一致: transport=${eventId} payload=${String(payload.event_id)}`,
    );
  }
  for (const [field, value] of [
    ["session_id", payload.session_id],
    ["job_id", payload.job_id],
    ["timestamp", payload.timestamp],
    ["type", payload.type],
  ] as const) {
    if (typeof value !== "string" || !value) {
      throw new Error(`SSE trace 缺少 ${field}: event_id=${eventId}`);
    }
  }
  return payload as SessionStreamEvent;
}

export async function streamSessionEvents(
  port: number,
  sessionId: string,
  options?: {
    workspaceId?: string | null;
    afterEventId?: string | null;
    onEvent?: (event: SessionStreamEvent) => void;
    onError?: (error: unknown) => void;
    signal?: AbortSignal;
  },
): Promise<void> {
  const url = `${getBaseUrl(port)}/api/v1/sessions/${encodeURIComponent(sessionId)}/traces/stream`;
  const localToken = await gatewayToken(port);
  const response = await fetch(url, {
    signal: options?.signal,
    headers: {
      accept: "text/event-stream",
      "X-Local-Token": localToken,
      ...workspaceHeader(options?.workspaceId),
      ...(options?.afterEventId
        ? { "Last-Event-ID": options.afterEventId }
        : {}),
    },
  });

  if (response.status === 410) {
    throw new TraceCursorGoneError(options?.afterEventId ?? "");
  }
  if (!response.ok || !response.body) {
    throw new Error(
      `无法连接会话事件流: ${response.status} ${response.statusText}`,
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });

      let boundaryIndex = buffer.indexOf("\n\n");
      while (boundaryIndex !== -1) {
        const block = buffer.slice(0, boundaryIndex).trim();
        buffer = buffer.slice(boundaryIndex + 2);
        boundaryIndex = buffer.indexOf("\n\n");

        if (!block) {
          continue;
        }

        const parsed = parseSseBlock(block);
        if (parsed) {
          options?.onEvent?.(parsed);
        }
      }

      if (done) {
        break;
      }
    }
  } catch (error) {
    if (options?.signal?.aborted) {
      return;
    }

    options?.onError?.(error);
    throw error;
  } finally {
    reader.releaseLock();
  }
}
