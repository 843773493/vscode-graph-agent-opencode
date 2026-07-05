import React from "react";
import type { AttachmentRef } from "../types/backend";

function attachmentDisplayName(attachment: AttachmentRef): string {
  return attachment.name || attachment.file_id || "图片附件";
}

function attachmentPreviewUrl(attachment: AttachmentRef): string | null {
  if (
    typeof attachment.data_url === "string" &&
    attachment.data_url.startsWith("data:image/")
  ) {
    return attachment.data_url;
  }
  return null;
}

export default function AttachmentList({
  attachments,
}: {
  attachments: AttachmentRef[];
}): React.ReactNode {
  const [preview, setPreview] = React.useState<{
    url: string;
    name: string;
  } | null>(null);

  if (attachments.length === 0) {
    return null;
  }

  return (
    <div className="message-attachments" aria-label="消息附件">
      {attachments.map((attachment) => {
        const previewUrl = attachmentPreviewUrl(attachment);
        const name = attachmentDisplayName(attachment);
        return (
          <div key={attachment.file_id} className="message-attachment">
            {previewUrl ? (
              <button
                type="button"
                className="message-attachment-preview-button"
                onClick={() => setPreview({ url: previewUrl, name })}
                title="放大查看图片"
              >
                <img
                  src={previewUrl}
                  alt={name}
                  className="message-attachment-thumb"
                />
              </button>
            ) : (
              <span className="message-attachment-icon" aria-hidden="true">
                IMG
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
            aria-label="关闭图片预览"
          >
            ×
          </button>
          <img
            src={preview.url}
            alt={preview.name}
            className="image-preview-large"
            onClick={(event) => event.stopPropagation()}
          />
        </div>
      ) : null}
    </div>
  );
}
