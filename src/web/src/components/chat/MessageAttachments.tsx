import React from "react";
import { createPortal } from "react-dom";
import type { AttachmentRef } from "../../types/backend";
import { useMessageMediaSources } from "../../hooks/useMessageMediaSources";
import {
  buildMessageMediaItems,
  type MessageMediaItem,
  type MessageMediaKind,
} from "../../utils/messageMedia";

function fallbackLabel(kind: MessageMediaKind): string {
  if (kind === "image") {
    return "IMG";
  }
  if (kind === "audio") {
    return "AUDIO";
  }
  if (kind === "video") {
    return "VIDEO";
  }
  return "FILE";
}

function ImageViewer({
  items,
  sources,
  initialIndex,
  onClose,
}: {
  items: MessageMediaItem[];
  sources: ReadonlyMap<string, string>;
  initialIndex: number;
  onClose: () => void;
}): React.ReactNode {
  const [index, setIndex] = React.useState(initialIndex);
  const [scale, setScale] = React.useState(1);
  const item = items[index];
  const source = sources.get(item.id);

  React.useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      } else if (event.key === "ArrowLeft" && items.length > 1) {
        setIndex((current) => (current - 1 + items.length) % items.length);
        setScale(1);
      } else if (event.key === "ArrowRight" && items.length > 1) {
        setIndex((current) => (current + 1) % items.length);
        setScale(1);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [items.length, onClose]);

  React.useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, []);

  return createPortal(
    <div
      className="message-media-viewer"
      role="dialog"
      aria-modal="true"
      aria-label={`查看图片：${item.name}`}
      onClick={onClose}
    >
      <header className="message-media-viewer-toolbar" onClick={(event) => event.stopPropagation()}>
        <div className="message-media-viewer-title">
          <strong>{item.name}</strong>
          <span>{index + 1} / {items.length}</span>
        </div>
        <div className="message-media-viewer-actions">
          <button type="button" onClick={() => setScale((value) => Math.max(0.25, value - 0.25))} aria-label="缩小图片">−</button>
          <button type="button" onClick={() => setScale(1)} aria-label="恢复图片大小">{Math.round(scale * 100)}%</button>
          <button type="button" onClick={() => setScale((value) => Math.min(4, value + 0.25))} aria-label="放大图片">＋</button>
          <button type="button" onClick={onClose} aria-label="关闭图片查看器">×</button>
        </div>
      </header>
      {items.length > 1 ? (
        <>
          <button
            type="button"
            className="message-media-viewer-nav previous"
            onClick={(event) => {
              event.stopPropagation();
              setIndex((index - 1 + items.length) % items.length);
              setScale(1);
            }}
            aria-label="上一张图片"
          >‹</button>
          <button
            type="button"
            className="message-media-viewer-nav next"
            onClick={(event) => {
              event.stopPropagation();
              setIndex((index + 1) % items.length);
              setScale(1);
            }}
            aria-label="下一张图片"
          >›</button>
        </>
      ) : null}
      <div className="message-media-viewer-canvas" onClick={(event) => event.stopPropagation()}>
        {source ? (
          <img
            src={source}
            alt={item.name}
            style={{ transform: `scale(${scale})` }}
          />
        ) : (
          <div className="message-media-load-error" role="alert">图片内容不可用</div>
        )}
      </div>
    </div>,
    document.body,
  );
}

export default function MessageAttachments({
  attachments,
  apiPort,
  sessionId,
  workspaceId,
}: {
  attachments: AttachmentRef[];
  apiPort: number;
  sessionId: string;
  workspaceId?: string | null;
}): React.ReactNode {
  const items = React.useMemo(() => buildMessageMediaItems(attachments), [attachments]);
  const imageItems = React.useMemo(
    () => items.filter((item) => item.kind === "image"),
    [items],
  );
  const { sources, errors, reload } = useMessageMediaSources(
    items,
    apiPort,
    sessionId,
    workspaceId,
  );
  const [viewerIndex, setViewerIndex] = React.useState<number | null>(null);

  if (items.length === 0) {
    return null;
  }

  return (
    <div className="message-attachments" aria-label="消息附件">
      {items.map((item) => {
        const source = sources.get(item.id);
        const error = errors.get(item.id);
        const imageIndex = imageItems.findIndex((image) => image.id === item.id);
        return (
          <article key={item.id} className={`message-attachment is-${item.kind}`}>
            {item.kind === "image" && source ? (
              <button
                type="button"
                className="message-attachment-preview-button"
                onClick={() => setViewerIndex(imageIndex)}
                aria-label={`查看图片：${item.name}`}
              >
                <img src={source} alt={item.name} className="message-attachment-thumb" />
                <span className="message-attachment-open-hint" aria-hidden="true">
                  <span className="codicon codicon-screen-full" />
                </span>
              </button>
            ) : (
              // TODO: 在媒体查看器中接入音频波形与视频播放器后，替换对应类型的占位卡片。
              <span className="message-attachment-icon" aria-hidden="true">
                {fallbackLabel(item.kind)}
              </span>
            )}
            <span className="message-attachment-name" title={item.name}>{item.name}</span>
            {error ? (
              <button
                type="button"
                className="message-attachment-error"
                title={error}
                onClick={reload}
              >
                重新加载图片
              </button>
            ) : null}
          </article>
        );
      })}
      {viewerIndex !== null ? (
        <ImageViewer
          items={imageItems}
          sources={sources}
          initialIndex={viewerIndex}
          onClose={() => setViewerIndex(null)}
        />
      ) : null}
    </div>
  );
}
