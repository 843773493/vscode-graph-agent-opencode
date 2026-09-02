import React from "react";
import { getSessionAttachmentBlob } from "../../../api";
import type { AttachmentRef } from "../../../types/backend";

const MAX_TEXT_PREVIEW_CHARS = 200_000;

function attachmentName(attachment: AttachmentRef): string {
  return attachment.name || attachment.file_id || "附件";
}

function isTextContent(contentType: string): boolean {
  return contentType.startsWith("text/")
    || contentType === "application/json"
    || contentType === "application/xml";
}

export default function WorkspaceAttachmentPreview({
  attachment,
  apiPort,
  sessionId,
  workspaceId,
}: {
  attachment: AttachmentRef;
  apiPort: number;
  sessionId: string;
  workspaceId?: string | null;
}): React.ReactNode {
  const [source, setSource] = React.useState<string | null>(null);
  const [text, setText] = React.useState<string | null>(null);
  const [truncated, setTruncated] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const name = attachmentName(attachment);
  const contentType = attachment.content_type || "application/octet-stream";

  React.useEffect(() => {
    let active = true;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    setSource(null);
    setText(null);
    setTruncated(false);
    setLoading(true);
    setError(null);

    void getSessionAttachmentBlob(
      apiPort,
      sessionId,
      attachment.file_id,
      workspaceId,
      { variant: "original", signal: controller.signal },
    ).then(async (blob) => {
      if (!active) return;
      if (isTextContent(contentType)) {
        const content = await blob.text();
        if (!active) return;
        setText(content.slice(0, MAX_TEXT_PREVIEW_CHARS));
        setTruncated(content.length > MAX_TEXT_PREVIEW_CHARS);
      } else {
        objectUrl = URL.createObjectURL(blob);
        setSource(objectUrl);
      }
    }).catch((reason: unknown) => {
      if (!active || (reason instanceof DOMException && reason.name === "AbortError")) {
        return;
      }
      setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (active) setLoading(false);
    });

    return () => {
      active = false;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [apiPort, attachment.file_id, contentType, sessionId, workspaceId]);

  return (
    <section className="workspace-attachment-preview" aria-label={`附件预览：${name}`}>
      <header className="workspace-attachment-preview-header">
        <div>
          <strong title={name}>{name}</strong>
          <span>{contentType}</span>
        </div>
        {source ? (
          <a
            href={source}
            download={name}
            className="workspace-attachment-download"
          >
            下载原件
          </a>
        ) : null}
      </header>
      {loading ? <div className="workspace-attachment-preview-status" role="status">正在读取附件原件…</div> : null}
      {error ? (
        <div className="workspace-attachment-preview-error" role="alert">
          附件原件读取失败：{error}
        </div>
      ) : null}
      {!loading && !error && text !== null ? (
        <div className="workspace-attachment-text-preview">
          <pre>{text}</pre>
          {truncated ? <div role="status">文本预览已截断，原件仍可下载。</div> : null}
        </div>
      ) : null}
      {!loading && !error && source && contentType.startsWith("image/") ? (
        <div className="workspace-attachment-media-preview">
          <img src={source} alt={name} />
        </div>
      ) : null}
      {!loading && !error && source && contentType === "application/pdf" ? (
        <iframe
          className="workspace-attachment-document-preview"
          src={source}
          title={`PDF 附件：${name}`}
        />
      ) : null}
      {!loading && !error && source
        && !contentType.startsWith("image/")
        && contentType !== "application/pdf" ? (
        <div className="workspace-attachment-generic-preview">
          <span className="codicon codicon-file" aria-hidden="true" />
          <span>当前浏览器没有该 MIME 类型的内置预览器，请下载原件后使用现有工具查看。</span>
          <a href={source} download={name}>下载原件</a>
        </div>
      ) : null}
    </section>
  );
}
