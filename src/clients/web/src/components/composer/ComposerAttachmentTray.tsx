import { t } from "../../../../shared/core/i18n/index.js";
import React from "react";
import type { SelectedAttachment } from "../../utils/media/mediaAttachments";

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
        <div className="composer-attachments" aria-label={t("composer.attachments.added")}>
          {attachments.map((attachment) => (
            <div key={attachment.file_id} className="composer-attachment">
              {attachment.mediaKind === "video" ? (
                <video
                  src={attachment.previewUrl}
                  className="composer-attachment-thumb composer-attachment-video"
                  muted
                  playsInline
                  preload="metadata"
                  aria-label={attachment.name ?? t("composer.attachments.video")}
                />
              ) : attachment.mediaKind === "image" ? (
                <img
                  src={attachment.previewUrl}
                  alt={attachment.name ?? t("composer.attachments.image")}
                  className="composer-attachment-thumb"
                />
              ) : (
                <div
                  className="composer-attachment-thumb composer-attachment-file"
                  aria-label={attachment.name ?? t("composer.attachments.file")}
                >
                  <span className="codicon codicon-file" aria-hidden="true" />
                  <span>FILE</span>
                </div>
              )}
              <span className="composer-attachment-name">
                {attachment.name ?? t("composer.attachments.attachment")}
              </span>
              <button
                type="button"
                className="composer-attachment-remove"
                onClick={() => onRemove(attachment.file_id)}
                title={t("composer.attachments.remove")}
                aria-label={t("composer.attachments.removeNamed", attachment.name ?? t("composer.attachments.attachment"))}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
      {notice && <div className="composer-attachment-notice">{notice}</div>}
      {error && <div className="composer-attachment-error" role="alert">{error}</div>}
    </>
  );
}
