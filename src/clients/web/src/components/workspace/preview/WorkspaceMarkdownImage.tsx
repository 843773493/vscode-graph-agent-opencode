import { useEffect, useRef, useState } from "react";
import { t } from "../../../../../shared/core/i18n/index.js";
import { getWorkspaceRawFileBlob } from "../../../api";
import { onGatewayTokenInvalidated } from "../../../api/http";
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

type WorkspaceImageTarget = Extract<WorkspaceMarkdownTarget, { kind: "workspace" }>;

function WorkspaceFileMarkdownImage({
  apiPort,
  workspaceId,
  target,
  alt,
  title,
}: {
  apiPort: number;
  workspaceId: string | null;
  target: WorkspaceImageTarget;
  alt: string;
  title?: string;
}) {
  const placeholderRef = useRef<HTMLSpanElement | null>(null);
  const [isWithinPreloadMargin, setWithinPreloadMargin] = useState(false);
  const [resolvedSrc, setResolvedSrc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isWithinPreloadMargin) return;
    const placeholder = placeholderRef.current;
    if (!placeholder) return;

    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        setWithinPreloadMargin(true);
      }
    }, { rootMargin: "200px 0px" });
    observer.observe(placeholder);
    return () => observer.disconnect();
  }, [isWithinPreloadMargin]);

  useEffect(() => {
    if (!isWithinPreloadMargin) return;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    let releaseIdentity: (() => void) | undefined;
    const releaseObjectUrl = () => {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    };
    void getWorkspaceRawFileBlob(
      apiPort,
      target.path,
      workspaceId,
      controller.signal,
    ).then((blob) => {
      if (controller.signal.aborted) return;
      // 下载期间由文件 API 处理刷新与撤销；展示后接管订阅，避免把正常 401 刷新取消。
      releaseIdentity = onGatewayTokenInvalidated(apiPort, () => {
        controller.abort();
        releaseObjectUrl();
        setResolvedSrc(null);
        setError(t("workspace.markdown.image.identityChanged"));
      });
      objectUrl = URL.createObjectURL(blob);
      setResolvedSrc(`${objectUrl}${target.fragment}`);
    }).catch((loadError: unknown) => {
      if (!controller.signal.aborted) {
        setError(errorMessage(loadError));
      }
    });

    return () => {
      releaseIdentity?.();
      controller.abort();
      releaseObjectUrl();
    };
  }, [apiPort, isWithinPreloadMargin, target.fragment, target.path, workspaceId]);

  if (error) {
    return <span className="workspace-markdown-image-error" role="alert">{alt}: {error}</span>;
  }
  if (!resolvedSrc) {
    return (
      <span ref={placeholderRef} className="workspace-markdown-image-loading">
        {t("workspace.markdown.image.loadingNearViewport", alt)}
      </span>
    );
  }
  return <img src={resolvedSrc} alt={alt} title={title} loading="lazy" />;
}

export default function WorkspaceMarkdownImage({
  apiPort,
  workspaceId,
  markdownPath,
  src,
  alt,
  title,
}: WorkspaceMarkdownImageProps) {
  let target: WorkspaceMarkdownTarget;
  try {
    target = resolveWorkspaceMarkdownTarget(markdownPath, src);
  } catch (resolveError) {
    return <span className="workspace-markdown-image-error" role="alert">{alt}: {errorMessage(resolveError)}</span>;
  }

  if (target.kind === "external") {
    return <img src={target.href} alt={alt} title={title} loading="lazy" />;
  }
  if (target.kind === "anchor") {
    return <span className="workspace-markdown-image-error" role="alert">{t("workspace.markdown.image.anchorOnly", alt, src)}</span>;
  }

  return (
    <WorkspaceFileMarkdownImage
      key={JSON.stringify([apiPort, workspaceId, markdownPath, src])}
      apiPort={apiPort}
      workspaceId={workspaceId}
      target={target}
      alt={alt}
      title={title}
    />
  );
}
