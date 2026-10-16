import type { AttachmentRef, TraceEvent } from "../types/backend";
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
  sessionCacheKey: string,
  attachments: AttachmentRef[],
  createdAt: string | null,
) {
  if (attachments.length === 0) {
    return;
  }

  const previous = summaries.get(sessionCacheKey);
  const names = new Set(previous?.names ?? []);
  for (const attachment of attachments) {
    names.add(attachmentDisplayName(attachment));
  }
  summaries.set(sessionCacheKey, {
    count: Math.max(previous?.count ?? 0, names.size, attachments.length),
    names: Array.from(names).slice(0, 3),
    latestAt: createdAt ?? previous?.latestAt ?? null,
  });
}

export function updateAttachmentSummariesFromTraces(
  summaries: Map<string, SessionAttachmentSummary>,
  sessionId: string,
  sessionCacheKey: string,
  traceEvents: TraceEvent[],
) {
  for (const event of traceEvents) {
    if (event.type !== "message_created") {
      continue;
    }
    const payload = rawTracePayload(event);
    if (payload.session_id !== sessionId) continue;
    const attachments = attachmentsFromPayload(payload.attachments);
    const createdAt =
      typeof payload.created_at === "string"
        ? payload.created_at
        : event.timestamp;
    updateSessionAttachmentSummary(
      summaries,
      sessionCacheKey,
      attachments,
      createdAt,
    );
  }
}
