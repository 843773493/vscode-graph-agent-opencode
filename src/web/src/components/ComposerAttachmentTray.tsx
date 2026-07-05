import React from "react";
import type { SelectedAttachment } from "../utils/mediaAttachments";

export default function ComposerAttachmentTray({
  attachments,
  error,
  notice,
  onRemove,
}: {
  attachments: SelectedAttachment[];
  error: string;
  notice: string;
  onRemove: (fileId: string) => void;
}): React.ReactNode {
  return (
    <>
      {attachments.length > 0 && (
        <div className="composer-attachments" aria-label="已添加附件">
          {attachments.map((attachment) => (
            <div key={attachment.file_id} className="composer-attachment">
              {attachment.mediaKind === "video" ? (
                <video
                  src={attachment.previewUrl}
                  className="composer-attachment-thumb composer-attachment-video"
                  muted
                  playsInline
                  preload="metadata"
                  aria-label={attachment.name ?? "视频附件"}
                />
              ) : (
                <img
                  src={attachment.previewUrl}
                  alt={attachment.name ?? "图片附件"}
                  className="composer-attachment-thumb"
                />
              )}
              <span className="composer-attachment-name">
                {attachment.name ?? "附件"}
              </span>
              <button
                type="button"
                className="composer-attachment-remove"
                onClick={() => onRemove(attachment.file_id)}
                title="移除附件"
                aria-label={`移除 ${attachment.name ?? "附件"}`}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
      {notice && <div className="composer-attachment-notice">{notice}</div>}
      {error && <div className="composer-attachment-error">{error}</div>}
    </>
  );
}
