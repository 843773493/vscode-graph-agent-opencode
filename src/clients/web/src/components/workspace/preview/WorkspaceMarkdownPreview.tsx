import { lazy, Suspense, useState } from "react";

const WorkspaceMarkdownRenderer = lazy(() => import("./WorkspaceMarkdownRenderer"));

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
      <Suspense fallback={(
        <div className="workspace-markdown-preview-loading" role="status">
          正在加载 Markdown 预览…
        </div>
      )}>
        <WorkspaceMarkdownRenderer
          apiPort={apiPort}
          workspaceId={workspaceId}
          path={path}
          content={documentBounded
            ? content.slice(0, LARGE_MARKDOWN_PREVIEW_LENGTH)
            : content}
          onOpenWorkspacePath={onOpenWorkspacePath}
        />
      </Suspense>
    </div>
  );
}
