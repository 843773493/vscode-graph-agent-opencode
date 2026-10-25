import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import {
  resolveWorkspaceMarkdownTarget,
  type WorkspaceMarkdownTarget,
} from "../../../utils/markdown/workspaceMarkdown";
import { errorMessage } from "../../../utils/errorMessage";
import MermaidDiagram from "./MermaidDiagram";
import WorkspaceMarkdownImage from "./WorkspaceMarkdownImage";

interface WorkspaceMarkdownRendererProps {
  apiPort: number;
  workspaceId: string | null;
  path: string;
  content: string;
  onOpenWorkspacePath: (path: string) => Promise<void>;
}

export default function WorkspaceMarkdownRenderer({
  apiPort,
  workspaceId,
  path,
  content,
  onOpenWorkspacePath,
}: WorkspaceMarkdownRendererProps) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeSanitize]}
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
                  void onOpenWorkspacePath(target.path);
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
            >
              {children}
            </a>
          );
        },
        img: ({ src, alt = "图片", title }) => src ? (
          <WorkspaceMarkdownImage
            apiPort={apiPort}
            workspaceId={workspaceId}
            markdownPath={path}
            src={src}
            alt={alt}
            title={title ?? undefined}
          />
        ) : <span className="workspace-markdown-image-error">{alt}: 缺少图片地址</span>,
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
  );
}
