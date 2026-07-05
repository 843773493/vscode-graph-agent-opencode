import React from "react";
import type { SelectedAttachment } from "../utils/imageAttachments";

export default function ComposerAttachmentTray({
  attachments,
  error,
  onRemove,
}: {
  attachments: SelectedAttachment[];
  error: string;
  onRemove: () => void;
}): React.ReactNode {
  return (
    <>
      {attachments.length > 0 && (
        <div className="composer-attachments" aria-label="已添加附件">
          {attachments.map((attachment) => (
            <div key={attachment.file_id} className="composer-attachment">
              <img
                src={attachment.previewUrl}
                alt={attachment.name ?? "图片附件"}
                className="composer-attachment-thumb"
              />
              <span className="composer-attachment-name">
                {attachment.name ?? "图片附件"}
              </span>
              <button
                type="button"
                className="composer-attachment-remove"
                onClick={onRemove}
                title="移除附件"
                aria-label="移除附件"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
      {error && <div className="composer-attachment-error">{error}</div>}
    </>
  );
}
