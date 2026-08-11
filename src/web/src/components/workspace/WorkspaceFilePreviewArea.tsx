import { useEffect, useRef, type CSSProperties } from "react";
import type { SessionFileChange, WorkspaceFileContent } from "../../types/backend";
import type { WorkspaceFileSelection } from "../../utils/workspaceFileReferences";
import WorkspaceMarkdownPreview from "./preview/WorkspaceMarkdownPreview";

export type WorkspacePreviewTab =
  | (WorkspaceFileContent & {
      previewType: "file";
      selection: WorkspaceFileSelection | null;
    })
  | {
      previewType: "file-placeholder";
      path: string;
      name: string;
    }
  | {
      previewType: "terminal";
      path: string;
      name: string;
      scopeLabel?: string;
      terminalId: string;
      attachUrl: string;
    }
  | {
      previewType: "browser";
      path: string;
      name: string;
      scopeLabel?: string;
      browserId: string;
      attachUrl: string;
    }
  | {
      previewType: "session-diff";
      path: string;
      name: string;
      change: SessionFileChange;
      changesetLabel: string;
    };

interface WorkspaceFilePreviewAreaProps {
  context: "files" | "changes";
  visible: boolean;
  flexRatio: number;
  apiPort: number;
  workspaceId: string | null;
  workspaceName: string;
  sessionTitle: string;
  tabs: WorkspacePreviewTab[];
  activePath: string | null;
  loadingPath: string | null;
  error: string | null;
  editingPath: string | null;
  draftContent: string;
  savingPath: string | null;
  hasUnsavedEdit: boolean;
  markdownSourceVisible: boolean;
  onMarkdownSourceChange: (visible: boolean) => void;
  onBeginEdit: (path: string) => void;
  onDraftChange: (content: string) => void;
  onCancelEdit: () => void;
  onSaveEdit: () => Promise<void>;
  onOpenWorkspacePath: (path: string) => Promise<void>;
}

function lineRows(content: string): string[] {
  const normalized = content.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  return normalized.split("\n");
}

function diffLineKind(line: string): "add" | "remove" | "meta" | "context" {
  if (line.startsWith("+") && !line.startsWith("+++")) {
    return "add";
  }
  if (line.startsWith("-") && !line.startsWith("---")) {
    return "remove";
  }
  if (
    line.startsWith("@@") ||
    line.startsWith("---") ||
    line.startsWith("+++")
  ) {
    return "meta";
  }
  return "context";
}

function formatFileSize(size: number): string {
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

export default function WorkspaceFilePreviewArea({
  context,
  visible,
  flexRatio,
  apiPort,
  workspaceId,
  workspaceName,
  sessionTitle,
  tabs,
  activePath,
  loadingPath,
  error,
  editingPath,
  draftContent,
  savingPath,
  hasUnsavedEdit,
  markdownSourceVisible,
  onMarkdownSourceChange,
  onBeginEdit,
  onDraftChange,
  onCancelEdit,
  onSaveEdit,
  onOpenWorkspacePath,
}: WorkspaceFilePreviewAreaProps) {
  const editorRef = useRef<HTMLTextAreaElement | null>(null);
  const editorLineNumbersRef = useRef<HTMLDivElement | null>(null);
  const activeTab = tabs.find((tab) => tab.path === activePath) ?? tabs[0] ?? null;
  const activeDiffLines =
    activeTab?.previewType === "session-diff"
      ? lineRows(activeTab.change.diff_text)
      : [];
  const diffLineNumberWidth = Math.max(2, String(activeDiffLines.length).length);
  const showLoadingEmpty = loadingPath &&
    (!activeTab || activeTab.previewType === "file-placeholder");
  const editingActiveFile = activeTab?.previewType === "file" &&
    editingPath === activeTab.path;
  const activeEditorContent = activeTab?.previewType === "file"
    ? editingActiveFile ? draftContent : activeTab.content
    : "";
  const activeEditorLines = lineRows(activeEditorContent);
  const markdownActiveFile = activeTab?.previewType === "file" &&
    activeTab.language === "markdown";

  useEffect(() => {
    if (activeTab?.previewType !== "file" || !activeTab.selection) {
      return;
    }
    const editor = editorRef.current;
    if (!editor) {
      return;
    }
    const lines = lineRows(activeTab.content);
    const start = lines
      .slice(0, activeTab.selection.startLine - 1)
      .reduce((offset, line) => offset + line.length + 1, 0);
    const end = lines
      .slice(0, activeTab.selection.endLine)
      .reduce((offset, line) => offset + line.length + 1, 0) - 1;
    editor.setSelectionRange(start, Math.max(start, end));
    editor.scrollTop = Math.max(0, (activeTab.selection.startLine - 3) * 20);
  }, [activeTab]);

  useEffect(() => {
    if (editingActiveFile) {
      editorRef.current?.focus();
    }
  }, [editingActiveFile]);

  return (
    <section
      className={`workspace-preview-panel${visible ? "" : " preserve-mounted-hidden"}`}
      hidden={!visible}
      style={{ flexBasis: 0, flexGrow: flexRatio }}
      aria-label={context === "changes" ? "更改代码预览区" : "文件代码预览区"}
      data-bt-surface="workspace"
    >
      <div className="workspace-preview-content">
        {activeTab?.previewType === "file" ? (
          <div className="workspace-preview-toolbar workspace-file-preview-toolbar">
            <div className="workspace-preview-title-group">
              <span className="workspace-preview-title" title={activeTab.path}>
                {activeTab.name}
              </span>
              <span
                className="workspace-preview-scope"
                title={`${workspaceName || "当前工作区"} · ${sessionTitle || "当前会话"}`}
              >
                {context === "changes" ? "会话变更" : workspaceName || "当前工作区"} · {sessionTitle || "当前会话"}
              </span>
            </div>
            <div className="workspace-preview-toolbar-actions" aria-label="文件预览操作">
              <span className="workspace-preview-meta">
                {activeTab.language} · {formatFileSize(activeTab.size)}
              </span>
              {activeTab.language === "markdown" ? (
                <div className="workspace-preview-mode-switch" role="group" aria-label="Markdown 查看方式">
                  <button
                    type="button"
                    className={markdownSourceVisible ? "active" : ""}
                    aria-pressed={markdownSourceVisible}
                    onClick={() => onMarkdownSourceChange(true)}
                  >
                    源码
                  </button>
                  <button
                    type="button"
                    className={!markdownSourceVisible ? "active" : ""}
                    aria-pressed={!markdownSourceVisible}
                    onClick={() => onMarkdownSourceChange(false)}
                  >
                    预览
                  </button>
                </div>
              ) : null}
              {editingActiveFile ? (
                <>
                  {hasUnsavedEdit ? (
                    <span className="workspace-preview-dirty-indicator">未保存</span>
                  ) : null}
                  <button
                    type="button"
                    disabled={savingPath === activeTab.path}
                    onClick={onCancelEdit}
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    className="workspace-preview-save-button"
                    disabled={!hasUnsavedEdit || savingPath === activeTab.path}
                    onClick={() => void onSaveEdit()}
                  >
                    <span
                      className={`codicon ${
                        savingPath === activeTab.path
                          ? "codicon-loading codicon-modifier-spin"
                          : "codicon-save"
                      }`}
                      aria-hidden="true"
                    />
                    {savingPath === activeTab.path ? "保存中" : "保存"}
                  </button>
                </>
              ) : null}
            </div>
          </div>
        ) : null}
        {activeTab?.previewType === "file" ? (
          <>
            {markdownActiveFile && !markdownSourceVisible ? (
              <WorkspaceMarkdownPreview
                apiPort={apiPort}
                workspaceId={workspaceId}
                path={activeTab.path}
                content={activeEditorContent}
                onOpenWorkspacePath={onOpenWorkspacePath}
              />
            ) : <div className="workspace-preview-text-editor-shell">
              <div
                ref={editorLineNumbersRef}
                className="workspace-preview-text-editor-line-numbers"
                aria-hidden="true"
                style={{ width: `${Math.max(2, String(activeEditorLines.length).length) + 2}ch` }}
              >
                {activeEditorLines.map((_, index) => (
                  <span key={index}>{index + 1}</span>
                ))}
              </div>
              <textarea
                ref={editorRef}
                className="workspace-preview-text-editor"
                aria-label={`编辑 ${activeTab.path}`}
                value={activeEditorContent}
                readOnly={!editingActiveFile}
                spellCheck={false}
                onPointerDown={() => {
                  if (!editingActiveFile) {
                    onBeginEdit(activeTab.path);
                  }
                }}
                onScroll={(event) => {
                  if (editorLineNumbersRef.current) {
                    editorLineNumbersRef.current.scrollTop = event.currentTarget.scrollTop;
                  }
                }}
                onChange={(event) => onDraftChange(event.target.value)}
                onKeyDown={(event) => {
                  if ((event.ctrlKey || event.metaKey) && event.key === "s") {
                    event.preventDefault();
                    if (hasUnsavedEdit && savingPath !== activeTab.path) {
                      void onSaveEdit();
                    }
                    return;
                  }
                  if (!editingActiveFile || event.key !== "Tab") {
                    return;
                  }
                  event.preventDefault();
                  const editor = event.currentTarget;
                  const start = editor.selectionStart;
                  const end = editor.selectionEnd;
                  onDraftChange(
                    `${draftContent.slice(0, start)}\t${draftContent.slice(end)}`,
                  );
                  window.requestAnimationFrame(() => {
                    editor.selectionStart = start + 1;
                    editor.selectionEnd = start + 1;
                  });
                }}
              />
            </div>}
          </>
        ) : activeTab?.previewType === "terminal" ? (
          <iframe
            className="workspace-preview-terminal-frame"
            src={activeTab.attachUrl}
            title={`终端 ${activeTab.terminalId}`}
          />
        ) : activeTab?.previewType === "browser" ? (
          <iframe
            className="workspace-preview-terminal-frame workspace-preview-browser-frame"
            src={activeTab.attachUrl}
            title={`浏览器 ${activeTab.browserId}`}
          />
        ) : activeTab?.previewType === "session-diff" ? (
          <div className="workspace-preview-diff-scroll">
              <div
                className="workspace-preview-code-table workspace-preview-diff-table"
                style={{ "--preview-line-number-width": `${diffLineNumberWidth}ch` } as CSSProperties}
              >
                {activeDiffLines.map((line, index) => (
                  <div
                    className={`workspace-preview-line workspace-preview-diff-line diff-${diffLineKind(line)}`}
                    key={`${index}-${line.length}`}
                  >
                    <span className="workspace-preview-line-number">{index + 1}</span>
                    <code className="workspace-preview-line-code">
                      {line.length > 0 ? line : "\u00A0"}
                    </code>
                  </div>
                ))}
              </div>
          </div>
        ) : (
          <div className="workspace-preview-empty">
            <span className="workspace-preview-empty-icon">▱</span>
            <span>{showLoadingEmpty ? "正在读取文件..." : "从右侧文件树选择文件预览"}</span>
          </div>
        )}

        {loadingPath ? (
          <div className="workspace-preview-status">正在读取 {loadingPath}</div>
        ) : null}
        {error ? (
          <div className="workspace-preview-error" role="alert">
            {error}
          </div>
        ) : null}
      </div>
    </section>
  );
}
