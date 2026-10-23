import { useEffect, useRef, useState } from "react";
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
    void getWorkspaceRawFileBlob(
      apiPort,
      target.path,
      workspaceId,
      controller.signal,
    ).then((blob) => {
      if (controller.signal.aborted) return;
      objectUrl = URL.createObjectURL(blob);
      setResolvedSrc(`${objectUrl}${target.fragment}`);
    }).catch((loadError: unknown) => {
      if (!controller.signal.aborted) {
        setError(errorMessage(loadError));
      }
    });

    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [apiPort, isWithinPreloadMargin, target.fragment, target.path, workspaceId]);

  if (error) {
    return <span className="workspace-markdown-image-error" role="alert">{alt}: {error}</span>;
  }
  if (!resolvedSrc) {
    return (
      <span ref={placeholderRef} className="workspace-markdown-image-loading">
        图片进入视口附近时加载：{alt}
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
    return <span className="workspace-markdown-image-error" role="alert">{alt}: 图片地址不能只包含页内锚点: {src}</span>;
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
