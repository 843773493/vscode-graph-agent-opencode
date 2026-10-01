import { useEffect, useState } from "react";
import { getWorkspaceRawFileBlob } from "../../../api";
import {
  resolveWorkspaceMarkdownTarget,
  type WorkspaceMarkdownTarget,
} from "../../../utils/markdown/workspaceMarkdown";
import { errorMessage } from "../../../utils/errorMessage";

interface WorkspaceMarkdownImageProps {
  apiPort: number;
  workspaceId: string | null;
  markdownPath: string;
  src: string;
  alt: string;
  title?: string;
}

export default function WorkspaceMarkdownImage({
  apiPort,
  workspaceId,
  markdownPath,
  src,
  alt,
  title,
}: WorkspaceMarkdownImageProps) {
  const [resolvedSrc, setResolvedSrc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let target: WorkspaceMarkdownTarget;
    try {
      target = resolveWorkspaceMarkdownTarget(markdownPath, src);
    } catch (resolveError) {
      setResolvedSrc(null);
      setError(errorMessage(resolveError));
      return;
    }
    if (target.kind === "external") {
      setResolvedSrc(target.href);
      setError(null);
      return;
    }
    if (target.kind === "anchor") {
      setResolvedSrc(null);
      setError(`图片地址不能只包含页内锚点: ${src}`);
      return;
    }

    const controller = new AbortController();
    let objectUrl: string | null = null;
    setResolvedSrc(null);
    setError(null);
    void getWorkspaceRawFileBlob(
      apiPort,
      target.path,
      workspaceId,
      controller.signal,
    )
      .then((blob) => {
        // 与同目录的 WorkspaceAttachmentPreview 同一判据：请求可能在途中被 abort，
        // 迟到的旧响应若还创建对象 URL，就会泄漏一个永远得不到 revoke 的 URL，
        // 并把上一个 src 的图片写回状态。作废后直接返回，不创建、不回写。
        if (controller.signal.aborted) {
          return;
        }
        objectUrl = URL.createObjectURL(blob);
        setResolvedSrc(`${objectUrl}${target.fragment}`);
      })
      .catch((loadError: unknown) => {
        if (!controller.signal.aborted) {
          setError(errorMessage(loadError));
        }
      });
    return () => {
      controller.abort();
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }
    };
  }, [apiPort, markdownPath, src, workspaceId]);

  if (error) {
    return <span className="workspace-markdown-image-error" role="alert">{alt}: {error}</span>;
  }
  if (!resolvedSrc) {
    return <span className="workspace-markdown-image-loading">正在加载图片：{alt}</span>;
  }
  return <img src={resolvedSrc} alt={alt} title={title} />;
}
