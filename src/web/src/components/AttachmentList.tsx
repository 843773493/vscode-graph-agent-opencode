import React from "react";
import type { AttachmentRef } from "../types/backend";

function attachmentDisplayName(attachment: AttachmentRef): string {
  return attachment.name || attachment.file_id || "附件";
}

function attachmentPreviewUrl(attachment: AttachmentRef): string | null {
  if (
    typeof attachment.data_url === "string" &&
    (
      attachment.data_url.startsWith("data:image/") ||
      attachment.data_url.startsWith("data:video/")
    )
  ) {
    return attachment.data_url;
  }
  return null;
}

function attachmentKind(attachment: AttachmentRef): "image" | "video" | "file" {
  const contentType = attachment.content_type ?? "";
  if (contentType.startsWith("image/")) {
    return "image";
  }
  if (contentType.startsWith("video/")) {
    return "video";
  }
  if (typeof attachment.data_url === "string") {
    if (attachment.data_url.startsWith("data:image/")) {
      return "image";
    }
    if (attachment.data_url.startsWith("data:video/")) {
      return "video";
    }
  }
  return "file";
}

function previewKind(previewUrl: string): "image" | "video" {
  return previewUrl.startsWith("data:video/") ? "video" : "image";
}

function fallbackIconLabel(kind: "image" | "video" | "file"): string {
  if (kind === "image") {
    return "IMG";
  }
  if (kind === "video") {
    return "VID";
  }
  return "FILE";
}

export default function AttachmentList({
  attachments,
}: {
  attachments: AttachmentRef[];
}): React.ReactNode {
  const [preview, setPreview] = React.useState<{
    url: string;
    name: string;
    kind: "image" | "video";
  } | null>(null);

  if (attachments.length === 0) {
    return null;
  }

  return (
    <div className="message-attachments" aria-label="消息附件">
      {attachments.map((attachment) => {
        const previewUrl = attachmentPreviewUrl(attachment);
        const name = attachmentDisplayName(attachment);
        const kind = attachmentKind(attachment);
        return (
          <div key={attachment.file_id} className="message-attachment">
            {previewUrl ? (
              <button
                type="button"
                className="message-attachment-preview-button"
                onClick={() => setPreview({ url: previewUrl, name, kind: previewKind(previewUrl) })}
                title="预览附件"
              >
                {previewKind(previewUrl) === "video" ? (
                  <video
                    src={previewUrl}
                    className="message-attachment-thumb message-attachment-video"
                    muted
                    playsInline
                    preload="metadata"
                    aria-label={name}
                  />
                ) : (
                  <img
                    src={previewUrl}
                    alt={name}
                    className="message-attachment-thumb"
                  />
                )}
              </button>
            ) : (
              <span className="message-attachment-icon" aria-hidden="true">
                {fallbackIconLabel(kind)}
              </span>
            )}
            <span className="message-attachment-name">{name}</span>
          </div>
        );
      })}
      {preview ? (
        <div
          className="image-preview-overlay"
          role="dialog"
          aria-modal="true"
          aria-label={preview.name}
          onClick={() => setPreview(null)}
        >
          <button
            type="button"
            className="image-preview-close"
            onClick={() => setPreview(null)}
            aria-label="关闭附件预览"
          >
            ×
          </button>
          {preview.kind === "video" ? (
            <video
              src={preview.url}
              className="image-preview-large"
              controls
              autoPlay
              onClick={(event) => event.stopPropagation()}
            />
          ) : (
            <img
              src={preview.url}
              alt={preview.name}
              className="image-preview-large"
              onClick={(event) => event.stopPropagation()}
            />
          )}
        </div>
      ) : null}
    </div>
  );
}
