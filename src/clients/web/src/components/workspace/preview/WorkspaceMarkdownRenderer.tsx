import { t } from "../../../../../shared/core/i18n/index.js";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import rehypeSlug from "rehype-slug";
import { useEffect, useRef, useState } from "react";
import {
  resolveWorkspaceMarkdownTarget,
  type WorkspaceMarkdownTarget,
} from "../../../utils/markdown/workspaceMarkdown";
import { errorMessage } from "../../../utils/errorMessage";
import MermaidDiagram from "./MermaidDiagram";
import WorkspaceMarkdownImage from "./WorkspaceMarkdownImage";

const workspaceSchema = {
  ...defaultSchema,
  protocols: {
    ...defaultSchema.protocols,
    href: [...(defaultSchema.protocols?.href ?? []), "boxteam"],
    src: [...(defaultSchema.protocols?.src ?? []), "boxteam"],
  },
};

interface WorkspaceMarkdownRendererProps {
  apiPort: number;
  workspaceId: string | null;
  path: string;
  content: string;
  fragment?: string;
  onOpenWorkspacePath: (path: string, fragment?: string) => Promise<void>;
}

export default function WorkspaceMarkdownRenderer({
  apiPort,
  workspaceId,
  path,
  content,
  fragment,
  onOpenWorkspacePath,
}: WorkspaceMarkdownRendererProps) {
  const root = useRef<HTMLDivElement>(null);
  const [anchorError, setAnchorError] = useState<string | null>(null);
  const revealAnchor = (href: string) => {
    setAnchorError(null);
    let id: string;
    try { id = decodeURIComponent(href.replace(/^#/, "")).toLowerCase(); }
    catch (error) { setAnchorError(errorMessage(error)); return; }
    if (!id) { root.current?.scrollIntoView({ block: "start" }); return; }
    const heading = Array.from(root.current?.querySelectorAll<HTMLElement>("[id]") ?? [])
      .find(element => element.id === id);
    if (heading) heading.scrollIntoView({ block: "start", inline: "nearest" });
    else setAnchorError(t("workspace.markdown.anchorMissing", href));
  };
  useEffect(() => {
    if (fragment) revealAnchor(fragment);
    else setAnchorError(null);
  }, [fragment, content]);
  return (
    <div ref={root} className="workspace-markdown-document">
    {anchorError ? <div role="alert">{anchorError}</div> : null}
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[[rehypeSanitize, workspaceSchema], rehypeSlug]}
      urlTransform={url => url.startsWith("boxteam://") ? url : defaultUrlTransform(url)}
      components={{
        a: ({ children, href, title }) => {
          if (!href) {
            return <span>{children}</span>;
          }
          let target: WorkspaceMarkdownTarget;
          try {
            target = resolveWorkspaceMarkdownTarget(path, href);
          } catch (resolveError) {
            const message = errorMessage(resolveError);
            return <span className="workspace-markdown-link-error" title={message}>{children}</span>;
          }
          if (target.kind === "workspace") {
            return (
              <a
                href={href}
                title={title}
                onClick={(event) => {
                  event.preventDefault();
                  void onOpenWorkspacePath(target.path, target.fragment);
                }}
              >
                {children}
              </a>
            );
          }
          return (
            <a
              href={target.href}
              title={title}
              target={target.kind === "external" ? "_blank" : undefined}
              rel={target.kind === "external" ? "noopener noreferrer" : undefined}
              onClick={target.kind === "anchor" ? event => {
                event.preventDefault();
                revealAnchor(target.href);
              } : undefined}
            >
              {children}
            </a>
          );
        },
        img: ({ src, alt = t("workspace.markdown.image"), title }) => src ? (
          <WorkspaceMarkdownImage
            apiPort={apiPort}
            workspaceId={workspaceId}
            markdownPath={path}
            src={src}
            alt={alt}
            title={title ?? undefined}
          />
        ) : <span className="workspace-markdown-image-error">{t("workspace.markdown.imageMissingSource", alt)}</span>,
        code: ({ className, children }) => {
          const source = String(children).replace(/\n$/, "");
          if (className === "language-mermaid") {
            return <MermaidDiagram source={source} />;
          }
          return <code className={className}>{children}</code>;
        },
      }}
    >
      {content}
    </ReactMarkdown>
    </div>
  );
}
