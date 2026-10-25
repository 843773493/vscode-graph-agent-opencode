import React from "react";
import {
  addSessionFileTreeShortcut,
  getSessionAttachmentBlob,
  getSessionAttachmentTextPreview,
} from "../../../api";
import type { AttachmentRef } from "../../../types/backend";
import { errorMessage } from "../../../utils/errorMessage";

const MAX_TEXT_PREVIEW_CODE_UNITS = 200_000;

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
  onDirectoryShortcutUpdated,
}: {
  attachment: AttachmentRef;
  apiPort: number;
  sessionId: string;
  workspaceId?: string | null;
  onDirectoryShortcutUpdated?: () => void;
}): React.ReactNode {
  const [shortcutBusy, setShortcutBusy] = React.useState(false);
  const [shortcutAdded, setShortcutAdded] = React.useState(false);
  const [shortcutError, setShortcutError] = React.useState<string | null>(null);
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

    void (async () => {
      if (isTextContent(contentType)) {
        const preview = await getSessionAttachmentTextPreview(
          apiPort,
          sessionId,
          attachment.file_id,
          workspaceId,
          { maxCodeUnits: MAX_TEXT_PREVIEW_CODE_UNITS, signal: controller.signal },
        );
        if (!active) return;
        setText(preview.text);
        setTruncated(preview.truncated);
      } else {
        const blob = await getSessionAttachmentBlob(
          apiPort,
          sessionId,
          attachment.file_id,
          workspaceId,
          { variant: "original", signal: controller.signal },
        );
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setSource(objectUrl);
      }
    })().catch((reason: unknown) => {
      if (!active || (reason instanceof DOMException && reason.name === "AbortError")) {
        return;
      }
      setError(errorMessage(reason));
    }).finally(() => {
      if (active) setLoading(false);
    });

    return () => {
      active = false;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [apiPort, attachment.file_id, contentType, sessionId, workspaceId]);

  const addDirectoryShortcut = async () => {
    if (!attachment.directory_uri) return;
    setShortcutBusy(true);
    setShortcutError(null);
    setShortcutAdded(false);
    try {
      await addSessionFileTreeShortcut(apiPort, sessionId, {
        path: attachment.directory_uri, label: `附件：${name}`,
      }, workspaceId);
      setShortcutAdded(true);
    } catch (reason: unknown) {
      setShortcutError(errorMessage(reason));
    } finally {
      // 成功或响应丢失都重新读取原后端设置，不能仅在本地插入快捷路径。
      onDirectoryShortcutUpdated?.();
      setShortcutBusy(false);
    }
  };

  return (
    <section className="workspace-attachment-preview" aria-label={`附件预览：${name}`}>
      <header className="workspace-attachment-preview-header">
        <div>
          <strong title={name}>{name}</strong>
          <span>{contentType}</span>
        </div>
        {attachment.directory_uri && onDirectoryShortcutUpdated ? (
          <button type="button" disabled={shortcutBusy}
            onClick={() => void addDirectoryShortcut()}>
            {shortcutBusy ? "正在添加目录…" : "添加附件目录快捷路径"}
          </button>
        ) : null}
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
      {shortcutAdded ? <div role="status">附件目录已添加到当前会话文件树</div> : null}
      {shortcutError ? <div role="alert">附件目录添加失败：{shortcutError}</div> : null}
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
