import React, {
  startTransition,
  useEffect,
  useRef,
  useState,
} from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import {
  isLikelyWorkspaceFileReference,
  remarkWorkspaceFileReferences,
} from "../../utils/workspaceFileReferences";
import WorkspaceFileReferenceLink from "./WorkspaceFileReferenceLink";

function isExternalHref(href: string): boolean {
  return /^(https?:|mailto:|tel:)/i.test(href);
}

const REMARK_PLUGINS = [remarkGfm, remarkWorkspaceFileReferences];
const REHYPE_PLUGINS = [rehypeSanitize];
const STREAMING_MARKDOWN_RENDER_INTERVAL_MS = 80;
const MARKDOWN_ENHANCEMENT_QUIET_MS = 600;
export const LARGE_MARKDOWN_DEFER_THRESHOLD = 6_000;
export const LARGE_MARKDOWN_PREVIEW_LENGTH = 6_000;

type MarkdownRenderMode = "progressive" | "plain";

interface IdleSchedulerWindow {
  requestIdleCallback?: (
    callback: IdleRequestCallback,
    options?: IdleRequestOptions,
  ) => number;
  cancelIdleCallback?: (handle: number) => void;
}

function scheduleMarkdownEnhancement(callback: () => void): () => void {
  if (typeof window === "undefined") {
    const timer = globalThis.setTimeout(callback, 0);
    return () => globalThis.clearTimeout(timer);
  }
  const scheduler = window as IdleSchedulerWindow;
  let quietTimer: number | null = null;
  let idleHandle: number | null = null;
  let fallbackTimer: number | null = null;
  let completed = false;
  const activityEvents = ["keydown", "pointerdown", "input"] as const;
  const cleanup = () => {
    if (quietTimer !== null) {
      window.clearTimeout(quietTimer);
      quietTimer = null;
    }
    if (idleHandle !== null) {
      scheduler.cancelIdleCallback?.(idleHandle);
      idleHandle = null;
    }
    if (fallbackTimer !== null) {
      window.clearTimeout(fallbackTimer);
      fallbackTimer = null;
    }
  };
  const finish = () => {
    if (completed) return;
    completed = true;
    cleanup();
    for (const eventName of activityEvents) {
      window.removeEventListener(eventName, scheduleAfterQuiet, true);
    }
    callback();
  };
  function scheduleAfterQuiet(): void {
    if (completed) return;
    cleanup();
    quietTimer = window.setTimeout(() => {
      quietTimer = null;
      if (scheduler.requestIdleCallback) {
        idleHandle = scheduler.requestIdleCallback(finish, { timeout: 1_000 });
      } else {
        fallbackTimer = window.setTimeout(finish, 0);
      }
    }, MARKDOWN_ENHANCEMENT_QUIET_MS);
  }
  for (const eventName of activityEvents) {
    window.addEventListener(eventName, scheduleAfterQuiet, true);
  }
  scheduleAfterQuiet();
  return () => {
    completed = true;
    cleanup();
    for (const eventName of activityEvents) {
      window.removeEventListener(eventName, scheduleAfterQuiet, true);
    }
  };
}

function useStreamingMarkdownValue(value: string, streaming: boolean): string {
  const [renderedValue, setRenderedValue] = useState(value);
  const latestValueRef = useRef(value);
  const timerRef = useRef<number | null>(null);
  latestValueRef.current = value;

  useEffect(() => {
    if (!streaming) {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      setRenderedValue(value);
      return;
    }
    if (timerRef.current !== null) {
      return;
    }
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      setRenderedValue(latestValueRef.current);
    }, STREAMING_MARKDOWN_RENDER_INTERVAL_MS);
  }, [streaming, value]);

  useEffect(() => () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
    }
  }, []);

  return renderedValue;
}

function useProgressiveMarkdown(
  value: string,
  streaming: boolean,
  renderMode: MarkdownRenderMode,
): boolean {
  const shouldDefer = value.length >= LARGE_MARKDOWN_DEFER_THRESHOLD;
  const [enhancedValue, setEnhancedValue] = useState<string | null>(() =>
    renderMode === "progressive" && !shouldDefer ? value : null,
  );

  useEffect(() => {
    if (renderMode === "plain" || (shouldDefer && streaming)) {
      setEnhancedValue(null);
      return;
    }
    if (!shouldDefer) {
      setEnhancedValue(value);
      return;
    }
    if (enhancedValue === value) {
      return;
    }
    return scheduleMarkdownEnhancement(() => {
      startTransition(() => setEnhancedValue(value));
    });
  }, [enhancedValue, renderMode, shouldDefer, streaming, value]);

  return renderMode === "progressive"
    && (!shouldDefer || (!streaming && enhancedValue === value));
}

export interface MarkdownContentProps {
  value: string;
  className?: string;
  streaming?: boolean;
  renderMode?: MarkdownRenderMode;
}

function MarkdownContent({
  value,
  className = "",
  streaming = false,
  renderMode = "progressive",
}: MarkdownContentProps): React.ReactNode {
  const renderedValue = useStreamingMarkdownValue(value, streaming);
  const [fullyRenderedValue, setFullyRenderedValue] = useState<string | null>(null);
  const renderMarkdown = useProgressiveMarkdown(
    renderedValue,
    streaming,
    renderMode,
  );
  const boundedMarkdown = renderMarkdown
    && renderedValue.length >= LARGE_MARKDOWN_DEFER_THRESHOLD
    && fullyRenderedValue !== renderedValue;
  const markdownValue = boundedMarkdown
    ? renderedValue.slice(0, LARGE_MARKDOWN_PREVIEW_LENGTH)
    : renderedValue;
  const markdown = (
    <ReactMarkdown
      remarkPlugins={REMARK_PLUGINS}
      rehypePlugins={REHYPE_PLUGINS}
      components={{
        a: ({ children, href }) => {
          if (href && isExternalHref(href)) {
            return (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            );
          }
          return href ? (
            <WorkspaceFileReferenceLink target={href}>
              {children}
            </WorkspaceFileReferenceLink>
          ) : (
            <span>{children}</span>
          );
        },
        code: ({ children, className }) => {
          const codeValue = String(children).replace(/\n$/, "");
          if (className || String(children).endsWith("\n")) {
            return <code className={className}>{children}</code>;
          }
          if (!isLikelyWorkspaceFileReference(codeValue)) {
            return <code>{children}</code>;
          }
          return (
            <WorkspaceFileReferenceLink target={codeValue} inlineCode>
              {children}
            </WorkspaceFileReferenceLink>
          );
        },
      }}
    >
      {markdownValue}
    </ReactMarkdown>
  );
  return (
    <div
      className={`chat-markdown ${className}`.trim()}
      data-markdown-rendering={renderMarkdown ? "enhanced" : "lightweight"}
      aria-busy={!renderMarkdown}
    >
      {renderMarkdown ? (
        boundedMarkdown ? (
          <>
            <div className="chat-markdown-bounded">{markdown}</div>
            <button
              type="button"
              className="chat-markdown-show-full"
              onClick={() => setFullyRenderedValue(renderedValue)}
            >
              渲染完整 Markdown（{renderedValue.length.toLocaleString()} 字符）
            </button>
          </>
        ) : markdown
      ) : (
        <div className="chat-markdown-lightweight">{renderedValue}</div>
      )}
    </div>
  );
}

export default React.memo(MarkdownContent, (previous, next) =>
  previous.value === next.value
  && previous.className === next.className
  && previous.streaming === next.streaming
  && previous.renderMode === next.renderMode,
);
