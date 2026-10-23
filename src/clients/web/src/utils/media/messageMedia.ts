import type { AttachmentRef, TurnAttachment } from "../../types/backend";

export type MessageAttachment = AttachmentRef & Pick<TurnAttachment, "availability">;

export type MessageMediaKind = "image" | "audio" | "video" | "file";

export interface MessageMediaItem {
  id: string;
  attachment: MessageAttachment;
  kind: MessageMediaKind;
  name: string;
}

function dataUrlContentType(dataUrl: string | null | undefined): string | undefined {
  if (dataUrl == null) {
    return undefined;
  }

  let contentTypeEnd = 5;
  while (contentTypeEnd < dataUrl.length) {
    const character = dataUrl[contentTypeEnd];
    if (character === ";" || character === ",") {
      break;
    }
    contentTypeEnd += 1;
  }
  return dataUrl.slice(5, contentTypeEnd);
}

export function messageMediaKind(attachment: MessageAttachment): MessageMediaKind {
  const contentType = attachment.content_type
    ?? dataUrlContentType(attachment.data_url)
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
  attachments: MessageAttachment[],
): MessageMediaItem[] {
  return attachments.map((attachment, index) => ({
    id: `${attachment.file_id}:${index}`,
    attachment,
    kind: messageMediaKind(attachment),
    name: attachment.name || attachment.file_id || "附件",
  }));
}
