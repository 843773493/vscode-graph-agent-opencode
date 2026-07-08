// 该文件是前端业务类型适配层，封装后端实际返回结构。
// 本目录业务代码统一从这里导入类型；后端 DTO 直接别名到生成文件，避免手写协议漂移。

export type { AgentDTO as Agent } from "./gen/agent";
import type { LLMRequestLogRecordDTO } from "./gen/llm_request_log";
export type {
  AgentStateMessagesDTO as AgentStateMessages,
  AttachmentRef,
  MessageDTO as Message,
  MessageRunAccepted,
  MessageRunRequest,
  RunOptions,
} from "./gen/message";
export type {
  DeleteSessionResultDTO as DeleteSessionResult,
  SessionCompactResultDTO as SessionCompactResult,
  SessionDTO as Session,
  SessionInterruptResultDTO as InterruptSessionResult,
  SessionUpdateRequest,
} from "./gen/session";
import type {
  SessionResourceControlResultDTO,
  SessionResourceDTO,
  SessionResourceListDTO,
} from "./gen/session_resource";
import type { TraceEventDTO } from "./gen/trace";
export type {
  WorkspaceDTO as WorkspaceInfo,
  WorkspaceFileContentDTO as WorkspaceFileContent,
  WorkspaceFileListDTO as WorkspaceFileList,
  WorkspaceFileNodeDTO as WorkspaceFileNode,
} from "./gen/workspace";

export type LLMRequestLogRecord = Omit<
  LLMRequestLogRecordDTO,
  "request" | "response"
> & {
  request: Record<string, unknown>;
  response: Record<string, unknown>;
};

export type SessionResource = Omit<
  SessionResourceDTO,
  "available_actions" | "metadata"
> & {
  available_actions: NonNullable<SessionResourceDTO["available_actions"]>;
  metadata: Record<string, unknown>;
};

export type SessionResourceList = Omit<SessionResourceListDTO, "items"> & {
  items: SessionResource[];
};

export type SessionResourceControlResult = Omit<
  SessionResourceControlResultDTO,
  "resource"
> & {
  resource?: SessionResource | null;
};

type TraceRaw = NonNullable<TraceEventDTO["raw"]> & {
  payload?: Record<string, unknown>;
  session_id?: string;
  agent_id?: string | null;
  step_id?: string | null;
};

export interface TraceEvent
  extends Omit<TraceEventDTO, "session_id" | "phase" | "title" | "content" | "raw"> {
  session_id: string;
  phase?: TraceEventDTO["phase"];
  title?: string;
  content?: string;
  agent_id?: string | null;
  payload?: Record<string, unknown>;
  raw?: TraceRaw;
}

export interface APIResponse<T> {
  code: number;
  message: string;
  data: T | null;
  request_id?: string | null;
}

export interface CursorPage<T> {
  items: T[];
  next_cursor?: string | null;
  has_more?: boolean;
}

export type SessionResourceKind = SessionResourceDTO["kind"];
export type SessionResourceAction = NonNullable<SessionResourceDTO["available_actions"]>[number];
