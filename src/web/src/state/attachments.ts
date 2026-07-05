import type { AttachmentRef, Message, TraceEvent } from "../types/backend";
import type { SessionAttachmentSummary } from "../types/frontend";
import { rawTracePayload } from "./traceEvents";

function attachmentDisplayName(attachment: AttachmentRef): string {
  return attachment.name || attachment.file_id || "附件";
}

function attachmentsFromPayload(value: unknown): AttachmentRef[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is AttachmentRef => {
    return Boolean(
      item &&
        typeof item === "object" &&
        typeof (item as { file_id?: unknown }).file_id === "string",
    );
  });
}

export function updateSessionAttachmentSummary(
  summaries: Map<string, SessionAttachmentSummary>,
  sessionId: string,
  attachments: AttachmentRef[],
  createdAt: string | null,
) {
  if (attachments.length === 0) {
    return;
  }

  const previous = summaries.get(sessionId);
  const names = new Set(previous?.names ?? []);
  for (const attachment of attachments) {
    names.add(attachmentDisplayName(attachment));
  }
  summaries.set(sessionId, {
    count: Math.max(previous?.count ?? 0, names.size, attachments.length),
    names: Array.from(names).slice(0, 3),
    latestAt: createdAt ?? previous?.latestAt ?? null,
  });
}

export function updateAttachmentSummariesFromMessages(
  summaries: Map<string, SessionAttachmentSummary>,
  messages: Message[],
) {
  for (const message of messages) {
    if (message.role !== "user") {
      continue;
    }
    updateSessionAttachmentSummary(
      summaries,
      message.session_id,
      message.attachments ?? [],
      message.created_at ?? null,
    );
  }
}

export function updateAttachmentSummariesFromTraces(
  summaries: Map<string, SessionAttachmentSummary>,
  sessionId: string,
  traceEvents: TraceEvent[],
) {
  for (const event of traceEvents) {
    if (event.type !== "message_created") {
      continue;
    }
    const payload = rawTracePayload(event);
    const payloadSessionId =
      typeof payload.session_id === "string" ? payload.session_id : sessionId;
    const attachments = attachmentsFromPayload(payload.attachments);
    const createdAt =
      typeof payload.created_at === "string"
        ? payload.created_at
        : event.timestamp;
    updateSessionAttachmentSummary(
      summaries,
      payloadSessionId,
      attachments,
      createdAt,
    );
  }
}
