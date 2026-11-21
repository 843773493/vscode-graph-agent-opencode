import type {
  AgentStateMessages,
  APIResponse,
  AttachmentRef,
  CursorPage,
  InterruptSessionResult,
  LLMRequestLogRecord,
  LLMRequestLogSummaryDTO,
  Message,
  MessageReplayAccepted,
  MessageReplayRequest,
  MessageRunAccepted,
  MessageRunRequest,
  DeliveryPolicy,
} from "../../types/backend";
import type { LLMRequestLogReferenceDTO, LLMRequestLogLocationDTO, TraceLocationDTO, TraceReferenceDTO } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import type { TraceEvent } from "../../types/backend";
import { HttpRequestError } from "../http";
import {
  freezeMessageSubmission, loadMessageSubmissions, rejectMessageSubmission, releaseMessageSubmission,
  type FrozenMessageSubmission, type MessageSubmissionOwner,
} from "../../state/messageSubmissions/storage";
import type { MessageSubmissionResultDTO as GeneratedSubmissionResult } from "../../types/protocol_generated/boxteam/workspace/v2/public";
import { getWorkspaceRawFileBlob } from "../workspaceFilesystem";
import { truncateDisplayText } from "../../utils/displayText";
type MessageSubmissionResult = Omit<GeneratedSubmissionResult, "accepted"> & { accepted?: MessageRunAccepted | null };
import {
  DEFAULT_API_REQUEST_TIMEOUT_MS,
  normalizePageResult,
  requestGatewayResponse,
  requestJson,
  unwrapApiData,
  workspaceHeader,
} from "../http";

export const DEFAULT_AGENT_ID = "default";

export async function uploadSessionAttachment(
  port: number, sessionId: string, threadId: string, workspaceId: string,
  file: Blob, name: string, contentType: string, signal?: AbortSignal,
): Promise<AttachmentRef> {
  if (file.size > 30 * 1024 * 1024) throw new Error("附件超过 30 MiB 限制");
  const response = await requestGatewayResponse(port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/threads/${encodeURIComponent(threadId)}/attachments?${new URLSearchParams({ name })}`,
    { method: "POST", body: file, signal,
      headers: { ...workspaceHeader(workspaceId), "Content-Type": contentType } },
  );
  const { data_url: _inlineBytes, ...reference } = unwrapApiData(await response.json() as APIResponse<AttachmentRef>);
  return reference;
}

export async function discardSessionAttachment(
  port: number, sessionId: string, threadId: string, workspaceId: string, fileId: string,
): Promise<void> {
  await requestJson(port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/threads/${encodeURIComponent(threadId)}/attachments?${new URLSearchParams({ file_id: fileId })}`,
    { method: "DELETE", headers: workspaceHeader(workspaceId) },
  );
}

export type LocatedTrace = Omit<TraceLocationDTO, "record"> & { record: TraceEvent };

export async function locateTrace(
  apiPort: number, reference: TraceReferenceDTO, workspaceId: string, signal?: AbortSignal,
): Promise<LocatedTrace> {
  return unwrapApiData(await requestJson<APIResponse<LocatedTrace>>(
    apiPort, `/api/v1/sessions/${encodeURIComponent(reference.session_id)}/traces/locate`,
    { method: "POST", body: JSON.stringify(reference), headers: workspaceHeader(workspaceId), signal, timeoutMs: 10_000 },
  ));
}

export type LocatedLLMRequestLog = Omit<LLMRequestLogLocationDTO, "record"> & { record: LLMRequestLogRecord };

export async function locateLLMRequestLog(
  apiPort: number, reference: LLMRequestLogReferenceDTO, workspaceId: string, signal?: AbortSignal,
): Promise<LocatedLLMRequestLog> {
  return unwrapApiData(await requestJson<APIResponse<LocatedLLMRequestLog>>(
    apiPort, `/api/v1/sessions/${encodeURIComponent(reference.session_id)}/llm-request-logs/locate`,
    { method: "POST", body: JSON.stringify(reference), headers: workspaceHeader(workspaceId), signal, timeoutMs: 10_000 },
  ));
}

const AGENT_STATE_TIMEOUT_MS = 10000;
const SESSION_HISTORY_TIMEOUT_MS = 10000;
// 列表只读取目录摘要；单条完整正文沿用有界请求超时。
const LLM_REQUEST_LOG_TIMEOUT_MS = DEFAULT_API_REQUEST_TIMEOUT_MS;
const ATTACHMENT_TEXT_DECODE_CHUNK_BYTES = 16 * 1024;

interface SessionAttachmentTextPreview {
  text: string;
  truncated: boolean;
}

async function requestSessionAttachmentContent(
  port: number,
  sessionId: string,
  fileId: string,
  workspaceId: string | null | undefined,
  options: {
    variant?: "thumbnail" | "original";
    signal?: AbortSignal;
  },
): Promise<Response> {
  const query = new URLSearchParams({ file_id: fileId });
  query.set("variant", options.variant ?? "original");
  if (options.variant === "thumbnail") {
    query.set("max_edge", "512");
  }
  // 附件内容复用统一本机凭据及一次有界刷新重试。
  return await requestGatewayResponse(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/attachments/content?${query}`,
    {
      headers: workspaceHeader(workspaceId),
      signal: options.signal,
    },
  );
}

async function readSessionAttachmentTextPreview(
  response: Response,
  maxCodeUnits: number,
): Promise<SessionAttachmentTextPreview> {
  const reader = response.body?.getReader();
  if (!reader) return { text: "", truncated: false };

  const decoder = new TextDecoder();
  const textParts: string[] = [];
  let codeUnitCount = 0;
  const append = (decoded: string): boolean => {
    const remainingCodeUnits = maxCodeUnits - codeUnitCount;
    if (decoded.length > remainingCodeUnits) {
      const visiblePart = truncateDisplayText(decoded, remainingCodeUnits);
      if (visiblePart) textParts.push(visiblePart);
      codeUnitCount += visiblePart.length;
      return true;
    }
    if (decoded) textParts.push(decoded);
    codeUnitCount += decoded.length;
    return false;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        const truncated = append(decoder.decode());
        return { text: textParts.join(""), truncated };
      }
      for (let offset = 0; offset < value.length; offset += ATTACHMENT_TEXT_DECODE_CHUNK_BYTES) {
        const decoded = decoder.decode(
          value.subarray(offset, offset + ATTACHMENT_TEXT_DECODE_CHUNK_BYTES),
          { stream: true },
        );
        if (append(decoded)) {
          await reader.cancel();
          return { text: textParts.join(""), truncated: true };
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export async function getSessionAttachmentBlob(
  port: number,
  sessionId: string,
  fileId: string,
  workspaceId?: string | null,
  options: {
    variant?: "thumbnail" | "original";
    signal?: AbortSignal;
  } = {},
): Promise<Blob> {
  const response = await requestSessionAttachmentContent(
    port,
    sessionId,
    fileId,
    workspaceId,
    options,
  );
  return await response.blob();
}

export async function getSessionAttachmentTextPreview(
  port: number,
  sessionId: string,
  fileId: string,
  workspaceId: string | null | undefined,
  options: {
    maxCodeUnits: number;
    signal?: AbortSignal;
  },
): Promise<SessionAttachmentTextPreview> {
  if (!Number.isSafeInteger(options.maxCodeUnits) || options.maxCodeUnits < 0) {
    throw new RangeError(`附件文本预览长度必须是非负安全整数: ${options.maxCodeUnits}`);
  }
  const response = await requestSessionAttachmentContent(
    port,
    sessionId,
    fileId,
    workspaceId,
    { variant: "original", signal: options.signal },
  );
  return await readSessionAttachmentTextPreview(response, options.maxCodeUnits);
}

export async function listMessages(
  port: number,
  sessionId: string,
  workspaceId?: string | null,
  options: { cursor?: string | null; limit?: number; signal?: AbortSignal } = {},
): Promise<CursorPage<Message>> {
  const params = new URLSearchParams();
  params.set("limit", String(options.limit ?? 40));
  if (options.cursor) {
    params.set("cursor", options.cursor);
  }
  const data = await requestJson<APIResponse<CursorPage<Message>>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages?${params.toString()}`,
    {
      headers: workspaceHeader(workspaceId),
      timeoutMs: SESSION_HISTORY_TIMEOUT_MS,
      signal: options.signal,
    },
  );
  return normalizePageResult<Message>(unwrapApiData(data), "会话消息列表");
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
  signal?: AbortSignal,
): Promise<LLMRequestLogSummaryDTO[]> {
  const data = await requestJson<APIResponse<LLMRequestLogSummaryDTO[]>>(
    port,
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/llm-request-logs`,
    {
      headers: workspaceHeader(workspaceId),
      timeoutMs: LLM_REQUEST_LOG_TIMEOUT_MS,
      signal,
    },
  );
  return unwrapApiData(data);
}

export async function getLLMRequestLog(
  port: number, sessionId: string, fileName: string, workspaceId: string | null, signal?: AbortSignal,
): Promise<LLMRequestLogRecord> {
  return unwrapApiData(await requestJson<APIResponse<LLMRequestLogRecord>>(
    port, `/api/v1/sessions/${encodeURIComponent(sessionId)}/llm-request-logs/${encodeURIComponent(fileName)}`,
    { headers: workspaceHeader(workspaceId), signal, timeoutMs: LLM_REQUEST_LOG_TIMEOUT_MS },
  ));
}

function validateAcceptedMessage(accepted: MessageRunAccepted, action: string): void {
  if (typeof accepted.message_id !== "string" || !accepted.message_id) {
    throw new Error(`${action}响应缺少 message_id`);
  }
  if (typeof accepted.job_id !== "string" || !accepted.job_id) {
    throw new Error(`${action}响应缺少 job_id`);
  }
  if (typeof accepted.turn_id !== "string" || !accepted.turn_id || accepted.turn_id === accepted.job_id) {
    throw new Error(`${action}响应缺少独立的 turn_id`);
  }
  if (typeof accepted.thread_id !== "string" || !accepted.thread_id) {
    throw new Error(`${action}响应缺少 thread_id`);
  }
  const dispatch = accepted.dispatch;
  if (!dispatch || dispatch.thread_id !== accepted.thread_id || dispatch.job_id !== accepted.job_id
    || dispatch.job_status !== accepted.status || !dispatch.session_id
    || typeof dispatch.queue_snapshot_version !== "number" || !Number.isSafeInteger(dispatch.queue_snapshot_version)
    || dispatch.queue_snapshot_version < 0) {
    throw new Error(`${action}响应缺少对应 Job／Thread 的权威队列水位`);
  }
}

export async function sendMessage(
  port: number,
  sessionId: string,
  payload: MessageRunRequest,
  workspaceId?: string | null,
  threadId?: string,
): Promise<MessageRunAccepted> {
  const owner = `/api/v1/sessions/${encodeURIComponent(sessionId)}`;
  const accepted = unwrapApiData(
    await requestJson<APIResponse<MessageRunAccepted>>(
      port,
      threadId === undefined
        ? `${owner}/messages`
        : `${owner}/threads/${encodeURIComponent(threadId)}/messages`,
      {
        method: "POST",
        headers: workspaceHeader(workspaceId),
        body: JSON.stringify(payload),
      },
    ),
  );
  validateAcceptedMessage(accepted, "发送消息");
  if (accepted.operation_id !== payload.operation_id || accepted.dispatch.session_id !== sessionId
    || (threadId !== undefined && accepted.thread_id !== threadId)) {
    throw new Error("消息接受结果与原操作身份或 Thread 不一致");
  }
  return accepted;
}

export async function queryMessageSubmission(port: number, owner: MessageSubmissionOwner, operationId: string, signal?: AbortSignal) {
  const result = unwrapApiData(await requestJson<APIResponse<MessageSubmissionResult>>(port,
    `/api/v1/sessions/${encodeURIComponent(owner.sessionId)}/threads/${encodeURIComponent(owner.threadId)}/message-submissions/${encodeURIComponent(operationId)}`,
    { headers: workspaceHeader(owner.workspaceKey), signal },
  ));
  if (result.session_id !== owner.sessionId || result.thread_id !== owner.threadId || result.operation_id !== operationId) {
    throw new Error("消息操作查询返回了另一归属");
  }
  if (result.status === "accepted") {
    if (!result.accepted) throw new Error("已接收的消息操作缺少权威身份");
    validateAcceptedMessage(result.accepted, "消息操作查询");
    if (result.accepted.operation_id !== operationId || result.accepted.thread_id !== owner.threadId
      || result.accepted.dispatch.session_id !== owner.sessionId) {
      throw new Error("消息操作回执身份不一致");
    }
  } else if (result.status !== "unknown" || result.accepted) {
    throw new Error("消息操作查询状态非法");
  }
  return result;
}

export class MessageSubmissionUnknownError extends Error {
  constructor(readonly operationId: string, cause: unknown) {
    super(`消息接收结果尚未确认，已保留原输入：${cause instanceof Error ? cause.message : String(cause)}`, { cause });
  }
}

export class MessageSubmissionRejectedError extends Error {
  constructor(readonly operationId: string, cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause), { cause });
  }
}

/** 只按冻结输入提交；网络失败先查原身份，不把队列缺项当作未接收。 */
export async function submitFrozenMessage(port: number, record: FrozenMessageSubmission): Promise<MessageRunAccepted> {
  if (record.state === "rejected") throw new MessageSubmissionRejectedError(record.request.operation_id, record.error ?? "消息已被拒绝，请编辑后重新提交");
  const { owner, request } = record;
  let accepted: MessageRunAccepted;
  try {
    accepted = await sendMessage(port, owner.sessionId, request, owner.workspaceKey, owner.threadId);
  } catch (error) {
    let result: Awaited<ReturnType<typeof queryMessageSubmission>>;
    try {
      result = await queryMessageSubmission(port, owner, request.operation_id);
    } catch (queryError) {
      throw new MessageSubmissionUnknownError(request.operation_id, queryError);
    }
    if (result.status === "accepted" && result.accepted) accepted = result.accepted;
    else if (error instanceof HttpRequestError && [400, 422].includes(error.status)) {
      // 消息入口在准入前拒绝且原身份查询未接收，才允许用户编辑为新操作。
      try { await rejectMessageSubmission(record, error.message); }
      catch (storageError) { throw new MessageSubmissionUnknownError(request.operation_id, storageError); }
      throw new MessageSubmissionRejectedError(request.operation_id, error);
    } else throw new MessageSubmissionUnknownError(request.operation_id, error);
  }
  // 接收已确定时，清理失败仍按原身份恢复，不能让 Composer 生成新操作重发。
  try { await releaseMessageSubmission(record); }
  catch (error) { throw new MessageSubmissionUnknownError(request.operation_id, error); }
  return accepted;
}

/** 刷新只对账原操作；未知结果保留，不用新身份补发，拒绝项不自动发送。 */
export async function reconcileMessageSubmissions(port: number, owner: MessageSubmissionOwner, signal?: AbortSignal) {
  const records = await loadMessageSubmissions(owner);
  signal?.throwIfAborted();
  const results: Array<{ record: FrozenMessageSubmission; accepted: MessageRunAccepted | null }> = [];
  for (const record of records) {
    signal?.throwIfAborted();
    if (record.state === "rejected") { results.push({ record, accepted: null }); continue; }
    const result = await queryMessageSubmission(port, owner, record.request.operation_id, signal);
    signal?.throwIfAborted();
    if (result.status === "accepted" && result.accepted) {
      await releaseMessageSubmission(record);
      results.push({ record, accepted: result.accepted });
    } else results.push({ record, accepted: null });
  }
  return results;
}

export async function sendUserMessage(
  port: number,
  sessionId: string,
  content: string,
  agentId: string = DEFAULT_AGENT_ID,
  attachments: AttachmentRef[] = [],
  workspaceId?: string | null,
  deliveryPolicy: DeliveryPolicy = "after_turn",
  threadId?: string,
  operationId = `op_${crypto.randomUUID().replaceAll("-", "")}`,
): Promise<MessageRunAccepted> {
  if (!workspaceId || !threadId) throw new Error("可靠消息提交缺少固定工作区或 Thread 身份");
  // 消息提交只冻结稳定引用；本机粘贴内容和可变文件先走同一原始字节上传入口。
  const frozenAttachments: AttachmentRef[] = [];
  for (const attachment of structuredClone(attachments)) {
    if (attachment.directory_uri && !attachment.data_url) { frozenAttachments.push(attachment); continue; }
    const blob = attachment.data_url?.startsWith("data:")
      ? await (await fetch(attachment.data_url)).blob()
      : await getWorkspaceRawFileBlob(port, attachment.file_id, workspaceId);
    frozenAttachments.push(await uploadSessionAttachment(port, sessionId, threadId, workspaceId,
      blob, attachment.name ?? "attachment", attachment.content_type ?? (blob.type || "application/octet-stream")));
  }
  const payload: MessageRunRequest = {
    operation_id: operationId,
    message: {
      role: "user",
      content,
      attachments: frozenAttachments,
      metadata: {},
    },
    run: {
      mode: "single_agent",
      agent_id: agentId,
      response_mode: "stream",
      async: true,
      delivery_policy: deliveryPolicy,
    },
  };

  const frozen = await freezeMessageSubmission({ workspaceKey: workspaceId, sessionId, threadId }, payload);
  return submitFrozenMessage(port, frozen);
}

export async function replayMessageTurn(
  port: number,
  sessionId: string,
  messageId: string,
  payload: MessageReplayRequest,
  workspaceId?: string | null,
): Promise<MessageReplayAccepted> {
  const accepted = unwrapApiData(
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
  validateAcceptedMessage(accepted, "重放消息");
  return accepted;
}

export async function interruptSession(
  port: number,
  sessionId: string,
  threadId: string,
  workspaceId?: string | null,
): Promise<InterruptSessionResult> {
  const result = unwrapApiData(
    await requestJson<APIResponse<InterruptSessionResult>>(
      port,
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/interrupt?${new URLSearchParams({ thread_id: threadId })}`,
      { method: "POST", headers: workspaceHeader(workspaceId) },
    ),
  );
  if (result.session_id !== sessionId || result.thread_id !== threadId) {
    throw new Error(`中断响应归属不匹配: 请求 ${sessionId}/${threadId}，收到 ${result.session_id}/${result.thread_id}`);
  }
  return result;
}
