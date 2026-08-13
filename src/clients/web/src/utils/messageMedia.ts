import type { AttachmentRef } from "../types/backend";

export type MessageMediaKind = "image" | "audio" | "video" | "file";

export interface MessageMediaItem {
  id: string;
  attachment: AttachmentRef;
  kind: MessageMediaKind;
  name: string;
}

export function messageMediaKind(attachment: AttachmentRef): MessageMediaKind {
  const contentType = attachment.content_type
    ?? attachment.data_url?.slice(5).split(/[;,]/, 1)[0]
    ?? "";
  if (contentType.startsWith("image/")) {
    return "image";
  }
  if (contentType.startsWith("audio/")) {
    return "audio";
  }
  if (contentType.startsWith("video/")) {
    return "video";
  }
  return "file";
}

export function buildMessageMediaItems(
  attachments: AttachmentRef[],
): MessageMediaItem[] {
  return attachments.map((attachment, index) => ({
    id: `${attachment.file_id}:${index}`,
    attachment,
    kind: messageMediaKind(attachment),
    name: attachment.name || attachment.file_id || "附件",
  }));
}
