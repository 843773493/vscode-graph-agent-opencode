import { useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import {
  resolveWorkspaceMarkdownTarget,
  type WorkspaceMarkdownTarget,
} from "../../../utils/markdown/workspaceMarkdown";
import MermaidDiagram from "./MermaidDiagram";
import WorkspaceMarkdownImage from "./WorkspaceMarkdownImage";
import { errorMessage } from "../../../utils/errorMessage";

interface WorkspaceMarkdownPreviewProps {
  apiPort: number;
  workspaceId: string | null;
  path: string;
  content: string;
  onOpenWorkspacePath: (path: string) => Promise<void>;
}

/**
 * Markdown 预览一次交给 remark-gfm 解析的最大字符数。
 *
 * 后端 workspace.files.preview.max_bytes 默认 1 MiB，允许预览的文件最大可达约
 * 100 万字符。GFM 表格解析是超线性的：真实浏览器实测 73 KB 宽表渲染 10.1 秒、
 * 291 KB 宽表 118.8 秒、1 MiB 宽表超过 10 分钟仍未返回，期间主线程被同步占满、
 * 界面完全假死（普通散文则是线性的，1 MiB 仅 486 ms）。
 *
 * 因此只在「预览」视图对超限文档做上限渲染并显式提示；工具栏的「源码」视图仍可
 * 查看完整正文，用户不会因为预览上限而看不到内容，也绝不静默截断。
 * 20 000 字符时最坏的宽表渲染约 0.7 秒，是实测曲线上仍可接受的量级。
 */
export const LARGE_MARKDOWN_PREVIEW_LENGTH = 20_000;

export default function WorkspaceMarkdownPreview({
  apiPort,
  workspaceId,
  path,
  content,
  onOpenWorkspacePath,
}: WorkspaceMarkdownPreviewProps) {
  const [renderFullDocument, setRenderFullDocument] = useState(false);
  const documentBounded =
    content.length > LARGE_MARKDOWN_PREVIEW_LENGTH && !renderFullDocument;
  return (
    <div className="workspace-markdown-preview">
      {content.length > LARGE_MARKDOWN_PREVIEW_LENGTH ? (
        <div className="workspace-preview-truncation-notice" role="status">
          <span>
            文档共 {content.length.toLocaleString()} 字符，已只渲染前{" "}
            {LARGE_MARKDOWN_PREVIEW_LENGTH.toLocaleString()} 字符以避免界面卡顿。
          </span>
          <button
            type="button"
            onClick={() => setRenderFullDocument((value) => !value)}
          >
            {documentBounded
              ? `渲染完整 Markdown（${content.length.toLocaleString()} 字符）`
              : `仅显示前 ${LARGE_MARKDOWN_PREVIEW_LENGTH.toLocaleString()} 字符`}
          </button>
        </div>
      ) : null}
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
        {documentBounded
          ? content.slice(0, LARGE_MARKDOWN_PREVIEW_LENGTH)
          : content}
      </ReactMarkdown>
    </div>
  );
}
