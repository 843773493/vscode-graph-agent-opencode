import type { AttachmentRef, Message } from "../types/backend";
import type { TimelineItem } from "./timelineTypes";

function isAttachmentRef(value: unknown): value is AttachmentRef {
  if (!value || typeof value !== "object") {
    return false;
  }
  return typeof (value as { file_id?: unknown }).file_id === "string";
}

function normalizeAttachments(value: unknown): AttachmentRef[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(isAttachmentRef);
}

function imageDataUrlsFromMetadata(metadata: Record<string, unknown>): string[] {
  const blocks = metadata.content_blocks;
  if (!Array.isArray(blocks)) {
    return [];
  }

  return blocks.flatMap((block) => {
    if (!block || typeof block !== "object") {
      return [];
    }
    const typedBlock = block as {
      type?: unknown;
      image_url?: unknown;
    };
    if (typedBlock.type !== "image_url") {
      return [];
    }
    if (
      typedBlock.image_url &&
      typeof typedBlock.image_url === "object" &&
      typeof (typedBlock.image_url as { url?: unknown }).url === "string"
    ) {
      const url = (typedBlock.image_url as { url: string }).url;
      return url.startsWith("data:image/") ? [url] : [];
    }
    return [];
  });
}

function hydrateAttachmentPreviews(
  attachments: AttachmentRef[],
  metadata: Record<string, unknown>,
): AttachmentRef[] {
  const previewUrls = imageDataUrlsFromMetadata(metadata);
  if (previewUrls.length === 0) {
    return attachments;
  }

  return attachments.map((attachment, index) =>
    attachment.data_url
      ? attachment
      : {
          ...attachment,
          data_url: previewUrls[index],
        },
  );
}

export function normalizeTimelineMessage(message: Message): TimelineItem {
  const metadata = message.metadata ?? {};
  return {
    kind: "message",
    id: message.message_id,
    role: message.role,
    content: message.content,
    attachments: hydrateAttachmentPreviews(
      normalizeAttachments(message.attachments),
      metadata,
    ),
    createdAt: message.created_at,
    metadata,
  };
}
